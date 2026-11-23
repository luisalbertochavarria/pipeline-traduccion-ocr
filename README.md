# Pipeline de OCR y traducción de documentos con IA local

Sistema web que convierte documentos PDF a Markdown, los traduce al español y permite traducir
manga "in-place" (reemplazando el texto dentro de las viñetas). Todo el procesamiento puede correr
de forma local, sin enviar documentos a servicios externos.

Desarrollo propio (proyecto personal, autodidacta).

## Qué resuelve

Traducir libros y documentos técnicos completos conservando la estructura (títulos, listas, tablas,
fórmulas, bloques de código) es un problema real: los traductores automáticos rompen el formato y
los modelos de lenguaje puros tienden a resumir o inventar. Este sistema separa el problema en
etapas y protege la estructura en cada una.

## Arquitectura

```
app/main.py            Punto de entrada FastAPI; monta la interfaz y las rutas /api
app/config.py          Configuracion desde config.yaml (rutas de motores y parametros)
app/jobs.py            Gestor de trabajos persistente (estado, log, cancelacion)
app/engines.py         Ejecucion de motores de OCR/conversion como subprocesos
app/engines_manga.py   Orquestacion del motor de manga in-place
app/translate.py       Traduccion: proteccion de estructura + motores NLLB/LLM
app/bridge_traductor.py Puente al modelo de traduccion neuronal local (Marian/OPUS-MT)
app/dividir.py         Division de PDF por lotes de paginas
app/routers/           Endpoints REST: archivos, modelos, transformar, traducir, manga
app/static/            Interfaz web (HTML + JS + CSS sin frameworks)
```

### Decisiones de diseño

- **Trabajos persistentes con progreso en vivo**: el gestor de trabajos guarda estado, log y
  progreso en disco. Si el servidor se reinicia, los trabajos en curso se marcan como
  interrumpidos en vez de perderse. La interfaz consume el avance por polling.
- **Motores como subprocesos**: los OCR (MinerU, Chandra) corren en sus propios entornos
  virtuales y se invocan como procesos. Esto aísla dependencias pesadas y permite cancelarlos.
- **Protección de estructura al traducir**: el texto se divide en items (traducible, inline
  protegido, intacto). Código, fórmulas LaTeX, URLs, imágenes y pipes de tablas nunca se envían
  al modelo; en el caso del LLM se reemplazan por marcadores `{{P1}}`, `{{P2}}` que deben
  conservarse intactos. Así la traducción no rompe el Markdown.
- **Traducción por lotes con chunking**: el modelo neuronal (NLLB/OPUS-MT) tiene límite de
  tokens por entrada, por lo que los párrafos largos se fragmentan y se reensamblan.
- **Dos motores de traducción**: uno neuronal local rápido (CTranslate2) y uno de calidad vía LLM
  local (LM Studio con API compatible con OpenAI), seleccionable por trabajo.
- **Manga in-place**: se detectan las burbujas, se hace OCR del japonés, se borra el texto
  original con inpainting y se renderiza la traducción ajustada al espacio disponible.

## Stack

Python 3.11, FastAPI + Uvicorn, PyMuPDF, PyYAML, CTranslate2 / transformers (motor neuronal
local), y conexión HTTP a un servidor LLM local. Interfaz web en HTML/CSS/JavaScript sin frameworks.

## Dependencias externas

Los motores de conversión y de manga son proyectos independientes que se configuran por ruta en
`config.yaml`:

- MinerU y Chandra para PDF a Markdown (OCR de documentos).
- manga-image-translator para la traducción de manga.
- Un servidor LLM local compatible con la API de OpenAI (por ejemplo LM Studio) para el motor de
  traducción de mayor calidad.

Ninguno es obligatorio: el sistema arranca y muestra el estado de cada motor.

## Instalación y uso

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml    # ajustar rutas de los motores que se usen
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Luego abrir `http://127.0.0.1:8000`.

## Nota

El repositorio no incluye documentos, salidas ni modelos: `config.yaml`, las carpetas de trabajo
y los pesos de los modelos están excluidos por `.gitignore`.

## Uso responsable

La herramienta traduce documentos e imágenes. Se debe utilizar únicamente con material sobre el
cual se tenga derecho a hacerlo: obras propias, de dominio público, con licencia que lo permita, o
con autorización del titular. Traducir y redistribuir obras protegidas por derechos de autor sin
autorización puede constituir una infracción, y esa responsabilidad es de quien usa el software.

El código se entrega tal cual, sin garantía, según los términos de la licencia MIT.

## Licencia

MIT. Ver `LICENSE`.
