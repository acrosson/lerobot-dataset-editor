/* LeRobot dataset editor — client.
 *
 * Everything the user does is staged on the server (a sidecar JSON next to the
 * dataset) and only written into data/ and meta/ when they hit Save. That means
 * a reload never loses work, and nothing half-edited can ever be on disk. */

const SERIES_COLORS = ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#008300'];
const GRIPPER_COLOR = '#9085e9';
const SPEED_COLOR = '#c3c2b7';
const ZOOMS = [0.25, 0.5, 1, 2, 3, 4, 6, 8, 12, 16, 24];

const state = {
  datasets: [],
  ds: null,
  episodes: [],
  filter: '',
  ep: null,
  frame: 0,
  playing: false,
  zoom: 4,
  loopCrop: false,
  hidden: new Set(),
  showAction: false,
  videos: [],
  stripStep: 20,
  dirty: 0,
};

const $ = id => document.getElementById(id);
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const fmt = (n, d = 2) => Number(n).toFixed(d);

async function api(path, body) {
  const res = await fetch(path, body
    ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
    : undefined);
  const json = await res.json().catch(() => ({ error: `${res.status} ${res.statusText}` }));
  if (!res.ok) throw new Error(json.error || res.statusText);
  return json;
}

function toast(message, isError = false) {
  const el = document.createElement('div');
  el.className = 'toast' + (isError ? ' err' : '');
  el.textContent = message;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), isError ? 6000 : 2600);
}

/* ------------------------------------------------------------------ dialogs */

function confirmDialog({ title, bodyHtml, okLabel, danger }) {
  const dlg = $('confirmDlg');
  $('confirmTitle').textContent = title;
  $('confirmBody').innerHTML = bodyHtml;
  const ok = $('confirmOk');
  ok.textContent = okLabel;
  ok.className = danger ? 'danger' : 'primary';
  ok.disabled = false;
  return new Promise(resolve => {
    const done = value => {
      ok.removeEventListener('click', onOk);
      $('confirmCancel').removeEventListener('click', onCancel);
      dlg.removeEventListener('close', onClose);
      dlg.close();
      resolve(value);
    };
    const onOk = () => done(true);
    const onCancel = () => done(false);
    const onClose = () => resolve(false);
    ok.addEventListener('click', onOk);
    $('confirmCancel').addEventListener('click', onCancel);
    dlg.addEventListener('close', onClose);
    dlg.showModal();
    ok.focus();  // Enter confirms, Esc cancels
  });
}

/* ----------------------------------------------------------------- datasets */

async function boot() {
  try {
    const info = await api('/api/datasets');
    state.datasets = info.datasets.filter(d => !d.error);
    const picker = $('dsPicker');
    picker.innerHTML = '';
    if (!state.datasets.length) {
      $('emptyState').textContent = `No readable LeRobot datasets under ${info.roots.join(', ')}`;
      return;
    }
    for (const d of info.datasets) {
      const opt = document.createElement('option');
      opt.value = d.path;
      // a dataset whose metadata won't parse still gets a row, greyed out, rather
      // than vanishing from the list with no explanation
      opt.disabled = Boolean(d.error);
      opt.textContent = d.error
        ? `${d.name}  (${d.error})`
        : `${d.name}  (${d.total_episodes} ep${d.staged_edits ? `, ${d.staged_edits} staged` : ''})`;
      picker.appendChild(opt);
    }
    picker.onchange = () => loadDataset(picker.value);
    const remembered = localStorage.getItem('lrde.dataset');
    const initial = state.datasets.some(d => d.path === remembered) ? remembered : state.datasets[0].path;
    picker.value = initial;
    await loadDataset(initial);
  } catch (err) {
    $('emptyState').textContent = err.message;
  }
}

