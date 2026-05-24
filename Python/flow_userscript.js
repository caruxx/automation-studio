// ==UserScript==
// @name         Flow カスタマイズ
// @namespace    monomono-flow-ex
// @version      1.3
// @description  Google FlowでEnter送信を防ぐ（改行OK）。Shift+Enter / Ctrl(⌘)+Enter で送信（設定でON/OFF）。/editではEscで戻る＆アップスケール完了通知を自動で閉じる。
// @author       ものもの
// @match        https://labs.google/*
// @icon         https://www.google.com/s2/favicons?sz=64&domain=labs.google
// @grant        GM_setValue
// @grant        GM_getValue
// @updateURL    https://gist.github.com/blogmonomono/5e3aa029bf58b47a4f82cd3490d3316e/raw/flow_customize_tool.user.js
// @downloadURL  https://gist.github.com/blogmonomono/5e3aa029bf58b47a4f82cd3490d3316e/raw/flow_customize_tool.user.js
// @grant        GM_registerMenuCommand
// @grant        GM_unregisterMenuCommand
// @grant        GM_openInTab
// ==/UserScript==

(function () {
    'use strict';

    // ─── 状態管理 ────────────────────────────────────────
    const storedShift = GM_getValue('sendWithShiftEnter');
    const storedCtrlCmd = GM_getValue('sendWithCtrlCmdEnter');
    let sendWithShiftEnter = typeof storedShift === 'boolean' ? storedShift : true;
    let sendWithCtrlCmdEnter = typeof storedCtrlCmd === 'boolean' ? storedCtrlCmd : true;

    // 初期値が未設定ならデフォルトONで保存
    if (typeof storedShift !== 'boolean') {
        GM_setValue('sendWithShiftEnter', sendWithShiftEnter);
    }
    if (typeof storedCtrlCmd !== 'boolean') {
        GM_setValue('sendWithCtrlCmdEnter', sendWithCtrlCmdEnter);
    }

    // macOS判定（UserAgentやplatformで検出）
    const isMac = /Mac|iPad|iPhone/.test(navigator.platform || navigator.userAgent);

    /**
     * IMEの状態を自前で追跡する
     *
     * 【Windows/Mac共通バグ】
     * compositionend の直後に keydown(Enter, isComposing=false) が発火する「幽霊Enter」。
     * これが Google Flow の送信をトリガーしてしまう。
     *
     * 【macOS固有】
     * macOSのIMEはWindowsより変換確定後のイベント発火タイミングが遅いことがある。
     * ガード時間を200msに設定し、両OSに対応する。
     */
    let composing = false;       // IME変換中フラグ
    let justComposed = false;    // 変換確定直後フラグ（幽霊Enter対策）
    let justComposedTimer = null;
    const COMPOSE_GUARD_MS = 200; // macOSのIMEタイミングに対応（Windowsより余裕を持たせる）

    document.addEventListener('compositionstart', () => {
        composing = true;
        justComposed = false;
        if (justComposedTimer) clearTimeout(justComposedTimer);
    }, true);

    document.addEventListener('compositionend', () => {
        composing = false;
        // 変換確定直後 200ms はEnterをブロック（幽霊Enter対策、macOS対応）
        justComposed = true;
        if (justComposedTimer) clearTimeout(justComposedTimer);
        justComposedTimer = setTimeout(() => {
            justComposed = false;
        }, COMPOSE_GUARD_MS);
    }, true);

    // ─── 送信ボタン検索 ──────────────────────────────────
    function isVisibleEnabled(btn) {
        if (!btn) return false;
        if (btn.offsetParent === null) return false;
        if (btn.disabled) return false;
        if (btn.getAttribute('aria-disabled') === 'true') return false;
        return true;
    }

    function triggerSend() {
        // ── 最優先: google-symbols の arrow_forward アイコンを持つボタン ──
        // 新UIは aria-label を持たず、<i class="google-symbols">arrow_forward</i> と
        // visually-hidden の <span>作成</span> で構成されている。
        try {
            const icons = document.querySelectorAll('i.google-symbols, span.google-symbols');
            for (const icon of icons) {
                const txt = (icon.textContent || '').trim();
                if (txt !== 'arrow_forward') continue;
                const btn = icon.closest('button, [role="button"]');
                if (isVisibleEnabled(btn)) {
                    console.log('[Flow Override] Send (arrow_forward icon) →', btn);
                    btn.click();
                    return true;
                }
            }
        } catch (_) { }

        // ── 次点: visually-hidden の「作成」ラベルを持つボタン ──
        try {
            for (const btn of document.querySelectorAll('button, [role="button"]')) {
                if (!isVisibleEnabled(btn)) continue;
                const txt = (btn.textContent || '').trim();
                if (txt === '作成' || txt === 'Create' || /arrow_forward/.test(txt)) {
                    console.log('[Flow Override] Send (label match) →', btn);
                    btn.click();
                    return true;
                }
            }
        } catch (_) { }

        // ── 従来のセレクタフォールバック ──
        const selectors = [
            'button[aria-label*="Send"]',
            'button[aria-label*="送信"]',
            'button[aria-label*="Generate"]',
            'button[aria-label*="Compute"]',
            'button[aria-label*="生成"]',
            'button[aria-label*="作成"]',
            'button.send-button',
            '[role="button"][aria-label*="Send"]',
            'button[type="submit"]',
        ];

        for (const selector of selectors) {
            try {
                const btns = document.querySelectorAll(selector);
                for (const btn of btns) {
                    if (isVisibleEnabled(btn)) {
                        console.log('[Flow Override] Send →', btn);
                        btn.click();
                        return true;
                    }
                }
            } catch (_) { }
        }

        // テキストで広めに探す（最終フォールバック）
        for (const btn of document.querySelectorAll('button, [role="button"]')) {
            if (!isVisibleEnabled(btn)) continue;
            const text = btn.innerText || btn.textContent || '';
            if (/送信|Send|Generate|Compute|生成|作成|arrow_forward/.test(text)) {
                console.log('[Flow Override] Send (text match) →', btn);
                btn.click();
                return true;
            }
        }

        console.warn('[Flow Override] Send button not found.');
        return false;
    }

    // ─── バッチ実行（送信ボタン右クリック） ────────────────
    const BULK_IN_PROGRESS_THRESHOLD = 12;
    const BULK_POLL_MS = 2000;
    const BULK_COOLDOWN_MS = 4000;

    let bulkRemaining = 0;
    let bulkTimer = null;
    let bulkLastFire = 0;
    let bulkSavedPrompt = null;

    function countInProgressImages() {
        // 進捗表示 "NN%" のテキストを持つ葉 div を数える
        let count = 0;
        const divs = document.querySelectorAll('div');
        for (const d of divs) {
            if (d.children.length !== 0) continue;
            const txt = (d.textContent || '').trim();
            if (/^\d{1,3}%$/.test(txt)) count++;
        }
        return count;
    }

    function findPromptInput() {
        for (const ta of document.querySelectorAll('textarea')) {
            if (ta.offsetParent !== null) return ta;
        }
        for (const el of document.querySelectorAll('[contenteditable="true"]')) {
            if (el.offsetParent !== null) return el;
        }
        return null;
    }

    function getInputValue(input) {
        if (!input) return '';
        if (input.tagName === 'TEXTAREA' || input.tagName === 'INPUT') return input.value;
        return input.textContent || '';
    }

    function setInputValue(input, value) {
        if (!input) return;
        if (input.tagName === 'TEXTAREA' || input.tagName === 'INPUT') {
            // React管理下の input/textarea は native setter 経由で値を設定する
            const proto = input.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
            setter.call(input, value);
            input.dispatchEvent(new Event('input', { bubbles: true }));
        } else {
            input.focus();
            input.textContent = value;
            input.dispatchEvent(new InputEvent('input', { bubbles: true }));
        }
    }

    function updateBulkStatus() {
        const el = document.getElementById('gf-bulk-status');
        if (!el) return;
        if (bulkRemaining > 0) {
            const inProgress = countInProgressImages();
            el.textContent = `実行中: 残り ${bulkRemaining} 回 / 生成中 ${inProgress} 個`;
        } else {
            el.textContent = '';
        }
    }

    function stopBulk() {
        if (bulkTimer) { clearInterval(bulkTimer); bulkTimer = null; }
        bulkRemaining = 0;
        bulkSavedPrompt = null;
        updateBulkStatus();
        console.log('[Flow Override] Bulk stopped.');
    }

    function bulkTick() {
        if (bulkRemaining <= 0) { stopBulk(); return; }
        updateBulkStatus();
        const now = Date.now();
        if (now - bulkLastFire < BULK_COOLDOWN_MS) return;
        const inProgress = countInProgressImages();
        if (inProgress > BULK_IN_PROGRESS_THRESHOLD) return;

        // 必要ならプロンプトを復元してから送信
        if (bulkSavedPrompt) {
            const input = findPromptInput();
            if (input && getInputValue(input).trim() !== bulkSavedPrompt.trim()) {
                setInputValue(input, bulkSavedPrompt);
            }
        }

        // React が value を反映するのを少し待ってからクリック
        setTimeout(() => {
            if (triggerSend()) {
                bulkRemaining--;
                bulkLastFire = Date.now();
                console.log(`[Flow Override] Bulk fire → remaining=${bulkRemaining}`);
                updateBulkStatus();
                if (bulkRemaining <= 0) stopBulk();
            }
        }, 120);
    }

    function startBulk(n) {
        if (!Number.isFinite(n) || n <= 0) return;
        const input = findPromptInput();
        bulkSavedPrompt = input ? getInputValue(input) : null;
        bulkRemaining = n;
        bulkLastFire = 0;
        if (bulkTimer) clearInterval(bulkTimer);
        bulkTimer = setInterval(bulkTick, BULK_POLL_MS);
        console.log(`[Flow Override] Bulk start: ${n} runs`);
        bulkTick();
    }

    function openBulkDialog() {
        const existing = document.getElementById('gf-bulk-dialog');
        if (existing) { existing.remove(); return; }

        const dialog = document.createElement('div');
        dialog.id = 'gf-bulk-dialog';
        Object.assign(dialog.style, {
            position: 'fixed',
            background: '#1e1e1e',
            border: '1px solid rgba(255,255,255,0.15)',
            borderRadius: '12px',
            padding: '20px 24px',
            zIndex: '2000000',
            boxShadow: '0 12px 40px rgba(0,0,0,0.6)',
            fontFamily: 'sans-serif',
            color: '#f2f2f2',
            display: 'flex',
            flexDirection: 'column',
            gap: '14px',
            minWidth: '280px',
        });

        // 保存された位置があれば復元、なければ中央
        const savedPos = GM_getValue('bulkDialogPos', null);
        if (savedPos && typeof savedPos.left === 'number' && typeof savedPos.top === 'number') {
            dialog.style.left = savedPos.left + 'px';
            dialog.style.top = savedPos.top + 'px';
        } else {
            dialog.style.left = '50%';
            dialog.style.top = '50%';
            dialog.style.transform = 'translate(-50%, -50%)';
        }

        const title = document.createElement('div');
        title.textContent = '実行ボタンぽちぽちくん';
        title.style.cssText = 'font-size:15px;font-weight:bold;cursor:move;user-select:none;';

        // ── ドラッグで移動 ──
        let dragging = false;
        let dragOffsetX = 0;
        let dragOffsetY = 0;
        title.addEventListener('mousedown', (ev) => {
            if (ev.button !== 0) return;
            // translate が残っていれば実座標に変換してから外す
            const rect = dialog.getBoundingClientRect();
            dialog.style.transform = '';
            dialog.style.left = rect.left + 'px';
            dialog.style.top = rect.top + 'px';
            dragging = true;
            dragOffsetX = ev.clientX - rect.left;
            dragOffsetY = ev.clientY - rect.top;
            ev.preventDefault();
        });
        const onDragMove = (ev) => {
            if (!dragging) return;
            const maxLeft = window.innerWidth - 40;
            const maxTop = window.innerHeight - 40;
            const left = Math.max(0, Math.min(maxLeft, ev.clientX - dragOffsetX));
            const top = Math.max(0, Math.min(maxTop, ev.clientY - dragOffsetY));
            dialog.style.left = left + 'px';
            dialog.style.top = top + 'px';
        };
        const onDragEnd = () => {
            if (!dragging) return;
            dragging = false;
            const left = parseFloat(dialog.style.left) || 0;
            const top = parseFloat(dialog.style.top) || 0;
            GM_setValue('bulkDialogPos', { left, top });
        };
        document.addEventListener('mousemove', onDragMove, true);
        document.addEventListener('mouseup', onDragEnd, true);
        // ダイアログ削除時にグローバルリスナーも外す
        const origRemove = dialog.remove.bind(dialog);
        dialog.remove = () => {
            document.removeEventListener('mousemove', onDragMove, true);
            document.removeEventListener('mouseup', onDragEnd, true);
            origRemove();
        };

        const row = document.createElement('div');
        row.style.cssText = 'display:flex;align-items:center;gap:8px;font-size:13px;';
        const countInput = document.createElement('input');
        countInput.type = 'number';
        countInput.min = '1';
        countInput.max = '9999';
        countInput.value = GM_getValue('bulkCount', 10);
        countInput.style.cssText = 'width:80px;background:#2a2a2a;border:1px solid #3a3a3a;color:#f2f2f2;padding:6px 10px;border-radius:6px;font-size:14px;outline:none;text-align:right;';
        const label1 = document.createElement('span');
        label1.textContent = '回実行する';
        row.appendChild(countInput);
        row.appendChild(label1);

        const hint = document.createElement('div');
        hint.style.cssText = 'font-size:11px;color:#aaa;line-height:1.5;';
        hint.textContent = `生成中の画像が ${BULK_IN_PROGRESS_THRESHOLD} 個以下になったら自動で次を実行します。`;

        const status = document.createElement('div');
        status.id = 'gf-bulk-status';
        status.style.cssText = 'font-size:12px;color:#6cf;min-height:16px;';

        const btnRow = document.createElement('div');
        btnRow.style.cssText = 'display:flex;gap:8px;justify-content:flex-end;';

        const startBtn = document.createElement('button');
        startBtn.textContent = '実行開始';
        startBtn.style.cssText = 'background:#3a7afe;color:#fff;border:none;padding:8px 16px;border-radius:6px;cursor:pointer;font-size:13px;';

        const stopBtn = document.createElement('button');
        stopBtn.textContent = '停止';
        stopBtn.style.cssText = 'background:#6a2020;color:#fff;border:none;padding:8px 16px;border-radius:6px;cursor:pointer;font-size:13px;';

        const closeBtn = document.createElement('button');
        closeBtn.textContent = '閉じる';
        closeBtn.style.cssText = 'background:#333;color:#ddd;border:1px solid #555;padding:8px 16px;border-radius:6px;cursor:pointer;font-size:13px;';

        startBtn.addEventListener('click', () => {
            const n = parseInt(countInput.value, 10);
            if (!Number.isFinite(n) || n <= 0) return;
            GM_setValue('bulkCount', n);
            startBulk(n);
            updateBulkStatus();
        });
        stopBtn.addEventListener('click', () => stopBulk());
        closeBtn.addEventListener('click', () => dialog.remove());

        btnRow.appendChild(startBtn);
        btnRow.appendChild(stopBtn);
        btnRow.appendChild(closeBtn);

        dialog.appendChild(title);
        dialog.appendChild(row);
        dialog.appendChild(hint);
        dialog.appendChild(status);
        dialog.appendChild(btnRow);

        document.body.appendChild(dialog);
        updateBulkStatus();
        countInput.focus();
        countInput.select();
    }

    // 右クリック: 送信ボタン上でバッチ実行ダイアログを開く
    document.addEventListener('contextmenu', (e) => {
        const btn = e.target && e.target.closest && e.target.closest('button, [role="button"]');
        if (!btn) return;
        const icon = btn.querySelector('i.google-symbols, span.google-symbols');
        if (!icon) return;
        if ((icon.textContent || '').trim() !== 'arrow_forward') return;
        e.preventDefault();
        e.stopPropagation();
        openBulkDialog();
    }, true);

    // ─── DLバッジ色 ──────────────────────────────────────
    let badgeColor = GM_getValue('badgeColor', '#990000');

    function applyBadgeColor(color) {
        let el = document.getElementById('gf-badge-color-style');
        if (!el) {
            el = document.createElement('style');
            el.id = 'gf-badge-color-style';
            document.head.appendChild(el);
        }
        el.textContent = `.gf-dl-badge { background: ${color} !important; }`;
    }

    applyBadgeColor(badgeColor);

    // ─── 設定メニュー ──────────────────────────────────
    const menuCommandIds = [];
    function refreshMenuCommands() {
        if (typeof GM_registerMenuCommand !== 'function') return;

        if (typeof GM_unregisterMenuCommand === 'function') {
            while (menuCommandIds.length) {
                const id = menuCommandIds.pop();
                try {
                    GM_unregisterMenuCommand(id);
                } catch (_) { }
            }
        } else {
            // GM_unregisterMenuCommand 非対応環境では重複登録を避けるため再登録しない
            if (menuCommandIds.length) return;
        }

        menuCommandIds.push(GM_registerMenuCommand(
            `Shift+Enter 送信: ${sendWithShiftEnter ? 'ON' : 'OFF'}`,
            () => {
                sendWithShiftEnter = !sendWithShiftEnter;
                GM_setValue('sendWithShiftEnter', sendWithShiftEnter);
                console.log(`[Flow Override] sendWithShiftEnter = ${sendWithShiftEnter}`);
                refreshMenuCommands();
            }
        ));

        menuCommandIds.push(GM_registerMenuCommand(
            `${isMac ? '⌘+Enter' : 'Ctrl+Enter'} 送信: ${sendWithCtrlCmdEnter ? 'ON' : 'OFF'}`,
            () => {
                sendWithCtrlCmdEnter = !sendWithCtrlCmdEnter;
                GM_setValue('sendWithCtrlCmdEnter', sendWithCtrlCmdEnter);
                console.log(`[Flow Override] sendWithCtrlCmdEnter = ${sendWithCtrlCmdEnter}`);
                refreshMenuCommands();
            }
        ));

        menuCommandIds.push(GM_registerMenuCommand(
            'DLバッジの色を変更',
            () => {
                const existing = document.getElementById('gf-badge-color-dialog');
                if (existing) { existing.remove(); return; }

                const dialog = document.createElement('div');
                dialog.id = 'gf-badge-color-dialog';
                Object.assign(dialog.style, {
                    position: 'fixed',
                    top: '50%',
                    left: '50%',
                    transform: 'translate(-50%, -50%)',
                    background: '#1e1e1e',
                    border: '1px solid rgba(255,255,255,0.15)',
                    borderRadius: '12px',
                    padding: '20px 24px',
                    zIndex: '2000000',
                    boxShadow: '0 12px 40px rgba(0,0,0,0.6)',
                    fontFamily: 'sans-serif',
                    color: '#f2f2f2',
                    display: 'flex',
                    flexDirection: 'column',
                    gap: '14px',
                    minWidth: '240px',
                });

                const label = document.createElement('div');
                label.textContent = 'DLバッジの背景色';
                label.style.cssText = 'font-size:14px;font-weight:bold;';

                const row = document.createElement('div');
                row.style.cssText = 'display:flex;align-items:center;gap:12px;';

                const picker = document.createElement('input');
                picker.type = 'color';
                picker.value = badgeColor;
                picker.style.cssText = 'width:48px;height:36px;border:none;background:none;cursor:pointer;padding:0;border-radius:6px;';

                const hexInput = document.createElement('input');
                hexInput.type = 'text';
                hexInput.value = badgeColor;
                hexInput.maxLength = 7;
                hexInput.style.cssText = 'flex:1;background:#2a2a2a;border:1px solid #3a3a3a;color:#f2f2f2;padding:6px 10px;border-radius:6px;font-size:13px;outline:none;';

                picker.addEventListener('input', () => {
                    badgeColor = picker.value;
                    hexInput.value = badgeColor;
                    applyBadgeColor(badgeColor);
                });

                hexInput.addEventListener('input', () => {
                    const v = hexInput.value;
                    if (/^#[0-9a-fA-F]{6}$/.test(v)) {
                        badgeColor = v;
                        picker.value = v;
                        applyBadgeColor(badgeColor);
                    }
                });

                const btnRow = document.createElement('div');
                btnRow.style.cssText = 'display:flex;gap:8px;justify-content:flex-end;';

                const saveBtn = document.createElement('button');
                saveBtn.textContent = '保存して閉じる';
                saveBtn.style.cssText = 'padding:7px 14px;border-radius:8px;border:none;background:#4f8cff;color:#fff;cursor:pointer;font-size:13px;';
                saveBtn.addEventListener('click', () => {
                    GM_setValue('badgeColor', badgeColor);
                    dialog.remove();
                });

                const cancelBtn = document.createElement('button');
                cancelBtn.textContent = 'キャンセル';
                cancelBtn.style.cssText = 'padding:7px 14px;border-radius:8px;border:1px solid #3a3a3a;background:#2a2a2a;color:#f2f2f2;cursor:pointer;font-size:13px;';
                cancelBtn.addEventListener('click', () => {
                    badgeColor = GM_getValue('badgeColor', '#990000');
                    applyBadgeColor(badgeColor);
                    dialog.remove();
                });

                row.appendChild(picker);
                row.appendChild(hexInput);
                btnRow.appendChild(cancelBtn);
                btnRow.appendChild(saveBtn);
                dialog.appendChild(label);
                dialog.appendChild(row);
                dialog.appendChild(btnRow);
                document.body.appendChild(dialog);
            }
        ));
    }

    // ─── キーイベントハンドラ ────────────────────────────
    function isFlowEditUrl() {
        const path = window.location.pathname || '';
        const isFlow = path.includes('/fx/ja/tools/flow') || path.includes('/fx/tools/flow');
        return isFlow && path.includes('/edit');
    }

    // ─── トースト（モジュールスコープ） ─────────────────────
    function showToast(message) {
        const existing = document.getElementById('gf-global-toast');
        if (existing) existing.remove();
        const toast = document.createElement('div');
        toast.id = 'gf-global-toast';
        toast.textContent = message;
        Object.assign(toast.style, {
            position: 'fixed',
            bottom: '28px',
            left: '50%',
            transform: 'translateX(-50%)',
            background: 'rgba(20, 20, 20, 0.92)',
            border: '1px solid rgba(255, 255, 255, 0.16)',
            color: '#f2f2f2',
            padding: '10px 14px',
            borderRadius: '999px',
            fontSize: '13px',
            boxShadow: '0 8px 24px rgba(0,0,0,0.35)',
            zIndex: '1000000',
            transition: 'opacity 0.2s ease',
            opacity: '0',
            fontFamily: 'sans-serif',
            pointerEvents: 'none',
        });
        document.body.appendChild(toast);
        requestAnimationFrame(() => { toast.style.opacity = '1'; });
        setTimeout(() => {
            toast.style.opacity = '0';
            setTimeout(() => toast.remove(), 250);
        }, 2500);
    }

    function getEditImageId() {
        const match = (window.location.pathname || '').match(/\/edit\/([a-f0-9-]+)/);
        return match ? match[1] : null;
    }

    // ─── Alt+Shift+D → ダウンロード > 2K ──────────────────
    function pointerClick(el) {
        const rect = el.getBoundingClientRect();
        const cx = rect.left + rect.width / 2;
        const cy = rect.top + rect.height / 2;
        const opts = { bubbles: true, cancelable: true, clientX: cx, clientY: cy, pointerId: 1, pointerType: 'mouse', isPrimary: true };
        el.dispatchEvent(new PointerEvent('pointerdown', opts));
        el.dispatchEvent(new PointerEvent('pointerup', opts));
        el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, clientX: cx, clientY: cy }));
    }

    function triggerDownload2K() {
        const allButtons = [...document.querySelectorAll('button, [role="button"]')];
        const downloadBtn = allButtons.find(b => /ダウンロード/.test(b.innerText || b.textContent || ''));
        if (!downloadBtn) {
            console.warn('[Flow Override] Download button not found.');
            return;
        }
        pointerClick(downloadBtn);
        setTimeout(() => {
            const menuItems = [...document.querySelectorAll('[role="menuitem"]')];
            const btn2K = menuItems.find(b => /^2K/.test((b.innerText || b.textContent || '').trim()));
            if (btn2K) {
                console.log('[Flow Override] Alt+Shift+D → Download 2K');
                pointerClick(btn2K);
            } else {
                console.warn('[Flow Override] 2K menu item not found.');
            }
        }, 300);
    }

    const DL_HISTORY_KEY = 'flowDownloadHistory';

    function openAutoDownloadTab(linkHref) {
        const m = linkHref.match(/\/edit\/([a-f0-9-]+)/);
        if (m) {
            const history = JSON.parse(localStorage.getItem(DL_HISTORY_KEY) || '[]');
            const resolutions = [...new Set(history.filter(e => e.imageId === m[1]).map(e => e.resolution))].sort().reverse();
            if (resolutions.length) {
                const ok = window.confirm(`この画像は既に ${resolutions.join('/')} でダウンロード済みです。\n再度ダウンロードしますか？`);
                if (!ok) return;
            }
        }
        const targetUrl = linkHref + (linkHref.includes('?') ? '&' : '?') + 'gf-auto-dl=1';
        GM_openInTab(targetUrl, { active: false });
    }

    function onDownloadResolution(resolution) {
        const imageId = getEditImageId();
        if (!imageId) return;

        const history = JSON.parse(localStorage.getItem(DL_HISTORY_KEY) || '[]');
        history.push({ imageId, resolution, downloadedAt: new Date().toISOString() });
        localStorage.setItem(DL_HISTORY_KEY, JSON.stringify(history));
        console.log(`[Flow Override] DL history saved: ${resolution} ${imageId}`);

        applyDownloadBadges();
        showToast(`${resolution} DL: ${imageId}`);
    }

    // 手動クリックも検出（1K / 2K）
    document.addEventListener('click', (e) => {
        if (!isFlowEditUrl()) return;
        const target = e.target.closest('[role="menuitem"]');
        if (!target) return;
        const text = (target.innerText || target.textContent || '').trim();
        const match = text.match(/^([12]K)/);
        if (!match) return;
        onDownloadResolution(match[1]);
    }, true);

    // ─── DL済みバッジ ─────────────────────────────────────
    (function injectStyles() {
        const style = document.createElement('style');
        style.textContent = `
            .gf-dl-badge {
                position: absolute;
                top: 8px;
                left: 8px;
                background: #990000;
                color: #fff;
                font-size: 11px;
                font-weight: bold;
                padding: 3px 8px;
                border-radius: 999px;
                z-index: 10;
                pointer-events: none;
                font-family: sans-serif;
                letter-spacing: 0.3px;
                box-shadow: 0 1px 4px rgba(0, 0, 0, 0.4);
            }
            .gf-dl-thumb-btn {
                position: absolute;
                bottom: 8px;
                right: 48px;
                background: rgba(20, 20, 20, 0.82);
                color: #fff;
                font-size: 12px;
                padding: 5px 12px;
                border-radius: 999px;
                z-index: 10;
                border: 1px solid rgba(255, 255, 255, 0.2);
                cursor: pointer;
                font-family: sans-serif;
                box-shadow: 0 1px 4px rgba(0, 0, 0, 0.5);
                transition: background 0.15s;
            }
            .gf-dl-thumb-btn:hover {
                background: rgba(34, 197, 94, 0.88);
                border-color: rgba(34, 197, 94, 0.6);
            }
            .gf-preview-btn {
                position: absolute;
                bottom: 8px;
                right: 8px;
                background: rgba(20, 20, 20, 0.82);
                color: #fff;
                font-size: 14px;
                width: 32px;
                height: 32px;
                border-radius: 50%;
                z-index: 10;
                border: 1px solid rgba(255, 255, 255, 0.2);
                cursor: pointer;
                font-family: sans-serif;
                box-shadow: 0 1px 4px rgba(0, 0, 0, 0.5);
                transition: background 0.15s;
                display: flex;
                align-items: center;
                justify-content: center;
                padding: 0;
            }
            .gf-preview-btn:hover {
                background: rgba(99, 179, 237, 0.88);
                border-color: rgba(99, 179, 237, 0.6);
            }
            #gf-dl-overlay {
                position: fixed;
                inset: 0;
                background: rgba(0, 0, 0, 0.72);
                z-index: 999999;
                display: flex;
                align-items: center;
                justify-content: center;
            }
            #gf-dl-overlay .gf-spinner {
                width: 48px;
                height: 48px;
                border: 4px solid rgba(255, 255, 255, 0.2);
                border-top-color: #fff;
                border-radius: 50%;
                animation: gf-spin 0.8s linear infinite;
            }
            @keyframes gf-spin {
                to { transform: rotate(360deg); }
            }
            #gf-thumb-preview {
                position: fixed;
                inset: 0;
                z-index: 1000001;
                background: rgba(0, 0, 0, 0.9);
                display: flex;
                align-items: center;
                justify-content: center;
                pointer-events: none;
                opacity: 0;
                transition: opacity 0.15s ease;
            }
            #gf-thumb-preview.visible {
                opacity: 1;
            }
            #gf-thumb-preview img {
                display: block;
                max-width: calc(100vw - 48px);
                max-height: calc(100vh - 120px);
                object-fit: contain;
                border-radius: 8px;
                box-shadow: 0 12px 48px rgba(0, 0, 0, 0.8);
            }
            #gf-thumb-preview .gf-preview-img-wrap {
                position: relative;
                display: inline-flex;
                flex-direction: column;
                align-items: center;
            }
            #gf-thumb-preview .gf-preview-hint {
                margin-top: 10px;
                color: rgba(255, 255, 255, 0.5);
                font-size: 12px;
                font-family: sans-serif;
                text-align: center;
                white-space: nowrap;
                pointer-events: none;
                line-height: 1.8;
            }
            #gf-thumb-preview .gf-preview-dl-badge {
                position: absolute;
                top: 8px;
                left: 8px;
                background: #990000;
                color: #fff;
                font-size: 12px;
                font-weight: bold;
                padding: 3px 10px;
                border-radius: 999px;
                font-family: sans-serif;
                pointer-events: none;
                box-shadow: 0 2px 8px rgba(0, 0, 0, 0.5);
                white-space: nowrap;
                z-index: 1;
            }
        `;
        document.head.appendChild(style);
    })();

    // ─── サムネイルホバープレビュー ─────────────────────────
    const PREVIEW_MOVE_THRESHOLD = 80;
    let currentPreviewBtn = null;
    let currentMouseX = 0;
    let currentMouseY = 0;

    document.addEventListener('mousemove', (e) => {
        currentMouseX = e.clientX;
        currentMouseY = e.clientY;
    }, true);

    function showThumbPreview(src, startX, startY) {
        let preview = document.getElementById('gf-thumb-preview');
        if (!preview) {
            preview = document.createElement('div');
            preview.id = 'gf-thumb-preview';
            const wrap = document.createElement('div');
            wrap.className = 'gf-preview-img-wrap';
            const img = document.createElement('img');
            wrap.appendChild(img);
            const dlBadge = document.createElement('div');
            dlBadge.className = 'gf-preview-dl-badge';
            dlBadge.style.display = 'none';
            wrap.appendChild(dlBadge);
            const hint = document.createElement('div');
            hint.className = 'gf-preview-hint';
            hint.innerHTML = `← → キーで前後の画像を表示<br>${isMac ? 'Option' : 'Alt'}+Shift+D で 2K ダウンロード`;
            wrap.appendChild(hint);
            preview.appendChild(wrap);
            document.body.appendChild(preview);
        }
        const img = preview.querySelector('img');

        // DL済みバッジ・ボーダーを更新
        const dlBadge = preview.querySelector('.gf-preview-dl-badge');
        if (dlBadge && currentPreviewBtn) {
            const link = currentPreviewBtn.parentElement?.querySelector('a[href*="/edit/"]');
            const m = link?.href.match(/\/edit\/([a-f0-9-]+)/);
            const isDl = (() => {
                if (!m) return false;
                const history = JSON.parse(localStorage.getItem(DL_HISTORY_KEY) || '[]');
                const resolutions = [...new Set(history.filter(e => e.imageId === m[1]).map(e => e.resolution))].sort().reverse();
                if (!resolutions.length) return false;
                dlBadge.textContent = `✓ ${resolutions.join('/')} DL済み`;
                return true;
            })();
            dlBadge.style.display = isDl ? '' : 'none';
            img.style.border = isDl ? `4px solid ${badgeColor}` : '';
        }

        const alreadyVisible = preview.classList.contains('visible');

        if (!alreadyVisible) {
            // 初回表示：フェードイン
            preview.classList.remove('visible');
            img.onload = () => { preview.classList.add('visible'); };
        }
        img.src = src;
        if (!alreadyVisible && img.complete && img.naturalWidth) {
            preview.classList.add('visible');
        }

        // 古いハンドラが残っていれば必ず解除してから登録
        if (preview._moveHandler) {
            document.removeEventListener('mousemove', preview._moveHandler);
        }
        const moveHandler = (e) => {
            const dx = e.clientX - startX;
            const dy = e.clientY - startY;
            if (Math.sqrt(dx * dx + dy * dy) > PREVIEW_MOVE_THRESHOLD) {
                hideThumbPreview();
            }
        };
        document.addEventListener('mousemove', moveHandler);
        preview._moveHandler = moveHandler;
    }

    function hideThumbPreview() {
        const preview = document.getElementById('gf-thumb-preview');
        if (preview) {
            preview.classList.remove('visible');
            if (preview._moveHandler) {
                document.removeEventListener('mousemove', preview._moveHandler);
                preview._moveHandler = null;
            }
        }
        currentPreviewBtn = null;
    }

    document.addEventListener('keydown', (e) => {
        if (!currentPreviewBtn) return;
        const preview = document.getElementById('gf-thumb-preview');
        if (!preview || !preview.classList.contains('visible')) return;

        // Alt+Shift+D → ↓2K ボタンと同じ動作（背景タブで自動DL）
        if (e.code === 'KeyD' && e.altKey && e.shiftKey && !e.ctrlKey && !e.metaKey) {
            e.preventDefault();
            e.stopPropagation();
            const container = currentPreviewBtn.parentElement;
            const link = container?.querySelector('a[href*="/edit/"]');
            if (link?.href) {
                openAutoDownloadTab(link.href);
            }
            return;
        }

        if (e.key === 'Escape') {
            e.preventDefault();
            e.stopPropagation();
            hideThumbPreview();
            return;
        }

        if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;

        e.preventDefault();
        e.stopPropagation();

        const allBtns = [...document.querySelectorAll('.gf-preview-btn')];
        const idx = allBtns.indexOf(currentPreviewBtn);
        if (idx === -1) return;

        const nextIdx = e.key === 'ArrowLeft' ? idx - 1 : idx + 1;
        if (nextIdx < 0 || nextIdx >= allBtns.length) return;

        const nextBtn = allBtns[nextIdx];
        const nextImg = nextBtn.parentElement?.querySelector('img');
        if (!nextImg?.src) return;

        currentPreviewBtn = nextBtn;
        showThumbPreview(nextImg.src, currentMouseX, currentMouseY);
    }, true);

    function applyDownloadBadges() {
        if (!isFlowPage()) return;
        const history = JSON.parse(localStorage.getItem(DL_HISTORY_KEY) || '[]');

        // imageId → ダウンロード済み解像度セット
        const dlMap = {};
        for (const entry of history) {
            if (!dlMap[entry.imageId]) dlMap[entry.imageId] = new Set();
            dlMap[entry.imageId].add(entry.resolution);
        }

        for (const link of document.querySelectorAll('a[href*="/edit/"]')) {
            const m = link.href.match(/\/edit\/([a-f0-9-]+)/);
            if (!m) continue;
            const container = link.parentElement;
            if (!container) continue;

            // DL済みバッジ
            const resolutions = dlMap[m[1]];
            if (resolutions && !container.querySelector('.gf-dl-badge')) {
                const label = [...resolutions].sort().reverse().join('/');
                const badge = document.createElement('div');
                badge.className = 'gf-dl-badge';
                badge.textContent = `✓ ${label}`;
                container.appendChild(badge);
            }

            // DLボタン（全サムネイルに配置）
            if (!container.querySelector('.gf-dl-thumb-btn')) {
                const btn = document.createElement('button');
                btn.className = 'gf-dl-thumb-btn';
                btn.textContent = '↓ 2K';
                btn.addEventListener('click', (e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    openAutoDownloadTab(link.href);
                });

                container.appendChild(btn);
            }

            // プレビューボタン（目アイコン）
            if (!container.querySelector('.gf-preview-btn')) {
                const thumbImg = container.querySelector('img');
                if (thumbImg) {
                    const previewBtn = document.createElement('button');
                    previewBtn.className = 'gf-preview-btn';
                    previewBtn.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3" fill="currentColor"/></svg>`;
                    let previewTimer = null;
                    previewBtn.addEventListener('mouseenter', (e) => {
                        const enterX = e.clientX;
                        const enterY = e.clientY;
                        previewTimer = setTimeout(() => {
                            if (!thumbImg.src) return;
                            currentPreviewBtn = previewBtn;
                            showThumbPreview(thumbImg.src, enterX, enterY);
                        }, 50);
                    });
                    previewBtn.addEventListener('mouseleave', () => {
                        clearTimeout(previewTimer);
                        previewTimer = null;
                        hideThumbPreview();
                    });
                    previewBtn.addEventListener('click', (e) => {
                        e.preventDefault();
                        e.stopPropagation();
                    });
                    container.appendChild(previewBtn);
                }
            }
        }
    }

    let badgeObserverStarted = false;
    function startBadgeObserver() {
        if (badgeObserverStarted) return;
        badgeObserverStarted = true;
        let timer = null;
        new MutationObserver(() => {
            if (!isFlowPage()) return;
            clearTimeout(timer);
            timer = setTimeout(applyDownloadBadges, 300);
        }).observe(document.body, { subtree: true, childList: true });
    }

    function handleEscape(e) {
        if (e.repeat) return;
        if (!isFlowEditUrl()) return;

        // Alt+Shift+D → ダウンロード > 2K
        if (e.code === 'KeyD' && e.altKey && e.shiftKey && !e.ctrlKey && !e.metaKey) {
            e.preventDefault();
            e.stopImmediatePropagation();
            triggerDownload2K();
            return;
        }

        if (e.key !== 'Escape') return;
        // Edit URLでは Esc でブラウザの戻る挙動を優先
        e.preventDefault();
        e.stopImmediatePropagation();
        window.history.back();
    }

    function handleKey(e) {
        if (e.key !== 'Enter') return;

        const target = e.target;
        const isInput = target.tagName === 'P' ||
            target.tagName === 'TEXTAREA' ||
            target.tagName === 'INPUT' ||
            target.isContentEditable ||
            !!target.closest('[contenteditable="true"]');

        if (!isInput) return;

        // ── IME変換中 or 変換確定直後 → サイトへの伝播のみブロック ──
        // (preventDefaultはしないことで、ブラウザ本来の確定動作や改行を許可する)
        if (composing || justComposed || e.isComposing || e.keyCode === 229) {
            console.log(`[Flow Override] IME Enter handled (site send blocked)`);
            e.stopPropagation();
            e.stopImmediatePropagation();
            return;
        }

        // ── 送信トリガー判定 ──
        // Shift+Enter: Win/Mac共通（設定でON/OFF）
        // Ctrl/⌘+Enter: Win/Mac共通（設定でON/OFF）
        const isShiftSend = sendWithShiftEnter && e.shiftKey;
        const isCtrlCmdSend = sendWithCtrlCmdEnter && (e.ctrlKey || e.metaKey);
        const isSendKey = isShiftSend || isCtrlCmdSend;

        if (isSendKey) {
            if (e.type === 'keydown') {
                let label = 'Enter';
                if (isShiftSend) label = 'Shift+Enter';
                else if (isCtrlCmdSend) label = isMac ? '⌘+Enter' : 'Ctrl+Enter';
                console.log(`[Flow Override] ${label} → Send`);
                // 送信時は「送信」だけしたいので改行等のデフォルト動作を止める
                e.preventDefault();
                e.stopImmediatePropagation();
                triggerSend();
            } else {
                e.preventDefault();
                e.stopImmediatePropagation();
            }
            return;
        }

        // ── Enter単体 → サイトへの送信挙動のみブロック ──
        // preventDefault() を呼ばないことで、ブラウザ標準の「改行」は通す。
        // ただし stopPropagation() により、サイト側の「Enterで送信」リスナーには届かせない。
        console.log(`[Flow Override] Enter (Newline allowed, Send blocked) on <${target.tagName}> (${e.type})`);
        e.stopPropagation();
        e.stopImmediatePropagation();
    }

    // キャプチャフェーズで全フェーズ取得
    window.addEventListener('keydown', handleEscape, true);
    window.addEventListener('keydown', handleKey, true);
    window.addEventListener('keypress', handleKey, true);
    window.addEventListener('keyup', handleKey, true);

    // ─── add_2 右側にボタン追加 + 仮ダイアログ ───────────────
    const ADD2_ICON_TEXT = 'add_2';
    const ADD2_BUTTON_ID = 'gf-add2-extra-btn';
    const ADD2_DIALOG_OVERLAY_ID = 'gf-add2-dialog-overlay';
    const TEMPLATE_STORAGE_KEY = 'flowTemplates';

    function loadTemplates() {
        const stored = GM_getValue(TEMPLATE_STORAGE_KEY, []);
        if (Array.isArray(stored)) {
            return stored.map((item) => {
                if (typeof item === 'string') return { name: item, text: '' };
                if (item && typeof item === 'object') {
                    return { name: item.name || '', text: item.text || '' };
                }
                return { name: '', text: '' };
            });
        }
        if (typeof stored === 'string') {
            try {
                const parsed = JSON.parse(stored);
                if (!Array.isArray(parsed)) return [];
                return parsed.map((item) => {
                    if (typeof item === 'string') return { name: item, text: '' };
                    if (item && typeof item === 'object') {
                        return { name: item.name || '', text: item.text || '' };
                    }
                    return { name: '', text: '' };
                });
            } catch (_) {
                return [];
            }
        }
        return [];
    }

    function saveTemplates(templates) {
        GM_setValue(TEMPLATE_STORAGE_KEY, templates);
    }

    function isFlowPage() {
        const path = window.location.pathname || '';
        return path.includes('/fx/ja/tools/flow') || path.includes('/fx/tools/flow');
    }

    function findAdd2Icon() {
        const icons = document.querySelectorAll('i.google-symbols, i');
        for (const icon of icons) {
            const text = (icon.textContent || '').trim();
            if (text === ADD2_ICON_TEXT) return icon;
        }
        return null;
    }

    function ensureAdd2Dialog() {
        let overlay = document.getElementById(ADD2_DIALOG_OVERLAY_ID);
        if (overlay) return overlay;

        overlay = document.createElement('div');
        overlay.id = ADD2_DIALOG_OVERLAY_ID;
        Object.assign(overlay.style, {
            position: 'fixed',
            inset: '0',
            background: 'rgba(0, 0, 0, 0.45)',
            display: 'none',
            alignItems: 'center',
            justifyContent: 'center',
            zIndex: '999999',
        });

        const panel = document.createElement('div');
        Object.assign(panel.style, {
            width: 'min(860px, 94vw)',
            height: '80vh',
            background: '#141414',
            border: '1px solid rgba(255, 255, 255, 0.12)',
            borderRadius: '16px',
            padding: '20px',
            boxShadow: '0 18px 48px rgba(0,0,0,0.35), 0 0 0 1px rgba(255,255,255,0.06)',
            fontFamily: 'sans-serif',
            color: '#f2f2f2',
            display: 'flex',
            flexDirection: 'column',
        });

        const title = document.createElement('h2');
        title.textContent = 'プロンプトテンプレート';
        Object.assign(title.style, {
            margin: '0 0 8px 0',
            fontSize: '18px',
            color: '#f7f7f7',
        });

        const body = document.createElement('p');
        body.textContent = '左でテンプレートを選択し、右で内容を編集します。内容は自動保存されます。\n左をダブルクリックするとプロンプト入力欄に挿入されます。\n左のアイテムをD&Dで順番を入れ替えられます。';
        Object.assign(body.style, {
            margin: '0 0 12px 0',
            fontSize: '13px',
            color: '#c9c9c9',
            whiteSpace: 'pre-line',
        });

        const layout = document.createElement('div');
        Object.assign(layout.style, {
            display: 'grid',
            gridTemplateColumns: '180px 1fr',
            gap: '12px',
            flex: '1',
            minHeight: '0',
            marginBottom: '12px',
        });

        const sidebar = document.createElement('div');
        Object.assign(sidebar.style, {
            borderRight: '1px solid #2a2a2a',
            paddingRight: '8px',
            overflow: 'auto',
        });

        const sidebarList = document.createElement('div');
        Object.assign(sidebarList.style, {
            display: 'flex',
            flexDirection: 'column',
            gap: '6px',
        });
        sidebar.appendChild(sidebarList);

        const editor = document.createElement('div');
        Object.assign(editor.style, {
            display: 'flex',
            flexDirection: 'column',
            gap: '8px',
            overflow: 'auto',
            minHeight: '0',
        });

        const emptyState = document.createElement('div');
        emptyState.textContent = 'テンプレートを選択してください';
        Object.assign(emptyState.style, {
            fontSize: '13px',
            color: '#9a9a9a',
            padding: '8px 0',
        });

        const nameInput = document.createElement('input');
        nameInput.type = 'text';
        nameInput.placeholder = 'テンプレート名';
        Object.assign(nameInput.style, {
            width: '100%',
            padding: '8px 10px',
            borderRadius: '8px',
            border: '1px solid #2c2c2c',
            background: '#1d1d1d',
            color: '#f2f2f2',
            fontSize: '13px',
            boxSizing: 'border-box',
            outline: 'none',
            boxShadow: 'none',
        });

        const textArea = document.createElement('textarea');
        textArea.placeholder = 'テンプレート本文（複数行OK）';
        Object.assign(textArea.style, {
            width: '100%',
            flex: '1',
            minHeight: '0',
            padding: '8px 10px',
            borderRadius: '8px',
            border: '1px solid #2c2c2c',
            background: '#1d1d1d',
            color: '#f2f2f2',
            fontSize: '13px',
            resize: 'vertical',
            boxSizing: 'border-box',
            outline: 'none',
            boxShadow: 'none',
        });

        const applyEditorFocusStyles = (el) => {
            el.addEventListener('focus', () => {
                el.style.border = '1px solid #4f8cff';
                el.style.boxShadow = '0 0 0 1px rgba(79, 140, 255, 0.35)';
            });
            el.addEventListener('blur', () => {
                el.style.border = '1px solid #2c2c2c';
                el.style.boxShadow = 'none';
            });
        };
        applyEditorFocusStyles(nameInput);
        applyEditorFocusStyles(textArea);

        editor.appendChild(emptyState);

        const actions = document.createElement('div');
        Object.assign(actions.style, {
            display: 'flex',
            gap: '8px',
            justifyContent: 'space-between',
            alignItems: 'center',
        });

        const addBtn = document.createElement('button');
        addBtn.type = 'button';
        addBtn.textContent = '＋ テンプレート追加';
        Object.assign(addBtn.style, {
            padding: '8px 12px',
            borderRadius: '10px',
            border: '1px solid #2c2c2c',
            background: '#1d1d1d',
            color: '#f2f2f2',
            cursor: 'pointer',
            fontSize: '13px',
        });

        const deleteBtn = document.createElement('button');
        deleteBtn.type = 'button';
        deleteBtn.textContent = '選択したテンプレートを削除';
        Object.assign(deleteBtn.style, {
            padding: '8px 12px',
            borderRadius: '10px',
            border: '1px solid #4a2020',
            background: '#2b1818',
            color: '#ffdddd',
            cursor: 'pointer',
            fontSize: '13px',
        });

        const closeBtn = document.createElement('button');
        closeBtn.type = 'button';
        closeBtn.textContent = '閉じる';
        Object.assign(closeBtn.style, {
            padding: '8px 12px',
            borderRadius: '10px',
            border: '1px solid #2c2c2c',
            background: '#1d1d1d',
            color: '#f2f2f2',
            cursor: 'pointer',
            fontSize: '13px',
            marginLeft: 'auto',
        });
        closeBtn.addEventListener('click', closeDialog);

        let templates = loadTemplates();
        let selectedIndex = templates.length ? 0 : -1;
        let draggingIndex = -1;

        const reorderTemplates = (fromIndex, toIndex) => {
            if (fromIndex < 0 || toIndex < 0) return;
            if (fromIndex === toIndex) return;
            if (fromIndex >= templates.length || toIndex >= templates.length) return;

            const selectedTemplate = selectedIndex >= 0 ? templates[selectedIndex] : null;
            const [moved] = templates.splice(fromIndex, 1);
            templates.splice(toIndex, 0, moved);

            if (selectedTemplate) {
                selectedIndex = templates.indexOf(selectedTemplate);
            } else {
                selectedIndex = templates.length ? 0 : -1;
            }

            saveTemplates(templates);
            renderSidebar();
            renderEditor();
        };

        const syncDeleteButtonState = () => {
            const hasSelection = selectedIndex >= 0 && !!templates[selectedIndex];
            deleteBtn.disabled = !hasSelection;
            deleteBtn.style.opacity = hasSelection ? '1' : '0.45';
            deleteBtn.style.cursor = hasSelection ? 'pointer' : 'not-allowed';
        };

        const showToast = (message) => {
            const existing = document.getElementById('gf-template-toast');
            if (existing) existing.remove();

            const toast = document.createElement('div');
            toast.id = 'gf-template-toast';
            toast.textContent = message;
            Object.assign(toast.style, {
                position: 'fixed',
                bottom: '28px',
                left: '50%',
                transform: 'translateX(-50%)',
                background: 'rgba(20, 20, 20, 0.92)',
                border: '1px solid rgba(255, 255, 255, 0.16)',
                color: '#f2f2f2',
                padding: '10px 14px',
                borderRadius: '999px',
                fontSize: '13px',
                boxShadow: '0 8px 24px rgba(0,0,0,0.35), 0 0 0 1px rgba(255,255,255,0.08)',
                zIndex: '1000000',
                transition: 'opacity 0.2s ease',
                opacity: '0',
                fontFamily: 'sans-serif',
                pointerEvents: 'none',
            });

            document.body.appendChild(toast);
            requestAnimationFrame(() => {
                toast.style.opacity = '1';
            });
            setTimeout(() => {
                toast.style.opacity = '0';
                setTimeout(() => toast.remove(), 250);
            }, 1600);
        };


        const findPromptElement = () => {
            const candidates = [
                'div[contenteditable="true"][role="textbox"]',
                'div[contenteditable="true"]',
                'textarea',
                'input[type="text"]',
            ];
            for (const selector of candidates) {
                const el = document.querySelector(selector);
                if (el) return el;
            }
            return null;
        };

        const findSlateEditor = (el) => {
            if (!el) return null;

            const fiberKey = Object.keys(el).find((k) => k.startsWith('__reactFiber'));
            if (!fiberKey) return null;

            let node = el[fiberKey];
            for (let i = 0; i < 60 && node; i++) {
                let s = node.memoizedState;
                while (s) {
                    const v = s.memoizedState;
                    if (
                        v && typeof v === 'object' && !Array.isArray(v) &&
                        typeof v.insertText === 'function' &&
                        v.children !== undefined
                    ) {
                        return v;
                    }
                    s = s.next;
                }
                node = node.return;
            }
            return null;
        };

        const insertToSlateEditor = (editor, text) => {
            if (!editor) return false;

            if (!editor.selection) {
                editor.selection = {
                    anchor: { path: [0, 0], offset: 0 },
                    focus: { path: [0, 0], offset: 0 },
                };
            }

            const firstNode = editor.children[0];
            const firstLeaf = firstNode && firstNode.children && firstNode.children[0];
            const existingLen = firstLeaf ? (firstLeaf.text || '').length : 0;

            if (existingLen > 0) {
                editor.selection = {
                    anchor: { path: [0, 0], offset: 0 },
                    focus: { path: [0, 0], offset: existingLen },
                };
                editor.deleteFragment();
            }

            editor.insertText(text);
            return true;
        };

        const insertTemplateText = (text) => {
            if (!text) return false;

            const promptEl = findPromptElement();
            if (!promptEl) {
                showToast('入力欄が見つかりませんでした');
                return false;
            }

            promptEl.focus();

            const slateEditor = findSlateEditor(promptEl);
            if (insertToSlateEditor(slateEditor, text)) {
                showToast('テンプレートを入力欄に挿入しました');
                closeDialog();
                return true;
            }

            if (promptEl.tagName === 'TEXTAREA' || promptEl.tagName === 'INPUT') {
                promptEl.value = text;
                promptEl.dispatchEvent(new Event('input', { bubbles: true }));
                showToast('テンプレートを入力欄に挿入しました');
                closeDialog();
                return true;
            }

            if (promptEl.isContentEditable) {
                try {
                    document.execCommand('selectAll', false, null);
                    document.execCommand('insertText', false, text);
                } catch (_) {
                    promptEl.textContent = text;
                }
                promptEl.dispatchEvent(new Event('input', { bubbles: true }));
                showToast('テンプレートを入力欄に挿入しました');
                closeDialog();
                return true;
            }

            showToast('入力欄への挿入に失敗しました');
            return false;
        };
        const onKeyDown = (e) => {
            if (e.key !== 'Escape' || e.repeat) return;
            if (overlay.style.display === 'none') return;
            e.preventDefault();
            e.stopImmediatePropagation();
            closeDialog();
        };

        function closeDialog() {
            overlay.style.display = 'none';
            document.removeEventListener('keydown', onKeyDown, true);
        }

        const renderSidebar = () => {
            sidebarList.innerHTML = '';
            templates.forEach((tpl, idx) => {
                const item = document.createElement('button');
                item.type = 'button';
                item.textContent = tpl.name ? tpl.name : '（無名テンプレート）';
                Object.assign(item.style, {
                    textAlign: 'left',
                    padding: '6px 8px',
                    borderRadius: '8px',
                    border: idx === selectedIndex ? '1px solid #4f8cff' : '1px solid #2c2c2c',
                    background: idx === selectedIndex ? '#1f2b3d' : '#161616',
                    cursor: 'grab',
                    fontSize: '12px',
                    color: '#eaeaea',
                });
                item.draggable = true;

                item.addEventListener('click', () => {
                    selectedIndex = idx;
                    renderEditor();
                    renderSidebar();
                });

                item.addEventListener('dblclick', () => {
                    insertTemplateText(tpl.text || '');
                });

                item.addEventListener('dragstart', (e) => {
                    draggingIndex = idx;
                    item.style.opacity = '0.6';
                    item.style.cursor = 'grabbing';
                    if (e.dataTransfer) {
                        e.dataTransfer.effectAllowed = 'move';
                        e.dataTransfer.setData('text/plain', String(idx));
                    }
                });

                item.addEventListener('dragover', (e) => {
                    if (draggingIndex < 0 || draggingIndex === idx) return;
                    e.preventDefault();
                    if (e.dataTransfer) e.dataTransfer.dropEffect = 'move';
                });

                item.addEventListener('dragenter', () => {
                    if (draggingIndex < 0 || draggingIndex === idx) return;
                    item.style.border = '1px dashed #4f8cff';
                });

                item.addEventListener('dragleave', () => {
                    item.style.border = idx === selectedIndex ? '1px solid #4f8cff' : '1px solid #2c2c2c';
                });

                item.addEventListener('drop', (e) => {
                    e.preventDefault();
                    const fromIndex = draggingIndex;
                    draggingIndex = -1;
                    reorderTemplates(fromIndex, idx);
                });

                item.addEventListener('dragend', () => {
                    draggingIndex = -1;
                    item.style.opacity = '1';
                    item.style.cursor = 'grab';
                    renderSidebar();
                });

                sidebarList.appendChild(item);
            });
            syncDeleteButtonState();
        };

        const renderEditor = () => {
            editor.innerHTML = '';
            if (selectedIndex < 0 || !templates[selectedIndex]) {
                editor.appendChild(emptyState);
                syncDeleteButtonState();
                return;
            }
            const current = templates[selectedIndex];
            nameInput.value = current.name || '';
            textArea.value = current.text || '';
            editor.appendChild(nameInput);
            editor.appendChild(textArea);
            syncDeleteButtonState();
        };

        nameInput.addEventListener('input', () => {
            if (selectedIndex < 0 || !templates[selectedIndex]) return;
            templates[selectedIndex].name = nameInput.value;
            saveTemplates(templates);
            renderSidebar();
        });

        textArea.addEventListener('input', () => {
            if (selectedIndex < 0 || !templates[selectedIndex]) return;
            templates[selectedIndex].text = textArea.value;
            saveTemplates(templates);
        });

        addBtn.addEventListener('click', () => {
            templates.push({ name: '', text: '' });
            selectedIndex = templates.length - 1;
            saveTemplates(templates);
            renderSidebar();
            renderEditor();
        });

        deleteBtn.addEventListener('click', () => {
            if (selectedIndex < 0 || !templates[selectedIndex]) return;
            
            const templateName = templates[selectedIndex].name || 'このテンプレート';
            const confirmMessage = `「${templateName}」を本当に削除しますか？\nこの操作は元に戻せません。`;
            
            if (!confirm(confirmMessage)) return;
            
            templates.splice(selectedIndex, 1);
            if (!templates.length) {
                selectedIndex = -1;
            } else if (selectedIndex >= templates.length) {
                selectedIndex = templates.length - 1;
            }
            saveTemplates(templates);
            renderSidebar();
            renderEditor();
        });

        panel.appendChild(title);
        panel.appendChild(body);
        layout.appendChild(sidebar);
        layout.appendChild(editor);
        panel.appendChild(layout);
        actions.appendChild(addBtn);
        actions.appendChild(deleteBtn);
        actions.appendChild(closeBtn);
        panel.appendChild(actions);
        overlay.appendChild(panel);
        overlay.addEventListener('click', (e) => {
            if (e.target === overlay) closeDialog();
        });

        document.body.appendChild(overlay);
        renderSidebar();
        renderEditor();
        overlay._refreshTemplates = () => {
            templates = loadTemplates();
            if (selectedIndex >= templates.length) {
                selectedIndex = templates.length ? 0 : -1;
            }
            renderSidebar();
            renderEditor();
        };
        overlay._onKeyDown = onKeyDown;
        return overlay;
    }

    function openAdd2Dialog() {
        const overlay = ensureAdd2Dialog();
        if (overlay._refreshTemplates) overlay._refreshTemplates();
        overlay.style.display = 'flex';
        if (overlay._onKeyDown) {
            document.addEventListener('keydown', overlay._onKeyDown, true);
        }
    }

    function ensureAdd2SiblingButton() {
        if (!isFlowPage()) return false;
        if (document.getElementById(ADD2_BUTTON_ID)) return true;

        const icon = findAdd2Icon();
        if (!icon) return false;

        const add2Button = icon.closest('button');
        if (!add2Button || !add2Button.parentElement) return false;

        const applyButtonStyles = (sourceBtn, targetBtn, sourceIcon, targetIcon) => {
            const btnStyle = getComputedStyle(sourceBtn);
            const iconStyle = sourceIcon ? getComputedStyle(sourceIcon) : null;

            const props = [
                'padding',
                'borderRadius',
                'border',
                'backgroundColor',
                'color',
                'boxShadow',
                'height',
                'width',
                'minWidth',
                'minHeight',
                'display',
                'alignItems',
                'justifyContent',
                'gap',
                'fontSize',
                'fontFamily',
                'lineHeight',
                'letterSpacing',
                'cursor',
                'transition',
                'boxSizing',
            ];

            for (const prop of props) {
                targetBtn.style[prop] = btnStyle[prop];
            }

            // アイコンサイズ・色を合わせる
            if (iconStyle && targetIcon) {
                targetIcon.style.fontSize = iconStyle.fontSize;
                targetIcon.style.color = iconStyle.color;
                targetIcon.style.fontVariationSettings = iconStyle.fontVariationSettings;
            }

            // 追加配置時のズレ防止
            targetBtn.style.margin = '0';
        };

        const btn = document.createElement('button');
        btn.id = ADD2_BUTTON_ID;
        btn.type = 'button';
        btn.setAttribute('aria-label', 'テンプレート');
        btn.title = 'テンプレート';
        const iconEl = document.createElement('i');
        iconEl.className = 'google-symbols';
        iconEl.textContent = 'view_quilt';
        btn.appendChild(iconEl);
        applyButtonStyles(add2Button, btn, icon, iconEl);

        btn.addEventListener('click', openAdd2Dialog);
        // add_2 の親 → その親の「2つめの div」の先頭に配置
        const parentDiv = add2Button.parentElement;
        const grandParent = parentDiv ? parentDiv.parentElement : null;
        const targetDiv = grandParent ? grandParent.querySelector(':scope > div:nth-child(2)') : null;
        if (targetDiv) {
            targetDiv.insertAdjacentElement('afterbegin', btn);
        } else {
            // フォールバック：add_2 の直後に配置
            add2Button.insertAdjacentElement('afterend', btn);
        }
        console.log('[Flow Override] add_2 sibling button created.');
        return true;
    }

    let add2ObserverStarted = false;
    function startAdd2Observer() {
        if (add2ObserverStarted) return;
        add2ObserverStarted = true;
        const observer = new MutationObserver(() => {
            if (!document.getElementById(ADD2_BUTTON_ID)) {
                ensureAdd2SiblingButton();
            }
        });
        observer.observe(document.body, { subtree: true, childList: true });
    }

    // 初期化関数
    function showDlOverlay() {
        if (document.getElementById('gf-dl-overlay')) return;
        const overlay = document.createElement('div');
        overlay.id = 'gf-dl-overlay';
        const spinner = document.createElement('div');
        spinner.className = 'gf-spinner';
        overlay.appendChild(spinner);
        document.body.appendChild(overlay);
    }

    function hideDlOverlay() {
        const overlay = document.getElementById('gf-dl-overlay');
        if (overlay) overlay.remove();
    }

    function triggerAutoDownloadIfNeeded() {
        if (!isFlowEditUrl()) return;
        const fromSessionStorage = !!sessionStorage.getItem('gf-auto-dl-back');
        const fromUrlParam = new URLSearchParams(location.search).get('gf-auto-dl') === '1';
        if (!fromSessionStorage && !fromUrlParam) return;
        if (fromSessionStorage) sessionStorage.removeItem('gf-auto-dl-back');

        // 編集画面でも即オーバーレイを表示
        showDlOverlay();

        const finishAndLeave = () => {
            hideDlOverlay();
            if (fromUrlParam) {
                // 背景タブで開かれた場合はタブを閉じる
                window.close();
            } else {
                const backBtn = [...document.querySelectorAll('button')]
                    .find(b => {
                        const span = b.querySelector('span');
                        return span && (span.innerText || span.textContent || '').trim() === '戻る';
                    });
                if (backBtn) {
                    backBtn.click();
                } else {
                    window.history.back();
                }
            }
        };

        let attempts = 0;
        const tryDownload = () => {
            const downloadBtn = [...document.querySelectorAll('button, [role="button"]')]
                .find(b => /ダウンロード/.test(b.innerText || b.textContent || ''));
            if (downloadBtn) {
                triggerDownload2K();
                // ダウンロード完了通知を待ってから離脱（最大5分）
                let waitAttempts = 0;
                const waitForDownloadComplete = () => {
                    const toasts = document.querySelectorAll('li[data-sonner-toast]');
                    for (const toast of toasts) {
                        if ((toast.innerText || toast.textContent || '').includes(TOAST_MESSAGE)) {
                            finishAndLeave();
                            return;
                        }
                    }
                    if (waitAttempts++ < 1000) { // 300ms × 1000 = 5分
                        setTimeout(waitForDownloadComplete, 300);
                    } else {
                        // タイムアウト：通知が来なくても離脱
                        finishAndLeave();
                    }
                };
                setTimeout(waitForDownloadComplete, 500);
            } else if (attempts++ < 20) {
                setTimeout(tryDownload, 300);
            } else {
                hideDlOverlay();
            }
        };
        setTimeout(tryDownload, 500);
    }

    function init() {
        refreshMenuCommands();
        ensureAdd2SiblingButton();
        startAdd2Observer();
        startBadgeObserver();
        applyDownloadBadges();
        triggerAutoDownloadIfNeeded();
    }

    // ─── 通知トーストの自動クローズ ──────────────────────
    const TOAST_MESSAGE = 'アップスケールが完了し、画像がダウンロードされました。';
    const TOAST_SCAN_INTERVAL_MS = 1000;

    function scanAndCloseToast() {
        const toasts = document.querySelectorAll('li[data-sonner-toast]');
        for (const toast of toasts) {
            const text = toast.innerText || toast.textContent || '';
            if (!text.includes(TOAST_MESSAGE)) continue;
            const closeBtn = toast.querySelector('button');
            if (closeBtn && /閉じる/.test(closeBtn.innerText || closeBtn.textContent || '')) {
                closeBtn.click();
            }
        }
    }

    // ─── SPA (Single Page Application) 画面遷移対策 ───
    // URLが変わったことを検知してボタンを再作成・表示制御する
    let lastUrl = location.href;
    new MutationObserver(() => {
        const url = location.href;
        if (url !== lastUrl) {
            lastUrl = url;
            console.log(`[Flow Override] URL changed to ${url}. Re-initializing UI...`);
            setTimeout(init, 500); // UI構築待ち
        }
    }).observe(document, { subtree: true, childList: true });

    // トースト監視（1秒ごとに対象通知を探して即閉じる）
    setInterval(scanAndCloseToast, TOAST_SCAN_INTERVAL_MS);

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        setTimeout(init, 500);
    }

    // 初回チェック
    scanAndCloseToast();

    console.log(`[Flow Override] v0.9 loaded. isMac=${isMac}`);
})();
