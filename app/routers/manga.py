# routers/manga.py - Traduccion de manga in-place (PDF subido por UI)
import os
import shutil
import threading
import time

import pymupdf
from fastapi import APIRouter, File, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..config import cfg
from ..engines_manga import (_mineru_a_markdown, _parsear_paginas, _precalentar_lmstudio,
                             _preparar_entrada_motor, _render_md_paginas, _resolver_modelo,
                             _traducir_md_opus, correr_motor, extraer_paginas, unir_pdf)
from ..jobs import jobs

router = APIRouter()

EXT_PDF = ('.pdf',)


def _nombre_limpio(nombre):
    nombre = ''.join(c for c in nombre if c.isalnum() or c in ' _-').strip()
    return nombre or 'manga'


def _worker(job_id, req, pdf):
    try:
        jobs.set(job_id, estado='procesando')
        t0 = time.time()
        nombre = _nombre_limpio(req.nombre_salida or os.path.splitext(os.path.basename(pdf))[0])
        dir_job = os.path.join(cfg.manga_salidas, nombre)
        dir_paginas = os.path.join(dir_job, 'paginas')
        dir_motor = os.path.join(dir_job, 'para_traducir')
        dir_traducidas = os.path.join(dir_job, 'traducidas')
        os.makedirs(dir_job, exist_ok=True)

        # 0. parseo de paginas especiales (omitidas / de texto)
        omitir = _parsear_paginas(req.omitir_paginas)
        paginas_texto = _parsear_paginas(req.paginas_texto)
        total = 0
        # 1. extraccion de paginas (se omite al reanudar si ya existen)
        if req.reanudar and os.path.isdir(dir_paginas) and os.listdir(dir_paginas):
            total = len([f for f in os.listdir(dir_paginas) if f.lower().endswith('.png')])
            jobs.log(job_id, f'[manga] Reanudando: {total} paginas ya extraidas')
        else:
            jobs.log(job_id, f'[manga] Extrayendo paginas de {os.path.basename(pdf)}')
            r = extraer_paginas(pdf, dir_paginas, job_id, req.dpi or None)
            total = r['paginas']
            if r.get('cancelado'):
                jobs.set(job_id, estado='cancelado', progreso=100, paso='Cancelado')
                return
            jobs.set(job_id, progreso=10, paso=f'{total} paginas extraidas')
            # inicio limpio: sin paginas fantasma de runs anteriores con el mismo nombre
            for d in (dir_motor, dir_traducidas):
                if os.path.isdir(d):
                    shutil.rmtree(d)
        if omitir or paginas_texto:
            _validar_rangos(omitir, paginas_texto, total, req)

        # 2. paginas de solo texto -> sistema de libros (MinerU + OPUS)
        pdf_texto = None
        if paginas_texto:
            jobs.log(job_id, f'[manga] Paginas de texto ({len(paginas_texto)}): '
                             f'{sorted(paginas_texto)} -> sistema de libros')
            pdf_sub = os.path.join(dir_job, 'texto_sub.pdf')
            _extraer_sub_pdf(pdf, sorted(paginas_texto), pdf_sub)
            dir_md = os.path.join(dir_job, 'texto_md')
            md_orig = _mineru_a_markdown(pdf_sub, dir_md, job_id)
            md_trad = os.path.join(dir_job, 'texto_traducido.md')
            _traducir_md_opus(md_orig, md_trad, job_id)
            pdf_texto = os.path.join(dir_job, 'texto_paginas.pdf')
            _render_md_paginas(md_trad, len(paginas_texto), pdf_texto)
            jobs.log(job_id, f'[manga] Texto traducido y renderizado ({len(paginas_texto)} paginas)')

        # 3. motor in-place sobre el resto de paginas (resumible)
        excluidas = omitir | set(paginas_texto)
        a_traducir = _preparar_entrada_motor(dir_paginas, dir_motor, excluidas)
        if a_traducir == 0:
            jobs.log(job_id, '[manga] Sin paginas manga que traducir (todas especiales)')
            hechas = 0
        else:
            if req.motor == 'opus':
                jobs.log(job_id, '[manga] Motor OPUS-MT local (rapido)')
                modelo_real = ''
            elif req.motor == 'deepseek':
                jobs.log(job_id, f'[manga] Motor DeepSeek API (nube)')
                if not (req.deepseek_key or cfg.manga_deepseek_key):
                    raise RuntimeError('API key de DeepSeek no configurada: ponla en config.yaml '
                                       '(manga_deepseek_key) o en el campo de la UI')
                modelo_real = ''
            else:
                modelo_real = _resolver_modelo(job_id, req.modelo)
                if req.modelo and modelo_real != req.modelo:
                    jobs.log(job_id, f'[manga] Modelo {req.modelo} no disponible; usando {modelo_real}')
                if not _precalentar_lmstudio(job_id, modelo_real):
                    raise RuntimeError('LM Studio no tiene el modelo disponible. Abre LM Studio y carga '
                                       + modelo_real)
            jobs.log(job_id, f'[manga] Motor in-place (idioma {req.idioma}, '
                             f'{a_traducir} paginas)')
            rc = correr_motor(job_id, dir_paginas, dir_motor, dir_traducidas,
                              req.modelo if req.motor == 'llm' else '',
                              a_traducir, req.idioma, req.motor, req.fso,
                              req.deepseek_key, req.deepseek_model)
            if jobs.get(job_id).get('cancelar'):
                jobs.set(job_id, estado='cancelado', progreso=100,
                         paso='Cancelado (resumible con "reanudar")')
                return
            if rc != 0:
                raise RuntimeError(f'Motor de manga fallo (exit {rc})')
            hechas = len([f for f in os.listdir(dir_traducidas) if f.lower().endswith('.png')])
            if hechas == 0:
                raise RuntimeError('El motor no tradujo ninguna pagina (revisa el log)')
            jobs.set(job_id, progreso=92,
                     paso=f'Traducidas {hechas}/{a_traducir} paginas, generando PDF')

        # 4. PDF final (mixto)
        pdf_final = os.path.join(dir_job, nombre + '.pdf')
        unidas = unir_pdf(dir_traducidas, pdf_final, dir_paginas, omitir,
                          {n: True for n in paginas_texto}, pdf_texto)
        jobs.set(job_id, progreso=100, estado='completado', paso='Completado', resultado={
            'paginas': total,
            'traducidas': hechas if a_traducir else 0,
            'omitidas': len(omitir),
            'texto_libros': len(paginas_texto),
            'pdf': pdf_final,
            'directorio': dir_job,
            'tiempo_seg': round(time.time() - t0, 1),
        })
    except ValueError as e:
        jobs.log(job_id, f'[ERROR] {e}')
        jobs.set(job_id, estado='error', paso=str(e), progreso=100)
    except Exception as e:
        jobs.log(job_id, f'[ERROR] {e}')
        jobs.set(job_id, estado='error', paso=str(e), progreso=100)


