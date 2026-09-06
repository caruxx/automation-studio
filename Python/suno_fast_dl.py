"""SUNO の暗号化音声をページ内で復号し、Python へ分割転送する。

拡張機能 suno-fast-v0.10.0 の page-hook.js から移植。
参照元: ~/dev/suno-fast-ref/page-hook.js
"""

import base64
import time
from typing import Callable, Dict, List, Optional


FAST_DECRYPT_HOOK: str = r"""
(function () {
  'use strict';
  if (window.__sunoFastDecryptCaptureInstalled) return;
  window.__sunoFastDecryptCaptureInstalled = true;

  const originalFetch = window.fetch.bind(window);
  const originalDecrypt = crypto.subtle.decrypt.bind(crypto.subtle);
  const mediaRequests = [];
  const progressBySession = Object.create(null);
  window.__sunoFastResults = Object.create(null);
  let captureEnabled = false;
  let captureContext = null;
  let keyExportAttempted = false;

  function setProgress(context, received, total) {
    if (!context?.sessionId) return;
    progressBySession[context.sessionId] = { received, total };
  }

  function setError(context, error) {
    if (!context?.sessionId) return;
    if (window.__sunoFastResults[context.sessionId]?.status === 'done') return;
    window.__sunoFastResults[context.sessionId] = {
      status: 'error',
      error: error?.message || String(error)
    };
  }

  function copyBytes(data) {
    if (data instanceof ArrayBuffer) return new Uint8Array(data.slice(0));
    if (ArrayBuffer.isView(data)) {
      return new Uint8Array(data.buffer.slice(data.byteOffset, data.byteOffset + data.byteLength));
    }
    return null;
  }

  function copyPrefix(data, maximum = 64) {
    let view;
    if (data instanceof ArrayBuffer) view = new Uint8Array(data);
    else if (ArrayBuffer.isView(data)) view = new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
    else return null;
    return view.slice(0, maximum);
  }

  function hasBox(bytes, type) {
    const target = Array.from(type, (character) => character.charCodeAt(0));
    for (let offset = 4; offset <= bytes.byteLength - 4; offset += 1) {
      if (target.every((value, index) => bytes[offset + index] === value)) return true;
    }
    return false;
  }

  function isInitSegment(bytes) {
    return Boolean(startsWithFtyp(bytes) && hasBox(bytes, 'moov'));
  }

  function startsWithFtyp(bytes) {
    return Boolean(bytes?.byteLength >= 8
      && bytes[4] === 0x66 && bytes[5] === 0x74 && bytes[6] === 0x79 && bytes[7] === 0x70);
  }

  function prefixesMatch(left, right) {
    const length = Math.min(left?.byteLength || 0, right?.byteLength || 0);
    if (length < 16) return false;
    for (let index = 0; index < length; index += 1) {
      if (left[index] !== right[index]) return false;
    }
    return true;
  }

  async function claimMatchingRequest(candidates, encryptedPrefix) {
    const remaining = [...candidates];
    while (remaining.length) {
      let candidate;
      try {
        candidate = await Promise.any(remaining.map(async (entry) => {
          const prefix = await entry.prefixPromise;
          if (!prefixesMatch(prefix, encryptedPrefix)) throw new Error('暗号文prefix不一致');
          return entry;
        }));
      } catch (_) {
        return null;
      }
      remaining.splice(remaining.indexOf(candidate), 1);
      // awaitの後に同期的にwinnerだけを予約する。別decryptが先に
      // claimしていた場合は、同じ暗号文を持つ残り候補へ進む。
      if (candidate.used || candidate.reserved || candidate.cancelled) continue;
      candidate.reserved = true;
      return candidate;
    }
    return null;
  }

  async function claimPrioritizedRequest(candidates, encryptedPrefix, context) {
    const sessionId = context?.sessionId;
    const sameSong = candidates.filter((candidate) => mediaUrlMatchesSong(candidate, context));
    const sameSession = candidates.filter((candidate) => !mediaUrlMatchesSong(candidate, context)
      && sessionId && candidate.context?.sessionId === sessionId);
    const others = candidates.filter((candidate) => !sameSong.includes(candidate)
      && !sameSession.includes(candidate));
    for (const group of [sameSong, sameSession, others]) {
      if (!group.length) continue;
      const request = await claimMatchingRequest(group, encryptedPrefix);
      if (request) return request;
    }
    return null;
  }

  function resolveRequestPrefix(request, bytes) {
    if (!request.resolvePrefix) return;
    request.resolvePrefix(bytes || new Uint8Array());
    request.resolvePrefix = null;
  }

  function isMediaUrl(url) {
    return /cloudfront\.net\/.*\/clip\/.*\.(?:m4a|mp4)(?:[?#]|$)/i.test(url)
      || /\/clip\/.*\.(?:m4a|mp4)(?:[?#]|$)/i.test(url);
  }

  function getClipIdFromMediaUrl(url) {
    const encoded = String(url || '').match(/\/clip\/([^/?#]+?)\.(?:m4a|mp4)(?:[?#]|$)/i)?.[1];
    if (!encoded) return '';
    try {
      return decodeURIComponent(encoded).toLowerCase();
    } catch (_) {
      return encoded.toLowerCase();
    }
  }

  function mediaUrlMatchesSong(request, context) {
    const songId = String(context?.songId || '').toLowerCase();
    return Boolean(songId && getClipIdFromMediaUrl(request?.url) === songId);
  }

  function removeRequest(request) {
    const index = mediaRequests.indexOf(request);
    if (index >= 0) mediaRequests.splice(index, 1);
  }

  function cancelRequest(request) {
    if (!request || request.cancelled) return;
    request.cancelled = true;
    resolveRequestPrefix(request, new Uint8Array());
    try {
      const cancellation = request.reader?.cancel();
      cancellation?.catch?.(() => {});
    } catch (_) {
      // Readerが既に閉じていても後始末は続行する。
    }
    request.encryptedPromise = null;
  }

  function cancelRequests(predicate) {
    for (const request of [...mediaRequests]) {
      if (!predicate(request)) continue;
      cancelRequest(request);
      removeRequest(request);
    }
  }

  function trimRequests() {
    const cutoff = Date.now() - 60000;
    cancelRequests((request) => request.cancelled || (!request.used
      && Math.max(request.createdAt, request.lastProgressAt || 0) < cutoff));
    while (mediaRequests.length > 16) {
      const activeSessionId = captureContext?.sessionId;
      let index = mediaRequests.findIndex((request) => !request.used
        && request.context?.sessionId !== activeSessionId);
      if (index < 0) index = mediaRequests.findIndex((request) => !request.used);
      if (index < 0) break;
      const [request] = mediaRequests.splice(index, 1);
      cancelRequest(request);
    }
  }

  async function readEncryptedBody(response, request) {
    const reader = response.body?.getReader();
    if (!reader) {
      try {
        const bytes = new Uint8Array(await response.arrayBuffer());
        if (request.cancelled) throw new Error('取得対象がキャンセルされました');
        resolveRequestPrefix(request, bytes.slice(0, 64));
        setProgress(request.context, bytes.byteLength, request.total || bytes.byteLength);
        return bytes;
      } finally {
        resolveRequestPrefix(request, new Uint8Array());
      }
    }
    request.reader = reader;
    const chunks = [];
    const prefix = new Uint8Array(64);
    let prefixBytes = 0;
    let received = 0;
    try {
      for (;;) {
        if (request.cancelled) throw new Error('取得対象がキャンセルされました');
        const { done, value } = await reader.read();
        if (done) break;
        if (!value?.byteLength) continue;
        if (prefixBytes < prefix.byteLength) {
          const copyLength = Math.min(value.byteLength, prefix.byteLength - prefixBytes);
          prefix.set(value.subarray(0, copyLength), prefixBytes);
          prefixBytes += copyLength;
          if (prefixBytes === prefix.byteLength) resolveRequestPrefix(request, prefix);
        }
        chunks.push(value);
        received += value.byteLength;
        const now = Date.now();
        if (now - request.lastProgressAt >= 150) {
          request.lastProgressAt = now;
          setProgress(request.context, received, request.total || 0);
        }
      }
      if (request.cancelled) throw new Error('取得対象がキャンセルされました');
    } finally {
      resolveRequestPrefix(request, prefix.slice(0, prefixBytes));
      request.reader = null;
      try {
        reader.releaseLock();
      } catch (_) {
        // ロック解放済みの場合は何もしない。
      }
    }
    const combined = new Uint8Array(received);
    let offset = 0;
    for (const chunk of chunks) {
      combined.set(chunk, offset);
      offset += chunk.byteLength;
    }
    setProgress(request.context, received, request.total || received);
    return combined;
  }

  window.fetch = async function (...args) {
    const contextAtStart = captureContext ? { ...captureContext } : null;
    const response = await originalFetch(...args);
    const url = String(response.url || args[0]?.url || args[0] || '');
    if (!captureEnabled || !response.ok || !isMediaUrl(url)) return response;
    const contentRange = String(response.headers.get('content-range') || '');
    const rangeMatch = contentRange.match(/^bytes\s+(\d+)-(\d+)\/(\d+|\*)$/i);
    const partialRange = response.status === 206 && (!rangeMatch
      || Number(rangeMatch[1]) !== 0
      || rangeMatch[3] === '*'
      || Number(rangeMatch[2]) + 1 < Number(rangeMatch[3]));
    if (partialRange) return response;
    const request = {
      url,
      context: contextAtStart,
      createdAt: Date.now(),
      total: Number(response.headers.get('content-length')) || 0,
      lastProgressAt: 0,
      used: false,
      reserved: false,
      cancelled: false,
      reader: null,
      resolvePrefix: null,
      prefixPromise: null
    };
    try {
      request.prefixPromise = new Promise((resolve) => { request.resolvePrefix = resolve; });
      request.encryptedPromise = readEncryptedBody(response.clone(), request);
      // decryptに紐付いた読み取り失敗だけを状態へ残す。
      void request.encryptedPromise.catch((error) => {
        if (request.used && !request.cancelled) setError(request.context, error);
      });
      mediaRequests.push(request);
      trimRequests();
    } catch (error) {
      cancelRequest(request);
      removeRequest(request);
      setError(request.context, `レスポンス複製失敗: ${error.message}`);
    }
    return response;
  };

  crypto.subtle.decrypt = async function (algorithm, key, data) {
    const isCaptureTarget = captureEnabled && String(algorithm?.name || '').toUpperCase() === 'AES-CTR';
    const decryptContext = isCaptureTarget && captureContext ? { ...captureContext } : null;
    const counter = isCaptureTarget ? copyBytes(algorithm.counter) : null;
    const encryptedPrefix = isCaptureTarget ? copyPrefix(data) : null;
    const result = await originalDecrypt(algorithm, key, data);
    if (!isCaptureTarget || !captureEnabled) return result;
    const plain = new Uint8Array(result);
    if (!startsWithFtyp(plain)) return result;
    // 調査用に一度だけ試す。鍵の内容は保持せず、復号経路も変えない。
    if (!keyExportAttempted) {
      keyExportAttempted = true;
      void (async () => {
        try {
          await crypto.subtle.exportKey('raw', key);
          window.__sunoFastKeyExportable = true;
        } catch (_) {
          window.__sunoFastKeyExportable = false;
        }
      })();
    }
    trimRequests();
    const candidates = [...mediaRequests].reverse()
      .filter((candidate) => !candidate.used && !candidate.reserved && !candidate.cancelled);
    void (async () => {
      let request = null;
      try {
        if (!counter?.byteLength || !encryptedPrefix?.byteLength) {
          throw new Error('復号元の先頭データを取得できません');
        }
        request = await claimPrioritizedRequest(candidates, encryptedPrefix, decryptContext);
        if (!request) return;
        if (request.used || request.cancelled) return;
        request.used = true;
        // prefetchが旧contextで始まっても、URLのclip IDと現在の対象song IDが
        // 厳密一致する場合だけdecrypt時contextを正本にする。
        if (mediaUrlMatchesSong(request, decryptContext)) request.context = decryptContext;
        else if (!request.context && decryptContext) request.context = decryptContext;
        if (request.cancelled) return;
        const encrypted = await request.encryptedPromise;
        if (request.cancelled) return;
        setProgress(request.context, encrypted.byteLength, request.total || encrypted.byteLength);
        const decryptedBuffer = await originalDecrypt({
          name: 'AES-CTR',
          counter,
          length: Number(algorithm.length) || 128
        }, key, encrypted);
        const decrypted = new Uint8Array(decryptedBuffer);
        if (!isInitSegment(decrypted)) throw new Error('復号結果が音声MP4形式ではありません');
        if (request.cancelled) return;
        if (request.context?.sessionId) {
          window.__sunoFastResults[request.context.sessionId] = {
            status: 'done',
            bytes: decrypted,
            size: decrypted.byteLength,
            url: request.url
          };
        }
        request.encryptedPromise = null;
      } catch (error) {
        if (request?.cancelled) return;
        setError(request?.context || decryptContext, error);
      } finally {
        if (request) {
          if (request.reader) cancelRequest(request);
          request.encryptedPromise = null;
          removeRequest(request);
        }
      }
    })();
    return result;
  };

  window.__sunoFastSetContext = function (context) {
    captureContext = context ? { ...context } : null;
    captureEnabled = Boolean(captureContext);
  };

  window.__sunoFastGetState = function (sessionId) {
    const result = window.__sunoFastResults[sessionId];
    const progress = progressBySession[sessionId];
    return {
      status: result?.status || 'pending',
      received: progress?.received || 0,
      total: progress?.total || 0,
      size: result?.size || 0,
      error: result?.error || '',
      clipId: getClipIdFromMediaUrl(result?.url)
    };
  };

  window.__sunoFastSendResult = async function (sessionId) {
    const result = window.__sunoFastResults[sessionId];
    if (result?.status !== 'done') {
      throw new Error(result?.error || '復号結果がまだありません');
    }
    if (!(result.bytes instanceof Uint8Array)) {
      throw new Error('復号結果がUint8Arrayではありません');
    }
    const bytes = result.bytes;
    const size = result.size;
    const chunkSize = 1048576;
    // 空データでも宣言サイズを検証できるよう、1個の空chunkを送る。
    const total = Math.max(1, Math.ceil(bytes.byteLength / chunkSize));
    for (let index = 0; index < total; index += 1) {
      const chunk = bytes.subarray(index * chunkSize, (index + 1) * chunkSize);
      const parts = [];
      for (let offset = 0; offset < chunk.byteLength; offset += 8192) {
        parts.push(String.fromCharCode(...chunk.subarray(offset, offset + 8192)));
      }
      const base64 = btoa(parts.join(''));
      await window.__sunoFastChunk({ sessionId, index, base64, total, size });
    }
    await window.__sunoFastChunk({ sessionId, index: -1, done: true });
  };

  window.__sunoFastClear = function (sessionId) {
    const currentContext = captureContext;
    const songId = currentContext?.sessionId === sessionId ? currentContext?.songId : '';
    cancelRequests((request) => request.context?.sessionId === sessionId
      || mediaUrlMatchesSong(request, { songId }));
    delete window.__sunoFastResults[sessionId];
    delete progressBySession[sessionId];
    if (currentContext?.sessionId === sessionId) {
      captureContext = null;
      captureEnabled = false;
    }
  };
})();
"""


