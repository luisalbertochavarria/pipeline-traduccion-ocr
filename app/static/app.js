/* Sistema PDF Markdown - logica frontend (vanilla JS) */
'use strict';

const $ = (id) => document.getElementById(id);

// ---------- utilidades ----------
function toast(mensaje, tipo) {
  const t = $('toast');
  t.textContent = mensaje;
  t.className = 'toast ' + (tipo || '');
  setTimeout(() => { t.className = 'toast oculto'; }, 4000);
}
async function api(ruta, opciones) {
  const r = await fetch(ruta, opciones);
  let data = {};
  try { data = await r.json(); } catch (e) { /* respuesta no JSON */ }
  return data;
}
function fmtBytes(n) {
  if (!n) return '';
  if (n > 1048576) return (n / 1048576).toFixed(1) + ' MB';
  if (n > 1024) return (n / 1024).toFixed(0) + ' KB';
  return n + ' B';
}

// ---------- navegacion ----------
document.querySelectorAll('.nav-item').forEach((btn) => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.nav-item').forEach((b) => b.classList.remove('activo'));
    document.querySelectorAll('.vista').forEach((v) => v.classList.remove('activa'));
    btn.classList.add('activo');
    $('vista-' + btn.dataset.vista).classList.add('activa');
  });
});

// ---------- radio-cards ----------
function initRadioCards(selector, onCambio) {
  document.querySelectorAll(selector).forEach((card) => {
    card.addEventListener('click', () => {
      document.querySelectorAll(selector).forEach((c) => c.classList.remove('activa'));
      card.classList.add('activa');
      card.querySelector('input').checked = true;
      onCambio && onCambio(card.dataset);
    });
  });
}
initRadioCards('.radio-card[data-modo]', () => {
  const lotes = document.querySelector('input[name="modo"]:checked').value === 'lotes';
  $('opciones-lotes').classList.toggle('oculto', !lotes);
});
initRadioCards('.radio-card[data-motor]');
initRadioCards('.radio-card[data-tmotor]', () => {
  const llm = document.querySelector('input[name="tmotor"]:checked').value === 'llm';
  $('capa-modelo').classList.toggle('oculto', !llm);
  if (llm) cargarModelos();
});

// ---------- estado global ----------
let lmConectadoCache = null;
async function lmConectado() {
  if (lmConectadoCache !== null) return lmConectadoCache;
  const m = await api('/api/modelos');
  lmConectadoCache = !!m.conectado;
  return lmConectadoCache;
}
async function cargarEstado() {
  const e = await api('/api/estado');
  if (!e.ok) return;
  const okLM = await lmConectado();
  $('dot-lmstudio').className = 'punto ' + (okLM ? 'ok' : 'mal');
  $('lmstudio-estado').textContent = okLM ? 'conectado' : 'sin servidor';
  $('dot-nllb').className = 'punto ' + (e.opus ? 'ok' : 'mal');
  $('nllb-estado').textContent = e.opus ? 'modelo listo' : 'sin modelo';
}

// ---------- archivos ----------
async function cargarArchivos() {
  const pdfs = await api('/api/archivos?tipo=pdf');
  const sel = $('sel-pdf');
  sel.innerHTML = '';
  const lista = pdfs.archivos || [];
  if (!lista.length) sel.innerHTML = '<option value="">Sin PDFs en la carpeta de entrada</option>';
  lista.forEach((a) => {
    const opt = document.createElement('option');
    opt.value = a.ruta;
    opt.textContent = (a.carpeta ? a.carpeta + ' / ' : '') + a.nombre + '  (' + fmtBytes(a.tamano) + ')';
    sel.appendChild(opt);
  });

  const mds = await api('/api/archivos?tipo=md');
  const selMd = $('sel-md');
  selMd.innerHTML = '';
  const listaMd = mds.archivos || [];
  if (!listaMd.length) selMd.innerHTML = '<option value="">Sin markdown disponibles</option>';
  listaMd.forEach((a) => {
    const opt = document.createElement('option');
    opt.value = a.ruta;
    opt.textContent = (a.origen === 'traducciones' ? '[traduccion] ' : '[salida] ') +
      (a.carpeta ? a.carpeta + ' / ' : '') + a.nombre;
    selMd.appendChild(opt);
  });
}