def _validar_rangos(omitir, paginas_texto, total, req):
    if omitir and any(p < 1 or p > total for p in omitir):
        raise ValueError(f'Paginas a omitir fuera de rango (1-{total})')
    if paginas_texto and any(p < 1 or p > total for p in paginas_texto):
        raise ValueError(f'Paginas de texto fuera de rango (1-{total})')
    cruce = omitir & set(paginas_texto)
    if cruce:
        raise ValueError(f'Paginas duplicadas en omitir y texto: {sorted(cruce)}')


def _extraer_sub_pdf(pdf, paginas, pdf_sub):
    """Sub-PDF con las paginas indicadas (para MinerU)."""
    doc = pymupdf.open(pdf)
    doc.select([p - 1 for p in paginas])
    doc.save(pdf_sub)
    doc.close()


class MangaReq(BaseModel):
    archivo: str
    idioma: str = 'ja'        # ja | en | ru (afecta el OCR del motor)
    motor: str = 'llm'        # llm (LM Studio) | opus (OPUS-MT local) | deepseek (API nube)
    modelo: str = ''          # vacio = default (solo motor llm)
    deepseek_key: str = ''    # API key DeepSeek (opcional: si vacia usa config.yaml)
    deepseek_model: str = ''  # modelo DeepSeek (vacio = config.yaml)
    nombre_salida: str = ''
    dpi: int = 0              # 0 = usar config (200)
    fso: int = 0              # font_size_offset por job (0 = default config)
    reanudar: bool = False
    omitir_paginas: str = ''  # ej. "1, 5-6, 9" -> se mantienen en ingles
    paginas_texto: str = ''   # ej. "6-7" -> se traducen con el sistema de libros


