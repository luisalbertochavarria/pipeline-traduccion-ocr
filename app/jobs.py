# jobs.py - Gestor de trabajos (transformar/traducir) con persistencia en disco
import json
import os
import threading
import time
import uuid
from datetime import datetime

from .config import cfg


class JobManager:
    def __init__(self, data_dir):
        self.jobs_dir = os.path.join(data_dir, 'jobs')
        os.makedirs(self.jobs_dir, exist_ok=True)
        self._jobs = {}
        self._lock = threading.Lock()
        self._procs = {}  # jid -> Popen (en memoria, para cancelar)
        self._cargar()

    # ---------- API publica ----------
    def crear(self, tipo, params):
        jid = datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:6]
        job = {
            'id': jid, 'tipo': tipo, 'estado': 'pendiente', 'progreso': 0,
            'paso': '', 'log': [], 'resultado': None, 'params': params,
            'cancelar': False, 'creado': time.time(), 'actualizado': time.time(),
        }
        with self._lock:
            self._jobs[jid] = job
        self._guardar(jid)
        return jid

    def get(self, jid):
        with self._lock:
            j = self._jobs.get(jid)
            return dict(j) if j else None

    def lista(self):
        with self._lock:
            return [dict(j) for j in sorted(self._jobs.values(), key=lambda j: j['creado'], reverse=True)]

    def log(self, jid, linea):
        with self._lock:
            j = self._jobs.get(jid)
            if not j:
                return
            j['log'].append(str(linea))
            if len(j['log']) > 800:
                j['log'] = j['log'][-800:]
            j['actualizado'] = time.time()
        self._guardar(jid)

    def set(self, jid, **campos):
        with self._lock:
            j = self._jobs.get(jid)
            if not j:
                return
            j.update(campos)
            j['actualizado'] = time.time()
        self._guardar(jid)

    def registrar_proceso(self, jid, popen):
        with self._lock:
            self._procs[jid] = popen

    def cancelar(self, jid):
        with self._lock:
            j = self._jobs.get(jid)
            if not j:
                return False
            j['cancelar'] = True
            popen = self._procs.get(jid)
        if popen is not None:
            try:
                import subprocess
                subprocess.run(['taskkill', '/F', '/T', '/PID', str(popen.pid)],
                               capture_output=True, timeout=10)
            except Exception:
                pass
        self._guardar(jid)
        return True

    # ---------- persistencia ----------
    def _guardar(self, jid):
        try:
            with self._lock:
                j = self._jobs.get(jid)
                if not j:
                    return
                snap = dict(j)
                with open(os.path.join(self.jobs_dir, jid + '.json'), 'w', encoding='utf-8') as f:
                    json.dump(snap, f, ensure_ascii=False, indent=1)
        except Exception:
            pass

    def _cargar(self):
        try:
            for fn in os.listdir(self.jobs_dir):
                if not fn.endswith('.json'):
                    continue
                try:
                    with open(os.path.join(self.jobs_dir, fn), encoding='utf-8') as f:
                        j = json.load(f)
                    if j.get('estado') in ('pendiente', 'procesando'):
                        j['estado'] = 'interrumpido'
                    j.setdefault('cancelar', False)
                    self._jobs[j['id']] = j
                except Exception:
                    continue
        except Exception:
            pass


# singleton del gestor de trabajos
jobs = JobManager(cfg.data_dir)
