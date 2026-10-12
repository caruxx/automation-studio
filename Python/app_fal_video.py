"""Optional fal cloud API controlled from Mac; no implicit retries or secret logging."""
from __future__ import annotations

import contextlib
import base64
import fcntl
import hashlib
import json
import math
import os
import re
import sqlite3
import struct
import subprocess
import time
import uuid
from datetime import datetime
from decimal import Decimal, ROUND_UP
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from typing import Literal, Optional

MODEL = "minimax/h3-max-turbo/image-to-video"
QUEUE = "https://queue.fal.run/"
PRICE_DATE = "2026-10-03"
# Fail closed after this UTC date until an owner rechecks official prices.
PRICE_VALID_UNTIL = "2026-10-04T00:00:00+00:00"
PROMO = {"480P": Decimal("0.015"), "768P": Decimal("0.024"), "1080P": Decimal("0.048")}
NORMAL = {"480P": Decimal("0.025"), "768P": Decimal("0.04"), "1080P": Decimal("0.08")}
MAX_DOWNLOAD = 128 * 1024 * 1024
RID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
JID = re.compile(r"[0-9a-f]{32}\Z")


class GateError(Exception):
    """Safe, user-visible message; never include upstream response/exception text."""


class VideoSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    model: Literal["minimax/h3-max-turbo/image-to-video"] = MODEL
    prompt: str = Field(min_length=1, max_length=50000)
    duration: float = Field(ge=0.92, le=15, allow_inf_nan=False)
    resolution: Literal["480P", "768P", "1080P"] = "768P"
    prompt_expansion_mode: Literal["disabled", "balanced", "quality"] = "disabled"
    image_url: str = Field(min_length=1, max_length=12*1024*1024)
    end_image_url: Optional[str] = Field(default=None, max_length=12*1024*1024)
    loop: StrictBool = False
    project_tag: str = Field(default="", max_length=100)


def _source_url(value):
    if value.startswith('data:image/png;base64,'):
        try:
            data = base64.b64decode(value[22:], validate=True)
            if len(data) > 8*1024*1024 or len(data) < 33 or data[:8] != b'\x89PNG\r\n\x1a\n' or data[12:16] != b'IHDR':
                raise ValueError('invalid png')
            width, height = struct.unpack('>II', data[16:24])
            if not 1 <= width <= 8192 or not 1 <= height <= 8192:
                raise ValueError('invalid dimensions')
        except Exception:
            raise GateError('直接添付する画像は8 MiB以下のPNG原本にしてください。') from None
        return value
    p = urlsplit(value)
    host = p.hostname or ""
    # Minimal first version accepts already-hosted public images; never auto-upload.
    allowed = host == "fal.media" or host.endswith(".fal.media") or host == "storage.googleapis.com"
    if p.scheme != "https" or p.username or p.password or p.port not in (None, 443) or not allowed or p.query or p.fragment:
        raise GateError("画像はfal.mediaまたはstorage.googleapis.comの認証情報を含まない公開HTTPS URLを指定してください。画像の自動アップロードは行いません。")
    return value


def _input(spec):
    first = _source_url(spec.image_url)
    last = _source_url(spec.end_image_url) if spec.end_image_url else None
    if spec.loop:
        if last and last != first:
            raise GateError("ループ補助では開始と終了に同じ画像を指定してください。")
        last = first
    result = dict(prompt=spec.prompt, duration=spec.duration, resolution=spec.resolution,
                  prompt_expansion_mode=spec.prompt_expansion_mode, image_url=first,
                  enable_safety_checker=True, sync_mode=False)
    if last:
        result["end_image_url"] = last
    return result


def _money(value):
    return str(value.quantize(Decimal("0.0001"), rounding=ROUND_UP))


def _queue_url(value, request_id):
    p = urlsplit(value)
    base = "/minimax/h3-max-turbo"
    paths = {f"{root}/requests/{request_id}{suffix}" for root in (base, base + "/image-to-video")
             for suffix in ("", "/response", "/status", "/cancel")}
    if p.scheme != "https" or p.netloc != "queue.fal.run" or p.path not in paths or p.query or p.fragment:
        raise GateError("falのジョブ応答URLを検証できません。再送せず公式履歴を確認してください。")
    return value


