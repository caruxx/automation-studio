/* Premiere Link v1 - JSX Launcher + File Polling IPC
 *
 * 2つの役割:
 *   (A) 任意の .jsx ファイルをフォルダから選んでワンクリック実行（JSX Launcher 方式）
 *   (B) Python からファイルポーリング経由で JSX を実行（Python → trigger.json → evalScript → result.json）
 *
 * IPC ファイル名は互換性のため /tmp/pymiere_*.json を維持（既存 Python 側と契約）。
 */
var csInterface = new CSInterface();

// ─── IPC paths (legacy names, kept for Python-side compatibility) ───
var TRIGGER  = '/tmp/pymiere_trigger.json';
var RESULT   = '/tmp/pymiere_result.json';
var PING     = '/tmp/pymiere_ping.txt';
var ACTIVITY = '/tmp/pymiere_activity.json';

// ─── localStorage keys ───
var LS_FOLDER = 'premiereLinkScriptFolder';

// ─── DOM ───
var statusEl = document.getElementById('status');
var dotEl    = document.getElementById('dot');
var logEl    = document.getElementById('log');
var scriptsEl = document.getElementById('scripts');
var folderEl = document.getElementById('folder');

var tickCount = 0;
var activityLog = [];
var currentFolder = '';

function setStatus(text, state) {
    if (statusEl) statusEl.textContent = text;
    if (dotEl) {
        dotEl.classList.remove('ok', 'err');
        if (state) dotEl.classList.add(state);
    }
}

function escapeHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function renderLog() {
    if (!logEl) return;
    if (!activityLog.length) {
        logEl.innerHTML = '<div class="empty">待機中</div>';
        return;
    }
    var html = '';
    for (var i = 0; i < activityLog.length; i++) {
        var e = activityLog[i];
        var cls = e.type === 'error' ? 'err' : e.type === 'done' ? 'done' : 'info';
        html += '<div class="ln ' + cls + '"><span class="t">' + escapeHtml(e.t) + '</span>' + escapeHtml(e.msg) + '</div>';
    }
    logEl.innerHTML = html;
}

function addLog(msg, type) {
    var entry = {t: new Date().toLocaleTimeString('ja-JP', {hour12:false}), msg: msg, type: type || 'info'};
    activityLog.unshift(entry);
    if (activityLog.length > 30) activityLog.length = 30;
    try { window.cep.fs.writeFile(ACTIVITY, JSON.stringify(activityLog)); } catch (e) {}
    renderLog();
}

// ─── Script folder / button list ───
function renderScripts(files) {
    if (!scriptsEl) return;
    if (!files || !files.length) {
        scriptsEl.innerHTML = '<div class="empty">.jsx ファイルなし</div>';
        return;
    }
    var html = '';
    for (var i = 0; i < files.length; i++) {
        var name = files[i];
        html += '<button class="scbtn" onclick="runScript(\'' + escapeHtml(name).replace(/\\/g,'\\\\').replace(/'/g,"\\'") + '\')" title="' + escapeHtml(name) + '">' + escapeHtml(name) + '</button>';
    }
    scriptsEl.innerHTML = html;
}

function loadScripts(folder) {
    if (!folder) {
        folderEl.textContent = '';
        scriptsEl.innerHTML = '<div class="empty">フォルダ未選択</div>';
        return;
    }
    currentFolder = folder;
    folderEl.textContent = folder;
    // ExtendScript 側で一覧を取得
    var safe = folder.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
    csInterface.evalScript('getJsxFiles("' + safe + '")', function(cb) {
        if (!cb || cb === 'EvalScript error.') {
            renderScripts([]);
            return;
        }
        var files = cb.split(',').filter(function(s){ return s && s.indexOf('.jsx') >= 0; });
        renderScripts(files);
    });
}

function pickScriptFolder() {
    csInterface.evalScript('pickFolder("Premiere Link: Select script folder")', function(cb) {
        if (!cb || cb === 'EvalScript error.' || cb === 'null') return;
        var folder = cb.replace(/^"|"$/g, '');
        if (!folder) return;
        try { localStorage.setItem(LS_FOLDER, folder); } catch (e) {}
        loadScripts(folder);
        addLog('フォルダ設定: ' + folder, 'done');
    });
}

function refreshScripts() {
    if (currentFolder) loadScripts(currentFolder);
}

function runScript(filename) {
    if (!currentFolder) return;
    var sep = currentFolder.charAt(currentFolder.length - 1) === '/' ? '' : '/';
    var full = currentFolder + sep + filename;
    addLog('▶ ' + filename, 'info');
    setStatus('実行中: ' + filename, 'ok');
    var safe = full.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
    csInterface.evalScript('callJsxFile("' + safe + '")', function(result) {
        var isErr = !result || result === 'EvalScript error.' || String(result).indexOf('error:') === 0;
        setStatus('監視中', 'ok');
        if (isErr) {
            addLog('✕ ' + filename + ' → ' + (result || 'error'), 'error');
        } else {
            addLog('✓ ' + filename, 'done');
        }
    });
}

// ─── IPC polling (Python bridge) ───
function writePing() {
    try { window.cep.fs.writeFile(PING, String(Date.now())); } catch (e) {}
}

function poll() {
    tickCount++;
    if (tickCount % 25 === 0) writePing();

    var r = window.cep.fs.readFile(TRIGGER);
    if (r.err !== 0 || !r.data || !r.data.trim()) return;

    window.cep.fs.deleteFile(TRIGGER);

    var req;
    try { req = JSON.parse(r.data); } catch(e) {
        window.cep.fs.writeFile(RESULT, JSON.stringify({error: 'parse error: ' + e.message}));
        addLog('parse error: ' + e.message, 'error');
        return;
    }

    var code = req.code || '';
    var preview = code.replace(/\s+/g, ' ').substring(0, 80);
    addLog('▶ IPC: ' + preview, 'info');
    setStatus('IPC 実行中', 'ok');

    csInterface.evalScript(code, function(result) {
        var isErr = (result === 'EvalScript error.' || result === null || result === undefined);
        var resp = isErr
            ? JSON.stringify({error: result || 'eval error'})
            : JSON.stringify({result: result});
        window.cep.fs.writeFile(RESULT, resp);
        setStatus('監視中', 'ok');
        if (isErr) {
            addLog('✕ IPC: ' + (result || 'eval error'), 'error');
        } else {
            addLog('✓ IPC: ' + (result || '').toString().substring(0, 80), 'done');
        }
    });
}

// ─── Init ───
(function init() {
    writePing();
    addLog('Premiere Link 起動', 'done');
    setStatus('監視中', 'ok');

    // 記憶していたフォルダを復元
    var saved = '';
    try { saved = localStorage.getItem(LS_FOLDER) || ''; } catch (e) {}
    if (saved) loadScripts(saved);

    setInterval(poll, 200);
})();
