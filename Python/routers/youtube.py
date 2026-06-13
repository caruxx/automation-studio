#!/usr/bin/env python3
"""YouTube ルータ（D9）。説明文生成/通知(Discord・LINE)/YouTube API・upload・batch-upload・status/アップロード履歴。共有ヘルパ(UploadRequest/_build_youtube_upload_command/get_yt_upload_defaults/registry系)は app_core から取り込み。重い処理はハンドラ内ローカルimport。"""
from fastapi import APIRouter
from app_core import *  # noqa: F401,F403  土台シンボル(stdlib/fastapi/foundation)を全取り込み

router = APIRouter()


# ─── API: YouTube 説明文生成 ───

class DescriptionGenerateRequest(BaseModel):
    video_folder: str
    style_reference: Optional[str] = None  # 参考にする過去の説明文

@router.get("/api/youtube-desc/references")
def api_desc_references():
    """過去の説明文を参考用にリストアップ"""
    config = get_dashboard_config()
    channel_dir = Path(config["channel_folder"])
    refs = []
    if channel_dir.exists():
        for d in sorted(channel_dir.iterdir(), reverse=True):
            desc_file = d / "youtube_description.txt"
            if desc_file.exists():
                m = re.match(r'^(\d+)_', d.name)
                num = m.group(1) if m else "?"
                text = desc_file.read_text(encoding="utf-8").strip()
                refs.append({
                    "num": num,
                    "name": d.name,
                    "text": text[:200] + "..." if len(text) > 200 else text,
                    "full_text": text,
                })
                if len(refs) >= 10:
                    break
    return {"references": refs}


@router.get("/api/youtube-desc/video-info/{video_name}")
def api_desc_video_info(video_name: str):
    """動画フォルダの情報を取得（サムネイル、タイムコード、曲リスト）"""
    config = get_dashboard_config()
    channel_dir = Path(config["channel_folder"])
    folder = channel_dir / video_name
    if not folder.exists():
        raise HTTPException(404, "フォルダが見つかりません")

    m = re.match(r'^(\d+)_', video_name)
    num = m.group(1) if m else "00"

    # タイムコード読み込み（対応ファイルから LOOP 直前まで）
    timecodes, tc_file = _read_matching_timecodes_until_loop(folder, num)

    # サムネイル情報
    thumb = None
    for pattern in ["サムネイル.jpg", "サムネイル.png", "thumbnail.jpg", "thumbnail.png", f"vol{num}.jpg", f"vol{num}.png"]:
        t = folder / pattern
        if t.exists():
            thumb = {"name": t.name, "path": str(t)}
            break

    # 曲リスト（LOOPまで）
    songs = []
    for line in timecodes.split("\n"):
        if "LOOP" in line:
            break
        if " - " in line:
            songs.append(line.strip())

    return {
        "num": num,
        "name": video_name,
        "timecodes": timecodes,
        "timecode_file": tc_file.name if tc_file else "",
        "songs": songs,
        "song_count": len(songs),
        "thumbnail": thumb,
    }

@router.get("/api/youtube-desc/thumbnail/{video_name}")
def api_desc_thumbnail(video_name: str):
    """サムネイル画像を返す"""
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / video_name
    m = re.match(r'^(\d+)_', video_name)
    num = m.group(1) if m else "00"
    for pattern in ["サムネイル.jpg", "サムネイル.png", "thumbnail.jpg", "thumbnail.png", f"vol{num}.jpg", f"vol{num}.png"]:
        t = folder / pattern
        if t.exists():
            return FileResponse(str(t))
    raise HTTPException(404, "サムネイルが見つかりません")

class DescriptionSaveRequest(BaseModel):
    video_name: str
    text: str

@router.post("/api/youtube-desc/save")
def api_desc_save(req: DescriptionSaveRequest):
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / req.video_name
    if not folder.exists():
        raise HTTPException(404, "フォルダが見つかりません")
    desc_file = folder / "youtube_description.txt"
    desc_file.write_text(req.text, encoding="utf-8")
    return {"status": "ok"}