class FalTransport:
    """Official REST with a single submission attempt; httpx transport has no retries."""
    def __init__(self, key, client=None):
        if not key:
            raise GateError("FAL_KEYが未設定です。本人がAutomation Studioの実行環境に設定してください。キーを画面や共有設定へ貼り付けないでください。")
        self.client = client or httpx.Client(timeout=30, follow_redirects=False, trust_env=False)
        self._headers = {"Authorization": "Key " + key}

    def submit(self, arguments):
        # Do not replace with fal_client.submit: SDK 1.0.3 retries some POST failures.
        r = self.client.post(QUEUE + MODEL, headers={**self._headers, "X-Fal-No-Retry": "1"}, json=arguments)
        r.raise_for_status()
        return r.json()

    def status(self, url):
        r = self.client.get(url, headers=self._headers, params={"logs": 0})
        r.raise_for_status()
        return r.json()

    def result(self, url):
        r = self.client.get(url, headers=self._headers)
        r.raise_for_status()
        return r.json()

    def cancel(self, url):
        r = self.client.put(url, headers=self._headers)
        if r.status_code not in (202, 400, 404):
            r.raise_for_status()
        return r.status_code

    def upload_png(self, data, filename):
        # Official SDK 1.0.3 storage REST protocol, with retries disabled here.
        response = self.client.post("https://rest.fal.ai/storage/upload/initiate?storage_type=gcs",
                                    headers=self._headers, json={"file_name": filename, "content_type": "image/png"})
        response.raise_for_status()
        result = response.json()
        upload_url = result["upload_url"]
        parsed = urlsplit(upload_url)
        host = parsed.hostname or ""
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.fragment or parsed.port not in (None, 443) or not (host == "storage.googleapis.com" or host.endswith(".storage.googleapis.com")):
            raise GateError("画像の転送先を検証できません。再送しません。")
        image_url = _source_url(result["file_url"])
        # The API key belongs only to fal; NEVER forward it to the signed storage URL.
        response = self.client.put(upload_url, content=data, headers={"Content-Type": "image/png"})
        response.raise_for_status()
        return image_url


