# routers/modelos.py - Proxy a LM Studio (lista de modelos locales)
import requests

from fastapi import APIRouter

from ..config import cfg

router = APIRouter()


@router.get('/modelos')
def modelos():
    try:
        r = requests.get(f'{cfg.lmstudio_url}/models', timeout=5)
        if r.status_code == 200:
            data = r.json()
            lista = [{'id': m.get('id'), 'object': m.get('object', '')} for m in data.get('data', [])]
            return {'conectado': True, 'url': cfg.lmstudio_url, 'modelos': lista}
        return {'conectado': False, 'url': cfg.lmstudio_url, 'modelos': [], 'error': f'HTTP {r.status_code}'}
    except Exception as e:
        return {'conectado': False, 'url': cfg.lmstudio_url, 'modelos': [], 'error': str(e)}