async function loadDataset(path, position = 0) {
  localStorage.setItem('lrde.dataset', path);
  const ds = await api('/api/dataset?path=' + encodeURIComponent(path));
  state.ds = ds;
  state.episodes = ds.episodes;
  state.dirty = Object.keys(ds.staged).length;
  $('dsMeta').textContent =
    `${ds.total_episodes} episodes · ${ds.total_frames.toLocaleString()} frames · ${ds.fps} fps · ${ds.cameras.length} cam`;
  if (!ds.ffmpeg) toast('ffmpeg is not on PATH — video previews are unavailable', true);
  renderEpisodeList();
  updateDirty();
  const landing = state.episodes[clamp(position, 0, state.episodes.length - 1)] || state.episodes[0];
  if (landing) await openEpisode(landing.index);
}

function renderEpisodeList() {
  const list = $('epList');
  const needle = state.filter.trim().toLowerCase();
  list.innerHTML = '';
  let shown = 0;
  for (const ep of state.episodes) {
    if (needle && !(`${ep.index}`.includes(needle) || (ep.task || '').toLowerCase().includes(needle))) continue;
    shown++;
    const row = document.createElement('div');
    row.className = 'ep'
      + (state.ep && state.ep.index === ep.index ? ' active' : '')
      + (ep.deleted ? ' deleted' : '')
      + (!ep.deleted && (ep.start > 0 || ep.end < ep.length) ? ' trimmed' : '');
    const kept = ep.deleted ? 0 : ep.end - ep.start;
    row.innerHTML =
      `<span class="n">#${ep.index}</span>` +
      `<span class="t"></span>` +
      `<span class="s">${fmt(kept / state.ds.fps, 1)}s</span>`;
    row.querySelector('.t').textContent = ep.task || '';
    row.onclick = () => openEpisode(ep.index);
    list.appendChild(row);
  }
  const staged = state.episodes.filter(e => e.deleted).length;
  const foot = $('sidebarFoot');
  foot.textContent = `${shown} shown · ${staged} marked for deletion · `;
  const clear = document.createElement('button');
  clear.className = 'ghost';
  clear.style.padding = '0 4px';
  clear.textContent = 'clear preview cache';
  clear.title = 'Delete the generated preview clips and filmstrips in _editor/cache';
  clear.onclick = async () => {
    const res = await api('/api/cache/clear', { path: state.ds.path });
    toast(`Freed ${(res.freed_bytes / 1e6).toFixed(1)} MB of previews`);
    if (state.ep) buildStage();
  };
  foot.appendChild(clear);
}

function updateDirty() {
  const badge = $('stagedBadge');
  badge.hidden = state.dirty === 0;
  badge.className = 'badge' + (state.dirty ? ' on' : '');
  badge.textContent = `${state.dirty} staged edit${state.dirty === 1 ? '' : 's'}`;
  $('saveBtn').disabled = state.dirty === 0;
  $('revertBtn').disabled = state.dirty === 0;
}

/* ----------------------------------------------------------------- episodes */

async function openEpisode(index) {
  pause();
  const ep = await api(`/api/episode?path=${encodeURIComponent(state.ds.path)}&ep=${index}`);
  state.ep = ep;
  state.frame = ep.start;
  buildStage();
  renderEpisodeList();
  warmNeighbours(index);
}

function warmNeighbours(index) {
  const around = state.episodes
    .map(e => e.index)
    .filter(i => i > index && i <= index + 3);
  if (around.length) api('/api/warm', { path: state.ds.path, episodes: around }).catch(() => {});
}