def install_fast_capture(page) -> None:
    """次回以降のナビゲーションで復号フックを注入する。"""
    page.add_init_script(FAST_DECRYPT_HOOK)


def ensure_fast_capture(page) -> bool:
    """現ページの注入を確認し、未注入なら後注入する。"""
    try:
        # add_init_script 経由で注入済みか確認（既存タブの場合は後注入）。
        installed = page.evaluate("() => !!window.__sunoFastDecryptCaptureInstalled")
        if not installed:
            print(" 復号フック未インストール。現ページに後注入します（既存トラフィックの一部は取り逃す可能性）")
            page.evaluate(FAST_DECRYPT_HOOK)
            installed = page.evaluate("() => !!window.__sunoFastDecryptCaptureInstalled")
        return bool(installed)
    except Exception as e:
        print(f" 復号フックの注入確認に失敗: {e}")
        return False


def register_transfer_binding(page) -> None:
    """ページごとに一度だけ、sessionId 別の分割転送受信を登録する。"""
    if hasattr(page, "_suno_fast_transfers"):
        return
    transfers = {}

    def handler(source, payload):
        if not isinstance(payload, dict):
            raise ValueError("転送メッセージが辞書ではありません")
        session_id = payload.get("sessionId")
        if not isinstance(session_id, str) or session_id not in transfers:
            raise ValueError("受信待ちでないsessionIdです")
        state = transfers[session_id]
        try:
            if state["error"] is not None:
                raise ValueError(state["error"])
            if state["result"] is not None:
                raise ValueError("完了後にchunkを受信しました")
            index = payload.get("index")
            if type(index) is not int:
                raise ValueError("chunkのindexが整数ではありません")
            if payload.get("done") is True:
                if index != -1:
                    raise ValueError("完了通知のindexが-1ではありません")
                total = state["total"]
                chunks = state["chunks"]
                if total is None or len(chunks) != total:
                    raise ValueError("転送chunkが不足しています")
                combined = b"".join(chunks[i] for i in range(total))
                if len(combined) != state["size"]:
                    raise ValueError(
                        f"転送サイズ不一致: size={state['size']} received={len(combined)}"
                    )
                state["result"] = combined
                chunks.clear()
                return
            total = payload.get("total")
            size = payload.get("size")
            if type(total) is not int or total < 1:
                raise ValueError("chunkのtotalが正の整数ではありません")
            if type(size) is not int or size < 0:
                raise ValueError("宣言サイズが非負の整数ではありません")
            if not 0 <= index < total or index in state["chunks"]:
                raise ValueError("chunkのindexが範囲外または重複しています")
            if state["total"] is None:
                state["total"], state["size"] = total, size
            elif (state["total"], state["size"]) != (total, size):
                raise ValueError("転送中にtotalまたはsizeが変わりました")
            encoded = payload.get("base64")
            if not isinstance(encoded, str):
                raise ValueError("chunkのbase64が文字列ではありません")
            chunk = base64.b64decode(encoded, validate=True)
            if len(chunk) > 1048576:
                raise ValueError("chunkが1048576バイトを超えています")
            state["chunks"][index] = chunk
        except Exception as e:
            # JS側が例外を捕捉しても、失敗した転送を成功扱いしない。
            state["error"] = str(e)
            raise

    page.expose_binding("__sunoFastChunk", handler)
    page._suno_fast_transfers = transfers


