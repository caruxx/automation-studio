/* Independent motion drafts. Existing image-compositor state and saves are never changed. */
(function () {
  'use strict';
  const API = '/api/layer-motion';
  const MAX_LAYERS = 16, MAX_IMAGE_BYTES = 8 * 1024 * 1024;
  const drafts = new Map();
  const activeStates = new Set(['unknown', 'queued', 'running', 'cancelling']);
  let active = null, root = null, opener = null, bodyOverflow = '', opening = false;
  const $ = id => document.getElementById(id);
  const copy = value => JSON.parse(JSON.stringify(value));
  const finite = (value, min, max, label) => {
    if (typeof value !== 'number' || !Number.isFinite(value) || value < min || value > max)
      throw new Error(`${label}は ${min}〜${max} の数値にしてください。`);
    return value;
  };
  const defaultMotion = duration => ({sway_deg: 0, bob_px: 0, breathe_pct: 0, period: 3, phase: 0, start: 0, end: duration});
  function pngBytes(src) {
    if (typeof src !== 'string' || !/^data:image\/png;base64,[A-Za-z0-9+/]+={0,2}$/.test(src))
      throw new Error('保存素材は PNG 画像である必要があります。画像を追加し直してください。');
    const raw = src.slice(src.indexOf(',') + 1);
    const size = Math.floor(raw.length * 3 / 4) - (raw.endsWith('==') ? 2 : raw.endsWith('=') ? 1 : 0);
    if (size > MAX_IMAGE_BYTES) throw new Error('PNG 素材は 1 枚 8 MiB までです。画像を小さくして追加し直してください。');
    return size;
  }
  function validateProject(project) {
    if (!project || project.version !== 1) throw new Error('この形式のモーション設定は読み込めません。');
    finite(project.width, 2, 1920, '画面の幅'); finite(project.height, 2, 1080, '画面の高さ');
    if (project.width % 2 || project.height % 2) throw new Error('画面サイズは偶数のピクセル数にしてください。');
    finite(project.duration, 0.1, 60, '長さ（秒）'); finite(project.fps, 1, 30, 'fps');
    if (!Number.isInteger(project.fps)) throw new Error('fps は整数にしてください。');
    pngBytes(project.background);
    if (!Array.isArray(project.layers) || project.layers.length > MAX_LAYERS)
      throw new Error(`レイヤーは ${MAX_LAYERS} 枚までです。`);
    project.layers.forEach((layer, index) => {
      const label = `レイヤー ${index + 1}`;
      pngBytes(layer.data_url);
      finite(layer.x, -3840, 5760, `${label} の X`); finite(layer.y, -2160, 3240, `${label} の Y`);
      finite(layer.width, 1, 3840, `${label} の幅`); finite(layer.height, 1, 2160, `${label} の高さ`);
      finite(layer.opacity, 0, 1, `${label} の不透明度`);
      if (!layer.motion) throw new Error(`${label} の動き設定がありません。`);
      const m = layer.motion;
      finite(m.sway_deg, -180, 180, '左右の揺れ'); finite(m.bob_px, -1080, 1080, '上下の揺れ');
      finite(m.breathe_pct, 0, 50, '拡縮'); finite(m.period, 0.1, 60, '周期');
      finite(m.phase, -3600, 3600, '位相'); finite(m.start, 0, project.duration, '開始');
      finite(m.end, 0, project.duration, '終了');
      if (m.end <= m.start) throw new Error(`${label} の終了は開始より後にしてください。`);
    });
    if (new TextEncoder().encode(JSON.stringify(project)).byteLength > 32 * 1024 * 1024)
      throw new Error('設定全体が 32 MiB を超えています。素材を縮小するかレイヤーを減らしてください。');
    return project;
  }
  // Clockwise canvas rotation and this sine equation match the server renderer.
  function motionAt(layer, t) {
    const m = layer.motion;
    const wave = Math.sin(2 * Math.PI * ((t - m.start) / m.period + m.phase / 360));
    return {visible: layer.visible !== false && t >= m.start && t < m.end,
      x: layer.x, y: layer.y + m.bob_px * wave,
      angle: m.sway_deg * wave * Math.PI / 180, scale: 1 + m.breathe_pct / 100 * wave};
  }
  function canvas(width, height) {
    const c = document.createElement('canvas');
    c.width = Math.max(1, Math.ceil(width)); c.height = Math.max(1, Math.ceil(height));
    if (c.width * c.height > 16000000) throw new Error('画像が大きすぎます。1600 万画素以下に縮小してください。');
    return c;
  }
  function png(c) {
    let src;
    try { src = c.toDataURL('image/png'); }
    catch (_) { throw new Error('画像をコピーできません。ローカルの PNG・JPEG・WebP を追加してください。'); }
    pngBytes(src); return src;
  }
  function image(src) {
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => {
        if (!img.naturalWidth || img.naturalWidth * img.naturalHeight > 16000000)
          reject(new Error('画像は 1600 万画素以下にしてください。'));
        else resolve(img);
      };
      img.onerror = () => reject(new Error('画像を読み込めません。PNG・JPEG・WebP ファイルを確認してください。'));
      img.src = src;
    });
  }
  async function assets(project) {
    const loaded = await Promise.all([image(project.background), ...project.layers.map(l => image(l.data_url))]);
    return {background: loaded[0], layers: loaded.slice(1)};
  }
  async function snapshot(name) {
    if (typeof _layerEditors !== 'undefined' && !_layerEditors[name] && typeof initLayerEditor === 'function')
      await initLayerEditor(name, false);
    if (document.fonts) await document.fonts.ready;
    const st = typeof _layerEditors !== 'undefined' ? _layerEditors[name] : null;
    if (st && (st.canvas !== document.getElementById(`layerCanvas_${name}`) || !st.canvas.isConnected))
      throw new Error('元の Image2 が別画面の状態です。未保存内容を保存し、対象の画像画面を開き直してください。');
    const sourceWidth = st?.canvas?.width || 1280, sourceHeight = st?.canvas?.height || 720;
    const ratio = Math.min(1, 1920 / sourceWidth, 1080 / sourceHeight);
    const width = Math.max(2, Math.floor(sourceWidth * ratio / 2) * 2);
    const height = Math.max(2, Math.floor(sourceHeight * ratio / 2) * 2);
    const sx = width / sourceWidth, sy = height / sourceHeight;
    const bg = canvas(width, height), bgctx = bg.getContext('2d');
    bgctx.fillStyle = '#111'; bgctx.fillRect(0, 0, width, height);
    if (st?.bg) bgctx.drawImage(st.bg, 0, 0, width, height);
    if ((st?.layers?.length || 0) > MAX_LAYERS) throw new Error(`元の編集に ${MAX_LAYERS} 枚を超えるレイヤーがあります。モーションは ${MAX_LAYERS} 枚までです。`);
    const layers = [];
    for (const l of st?.layers || []) {
      let bitmap, displayWidth, displayHeight;
      if (l.type === 'image' && l.img) {
        bitmap = canvas(l.img.naturalWidth || l.img.width, l.img.naturalHeight || l.img.height);
        bitmap.getContext('2d').drawImage(l.img, 0, 0, bitmap.width, bitmap.height);
        displayWidth = l.img.width * l.scale * sx; displayHeight = l.img.height * l.scale * sy;
      } else if (l.type === 'text') {
        const fs = l.size * l.scale, text = l.text || 'TEXT';
        const font = `700 ${fs}px ${typeof _layerFontStack === 'function' ? _layerFontStack(l.font) : 'system-ui'}`;
        const measure = canvas(1, 1).getContext('2d');
        measure.font = font; measure.textAlign = 'center'; measure.textBaseline = 'middle';
        const metrics = measure.measureText(text), stroke = Math.max(3, fs * 0.045);
        const halfWidth = Math.max(metrics.width / 2, Math.abs(metrics.actualBoundingBoxLeft || 0), Math.abs(metrics.actualBoundingBoxRight || 0));
        const halfHeight = Math.max(fs, Math.abs(metrics.actualBoundingBoxAscent || 0), Math.abs(metrics.actualBoundingBoxDescent || 0));
        bitmap = canvas((halfWidth + stroke + 2) * 2, (halfHeight + stroke + 2) * 2);
        const ctx = bitmap.getContext('2d');
        ctx.font = font; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
        ctx.lineWidth = stroke; ctx.strokeStyle = 'rgba(0,0,0,.55)'; ctx.fillStyle = l.color || '#fff';
        ctx.strokeText(text, bitmap.width / 2, bitmap.height / 2);
        ctx.fillText(text, bitmap.width / 2, bitmap.height / 2);
        displayWidth = bitmap.width * sx; displayHeight = bitmap.height * sy;
      } else throw new Error(`「${l.name || 'レイヤー'}」をコピーできません。元の画像が読み込み済みか確認してください。`);
      layers.push({name: String(l.name || (l.type === 'text' ? l.text : '') || `レイヤー ${layers.length + 1}`).slice(0, 120),
        data_url: png(bitmap), x: l.x * sx, y: l.y * sy, width: displayWidth, height: displayHeight,
        opacity: l.opacity ?? 1, visible: l.visible !== false, motion: defaultMotion(6)});
    }
    return validateProject({version: 1, width, height, duration: 6, fps: 24, background: png(bg), layers});
  }
  function numeric(label, field, min, max, step = 'any') {
    return `<label for="lm-${field}">${label}<input id="lm-${field}" data-lm-field="${field}" type="number" min="${min}" max="${max}" step="${step}"></label>`;
  }
  function ensureRoot() {
    if (root) return;
    root = document.createElement('div'); root.id = 'layer-motion-modal'; root.className = 'lm-overlay'; root.hidden = true;
    root.innerHTML = `<section class="lm-dialog" role="dialog" aria-modal="true" aria-labelledby="lm-heading" tabindex="-1">
      <header class="lm-header"><div><h2 id="lm-heading">レイヤーモーション</h2><p id="lm-subtitle"></p></div><button id="lm-close" data-lm-action="close" class="btn btn-ghost" aria-label="レイヤーモーションを閉じる">閉じる ×</button></header>
      <div class="lm-message" id="lm-message" role="status" aria-live="polite"></div>
      <div class="lm-body"><main class="lm-main">
        <div class="lm-toolbar"><label class="btn btn-secondary lm-file-label">背景を変更<input id="lm-background-file" type="file" accept="image/png,image/jpeg,image/webp" data-lm-import="background"></label><label class="btn btn-secondary lm-file-label">画像レイヤーを追加<input id="lm-layer-file" type="file" accept="image/png,image/jpeg,image/webp" data-lm-import="layer"></label><span class="lm-hint">PNG で透明部分を保持 · 各 8 MiB まで</span></div>
        <div class="lm-preview"><canvas id="lm-canvas" aria-label="モーションプレビュー"></canvas></div>
        <div class="lm-transport"><button id="lm-play" data-lm-action="play" class="btn btn-secondary">再生</button><input id="lm-seek" type="range" min="0" max="6" step="0.001" value="0" aria-label="プレビュー位置（秒）"><output id="lm-clock">0.00 / 6.00 秒</output></div>
        <div class="lm-composition"><label for="lm-duration">長さ（秒）<input id="lm-duration" type="number" min="0.1" max="60" step="any" data-lm-project="duration"></label><label for="lm-fps">fps<input id="lm-fps" type="number" min="1" max="30" step="1" data-lm-project="fps"></label><span id="lm-dimensions" class="lm-hint"></span></div>
        <section class="lm-projects" aria-label="モーション設定の保存"><div class="lm-section-title">モーション設定 <span id="lm-dirty"></span></div><div class="lm-save-row"><input id="lm-title" maxlength="120" placeholder="設定名" aria-label="保存する設定名"><button id="lm-save" data-lm-action="save" class="btn btn-secondary">新しく保存</button></div><div class="lm-save-row"><select id="lm-project-list" aria-label="保存済みモーション設定"><option value="">保存済み設定を選択</option></select><button id="lm-load" data-lm-action="load" class="btn btn-secondary">読み込む</button><button id="lm-refresh" data-lm-action="refresh" class="btn btn-ghost" aria-label="保存済み設定を更新">更新</button></div><p class="lm-hint">背景とレイヤーをまとめて別ファイルへ保存します。下書きは、このページを開いている間保持されます。</p></section>
      </main><aside class="lm-side"><section><div class="lm-section-title">レイヤー <span id="lm-layer-count"></span></div><p class="lm-hint">一覧の上が手前です。</p><div id="lm-layers" class="lm-layers" role="listbox" aria-label="モーションレイヤー"></div><div class="lm-layer-actions"><button id="lm-forward" data-lm-action="forward" class="btn btn-ghost">手前へ</button><button id="lm-backward" data-lm-action="backward" class="btn btn-ghost">奥へ</button><button id="lm-delete" data-lm-action="delete" class="btn btn-ghost">削除</button></div></section>
        <fieldset id="lm-inspector"><legend>選択レイヤー</legend><label class="lm-name-label" for="lm-name">名前<input id="lm-name" data-lm-field="name" maxlength="120"></label><label class="lm-visible-label"><input id="lm-visible" data-lm-field="visible" type="checkbox">表示する</label><div class="lm-field-grid">${numeric('X（中心）', 'x', -3840, 5760)}${numeric('Y（中心）', 'y', -2160, 3240)}${numeric('幅（px）', 'width', 1, 3840)}${numeric('高さ（px）', 'height', 1, 2160)}${numeric('不透明度（%）', 'opacity', 0, 100)}</div><div class="lm-section-title">動き</div><p class="lm-hint">揺れ・拡縮を 0 にすると固定。位相で動くタイミングをずらせます。</p><div class="lm-field-grid">${numeric('左右の揺れ（度）', 'sway_deg', -180, 180)}${numeric('上下の揺れ（px）', 'bob_px', -1080, 1080)}${numeric('拡縮（%）', 'breathe_pct', 0, 50)}${numeric('周期（秒）', 'period', 0.1, 60)}${numeric('位相（度）', 'phase', -3600, 3600)}${numeric('開始（秒）', 'start', 0, 60)}${numeric('終了（秒）', 'end', 0.1, 60)}</div></fieldset>
      </aside></div>
      <footer class="lm-footer"><div class="lm-export-actions"><button id="lm-export" data-lm-action="export" class="btn btn-primary">MP4 を書き出す</button><button id="lm-cancel" data-lm-action="cancel" class="btn btn-secondary" hidden>書き出しを中止</button><button id="lm-status-retry" data-lm-action="status" class="btn btn-secondary" hidden>状態を再確認</button><button id="lm-submit-retry" data-lm-action="submit-retry" class="btn btn-secondary" hidden>同じ受付を再送</button><button id="lm-recover-editing" data-lm-action="recover-editing" class="btn btn-secondary" hidden>保存先を確認して編集に戻る</button><span id="lm-job-status" role="status" aria-live="polite"></span></div><progress id="lm-progress" max="100" value="0" aria-label="書き出し進捗" hidden></progress><details class="lm-recovery"><summary>ジョブ ID で状態を確認</summary><div class="lm-save-row"><input id="lm-job-id" placeholder="ジョブ ID" aria-label="書き出しジョブ ID"><button id="lm-lookup" data-lm-action="lookup" class="btn btn-secondary">確認</button></div></details><div id="lm-result" class="lm-result" hidden><video id="lm-output-video" controls preload="metadata" aria-label="書き出したモーション動画"></video><a id="lm-output-link" class="btn btn-secondary" target="_blank" rel="noopener">MP4 を開く</a><p id="lm-output-path" class="lm-hint"></p></div></footer>
    </section>`;
    document.body.appendChild(root);
    root.addEventListener('click', event => {
      const button = event.target.closest('[data-lm-action]');
      if (button && !button.disabled) run(() => action(button.dataset.lmAction));
      const layer = event.target.closest('[data-lm-layer]');
      if (layer && active) run(() => { flushVisible(); active.selected = Number(layer.dataset.lmLayer); renderLayers(); renderInspector(); });
    });
    root.addEventListener('change', event => {
      if (event.target.dataset.lmImport) run(() => importFile(event.target));
      else if (event.target.dataset.lmField || event.target.dataset.lmProject || event.target.id === 'lm-title') run(flushVisible);
    });
    root.addEventListener('input', event => {
      if (active && !editingLocked() && (event.target.dataset.lmField || event.target.dataset.lmProject || event.target.id === 'lm-title')) changed();
    });
    $('lm-seek').addEventListener('input', event => {
      if (!active) return;
      run(() => { flushVisible(); pause(active); active.time = Number(event.target.value); draw(); });
    });
    document.addEventListener('keydown', event => {
      if (!active || root.hidden) return;
      if (event.key === 'Escape') { event.preventDefault(); run(() => close()); }
      if (event.key === 'Tab') {
        const nodes = Array.from(root.querySelectorAll('button,input,select,summary,a[href],video[controls]')).filter(el => !el.disabled && el.getClientRects().length);
        const first = nodes[0], last = nodes[nodes.length - 1];
        if (event.shiftKey && (document.activeElement === first || !root.contains(document.activeElement))) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      }
    });
    document.addEventListener('visibilitychange', () => {
      if (!active) return;
      if (document.hidden) { pause(active); clearTimeout(active.pollTimer); }
      else if (!root.hidden && active.job && activeStates.has(active.job.status) && !active.job.pollError) poll(active);
    });
    window.addEventListener('beforeunload', event => {
      if (Array.from(drafts.values()).some(d => d.dirty || activeStates.has(d.job?.status))) {
        event.preventDefault(); event.returnValue = '';
      }
    });
  }
  async function run(fn) {
    try { await fn(); } catch (error) { message(error.message || '処理に失敗しました。入力を確認してください。', true); }
  }
  function message(text, error = false) {
    if (!root) return;
    $('lm-message').textContent = text; $('lm-message').classList.toggle('is-error', error);
  }
  function jobKey(draft) { return `automation-layer-motion-job:${draft.scope}:${draft.name}`; }
  function rememberJob(draft) {
    try { if (draft.job?.id || draft.job?.request_id) sessionStorage.setItem(jobKey(draft), JSON.stringify(draft.job)); } catch (_) {}
  }
  function restoredJob(name, scope) {
    try {
      const job = JSON.parse(sessionStorage.getItem(`automation-layer-motion-job:${scope}:${name}`) || 'null');
      return job && ((typeof job.id === 'string' && job.id.length > 0 && job.id.length <= 200) || /^[a-f0-9-]{36}$/i.test(job.request_id || '')) ? job : null;
    } catch (_) { return null; }
  }
  async function openLayerMotion(name) {
    if (opening || !name) return;
    ensureRoot();
    if (active && !root.hidden && !close()) return;
    opening = true;
    try {
      const context = await request(`/context?video_name=${encodeURIComponent(name)}`);
      if (!context.scope || typeof context.scope !== 'string') throw new Error('対象動画の保存先を確認できません。画面を開き直してください。');
      const scope = context.scope, key = `${scope}:${name}`;
      let d = drafts.get(key), resumed = !!d;
      if (!d) {
        const project = await snapshot(name);
        d = {name, scope, project, assets: await assets(project), title: 'モーション', dirty: true, selected: project.layers.length ? project.layers.length - 1 : -1,
          time: 0, playing: false, busy: false, pollTimer: null, job: restoredJob(name, scope), submission: null, projects: []};
        drafts.set(key, d);
      }
      active = d; opener = document.activeElement; bodyOverflow = document.body.style.overflow;
      document.body.style.overflow = 'hidden'; root.hidden = false;
      render(); message(resumed ? 'この動画のモーション下書きを再開しました。' : '現在のレイヤー編集を独立コピーしました。動きを設定し、「新しく保存」で下書きを残せます。');
      $('lm-close').focus();
      try { await refreshProjects(d); } catch (error) { message(error.message + ' 保存済み設定の一覧は「更新」で再取得できます。', true); }
      if (d.job?.id || d.job?.request_id) poll(d);
    } catch (error) {
      if (active && !root.hidden) message(error.message, true);
      else if (typeof toast === 'function') toast(error.message, 'e');
      else window.alert(error.message);
    } finally { opening = false; }
  }
  function close() {
    if (!active) return true;
    if (active.busy) { message('処理が完了してから閉じてください。'); return false; }
    try { flushVisible(); } catch (error) { message(error.message, true); return false; }
    if (active.dirty && !window.confirm('未保存の変更があります。閉じてもこのページ内に下書きを保持します。閉じますか？')) return false;
    pause(active); clearTimeout(active.pollTimer); $('lm-output-video').pause();
    root.hidden = true; document.body.style.overflow = bodyOverflow;
    opener?.focus?.(); active = null; return true;
  }
  function changed() { active.dirty = true; $('lm-dirty').textContent = '未保存'; }
  // Commit the values the user actually sees, including automation that dispatches
  // input without blur/change. Validate together so paired start/end edits are atomic.
  function flushVisible() {
    if (!active || editingLocked()) return;
    const p = copy(active.project), oldDuration = p.duration, layer = p.layers[active.selected];
    const numberFrom = input => {
      if (!input.value.trim() || !Number.isFinite(Number(input.value)))
        throw new Error('入力欄に有効な数値を指定してください。');
      return Number(input.value);
    };
    if (layer) root.querySelectorAll('[data-lm-field]').forEach(input => {
      const field = input.dataset.lmField, target = Object.hasOwn(layer.motion, field) ? layer.motion : layer;
      target[field] = field === 'name' ? input.value.trim() || 'レイヤー' : field === 'visible' ? input.checked : numberFrom(input) / (field === 'opacity' ? 100 : 1);
    });
    p.duration = numberFrom($('lm-duration')); p.fps = numberFrom($('lm-fps'));
    if (p.duration !== oldDuration) p.layers.forEach(l => {
      if (l.motion.end === oldDuration || l.motion.end > p.duration) l.motion.end = p.duration;
    });
    validateProject(p);
    const title = $('lm-title').value;
    if (JSON.stringify(p) !== JSON.stringify(active.project) || title !== active.title) {
      active.project = p; active.title = title; active.time = Math.min(active.time, p.duration); changed();
      $('lm-seek').max = p.duration; $('lm-seek').step = 1 / p.fps;
      renderLayers(); renderInspector(); draw();
    }
  }
  function editingLocked(d = active) { return !!(d?.busy || activeStates.has(d?.job?.status)); }
  function controls() {
    if (!active) return;
    const locked = editingLocked(), layer = active.project.layers[active.selected];
    root.querySelectorAll('[data-lm-field],[data-lm-project],[data-lm-import],#lm-title,#lm-save,#lm-load,#lm-delete,#lm-forward,#lm-backward').forEach(el => { el.disabled = locked; });
    $('lm-inspector').disabled = locked || !layer;
    $('lm-forward').disabled = locked || !layer || active.selected === active.project.layers.length - 1;
    $('lm-backward').disabled = locked || !layer || active.selected === 0;
    $('lm-delete').disabled = locked || !layer;
    $('lm-layer-file').disabled = locked || active.project.layers.length >= MAX_LAYERS;
    $('lm-export').disabled = locked;
    $('lm-lookup').disabled = active.busy || !!((active.job?.id || active.job?.request_id) && activeStates.has(active.job.status));
    $('lm-cancel').hidden = !active.job?.id || !activeStates.has(active.job?.status);
    $('lm-cancel').disabled = active.busy || active.job?.status === 'cancelling';
    $('lm-status-retry').hidden = (!active.job?.id && !active.job?.request_id) || (!active.job.pollError && !activeStates.has(active.job.status));
    $('lm-status-retry').disabled = active.busy || active.polling;
    $('lm-submit-retry').hidden = active.job?.status !== 'unknown' || !active.job?.notFound || !active.submission;
    $('lm-submit-retry').disabled = active.busy || active.polling;
    $('lm-recover-editing').hidden = active.job?.status !== 'unknown' || !active.job?.notFound || !!active.submission;
    $('lm-recover-editing').disabled = active.busy || active.polling;
    root.querySelectorAll('.lm-file-label').forEach(label => label.classList.toggle('is-disabled', label.querySelector('input').disabled));
  }
  function render() {
    const d = active, p = d.project;
    $('lm-subtitle').textContent = d.name;
    $('lm-title').value = d.title; $('lm-dirty').textContent = d.dirty ? '未保存' : '保存済み';
    $('lm-duration').value = p.duration; $('lm-fps').value = p.fps;
    $('lm-dimensions').textContent = `${p.width} × ${p.height} px · 最大 60 秒`;
    $('lm-canvas').width = p.width; $('lm-canvas').height = p.height;
    $('lm-seek').max = p.duration; $('lm-seek').step = 1 / p.fps;
    renderLayers(); renderInspector(); renderProjects(); renderJob(); draw(); controls();
  }
  function renderLayers() {
    const list = $('lm-layers'); list.replaceChildren();
    $('lm-layer-count').textContent = `${active.project.layers.length} / ${MAX_LAYERS}`;
    active.project.layers.forEach((l, i) => {
      const button = document.createElement('button'); button.type = 'button';
      button.dataset.lmLayer = i; button.id = `lm-layer-${i}`; button.className = 'lm-layer';
      button.setAttribute('role', 'option'); button.setAttribute('aria-selected', String(active.selected === i));
      const thumb = document.createElement('img'); thumb.src = l.data_url; thumb.alt = '';
      const text = document.createElement('span'); text.textContent = `${l.visible === false ? '○ ' : ''}${l.name || `レイヤー ${i + 1}`}`;
      button.append(thumb, text); list.prepend(button);
    });
    if (!active.project.layers.length) list.textContent = '画像レイヤーを追加すると動きを設定できます。';
    controls();
  }
  function renderInspector() {
    const layer = active.project.layers[active.selected];
    root.querySelectorAll('[data-lm-field]').forEach(input => {
      const field = input.dataset.lmField;
      if (input.type === 'checkbox') input.checked = layer?.visible !== false;
      else input.value = layer ? (field === 'opacity' ? layer.opacity * 100 : layer[field] ?? layer.motion[field]) : '';
    });
    $('lm-start').max = active.project.duration; $('lm-end').max = active.project.duration;
    controls();
  }
  function editLayer(input) {
    if (!active || editingLocked()) return;
    const layer = active.project.layers[active.selected]; if (!layer) return;
    const field = input.dataset.lmField, target = Object.hasOwn(layer.motion, field) ? layer.motion : layer;
    const previous = target[field];
    if (field === 'name') target[field] = input.value.trim() || 'レイヤー';
    else if (field === 'visible') target[field] = input.checked;
    else {
      if (input.value.trim() === '' || !input.checkValidity()) { input.reportValidity(); renderInspector(); return; }
      target[field] = Number(input.value) / (field === 'opacity' ? 100 : 1);
    }
    try { validateProject(active.project); }
    catch (error) { target[field] = previous; message(error.message, true); renderInspector(); return; }
    changed(); renderLayers(); draw();
  }
  function editProject(input) {
    if (!active || editingLocked()) return;
    const old = copy(active.project), field = input.dataset.lmProject, value = Number(input.value);
    if (!input.value.trim() || !input.checkValidity()) { input.reportValidity(); input.value = old[field]; return; }
    active.project[field] = value;
    if (field === 'duration') active.project.layers.forEach(l => {
      if (l.motion.end === old.duration || l.motion.end > value) l.motion.end = value;
    });
    try { validateProject(active.project); }
    catch (error) { active.project = old; input.value = old[field]; message(error.message, true); return; }
    pause(active); if (field === 'duration') active.time = Math.min(active.time, value); changed(); render();
  }
  function draw() {
    if (!active || root.hidden) return;
    const {project: p, assets: a} = active, c = $('lm-canvas'), ctx = c.getContext('2d');
    ctx.clearRect(0, 0, c.width, c.height); ctx.fillStyle = '#000'; ctx.fillRect(0, 0, c.width, c.height);
    ctx.drawImage(a.background, 0, 0, p.width, p.height);
    p.layers.forEach((l, i) => {
      const pose = motionAt(l, active.time); if (!pose.visible) return;
      ctx.save(); ctx.globalAlpha = l.opacity; ctx.translate(pose.x, pose.y); ctx.rotate(pose.angle); ctx.scale(pose.scale, pose.scale);
      ctx.drawImage(a.layers[i], -l.width / 2, -l.height / 2, l.width, l.height); ctx.restore();
    });
    $('lm-seek').value = active.time;
    $('lm-clock').textContent = `${active.time.toFixed(2)} / ${p.duration.toFixed(2)} 秒`;
  }
  function pause(d) {
    d.playing = false; cancelAnimationFrame(d.raf); d.raf = null;
    if (active === d) $('lm-play').textContent = '再生';
  }
  function play() {
    const d = active;
    if (d.playing) { pause(d); return; }
    if (d.time >= d.project.duration) d.time = 0;
    d.playing = true; $('lm-play').textContent = '一時停止';
    let previous = performance.now();
    const frame = now => {
      if (active !== d || !d.playing || root.hidden || document.hidden) { pause(d); return; }
      d.time += (now - previous) / 1000; previous = now;
      if (d.time >= d.project.duration) { d.time = 0; }
      draw(); d.raf = requestAnimationFrame(frame);
    };
    d.raf = requestAnimationFrame(frame);
  }
  async function importFile(input) {
    const file = input.files?.[0], kind = input.dataset.lmImport, d = active; input.value = '';
    if (!file || !d || editingLocked()) return;
    if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type)) throw new Error('PNG・JPEG・WebP を選んでください。SVG や動画は追加できません。');
    if (file.size > MAX_IMAGE_BYTES) throw new Error('画像は 8 MiB 以下にしてください。');
    if (kind === 'layer' && d.project.layers.length >= MAX_LAYERS) throw new Error(`レイヤーは ${MAX_LAYERS} 枚までです。`);
    d.busy = true; controls(); const url = URL.createObjectURL(file);
    try {
      const img = await image(url), p = copy(d.project);
      if (kind === 'background') {
        const bg = canvas(p.width, p.height); bg.getContext('2d').drawImage(img, 0, 0, p.width, p.height);
        p.background = png(bg);
      } else {
        const bitmap = canvas(img.naturalWidth, img.naturalHeight); bitmap.getContext('2d').drawImage(img, 0, 0);
        const scale = Math.min(1, p.width * 0.7 / img.naturalWidth, p.height * 0.7 / img.naturalHeight);
        p.layers.push({name: file.name.slice(0, 120), data_url: png(bitmap), x: p.width / 2, y: p.height / 2,
          width: Math.max(1, img.naturalWidth * scale), height: Math.max(1, img.naturalHeight * scale), opacity: 1, visible: true, motion: defaultMotion(p.duration)});
      }
      validateProject(p); const loaded = await assets(p); d.project = p; d.assets = loaded;
      if (kind === 'layer') d.selected = p.layers.length - 1;
      changed(); render(); message(kind === 'background' ? '背景を画面サイズに合わせて配置しました。' : '画像を追加しました。選択レイヤーの動きを設定できます。');
    } finally { URL.revokeObjectURL(url); d.busy = false; controls(); }
  }
  async function request(path, body) {
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 30000);
    try {
      const response = await fetch(API + path, {method: body ? 'POST' : 'GET', credentials: 'same-origin', signal: controller.signal,
        ...(body ? {headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)} : {})});
      const result = await response.json().catch(() => ({}));
      if (!response.ok) {
        const error = new Error(typeof result.detail === 'string' ? result.detail : typeof result.error === 'string' ? result.error : `処理を受け付けられませんでした（HTTP ${response.status}）。設定を確認してください。`);
        error.httpStatus = response.status; throw error;
      }
      return result;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('応答がありません。接続を確認してから状態を再確認してください。');
      if (error instanceof TypeError) throw new Error('接続が切れました。接続を確認してください。');
      throw error;
    } finally { clearTimeout(timeout); }
  }
  function renderProjects() {
    const list = $('lm-project-list'), selected = list.value; list.replaceChildren(new Option('保存済み設定を選択', ''));
    active.projects.forEach(p => list.add(new Option(p.title || p.filename, p.filename)));
    if (active.projects.some(p => p.filename === selected)) list.value = selected;
  }
  async function refreshProjects(d = active) {
    const data = await request(`/projects?video_name=${encodeURIComponent(d.name)}&scope=${encodeURIComponent(d.scope)}`);
    d.projects = Array.isArray(data.projects) ? data.projects : [];
    if (active === d) renderProjects();
  }
  async function save() {
    const d = active; if (editingLocked(d)) return;
    validateProject(d.project); d.busy = true; controls();
    try {
      const data = await request('/project', {video_name: d.name, scope: d.scope, project: d.project, title: d.title.trim() || 'モーション'});
      d.dirty = false; $('lm-dirty').textContent = '保存済み';
      message(`保存しました: ${data.path || data.filename}`);
      try { await refreshProjects(d); $('lm-project-list').value = data.filename; }
      catch (_) { message(`保存しました: ${data.path || data.filename}。一覧は「更新」で再取得できます。`); }
    } finally { d.busy = false; controls(); }
  }
  async function load() {
    const d = active, filename = $('lm-project-list').value;
    if (editingLocked(d)) return;
    if (!filename) throw new Error('読み込む設定を選んでください。');
    if (d.dirty && !window.confirm('未保存の下書きを、選択した保存済み設定で置き換えますか？')) return;
    d.busy = true; pause(d); controls();
    try {
      const data = await request(`/project?video_name=${encodeURIComponent(d.name)}&scope=${encodeURIComponent(d.scope)}&filename=${encodeURIComponent(filename)}`);
      const project = validateProject(copy(data.project)), loaded = await assets(project);
      d.project = project; d.assets = loaded; d.title = data.title || 'モーション'; d.dirty = false;
      d.selected = project.layers.length - 1; d.time = 0; render(); message('保存済みのモーション設定を読み込みました。');
    } finally { d.busy = false; controls(); }
  }
  function renderJob() {
    const job = active.job;
    $('lm-job-id').value = job?.id || '';
    const labels = {unknown: '受付結果を確認中', queued: '待機中', running: '書き出し中', cancelling: '中止処理中', completed: '書き出し完了', cancelled: '中止しました', failed: '書き出し失敗'};
    $('lm-job-status').textContent = job ? `${labels[job.status] || '状態確認中'}${job.pollError ? ' · 接続を確認して「状態を再確認」' : ''}${job.id ? ` · ${job.id}` : job.request_id ? ` · 受付 ${job.request_id}` : ''}${job.error ? ` · ${job.error}` : ''}` : '';
    $('lm-progress').hidden = !job; $('lm-progress').value = Math.max(0, Math.min(100, Number(job?.progress) || 0));
    $('lm-result').hidden = true;
    if (job?.status === 'completed' && job.url) {
      let url;
      try { url = new URL(job.url, window.location.href); } catch (_) { return; }
      if (url.origin !== window.location.origin || !['http:', 'https:'].includes(url.protocol)) return;
      $('lm-result').hidden = false;
      if ($('lm-output-video').src !== url.href) $('lm-output-video').src = url.href;
      $('lm-output-link').href = url.href; $('lm-output-path').textContent = job.output_path || '';
    }
    controls();
  }
  async function poll(d) {
    clearTimeout(d.pollTimer);
    if ((!d.job?.id && !d.job?.request_id) || d.polling) return;
    d.polling = true; if (active === d) controls();
    const currentJob = d.job, query = d.job.id ? `job_id=${encodeURIComponent(d.job.id)}` : `request_id=${encodeURIComponent(d.job.request_id)}`;
    try {
      const status = await request(`/status?${query}`);
      if (d.job !== currentJob) return;
      d.job = {...status, id: status.job_id || currentJob.id, request_id: status.request_id || currentJob.request_id, pollError: false}; rememberJob(d);
      if (active === d) {
        renderJob();
        if (status.status === 'failed') message(status.error || '書き出しに失敗しました。設定を確認して再度実行してください。', true);
        if (status.status === 'cancelled') message('書き出しを中止しました。編集内容は保持されています。');
        if (status.status === 'completed') message('MP4 を書き出しました。下の動画で確認できます。');
      }
      if (activeStates.has(status.status) && active === d && !root.hidden && !document.hidden)
        d.pollTimer = setTimeout(() => poll(d), 1000);
    } catch (error) {
      d.job.pollError = true;
      if (error.httpStatus === 404) {
        d.job.notFound = true;
        if (d.job.id) { d.job.status = 'failed'; d.job.error = 'ジョブが見つかりません。サーバーの状態を確認してください。'; }
      }
      rememberJob(d);
      if (active === d) { renderJob(); message(`${error.message} 書き出しの再送はしていません。受付 ID を保持しています。「状態を再確認」を押してください。${d.job.notFound && d.submission ? ' 未受付の場合は「同じ受付を再送」で同一 ID のまま再確認・送信できます。' : ''}`, true); }
    } finally { d.polling = false; if (active === d) controls(); }
  }
  async function startExport() {
    const d = active; if (editingLocked(d)) return;
    validateProject(d.project);
    const requestId = crypto.randomUUID();
    d.submission = {video_name: d.name, scope: d.scope, project: copy(d.project), request_id: requestId};
    d.job = {id: '', request_id: requestId, status: 'unknown', progress: 0}; rememberJob(d);
    return submitExport(d);
  }
  async function submitExport(d) {
    d.busy = true; pause(d); controls(); message('書き出しを受け付けています…');
    try {
      const data = await request('/export', d.submission);
      if (!data.job_id) throw new Error('受付応答にジョブ ID がありません。実行状態を確認してください。');
      d.job = {id: data.job_id, request_id: data.request_id || d.submission.request_id, status: data.status || 'queued', progress: 0};
      rememberJob(d); renderJob();
      message('書き出し中です。この画面を閉じても処理は続きます。ジョブ ID は同じタブに保持されます。');
      poll(d);
    } catch (error) {
      if (error.httpStatus && error.httpStatus < 500) d.job = {...d.job, status: 'failed', error: error.message};
      else { d.job.status = 'unknown'; d.job.pollError = true; }
      rememberJob(d); renderJob();
      throw new Error(`${error.message}${d.job.status === 'unknown' ? ' 受付結果が不明のため、自動で再送しません。「状態を再確認」で受付 ID から確認してください。' : ''}`);
    } finally { d.busy = false; controls(); }
  }
  async function cancel() {
    const d = active; if (!d.job?.id || !activeStates.has(d.job.status) || d.busy) return;
    d.busy = true; controls();
    try {
      await request('/cancel', {job_id: d.job.id}); d.job.status = 'cancelling'; d.job.pollError = false;
      rememberJob(d); renderJob(); message('中止を要求しました。完了状態を確認しています。'); await poll(d);
    } finally { d.busy = false; controls(); }
  }
  async function action(kind) {
    if (!active) return;
    if (!['cancel', 'status', 'lookup', 'submit-retry', 'recover-editing', 'refresh'].includes(kind)) flushVisible();
    if (kind === 'close') return close();
    if (kind === 'play') return play();
    if (kind === 'save') return save();
    if (kind === 'load') return load();
    if (kind === 'refresh') return refreshProjects();
    if (kind === 'export') return startExport();
    if (kind === 'cancel') return cancel();
    if (kind === 'status') return poll(active);
    if (kind === 'submit-retry') {
      if (active.job?.status === 'unknown' && active.job.notFound && active.submission && !active.busy)
        return submitExport(active);
      return;
    }
    if (kind === 'recover-editing') {
      const d = active;
      if (d.job?.status !== 'unknown' || !d.job.notFound || d.submission || d.busy) return;
      if (!window.confirm('サーバーでこの受付を確認できません。新しく書き出す前に、対象動画の layer_motion/exports フォルダーに完成済み MP4 がないか確認してください。確認したうえで、保持している受付情報を解除して編集に戻りますか？ 既存ファイルは変更しません。')) return;
      clearTimeout(d.pollTimer); d.job = null;
      try { sessionStorage.removeItem(jobKey(d)); } catch (_) {}
      renderJob(); message('受付情報を解除しました。保存済み設定を読み込むか、編集を続けられます。'); return;
    }
    if (kind === 'lookup') {
      const id = $('lm-job-id').value.trim();
      if (!/^[A-Za-z0-9_-]{1,200}$/.test(id)) throw new Error('有効なジョブ ID を入力してください。');
      active.job = {id, status: 'queued', progress: 0}; rememberJob(active); renderJob(); return poll(active);
    }
    if (editingLocked()) return;
    const d = active, i = d.selected; if (i < 0) return;
    if (kind === 'delete') {
      d.project.layers.splice(i, 1); d.assets.layers.splice(i, 1); d.selected = Math.min(i, d.project.layers.length - 1);
    } else if (kind === 'forward' || kind === 'backward') {
      const j = i + (kind === 'forward' ? 1 : -1); if (j < 0 || j >= d.project.layers.length) return;
      [d.project.layers[i], d.project.layers[j]] = [d.project.layers[j], d.project.layers[i]];
      [d.assets.layers[i], d.assets.layers[j]] = [d.assets.layers[j], d.assets.layers[i]]; d.selected = j;
    } else return;
    changed(); renderLayers(); renderInspector(); draw();
  }
  window.openLayerMotion = openLayerMotion;
})();
