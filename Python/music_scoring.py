#!/usr/bin/env python3
"""楽曲テイクの採点（AI 選曲の土台）。

- measure(path): ffprobe / ffmpeg（silencedetect + ebur128）で尺・無音率・統合ラウドネス・トゥルーピークを測る。
- prompt_fit(title, prompt, llm): LLM にプロンプト適合度（0..1）を聞く。llm を渡さなければ None（測定値だけで採点）。
- score(measure, fit, policy): 方針（最小/最大尺、無音率上限、LUFS 範囲、重み）で合否と点数を出す。
外部プロセスと LLM は差し替え可能にし、テストでは呼ばない。
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

DEFAULT_POLICY: dict[str, Any] = {
    "min_sec": 120.0,
    "max_sec": 900.0,
    "max_silence_ratio": 0.15,
    "lufs_min": -20.0,
    "lufs_max": -8.0,
    "weights": {"duration": 0.3, "silence": 0.3, "loudness": 0.2, "prompt_fit": 0.2},
}

_SILENCE_RE = re.compile(r"silence_duration:\s*([0-9.]+)")
_LUFS_RE = re.compile(r"\bI:\s*(-?[0-9.]+)\s*LUFS")
_PEAK_RE = re.compile(r"Peak:\s*(-?[0-9.]+)\s*dBFS")


def policy_from(config: Optional[dict[str, Any]]) -> dict[str, Any]:
    """チャンネル設定の music_selection.policy を既定に重ねる。不正値は既定に戻す。"""
    out = json.loads(json.dumps(DEFAULT_POLICY))
    for k, v in (config or {}).items():
        if k == "weights" and isinstance(v, dict):
            for wk, wv in v.items():
                try:
                    out["weights"][wk] = max(0.0, float(wv))
                except (TypeError, ValueError):
                    pass
        elif k in out and k != "weights":
            try:
                out[k] = float(v)
            except (TypeError, ValueError):
                pass
    if out["min_sec"] > out["max_sec"]:
        out["min_sec"], out["max_sec"] = DEFAULT_POLICY["min_sec"], DEFAULT_POLICY["max_sec"]
    if out["lufs_min"] > out["lufs_max"]:
        out["lufs_min"], out["lufs_max"] = DEFAULT_POLICY["lufs_min"], DEFAULT_POLICY["lufs_max"]
    return out


def measure(path: Path, *, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe", timeout: int = 120) -> dict[str, Optional[float]]:
    """{duration_sec, silence_ratio, lufs, true_peak}。測れない項目は None。"""
    out: dict[str, Optional[float]] = {"duration_sec": None, "silence_ratio": None, "lufs": None, "true_peak": None}
    try:
        probe = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                                "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                               capture_output=True, text=True, timeout=timeout)
        out["duration_sec"] = float(probe.stdout.strip())
    except Exception:
        return out
    try:
        res = subprocess.run([ffmpeg, "-hide_banner", "-nostats", "-i", str(path),
                              "-af", "silencedetect=noise=-35dB:d=0.5,ebur128=peak=true", "-f", "null", "-"],
                             capture_output=True, text=True, timeout=timeout)
        text = res.stderr or ""
        silence = sum(float(m) for m in _SILENCE_RE.findall(text))
        if out["duration_sec"]:
            out["silence_ratio"] = max(0.0, min(1.0, silence / out["duration_sec"]))
        lufs = _LUFS_RE.findall(text)
        if lufs:
            out["lufs"] = float(lufs[-1])
        peak = _PEAK_RE.findall(text)
        if peak:
            out["true_peak"] = float(peak[-1])
    except Exception:
        pass
    return out


def prompt_fit(title: str, prompt: str, llm: Optional[Callable[[str], str]] = None, *, timeout: int = 120) -> Optional[float]:
    """曲名とプロンプトの適合度を LLM に 0..1 で答えさせる。llm が無い、または解釈不能なら None。"""
    if llm is None or not (prompt or "").strip():
        return None
    ask = ("次の曲名が、次の制作プロンプトにどれだけ合っているかを 0 から 1 の数値 1 つだけで答えてください。"
           "説明は不要です。\n曲名: " + str(title) + "\nプロンプト: " + str(prompt))
    try:
        text = llm(ask)
    except Exception:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", str(text or ""))
    if not m:
        return None
    try:
        return max(0.0, min(1.0, float(m.group(0))))
    except ValueError:
        return None


def score(m: dict[str, Any], fit: Optional[float], policy: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """合否と点数（0..100）。測れなかった項目は不合格にせず、その重みを配分から外す。"""
    p = policy_from(policy) if policy is not None and "weights" not in policy else (policy or policy_from(None))
    w = p["weights"]
    reasons: list[str] = []
    parts: list[tuple[float, float]] = []  # (weight, 0..1)
    passed = True

    d = m.get("duration_sec")
    if d is not None:
        if d < p["min_sec"]:
            passed = False
            reasons.append(f"尺が短い ({d:.0f}s < {p['min_sec']:.0f}s)")
            parts.append((w["duration"], 0.0))
        elif d > p["max_sec"]:
            passed = False
            reasons.append(f"尺が長い ({d:.0f}s > {p['max_sec']:.0f}s)")
            parts.append((w["duration"], 0.0))
        else:
            span = max(1.0, p["max_sec"] - p["min_sec"])
            center = (p["min_sec"] + p["max_sec"]) / 2
            parts.append((w["duration"], 1.0 - min(1.0, abs(d - center) / span)))
    else:
        reasons.append("尺を測れなかった")

    s = m.get("silence_ratio")
    if s is not None:
        if s > p["max_silence_ratio"]:
            passed = False
            reasons.append(f"無音が多い ({s:.2f} > {p['max_silence_ratio']:.2f})")
            parts.append((w["silence"], 0.0))
        else:
            parts.append((w["silence"], 1.0 - s / max(p["max_silence_ratio"], 1e-9)))
    else:
        reasons.append("無音率を測れなかった")

    l = m.get("lufs")
    if l is not None:
        if l < p["lufs_min"] or l > p["lufs_max"]:
            passed = False
            reasons.append(f"ラウドネスが範囲外 ({l:.1f} LUFS)")
            parts.append((w["loudness"], 0.0))
        else:
            span = max(1.0, p["lufs_max"] - p["lufs_min"])
            center = (p["lufs_min"] + p["lufs_max"]) / 2
            parts.append((w["loudness"], 1.0 - min(1.0, abs(l - center) / span)))
    else:
        reasons.append("ラウドネスを測れなかった")

    if fit is not None:
        parts.append((w["prompt_fit"], max(0.0, min(1.0, float(fit)))))

    total_w = sum(pw for pw, _ in parts)
    value = round(100.0 * sum(pw * pv for pw, pv in parts) / total_w, 1) if total_w > 0 else 0.0
    if not parts:
        passed = False
        reasons.append("採点できる項目がない")
    return {"score": value, "passed": passed, "reasons": reasons}