def fetch_result_bytes(page, session_id) -> bytes:
    """Python から転送を開始し、完了・サイズ検証済みのバイト列を返す。"""
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session_idには空でない文字列が必要です")
    register_transfer_binding(page)
    transfers = page._suno_fast_transfers
    if session_id in transfers:
        raise RuntimeError("同じsession_idの転送が既に進行中です")
    state = {"chunks": {}, "total": None, "size": None, "result": None, "error": None}
    transfers[session_id] = state
    try:
        # async関数のPromiseをevaluateが待つ。戻り値に音声本体は含めない。
        page.evaluate("sessionId => window.__sunoFastSendResult(sessionId)", session_id)
        if state["error"] is not None:
            raise RuntimeError(state["error"])
        if state["result"] is None:
            raise RuntimeError("転送の完了通知を受信していません")
        return state["result"]
    finally:
        # 再取得時に前回のchunkを混ぜず、成功・失敗のどちらでも解放する。
        transfers.pop(session_id, None)


# content.js の rowMetadata / ページ巡回を、復号フックとは独立して使用する。
_WORKSPACE_ROWS_DOM = r"""
(action) => {
  const rowSelector = '[data-testid="clip-row"]';
  const visible = (element) => element.getClientRects().length > 0;
  const rowNodes = [...document.querySelectorAll(rowSelector)].filter(visible)
    .sort((left, right) => left.getBoundingClientRect().top - right.getBoundingClientRect().top);
  const rows = rowNodes.map((row) => {
    const links = [...row.querySelectorAll('a[href*="/song/"]')];
    const uuidPattern = /\/song\/([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})(?:[/?#]|$)/i;
    const link = links.find((candidate) => uuidPattern.test(candidate.getAttribute('href') || ''));
    const titleLink = links.find((candidate) => String(candidate.textContent || '').trim());
    const title = String(titleLink?.textContent || '').trim()
      || String(row.getAttribute('aria-label') || '').trim() || 'suno-audio';
    return {
      song_id: (link?.getAttribute('href')?.match(uuidPattern)?.[1] || '').toLowerCase(),
      title: title.replace(/[<>:"/\\|?*\u0000-\u001F]/g, '_')
        .replace(/[. ]+$/g, '').trim().slice(0, 150) || 'suno-audio'
    };
  }).filter((row) => row.song_id);
  const pageControl = [...document.querySelectorAll(
    'input[aria-label="Current page number" i], input[aria-label="現在のページ番号"], '
    + '[role="spinbutton"][aria-label="Current page number" i]'
  )].find(visible);
  const pageMatch = String(pageControl?.value || pageControl?.getAttribute('aria-valuenow') || '').match(/\d+/);
  const pageNumber = pageMatch ? Number(pageMatch[0]) : null;
  const button = (direction) => [...document.querySelectorAll('button, [role="button"]')].find((element) => {
    const pattern = direction === 'next' ? /^(next page|次のページ)$/i : /^(previous page|前のページ)$/i;
    return visible(element) && [element.getAttribute('aria-label'), element.textContent]
        .some((label) => pattern.test(String(label || '').trim()));
  });
  const enabled = (element) => Boolean(element) && !element.disabled
    && element.getAttribute('aria-disabled') !== 'true';
  if (action === 'next' || action === 'previous') {
    const target = button(action);
    if (!enabled(target)) return false;
    target.click();
    return true;
  }
  let node = rowNodes[0];
  let fallback = null;
  let scroller = null;
  while (node && node !== document.body && node !== document.documentElement) {
    if (node.scrollHeight > node.clientHeight + 2 && node.clientHeight > 100) {
      if (!fallback) fallback = node;
      if (/auto|scroll|overlay/i.test(getComputedStyle(node).overflowY)) {
        scroller = node;
        break;
      }
    }
    node = node.parentElement;
  }
  scroller = scroller || fallback || document.scrollingElement || document.documentElement;
  const maximum = Math.max(0, scroller.scrollHeight - scroller.clientHeight);
  if (action === 'top') scroller.scrollTop = 0;
  if (action === 'scroll') {
    // ビューポートを重ねながら進め、仮想行の境界で取りこぼさない。
    scroller.scrollTop = Math.min(maximum, scroller.scrollTop + Math.max(1, Math.floor(scroller.clientHeight * 0.72)));
  }
  const leaves = [...document.querySelectorAll('div, span, p, h1, h2, h3')]
    .filter((element) => !element.children.length && visible(element)
      && !element.closest('[role="dialog"], [role="listbox"], [role="menu"]'));
  const noSongs = !rows.length && leaves.some((element) => {
    if (!/^(no songs found|曲が見つかりません)$/i.test(element.textContent.trim())) return false;
    if (!pageControl) return Boolean(element.closest('main, [role="main"], table, [role="table"]'));
    let ancestor = element.parentElement;
    while (ancestor && ancestor !== document.body && ancestor !== document.documentElement) {
      if (ancestor.contains(pageControl)) return true;
      ancestor = ancestor.parentElement;
    }
    return false;
  });
  const counts = leaves.map((element) => {
    const match = element.textContent.trim().match(/^([\d,]+)\s+songs?$/i);
    return match ? { element, count: Number(match[1].replace(/,/g, '')) } : null;
  }).filter(Boolean);
  let expected = null;
  if (pageControl) {
    for (const candidate of counts) {
      let ancestor = candidate.element.parentElement;
      for (let depth = 0; ancestor && depth < 8; depth += 1, ancestor = ancestor.parentElement) {
        if (ancestor.contains(pageControl) && ancestor.querySelector(rowSelector)) {
          expected = candidate.count;
          break;
        }
      }
      if (expected !== null) break;
    }
  }
  if (expected === null && counts.length === 1) expected = counts[0].count;
  const loading = [...document.querySelectorAll('[aria-busy="true"], [role="progressbar"], [data-testid*="loading"], [data-testid*="spinner"]')].some(visible);
  const rect = scroller.getBoundingClientRect();
  const rowRects = rowNodes.map((row) => row.getBoundingClientRect());
  // 全行がDOMにある通常リストなら、スクロールでID列が変わらなくてもよい。
  const rowsCoverScroll = rowRects.length > 0
    && Math.min(...rowRects.map((r) => r.top)) <= rect.top - scroller.scrollTop + 3
    && Math.max(...rowRects.map((r) => r.bottom)) >= rect.top - scroller.scrollTop + scroller.scrollHeight - 3;
  return {
    loading, rows_cover_scroll: rowsCoverScroll,
    rows_cover_viewport: rowRects.length > 0
      && Math.min(...rowRects.map((r) => r.top)) <= rect.top + 3
      && Math.max(...rowRects.map((r) => r.bottom)) >= Math.min(rect.bottom, rect.top + scroller.scrollHeight - scroller.scrollTop) - 3,
    next_present: Boolean(button('next')), previous_present: Boolean(button('previous')),
    rows, page_no: pageNumber, expected, no_songs: noSongs,
    previous: enabled(button('previous')), next: enabled(button('next')),
    top: Math.round(scroller.scrollTop), client: scroller.clientHeight,
    height: scroller.scrollHeight, maximum
  };
}
"""