// ---------- modelos LM Studio ----------
async function cargarModelos(selId, notaId, defaultKey) {
  const m = await api('/api/modelos');
  const sel = $(selId || 'sel-modelo');
  sel.innerHTML = '';
  if (m.conectado && m.modelos.length) {
    m.modelos.forEach((mod) => {
      const opt = document.createElement('option');
      opt.value = mod.id;
      opt.textContent = mod.id;
      sel.appendChild(opt);
    });
    $(notaId || 'nota-lmstudio').textContent = 'LM Studio conectado (' + m.modelos.length + ' modelos)';
    const e = await api('/api/estado');
    const def = defaultKey === 'manga' ? (e.manga && e.manga.default) : e.llm_default;
    if (def && [...sel.options].some((o) => o.value === def)) sel.value = def;
  } else {
    sel.innerHTML = '<option value="">LM Studio no disponible</option>';
    $(notaId || 'nota-lmstudio').textContent = m.error ? 'Error: ' + m.error : 'LM Studio sin servidor';
  }
}

// ---------- subida ----------
function subirArchivo(input, tipo, recargar) {
  input.addEventListener('change', async () => {
    const archivo = input.files && input.files[0];
    if (!archivo) return;
    const fd = new FormData();
    fd.append('archivo', archivo);
    const r = await api('/api/subir?tipo=' + tipo, { method: 'POST', body: fd });
    if (r.ok) { toast('Archivo subido: ' + r.archivo, 'ok'); recargar && recargar(); }
    else toast(r.error || 'Error al subir', 'error');
    input.value = '';
  });
}
subirArchivo($('file-pdf'), 'pdf', cargarArchivos);
subirArchivo($('file-md'), 'md', cargarArchivos);

// ---------- job: stream SSE ----------
function abrirStream(jid, ids, alFinal) {
  const es = new EventSource('/api/jobs/' + jid + '/stream');
  ids.progreso.style.width = '0%';
  ids.log.textContent = '';
  es.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (e) { return; }
    if (msg.type === 'estado') {
      ids.estado.textContent = msg.estado;
      ids.estado.className = 'badge ' + msg.estado;
      ids.progreso.style.width = (msg.progreso || 0) + '%';
      ids.paso.textContent = msg.paso || '';
    } else if (msg.type === 'log') {
      (msg.lineas || []).forEach((l) => { ids.log.textContent += l + '\n'; });
      ids.log.scrollTop = ids.log.scrollHeight;
    } else if (msg.type === 'fin') {
      es.close();
      ids.estado.textContent = msg.estado;
      ids.estado.className = 'badge ' + msg.estado;
      ids.progreso.style.width = '100%';
      alFinal && alFinal(msg.resultado, msg.estado);
    } else if (msg.type === 'error') {
      es.close();
      toast(msg.text, 'error');
    }
  };
  es.onerror = () => { es.close(); toast('Conexion con el servidor perdida', 'error'); };
  return es;
}

// ---------- transformar ----------
let streamTransformar = null;
let jidTransformar = null;

