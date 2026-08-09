#!/usr/bin/env python3
"""Pull jobs from Automation Studio and execute them on a Mac worker."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import signal
import ssl
import stat
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = "~/.config/automation-studio/worker.json"
LEASE_SECONDS = 600
HEARTBEAT_INTERVAL_SECONDS = 120
UPLOAD_CHUNK_SIZE = 8 * 1024 * 1024
YOUTUBE_UPLOAD_URL = (
    "https://www.googleapis.com/upload/youtube/v3/videos"
    "?uploadType=resumable&part=snippet,status"
)

LOG = logging.getLogger("automation-studio-worker")


class WorkerError(Exception):
    """A worker failure with retryability information for the queue."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class ConfigurationError(WorkerError):
    pass


class JobInterrupted(WorkerError):
    def __init__(self) -> None:
        super().__init__("Worker received a termination signal", retryable=True)


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
    LOG.handlers.clear()
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)
    LOG.propagate = False


def load_config(path_value: str) -> dict[str, Any]:
    path = Path(path_value).expanduser()
    try:
        file_stat = path.stat()
    except OSError as exc:
        raise ConfigurationError(f"Cannot read config file {path}: {exc}") from None

    mode = stat.S_IMODE(file_stat.st_mode)
    if mode != 0o600:
        raise ConfigurationError(
            f"Config file {path} must have permissions 0600; found {mode:04o}"
        )

    try:
        with path.open("r", encoding="utf-8") as handle:
            config = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"Invalid config file {path}: {exc}") from None

    if not isinstance(config, dict):
        raise ConfigurationError("Config must be a JSON object")

    base_url = config.get("base_url")
    worker_token = config.get("worker_token")
    poll_interval = config.get("poll_interval_seconds", 30)
    job_types = config.get("job_types")
    if not isinstance(base_url, str) or not base_url.startswith("https://"):
        raise ConfigurationError("base_url must be an HTTPS URL")
    if not isinstance(worker_token, str) or not worker_token.strip():
        raise ConfigurationError("worker_token must be a non-empty string")
    if (
        isinstance(poll_interval, bool)
        or not isinstance(poll_interval, (int, float))
        or poll_interval <= 0
    ):
        raise ConfigurationError("poll_interval_seconds must be greater than zero")
    if (
        not isinstance(job_types, list)
        or not job_types
        or not all(isinstance(item, str) and item for item in job_types)
    ):
        raise ConfigurationError("job_types must be a non-empty list of strings")

    return {
        "base_url": base_url.rstrip("/"),
        "worker_token": worker_token,
        "poll_interval_seconds": float(poll_interval),
        "job_types": job_types,
    }