def _workspace_row_ids(state: Dict) -> tuple:
    return tuple(row["song_id"] for row in state["rows"])


def _wait_workspace_rows_stable(page, previous_ids: Optional[tuple] = None,
                                timeout: float = 15.0, target_page: Optional[int] = None) -> Dict:
    """行とスクロール寸法の署名が2回連続で一致するまで待つ。"""
    started = time.monotonic()
    last_signature = None
    stable_polls = 0
    while time.monotonic() - started < timeout:
        state = page.evaluate(_WORKSPACE_ROWS_DOM, "read")
        ids = _workspace_row_ids(state)
        signature = (ids, tuple(row["title"] for row in state["rows"]),
                     state["page_no"], state["top"], state["client"], state["height"],
                     state["no_songs"], state["loading"], state["next"],
                     state["next_present"], state["expected"])
        # ページ番号だけが先に変わっても旧曲行を次ページの行と判定しない。
        ready = not state["loading"] and (
            (state["no_songs"] and previous_ids is None and target_page is None)
            or (ids and (previous_ids is None or ids != previous_ids)))
        if target_page is not None:
            ready = ready and state["page_no"] == target_page
        if ready and signature == last_signature:
            stable_polls += 1
        else:
            stable_polls = 0
        last_signature = signature
        if stable_polls >= 2 and time.monotonic() - started >= 0.54:
            return state
        page.wait_for_timeout(180)
    raise RuntimeError("曲行の安定またはページ遷移を確認できませんでした")


