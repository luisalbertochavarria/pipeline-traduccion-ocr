# main.py - Entry point del sistema PDF -> Markdown + Traduccion
import os

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import cfg
from .routers import archivos, manga, modelos, traducir, transformar

app = FastAPI(title='Sistema PDF -> Markdown + Traduccion', version='1.1.0')

app.include_router(archivos.router, prefix='/api')
app.include_router(modelos.router, prefix='/api')
app.include_router(transformar.router, prefix='/api')
app.include_router(traducir.router, prefix='/api')
app.include_router(manga.router, prefix='/api')

if os.path.isdir(cfg.static_dir):
    app.mount('/static', StaticFiles(directory=cfg.static_dir), name='static')


@app.get('/')
def index():
    return FileResponse(os.path.join(cfg.static_dir, 'index.html'))


@app.get('/api/estado')
def estado():
    return {
        'ok': True,
        'motores': {
            'mineru': os.path.exists(cfg.mineru_exe),
            'chandra': os.path.exists(cfg.chandra_exe),
        },
        'lmstudio': {'url': cfg.lmstudio_url},
        'opus': os.path.isdir(cfg.opus_model_dir),
        'llm_default': cfg.modelo_llm_default,
        'manga': {'motor': os.path.isfile(cfg.manga_python) and os.path.isdir(cfg.manga_repo),
                  'default': cfg.manga_modelo_default},
        'carpeta_entrada': cfg.carpeta_entrada,
        'salidas': cfg.salidas_dir,
        'traducciones': cfg.traducciones_dir,
    }


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=cfg.puerto)