@router.post('/manga/subir')
async def subir_manga(archivo: UploadFile = File(...)):
    nombre = os.path.basename(archivo.filename or 'manga.pdf')
    if not nombre.lower().endswith(EXT_PDF):
        nombre += '.pdf'
    os.makedirs(cfg.manga_entrada, exist_ok=True)
    destino = os.path.join(cfg.manga_entrada, nombre)
    with open(destino, 'wb') as f:
        while True:
            chunk = await archivo.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)
    return {'ok': True, 'archivo': nombre}


@router.get('/manga/archivos')
def lista_manga():
    if not os.path.isdir(cfg.manga_entrada):
        return {'archivos': []}
    archivos = []
    for fn in sorted(os.listdir(cfg.manga_entrada)):
        if fn.lower().endswith(EXT_PDF):
            ruta = os.path.join(cfg.manga_entrada, fn)
            archivos.append({
                'nombre': fn,
                'tamano': os.path.getsize(ruta),
                'modificado': time.strftime('%Y-%m-%d %H:%M',
                                            time.localtime(os.path.getmtime(ruta))),
            })
    return {'archivos': archivos}


@router.get('/manga/salidas')
def lista_salidas():
    if not os.path.isdir(cfg.manga_salidas):
        return {'salidas': []}
    salidas = []
    for fn in sorted(os.listdir(cfg.manga_salidas)):
        d = os.path.join(cfg.manga_salidas, fn)
        if not os.path.isdir(d):
            continue
        dir_t = os.path.join(d, 'traducidas')
        salidas.append({
            'nombre': fn,
            'pdf': os.path.isfile(os.path.join(d, fn + '.pdf')),
            'paginas': len([f for f in os.listdir(dir_t) if f.lower().endswith('.png')])
                       if os.path.isdir(dir_t) else 0,
            'modificado': time.strftime('%Y-%m-%d %H:%M',
                                        time.localtime(os.path.getmtime(d))),
        })
    return {'salidas': salidas}


@router.post('/manga/transformar')
def transformar_manga(req: MangaReq):
    pdf = os.path.join(cfg.manga_entrada, os.path.basename(req.archivo))
    if not os.path.isfile(pdf):
        return {'error': f'PDF no encontrado: {req.archivo}'}
    if req.idioma not in ('ja', 'en', 'ru'):
        return {'error': f'Idioma no soportado: {req.idioma} (ja|en|ru)'}
    if req.motor not in ('llm', 'opus', 'deepseek'):
        return {'error': f'Motor no soportado: {req.motor} (llm|opus|deepseek)'}
    if req.motor == 'opus' and req.idioma == 'ja':
        return {'error': 'El motor OPUS rapido no esta disponible para japones '
                         '(calidad insuficiente). Usa LLM para manga japones.'}
    # la API key NO se persiste en el JSON del job (se reemplaza por ***)
    params = req.model_dump()
    if params.get('deepseek_key'):
        params['deepseek_key'] = '***'
    jid = jobs.crear('manga', params)
    t = threading.Thread(target=_worker, args=(jid, req, pdf), daemon=True)
    t.start()
    return {'job_id': jid}


@router.get('/manga/salidas/{nombre}/pdf')
def descargar_pdf(nombre: str):
    base = os.path.basename(nombre)
    pdf = os.path.join(cfg.manga_salidas, base, base + '.pdf')
    if not os.path.isfile(pdf):
        return {'error': 'PDF no existe'}
    return FileResponse(pdf, filename=base + '.pdf')


@router.get('/manga/salidas/{nombre}/paginas')
def listar_paginas(nombre: str):
    dir_t = os.path.join(cfg.manga_salidas, os.path.basename(nombre), 'traducidas')
    if not os.path.isdir(dir_t):
        return {'paginas': []}
    paginas = sorted((f for f in os.listdir(dir_t) if f.lower().endswith('.png')))
    return {'paginas': paginas}


@router.get('/manga/salidas/{nombre}/pagina/{n}')
def ver_pagina(nombre: str, n: int):
    ruta = os.path.join(cfg.manga_salidas, os.path.basename(nombre), 'traducidas',
                        f'pagina_{n:03d}.png')
    if not os.path.isfile(ruta):
        return {'error': 'pagina no existe'}
    return FileResponse(ruta, media_type='image/png')
