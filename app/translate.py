# translate.py - Traduccion a espanol con motores locales.
# - nllb: NLLB-200-distilled-600M via CTranslate2 (rapido, por lotes)
# - llm : LM Studio /v1/chat/completions (calidad, por chunks con placeholders)
# Estrategia comun: el texto se divide en items ('txt' traducible | 'ph' inline
# protegido | 'raw' intacto). Los items protegidos (codigo, LaTeX, imagenes,
# URLs, pipes de tablas, prefijos #) NUNCA se envian al modelo (NLLB) o se
# ocultan con placeholders {{P1}} (LLM), garantizando estructura intacta.
import json
import os
import re
import shutil
import subprocess
import threading

import requests

from .config import cfg
from .jobs import jobs

SISTEMA_TRADUCTOR = (
    'Eres un traductor profesional al espanol. Traduce SOLO el texto del usuario. '
    'Reglas estrictas: 1) Conserva EXACTAMENTE los marcadores {{P1}}, {{P2}}, etc. '
    'sin modificarlos ni reordenarlos. 2) No agregues ni elimines lineas. '
    '3) Respeta la estructura markdown (encabezados #, listas, citas, tablas con |). '
    '4) Devuelve unicamente la traduccion, sin comentarios ni explicaciones.'
)

INLINE_PATTERNS = [
    (re.compile(r'(`[^`\n]+`)'), 'codigo'),
    (re.compile(r'(\$\$[^$\n]*\$\$|\$[^$\n]+\$)'), 'math'),
    (re.compile(r'(!\[[^\]]*\]\([^)]*\))'), 'imagen'),
    (re.compile(r'(https?://\S+)'), 'url'),
]
_TABLA_SEP = re.compile(r'^:?-+:?$')


# ---------- segmentacion por lineas ----------
_TAGS_HTML_MULTI = {'table', 'div', 'math', 'span', 'sub', 'sup', 'ul', 'ol', 'li', 'pre'}
_TAGS_HTML_LINEA = {'img', 'br', 'td', 'tr', 'th', 'p', 'caption', 'b', 'i', 'em', 'strong', 'hr', 'a'}


def _es_html(linea):
    low = linea.strip().lower()
    if low.startswith('</'):
        return True
    for t in _TAGS_HTML_MULTI | _TAGS_HTML_LINEA:
        if low.startswith('<' + t) and (len(low) == len('<' + t) or low[len('<' + t)] in ' >'):
            return True
    return False


def _segmentar(texto):
    """Lista de segmentos (tipo, contenido): protegido|texto|heading|tabla.
    Protege: bloques de codigo (fence en CUALQUIER posicion de la linea),
    HTML (tablas, comentarios, etiquetas), LaTeX display, imagenes, URLs."""
    segs = []
    lineas = texto.split('\n')
    i, n = 0, len(lineas)
    while i < n:
        linea = lineas[i]
        s = linea.strip()
        # --- bloque de codigo: fence en cualquier posicion ---
        pos_fence = linea.find('```')
        if pos_fence >= 0:
            if pos_fence > 0:
                segs.append(('texto', linea[:pos_fence].rstrip()))
            j = i + 1
            while j < n and '```' not in lineas[j]:
                j += 1
            if j >= n:
                j = n - 1
            bloque = linea[pos_fence:] + '\n' + '\n'.join(lineas[i + 1:j + 1])
            segs.append(('protegido', bloque))
            i = j + 1
        # --- tabla html pegada al final de una linea de texto ---
        elif (pos_tabla := linea.lower().find('<table')) > 0:
            segs.append(('texto', linea[:pos_tabla].rstrip()))
            contenido = linea[pos_tabla:]
            j = i
            while '</table' not in contenido.lower() and j + 1 < n:
                j += 1
                contenido += '\n' + lineas[j]
            segs.append(('protegido', contenido))
            i = j + 1
        # --- math display ---
        elif '$$' in linea:
            contenido = linea
            j = i
            if linea.count('$$') < 2:
                j = i + 1
                while j < n and '$$' not in lineas[j]:
                    contenido += '\n' + lineas[j]
                    j += 1
                if j < n:
                    contenido += '\n' + lineas[j]
            segs.append(('protegido', contenido))
            i = j + 1
        # --- comentario html (multilinea) ---
        elif s.startswith('<!--'):
            contenido = linea
            j = i
            while '-->' not in contenido and j + 1 < n:
                j += 1
                contenido += '\n' + lineas[j]
            segs.append(('protegido', contenido))
            i = j + 1
        # --- html: cierre pegado a texto (ej. </table>## TITULO) ---
        elif s.startswith('</'):
            m = re.match(r'^(</[^>]+>)', s)
            if m:
                segs.append(('protegido', m.group(1)))
                resto = s[len(m.group(1)):]
                if resto:
                    if re.match(r'^#{1,6}\s', resto):
                        segs.append(('heading', resto))
                    else:
                        segs.append(('texto', resto))
            else:
                segs.append(('protegido', linea))
            i += 1
        # --- html: bloques multilinea (table/div/math/...) ---
        elif _es_html(linea):
            low = s.lower()
            tag = next((t for t in _TAGS_HTML_MULTI if low.startswith('<' + t)), None)
            if tag and '</' + tag not in linea.lower():
                contenido = linea
                j = i
                while j + 1 < n and '</' + tag not in lineas[j + 1].lower():
                    j += 1
                    contenido += '\n' + lineas[j]
                if j + 1 < n:
                    j += 1
                    contenido += '\n' + lineas[j]
                segs.append(('protegido', contenido))
                i = j + 1
            else:
                segs.append(('protegido', linea))
                i += 1
        # --- imagen en su propia linea ---
        elif s.startswith('![') and '](' in s:
            segs.append(('protegido', linea))
            i += 1
        # --- encabezado ---
        elif re.match(r'^#{1,6}\s', linea):
            segs.append(('heading', linea))
            i += 1
        # --- tabla markdown ---
        elif s.startswith('|') and s.endswith('|'):
            segs.append(('tabla', linea))
            i += 1
        else:
            segs.append(('texto', linea))
            i += 1
    return segs


