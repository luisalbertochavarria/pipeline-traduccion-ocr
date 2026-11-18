# routers/transformar.py - Jobs de transformacion PDF -> Markdown
import io
import json
import os
import threading
import time
import zipfile

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..config import cfg
from ..engines import (aplanar_mineru, buscar_md, dividir_pdf, ejecutar,
                       limpiar_md, limpiar_nombre, nombre_base,
                       renombrar_salida_chandra, rotas_en_log, run_chandra,
                       run_mineru, run_mineru_por_lotes, unir_bloques, _num_paginas)
from ..jobs import jobs

router = APIRouter()


class TransformarReq(BaseModel):
    archivo: str
    modo: str = 'completo'        # completo | lotes
    motor: str = 'mineru'         # mineru | chandra
    paginas_por_lote: int = 10
    limpiar: str = 'auto'         # auto | no
    unir: bool = True


def _resolver_pdf(archivo):
    ruta = archivo if os.path.isabs(archivo) else os.path.join(cfg.carpeta_entrada, archivo)
    return ruta if os.path.isfile(ruta) else None


def _detectar_idioma(job_id, md):
    """Detecta idioma del md con fast-langdetect (python de mineru-env)."""
    codigo = (
        'import sys;'
        'from fast_langdetect import detect_language;'
        'print(detect_language(" ".join(open(sys.argv[1], encoding="utf-8").read().split())[:2000]))'
    )
    cmd = [cfg.python_mineru, '-c', codigo, md]
    rc = ejecutar(job_id, cmd, prefijo='[idioma] ')
    return None


def _worker(job_id, req, pdf, nombre):
    try:
        jobs.set(job_id, estado='procesando')
        t0 = time.time()
        base = os.path.join(cfg.salidas_dir, limpiar_nombre(nombre))
        salida = os.path.join(base, req.modo)
        os.makedirs(salida, exist_ok=True)
        mds_generados = []
        bloques = 0

        if req.modo == 'completo':
            jobs.set(job_id, paso='Procesando documento completo', progreso=10)
            if req.motor == 'mineru':
                if _num_paginas(pdf) > 50:
                    # libros grandes: por rangos de paginas para evitar OOM
                    rc = run_mineru_por_lotes(job_id, pdf, salida, cfg.mineru_paginas_por_lote)
                    if rc != 0:
                        raise RuntimeError(f'MinerU fallo con codigo {rc}')
                    aplanar_mineru(job_id, salida)
                else:
                    rc = run_mineru(job_id, pdf, salida)
                    if rc != 0:
                        raise RuntimeError(f'Motor {req.motor} fallo con codigo {rc}')
            else:
                rc = run_chandra(job_id, pdf, salida)
                renombrar_salida_chandra(job_id, salida, nombre)
                if rc != 0:
                    raise RuntimeError(f'Motor {req.motor} fallo con codigo {rc}')
            mds_generados = buscar_md(salida)
            bloques = 1
            jobs.set(job_id, progreso=80, paso='Limpieza ortografica')
        else:
            lotes_dir = os.path.join(base, 'lotes')
            jobs.set(job_id, paso='Dividiendo PDF en lotes', progreso=5)
            if dividir_pdf(job_id, pdf, lotes_dir, req.paginas_por_lote) != 0:
                raise RuntimeError('Fallo al dividir el PDF')
            archivos_lotes = sorted(f for f in os.listdir(lotes_dir) if f.lower().endswith('.pdf'))
            total = len(archivos_lotes)
            if not archivos_lotes:
                raise RuntimeError('No se generaron lotes')
            jobs.set(job_id, paso=f'Procesando {total} lotes con {req.motor}', progreso=10)

            if req.motor == 'mineru':
                rc = run_mineru(job_id, lotes_dir, salida)
                if rc != 0:
                    raise RuntimeError(f'MinerU fallo con codigo {rc}')
                aplanar_mineru(job_id, salida)
                bloques = len([d for d in os.listdir(salida)
                               if os.path.isdir(os.path.join(salida, d)) and d.startswith('bloque_pag_')
                               and any(f.lower().endswith('.md') for f in os.listdir(os.path.join(salida, d)))])
                jobs.set(job_id, progreso=80, paso='Limpieza ortografica')
            else:
                for i, bl in enumerate(archivos_lotes, 1):
                    if jobs.get(job_id)['cancelar']:
                        jobs.set(job_id, estado='cancelado', paso='Cancelado por el usuario', progreso=100)
                        return
                    jobs.set(job_id, paso=f'Chandra lote {i}/{total}: {bl}',
                             progreso=int(10 + 65 * (i - 1) / total))
                    out_bloque = os.path.join(salida, os.path.splitext(bl)[0])
                    rc = run_chandra(job_id, os.path.join(lotes_dir, bl), out_bloque)
                    if rc != 0:
                        jobs.log(job_id, f'[ERROR] lote {bl} fallo (codigo {rc}); se continua')
                        continue
                    renombrar_salida_chandra(job_id, out_bloque, os.path.splitext(bl)[0])
                    bloques += 1
                jobs.set(job_id, progreso=80, paso='Limpieza ortografica')

            mds_generados = buscar_md(salida)

        # limpiar ortografia (auto = el limpiador decide por idioma)
        if req.limpiar == 'auto':
            for md in mds_generados:
                if jobs.get(job_id)['cancelar']:
                    jobs.set(job_id, estado='cancelado', paso='Cancelado por el usuario')
                    return
                limpiar_md(job_id, md)

        # union de bloques (modo lotes, o completo con mineru por rangos)
        unido = None
        rotas = None
        hay_bloques = any(
            d.startswith('bloque_pag_') and os.path.isdir(os.path.join(salida, d))
            for d in os.listdir(salida)) if os.path.isdir(salida) else False
        if req.unir and (req.modo == 'lotes' or hay_bloques):
            jobs.set(job_id, paso='Uniendo bloques en un solo markdown', progreso=92)
            unir_bloques(job_id, salida)
            rotas = rotas_en_log(job_id, job_id)
            unido_dir = os.path.join(salida, 'UNIDO')
            if os.path.isdir(unido_dir):
                for fn in os.listdir(unido_dir):
                    if fn.endswith('.md'):
                        destino = os.path.join(unido_dir, f'{limpiar_nombre(nombre)}_unido.md')
                        if fn != os.path.basename(destino):
                            os.rename(os.path.join(unido_dir, fn), destino)
                        unido = destino
                        break

        jobs.set(job_id, progreso=100, paso='Completado',
                 estado='completado',
                 resultado={
                     'tipo': 'transformar',
                     'archivo': nombre,
                     'modo': req.modo,
                     'motor': req.motor,
                     'carpeta_salida': salida,
                     'mds': mds_generados,
                     'bloques': bloques,
                     'unido': unido,
                     'imagenes_rotas': rotas,
                     'tiempo_seg': round(time.time() - t0, 1),
                 })
    except Exception as e:
        jobs.log(job_id, f'[ERROR] {e}')
        jobs.set(job_id, estado='error', paso=str(e), progreso=100)


