# engines_manga.py - Motor de traduccion de manga in-place (manga-image-translator)
# Extrae paginas del PDF, ejecuta el motor en modo lote (resumible), traduce paginas de
# solo texto con el sistema de libros (MinerU + OPUS) y une el PDF final (mixto).
import os
import re
import shutil
import subprocess

import pymupdf

from .config import cfg
from .jobs import jobs


def _precalentar_lmstudio(job_id, modelo):
    """Carga el modelo en LM Studio antes del motor (evita 400 'No models loaded')."""
    import time

    import requests
    url = cfg.lmstudio_url.rstrip('/') + '/chat/completions'
    payload = {'model': modelo,
               'messages': [{'role': 'user', 'content': 'Hola'}],
               'max_tokens': 5}
    for intento in range(4):
        try:
            r = requests.post(url, json=payload, timeout=180)
            if r.status_code == 200:
                jobs.log(job_id, '[manga] Modelo LM Studio listo')
                return True
            jobs.log(job_id, f'[manga] LM Studio aviso {r.status_code}: {r.text[:150]}')
        except Exception as e:
            jobs.log(job_id, f'[manga] LM Studio no responde: {e}')
        if intento < 3:
            jobs.log(job_id, '[manga] Reintentando carga del modelo...')
            time.sleep(15)
    return False


def _resolver_modelo(job_id, solicitado):
    """Resuelve el modelo real a usar: el solicitado si esta en el catalogo de LM Studio,
    si no, el primer candidato disponible de la cadena de respaldo."""
    import requests
    cadena = [solicitado or cfg.manga_modelo_default,
              'hermes-3-llama-3.1-8b',
              'google/gemma-4-e4b',
              'ibm-granite_granite-4.1-8b']
    disponibles = []
    try:
        r = requests.get(cfg.lmstudio_url.rstrip('/') + '/models', timeout=15)
        disponibles = [m.get('id') for m in r.json().get('data', [])]
    except Exception:
        pass
    for c in cadena:
        if c and c in disponibles:
            return c
    if disponibles:
        return disponibles[0]
    return cadena[0]


def extraer_paginas(pdf, dir_paginas, job_id=None, dpi=None):
    """Extrae las paginas del PDF como PNG (pagina_001.png, ...). Devuelve el numero de paginas."""
    dpi = dpi or cfg.manga_dpi
    os.makedirs(dir_paginas, exist_ok=True)
    doc = pymupdf.open(pdf)
    n = doc.page_count
    for i in range(n):
        if job_id:
            j = jobs.get(job_id)
            if j and j.get('cancelar'):
                doc.close()
                return {'cancelado': True, 'paginas': n}
        pagina = doc.load_page(i)
        pix = pagina.get_pixmap(dpi=dpi)
        pix.save(os.path.join(dir_paginas, f'pagina_{i + 1:03d}.png'))
        if job_id:
            jobs.set(job_id, progreso=int(8 * (i + 1) / n), paso=f'Extrayendo pagina {i + 1}/{n}')
    doc.close()
    return {'cancelado': False, 'paginas': n}


def _parsear_paginas(texto, total=None):
    """Parsea '1, 5-6, 9, 20-26' -> set de numeros de pagina. Total: valida rango."""
    if not texto or not texto.strip():
        return set()
    paginas = set()
    for parte in texto.replace(';', ',').split(','):
        parte = parte.strip()
        if not parte:
            continue
        if '-' in parte:
            a, b = parte.split('-', 1)
            try:
                ini, fin = int(a.strip()), int(b.strip())
            except ValueError:
                raise ValueError(f'Rango invalido: "{parte}" (usa ej. 5-6)')
            paginas.update(range(ini, fin + 1))
        else:
            try:
                paginas.add(int(parte))
            except ValueError:
                raise ValueError(f'Pagina invalida: "{parte}" (usa ej. 1, 5-6, 9)')
    if total and any(p < 1 or p > total for p in paginas):
        raise ValueError(f'Paginas fuera de rango (1-{total})')
    return paginas