def parse_lease_response(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, bytes):
        try:
            value = json.loads(value.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkerError(f"Invalid lease response: {exc}", retryable=True) from None
    elif isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise WorkerError(f"Invalid lease response: {exc}", retryable=True) from None

    if not isinstance(value, list):
        raise WorkerError("Invalid lease response: expected a JSON array", retryable=True)
    for job in value:
        if not isinstance(job, dict):
            raise WorkerError("Invalid lease response: job must be an object", retryable=True)
        for key in ("id", "job_type", "payload"):
            if key not in job:
                raise WorkerError(
                    f"Invalid lease response: job is missing {key}", retryable=True
                )
        if isinstance(job["id"], bool) or not isinstance(job["id"], int):
            raise WorkerError("Invalid lease response: job id must be an integer", retryable=True)
        if not isinstance(job["job_type"], str) or not isinstance(job["payload"], dict):
            raise WorkerError("Invalid lease response: invalid job fields", retryable=True)
    return value


def validate_upload_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise WorkerError("Upload payload must be an object")

    required_types = {
        "channel_id": int,
        "file": str,
        "title": str,
        "description": str,
        "privacy_status": str,
    }
    for key, expected_type in required_types.items():
        if key not in payload:
            raise WorkerError(f"Upload payload is missing required key: {key}")
        if isinstance(payload[key], bool) or not isinstance(payload[key], expected_type):
            raise WorkerError(f"Upload payload has invalid type for: {key}")

    if payload["channel_id"] <= 0:
        raise WorkerError("Upload payload channel_id must be positive")
    file_path = Path(payload["file"])
    if not file_path.is_absolute():
        raise WorkerError("Upload payload file must be an absolute path")
    if payload["privacy_status"] not in {"private", "unlisted", "public"}:
        raise WorkerError("Upload payload privacy_status is invalid")

    tags = payload.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise WorkerError("Upload payload tags must be a list of strings")
    category_id = payload.get("category_id", "10")
    if not isinstance(category_id, str) or not category_id:
        raise WorkerError("Upload payload category_id must be a non-empty string")
    publish_at = payload.get("publish_at")
    if publish_at is not None:
        if not isinstance(publish_at, str) or not publish_at:
            raise WorkerError("Upload payload publish_at must be an ISO8601 string")
        try:
            dt.datetime.fromisoformat(publish_at.replace("Z", "+00:00"))
        except ValueError:
            raise WorkerError("Upload payload publish_at must be an ISO8601 string") from None

    normalized = dict(payload)
    normalized["tags"] = tags
    normalized["category_id"] = category_id
    return normalized


def _http_error(status_code: int) -> WorkerError:
    return WorkerError(
        f"HTTP request failed with status {status_code}",
        retryable=status_code >= 500,
    )


def _one_line(message: str) -> str:
    return " ".join(message.splitlines())


class WorkerClient:
    def __init__(self, config: dict[str, Any]) -> None:
        self.base_url = config["base_url"]
        self.worker_token = config["worker_token"]
        self.poll_interval = config["poll_interval_seconds"]
        self.job_types = config["job_types"]
        self.stop_event = threading.Event()
        self.ssl_context = ssl.create_default_context()
        self.current_job_id: int | None = None

    def request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        data = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.worker_token}",
        }
        if payload is not None:
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(
                request, timeout=60, context=self.ssl_context
            ) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            raise _http_error(exc.code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise WorkerError(
                f"Network request failed: {type(exc).__name__}", retryable=True
            ) from None
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkerError(f"Invalid JSON response: {exc}", retryable=True) from None

    def lease_jobs(self) -> list[dict[str, Any]]:
        response = self.request_json(
            "POST",
            "/api/worker/jobs/lease",
            {
                "job_types": self.job_types,
                "lease_seconds": LEASE_SECONDS,
                "max_jobs": 1,
            },
        )
        return parse_lease_response(response)

    def complete_job(
        self, job_id: int, result: dict[str, Any], *, quota_units: int = 0
    ) -> None:
        self.request_json(
            "POST",
            f"/api/worker/jobs/{job_id}/complete",
            {"result": result, "quota_units": quota_units},
        )

    def fail_job(self, job_id: int, error: str, *, retryable: bool) -> None:
        self.request_json(
            "POST",
            f"/api/worker/jobs/{job_id}/fail",
            {"error": error[:10000] or "Unknown worker failure", "retryable": retryable},
        )

    def heartbeat_job(self, job_id: int) -> None:
        self.request_json(
            "POST",
            f"/api/worker/jobs/{job_id}/heartbeat",
            {"lease_seconds": LEASE_SECONDS},
        )

    def _credentials(self, channel_id: int) -> str:
        response = self.request_json(
            "GET", f"/api/worker/channels/{channel_id}/credentials"
        )
        if not isinstance(response, dict) or not isinstance(
            response.get("access_token"), str
        ):
            raise WorkerError("Credentials response did not contain an access token")
        return response["access_token"]

    def _youtube_request(
        self,
        request: urllib.request.Request,
        *,
        allow_resume_incomplete: bool = False,
    ) -> tuple[int, Any, bytes]:
        try:
            with urllib.request.urlopen(
                request, timeout=180, context=self.ssl_context
            ) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as exc:
            if allow_resume_incomplete and exc.code == 308:
                return exc.code, exc.headers, exc.read()
            raise _http_error(exc.code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise WorkerError(
                f"YouTube network request failed: {type(exc).__name__}", retryable=True
            ) from None

    def upload_video(self, payload: dict[str, Any]) -> dict[str, str]:
        details = validate_upload_payload(payload)
        file_path = Path(details["file"])
        try:
            file_size = file_path.stat().st_size
        except OSError as exc:
            raise WorkerError(f"Cannot access upload file: {exc}") from None
        if not file_path.is_file() or file_size <= 0:
            raise WorkerError("Upload file must be a non-empty regular file")

        access_token = self._credentials(details["channel_id"])
        privacy_status = details["privacy_status"]
        status: dict[str, Any] = {"privacyStatus": privacy_status}
        if details.get("publish_at"):
            status["publishAt"] = details["publish_at"]
            status["privacyStatus"] = "private"
        metadata = {
            "snippet": {
                "title": details["title"],
                "description": details["description"],
                "tags": details["tags"],
                "categoryId": details["category_id"],
            },
            "status": status,
        }
        initiation = urllib.request.Request(
            YOUTUBE_UPLOAD_URL,
            data=json.dumps(metadata, separators=(",", ":")).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Length": str(file_size),
                "X-Upload-Content-Type": "video/mp4",
            },
            method="POST",
        )
        _, headers, _ = self._youtube_request(initiation)
        upload_url = headers.get("Location")
        if not upload_url:
            raise WorkerError("YouTube did not return a resumable upload URL", retryable=True)

        response_body = b""
        offset = 0
        try:
            with file_path.open("rb") as video_file:
                while offset < file_size:
                    if self.stop_event.is_set():
                        raise JobInterrupted()
                    video_file.seek(offset)
                    chunk = video_file.read(min(UPLOAD_CHUNK_SIZE, file_size - offset))
                    end = offset + len(chunk) - 1
                    chunk_request = urllib.request.Request(
                        upload_url,
                        data=chunk,
                        headers={
                            "Authorization": f"Bearer {access_token}",
                            "Content-Type": "video/mp4",
                            "Content-Length": str(len(chunk)),
                            "Content-Range": f"bytes {offset}-{end}/{file_size}",
                        },
                        method="PUT",
                    )
                    code, chunk_headers, response_body = self._youtube_request(
                        chunk_request, allow_resume_incomplete=True
                    )
                    if code == 308:
                        uploaded_range = chunk_headers.get("Range", "")
                        if uploaded_range.startswith("bytes=0-"):
                            try:
                                offset = int(uploaded_range.rsplit("-", 1)[1]) + 1
                            except ValueError:
                                offset = end + 1
                        else:
                            offset = end + 1
                    else:
                        offset = file_size
        except OSError as exc:
            raise WorkerError(f"Cannot read upload file: {exc}") from None

        try:
            upload_result = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkerError(f"Invalid YouTube upload response: {exc}") from None
        video_id = upload_result.get("id") if isinstance(upload_result, dict) else None
        if not isinstance(video_id, str) or not video_id:
            raise WorkerError("YouTube upload response did not contain a video id")
        return {"video_id": video_id, "url": f"https://youtu.be/{video_id}"}

    def _heartbeat_loop(self, job_id: int, finished: threading.Event) -> None:
        while not finished.wait(HEARTBEAT_INTERVAL_SECONDS):
            if self.stop_event.is_set():
                return
            try:
                self.heartbeat_job(job_id)
                LOG.info("Job %s lease renewed", job_id)
            except WorkerError as exc:
                LOG.warning("Job %s heartbeat failed: %s", job_id, _one_line(str(exc)))

    def process_job(self, job: dict[str, Any]) -> None:
        job_id = job["id"]
        job_type = job["job_type"]
        payload = job["payload"]
        self.current_job_id = job_id
        LOG.info("Job %s started type=%s", job_id, job_type)
        heartbeat_finished: threading.Event | None = None
        heartbeat_thread: threading.Thread | None = None
        try:
            if self.stop_event.is_set():
                raise JobInterrupted()
            if job_type == "test":
                self.complete_job(job_id, payload)
            elif job_type == "upload":
                heartbeat_finished = threading.Event()
                heartbeat_thread = threading.Thread(
                    target=self._heartbeat_loop,
                    args=(job_id, heartbeat_finished),
                    name=f"heartbeat-{job_id}",
                    daemon=True,
                )
                heartbeat_thread.start()
                result = self.upload_video(payload)
                if self.stop_event.is_set():
                    raise JobInterrupted()
                self.complete_job(job_id, result, quota_units=100)
            else:
                raise WorkerError(f"Unsupported job type: {job_type}")
            LOG.info("Job %s completed", job_id)
        except WorkerError as exc:
            retryable = True if self.stop_event.is_set() else exc.retryable
            LOG.error("Job %s failed: %s", job_id, _one_line(str(exc)))
            try:
                self.fail_job(job_id, str(exc), retryable=retryable)
            except WorkerError as report_error:
                LOG.error(
                    "Job %s failure report failed: %s",
                    job_id,
                    _one_line(str(report_error)),
                )
        except Exception as exc:
            error = f"Unexpected worker error: {type(exc).__name__}: {exc}"
            retryable = self.stop_event.is_set()
            LOG.error("Job %s failed unexpectedly: %s", job_id, _one_line(error))
            try:
                self.fail_job(job_id, error, retryable=retryable)
            except WorkerError as report_error:
                LOG.error(
                    "Job %s failure report failed: %s",
                    job_id,
                    _one_line(str(report_error)),
                )
        finally:
            if heartbeat_finished is not None:
                heartbeat_finished.set()
            if heartbeat_thread is not None:
                heartbeat_thread.join(timeout=5)
            self.current_job_id = None

    def request_stop(self, signum: int, _frame: Any) -> None:
        LOG.info("Signal %s received; stopping worker", signum)
        self.stop_event.set()

    def run(self, *, once: bool = False) -> int:
        while not self.stop_event.is_set():
            try:
                jobs = self.lease_jobs()
            except WorkerError as exc:
                LOG.error("Lease request failed: %s", _one_line(str(exc)))
                if once or not exc.retryable:
                    return 1
                self.stop_event.wait(self.poll_interval)
                continue

            if jobs:
                self.process_job(jobs[0])
            elif not once:
                self.stop_event.wait(self.poll_interval)
            if once:
                break
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Automation Studio Mac worker")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    parser.add_argument(
        "--once",
        action="store_true",
        help="Lease once, process at most one job, and exit",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigurationError as exc:
        LOG.error("%s", exc)
        return 2

    client = WorkerClient(config)
    signal.signal(signal.SIGINT, client.request_stop)
    signal.signal(signal.SIGTERM, client.request_stop)
    LOG.info("Worker started job_types=%s", ",".join(client.job_types))
    return client.run(once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