class VideoService:
    def __init__(self, root, *, role="mac", allow_paid_submit=False, transport_factory=None, clock=time.time,
                 download_client=None, probe=None):
        self.root = Path(root).absolute()
        if self.root.is_symlink() or self.root.resolve() != self.root:
            raise GateError("falの保存先にシンボリックリンクは使用できません。")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.role, self.clock, self.allow_paid_submit = role, clock, allow_paid_submit
        self.transport_factory = transport_factory or (lambda: FalTransport(os.environ.get("FAL_KEY")))
        self.download_client = download_client
        self.probe = probe or self._probe
        for kind in ("exports", "locks"):
            p = self.root / kind
            if p.is_symlink():
                raise GateError("falの保存先にシンボリックリンクは使用できません。")
            p.mkdir(exist_ok=True, mode=0o700)
        self.dbpath = self.root / "history.sqlite3"
        if self.dbpath.is_symlink():
            raise GateError("falの履歴にシンボリックリンクは使用できません。")
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, record TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS originals (id TEXT PRIMARY KEY, record TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS uploads (id TEXT PRIMARY KEY, record TEXT NOT NULL)")
        os.chmod(self.dbpath, 0o600)

    def _db(self):
        return sqlite3.connect(self.dbpath, timeout=30)

    @contextlib.contextmanager
    def _lock(self, job_id):
        if not JID.fullmatch(job_id):
            raise GateError("ジョブIDが不正です。")
        fd = os.open(self.root / "locks" / (job_id + ".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _read(self, job_id):
        if not JID.fullmatch(job_id):
            raise GateError("ジョブIDが不正です。")
        with self._db() as db:
            row = db.execute("SELECT record FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise GateError("ジョブが見つかりません。")
        return json.loads(row[0])

    def _save(self, job):
        with self._db() as db:
            db.execute("UPDATE jobs SET record=? WHERE id=?", (json.dumps(job), job["id"]))

    def _submission_gate(self):
        if self.role != "cloud" and not (self.role == "mac" and self.allow_paid_submit):
            raise GateError("有料API送信は未有効化です。MacでAPIを操作できますが、予算承認後に送信を有効化してください。計算はfalのCloudで行われます。")

    def upload_png(self, data, *, approved_sha256, approve_transfer):
        if approve_transfer is not True:
            raise GateError("選択した原本のfal転送を明示承認してください。")
        if len(data) > 8 * 1024 * 1024 or len(data) < 33 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
            raise GateError("画像は8 MiB以下のPNG原本を選択してください。")
        width, height = struct.unpack(">II", data[16:24])
        if not 1 <= width <= 8192 or not 1 <= height <= 8192:
            raise GateError("PNGの寸法を確認できません。")
        sha = hashlib.sha256(data).hexdigest()
        if approved_sha256 != sha:
            raise GateError("承認した原本のハッシュと一致しません。画像を再確認してください。")
        with self._lock(sha[:32]):
            with self._db() as db:
                row = db.execute("SELECT record FROM uploads WHERE id=?", (sha,)).fetchone()
            if row:
                record = json.loads(row[0])
                if record["status"] != "uploaded":
                    raise GateError("画像転送の結果が不明です。再送せずfalの保存状況を確認してください。")
                return record
            transport = self.transport_factory()  # Missing key does not consume the transfer.
            record = dict(id=sha, sha256=sha, bytes=len(data), width=width, height=height, status="uploading", created=self.clock())
            with self._db() as db:
                db.execute("INSERT INTO uploads VALUES (?,?)", (sha, json.dumps(record)))
            try:
                record.update(image_url=_source_url(transport.upload_png(data, "source-" + sha + ".png")), status="uploaded")
            except Exception:
                record["status"] = "upload_unknown"
            with self._db() as db:
                db.execute("UPDATE uploads SET record=? WHERE id=?", (json.dumps(record), sha))
            if record["status"] != "uploaded":
                raise GateError("画像転送を確認できません。自動再送はしません。原本は変更していません。")
            return record

    def quote(self, spec: VideoSpec, max_usd: Decimal):
        arguments = _input(spec)
        if not max_usd.is_finite() or max_usd <= 0 or max_usd > Decimal("10"):
            raise GateError("1回の予算上限は0ドル超、10ドル以下にしてください。")
        now = self.clock()
        if now >= datetime.fromisoformat(PRICE_VALID_UNTIL).timestamp():
            raise GateError("料金表の確認期限が過ぎています。公式単価を再確認して料金表を更新してください。")
        seconds = Decimal(str(spec.duration))
        estimate = PROMO[spec.resolution] * seconds
        # Reservation uses known regular price and documented ~0.7s output overrun.
        reserve = NORMAL[spec.resolution] * (seconds + Decimal("0.7"))
        reserve = Decimal(_money(reserve))
        if reserve > max_usd:
            raise GateError("通常単価と尺の余裕を含む予約額が予算上限を超えています。")
        jid = uuid.uuid4().hex
        fingerprint = hashlib.sha256(json.dumps(arguments, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        job = dict(id=jid, status="quoted", fingerprint=fingerprint, created=now, expires=min(now+900, datetime.fromisoformat(PRICE_VALID_UNTIL).timestamp()),
                   model=MODEL, input=arguments, loop=spec.loop, project_tag=spec.project_tag,
                   summary=dict(duration=spec.duration, resolution=spec.resolution, loop=spec.loop,
                                prompt_sha256=hashlib.sha256(spec.prompt.encode()).hexdigest(),
                                image_sha256=hashlib.sha256(spec.image_url.encode()).hexdigest()),
                   quote=dict(currency="USD", price_checked_at=PRICE_DATE, promo_end="2026-10-15",
                              unit_usd=str(PROMO[spec.resolution]), regular_unit_usd=str(NORMAL[spec.resolution]),
                              estimated_usd=_money(estimate), reserved_usd=str(reserve), max_usd=str(max_usd),
                              excludes="taxes, FX and payment fees", billing="Estimate only; not a provider-enforced spend cap"))
        with self._db() as db:
            db.execute("INSERT INTO jobs VALUES (?,?)", (jid, json.dumps(job)))
            db.execute("INSERT INTO originals VALUES (?,?)", (jid, json.dumps({"image_url": spec.image_url,
                       "end_image_url": arguments.get("end_image_url"), "project_tag": spec.project_tag})))
        return self.public(job)

    @staticmethod
    def public(job):
        keys = ("id", "status", "created", "expires", "model", "quote", "summary", "request_id", "error", "cancel_requested", "media", "normalized_media", "normalize_error", "project_tag")
        out = {k: job[k] for k in keys if k in job}
        # Exact immutable input for informed consent and safe review after reload.
        out["approval_preview"] = {k: job["input"][k] for k in ("prompt", "image_url", "end_image_url", "duration", "resolution", "prompt_expansion_mode") if k in job["input"]}
        for field in ('image_url', 'end_image_url'):
            value = out['approval_preview'].get(field, '')
            if value.startswith('data:image/png;base64,'):
                data = base64.b64decode(value[22:])
                out['approval_preview'][field] = 'PNG原本の直接添付 SHA-256: ' + hashlib.sha256(data).hexdigest()
        out["loop_warning"] = "同じ開始・終了画像でも完全なループは保証されません。"
        return out

    def get(self, jid):
        job = self._read(jid)
        if job["status"] == "submitting":
            # Includes a process crash before request ID persistence. Never submit again.
            job["error"] = "送信結果が不明です。再送せずfal公式履歴を確認してください。"
        return self.public(job)

    def history(self):
        with self._db() as db:
            rows = db.execute("SELECT record FROM jobs ORDER BY rowid DESC LIMIT 100").fetchall()
        return [self.public(json.loads(row[0])) for row in rows]

    def submit(self, jid, *, confirmation, approve_transfer, approved_max_usd):
        self._submission_gate()
        with self._lock(jid):
            job = self._read(jid)
            if job["status"] != "quoted":
                return self.get(jid)  # Idempotent: never a second paid POST for this quote.
            if confirmation != "GENERATE_PAID_VIDEO" or approve_transfer is not True:
                raise GateError("この見積りの有料生成と、指定画像・プロンプトのfal送信を明示承認してください。")
            if str(approved_max_usd) != job["quote"]["max_usd"]:
                raise GateError("承認した予算上限が見積りと一致しません。")
            if self.clock() >= job["expires"]:
                raise GateError("見積りの期限が切れました。新しく見積りを作成してください。")
            transport = self.transport_factory()  # Missing key leaves quote unconsumed.
            job.update(status="submitting", approved_at=self.clock())
            # Atomic cross-job guard: a new quote cannot accidentally regenerate an
            # identical in-flight/ambiguous input. Completed/known-failed jobs may be retried
            # only through a new separately approved quote.
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                for (record,) in db.execute("SELECT record FROM jobs WHERE id != ?", (jid,)):
                    other = json.loads(record)
                    if other.get("fingerprint") == job["fingerprint"] and other["status"] not in ("quoted", "completed", "failed"):
                        raise GateError("同じ入力の未完了ジョブがあります。そのジョブを確認してください。二重生成は行いません。")
                db.execute("UPDATE jobs SET record=? WHERE id=?", (json.dumps(job), jid))
            # Durable BEFORE network I/O.
            try:
                reply = transport.submit(job["input"])
                rid = reply.get("request_id")
                if not isinstance(rid, str) or not RID.fullmatch(rid):
                    raise ValueError("invalid request id")
                job["request_id"] = rid
                self._save(job)  # Retain ID even if URL verification subsequently fails.
                urls = {key: _queue_url(reply[key], rid) for key in ("response_url", "status_url", "cancel_url")}
                job.update(status="submitted", **urls)
            except Exception:
                job.update(status="submission_unknown", error="送信結果を確認できません。再送せずfal公式履歴を確認してください。")
            self._save(job)
            return self.public(job)

    def refresh(self, jid):
        with self._lock(jid):
            job = self._read(jid)
            if job["status"] in ("quoted", "completed", "failed"):
                return self.public(job)
            if not job.get("status_url"):
                raise GateError("ジョブIDまたは追跡URLが未確定です。公式履歴との照合が必要です。再送しません。")
            transport = self.transport_factory()
            try:
                remote = transport.status(job["status_url"])
                state = remote.get("status")
                if state == "COMPLETED":
                    if remote.get("error") or remote.get("error_type"):
                        job.update(status="failed", error="fal側で生成が失敗しました。自動再生成は行いません。")
                    else:
                        result = transport.result(job["response_url"])
                        media_url = result["video"]["url"]
                        self._media_url(media_url)
                        job.update(status="ready", media_url=media_url)
                        job.pop("error", None)
                elif state in ("IN_QUEUE", "IN_PROGRESS"):
                    job["status"] = "queued" if state == "IN_QUEUE" else "running"
                    job.pop("error", None)
                else:
                    raise ValueError("unknown status")
            except Exception:
                # Keep known request identity; transient failures resume via refresh only.
                job["error"] = "状況を確認できません。同じジョブで再確認してください。再送しません。"
            self._save(job)
            return self.public(job)

    def cancel(self, jid):
        with self._lock(jid):
            job = self._read(jid)
            if not job.get("cancel_url"):
                raise GateError("falジョブが未確定のためキャンセルできません。")
            try:
                code = self.transport_factory().cancel(job["cancel_url"])
                if code == 202:
                    job["cancel_requested"] = True
                    job["error"] = "キャンセル要求を受け付けました。実行中の生成停止や無課金は保証されません。"
                elif code == 400:
                    job["error"] = "falでは既に完了しています。同じジョブで結果を確認してください。"
                else:
                    job["error"] = "falでジョブが見つかりません。公式履歴を確認してください。再送しません。"
            except Exception:
                job["error"] = "キャンセル結果が不明です。同じジョブで状況を確認してください。"
            self._save(job)
            return self.public(job)

    def reconcile_request(self, jid, request_id, *, verified_in_dashboard=False):
        """Operator-only recovery AFTER matching input in official dashboard; no submit.

        Not exposed as a browser API: an authenticated cloud operator must explicitly
        verify the original input/request in fal history before binding a missing ID.
        """
        if verified_in_dashboard is not True or not isinstance(request_id, str) or not RID.fullmatch(request_id):
            raise GateError("公式履歴で元画像・プロンプト・送信時刻を照合したジョブIDが必要です。")
        with self._lock(jid):
            job = self._read(jid)
            if job["status"] not in ("submitting", "submission_unknown"):
                raise GateError("送信結果が不明なジョブだけを照合できます。")
            if job.get("request_id") and job["request_id"] != request_id:
                raise GateError("保存済みfalジョブIDと一致しません。")
            base = QUEUE + "minimax/h3-max-turbo/requests/" + request_id
            job.update(request_id=request_id, response_url=base, status_url=base + "/status",
                       cancel_url=base + "/cancel", status="submitted", reconciled_at=self.clock())
            job.pop("error", None)
            self._save(job)
            return self.public(job)

    @staticmethod
    def _media_url(value):
        p = urlsplit(value)
        host = p.hostname or ""
        if p.scheme != "https" or p.port not in (None, 443) or p.username or p.password or p.fragment or not (host == "fal.media" or host.endswith(".fal.media")):
            raise GateError("完成素材URLがfalの配布元ではありません。")
        return value

    @staticmethod
    def _probe(path, duration):
        result = subprocess.run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
                                capture_output=True, timeout=30, check=True)
        data = json.loads(result.stdout)
        streams = [s for s in data.get("streams", []) if s.get("codec_type") == "video"]
        seconds = float(data["format"]["duration"])
        if "mp4" not in data.get("format", {}).get("format_name", "") or not streams or not math.isfinite(seconds) or not max(.5, duration - 1) <= seconds <= duration + 1.5:
            raise GateError("完成MP4の映像・尺を検証できません。")
        stream = streams[0]
        if stream.get("codec_name") not in ("h264", "hevc", "av1") or stream.get("width", 0) <= 0 or stream.get("height", 0) <= 0:
            raise GateError("完成MP4のコーデック・解像度を検証できません。")
        return dict(duration=seconds, codec=stream["codec_name"], width=stream["width"], height=stream["height"])

    def download(self, jid):
        with self._lock(jid):
            job = self._read(jid)
            target = self.root / "exports" / (jid + ".mp4")
            if job["status"] == "completed":
                return self.public(job)
            if job["status"] != "ready":
                raise GateError("完成素材がまだ取得できません。先に同じジョブの状況を確認してください。")
            self._media_url(job["media_url"])
            partial = target.with_suffix(".partial")
            # Crash recovery only touches this job's own staging file, never an original.
            if partial.is_symlink() or target.is_symlink():
                raise GateError("完成素材の保存先が不正です。")
            client = self.download_client or httpx.Client(timeout=60, follow_redirects=False, trust_env=False)
            try:
                # If publication completed just before a crash, verify and adopt it.
                if not target.exists():
                    with client.stream("GET", job["media_url"]) as response:
                        response.raise_for_status()
                        count = 0
                        with partial.open("wb") as output:
                            for chunk in response.iter_bytes():
                                count += len(chunk)
                                if count > MAX_DOWNLOAD:
                                    raise GateError("完成素材が128 MiBを超えています。")
                                output.write(chunk)
                            output.flush()
                            os.fsync(output.fileno())
                    with partial.open("rb") as inp:
                        header = inp.read(32)
                    if len(header) < 12 or header[4:8] != b"ftyp":
                        raise GateError("取得したファイルはMP4ではありません。")
                    media = self.probe(partial, job["input"]["duration"])
                    os.link(partial, target)  # No overwrite of existing output.
                    partial.unlink()
                else:
                    media = self.probe(target, job["input"]["duration"])
                media["sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
                media["bytes"] = target.stat().st_size
                media["filename"] = target.name
                job.update(status="completed", media=media)
                job.pop("error", None)
            except Exception:
                job["error"] = "完成MP4の取得・検証に失敗しました。同じジョブの素材取得を再実行できます。生成は再送しません。"
            finally:
                if self.download_client is None:
                    client.close()
                if partial.exists() and not partial.is_symlink():
                    partial.unlink()
            self._save(job)
            return self.public(job)

    def normalize_720p(self, jid):
        with self._lock(jid):
            job = self._read(jid)
            original = self.file(jid)
            if job.get("normalized_media"):
                self.file(jid, normalized=True)
                return self.public(job)
            media = job["media"]
            if media["width"] < 1280 or media["height"] < 720:
                raise GateError("元映像が1280×720未満です。アップスケールは自動で行いません。元の寸法で保存してください。")
            target = self.root / "exports" / (jid + "_1280x720.mp4")
            stage = target.with_suffix(".staging.mp4")
            if target.exists() or target.is_symlink() or stage.is_symlink():
                raise GateError("整形先が既に存在します。上書きせず確認してください。")
            lock = None
            try:
                # Share the existing Mac ffmpeg lock; no new rendering service.
                from resource_lock import acquire_resource
                lock = acquire_resource("ffmpeg_edit", owner="fal-video:" + jid, blocking=False)
                subprocess.run(["nice", "-n", "10", "ffmpeg", "-nostdin", "-v", "error", "-n", "-i", str(original),
                                "-map", "0:v:0", "-map", "0:a?", "-vf",
                                "scale=1280:720:force_original_aspect_ratio=decrease:force_divisible_by=2,pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=white,setsar=1",
                                "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", str(stage)],
                               capture_output=True, check=True, timeout=180)
                normalized = self.probe(stage, media["duration"])
                if (normalized["width"], normalized["height"]) != (1280, 720) or abs(normalized["duration"] - media["duration"]) > .2:
                    raise GateError("整形後の寸法・尺を確認できません。元映像は保持しています。")
                normalized.update(sha256=hashlib.sha256(stage.read_bytes()).hexdigest(), bytes=stage.stat().st_size,
                                  filename=target.name, source_sha256=media["sha256"], upscaled=False,
                                  duration_policy="preserved; no trimming or speed change")
                os.link(stage, target)
                job["normalized_media"] = normalized
                job.pop("normalize_error", None)
            except Exception:
                job["normalize_error"] = "1280×720整形を完了できません。元MP4は保持されています。生成は再送しません。"
            finally:
                if lock is not None:
                    lock.release()
                if stage.exists() and not stage.is_symlink():
                    stage.unlink()
            self._save(job)
            return self.public(job)

    def file(self, jid, normalized=False):
        job = self._read(jid)
        target = self.root / "exports" / (jid + ("_1280x720.mp4" if normalized else ".mp4"))
        media = job.get("normalized_media") if normalized else job.get("media")
        if job["status"] != "completed" or target.is_symlink() or not target.is_file():
            raise GateError("検証済み完成素材がありません。")
        if not media or hashlib.sha256(target.read_bytes()).hexdigest() != media["sha256"]:
            raise GateError("完成素材が取得後に変更されています。")
        return target