function buildStage() {
  hideTooltip();
  const stage = $('stage');
  stage.innerHTML = '';
  stage.appendChild($('stageTpl').content.cloneNode(true));
  const ep = state.ep;

  $('epTitle').textContent = `Episode ${ep.index}`;
  $('prevBtn').onclick = () => step(-1);
  $('nextBtn').onclick = () => step(1);
  $('deleteBtn').onclick = deleteEpisode;
  if (ep.deleted) {
    $('deleteBtn').textContent = 'Undo delete';
    $('deleteBtn').className = '';
  }

  const task = $('taskInput');
  task.value = ep.task;
  task.classList.toggle('changed', ep.task !== ep.recorded_task);
  $('taskResetBtn').hidden = ep.task === ep.recorded_task;
  task.onchange = () => commitTask(task.value);
  $('taskResetBtn').onclick = () => { task.value = ep.recorded_task; commitTask(ep.recorded_task); };

  buildViewers();
  buildLegend();

  $('playBtn').onclick = togglePlay;
  $('stepBack').onclick = () => seek(state.frame - 1);
  $('stepFwd').onclick = () => seek(state.frame + 1);
  $('markIn').onclick = () => setCrop(state.frame, ep.end);
  $('markOut').onclick = () => setCrop(ep.start, Math.max(ep.start + 1, state.frame + 1));
  $('resetCrop').onclick = () => setCrop(0, ep.length);
  $('loopBtn').onclick = () => {
    state.loopCrop = !state.loopCrop;
    $('loopBtn').setAttribute('aria-pressed', String(state.loopCrop));
  };
  $('loopBtn').setAttribute('aria-pressed', String(state.loopCrop));
  $('zoomIn').onclick = () => zoomBy(1);
  $('zoomOut').onclick = () => zoomBy(-1);

  fitZoom();
  layoutTracks();
  attachTrackInteraction();
  seek(ep.start, true);
}

/* Clip and filmstrip URLs are keyed on (path, episode, camera), none of which
   change when a save crops an episode in place — but the video behind them does.
   Without this token the browser serves the pre-crop clip from its own cache
   until max-age expires, so the viewer disagrees with the timeline.
   The token is the server's own content hash of the slice, so same token ⇔ same
   bytes: caching stays aggressive but can never be wrong. */
function sliceToken(cam) {
  return cam.token || `${(cam.from_timestamp ?? 0).toFixed(4)}-${state.ep.length}`;
}

function buildViewers() {
  const wrap = $('viewers');
  wrap.innerHTML = '';
  state.videos = [];
  for (const cam of state.ep.cameras) {
    const box = document.createElement('div');
    box.className = 'viewer';
    const video = document.createElement('video');
    video.preload = 'auto';
    video.muted = true;
    video.playsInline = true;
    // reserve the right box before metadata arrives, so the layout doesn't jump
    if (cam.width && cam.height) video.style.aspectRatio = `${cam.width} / ${cam.height}`;
    video.src = `/api/clip?path=${encodeURIComponent(state.ds.path)}&ep=${state.ep.index}`
      + `&cam=${encodeURIComponent(cam.key)}&v=${sliceToken(cam)}`;
    const cap = document.createElement('span');
    cap.className = 'cap';
    cap.textContent = cam.label;
    box.append(video, cap);
    wrap.appendChild(box);
    state.videos.push(video);
  }
  if (state.videos[0]) {
    state.videos[0].addEventListener('ended', () => pause());
    state.videos[0].addEventListener('loadedmetadata', () => seek(state.frame, true));
  }
}

function buildLegend() {
  const legend = $('legend');
  legend.innerHTML = '';
  const names = state.ep.joint_names || [];
  names.slice(0, 6).forEach((name, i) => {
    const b = document.createElement('button');
    const on = !state.hidden.has(i);
    b.setAttribute('aria-pressed', String(on));
    b.innerHTML = `<span class="sw" style="background:${SERIES_COLORS[i]}"></span>`;
    b.append(document.createTextNode(name.replace('.pos', '')));
    b.onclick = () => {
      if (state.hidden.has(i)) state.hidden.delete(i); else state.hidden.add(i);
      buildLegend();
      drawCharts();
    };
    legend.appendChild(b);
  });
  const act = document.createElement('button');
  act.setAttribute('aria-pressed', String(state.showAction));
  act.innerHTML = `<span class="sw" style="background:repeating-linear-gradient(90deg,#c3c2b7 0 3px,transparent 3px 6px)"></span>`;
  act.append(document.createTextNode('commanded action (dashed)'));
  act.onclick = () => { state.showAction = !state.showAction; buildLegend(); drawCharts(); };
  legend.appendChild(act);
}