$('btn-transformar').addEventListener('click', async () => {
  const archivo = $('sel-pdf').value;
  if (!archivo) return toast('Selecciona un PDF', 'error');
  const modo = document.querySelector('input[name="modo"]:checked').value;
  const motor = document.querySelector('input[name="motor"]:checked').value;
  const body = {
    archivo, modo, motor,
    paginas_por_lote: parseInt($('paginas-lote').value, 10) || 10,
    limpiar: $('chk-limpiar').checked ? 'auto' : 'no',
    unir: $('chk-unir').checked,
  };
  const r = await api('/api/transformar', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  if (!r.job_id) return toast(r.error || 'Error al iniciar', 'error');
  toast('Trabajo iniciado', 'ok');
  jidTransformar = r.job_id;
  $('job-transformar').classList.remove('oculto');
  $('job-titulo').textContent = 'Transformando: ' + archivo;
  $('btn-descargar').classList.add('oculto');
  $('btn-cancelar').classList.remove('oculto');
  if (streamTransformar) streamTransformar.close();
  streamTransformar = abrirStream(jidTransformar, {
    progreso: $('job-progreso'), estado: $('job-estado'), paso: $('job-paso'), log: $('job-log'),
  }, (resultado, estado) => {
    $('btn-cancelar').classList.add('oculto');
    if (estado === 'completado' && resultado) {
      const partes = [
        'Motor ' + resultado.motor + ' | modo ' + resultado.modo,
        resultado.bloques ? 'bloques: ' + resultado.bloques : '',
        'tiempo: ' + resultado.tiempo_seg + ' s',
        resultado.imagenes_rotas != null ? 'imagenes rotas: ' + resultado.imagenes_rotas : '',
        resultado.unido ? 'unido: ' + resultado.unido : '',
      ];
      $('job-paso').textContent = partes.filter(Boolean).join(' | ');
      $('btn-descargar').href = '/api/resultado/' + jidTransformar + '/descargar';
      $('btn-descargar').classList.remove('oculto');
    } else if (estado === 'error') {
      $('job-titulo').textContent = 'Error en el trabajo';
    }
  });
});

$('btn-cancelar').addEventListener('click', async () => {
  if (!jidTransformar) return;
  await api('/api/jobs/' + jidTransformar + '/cancelar', { method: 'POST' });
  toast('Cancelando...');
});

// ---------- traducir ----------
let streamTraducir = null;
let jidTraducir = null;

$('btn-traducir').addEventListener('click', async () => {
  const archivo = $('sel-md').value;
  if (!archivo) return toast('Selecciona un archivo markdown', 'error');
  const motor = document.querySelector('input[name="tmotor"]:checked').value;
  const body = {
    archivo, motor,
    modelo: $('sel-modelo').value || '',
    chunk_chars: parseInt($('chunk-chars').value, 10) || 0,
    forzar: $('chk-forzar').checked,
    reanudar: $('chk-reanudar').checked,
  };
  const r = await api('/api/traducir', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  if (!r.job_id) return toast(r.error || 'Error al iniciar', 'error');
  toast('Traduccion iniciada', 'ok');
  jidTraducir = r.job_id;
  $('job-traducir').classList.remove('oculto');
  $('tj-titulo').textContent = 'Traduciendo: ' + archivo;
  $('tj-descargar').classList.add('oculto');
  $('tj-cancelar').classList.remove('oculto');
  if (streamTraducir) streamTraducir.close();
  streamTraducir = abrirStream(jidTraducir, {
    progreso: $('tj-progreso'), estado: $('tj-estado'), paso: $('tj-paso'), log: $('tj-log'),
  }, (resultado, estado) => {
    $('tj-cancelar').classList.add('oculto');
    if (estado === 'completado' && resultado) {
      if (resultado.sin_cambios) {
        $('tj-paso').textContent = resultado.mensaje || 'Documento ya en espanol';
        return;
      }
      $('tj-paso').textContent =
        'Motor ' + (resultado.motor || '') + ' | items traducidos: ' +
        (resultado.traducidos != null ? resultado.traducidos : resultado.completados) +
        ' | tiempo: ' + (resultado.tiempo_seg || 0) + ' s';
      if (resultado.salida) {
        const nombre = resultado.salida.split(/[\\/]/).pop();
        $('tj-descargar').href = '/api/traducciones/' + encodeURIComponent(nombre);
        $('tj-descargar').classList.remove('oculto');
      }
    } else if (estado === 'error') {
      $('tj-titulo').textContent = 'Error en la traduccion';
    }
  });
});

$('tj-cancelar').addEventListener('click', async () => {
  if (!jidTraducir) return;
  await api('/api/jobs/' + jidTraducir + '/cancelar', { method: 'POST' });
  toast('Cancelando (el parcial queda resumible)');
});

// ---------- arranque ----------
cargarEstado();
cargarArchivos();
setInterval(() => { lmConectadoCache = null; cargarEstado(); }, 30000);

// ============ MANGA (traduccion in-place) ============
async function cargarMangaArchivos() {
  const r = await api('/api/manga/archivos');
  const sel = $('sel-manga-pdf');
  sel.innerHTML = '';
  const lista = r.archivos || [];
  if (!lista.length) sel.innerHTML = '<option value="">Sin mangas PDF subidos</option>';
  lista.forEach((a) => {
    const opt = document.createElement('option');
    opt.value = a.nombre;
    opt.textContent = a.nombre + '  (' + fmtBytes(a.tamano) + ')';
    sel.appendChild(opt);
  });
}

$('file-manga-pdf').addEventListener('change', async (ev) => {
  const archivo = ev.target.files && ev.target.files[0];
  if (!archivo) return;
  const fd = new FormData();
  fd.append('archivo', archivo);
  const r = await api('/api/manga/subir', { method: 'POST', body: fd });
  if (r.ok) { toast('Manga subido: ' + r.archivo, 'ok'); cargarMangaArchivos(); }
  else toast(r.error || 'Error al subir', 'error');
  ev.target.value = '';
});

initRadioCards('.radio-card[data-midioma]');
initRadioCards('.radio-card[data-mmotor]', () => {
  const motor = document.querySelector('input[name="mmotor"]:checked').value;
  $('capa-manga-modelo').classList.toggle('oculto', motor !== 'llm');
  $('capa-manga-deepseek').classList.toggle('oculto', motor !== 'deepseek');
  if (motor === 'llm') cargarModelos('sel-manga-modelo', 'nota-manga-lmstudio', 'manga');
});

// seleccionar el modelo de manga junto al de traduccion
cargarModelos('sel-manga-modelo', 'nota-manga-lmstudio', 'manga');
document.querySelector('[data-tmotor="llm"]').addEventListener('click', () => cargarModelos());
document.querySelector('[data-tmotor="opus"]').addEventListener('click', () => { /* nada */ });

let streamManga = null;
let jidManga = null;
let mangaNombre = null;

$('btn-manga').addEventListener('click', async () => {
  const archivo = $('sel-manga-pdf').value;
  if (!archivo) return toast('Selecciona un PDF de manga', 'error');
  const body = {
    archivo,
    idioma: document.querySelector('input[name="midioma"]:checked').value,
    motor: document.querySelector('input[name="mmotor"]:checked').value,
    modelo: $('sel-manga-modelo').value || '',
    deepseek_key: $('manga-deepseek-key').value.trim(),
    deepseek_model: $('manga-deepseek-model').value.trim(),
    nombre_salida: $('manga-nombre').value.trim(),
    dpi: parseInt($('manga-dpi').value, 10) || 200,
    fso: parseInt($('manga-fso').value, 10) || 0,
    reanudar: $('chk-manga-reanudar').checked,
    omitir_paginas: $('manga-omitir').value.trim(),
    paginas_texto: $('manga-texto').value.trim(),
  };
  const r = await api('/api/manga/transformar', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  if (!r.job_id) return toast(r.error || 'Error al iniciar', 'error');
  toast('Traduccion de manga iniciada', 'ok');
  jidManga = r.job_id;
  mangaNombre = null;
  $('job-manga').classList.remove('oculto');
  $('manga-resultado').classList.add('oculto');
  $('mj-titulo').textContent = 'Traduciendo: ' + archivo;
  $('mj-pdf').classList.add('oculto');
  $('mj-cancelar').classList.remove('oculto');
  if (streamManga) streamManga.close();
  streamManga = abrirStream(jidManga, {
    progreso: $('mj-progreso'), estado: $('mj-estado'), paso: $('mj-paso'), log: $('mj-log'),
  }, (resultado, estado) => {
    $('mj-cancelar').classList.add('oculto');
    if (estado === 'completado' && resultado) {
      $('mj-paso').textContent =
        'paginas: ' + resultado.paginas + ' | traducidas: ' + resultado.traducidas +
        ' | tiempo: ' + resultado.tiempo_seg + ' s';
      if (resultado.pdf) {
        const nombre = resultado.pdf.split(/[\\/]/).pop();
        mangaNombre = nombre.replace(/\.pdf$/, '');
        $('mj-pdf').href = '/api/manga/salidas/' + encodeURIComponent(mangaNombre) + '/pdf';
        $('mj-pdf').classList.remove('oculto');
        cargarMangaPaginas(mangaNombre);
      }
    } else if (estado === 'error') {
      $('mj-titulo').textContent = 'Error en la traduccion de manga';
    }
  });
});

$('mj-cancelar').addEventListener('click', async () => {
  if (!jidManga) return;
  await api('/api/jobs/' + jidManga + '/cancelar', { method: 'POST' });
  toast('Cancelando (el parcial queda resumible)');
});

async function cargarMangaPaginas(nombre) {
  const r = await api('/api/manga/salidas/' + encodeURIComponent(nombre) + '/paginas');
  const cont = $('manga-paginas');
  cont.innerHTML = '';
  (r.paginas || []).forEach((f) => {
    const n = parseInt(f.replace(/[^0-9]/g, ''), 10) || 0;
    const a = document.createElement('a');
    a.href = '/api/manga/salidas/' + encodeURIComponent(nombre) + '/pagina/' + n;
    a.target = '_blank';
    a.title = f;
    const img = document.createElement('img');
    img.src = a.href;
    img.alt = f;
    a.appendChild(img);
    cont.appendChild(a);
  });
  $('manga-resultado').classList.remove('oculto');
}

cargarMangaArchivos();
setInterval(cargarMangaArchivos, 60000);