# ─── API: 通知 ───
class NotifyRequest(BaseModel):
    message: str

@router.post("/api/notify/discord")
def api_notify_discord(req: NotifyRequest):
    try:
        result = subprocess.run(["bash", str(NOTIFY_SCRIPT), req.message], capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            raise HTTPException(500, result.stderr.strip() or result.stdout.strip() or "Discord通知に失敗しました")
        return {"status": "ok", "output": result.stdout}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))

@router.post("/api/notify/line")
def api_notify_line_compat(req: NotifyRequest):
    """Backward-compatible alias. Notifications now go to Discord."""
    return api_notify_discord(req)

# ─── API: Discord Webhook 設定 ───
# 通知先は全 PC・全チャンネル共通のため共有ドライブ config/ に保存（app_core.DISCORD_CONFIG）。
# app_notify.sh も同じ共有パス（スクリプト位置基準 ../config/）を読む。
_DISCORD_CONFIG_FILE = DISCORD_CONFIG

class DiscordConfigUpdate(BaseModel):
    webhook_url: str = ""
    username: str = ""

def _read_discord_config() -> dict:
    try:
        return json.loads(_DISCORD_CONFIG_FILE.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}

@router.get("/api/notify/discord/config")
def api_notify_discord_config():
    cfg = _read_discord_config()
    url = (cfg.get("webhook_url") or "").strip()
    configured = url.startswith("https://discord.com/api/webhooks/") or url.startswith("https://discordapp.com/api/webhooks/")
    preview = (url[:45] + "…" + url[-4:]) if configured and len(url) > 55 else ("" if not configured else url)
    return {
        "configured": configured,
        "preview": preview,
        "username": cfg.get("username") or "Automation Studio",
    }

@router.put("/api/notify/discord/config")
def api_notify_discord_config_save(req: DiscordConfigUpdate):
    url = (req.webhook_url or "").strip()
    if url and not (url.startswith("https://discord.com/api/webhooks/") or url.startswith("https://discordapp.com/api/webhooks/")):
        raise HTTPException(400, "Discord Webhook URL の形式ではありません（https://discord.com/api/webhooks/… を貼り付けてください）")
    cfg = _read_discord_config()
    if url:
        cfg["webhook_url"] = url
    elif "webhook_url" in cfg and not url:
        # 空文字での保存は「削除」扱い
        cfg["webhook_url"] = ""
    if req.username.strip():
        cfg["username"] = req.username.strip()
    cfg.setdefault("username", "Automation Studio")
    _DISCORD_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    _DISCORD_CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        _DISCORD_CONFIG_FILE.chmod(0o600)
    except Exception:
        pass
    return api_notify_discord_config()

# ─── API: YouTube ───