/* ------------------------------------------------------------------- tracks */

function trackWidth() { return state.ep.length * state.zoom; }
function xOfFrame(f) { return f * state.zoom; }
function frameOfX(x) { return clamp(Math.floor(x / state.zoom), 0, state.ep.length - 1); }

/* Open on "whole episode across the window" rather than snapping to a preset —
   the first thing you want to see is the shape of the entire take. */
function fitZoom() {
  const avail = ($('tracks').clientWidth || window.innerWidth - 260) - 2;
  state.zoom = clamp(avail / state.ep.length, ZOOMS[0], ZOOMS[ZOOMS.length - 1]);
}

function zoomBy(direction) {
  const at = state.frame;
  let next;
  if (direction > 0) next = ZOOMS.find(z => z > state.zoom * 1.001);
  else next = [...ZOOMS].reverse().find(z => z < state.zoom / 1.001);
  if (next === undefined) return;
  state.zoom = next;
  layoutTracks();
  centerOn(at);
}

function centerOn(frame) {
  const tracks = $('tracks');
  tracks.scrollLeft = xOfFrame(frame) - tracks.clientWidth / 2;
}

function layoutTracks() {
  const width = trackWidth();
  $('trackInner').style.width = width + 'px';
  $('strip').style.width = width + 'px';
  loadFilmstrip();
  Chart.ruler($('ruler').querySelector('canvas'), {
    frames: state.ep.length, px: state.zoom, fps: state.ep.fps, height: 18,
  });
  drawCharts();
  updateCropUi();
  updatePlayhead();
  pinLabels();
}

/* Chart labels sit inside the scrolling track, so slide them back by the scroll
   offset to keep them readable at the left edge. */
function pinLabels() {
  const x = $('tracks').scrollLeft;
  for (const label of document.querySelectorAll('.chart .label')) {
    label.style.transform = `translateX(${x}px)`;
  }
}

function loadFilmstrip() {
  const step = clamp(Math.round(80 / state.zoom), 1, 400);
  state.stripStep = step;
  const cam = state.ep.cameras[0];
  if (!cam) return;
  const url = `/api/filmstrip?path=${encodeURIComponent(state.ds.path)}&ep=${state.ep.index}`
    + `&cam=${encodeURIComponent(cam.key)}&step=${step}&v=${sliceToken(cam)}`;
  const strip = $('strip');
  const img = new Image();
  img.onload = () => {
    const tiles = Math.round(img.naturalWidth / 80);
    strip.style.backgroundImage = `url("${url}")`;
    // each tile covers `step` frames, so it is drawn `step * zoom` px wide
    strip.style.backgroundSize = `${tiles * step * state.zoom}px 56px`;
  };
  img.onerror = () => { strip.style.backgroundImage = 'none'; };
  img.src = url;
}

