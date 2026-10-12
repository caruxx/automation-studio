"""Bounded, local-only PNG compositing for independent short motion assets.

No pipeline state or source image is changed. Pillow is imported on first use so
installations without image support can still import the main web application.
"""
from __future__ import annotations

import base64
import binascii
import io
import json
import math
import os
import select
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

MAX_PROJECT_BYTES = 32 * 1024 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
MAX_TOTAL_IMAGE_PIXELS = 24_000_000
MAX_LAYERS = 16
MAX_WORK_PIXELS = 8_000_000_000
EXPORT_TIMEOUT = 900
PNG_PREFIX = "data:image/png;base64,"


class InvalidProject(ValueError):
    pass


class ExportCancelled(Exception):
    pass


def _pil():
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("PNG 合成に必要な Pillow がこの Python 環境にありません") from exc
    return Image


def _number(value: Any, label: str, low: float, high: float, *, integer: bool = False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidProject(f"{label} は有限の数値にしてください")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise InvalidProject(f"{label} は有限の数値にしてください")
    if not low <= value <= high or (integer and int(value) != value):
        raise InvalidProject(f"{label} は {low:g}〜{high:g}{' の整数' if integer else ''}にしてください")
    return int(value) if integer else float(value)


def _png(value: Any, label: str):
    Image = _pil()
    if not isinstance(value, str) or not value.startswith(PNG_PREFIX):
        raise InvalidProject(f"{label} は PNG data URL にしてください（外部 URL・パス不可）")
    encoded = value[len(PNG_PREFIX):]
    if len(encoded) > (MAX_IMAGE_BYTES + 2) // 3 * 4:
        raise InvalidProject(f"{label} の PNG は 8 MiB 以下にしてください")
    try:
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) > MAX_IMAGE_BYTES or not raw.startswith(b"\x89PNG\r\n\x1a\n"):
            raise InvalidProject(f"{label} の PNG が不正です")
        # Pillow may clip 16-bit grayscale during RGBA conversion. Never silently
        # alter those pixels: the browser rasterizes imports to ordinary 8-bit PNG.
        if len(raw) > 24 and raw[24] == 16:
            raise InvalidProject(f"{label} は 8-bit PNG に変換してください（16-bit PNG は未対応）")
        with Image.open(io.BytesIO(raw)) as source:
            if source.format != "PNG" or getattr(source, "n_frames", 1) != 1:
                raise InvalidProject(f"{label} は静止 PNG にしてください")
            w, h = source.size
            if w < 1 or h < 1 or w > 8192 or h > 8192 or w * h > MAX_IMAGE_PIXELS:
                raise InvalidProject(f"{label} は各辺 8192 px、合計 1600 万画素以下にしてください")
            source.load()  # Validate compressed data before starting a job.
            return source.convert("RGBA")
    except InvalidProject:
        raise
    except (ValueError, OSError, binascii.Error, Image.DecompressionBombError) as exc:
        raise InvalidProject(f"{label} の PNG を読み込めません") from exc


