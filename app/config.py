# config.py - Carga config.yaml del proyecto
import os

import yaml

_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Config:
    def __init__(self, path=None):
        path = path or os.path.join(_DIR, 'config.yaml')
        with open(path, encoding='utf-8') as f:
            raw = yaml.safe_load(f) or {}

        self.base = _DIR
        self.mineru_exe = raw.get('mineru_exe', '')
        self.chandra_exe = raw.get('chandra_exe', '')
        self.python_mineru = raw.get('python_mineru', '')
        self.python_chandra = raw.get('python_chandra', '')
        self.limpiador_script = raw.get('limpiador_script', '')
        self.unidor_script = raw.get('unidor_script', '')
        self.carpeta_entrada = raw.get('carpeta_entrada', os.path.join(_DIR, 'pdf_original'))
        self.carpetas_md_extra = [c for c in raw.get('carpetas_md_extra', []) if c]
        self.puerto = int(raw.get('puerto', 8000))
        self.lmstudio_url = raw.get('lmstudio_url', 'http://127.0.0.1:1234/v1')
        self.opus_model_dir = os.path.join(_DIR, raw.get('opus_model_dir', 'modelos/opus-en-es'))
        self.modelo_llm_default = raw.get('modelo_llm_default', '')
        self.chunk_traduccion = int(raw.get('chunk_traduccion', 2000))
        self.paginas_por_lote = int(raw.get('paginas_por_lote', 10))
        self.mineru_paginas_por_lote = int(raw.get('mineru_paginas_por_lote', 100))

        # Manga
        self.manga_python = raw.get('manga_python', '')
        self.manga_repo = raw.get('manga_repo', '')
        self.manga_models = raw.get('manga_models', '')
        self.manga_config_ja = raw.get('manga_config_ja', '')
        self.manga_config_ocr = raw.get('manga_config_ocr', '')
        self.manga_config_opus = raw.get('manga_config_opus', '')
        self.manga_entrada = raw.get('manga_entrada', os.path.join(_DIR, 'entrada_manga'))
        self.manga_salidas = raw.get('manga_salidas', os.path.join(_DIR, 'salidas_manga'))
        self.manga_dpi = int(raw.get('manga_dpi', 150))
        self.manga_fso_default = int(raw.get('manga_fso_default', -4))
        self.manga_modelo_default = raw.get('manga_modelo_default', 'hermes-3-llama-3.1-8b')
        self.manga_font = raw.get('manga_font', '')
        self.manga_font_ja = raw.get('manga_font_ja', '')
        self.manga_context_size = int(raw.get('manga_context_size', 0))
        self.manga_pre_dict = raw.get('manga_pre_dict', '')
        self.manga_deepseek_base = raw.get('manga_deepseek_base', 'https://api.deepseek.com/v1')
        self.manga_deepseek_model = raw.get('manga_deepseek_model', 'deepseek-chat')
        self.manga_deepseek_key = raw.get('manga_deepseek_key', '')

        self.data_dir = os.path.join(_DIR, 'data')
        self.salidas_dir = os.path.join(_DIR, 'salidas')
        self.traducciones_dir = os.path.join(_DIR, 'traducciones')
        self.static_dir = os.path.join(_DIR, 'app', 'static')
        for d in (self.data_dir, self.salidas_dir, self.traducciones_dir,
                  self.manga_entrada, self.manga_salidas):
            os.makedirs(d, exist_ok=True)


cfg = Config()