# YouTube ローカリゼーション言語マスタ（BCP47 + 表示名 + ネイティブ + 国旗）
# BGM チャンネル運用で実用価値の高い 24 言語を厳選。
# UI ではこの順序でグリッド表示、JS 側もこのマスタを GET API で取得して同期。
YT_LANGUAGE_CATALOG = [
    {"code": "en",      "name": "英語",                  "native": "English",          "flag": "🇺🇸"},
    {"code": "ja",      "name": "日本語",                "native": "日本語",            "flag": "🇯🇵"},
    {"code": "zh-Hans", "name": "中国語（簡体字）",        "native": "简体中文",          "flag": "🇨🇳"},
    {"code": "zh-Hant", "name": "中国語（繁体字）",        "native": "繁體中文",          "flag": "🇹🇼"},
    {"code": "ko",      "name": "韓国語",                 "native": "한국어",            "flag": "🇰🇷"},
    {"code": "es",      "name": "スペイン語",              "native": "Español",          "flag": "🇪🇸"},
    {"code": "es-419",  "name": "スペイン語（中南米）",     "native": "Español LA",       "flag": "🌎"},
    {"code": "pt-BR",   "name": "ポルトガル語（ブラジル）", "native": "Português BR",     "flag": "🇧🇷"},
    {"code": "fr",      "name": "フランス語",              "native": "Français",         "flag": "🇫🇷"},
    {"code": "de",      "name": "ドイツ語",                "native": "Deutsch",          "flag": "🇩🇪"},
    {"code": "it",      "name": "イタリア語",              "native": "Italiano",         "flag": "🇮🇹"},
    {"code": "ru",      "name": "ロシア語",                "native": "Русский",          "flag": "🇷🇺"},
    {"code": "id",      "name": "インドネシア語",          "native": "Bahasa Indonesia", "flag": "🇮🇩"},
    {"code": "vi",      "name": "ベトナム語",              "native": "Tiếng Việt",      "flag": "🇻🇳"},
    {"code": "th",      "name": "タイ語",                  "native": "ไทย",              "flag": "🇹🇭"},
    {"code": "tr",      "name": "トルコ語",                "native": "Türkçe",           "flag": "🇹🇷"},
    {"code": "ar",      "name": "アラビア語",              "native": "العربية",          "flag": "🇸🇦"},
    {"code": "hi",      "name": "ヒンディー語",            "native": "हिन्दी",             "flag": "🇮🇳"},
    {"code": "nl",      "name": "オランダ語",              "native": "Nederlands",       "flag": "🇳🇱"},
    {"code": "pl",      "name": "ポーランド語",            "native": "Polski",           "flag": "🇵🇱"},
    {"code": "sv",      "name": "スウェーデン語",          "native": "Svenska",          "flag": "🇸🇪"},
    {"code": "fil",     "name": "フィリピノ語",            "native": "Filipino",         "flag": "🇵🇭"},
    {"code": "ms",      "name": "マレー語",                "native": "Bahasa Melayu",    "flag": "🇲🇾"},
    {"code": "uk",      "name": "ウクライナ語",            "native": "Українська",       "flag": "🇺🇦"},
    {"code": "he",      "name": "ヘブライ語",              "native": "עברית",            "flag": "🇮🇱"},
]

# YouTube アップロード詳細設定（チャンネル横断テンプレート）
def _resolve_video_folder(video_name: str) -> Path:
    """video_name から動画フォルダの絶対パスを返す。"""
    config = get_dashboard_config()
    base = config.get("channel_folder")
    if not base:
        raise HTTPException(400, "channel_folder が未設定です")
    p = Path(base) / video_name
    if not p.exists():
        raise HTTPException(404, f"動画フォルダが見つかりません: {video_name}")
    return p


def _write_json_dict(p: Path, data: dict):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class YouTubeUploadDefaults(BaseModel):
    category_id: Optional[str] = None
    default_language: Optional[str] = None
    default_audio_language: Optional[str] = None
    made_for_kids: Optional[bool] = None
    synthetic_media: Optional[bool] = None
    license: Optional[str] = None
    embeddable: Optional[bool] = None
    public_stats_viewable: Optional[bool] = None
    notify_subscribers: Optional[bool] = None
    localization_languages: Optional[List[str]] = None


@router.get("/api/youtube/languages")
def api_get_yt_languages():
    """ローカリゼーション言語マスタ（24言語）を返す。UI のチェックボックスグリッド用。"""
    return {"languages": YT_LANGUAGE_CATALOG}


@router.get("/api/youtube/upload-defaults")
def api_get_yt_upload_defaults():
    """テンプレート設定（チャンネル横断）を返す。"""
    return {
        "defaults": get_yt_upload_defaults(),
        "builtin": YT_UPLOAD_BUILTIN_DEFAULTS,
        "saved": YT_UPLOAD_DEFAULTS_FILE.exists(),
    }