@router.post('/transformar')
def transformar(req: TransformarReq):
    pdf = _resolver_pdf(req.archivo)
    if not pdf:
        return {'error': f'Archivo no encontrado: {req.archivo}'}
    jid = jobs.crear('transformar', req.model_dump())
    t = threading.Thread(target=_worker, args=(jid, req, pdf, nombre_base(pdf)), daemon=True)
    t.start()
    return {'job_id': jid}


@router.get('/jobs')
def lista_jobs():
    return {'jobs': jobs.lista()}


@router.get('/jobs/{jid}')
def get_job(jid: str):
    j = jobs.get(jid)
    if not j:
        return {'error': 'job no existe'}
    return j


@router.post('/jobs/{jid}/cancelar')
def cancelar_job(jid: str):
    return {'ok': jobs.cancelar(jid)}


@router.get('/jobs/{jid}/stream')
def stream_job(jid: str):
    def gen():
        ultimo_log = 0
        ultimo_estado = None
        while True:
            j = jobs.get(jid)
            if not j:
                yield 'data: {"type":"error","text":"job no existe"}\n\n'
                return
            if j['estado'] != ultimo_estado:
                ultimo_estado = j['estado']
                yield f'data: {json.dumps({"type":"estado","estado":j["estado"],"progreso":j["progreso"],"paso":j["paso"]})}\n\n'
            nuevos = j['log'][ultimo_log:]
            if nuevos:
                ultimo_log = len(j['log'])
                yield f'data: {json.dumps({"type":"log","lineas":nuevos})}\n\n'
            if j['estado'] in ('completado', 'error', 'cancelado', 'interrumpido'):
                yield f'data: {json.dumps({"type":"fin","estado":j["estado"],"resultado":j["resultado"]})}\n\n'
                return
            time.sleep(1)

    return StreamingResponse(gen(), media_type='text/event-stream',
                             headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@router.get('/resultado/{jid}/descargar')
def descargar_resultado(jid: str):
    j = jobs.get(jid)
    if not j or not j.get('resultado'):
        return {'error': 'sin resultado'}
    carpeta = j['resultado'].get('carpeta_salida')
    if not carpeta or not os.path.isdir(carpeta):
        return {'error': 'carpeta de salida no disponible'}

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for dirpath, _dirs, files in os.walk(carpeta):
            for fn in files:
                ruta = os.path.join(dirpath, fn)
                arc = os.path.relpath(ruta, carpeta).replace('\\', '/')
                z.write(ruta, arc)
    buf.seek(0)
    nombre_zip = f'{limpiar_nombre(j["resultado"].get("archivo", "resultado"))}_{j["resultado"].get("modo", "salida")}.zip'
    return StreamingResponse(buf, media_type='application/zip',
                             headers={'Content-Disposition': f'attachment; filename="{nombre_zip}"'})