def _preparar_entrada_motor(dir_paginas, dir_motor, omitir):
    """Copia al dir del motor solo las paginas a traducir (excluye omitidas/texto).
    Limpia el dir primero: evita paginas fantasma de runs anteriores con el mismo nombre."""
    if os.path.isdir(dir_motor):
        shutil.rmtree(dir_motor)
    os.makedirs(dir_motor, exist_ok=True)
    n = 0
    for fn in sorted(os.listdir(dir_paginas)):
        if not fn.lower().endswith('.png'):
            continue
        num = int(''.join(c for c in fn if c.isdigit()) or 0)
        if num in omitir:
            continue
        shutil.copy(os.path.join(dir_paginas, fn), os.path.join(dir_motor, fn))
        n += 1
    return n


def _config_con_fso(config, fso, dir_job):
    """Copia la config del motor con render.font_size_offset ajustado (config por job).
    SIEMPRE regenera: la config base puede cambiar (bt, merge...) y la copia debe
    reflejar el estado actual, no reutilizar copias viejas del mismo nombre."""
    import json
    ruta = os.path.join(dir_job, f'config_fso{fso}.json')
    with open(config, encoding='utf-8') as f:
        c = json.load(f)
    c.setdefault('render', {})['font_size_offset'] = fso
    with open(ruta, 'w', encoding='utf-8') as f:
        json.dump(c, f, ensure_ascii=False, indent=2)
    return ruta


def correr_motor(job_id, dir_paginas, dir_motor, dir_salida, modelo, total_paginas,
                 idioma='ja', motor='llm', fso=0, deepseek_key='', deepseek_model=''):
    """Ejecuta manga-image-translator en modo lote.
    Resumible: sin --overwrite el motor salta las paginas ya traducidas.
    motor: 'llm' (custom_openai -> LM Studio) | 'opus' (OPUS-MT local, solo en/ru)
           | 'deepseek' (custom_openai -> API de DeepSeek).
    fso: font_size_offset por job (0 = usar el de la config base)."""
    if motor == 'opus':
        config = cfg.manga_config_opus
    elif idioma == 'ja':
        config = cfg.manga_config_ja
    else:
        config = cfg.manga_config_ocr
    if fso:
        config = _config_con_fso(config, fso, os.path.dirname(dir_salida))
    os.makedirs(dir_salida, exist_ok=True)
    env = dict(os.environ)
    if motor == 'deepseek':
        env['CUSTOM_OPENAI_API_BASE'] = cfg.manga_deepseek_base
        env['CUSTOM_OPENAI_API_KEY'] = deepseek_key or cfg.manga_deepseek_key
        env['CUSTOM_OPENAI_MODEL'] = deepseek_model or cfg.manga_deepseek_model
    else:
        env['CUSTOM_OPENAI_API_BASE'] = cfg.lmstudio_url
        env['CUSTOM_OPENAI_API_KEY'] = 'lmstudio'
        env['CUSTOM_OPENAI_MODEL'] = modelo or cfg.manga_modelo_default
    if motor == 'opus':
        env['OPUS_MODEL_DIR'] = cfg.opus_model_dir
    cmd = [
        cfg.manga_python, '-m', 'manga_translator', 'local', '-v',
        '-i', dir_motor,
        '-o', dir_salida,
        '--config-file', config,
        '--model-dir', cfg.manga_models,
    ]
    if cfg.manga_context_size > 0:
        cmd += ['--context-size', str(cfg.manga_context_size)]
    if cfg.manga_pre_dict and os.path.isfile(cfg.manga_pre_dict):
        cmd += ['--pre-dict', cfg.manga_pre_dict]
    # fuente uniforme Anime Ace para todos los idiomas: las traducciones son espanol
    # (latino) y Anime Ace tiene los glifos completos; los kanji residuales se filtran
    fuente = cfg.manga_font
    if fuente and os.path.isfile(fuente):
        cmd += ['--font-path', fuente]
    flags = 0
    if os.name == 'nt':
        flags = subprocess.CREATE_NO_WINDOW
    proc = subprocess.Popen(cmd, cwd=cfg.manga_repo, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding='utf-8', errors='replace',
                            creationflags=flags)
    jobs.registrar_proceso(job_id, proc)
    for linea in proc.stdout:
        linea = linea.rstrip().replace('\r', '')
        if linea.strip():
            jobs.log(job_id, linea[:400])
        if '[local] Saving' in linea or 'Skipping as already translated' in linea:
            hechos = len([f for f in os.listdir(dir_salida) if f.lower().endswith('.png')])
            jobs.set(job_id, progreso=min(88, 10 + int(78 * hechos / max(1, total_paginas))),
                     paso=f'Traduciendo pagina {hechos}/{total_paginas}')
    proc.wait()
    return proc.returncode


