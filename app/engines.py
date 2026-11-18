# engines.py - Ejecucion de motores (MinerU/Chandra) y utilidades como subprocesos
import glob
import os
import re
import subprocess

from .config import cfg
from .jobs import jobs


def _env():
    env = dict(os.environ)
    env['PYTHONPATH'] = ''          # quirk: PYTHONPATH global de Hermes sombrea venvs
    env['PYTHONIOENCODING'] = 'utf-8'
    return env


def _spawn(cmd, cwd=None):
    return subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, env=_env(), cwd=cwd, shell=False,
        universal_newlines=True, encoding='utf-8', errors='replace',
        bufsize=1,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
    )


def ejecutar(job_id, cmd, cwd=None, prefijo=''):
    """Ejecuta un subproceso volcando su salida al log del job. Devuelve codigo de salida."""
    p = _spawn(cmd, cwd=cwd)
    jobs.registrar_proceso(job_id, p)
    for linea in p.stdout:
        linea = linea.rstrip('\r\n')
        if linea.strip():
            jobs.log(job_id, f'{prefijo}{linea}' if prefijo else linea)
    p.wait()
    return p.returncode


# ---------- utilidades ----------
def nombre_base(pdf):
    return os.path.splitext(os.path.basename(pdf))[0]


def limpiar_nombre(nombre):
    return re.sub(r'[\\/:*?"<>|]', '_', nombre).strip()


def buscar_md(carpeta, excluir_limpio=True):
    """Todos los .md bajo una carpeta (raiz de cada bloque)."""
    mds = []
    for dirpath, _dirs, files in os.walk(carpeta):
        if 'UNIDO' in dirpath or 'images' in dirpath or 'metadata' in dirpath:
            continue
        for fn in files:
            if fn.lower().endswith('.md'):
                if excluir_limpio and fn.endswith('_limpio.md'):
                    continue
                mds.append(os.path.join(dirpath, fn))
    return mds


# ---------- division ----------
def dividir_pdf(job_id, pdf, out_dir, paginas_por_lote):
    os.makedirs(out_dir, exist_ok=True)
    script = os.path.join(cfg.base, 'app', 'dividir.py')
    cmd = [cfg.python_chandra, script, pdf, out_dir, str(paginas_por_lote)]
    return ejecutar(job_id, cmd, prefijo='[dividir] ')


# ---------- motores ----------
def run_mineru(job_id, entrada, out):
    os.makedirs(out, exist_ok=True)
    cmd = [cfg.mineru_exe, '-p', entrada, '-o', out, '-b', 'pipeline', '-m', 'auto']
    return ejecutar(job_id, cmd, prefijo='[mineru] ')


def _num_paginas(pdf):
    """Paginas de un PDF (pymupdf)."""
    import pymupdf
    doc = pymupdf.open(pdf)
    n = doc.page_count
    doc.close()
    return n


def run_mineru_por_lotes(job_id, pdf, salida, paginas_por_lote):
    """MinerU por rangos de paginas (-s/-e): evita OOM en libros grandes.
    Cada bloque escribe en salida/bloque_pag_<ini>_a_<fin> (misma estructura que modo lotes);
    los bloques que fallan se registran y se continua."""
    import math
    total = _num_paginas(pdf)
    n_bloques = max(1, math.ceil(total / paginas_por_lote))
    jobs.log(job_id, f'[mineru] {total} paginas en {n_bloques} bloques de {paginas_por_lote}')
    ok = 0
    for i in range(n_bloques):
        if jobs.get(job_id)['cancelar']:
            jobs.set(job_id, estado='cancelado', paso='Cancelado por el usuario', progreso=100)
            return -1
        inicio = i * paginas_por_lote          # 0-based (inclusive)
        fin = min(total, inicio + paginas_por_lote) - 1  # 0-based (inclusive)
        bloque = f'bloque_pag_{inicio + 1}_a_{fin + 1}'
        out_bloque = os.path.join(salida, bloque)
        cmd = [cfg.mineru_exe, '-p', pdf, '-o', out_bloque, '-b', 'pipeline', '-m', 'auto',
               '-s', str(inicio), '-e', str(fin)]
        jobs.set(job_id, paso=f'MinerU bloque {i + 1}/{n_bloques}: paginas {inicio + 1}-{fin + 1}',
                 progreso=int(10 + 70 * i / n_bloques))
        jobs.log(job_id, f'[mineru] === bloque {i + 1}/{n_bloques}: paginas {inicio + 1}-{fin + 1} ===')
        rc = ejecutar(job_id, cmd, prefijo='[mineru] ')
        if rc == 0:
            ok += 1
        else:
            jobs.log(job_id, f'[ERROR] bloque {bloque} fallo (codigo {rc}); se continua')
    jobs.log(job_id, f'[mineru] {ok}/{n_bloques} bloques completados')
    return 0 if ok else 1


