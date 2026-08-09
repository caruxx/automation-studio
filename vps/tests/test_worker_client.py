from __future__ import annotations

import json
from unittest import mock

import pytest

from worker.client import ConfigurationError, WorkerClient, WorkerError, load_config


def valid_config() -> dict:
    return {
        "base_url": "https://yt.caruvistar.jp",
        "worker_token": "worker-secret",
        "poll_interval_seconds": 30,
        "job_types": ["test", "upload"],
    }


def test_config_file_requires_mode_0600(tmp_path):
    config_path = tmp_path / "worker.json"
    config_path.write_text(json.dumps(valid_config()), encoding="utf-8")
    config_path.chmod(0o644)

    with pytest.raises(ConfigurationError, match="0600"):
        load_config(str(config_path))

    config_path.chmod(0o600)
    assert load_config(str(config_path))["base_url"] == "https://yt.caruvistar.jp"


def test_lease_response_is_parsed_without_network():
    job = {"id": 17, "job_type": "test", "payload": {"ok": True}}
    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps([job]).encode()
    response.__exit__.return_value = False
    client = WorkerClient(valid_config())

    with mock.patch("worker.client.urllib.request.urlopen", return_value=response) as urlopen:
        assert client.lease_jobs() == [job]

    request = urlopen.call_args.args[0]
    assert request.full_url == "https://yt.caruvistar.jp/api/worker/jobs/lease"
    assert json.loads(request.data) == {
        "job_types": ["test", "upload"],
        "lease_seconds": 600,
        "max_jobs": 1,
    }


def test_upload_payload_rejects_each_missing_required_key():
    from worker.client import validate_upload_payload

    payload = {
        "channel_id": 1,
        "file": "/tmp/video.mp4",
        "title": "Title",
        "description": "Description",
        "privacy_status": "unlisted",
    }
    for key in tuple(payload):
        invalid = dict(payload)
        del invalid[key]
        with pytest.raises(WorkerError, match=key):
            validate_upload_payload(invalid)