# ---------- paginas de solo texto: sistema de libros (MinerU + OPUS) ----------

def _mineru_a_markdown(pdf_sub, dir_salida_md, job_id):
    """PDF de paginas de texto -> markdown con MinerU (subproceso, igual que el sistema de libros)."""
    os.makedirs(dir_salida_md, exist_ok=True)
    cmd = [cfg.mineru_exe, '-p', pdf_sub, '-o', dir_salida_md,
           '-b', 'pipeline', '-m', 'auto']
    proc = subprocess.Popen(cmd, env=dict(os.environ, PYTHONPATH=''),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding='utf-8', errors='replace',
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    jobs.registrar_proceso(job_id, proc)
    for linea in proc.stdout:
        linea = linea.rstrip().replace('\r', '')
        if linea.strip():
            jobs.log(job_id, '[mineru] ' + linea[:300])
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f'MinerU fallo (exit {proc.returncode})')
    mds = []
    for root, _, files in os.walk(dir_salida_md):
        for f in files:
            if f.lower().endswith('.md'):
                mds.append(os.path.join(root, f))
    if not mds:
        raise RuntimeError('MinerU no produjo markdown')
    return mds[0]


def _traducir_md_opus(md_orig, md_trad, job_id):
    """Traduce el markdown con OPUS en->es (bridge, subproceso persistente)."""
    import json
    texto = open(md_orig, encoding='utf-8').read()
    parrafos = [p.strip() for p in re.split(r'\n\s*\n', texto) if p.strip()]
    # el bridge traduce lotes; aqui se usan parrafos como unidades
    trads = []
    for i in range(0, len(parrafos), 32):
        lote = parrafos[i:i + 32]
        payload = json.dumps({'id': 1, 'textos': lote})
        r = subprocess.run(
            [cfg.python_mineru, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                             'bridge_traductor.py'), cfg.opus_model_dir],
            input=payload, capture_output=True, text=True, encoding='utf-8',
            timeout=600, env=dict(os.environ, PYTHONPATH=''))
        salida = json.loads(r.stdout.strip().splitlines()[-1] if r.stdout else '{}')
        trads.extend(salida.get('traducciones', []))
        jobs.set(job_id, progreso=min(97, 92 + int(5 * (i + len(lote)) / max(1, len(parrafos)))),
                 paso=f'Traduciendo texto (libros) {min(i + 32, len(parrafos))}/{len(parrafos)}')
    with open(md_trad, 'w', encoding='utf-8') as f:
        f.write('\n\n'.join(trads))
    return md_trad


def _render_md_paginas(md_trad, n_paginas, pdf_texto):
    """Renderiza el markdown traducido en exactamente n_paginas de texto (PDF).
    Divide el contenido en partes iguales; titulos en negrita, listas con viñetas."""
    texto = open(md_trad, encoding='utf-8').read()
    # limpiar artefactos de markdown: imagenes, links, negritas, codigo inline
    texto = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', texto)      # ![alt](url)
    texto = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', texto)   # [text](url)
    texto = re.sub(r'\*\*([^*]+)\*\*', r'\1', texto)          # **bold**
    texto = re.sub(r'`([^`]+)`', r'\1', texto)                # `code`
    bloques = [b.strip() for b in re.split(r'\n\s*\n', texto) if b.strip()]
    if not bloques:
        bloques = ['(pagina sin texto)']
    # distribuir bloques en n_paginas de forma equilibrada
    partes = [[] for _ in range(n_paginas)]
    idx = 0
    for b in bloques:
        partes[idx % n_paginas].append(b)
        idx += 1
    doc = pymupdf.open()
    for parte in partes:
        pagina = doc.new_page(width=595, height=842)  # A4
        y = 60
        margen = 70
        ancho = 595 - 2 * margen
        for b in parte:
            if b.startswith('#'):
                nivel = len(b) - len(b.lstrip('#'))
                titulo = b.lstrip('#').strip()
                tam = 20 if nivel == 1 else 16
                y = _insertar_texto(pagina, titulo, margen, y, ancho, tam, bold=True) + 10
            elif b.startswith('-') or b.startswith('*'):
                y = _insertar_texto(pagina, '• ' + b.lstrip('-* ').strip(),
                                    margen, y, ancho, 11) + 6
            elif b.startswith('```'):
                y = _insertar_texto(pagina, b.strip('`').strip(), margen, y,
                                    ancho, 9, mono=True) + 8
            else:
                y = _insertar_texto(pagina, b, margen, y, ancho, 11) + 10
            if y > 800:
                break
    doc.save(pdf_texto)
    doc.close()
    return n_paginas


