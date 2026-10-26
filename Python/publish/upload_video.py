"""YouTube への投稿本体（純関数。print / sys.exit / marker 書き込みをしない）。段階 3。

- resumable upload。セッション URI と offset を on_session で通知し、resume= で再開できる。
- 再試行できる HTTP エラー（403 quota / 429 / 5xx）は RetryableUploadError。
- 再開に使ったセッションが失効（404 / 410）していて結果が分からないときだけ UploadResultUnknown。
  （途中失敗は VPS に保存したセッションで次回再開するので「結果不明」にはしない）
"""
from __future__ import annotations

import mimetypes
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

DEFAULT_CHUNK = 10 * 1024 * 1024
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class RetryableUploadError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class UploadResultUnknown(RuntimeError):
    """送信したが結果を確定できない（セッション失効など）。成功にしてはいけない。"""


@dataclass
class UploadRequest:
    video_path: Path
    title: str
    description: str = ""
    tags: list[str] = field(default_factory=list)
    privacy: str = "private"
    publish_at: Optional[str] = None  # ISO 8601（UTC、末尾 Z）。指定時は privacy=private + publishAt
    category_id: str = "10"
    default_language: str = "en"
    default_audio_language: str = "en"
    made_for_kids: bool = False
    synthetic_media: bool = False
    license: str = "youtube"
    embeddable: bool = True
    public_stats_viewable: bool = True
    notify_subscribers: bool = False
    localizations: dict[str, Any] = field(default_factory=dict)
    thumbnail_path: Optional[Path] = None


@dataclass
class UploadResult:
    video_id: str
    url: str
    privacy: str
    publish_at: Optional[str]
    thumbnail_set: bool
    elapsed_sec: float


def is_retryable_http_error(err: HttpError) -> bool:
    status = getattr(getattr(err, "resp", None), "status", None)
    if status in RETRYABLE_STATUS:
        return True
    if status == 403:
        text = str(getattr(err, "content", b"") or b"")
        return "quotaExceeded" in text or "rateLimitExceeded" in text or "userRateLimitExceeded" in text
    return False


def build_body(req: UploadRequest) -> tuple[dict, list[str]]:
    snippet = {
        "title": (req.title or "")[:100],
        "description": (req.description or "")[:5000],
        "tags": list(req.tags or []),
        "categoryId": str(req.category_id),
        "defaultLanguage": req.default_language,
        "defaultAudioLanguage": req.default_audio_language,
    }
    status = {
        "privacyStatus": req.privacy,
        "selfDeclaredMadeForKids": bool(req.made_for_kids),
        "containsSyntheticMedia": bool(req.synthetic_media),
        "license": req.license if req.license in ("youtube", "creativeCommon") else "youtube",
        "embeddable": bool(req.embeddable),
        "publicStatsViewable": bool(req.public_stats_viewable),
    }
    if req.publish_at:
        status["privacyStatus"] = "private"  # 予約公開は private + publishAt
        status["publishAt"] = req.publish_at
    body = {"snippet": snippet, "status": status}
    parts = ["snippet", "status"]
    if req.localizations:
        body["localizations"] = req.localizations
        parts.append("localizations")
    return body, parts


def upload(youtube, req: UploadRequest, *, chunk_size: int = DEFAULT_CHUNK,
           on_session: Optional[Callable[[str, int], None]] = None,
           resume: Optional[dict] = None,
           progress: Optional[Callable[[float], None]] = None,
           media_factory: Callable[..., Any] = MediaFileUpload,
           execute: Optional[Callable[[Any, str], Any]] = None) -> UploadResult:
    """videos.insert（resumable）。成功時だけ UploadResult を返す。"""
    body, parts = build_body(req)
    media = media_factory(str(req.video_path), mimetype="video/mp4", resumable=True, chunksize=chunk_size)
    request = youtube.videos().insert(part=",".join(parts), body=body, media_body=media,
                                      notifySubscribers=bool(req.notify_subscribers))
    resumed = False
    if resume and resume.get("session_uri"):
        request.resumable_uri = resume["session_uri"]
        request.resumable_progress = int(resume.get("offset") or 0)
        resumed = True
    started = time.time()
    response = None
    last_uri = None
    try:
        while response is None:
            status_obj, response = request.next_chunk()
            uri = getattr(request, "resumable_uri", None)
            offset = int(getattr(request, "resumable_progress", 0) or 0)
            if uri and on_session and (uri != last_uri or status_obj is not None):
                on_session(uri, offset)
                last_uri = uri
            if status_obj is not None and progress:
                progress(float(status_obj.progress()))
    except HttpError as exc:
        status = getattr(getattr(exc, "resp", None), "status", None)
        if resumed and status in (404, 410):
            raise UploadResultUnknown(f"resumable session expired (HTTP {status}); 結果を確定できません") from exc
        if is_retryable_http_error(exc):
            raise RetryableUploadError(f"HTTP {status}", status=status) from exc
        raise
    video_id = response["id"]
    thumbnail_set = False
    if req.thumbnail_path and Path(req.thumbnail_path).is_file():
        mime = mimetypes.guess_type(str(req.thumbnail_path))[0] or "image/jpeg"
        call = youtube.thumbnails().set(videoId=video_id, media_body=media_factory(str(req.thumbnail_path), mimetype=mime))
        (execute(call, "thumbnails.set") if execute else call.execute())
        thumbnail_set = True
    return UploadResult(video_id=video_id, url=f"https://youtu.be/{video_id}", privacy=body["status"]["privacyStatus"],
                        publish_at=body["status"].get("publishAt"), thumbnail_set=thumbnail_set,
                        elapsed_sec=time.time() - started)


def verify_video(youtube, video_id: str) -> Optional[dict]:
    """videos.list で状態を読み戻す（publish_verify と同じ項目）。無ければ None。"""
    resp = youtube.videos().list(part="status,snippet,processingDetails", id=video_id).execute()
    items = resp.get("items") or []
    if not items:
        return None
    item = items[0]
    st = item.get("status") or {}
    return {"privacy_status": st.get("privacyStatus"), "publish_at": st.get("publishAt"),
            "upload_status": st.get("uploadStatus"), "channel_id": (item.get("snippet") or {}).get("channelId"),
            "processing_status": (item.get("processingDetails") or {}).get("processingStatus")}


def publish_to_public(youtube, video_id: str) -> dict:
    current = verify_video(youtube, video_id)
    if current is None:
        return {"status": "missing", "video_id": video_id}
    if current.get("privacy_status") == "public":
        return {"status": "already_public", "video_id": video_id}
    youtube.videos().update(part="status", body={"id": video_id, "status": {"privacyStatus": "public"}}).execute()
    return {"status": "ok", "video_id": video_id}