def validate_project(project: Any) -> dict:
    """Return an independent JSON snapshot; retain UI metadata for lossless reopen."""
    if not isinstance(project, dict):
        raise InvalidProject("project はオブジェクトにしてください")
    try:
        serialized = json.dumps(project, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(serialized.encode("utf-8")) > MAX_PROJECT_BYTES:
            raise InvalidProject("project は 32 MiB 以下にしてください")
        result = json.loads(serialized)
    except (TypeError, ValueError, RecursionError) as exc:
        raise InvalidProject("project の JSON が不正か、32 MiB を超えています") from exc
    if type(result.get("version")) is not int or result["version"] != 1:
        raise InvalidProject("対応する project version は 1 です")
    width = _number(result.get("width"), "width", 2, 1920, integer=True)
    height = _number(result.get("height"), "height", 2, 1080, integer=True)
    if width % 2 or height % 2:
        raise InvalidProject("MP4 の width / height は偶数にしてください")
    duration = _number(result.get("duration"), "duration", 0.1, 60)
    fps = _number(result.get("fps"), "fps", 1, 30, integer=True)
    result.update(width=width, height=height, duration=duration, fps=fps)
    layers = result.get("layers")
    if not isinstance(layers, list) or len(layers) > MAX_LAYERS:
        raise InvalidProject("layers は最大 16 個の配列にしてください")
    background = _png(result.get("background"), "background")
    total_pixels = background.width * background.height
    background.close()
    work_per_frame = width * height
    display_pixels = 0
    for index, layer in enumerate(layers):
        label = f"layers[{index}]"
        if not isinstance(layer, dict):
            raise InvalidProject(f"{label} はオブジェクトにしてください")
        name = layer.get("name", f"Layer {index + 1}")
        if not isinstance(name, str) or len(name) > 200:
            raise InvalidProject(f"{label}.name は 200 文字以内にしてください")
        layer["name"] = name
        _number(layer.get("x"), f"{label}.x", -3840, 5760)
        _number(layer.get("y"), f"{label}.y", -2160, 3240)
        lw = _number(layer.get("width"), f"{label}.width", 1, 3840)
        lh = _number(layer.get("height"), f"{label}.height", 1, 2160)
        layer["opacity"] = _number(layer.get("opacity", 1), f"{label}.opacity", 0, 1)
        layer.setdefault("visible", True)
        if not isinstance(layer["visible"], bool):
            raise InvalidProject(f"{label}.visible は true / false にしてください")
        motion = layer.setdefault("motion", {})
        if not isinstance(motion, dict):
            raise InvalidProject(f"{label}.motion はオブジェクトにしてください")
        for key, default, low, high in (
            ("sway_deg", 0, -180, 180), ("bob_px", 0, -1080, 1080),
            ("breathe_pct", 0, 0, 50), ("period", 3, 0.1, 60),
            ("phase", 0, -3600, 3600), ("start", 0, 0, duration),
            ("end", duration, 0, duration),
        ):
            motion[key] = _number(motion.get(key, default), f"{label}.motion.{key}", low, high)
        if motion["end"] < motion["start"]:
            raise InvalidProject(f"{label}.motion.end は start 以降にしてください")
        source = _png(layer.get("data_url"), f"{label}.data_url")
        total_pixels += source.width * source.height
        source.close()
        display_pixels += math.ceil(lw) * math.ceil(lh)
        # Bound the scaled, rotated bounding box, even when the layer is hidden.
        work_per_frame += (lw + lh) ** 2 * (1 + motion["breathe_pct"] / 100) ** 2
        if total_pixels > MAX_TOTAL_IMAGE_PIXELS or display_pixels > MAX_TOTAL_IMAGE_PIXELS:
            raise InvalidProject("画像の合計は 2400 万画素以下にしてください。素材を縮小してください")
    if work_per_frame * math.ceil(duration * fps) > MAX_WORK_PIXELS:
        raise InvalidProject("合成処理量が上限を超えます。尺・FPS・解像度・レイヤーサイズを減らしてください")
    if len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_PROJECT_BYTES:
        raise InvalidProject("project は 32 MiB 以下にしてください")
    return result


class Renderer:
    """Preload independent images once, then render using the browser's contract."""

    def __init__(self, project: dict):
        Image = _pil()
        self.project = project
        self.background = _png(project["background"], "background").resize(
            (project["width"], project["height"]), Image.Resampling.LANCZOS
        )
        self.layers = []
        for layer in project["layers"]:
            source = _png(layer["data_url"], layer["name"])
            self.layers.append((layer, source))

    def frame(self, t: float):
        Image = _pil()
        canvas = self.background.copy()
        for layer, source in self.layers:
            motion = layer["motion"]
            if not layer["visible"] or not motion["start"] <= t < motion["end"] or layer["opacity"] == 0:
                continue
            wave = math.sin(math.tau * ((t - motion["start"]) / motion["period"] + motion["phase"] / 360))
            scale = 1 + motion["breathe_pct"] / 100 * wave
            size = (max(1, round(layer["width"] * scale)), max(1, round(layer["height"] * scale)))
            sprite = source.resize(size, Image.Resampling.LANCZOS)
            angle = motion["sway_deg"] * wave
            if angle:
                sprite = sprite.rotate(-angle, resample=Image.Resampling.BICUBIC, expand=True)
            if layer["opacity"] != 1:
                sprite.putalpha(sprite.getchannel("A").point(lambda a: round(a * layer["opacity"])))
            x = round(layer["x"] - sprite.width / 2)
            y = round(layer["y"] + motion["bob_px"] * wave - sprite.height / 2)
            # Explicit crop makes partially/outside-canvas layers safe on every Pillow version.
            left, top = max(0, x), max(0, y)
            right, bottom = min(canvas.width, x + sprite.width), min(canvas.height, y + sprite.height)
            if right > left and bottom > top:
                visible = sprite.crop((left - x, top - y, right - x, bottom - y))
                canvas.alpha_composite(visible, (left, top))
        # MP4 has no alpha: composite transparent pixels against black consistently.
        output = Image.new("RGB", canvas.size, (0, 0, 0))
        output.paste(canvas, mask=canvas.getchannel("A"))
        return output

    def close(self):
        self.background.close()
        for _, source in self.layers:
            source.close()


def publish_unique(temporary: Path, destination: Path) -> None:
    """Reserve an unused name exclusively, then atomically replace only our empty file."""
    fd = os.open(str(destination), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        os.replace(temporary, destination)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def _stop_process(process):
    if process is None or process.poll() is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def render_mp4(project: dict, temporary: Path, cancel: threading.Event,
               progress: Callable[[float], None], on_process: Callable[[Any], None] = lambda p: None):
    """Write only the supplied temporary file; check cancellation even while stdin stalls."""
    executable = shutil.which("ffmpeg")
    if not executable:
        raise RuntimeError("ffmpeg が見つかりません。既存の ffmpeg 環境を確認してください")
    renderer = None
    process = None
    started = time.monotonic()
    def check():
        if cancel.is_set():
            raise ExportCancelled()
        if time.monotonic() - started > EXPORT_TIMEOUT:
            raise TimeoutError("書き出しが 15 分を超えたため停止しました。尺や解像度を減らしてください")
    try:
        check()
        renderer = Renderer(project)
        check()
        count = math.ceil(project["duration"] * project["fps"])
        command = [executable, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                   "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{project['width']}x{project['height']}",
                   "-r", str(project["fps"]), "-i", "pipe:0", "-an", "-c:v", "libx264",
                   "-preset", "veryfast", "-crf", "20", "-threads", "2", "-pix_fmt", "yuv420p",
                   "-movflags", "+faststart", "-f", "mp4", str(temporary)]
        with tempfile.TemporaryFile() as error_log:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=error_log)
            on_process(process)
            fd = process.stdin.fileno()
            os.set_blocking(fd, False)
            for index in range(count):
                check()
                frame = renderer.frame(index / project["fps"])
                data = memoryview(frame.tobytes())
                frame.close()
                while data:
                    check()
                    if process.poll() is not None:
                        raise RuntimeError("ffmpeg がフレーム受信前に終了しました")
                    if select.select([], [fd], [], 0.1)[1]:
                        try:
                            data = data[os.write(fd, data[:65536]):]
                        except BlockingIOError:
                            continue
                progress(round((index + 1) / count * 95, 2))
            process.stdin.close()
            while process.poll() is None:
                check()
                cancel.wait(0.1)
            check()
            if process.returncode != 0:
                error_log.seek(0)
                detail = error_log.read(2000).decode("utf-8", errors="replace").strip()
                raise RuntimeError(f"ffmpeg 終了コード {process.returncode}: {detail}")
        if not temporary.is_file() or temporary.stat().st_size < 100:
            raise RuntimeError("ffmpeg の完成 MP4 が確認できません")
    except (BrokenPipeError, OSError) as exc:
        check()
        raise RuntimeError(f"書き出しに失敗しました: {exc}") from exc
    finally:
        _stop_process(process)
        if process is not None and process.stdin and not process.stdin.closed:
            process.stdin.close()
        on_process(None)
        if renderer is not None:
            renderer.close()