@router.put("/api/youtube/upload-defaults")
def api_put_yt_upload_defaults(req: YouTubeUploadDefaults):
    """テンプレート設定を保存。指定キーだけ上書き。
    アクティブチャンネルがあればチャンネル別ファイル（Google Drive 同期対象）へ、
    無ければグローバルにフォールバック。"""
    payload = {k: v for k, v in req.model_dump().items() if v is not None}
    if _channel_config_path():
        cc = load_channel_config()
        current = cc.get("youtube_upload_defaults") or {}
        current.update(payload)
        cc["youtube_upload_defaults"] = current
        save_channel_config(cc)
    else:
        current = _read_json_dict(YT_UPLOAD_DEFAULTS_FILE)
        current.update(payload)
        _write_json_dict(YT_UPLOAD_DEFAULTS_FILE, current)
    return {"status": "ok", "defaults": get_yt_upload_defaults()}


# ── 動画別の上書き API ──
@router.get("/api/videos/{video_name}/youtube-overrides")
def api_get_video_yt_overrides(video_name: str):
    folder = _resolve_video_folder(video_name)
    overrides = _read_json_dict(folder / "youtube_upload_overrides.json")
    return {
        "video_name": video_name,
        "overrides": overrides,
        "effective": {**get_yt_upload_defaults(), **overrides},
    }


@router.put("/api/videos/{video_name}/youtube-overrides")
def api_put_video_yt_overrides(video_name: str, req: YouTubeUploadDefaults):
    folder = _resolve_video_folder(video_name)
    p = folder / "youtube_upload_overrides.json"
    current = _read_json_dict(p)
    payload = {k: v for k, v in req.model_dump().items() if v is not None}
    current.update(payload)
    if current:
        _write_json_dict(p, current)
    elif p.exists():
        p.unlink()
    return {
        "status": "ok",
        "overrides": current,
        "effective": {**get_yt_upload_defaults(), **current},
    }


# ── 多言語タイトル/説明（ローカライズ）API ──
class YouTubeLocalization(BaseModel):
    title: str = ""
    description: str = ""


class YouTubeLocalizationsPayload(BaseModel):
    localizations: dict = {}  # {"en": {"title":"...","description":"..."}, ...}


@router.get("/api/videos/{video_name}/youtube-localizations")
def api_get_video_yt_localizations(video_name: str):
    folder = _resolve_video_folder(video_name)
    return {
        "video_name": video_name,
        "localizations": _read_json_dict(folder / "youtube_localizations.json"),
    }


@router.put("/api/videos/{video_name}/youtube-localizations")
def api_put_video_yt_localizations(video_name: str, req: YouTubeLocalizationsPayload):
    folder = _resolve_video_folder(video_name)
    p = folder / "youtube_localizations.json"
    cleaned = {}
    for lang, entry in (req.localizations or {}).items():
        if not isinstance(entry, dict):
            continue
        t = (entry.get("title") or "").strip()
        d = (entry.get("description") or "").strip()
        if t or d:
            cleaned[str(lang)] = {"title": t, "description": d}
    if cleaned:
        _write_json_dict(p, cleaned)
    elif p.exists():
        p.unlink()
    return {"status": "ok", "localizations": cleaned}


class YouTubeTranslateRequest(BaseModel):
    languages: List[str]                # ["ja", "zh-Hans", "ko", ...] - en はソース言語なので通常スキップ
    title: Optional[str] = None         # 未指定なら youtube_title.txt
    description: Optional[str] = None   # 未指定なら youtube_description.txt
    source_language: str = "en"
    overwrite: bool = False             # True なら既存の翻訳を上書き