def _dividir_inline(texto):
    """Divide texto en items ('txt', fragmento) | ('ph', contenido) para spans inline."""
    if not texto.strip():
        return [('raw', texto)] if texto else []
    posiciones = []
    for rx, _tipo in INLINE_PATTERNS:
        for m in rx.finditer(texto):
            posiciones.append((m.start(), m.end()))
    if not posiciones:
        return [('txt', texto)]
    posiciones.sort()
    items = []
    ultimo = 0
    for a, b in posiciones:
        if a > ultimo:
            items.append(('txt', texto[ultimo:a]))
        items.append(('ph', texto[a:b]))
        ultimo = b
    if ultimo < len(texto):
        items.append(('txt', texto[ultimo:]))
    return items


def _items_de_unidades(unidades):
    """Unidades -> items ('txt'|'ph'|'raw')."""
    items = []
    for tipo, payload in unidades:
        if tipo == 'protegido':
            items.append(('raw', payload))
        elif tipo == 'heading':
            m = re.match(r'^(#{1,6})\s*(.*)$', payload)
            if not m:
                items.append(('raw', payload))
                continue
            items.append(('raw', m.group(1) + ' '))
            items.extend(_dividir_inline(m.group(2)))
        elif tipo == 'tabla':
            celdas = payload.split('|')
            items.append(('raw', celdas[0] + '|'))
            for celda in celdas[1:-1]:
                if not celda.strip() or _TABLA_SEP.match(celda.strip()):
                    items.append(('raw', celda + '|'))
                else:
                    items.extend(_dividir_inline(celda))
                    items.append(('raw', '|'))
            if len(celdas) > 1:
                items.append(('raw', '|' + celdas[-1]))
        else:
            items.extend(_dividir_inline(payload))
            items.append(('raw', '\n'))
    return items


def _a_unidades(segs):
    unidades = []
    buf = []

    def flush():
        if buf:
            unidades.append(('texto', '\n'.join(buf)))
            buf.clear()

    for tipo, contenido in segs:
        if tipo == 'texto':
            buf.append(contenido)
        else:
            flush()
            unidades.append((tipo, contenido))
    flush()
    return unidades


# ---------- deteccion de idioma ----------
def detectar_idioma(job_id, md):
    codigo = (
        'import sys;'
        'from fast_langdetect import detect_language;'
        'print(detect_language(" ".join(open(sys.argv[1], encoding="utf-8").read().split())[:2000]))'
    )
    try:
        r = subprocess.run([cfg.python_mineru, '-c', codigo, md],
                           capture_output=True, text=True, encoding='utf-8',
                           timeout=120, env={**os.environ, 'PYTHONPATH': ''})
        salida = (r.stdout or '').strip().upper()
        return salida[:2] if salida else None
    except Exception as e:
        jobs.log(job_id, f'[idioma] no detectado: {e}')
        return None