function drawCharts() {
  const ep = state.ep;
  const px = state.zoom;
  const jointCount = Math.min(6, (ep.joint_names || []).length);

  const series = [];
  for (let i = 0; i < jointCount; i++) {
    series.push({
      name: ep.joint_names[i], color: SERIES_COLORS[i], visible: !state.hidden.has(i),
      data: ep.state.map(row => row[i]),
    });
  }
  if (state.showAction && ep.action) {
    for (let i = 0; i < jointCount; i++) {
      series.push({
        name: ep.joint_names[i] + ' (action)', color: SERIES_COLORS[i], visible: !state.hidden.has(i),
        data: ep.action.map(row => row[i]), dash: [4, 3], width: 1.5, alpha: 0.75,
      });
    }
  }
  state.jointSeries = series;
  const jointRange = Chart.line($('chartJoints').querySelector('canvas'), {
    series, frames: ep.length, px, height: 138,
  });
  $('chartJoints').querySelector('.label').textContent =
    `joint positions (rad) · ${fmt(jointRange[0])} … ${fmt(jointRange[1])}`;

  const gi = (ep.joint_names || []).length - 1;
  const gripper = [{
    name: 'gripper', color: GRIPPER_COLOR, visible: true,
    data: ep.state.map(row => row[gi]),
  }];
  if (state.showAction && ep.action) {
    gripper.push({
      name: 'gripper (action)', color: GRIPPER_COLOR, visible: true,
      data: ep.action.map(row => row[gi]), dash: [4, 3], width: 1.5, alpha: 0.75,
    });
  }
  state.gripperSeries = gripper;
  const gripperRange = Chart.line($('chartGripper').querySelector('canvas'), {
    series: gripper, frames: ep.length, px, height: 66,
  });
  $('chartGripper').querySelector('.label').textContent =
    `gripper · ${fmt(gripperRange[0])} … ${fmt(gripperRange[1])}`;

  if (ep.speed) {
    Chart.area($('chartSpeed').querySelector('canvas'), {
      data: ep.speed, frames: ep.length, px, height: 50, color: SPEED_COLOR,
    });
  }
}

/* -------------------------------------------------------------- crop + play */

function updateCropUi() {
  const ep = state.ep;
  $('maskLeft').style.width = xOfFrame(ep.start) + 'px';
  $('maskRight').style.width = (trackWidth() - xOfFrame(ep.end)) + 'px';
  $('handleIn').style.left = xOfFrame(ep.start) + 'px';
  $('handleOut').style.left = xOfFrame(ep.end) + 'px';
  const kept = ep.end - ep.start;
  $('cropInfo').textContent =
    `keep ${ep.start}–${ep.end} · ${kept}/${ep.length} frames · ${fmt(kept / ep.fps)}s`
    + (kept < ep.length ? ` (−${fmt((ep.length - kept) / ep.fps)}s)` : '');
  $('epTitle').textContent = `Episode ${ep.index}` + (ep.deleted ? ' — will be deleted' : '');
}

let cropTimer = null;
function setCrop(start, end) {
  const ep = state.ep;
  ep.start = clamp(Math.round(start), 0, ep.length - 1);
  ep.end = clamp(Math.round(end), ep.start + 1, ep.length);
  updateCropUi();
  clearTimeout(cropTimer);
  cropTimer = setTimeout(commitCrop, 220);
}

async function commitCrop() {
  const ep = state.ep;
  try {
    const res = await api('/api/edit', {
      path: state.ds.path, episode: ep.index, start: ep.start, end: ep.end,
    });
    syncEpisodeRow(res);
  } catch (err) { toast(err.message, true); }
}

async function commitTask(text) {
  try {
    const res = await api('/api/edit', {
      path: state.ds.path, episode: state.ep.index, task: text,
    });
    state.ep.task = text;
    $('taskInput').classList.toggle('changed', text !== state.ep.recorded_task);
    $('taskResetBtn').hidden = text === state.ep.recorded_task;
    syncEpisodeRow(res);
  } catch (err) { toast(err.message, true); }
}

function syncEpisodeRow(res) {
  const row = state.episodes.find(e => e.index === res.episode);
  if (row) {
    row.start = res.start; row.end = res.end; row.deleted = res.deleted;
    row.task = state.ep.task;
  }
  state.dirty = res.staged_total;
  updateDirty();
  renderEpisodeList();
}