@router.post("/api/videos/{video_name}/youtube-translate")
async def api_translate_video_yt(video_name: str, req: YouTubeTranslateRequest):
    """Claude CLI でタイトル+説明文を指定言語に翻訳し、youtube_localizations.json に保存。
    既存の翻訳は overwrite=True でない限り保持する。"""
    folder = _resolve_video_folder(video_name)
    title = (req.title or "").strip()
    if not title:
        tf = folder / "youtube_title.txt"
        if tf.exists():
            title = tf.read_text(encoding="utf-8").strip()
    description = (req.description or "").strip()
    if not description:
        df = folder / "youtube_description.txt"
        if df.exists():
            description = df.read_text(encoding="utf-8").strip()
    if not title and not description:
        raise HTTPException(400, "翻訳対象のタイトルまたは説明文がありません")

    # 既存
    p = folder / "youtube_localizations.json"
    existing = _read_json_dict(p)

    targets = [lang for lang in (req.languages or []) if lang and lang.strip()]
    if not req.overwrite:
        targets = [lang for lang in targets if lang not in existing]
    if not targets:
        return {"status": "ok", "skipped": True, "localizations": existing,
                "message": "全言語が既に存在します（overwrite=False）"}

    suno_cfg = get_suno_config()
    cli_cmd = suno_cfg.get("claude_cli") or "claude"

    prompt = f"""You are a YouTube localization translator for a BGM/instrumental music channel.

Translate the title and description from {req.source_language} into the following target languages:
{json.dumps(targets, ensure_ascii=False)}

Source title:
{title or '(none)'}

Source description:
{description or '(none)'}

Rules:
- Keep the SAME tone, mood, and aesthetic as the source.
- For BGM channel titles: keep the evocative scene/moment-based phrasing. Don't translate too literally.
- Preserve hashtags, URLs, and timestamps verbatim (do not translate them).
- Preserve the description's structure (paragraph breaks, bullet lists, timestamps if any).
- Title length should fit YouTube's 100-character limit.
- Use natural, native-speaker phrasing — not machine-translation style.
- Use the language's native script (e.g. zh-Hans = Simplified Chinese characters, ko = Korean Hangul).
- For "en", use English.

Return ONLY a JSON object in this exact shape (no markdown fences, no prose):
{{
  "translations": {{
    "<lang_code>": {{"title": "...", "description": "..."}},
    ...
  }}
}}
"""
    try:
        from app_llm_runner import run_llm, LLMError
        out = run_llm(prompt, cli_cmd=cli_cmd, timeout=300, label="translate-meta").strip()
    except LLMError as e:
        raise HTTPException(500, f"Claude/Codex 失敗: {str(e)[:300]}")

    # JSON 抽出（ファンスやプリアンブルを許容）
    m = re.search(r"\{[\s\S]*\}", out)
    if not m:
        raise HTTPException(502, f"Claude 応答から JSON を抽出できませんでした: {out[:300]}")
    try:
        parsed = json.loads(m.group(0))
    except Exception as e:
        raise HTTPException(502, f"JSON 解析失敗: {e}; 応答: {out[:300]}")

    translations = parsed.get("translations") or {}
    if not isinstance(translations, dict):
        raise HTTPException(502, "translations フィールドが不正です")

    merged = dict(existing)
    added = []
    for lang in targets:
        entry = translations.get(lang)
        if not isinstance(entry, dict):
            continue
        t = (entry.get("title") or "").strip()
        d = (entry.get("description") or "").strip()
        if not (t or d):
            continue
        merged[lang] = {"title": t, "description": d}
        added.append(lang)

    if merged:
        _write_json_dict(p, merged)

    return {"status": "ok", "added": added, "localizations": merged}


# ── アップロード API（既存を拡張）──
@router.post("/api/youtube/upload")
async def api_youtube_upload(req: UploadRequest):
    cmd, meta = _build_youtube_upload_command(req)
    result = _enqueue_youtube_upload(cmd, meta, source="web")
    return {**result, **_youtube_queue_snapshot()}


class BatchUploadRequest(BaseModel):
    video_names: List[str]
    privacy: str = "unlisted"  # "private" / "unlisted" / "public"


