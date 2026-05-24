/* Photoshop Link UXP v1
 *
 * Premiere Link と同じ二役構成（CEP 廃止に伴う UXP 移行版）:
 *   (A) 任意の .psjs / .js ファイルを 1 クリックで実行（Script Launcher）
 *   (B) Python からのファイルポーリング IPC（trigger.json → exec → result.json）
 *
 * IPC ファイル名は CEP 版と衝突しないよう /tmp/photoshop_link_uxp_*.json を使用。
 * Photoshop 操作の具体的な API 呼び出し（actions / batchPlay 等）はこの後追加していく。
 */

const uxp = require("uxp");
const fs = require("fs");                          // Node 互換 fs（UXP 限定）
const photoshop = require("photoshop");
const { entrypoints, storage } = uxp;
const lfs = storage.localFileSystem;

// ─── IPC paths ───
const TRIGGER  = "/tmp/photoshop_link_uxp_trigger.json";
const RESULT   = "/tmp/photoshop_link_uxp_result.json";
const PING     = "/tmp/photoshop_link_uxp_ping.txt";
const ACTIVITY = "/tmp/photoshop_link_uxp_activity.json";

// ─── localStorage keys ───
const LS_FOLDER_TOKEN = "photoshopLinkFolderToken";

// ─── DOM ───
const statusEl = document.getElementById("status");
const dotEl    = document.getElementById("dot");
const logEl    = document.getElementById("log");
const scriptsEl = document.getElementById("scripts");
const folderEl  = document.getElementById("folder");

let activityLog = [];
let currentFolder = null;       // UXP folder entry
let currentFolderPath = "";

function setStatus(text, state) {
    if (statusEl) statusEl.textContent = text;
    if (dotEl) {
        dotEl.classList.remove("ok", "err");
        if (state) dotEl.classList.add(state);
    }
}