def _scroll_workspace_rows(page, action: str) -> Dict:
    before = page.evaluate(_WORKSPACE_ROWS_DOM, "read")
    moved = page.evaluate(_WORKSPACE_ROWS_DOM, action)
    require_new = (moved["top"] != before["top"] and not moved["rows_cover_scroll"]
                   and not moved["rows_cover_viewport"])
    return _wait_workspace_rows_stable(
        page, previous_ids=_workspace_row_ids(before) if require_new else None, timeout=8.0)


def _move_workspace_page(page, direction: str) -> Dict:
    """ページボタンは1回だけ押し、遅い遷移でもページを飛ばさない。"""
    # Next がスクロールだけ先に先頭へ戻しても、旧ページの別の仮想行を
    # 新ページと誤認しないよう、比較元も先頭で安定させる。
    state = _scroll_workspace_rows(page, "top")
    if not page.evaluate(_WORKSPACE_ROWS_DOM, direction):
        raise RuntimeError(f"ページボタンが操作前に消えました: {direction}")
    target = None if state["page_no"] is None else state["page_no"] + (1 if direction == "next" else -1)
    return _wait_workspace_rows_stable(page, previous_ids=_workspace_row_ids(state), target_page=target)


def iter_workspace_rows(page, status_cb: Optional[Callable[[str], None]] = None,
                        expected_count: Optional[int] = None) -> List[Dict]:
    """開いている Workspace を先頭から全ページ走査し、重複のない曲行を返す。

    同期 Playwright Page を受け取る。戻り値はページ順・行順で、page_no は1始まり。
    Workspace の移動、再生、復号、ダウンロードは行わない。
    expected_count にカード等で確認した総曲数を渡せる。総数不明時は
    読み込み終了と明示的な無効Nextが必要（初期の明示的空表示を除く）。
    読み込み失敗・巡回上限・画面総曲数との不一致は部分成功にせず例外にする。
    """
    if expected_count is not None and (
            isinstance(expected_count, bool) or not isinstance(expected_count, int) or expected_count < 0):
        raise ValueError("expected_count は0以上の整数にしてください")
    state = _wait_workspace_rows_stable(page)
    for _ in range(200):
        if not state["previous"]:
            if state["page_no"] is not None and state["page_no"] > 1:
                raise RuntimeError("先頭ページへ戻るボタンがありません")
            break
        state = _move_workspace_page(page, "previous")
    else:
        raise RuntimeError("先頭ページへの移動回数が上限に達しました")

    expected = expected_count

    def update_expected(current):
        nonlocal expected
        observed = current["expected"]
        if observed is not None:
            if expected is not None and expected != observed:
                raise RuntimeError(f"総曲数が変化または指定件数と不一致です: expected={expected} observed={observed}")
            expected = observed

    update_expected(state)
    result = []
    seen = set()
    visited_pages = set()
    for ordinal in range(1, 201):
        state = _scroll_workspace_rows(page, "top")
        update_expected(state)
        if state["no_songs"]:
            if result or state["next"] or (expected is not None and expected != 0):
                raise RuntimeError("全曲取得を確認する前に空表示になりました")
            break
        page_no = state["page_no"] or ordinal
        page_key = ("page", page_no) if state["page_no"] else ("ids", _workspace_row_ids(state))
        if page_key in visited_pages:
            raise RuntimeError(f"同じページを再訪しました: page={page_no}")
        visited_pages.add(page_key)
        before_count = len(result)
        bottom_signature = None
        bottom_stable = 0
        for _ in range(240):
            update_expected(state)
            if state["no_songs"] or (state["page_no"] is not None and state["page_no"] != page_no):
                raise RuntimeError("スクロール中に曲一覧のページが変わりました")
            found = 0
            for row in state["rows"]:
                if row["song_id"] not in seen:
                    seen.add(row["song_id"])
                    result.append(dict(row, page_no=page_no))
                    found += 1
            at_bottom = state["top"] >= state["maximum"] - 3
            signature = (_workspace_row_ids(state), state["top"], state["height"])
            if at_bottom:
                bottom_stable = bottom_stable + 1 if not found and signature == bottom_signature else 0
                bottom_signature = signature
                if bottom_stable >= 2:
                    break
            else:
                bottom_stable = 0
            state = _scroll_workspace_rows(page, "scroll")
        else:
            raise RuntimeError(f"仮想スクロールの走査回数が上限に達しました: page={page_no}")
        if status_cb:
            status_cb(f"page={page_no} rows={len(result) - before_count} total={len(result)} expected={expected}")
        if expected is not None and len(result) == expected:
            break
        # ボタンの消失を最終ページとみなさない。無効Nextが継続し、
        # 読み込み表示が消えたことを確認する。総数不明の単一ページは拒否する。
        end_signature = (_workspace_row_ids(state), state["top"], state["height"])
        deadline = time.monotonic() + 8.0
        terminal_since = None
        while time.monotonic() < deadline:
            state = _wait_workspace_rows_stable(page, timeout=8.0)
            update_expected(state)
            if end_signature != (_workspace_row_ids(state), state["top"], state["height"]):
                raise RuntimeError("終端確認中に曲一覧が更新されました。再走査が必要です")
            if state["next"]:
                break
            terminal = state["next_present"] and not state["loading"]
            if terminal:
                terminal_since = terminal_since or time.monotonic()
                if time.monotonic() - terminal_since >= 1.5:
                    break
            else:
                terminal_since = None
        else:
            raise RuntimeError("一覧の終端を確認できません。総曲数または明示的なNext無効状態が必要です")
        if not state["next"]:
            break
        state = _move_workspace_page(page, "next")
    else:
        raise RuntimeError("ページ巡回が上限に達しました")
    if expected is not None and len(result) != expected:
        raise RuntimeError(f"画面総曲数と取得件数が一致しません: expected={expected} rows={len(result)}")
    return result