def run_chandra(job_id, entrada, out):
    os.makedirs(out, exist_ok=True)
    cmd = [cfg.chandra_exe, '--method', 'hf', entrada, out]
    return ejecutar(job_id, cmd, prefijo='[chandra] ')


def renombrar_salida_chandra(job_id, out_dir, bloque):
    """Chandra genera un .md con nombre propio; se renombra a <bloque>.md."""
    mds = [f for f in glob.glob(os.path.join(out_dir, '*.md'))]
    if len(mds) == 1 and os.path.basename(mds[0]) != f'{bloque}.md':
        destino = os.path.join(out_dir, f'{bloque}.md')
        if os.path.exists(destino):
            os.remove(destino)
        os.rename(mds[0], destino)
        jobs.log(job_id, f'[chandra] salida renombrada a {bloque}.md')
    return 0


def aplanar_mineru(job_id, salida):
    """MinerU deja salida/<bloque>/<metodo>/* ; aplana al nivel <bloque>/ para el unidor.
    Baja por subcarpetas unicas (ocr/, auto/, txt/) mientras no haya md en la raiz."""
    for d in sorted(os.listdir(salida)):
        ruta = os.path.join(salida, d)
        if not os.path.isdir(ruta) or not d.startswith('bloque_pag_'):
            continue
        while True:
            sub = [x for x in os.listdir(ruta) if os.path.isdir(os.path.join(ruta, x))]
            tiene_md = any(f.lower().endswith('.md') for f in os.listdir(ruta))
            if len(sub) == 1 and not tiene_md:
                inner = os.path.join(ruta, sub[0])
                for item in os.listdir(inner):
                    src = os.path.join(inner, item)
                    dst = os.path.join(ruta, item)
                    if not os.path.exists(dst):
                        os.replace(src, dst)
                os.rmdir(inner)
                jobs.log(job_id, f'[mineru] bloque {d} aplanado ({sub[0]} -> raiz)')
            else:
                break
        # renombrar el md del bloque a <bloque>.md (el unidor lo busca por ese nombre)
        mds = [f for f in os.listdir(ruta) if f.lower().endswith('.md')]
        if mds:
            limpio = [f for f in mds if f.endswith('_limpio.md')]
            md_actual = (limpio or mds)[0]
            destino = os.path.join(ruta, f'{d}_limpio.md' if limpio else f'{d}.md')
            if os.path.basename(md_actual) != os.path.basename(destino):
                if os.path.exists(destino):
                    os.remove(destino)
                os.rename(os.path.join(ruta, md_actual), destino)
                jobs.log(job_id, f'[mineru] bloque {d} md renombrado a {os.path.basename(destino)}')


# ---------- limpiador ortografico ----------
def limpiar_md(job_id, md):
    cmd = [cfg.python_mineru, cfg.limpiador_script, md]
    return ejecutar(job_id, cmd, prefijo='[limpiar] ')


# ---------- union de bloques ----------
def unir_bloques(job_id, dir_bloques):
    cmd = [cfg.python_chandra, cfg.unidor_script, dir_bloques]
    return ejecutar(job_id, cmd, prefijo='[unir] ')


def rotas_en_log(job_id, jid):
    """Extrae 'rotas: N' del ultimo log de union."""
    for linea in reversed(jobs.get(jid)['log']):
        m = re.search(r'rotas:\s*(\d+)', linea)
        if m:
            return int(m.group(1))
    return None