function seek(frame, force = false) {
  const ep = state.ep;
  const f = clamp(Math.round(frame), 0, ep.length - 1);
  if (f === state.frame && !force) { updatePlayhead(); return; }
  state.frame = f;
  const t = f / ep.fps;
  for (const v of state.videos) {
    if (v.readyState >= 1 && Math.abs(v.currentTime - t) > 0.5 / ep.fps) v.currentTime = t;
  }
  updatePlayhead();
}

function step(direction) {
  const list = state.episodes;
  const at = list.findIndex(e => e.index === state.ep.index);
  const next = list[clamp(at + direction, 0, list.length - 1)];
  if (next && next.index !== state.ep.index) openEpisode(next.index);
}

function updatePlayhead() {
  const x = xOfFrame(state.frame + 0.5);
  const head = $('playhead');
  if (head) head.style.left = x + 'px';
  const ep = state.ep;
  const clock = $('clock');
  if (clock) {
    clock.textContent = `frame ${state.frame} / ${ep.length - 1}   ${fmt(state.frame / ep.fps)}s`;
  }
}

function togglePlay() { state.playing ? pause() : play(); }

function play() {
  if (!state.videos.length) return;
  if (state.loopCrop && (state.frame < state.ep.start || state.frame >= state.ep.end - 1)) {
    seek(state.ep.start, true);
  }
  state.playing = true;
  $('playBtn').textContent = '⏸';
  for (const v of state.videos) v.play().catch(() => {});
  requestAnimationFrame(tick);
}

function pause() {
  state.playing = false;
  const btn = $('playBtn');
  if (btn) btn.textContent = '▶';
  for (const v of state.videos) v.pause();
}

function tick() {
  if (!state.playing) return;
  const [master, ...rest] = state.videos;
  if (master) {
    const t = master.currentTime;
    for (const v of rest) {
      if (Math.abs(v.currentTime - t) > 0.12) v.currentTime = t;
    }
    const f = clamp(Math.floor(t * state.ep.fps), 0, state.ep.length - 1);
    if (f !== state.frame) { state.frame = f; updatePlayhead(); autoScroll(); }
    if (state.loopCrop && f >= state.ep.end - 1) { seek(state.ep.start, true); }
  }
  requestAnimationFrame(tick);
}

function autoScroll() {
  const tracks = $('tracks');
  const x = xOfFrame(state.frame);
  if (x < tracks.scrollLeft + 40 || x > tracks.scrollLeft + tracks.clientWidth - 40) {
    tracks.scrollLeft = x - tracks.clientWidth * 0.35;
  }
}

/* ------------------------------------------------------- pointer interaction */

/* One tooltip for the whole app, not one per episode.
   attachTrackInteraction() re-runs on every episode change, and its listeners die
   with the .trackinner that buildStage() replaces — so a tooltip created per call
   is orphaned mid-hover with nothing left to hide it, and they pile up on screen
   until a reload. */
let tooltipEl = null;
function crosshairTooltip() {
  if (!tooltipEl || !tooltipEl.isConnected) {
    tooltipEl = document.createElement('div');
    tooltipEl.className = 'tooltip';
    document.body.appendChild(tooltipEl);
  }
  tooltipEl.hidden = true;
  return tooltipEl;
}

function hideTooltip() {
  if (tooltipEl) tooltipEl.hidden = true;
}