@router.post("/api/youtube/batch-upload")
async def api_youtube_batch_upload(req: BatchUploadRequest):
    """複数 vol を**順次**アップロード（既存 upload queue を活用）。

    bash + curl + polling の脆さ（変数名衝突、polling 漏れ）を排除し、
    一度の API 呼び出しで全部 enqueue する。queue は内部で 1 つずつ順次処理。
    quota 枯渇時は YT_QUOTA_EXHAUSTED で残りをスキップして翌日 scheduler が再開。
    """
    if not req.video_names:
        raise HTTPException(400, "video_names が空です")
    enqueued: list = []
    skipped: list = []
    for name in req.video_names:
        try:
            up_req = UploadRequest(video_name=name, privacy=req.privacy)
            cmd, meta = _build_youtube_upload_command(up_req)
            result = _enqueue_youtube_upload(cmd, meta, source="web-batch")
            enqueued.append({"video_name": name, **{k: v for k, v in result.items() if k in ("status", "job", "position")}})
        except HTTPException as e:
            skipped.append({"video_name": name, "reason": e.detail, "status_code": e.status_code})
        except Exception as e:
            skipped.append({"video_name": name, "reason": str(e), "status_code": 500})
    snapshot = _youtube_queue_snapshot()
    return {
        "status": "ok",
        "enqueued": enqueued,
        "skipped": skipped,
        **snapshot,
    }

@router.get("/api/youtube/status")
def api_youtube_status():
    proc = active_tasks.get("youtube")
    snap = _youtube_queue_snapshot()
    return {
        "running": proc is not None and proc.returncode is None,
        "logs": task_logs.get("youtube", [])[-80:],
        **snap,
    }


# ─── YouTube アップロード履歴（D-4: スプシ記録向けローカル蓄積 + CSV エクスポート） ───
YT_UPLOAD_HISTORY_FILE = CONFIG_DIR / "youtube_upload_history.jsonl"


def _read_youtube_history() -> List[dict]:
    if not YT_UPLOAD_HISTORY_FILE.exists():
        return []
    out: List[dict] = []
    try:
        for line in YT_UPLOAD_HISTORY_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        return []
    return out


class YouTubeHistoryEntry(BaseModel):
    video_name: str
    vol: Optional[str] = None
    youtube_url: Optional[str] = None
    title: Optional[str] = None
    privacy: Optional[str] = None
    schedule: Optional[str] = None
    tags: Optional[List[str]] = None
    localizations: Optional[List[str]] = None
    description_chars: Optional[int] = None
    has_thumbnail: Optional[bool] = None


@router.post("/api/youtube/history")
def api_record_youtube_history(entry: YouTubeHistoryEntry):
    """アップロード結果を JSONL に追記。スプシ貼り付け用に CSV エクスポート可能。"""
    record = entry.model_dump()
    record["recorded_at"] = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    YT_UPLOAD_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with YT_UPLOAD_HISTORY_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return {"status": "ok", "entry": record, "total": len(_read_youtube_history())}


@router.get("/api/youtube/history")
def api_get_youtube_history(limit: int = 200):
    items = _read_youtube_history()
    items.reverse()  # 新しい順
    return {"items": items[: max(1, min(int(limit), 1000))], "total": len(items)}


@router.get("/api/youtube/history.csv")
def api_export_youtube_history_csv():
    """CSV をダウンロード。スプシに貼り付ければそのまま記録できる。"""
    import io, csv
    items = _read_youtube_history()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "投稿日時", "vol", "動画名", "YouTube URL", "タイトル",
        "公開設定", "予約", "翻訳言語数", "言語コード", "タグ数", "サムネ", "説明文字数",
    ])
    for it in items:
        w.writerow([
            it.get("recorded_at", ""), it.get("vol", ""), it.get("video_name", ""),
            it.get("youtube_url", ""), it.get("title", ""),
            it.get("privacy", ""), it.get("schedule", ""),
            len(it.get("localizations") or []),
            ",".join(it.get("localizations") or []),
            len(it.get("tags") or []),
            "✓" if it.get("has_thumbnail") else "",
            it.get("description_chars") or 0,
        ])
    csv_text = buf.getvalue()
    # BOM つきで Excel/Google Sheets でも文字化けしない
    return Response(
        content=("﻿" + csv_text).encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="youtube_upload_history.csv"'},
    )


@router.delete("/api/youtube/history")
def api_clear_youtube_history():
    if YT_UPLOAD_HISTORY_FILE.exists():
        YT_UPLOAD_HISTORY_FILE.unlink()
    return {"status": "ok"}