# ---------- motores ----------
class MotorTorch:
    """Traduccion rapida con transformers (Marian/NLLB) via subproceso persistente
    en mineru-env (tiene torch+transformers). CTranslate2 quedo descartado: sus
    conversiones de Marian generan bucles de repeticion en esta CPU (verificado)."""

    def __init__(self, model_dir=None, job_id=None, total_lotes=None):
        import subprocess
        self.dir = model_dir or cfg.opus_model_dir
        self.job_id = job_id
        self.total_lotes = total_lotes
        self._subprocess = subprocess
        env = dict(os.environ)
        env['PYTHONPATH'] = ''
        env['PYTHONIOENCODING'] = 'utf-8'
        self.proc = subprocess.Popen(
            [cfg.python_mineru, os.path.join(cfg.base, 'app', 'bridge_traductor.py'), self.dir],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=env, text=True, encoding='utf-8', errors='replace',
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self._contador = 0

    def traducir_batch(self, textos, batch=32):
        resultados = []
        total = self.total_lotes or max(1, -(-len(textos) // batch))
        for i in range(0, len(textos), batch):
            self._contador += 1
            lote = textos[i:i + batch]
            self.proc.stdin.write(json.dumps({'id': self._contador, 'textos': lote}) + '\n')
            self.proc.stdin.flush()
            linea = self.proc.stdout.readline()
            resp = json.loads(linea)
            if resp.get('error'):
                raise RuntimeError(f'bridge traductor: {resp["error"]}')
            resultados.extend(resp['traducciones'])
            if self.job_id:
                jobs.set(self.job_id,
                         progreso=min(85, int(85 * self._contador / total)),
                         paso=f'OPUS traduciendo lote {self._contador}/{total}')
        return resultados

    def cerrar(self):
        try:
            self.proc.stdin.close()
            self.proc.terminate()
        except Exception:
            pass


class MotorLLM:
    def __init__(self, modelo):
        self.modelo = modelo or cfg.modelo_llm_default or ''

    def traducir(self, texto, timeout=180):
        payload = {
            'model': self.modelo,
            'messages': [
                {'role': 'system', 'content': SISTEMA_TRADUCTOR},
                {'role': 'user', 'content': texto},
            ],
            'temperature': 0.2,
            'max_tokens': min(8192, int(len(texto) * 2.2) + 256),
        }
        r = requests.post(f'{cfg.lmstudio_url}/chat/completions', json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json()['choices'][0]['message']['content'].strip()


# ---------- pipeline OPUS (rapido, por lotes) ----------
_URL_SPLIT = re.compile(r'(https?://\S+|!\[[^\]]*\]\([^)]*\)|<math>.*?</math>|<sub>.*?</sub>|<sup>.*?</sup>)')


def _proteger_urls(texto):
    """Divide un parrafo en items ('txt') y spans protegidos ('raw') para el motor OPUS:
    URLs, imagenes markdown y etiquetas HTML inline no se traducen."""
    partes = _URL_SPLIT.split(texto)
    items = []
    for i, p in enumerate(partes):
        if not p:
            continue
        items.append(('raw', p) if i % 2 == 1 else ('txt', p))
    return items


def _cortar_parrafo(texto, max_palabras=280):
    """Divide parrafos muy largos en piezas <= max_palabras.
    Marian (OPUS-MT) limita la entrada a ~512 tokens; párrafos mayores
    causan 'index out of range in self' en generate()."""
    palabras = texto.split(' ')
    if len(palabras) <= max_palabras:
        return [texto]
    return [' '.join(palabras[i:i + max_palabras])
            for i in range(0, len(palabras), max_palabras)]


def _items_por_parrafo(segs):
    """Para OPUS: unidades por parrafo completo (mejor calidad que fragmentos).
    Protege bloques (code/math/img/comment), HTML, URLs e imagenes inline.
    Cada unidad queda separada por salto de linea ('' o '\n\n') para conservar
    la estructura del documento original."""
    items = []
    buf = []

    def flush():
        if buf:
            for pieza in _cortar_parrafo(' '.join(buf)):
                items.extend(_proteger_urls(pieza))
            items.append(('raw', '\n\n'))  # separador de parrafo
            buf.clear()

    for tipo, contenido in segs:
        if tipo == 'texto':
            if contenido.strip():
                buf.append(contenido.strip())
            else:
                flush()
        elif tipo == 'heading':
            flush()
            m = re.match(r'^(#{1,6})\s*(.*)$', contenido)
            if m:
                items.append(('raw', m.group(1) + ' '))
                if m.group(2).strip():
                    items.append(('txt', m.group(2).strip()))
                items.append(('raw', '\n\n'))
            else:
                items.append(('raw', contenido + '\n'))
        elif tipo == 'tabla':
            flush()
            items.append(('tabla', contenido))
        else:  # protegido
            flush()
            items.append(('raw', contenido.rstrip('\n') + '\n'))
    flush()
    return items


def _traducir_rapido(job_id, archivo_md, salida, forzar):
    texto = open(archivo_md, encoding='utf-8').read()
    unidades = _items_por_parrafo(_segmentar(texto))

    # tareas: parrafos + celdas de tabla (solo las >= 6 chars)
    tareas = []
    for i, (tipo, payload) in enumerate(unidades):
        if tipo == 'txt' and len(payload) >= 3:
            tareas.append(('txt', i, payload))
        elif tipo == 'tabla':
            celdas = payload.split('|')
            for nc, celda in enumerate(celdas[1:-1]):
                t = celda.strip()
                if len(t) >= 6 and not _TABLA_SEP.match(t):
                    tareas.append(('celda', i, nc, celda))

    resultados = {}
    if tareas:
        motor = MotorTorch(job_id=job_id, total_lotes=max(1, -(-len(tareas) // 32)))
        try:
            textos = [t[3] if t[0] == 'celda' else t[2] for t in tareas]
            traducidos = motor.traducir_batch(textos)
        finally:
            motor.cerrar()
        for t, trad in zip(tareas, traducidos):
            if t[0] == 'txt':
                resultados[('txt', t[1])] = trad
            else:
                resultados[('celda', t[1], t[2])] = trad

    partes = []
    total = len(unidades)
    for i, (tipo, payload) in enumerate(unidades):
        if tipo == 'txt':
            partes.append(resultados.get(('txt', i), payload))
        elif tipo == 'tabla':
            celdas = payload.split('|')
            nuevos = [celdas[0]]
            for nc, celda in enumerate(celdas[1:-1]):
                t = celda.strip()
                if len(t) >= 6 and not _TABLA_SEP.match(t):
                    nuevos.append(resultados.get(('celda', i, nc), celda))
                else:
                    nuevos.append(celda)
            nuevos.append(celdas[-1])
            partes.append('|'.join(nuevos) + '\n')
        else:
            partes.append(payload)
            if payload.startswith('http'):
                partes.append(' ')  # el modelo consume el espacio siguiente a la URL
        jobs.set(job_id, progreso=min(99, int(85 + 14 * (i + 1) / max(1, total))),
                 paso=f'OPUS ensamblando ({i + 1}/{total} unidades)')

    with open(salida, 'w', encoding='utf-8') as f:
        f.write(''.join(partes))
    return {'items': total, 'traducidos': len(tareas)}


# ---------- pipeline LLM (calidad, chunks con placeholders) ----------
_PH_RE = re.compile(r'\{\{P(\d+)\}\}')


def _traducir_llm(job_id, archivo_md, salida, modelo, chunk_chars, reanudar):
    texto = open(archivo_md, encoding='utf-8').read()
    items = _items_de_unidades(_a_unidades(_segmentar(texto)))

    # construir chunks serializando items
    chunks = []
    mapa_ph = {}  # id placeholder -> contenido protegido original
    actual = []
    peso = 0
    contador = 0
    for tipo, contenido in items:
        if tipo == 'txt':
            actual.append(contenido)
            peso += len(contenido)
        elif tipo == 'ph':
            contador += 1
            mapa_ph[contador] = contenido
            actual.append(f'{{{{P{contador}}}}}')
            peso += 8
        else:
            actual.append(contenido)
            peso += len(contenido)
        if peso >= chunk_chars:
            chunks.append(''.join(actual))
            actual, peso = [], 0
    if actual:
        chunks.append(''.join(actual))

    n = len(chunks)
    # resume: saltar chunks ya completados (persistidos en params) y reabrir en modo append
    inicio = 0
    if reanudar:
        params = jobs.get(job_id).get('params') or {}
        try:
            inicio = min(int(params.get('chunks_completados', 0) or 0), n)
        except (TypeError, ValueError):
            inicio = 0
        if inicio >= n:
            inicio = 0

    llm = MotorLLM(modelo)
    f = open(salida, 'a' if inicio else 'w', encoding='utf-8')
    try:
        for i in range(inicio, n):
            chunk = chunks[i]
            if jobs.get(job_id)['cancelar']:
                jobs.set(job_id, estado='cancelado', paso='Cancelado por el usuario (parcial resumible)')
                return {'chunks': n, 'completados': i, 'parcial': salida}
            jobs.set(job_id, paso=f'LLM chunk {i + 1}/{n}', progreso=int(95 * i / n))
            original_phs = set(_PH_RE.findall(chunk))
            traducido = llm.traducir(chunk)
            # QA: placeholders presentes
            faltan = original_phs - set(_PH_RE.findall(traducido))
            if faltan:
                jobs.log(job_id, f'[QA] chunk {i + 1}: faltan marcadores {sorted(faltan)}; reintento 1')
                try:
                    traducido = llm.traducir(chunk)
                except Exception as e:
                    jobs.log(job_id, f'[QA] reintento fallo: {e}')
            faltan = original_phs - set(_PH_RE.findall(traducido))
            if faltan:
                jobs.log(job_id, f'[QA] chunk {i + 1}: se conserva el original (faltan {sorted(faltan)})')
                traducido = chunk
            # restaurar contenido protegido en los marcadores
            traducido = _PH_RE.sub(lambda m: mapa_ph.get(int(m.group(1)), m.group(0)), traducido)
            f.write(traducido + '\n')
            f.flush()
            jobs.set(job_id, params={**jobs.get(job_id)['params'], 'chunks_completados': i + 1})
    finally:
        f.close()
    return {'chunks': n, 'completados': n}


# ---------- punto de entrada ----------
def _qa_estructura(archivo_md, salida):
    """Conteos de estructura del original vs el traducido (punto 8)."""

    def contadores(ruta):
        t = open(ruta, encoding='utf-8').read()
        return {
            'titulos': len(re.findall(r'(?m)^#{1,6}\s', t)),
            'fences': t.count('```'),
            'imagenes': len(re.findall(r'!\[[^\]]*\]\([^)]*\)', t)),
            'html_tablas': len(re.findall(r'(?i)<table', t)),
        }

    return {'original': contadores(archivo_md), 'traducido': contadores(salida)}


def _copiar_recursos(job_id, archivo_md, salida):
    """Copia images/ y metadata/ junto al md traducido (punto 7).
    Busca la carpeta de recursos junto al origen y, si no existe, en las
    carpetas md extra (mismo nombre de archivo)."""
    dir_origen = os.path.dirname(os.path.abspath(archivo_md))
    dir_salida = os.path.dirname(os.path.abspath(salida))
    candidatos = [dir_origen]
    if dir_origen != dir_salida:
        base = os.path.basename(archivo_md)
        for extra in cfg.carpetas_md_extra:
            if os.path.isfile(os.path.join(extra, base)):
                candidatos.append(extra)
    copiados = 0
    for sub in ('images', 'metadata'):
        for origen in candidatos:
            src = os.path.join(origen, sub)
            if os.path.isdir(src):
                try:
                    shutil.copytree(src, os.path.join(dir_salida, sub), dirs_exist_ok=True)
                    copiados += 1
                    jobs.log(job_id, f'[copia] {sub}/ copiado desde {origen}')
                except Exception as e:
                    jobs.log(job_id, f'[copia] aviso {sub}/: {e}')
                break
        else:
            jobs.log(job_id, f'[copia] aviso: no se encontro {sub}/ junto al origen')
    return copiados


def traducir_md(job_id, archivo_md, motor='nllb', modelo='', chunk_chars=None,
                nombre_salida=None, forzar=False, reanudar=False):
    """Ejecuta la traduccion de un md. Devuelve dict resultado."""
    chunk_chars = chunk_chars or cfg.chunk_traduccion
    salida = os.path.join(cfg.traducciones_dir, nombre_salida or
                          os.path.splitext(os.path.basename(archivo_md))[0] + '_es.md')

    if not forzar:
        idioma = detectar_idioma(job_id, archivo_md)
        jobs.log(job_id, f'Idioma detectado: {idioma}')
        if idioma == 'ES':
            return {'sin_cambios': True, 'idioma': 'ES',
                    'mensaje': 'El documento ya esta en espanol; traduccion omitida'}

    if motor == 'opus':
        detalle = _traducir_rapido(job_id, archivo_md, salida, forzar)
    else:
        detalle = _traducir_llm(job_id, archivo_md, salida, modelo, chunk_chars, reanudar)
    _copiar_recursos(job_id, archivo_md, salida)
    detalle.update({'motor': motor, 'salida': salida, 'archivo': os.path.basename(archivo_md),
                    'qa_estructura': _qa_estructura(archivo_md, salida)})
    return detalle