function attachTrackInteraction() {
  const tracks = $('tracks');
  const inner = $('trackInner');

  const localX = event => {
    const rect = inner.getBoundingClientRect();
    return clamp(event.clientX - rect.left, 0, trackWidth());
  };

  inner.addEventListener('pointerdown', event => {
    if (event.target.classList.contains('handle')) return;
    pause();
    seek(frameOfX(localX(event)));
    const move = e => seek(frameOfX(localX(e)));
    const up = () => { window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up); };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
  });

  for (const [id, isStart] of [['handleIn', true], ['handleOut', false]]) {
    const handle = $(id);
    handle.addEventListener('pointerdown', event => {
      event.preventDefault();
      event.stopPropagation();
      pause();
      handle.classList.add('drag');
      // listeners go on the window, not the handle: the pointer leaves the 11px
      // hit area immediately and capture is not reliable across browsers
      const move = e => {
        const f = clamp(Math.round(localX(e) / state.zoom), 0, state.ep.length);
        if (isStart) setCrop(Math.min(f, state.ep.end - 1), state.ep.end);
        else setCrop(state.ep.start, Math.max(f, state.ep.start + 1));
        seek(isStart ? state.ep.start : state.ep.end - 1);
      };
      const up = e => {
        move(e);
        handle.classList.remove('drag');
        window.removeEventListener('pointermove', move);
        window.removeEventListener('pointerup', up);
      };
      window.addEventListener('pointermove', move);
      window.addEventListener('pointerup', up);
    });
  }

  const tip = crosshairTooltip();
  inner.addEventListener('pointermove', event => {
    if (event.target.classList.contains('handle')) { tip.hidden = true; return; }
    const f = frameOfX(localX(event));
    tip.hidden = false;
    tip.style.left = Math.min(window.innerWidth - 220, event.clientX + 14) + 'px';
    tip.style.top = Math.min(window.innerHeight - 180, event.clientY + 14) + 'px';
    tip.innerHTML = valuesTable(f);
  });
  inner.addEventListener('pointerleave', () => { tip.hidden = true; });
  tracks.addEventListener('scroll', () => { tip.hidden = true; pinLabels(); }, { passive: true });
}

/* The table view the accessibility pass asks for: exact numbers for every
   series at one frame, never colour alone. */
function valuesTable(frame) {
  const ep = state.ep;
  const rows = [`frame ${frame}   ${fmt(frame / ep.fps)}s`];
  (ep.joint_names || []).forEach((name, i) => {
    if (i < 6 && state.hidden.has(i)) return;
    const color = i < 6 ? SERIES_COLORS[i] : GRIPPER_COLOR;
    const value = ep.state[frame] ? fmt(ep.state[frame][i], 3).padStart(7) : '   —';
    const act = state.showAction && ep.action && ep.action[frame]
      ? `  ⇢ ${fmt(ep.action[frame][i], 3).padStart(7)}` : '';
    rows.push(`<span class="sw" style="background:${color}"></span>${name.replace('.pos', '').padEnd(8)}${value}${act}`);
  });
  return rows.join('\n');
}

/* ------------------------------------------------------------ delete + save */

async function deleteEpisode() {
  const ep = state.ep;
  if (ep.deleted) {
    const res = await api('/api/edit', { path: state.ds.path, episode: ep.index, deleted: false });
    ep.deleted = false;
    syncEpisodeRow(res);
    $('deleteBtn').textContent = 'Delete episode';
    $('deleteBtn').className = 'danger';
    updateCropUi();
    return;
  }
  const ok = await confirmDialog({
    title: `Delete episode ${ep.index}?`,
    okLabel: 'Mark for deletion',
    danger: true,
    bodyHtml:
      `<p>This drops <strong>${ep.length} frames</strong> (${fmt(ep.length / ep.fps)}s) and renumbers the
       episodes after it.</p>
       <p class="note">Staged only — nothing is written until you hit Save, and you can undo it from this
       same button. Its frames stay inside the shared mp4 as unreferenced bytes; the video is never
       re-encoded.</p>`,
  });
  if (!ok) return;
  const res = await api('/api/edit', { path: state.ds.path, episode: ep.index, deleted: true });
  ep.deleted = true;
  syncEpisodeRow(res);
  $('deleteBtn').textContent = 'Undo delete';
  $('deleteBtn').className = '';
  updateCropUi();
}

