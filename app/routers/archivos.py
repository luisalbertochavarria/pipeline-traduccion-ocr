# routers/archivos.py - Listado de archivos locales y subida
import os
import time

from fastapi import APIRouter, File, UploadFile
from fastapi.responses import JSONResponse

from ..config import cfg

router = APIRouter()

EXT_PDF = ('.pdf',)
EXT_MD = ('.md',)


def _listar(carpeta, exts, max_profundidad=2):
    """Lista archivos de una carpeta (con subcarpetas hasta max_profundidad)."""
    if not os.path.isdir(carpeta):
        return []
    es_pdf = '.pdf' in exts
    salida = []
    raiz = os.path.abspath(carpeta)
    for dirpath, dirnames, filenames in os.walk(carpeta):
        excluir = ['lotes'] if es_pdf else []
        dirnames[:] = [d for d in dirnames if not d.startswith('.') and d not in excluir]
        rel = os.path.relpath(dirpath, raiz)
        prof = 0 if rel == '.' else rel.count(os.sep) + 1
        if prof > max_profundidad:
            dirnames[:] = []
            continue
        for fn in sorted(filenames):
            if fn.lower().endswith(exts):
                ruta = os.path.join(dirpath, fn)
                try:
                    tam = os.path.getsize(ruta)
                    mod = os.path.getmtime(ruta)
                except OSError:
                    continue
                salida.append({
                    'nombre': fn,
                    'ruta': os.path.relpath(ruta, raiz).replace('\\', '/'),
                    'carpeta': '' if rel == '.' else rel.replace('\\', '/'),
                    'tamano': tam,
                    'modificado': time.strftime('%Y-%m-%d %H:%M', time.localtime(mod)),
                })
    return salida


@router.get('/archivos')
def archivos(tipo: str = 'pdf', carpeta: str = 'entrada'):
    """tipo: pdf|md | carpeta: entrada|salidas|traducciones"""
    if tipo == 'md':
        carpetas = [('salidas', cfg.salidas_dir), ('traducciones', cfg.traducciones_dir)]
        for extra in cfg.carpetas_md_extra:
            carpetas.append(('extra', extra))
    else:
        carpetas = [('entrada', cfg.carpeta_entrada)]
    resultado = []
    for nombre, ruta in carpetas:
        for a in _listar(ruta, EXT_MD if tipo == 'md' else EXT_PDF):
            a['origen'] = nombre
            resultado.append(a)
    return {'archivos': resultado}


@router.post('/subir')
async def subir(tipo: str = 'pdf', archivo: UploadFile = File(...)):
    """Sube un PDF (a carpeta_entrada) o un md (a traducciones)."""
    nombre = os.path.basename(archivo.filename or 'archivo')
    if tipo == 'md':
        destino_dir = cfg.traducciones_dir
    else:
        destino_dir = cfg.carpeta_entrada
        if not nombre.lower().endswith(EXT_PDF):
            nombre += '.pdf'
    os.makedirs(destino_dir, exist_ok=True)
    destino = os.path.join(destino_dir, nombre)
    with open(destino, 'wb') as f:
        while True:
            chunk = await archivo.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)
    return {'ok': True, 'archivo': nombre, 'ruta': destino}