def _insertar_texto(pagina, texto, x, y, ancho, tam, bold=False, mono=False):
    fontname = 'Courier' if mono else ('Helvetica-Bold' if bold else 'Helvetica')
    tw = pymupdf.TextWriter(pagina.rect)
    # wrap simple por caracteres
    lineas = []
    linea_actual = ''
    for c in texto:
        linea_actual += c
        if len(linea_actual) >= int(ancho / (tam * 0.5)):
            lineas.append(linea_actual)
            linea_actual = ''
    if linea_actual:
        lineas.append(linea_actual)
    for ln in lineas[:30]:
        pagina.insert_text((x, y), ln, fontname=fontname, fontsize=tam)
        y += tam * 1.35
    return y


def _png_a_jpeg(ruta):
    """Convierte PNG -> JPEG temporal (calidad 88): el PDF final queda ~10x mas pequeno,
    porque Flate comprime mal el arte manga (pixeles sin comprimir = GB)."""
    from PIL import Image
    tmp = ruta + '.jpg'
    img = Image.open(ruta)
    if img.mode != 'RGB':
        img = img.convert('RGB')
    img.save(tmp, 'JPEG', quality=88)
    return tmp


def unir_pdf(dir_traducidas, pdf_final, dir_paginas=None, omitir=None,
             paginas_texto=None, pdf_texto=None):
    """Une el PDF final (mixto): paginas manga traducidas, originales para las
    omitidas, y paginas de texto del sistema de libros donde corresponda.
    Las imagenes se incrustan como JPEG (calidad 88) para que el PDF sea manejable."""
    omitir = omitir or set()
    paginas_texto = paginas_texto or {}
    doc_texto = pymupdf.open(pdf_texto) if pdf_texto and os.path.isfile(pdf_texto) else None
    archivos = []
    if os.path.isdir(dir_traducidas):
        archivos = sorted((f for f in os.listdir(dir_traducidas) if f.lower().endswith('.png')),
                          key=lambda f: int(''.join(c for c in f if c.isdigit()) or 0))
    if not archivos and not omitir and not paginas_texto:
        if doc_texto:
            doc_texto.close()
        return 0
    total = len(archivos) + len(omitir) + len(paginas_texto)
    nums_traducidas = {int(''.join(c for c in f if c.isdigit()) or 0) for f in archivos}
    nums_texto = sorted(paginas_texto)
    idx_texto = 0
    doc = pymupdf.open()
    temporales = []
    try:
        for n in range(1, total + 1):
            if n in omitir and dir_paginas:
                ruta = os.path.join(dir_paginas, f'pagina_{n:03d}.png')
                if os.path.isfile(ruta):
                    tmp = _png_a_jpeg(ruta)
                    temporales.append(tmp)
                    rect = pymupdf.open(tmp)[0].rect
                    p = doc.new_page(width=rect.width, height=rect.height)
                    p.insert_image(rect, filename=tmp)
                    continue
            if n in nums_texto and doc_texto and idx_texto < doc_texto.page_count:
                rect = doc_texto[idx_texto].rect
                p = doc.new_page(width=rect.width, height=rect.height)
                p.show_pdf_page(rect, doc_texto, idx_texto)
                idx_texto += 1
                continue
            if n in nums_traducidas:
                ruta = os.path.join(dir_traducidas, f'pagina_{n:03d}.png')
                if os.path.isfile(ruta):
                    tmp = _png_a_jpeg(ruta)
                    temporales.append(tmp)
                    rect = pymupdf.open(tmp)[0].rect
                    p = doc.new_page(width=rect.width, height=rect.height)
                    p.insert_image(rect, filename=tmp)
                    continue
            # caida: pagina vacia blanca
            doc.new_page(width=595, height=842)
    finally:
        for t in temporales:
            try:
                os.remove(t)
            except OSError:
                pass
    if doc_texto:
        doc_texto.close()
    doc.save(pdf_final, garbage=4, deflate=True)
    doc.close()
    return total