async function save() {
  if (!state.dirty) return;
  let plan;
  try {
    plan = await api('/api/plan?path=' + encodeURIComponent(state.ds.path));
  } catch (err) { toast(err.message, true); return; }

  const trimmedFrames = plan.trimmed.reduce((n, t) => n + (t.was - t.now), 0);
  const rows = [
    ['episodes', `${plan.episodes_before} → ${plan.episodes_after}`],
    ['frames', `${plan.frames_before.toLocaleString()} → ${plan.frames_after.toLocaleString()}`],
    ['deleted', plan.deleted.length ? plan.deleted.join(', ') : '—'],
    ['trimmed', plan.trimmed.length ? `${plan.trimmed.length} episodes, −${trimmedFrames} frames` : '—'],
    ['task edits', plan.retasked.length || '—'],
  ];
  const errors = plan.errors.length
    ? `<p class="err">${plan.errors.join('<br>')}</p>` : '';

  const ok = await confirmDialog({
    title: `Save to ${state.ds.name}?`,
    okLabel: plan.errors.length ? 'Cannot save' : 'Write to disk',
    bodyHtml:
      `<table>${rows.map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join('')}</table>` +
      errors +
      `<p class="note">Writes <code>data/</code> and <code>meta/</code> in place. Both are hardlinked into
       <code>_backups/&lt;timestamp&gt;/</code> first, so the previous state costs no disk and is one
       <code>mv</code> away. Videos are not touched — trimming is a timestamp shift, which is why this
       stays readable by LeRobot and Rerun.</p>`,
  });
  if (!ok || plan.errors.length) return;

  $('saveBtn').disabled = true;
  $('saveBtn').textContent = 'Saving…';
  try {
    const report = await api('/api/apply', { path: state.ds.path, backup: true });
    toast(`Saved — ${report.episodes_after} episodes, ${report.frames_after.toLocaleString()} frames`);
    state.dirty = 0;
    const position = state.episodes.findIndex(e => e.index === state.ep.index);
    await loadDataset(state.ds.path, position);
  } catch (err) {
    toast(err.message, true);
  } finally {
    $('saveBtn').textContent = 'Save…';
    updateDirty();
  }
}

async function revert() {
  const ok = await confirmDialog({
    title: 'Discard staged edits?',
    okLabel: 'Discard',
    danger: true,
    bodyHtml: `<p>Throws away all <strong>${state.dirty}</strong> staged edits. The dataset on disk is
      untouched either way.</p>`,
  });
  if (!ok) return;
  await api('/api/edits/clear', { path: state.ds.path });
  await loadDataset(state.ds.path);
  toast('Staged edits discarded');
}

/* ---------------------------------------------------------------- shortcuts */

document.addEventListener('keydown', event => {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 's') {
    event.preventDefault(); save(); return;
  }
  if (event.target.tagName === 'INPUT' || event.target.tagName === 'SELECT') return;
  if (!state.ep) return;
  const big = event.shiftKey ? 10 : 1;
  switch (event.key) {
    case ' ': event.preventDefault(); togglePlay(); break;
    case 'ArrowLeft': event.preventDefault(); pause(); seek(state.frame - big); break;
    case 'ArrowRight': event.preventDefault(); pause(); seek(state.frame + big); break;
    case 'ArrowUp': event.preventDefault(); step(-1); break;
    case 'ArrowDown': event.preventDefault(); step(1); break;
    case '[': setCrop(state.frame, state.ep.end); break;
    case ']': setCrop(state.ep.start, state.frame + 1); break;
    case '\\': setCrop(0, state.ep.length); break;
    case '+': case '=': zoomBy(1); break;
    case '-': zoomBy(-1); break;
    case 'Backspace': case 'Delete': event.preventDefault(); deleteEpisode(); break;
  }
});

$('epFilter').addEventListener('input', event => {
  state.filter = event.target.value;
  renderEpisodeList();
});
$('saveBtn').onclick = save;
$('revertBtn').onclick = revert;

let resizeTimer = null;
window.addEventListener('resize', () => {
  if (!state.ep) return;
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => { layoutTracks(); }, 200);
});

boot();