function escapeHtml(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
                    .replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function renderLog() {
    if (!logEl) return;
    if (!activityLog.length) {
        logEl.innerHTML = '<div class="empty">待機中</div>';
        return;
    }
    let html = "";
    for (const e of activityLog) {
        const cls = e.type === "error" ? "err" : e.type === "done" ? "done" : "info";
        html += `<div class="ln ${cls}"><span class="t">${escapeHtml(e.t)}</span>${escapeHtml(e.msg)}</div>`;
    }
    logEl.innerHTML = html;
}

function addLog(msg, type) {
    const entry = {
        t: new Date().toLocaleTimeString("ja-JP", { hour12: false }),
        msg: msg,
        type: type || "info",
    };
    activityLog.unshift(entry);
    if (activityLog.length > 30) activityLog.length = 30;
    try { fs.writeFileSync(ACTIVITY, JSON.stringify(activityLog)); } catch (e) {}
    renderLog();
}

// ─── Script folder / list ───
function renderScripts(files) {
    if (!scriptsEl) return;
    if (!files || !files.length) {
        scriptsEl.innerHTML = '<div class="empty">.psjs / .js ファイルなし</div>';
        return;
    }
    scriptsEl.innerHTML = "";
    for (const name of files) {
        const btn = document.createElement("button");
        btn.className = "scbtn";
        btn.title = name;
        btn.textContent = name;
        btn.addEventListener("click", () => runScript(name));
        scriptsEl.appendChild(btn);
    }
}

async function loadScriptsFromFolder(folder) {
    if (!folder) {
        folderEl.textContent = "";
        scriptsEl.innerHTML = '<div class="empty">フォルダ未選択</div>';
        return;
    }
    currentFolder = folder;
    currentFolderPath = folder.nativePath || folder.name || "";
    folderEl.textContent = currentFolderPath;
    try {
        const entries = await folder.getEntries();
        const files = entries
            .filter(e => e.isFile && /\.(psjs|js)$/i.test(e.name))
            .map(e => e.name)
            .sort();
        renderScripts(files);
    } catch (e) {
        addLog("フォルダ読み込み失敗: " + e.message, "error");
        renderScripts([]);
    }
}

async function pickScriptFolder() {
    try {
        const folder = await lfs.getFolder();
        if (!folder) return;
        // 永続トークン化して localStorage に保存
        try {
            const token = await lfs.createPersistentToken(folder);
            localStorage.setItem(LS_FOLDER_TOKEN, token);
        } catch (e) {}
        await loadScriptsFromFolder(folder);
        addLog("フォルダ設定: " + (folder.nativePath || folder.name), "done");
    } catch (e) {
        addLog("フォルダ選択キャンセル: " + e.message, "info");
    }
}

async function refreshScripts() {
    if (currentFolder) await loadScriptsFromFolder(currentFolder);
}

async function runScript(filename) {
    if (!currentFolder) return;
    addLog("▶ " + filename, "info");
    setStatus("実行中: " + filename, "ok");
    try {
        const fileEntry = await currentFolder.getEntry(filename);
        const code = await fileEntry.read({ format: storage.formats.utf8 });
        // photoshop API を AsyncFunction に注入してから eval
        const fn = new Function("photoshop", "uxp", "core", "app", "action", "constants", `
            return (async () => { ${code} })();
        `);
        await fn(
            photoshop,
            uxp,
            photoshop.core,
            photoshop.app,
            photoshop.action,
            photoshop.constants,
        );
        addLog("✓ " + filename, "done");
        setStatus("監視中", "ok");
    } catch (e) {
        addLog("✕ " + filename + " → " + e.message, "error");
        setStatus("監視中", "ok");
    }
}

// ─── IPC polling ───
let tickCount = 0;

function writePing() {
    try { fs.writeFileSync(PING, String(Date.now())); } catch (e) {}
}

async function pollIPC() {
    tickCount++;
    if (tickCount % 25 === 0) writePing();

    let raw;
    try {
        if (!fs.existsSync(TRIGGER)) return;
        raw = fs.readFileSync(TRIGGER, { encoding: "utf8" });
        if (!raw || !raw.trim()) return;
        fs.unlinkSync(TRIGGER);
    } catch (e) {
        return;
    }

    let req;
    try { req = JSON.parse(raw); }
    catch (e) {
        try { fs.writeFileSync(RESULT, JSON.stringify({ error: "parse error: " + e.message })); } catch {}
        addLog("parse error: " + e.message, "error");
        return;
    }

    const code = req.code || "";
    const preview = code.replace(/\s+/g, " ").substring(0, 80);
    addLog("▶ IPC: " + preview, "info");
    setStatus("IPC 実行中", "ok");

    try {
        const fn = new Function("photoshop", "uxp", "core", "app", "action", "constants", `
            return (async () => { ${code} })();
        `);
        const result = await fn(
            photoshop, uxp,
            photoshop.core, photoshop.app, photoshop.action, photoshop.constants,
        );
        try { fs.writeFileSync(RESULT, JSON.stringify({ result: result === undefined ? "ok" : result })); } catch {}
        addLog("✓ IPC", "done");
    } catch (e) {
        try { fs.writeFileSync(RESULT, JSON.stringify({ error: e.message })); } catch {}
        addLog("✕ IPC: " + e.message, "error");
    }
    setStatus("監視中", "ok");
}

// ─── Init ───
async function init() {
    // ボタン
    document.getElementById("btnPickFolder").addEventListener("click", pickScriptFolder);
    document.getElementById("btnRefresh").addEventListener("click", refreshScripts);

    writePing();
    addLog("Photoshop Link (UXP) 起動", "done");
    setStatus("監視中", "ok");

    // 永続トークンで前回フォルダを復元
    try {
        const token = localStorage.getItem(LS_FOLDER_TOKEN);
        if (token) {
            const folder = await lfs.getEntryForPersistentToken(token);
            if (folder) await loadScriptsFromFolder(folder);
        }
    } catch (e) {
        // トークンが無効 → 無視
    }

    setInterval(pollIPC, 200);
}

// パネル登録
entrypoints.setup({
    panels: {
        photoshopLinkPanel: {
            show() { /* DOM はパネル UI に既に存在 */ },
        },
    },
});

if (document.readyState === "complete" || document.readyState === "interactive") {
    init();
} else {
    document.addEventListener("DOMContentLoaded", init);
}
