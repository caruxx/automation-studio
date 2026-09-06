"""SUNO の暗号化音声をページ内で復号し、Python へ分割転送する。

拡張機能 suno-fast-v0.10.0 の page-hook.js から移植。
参照元: ~/dev/suno-fast-ref/page-hook.js
"""

import base64


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
