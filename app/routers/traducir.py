# routers/traducir.py - Jobs de traduccion markdown -> espanol
import os
import threading
import time

from fastapi import APIRouter
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..config import cfg
from ..jobs import jobs
from ..translate import traducir_md

router = APIRouter()


class TraducirReq(BaseModel):
    archivo: str                 # md: ruta absoluta o relativa a traducciones/salidas
    motor: str = 'opus'          # opus | llm
    modelo: str = ''
    chunk_chars: int = 0         # 0 = usar config
    nombre_salida: str = ''
    forzar: bool = False         # traducir aunque el idioma sea ES
    reanudar: bool = False


def _resolver_md(archivo):
    candidatas = []
    if os.path.isabs(archivo):
        candidatas.append(archivo)
    else:
        candidatas.append(os.path.join(cfg.traducciones_dir, archivo))
        candidatas.append(os.path.join(cfg.salidas_dir, archivo))
        for extra in cfg.carpetas_md_extra:
            candidatas.append(os.path.join(extra, archivo))
    for c in candidatas:
        if os.path.isfile(c):
            return c
    return None


def _worker(job_id, req, md):
    try:
        jobs.set(job_id, estado='procesando')
        t0 = time.time()
        detalle = traducir_md(
            job_id, md, motor=req.motor, modelo=req.modelo,
            chunk_chars=req.chunk_chars or None,
            nombre_salida=req.nombre_salida or None,
            forzar=req.forzar, reanudar=req.reanudar,
        )
        if detalle.get('sin_cambios'):
            jobs.set(job_id, progreso=100, paso='Omitido (documento en espanol)',
                     estado='completado', resultado={**detalle, 'tiempo_seg': round(time.time() - t0, 1)})
            return
        jobs.set(job_id, progreso=100, paso='Completado', estado='completado',
                 resultado={**detalle, 'tiempo_seg': round(time.time() - t0, 1)})
    except Exception as e:
        jobs.log(job_id, f'[ERROR] {e}')
        jobs.set(job_id, estado='error', paso=str(e), progreso=100)


@router.post('/traducir')
def traducir(req: TraducirReq):
    md = _resolver_md(req.archivo)
    if not md:
        return {'error': f'Archivo markdown no encontrado: {req.archivo}'}
    jid = jobs.crear('traducir', req.model_dump())
    t = threading.Thread(target=_worker, args=(jid, req, md), daemon=True)
    t.start()
    return {'job_id': jid}


@router.get('/traducciones')
def lista_traducciones():
    if not os.path.isdir(cfg.traducciones_dir):
        return {'archivos': []}
    archivos = []
    for fn in sorted(os.listdir(cfg.traducciones_dir)):
        if fn.lower().endswith('.md'):
            ruta = os.path.join(cfg.traducciones_dir, fn)
            archivos.append({
                'nombre': fn,
                'tamano': os.path.getsize(ruta),
                'modificado': time.strftime('%Y-%m-%d %H:%M', time.localtime(os.path.getmtime(ruta))),
            })
    return {'archivos': archivos}


@router.get('/traducciones/{nombre}')
def descargar_traduccion(nombre: str):
    ruta = os.path.join(cfg.traducciones_dir, os.path.basename(nombre))
    if not os.path.isfile(ruta):
        return {'error': 'no existe'}
    return FileResponse(ruta, filename=os.path.basename(ruta))
