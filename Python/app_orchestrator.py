#!/usr/bin/env python3
"""Automation Studio オーケストレーター（Phase 4 試作）。

設計: AGENTS_DESIGN.md §7（StageWorker 共通I/F）/ §9（確定事項）。

狙い:
  現行の「app.py の _job_* が pipeline を subprocess 起動し、stdout 解析で次を決める」
  中央集権モデルの上に、**台帳(runs.db)を黒板にした依存解決レイヤ**を載せる。
  各ドメインワーカーが「自分が動かせる vol」を can_run() で自己判定し、run() で
  既存 step_*（app_pipeline.STEP_FUNCS）をそのまま呼ぶ。ロジックは作り直さない。

確定事項（§9）:
  - 常駐方式 = APScheduler 定期ジョブ（本モジュールの evaluate() を定期 tick で呼ぶ想定）。
  - 能動トリガー = 空枠作成まで。SUNO 生成の自動実行はしない（prompt 合意は人間）。
  - quota 残 = .youtube_quota.json 台帳 + 403 検知のハイブリッド（app_youtube 側を再利用）。
  - 置き場 = 本ファイル（app.py を肥大化させない）。
  - まず 1 ドメイン（qa）を試作 → 挙動確認 → 残ドメインへ展開。

本ファイルは**スタンドアロンで import / dry-run 可能**。app.py への APScheduler 登録は次段。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Callable, Optional

# 既存資産（ロジックは再利用、作り直さない）
import app_pipeline as _pipe
try:
    import app_run_ledger as _ledger
except Exception:  # pragma: no cover - ledger 不在環境でも import は通す
    _ledger = None


# ─── sentinel exit（app_pipeline と一致させる） ───
class Exit(IntEnum):
    OK = 0
    FAIL = 1
    UNATTENDED_LOGIN = 75
    RETRYABLE = 76
    QUOTA_EXHAUSTED = 77
    PREFLIGHT_FAIL = 78


# step_* の戻り値（True / False / "retryable" / "unattended_login" / "quota_exhausted"）
# を Exit に正規化する。
def _normalize_result(r) -> Exit:
    if r is True:
        return Exit.OK
    if r == "unattended_login":
        return Exit.UNATTENDED_LOGIN
    if r == "quota_exhausted":
        return Exit.QUOTA_EXHAUSTED
    if r == "retryable":
        return Exit.RETRYABLE
    return Exit.FAIL


class Action(IntEnum):
    """on_fail の判断結果。"""
    RETRY = 1          # 同 stage を retry（RETRY_POLICY に従う）
    AUTO_RESUME = 2    # 前段へ差し戻して再開（_RESUME_OVERRIDE 考慮）
    NOTIFY = 3         # 人手必要（75/77/78）。retry/resume せず通知のみ
    DONE = 4           # 成功


# STEPS の成果物判定（フォルダにこれがあれば「その stage は実体として完了」とみなす補助）。
# 台帳が正典だが、台帳に記録の無い手動実行ぶんを拾うための二次情報。
def _has_export_mp4(folder: Path) -> bool:
    if not folder or not folder.exists():
        return False
    return bool(next(iter(folder.glob("*vol*.mp4")), None) or next(iter(folder.glob("*.mp4")), None))


@dataclass
class StageWorker:
    """ドメイン別ワーカーの基底（AGENTS_DESIGN §7.2）。

    既存 step_* を run() で呼ぶだけ。新規価値は can_run()（依存解決）と record()。
    """
    domain: str
    stages: list[str]

    # ── 依存解決 ──
    def prev_stage(self, stage: str) -> Optional[str]:
        """STEPS 上で stage の直前 stage を返す（先頭なら None）。"""
        steps = _pipe.STEPS
        if stage not in steps:
            return None
        i = steps.index(stage)
        return steps[i - 1] if i > 0 else None

    def can_run(self, vol: int, folder: Path, *, channel_id: str = "") -> bool:
        """担当の先頭 stage について「前段 done かつ自 stage 未完了」かを判定。

        台帳(runs.db)を一次情報、フォルダ成果物を二次情報に使う。
        サブクラスで stage 固有の成果物チェックを足してよい。
        """
        head = self.stages[0]
        prev = self.prev_stage(head)
        # 前段が無い（=パイプライン先頭）なら、常に着手候補（空枠起点）。
        prev_done = True if prev is None else self._stage_done(vol, prev, folder, channel_id=channel_id)
        self_done = self._stage_done(vol, head, folder, channel_id=channel_id)
        return prev_done and not self_done

    def _stage_done(self, vol: int, stage: str, folder: Path, *, channel_id: str = "") -> bool:
        """台帳優先で stage 完了を判定。台帳に無ければフォルダ成果物で補う。"""
        # 台帳: 同 vol の done run で failed_stage がその stage 以降に達していれば完了とみなす
        if _ledger is not None:
            try:
                runs = _ledger.list_runs(channel_id=channel_id or None, vol=vol, limit=20)
            except Exception:
                runs = []
            for r in runs:
                if r.get("status") == "done":
                    # done run は full pipeline 完走 or --from 再開完走。stage 到達済みとみなす。
                    return True
        # フォルダ成果物による補助判定（export は mp4）
        if stage == "export":
            return _has_export_mp4(folder)
        return False

    # ── 実行 ──
    def run(self, vol: int, folder: Path, *, via_api: bool = False, **kw) -> Exit:
        """担当 stage を順に実行（既存 STEP_FUNCS を呼ぶ）。最初の失敗で打ち切り。"""
        for stage in self.stages:
            func = _pipe.STEP_FUNCS.get(stage)
            if func is None:
                return Exit.FAIL
            r = func(vol, folder, via_api, **kw)
            code = _normalize_result(r)
            if code != Exit.OK:
                return code
        return Exit.OK

    # ── 失敗判断 ──
    def on_fail(self, code: Exit, stage: str) -> Action:
        if code == Exit.OK:
            return Action.DONE
        if code in (Exit.UNATTENDED_LOGIN, Exit.QUOTA_EXHAUSTED, Exit.PREFLIGHT_FAIL):
            return Action.NOTIFY
        if code == Exit.RETRYABLE:
            return Action.RETRY
        # 一般失敗。_RESUME_OVERRIDE に差し戻し先があれば前段再開、無ければ通知。
        override = getattr(_pipe, "_RESUME_OVERRIDE", {})
        if stage in override:
            return Action.AUTO_RESUME
        return Action.NOTIFY

    def resume_stage(self, stage: str) -> str:
        """差し戻し先 stage（_RESUME_OVERRIDE 考慮。例 qa→premiere）。"""
        return getattr(_pipe, "_RESUME_OVERRIDE", {}).get(stage, stage)

    # ── 台帳記録 ──
    def record_start(self, *, vol: int, channel_id: str, channel_folder: str,
                     channel_name: str, video_name: str) -> Optional[str]:
        if _ledger is None:
            return None
        try:
            return _ledger.start_run(
                kind="manual", channel_id=channel_id, channel_folder=channel_folder,
                channel_name=channel_name, vol=vol, video_name=video_name,
                meta={"orchestrator": self.domain},
            )
        except Exception:
            return None

    def record_finish(self, run_id: Optional[str], code: Exit, *, failed_stage: str = "",
                      summary: str = "") -> None:
        if _ledger is None or not run_id:
            return
        try:
            _ledger.finish_run(
                run_id,
                status="done" if code == Exit.OK else "failed",
                exit_code=int(code),
                failed_stage=failed_stage,
                summary=summary,
            )
        except Exception:
            pass


# ─── 具象ワーカー（まず qa を試作。§9.1 実装順 1） ───
class QAWorker(StageWorker):
    """export 後の MP4 を ffprobe 検証する qa-worker。

    既存 step_qa（app_pipeline）をそのまま run() で呼ぶ。NG 時は _RESUME_OVERRIDE に
    従って premiere へ差し戻す（on_fail → Action.AUTO_RESUME）。
    """
    def __init__(self):
        super().__init__(domain="qa", stages=["qa"])

    def can_run(self, vol: int, folder: Path, *, channel_id: str = "") -> bool:
        # qa は export 済み（mp4 存在）かつ未検証（qa_report.json 無し）が条件。
        # passed:false の report が残っている間も候補外（再dispatchループ防止）。
        # 人間が原因を解消して qa_report.json を削除すると再検査対象に戻る。
        if not _has_export_mp4(folder):
            return False
        if (folder / "qa_report.json").exists():
            return False
        return True


# ─── stage 別 成果物検出（can_run の依存解決の地に足のついた根拠） ───
# 台帳が一次情報だが、手動実行ぶんを拾うためフォルダ成果物も見る。
# premiere だけはクリーンなファイル成果物が無い（Premiere プロジェクト内状態）ため
# export mp4 を proxy にしつつ台帳併用。
def _img_has(folder: Path, vol: int, *names: str) -> bool:
    img = folder / "Image"
    for n in names:
        if (folder / n).exists() or (img / n).exists():
            return True
    return False


def _bgimage_done(folder: Path, vol: int) -> bool:
    return _img_has(folder, vol, f"vol{vol}.png", f"vol{vol}.jpg")


def _psd_done(folder: Path, vol: int) -> bool:
    # template_psd 未設定なら step_psd_composite は no-op（True 即返し）→ 完了扱い。
    try:
        if not _pipe._should_run_psd(folder):
            return True
    except Exception:
        pass
    return _img_has(folder, vol, "サムネイル.jpg", f"vol{vol}.jpg")


def _premiere_done(folder: Path, vol: int) -> bool:
    # ファイル成果物なし。export mp4 があれば premiere は確実に完了済み（台帳でも補完）。
    return _has_export_mp4(folder)


def _export_done(folder: Path, vol: int) -> bool:
    return _has_export_mp4(folder)


def _qa_done(folder: Path, vol: int) -> bool:
    # qa_report.json があり passed:true のみ done。passed:false（不合格/検査不能）は
    # done でも候補でもない保留状態＝人間が原因を直して qa_report.json を消すまで
    # 後続 stage に進めない（壊れ mp4 で report 無し→再dispatchループ→ブレーカー
    # 発動した vol87 の再発防止）。
    p = folder / "qa_report.json"
    if not p.exists():
        return False
    try:
        return bool(json.loads(p.read_text(encoding="utf-8")).get("passed", False))
    except Exception:
        return False


def _meta_done(folder: Path, vol: int) -> bool:
    # step_meta は youtube_{title,description,tags}.txt を書き出す（title を代表に判定）。
    return (folder / "youtube_title.txt").exists()


def _thumbnail_done(folder: Path, vol: int) -> bool:
    return _img_has(folder, vol, "サムネイル.jpg", f"vol{vol}.jpg")


_ARTIFACT_DONE = {
    "bgimage": _bgimage_done,
    "psd_composite": _psd_done,
    "premiere": _premiere_done,
    "export": _export_done,
    "qa": _qa_done,
    "meta": _meta_done,
    "thumbnail": _thumbnail_done,
}


def _ledger_stage_done(stage: str, vol: int, channel_id: str = "") -> bool:
    """台帳に「その stage の done run」があるか。orchestrator 単一stage run は
    meta_json に stage 名を含む。full-pipeline の done run（meta 空）も完了とみなす。"""
    if _ledger is None:
        return False
    try:
        runs = _ledger.list_runs(channel_id=channel_id or None, vol=vol, limit=30)
    except Exception:
        return False
    for r in runs:
        if r.get("status") == "done":
            mj = r.get("meta_json") or ""
            if (f'"{stage}"' in mj) or (mj == ""):
                return True
    return False


def _stage_artifact_done(stage: str, folder: Path, vol: int, channel_id: str = "") -> bool:
    fn = _ARTIFACT_DONE.get(stage)
    if fn and fn(folder, vol):
        return True
    return _ledger_stage_done(stage, vol, channel_id)


@dataclass
class StepWorker(StageWorker):
    """1 stage = 1 ワーカーの汎用実装。can_run は _stage_artifact_done で
    「前段 done × 自 stage 未完」を判定。STEPS 上で image ドメイン(bgimage/psd/
    thumbnail)は隣接しないため、ドメイン単位ではなく stage 単位が依存解決に正しい。
    domain_label は表示上のグルーピング(image/video/publish)。"""
    domain_label: str = ""

    def can_run(self, vol: int, folder: Path, *, channel_id: str = "") -> bool:
        head = self.stages[0]
        prev = self.prev_stage(head)
        prev_done = True if prev is None else _stage_artifact_done(prev, folder, vol, channel_id=channel_id)
        self_done = _stage_artifact_done(head, folder, vol, channel_id=channel_id)
        return prev_done and not self_done


# ─── plan ワーカー（P3-2 = plan 自動採択。§9-2「空枠作成まで」） ───
class PlanWorker(StageWorker):
    """ベンチマーク分析 → 次動画プラン(plan.json)を生成する plan-worker。

    既存 step_plan（app_pipeline）をそのまま run() で呼ぶ。step_plan は plan.json を
    書き出すだけで **SUNO 生成は実行しない**（後続 step_suno が plan.json を優先利用）。
    よって本ワーカーは §9-2「空枠作成まで」の範囲に収まる（音源生成は人間が prompt
    合意後に起動）。plan は STEPS（10工程）には含まれず STEPS_WITH_PLAN の先頭なので
    prev_stage は None＝依存なし。plan.json があれば can_run=False（再生成は手動削除）。
    """
    def __init__(self):
        super().__init__(domain="plan", stages=["plan"])

    def prev_stage(self, stage: str) -> Optional[str]:
        return None  # plan は最先頭。依存なし。

    def can_run(self, vol: int, folder: Path, *, channel_id: str = "") -> bool:
        if (folder / "plan.json").exists():
            return False  # 既存 plan は再利用（再生成は手動削除）
        return True


# ワーカーレジストリ（stage 単位）。
# ⚠ upload は **意図的に含めない**（最終投稿は手動ゲート。ポリシー §1）。
# ⚠ suno / rename も含めない（music ドメインは人間が prompt 合意後に起動。§9-2）。
# ⚠ plan も WORKERS には含めない（autopilot で全 vol に勝手に plan 生成しないため）。
#    plan は ALL_WORKERS / PLAN_WORKER 経由で明示的に評価する。
# まず全 stage を登録し、autopilot で実際に tick する範囲は app.py 統合時に
# per-channel 設定で絞る（既定 = export〜thumbnail）。
WORKERS: dict[str, StageWorker] = {
    "bgimage":       StepWorker(domain="bgimage", stages=["bgimage"], domain_label="image"),
    "psd_composite": StepWorker(domain="psd_composite", stages=["psd_composite"], domain_label="image"),
    "premiere":      StepWorker(domain="premiere", stages=["premiere"], domain_label="video"),
    "export":        StepWorker(domain="export", stages=["export"], domain_label="video"),
    "qa":            QAWorker(),
    "meta":          StepWorker(domain="meta", stages=["meta"], domain_label="publish"),
    "thumbnail":     StepWorker(domain="thumbnail", stages=["thumbnail"], domain_label="image"),
}

# plan を含む全ワーカー（plan 自動採択を明示的に走らせたい時に evaluate(workers=ALL_WORKERS)）。
PLAN_WORKER = PlanWorker()
ALL_WORKERS: dict[str, StageWorker] = {"plan": PLAN_WORKER, **WORKERS}

# autopilot 既定範囲（app.py 統合時に tick 対象を絞るためのヒント）。
# plan を入れると「次 vol を自動起案」になるが、§9-2 で空枠作成までは許容範囲。
# ただし既定では含めない（明示 opt-in）。
AUTOPILOT_DEFAULT_STAGES = ["export", "qa", "meta", "thumbnail"]


# ─── オーケストレーション tick（APScheduler 定期ジョブから呼ぶ想定） ───
@dataclass
class Candidate:
    domain: str
    vol: int
    folder: str
    channel_id: str = ""


def evaluate(channels: list[dict], *, dry_run: bool = True,
             workers: Optional[dict] = None) -> list[Candidate]:
    """各 channel × 各 worker で can_run を評価し、実行可能タスク集合を返す。

    channels: [{"channel_id":..., "folder":..., "name":..., "vols":[{"vol":int,"folder":str}, ...]}]
    dry_run=True なら候補列挙のみ（run() は呼ばない）。実投入は app.py 統合時に行う。
    workers: 評価対象の worker dict（既定 WORKERS）。plan を含めたい時は ALL_WORKERS を渡す。
    """
    pool = workers if workers is not None else WORKERS
    candidates: list[Candidate] = []
    for ch in channels:
        ch_id = ch.get("channel_id", "")
        for v in ch.get("vols", []):
            vol = int(v.get("vol", 0))
            folder = Path(v.get("folder", ""))
            for domain, worker in pool.items():
                try:
                    if worker.can_run(vol, folder, channel_id=ch_id):
                        candidates.append(Candidate(domain=domain, vol=vol,
                                                    folder=str(folder), channel_id=ch_id))
                except Exception:
                    continue
    return candidates


# ─── サーキットブレーカー（暴走防止・ポリシー §3） ───
# 同一チャンネルで連続 BREAKER_THRESHOLD 回 failed したら、そのチャンネルの
# 自動投入を止める。状態は専用ストアを持たず **台帳 list_runs から都度算出**する
# （永続 state を増やさない。台帳が真実の単一ソース）。
BREAKER_THRESHOLD = int(os.environ.get("APP_ORCH_BREAKER_THRESHOLD", "3"))


def consecutive_failures(channel_id: str, *, lookback: int = 20) -> int:
    """指定チャンネルの「直近の連続 failed 数」を台帳から算出。

    新しい run から順に見て、done/cancelled に当たったら 0 にリセット（連続が切れる）、
    failed が続く限りカウント。orchestrator 由来でない run（手動 vol_create 等）も
    チャンネルの健全性指標として一律にカウントする（quota 枯渇等はどの経路でも failed）。
    """
    if _ledger is None or not channel_id:
        return 0
    try:
        runs = _ledger.list_runs(channel_id=channel_id, limit=lookback)
    except Exception:
        return 0
    # list_runs は started_at DESC（新しい順）。先頭から連続 failed を数える。
    n = 0
    for r in runs:
        st = r.get("status")
        if st == "failed":
            n += 1
        elif st in ("done", "cancelled"):
            break  # 直近に成功/取消があれば連続は切れている
        else:
            # in_progress / reconstructed は判定保留＝連続を切らずスキップ
            continue
    return n


def is_channel_tripped(channel_id: str) -> bool:
    """ブレーカーが落ちている（自動投入を止めるべき）か。"""
    return consecutive_failures(channel_id) >= BREAKER_THRESHOLD


# ─── 実投入（dispatch）── ポリシー §2: シャドウ無し。範囲=export〜thumbnail。 ───
# ⚠ この関数を呼ぶと **実際に step_* が実行される**。ただし本モジュールは
# APScheduler に未登録なので、明示的に dispatch() を呼ばない限り無人稼働しない。
# upload ワーカーは WORKERS に存在しない＝自動投稿はしない（最終 upload は手動ゲート）。
def dispatch(candidate: "Candidate", channel: dict, *, via_api: bool = False,
             notify: Optional[Callable[[str], None]] = None, **kw) -> dict:
    """1 候補を実行: ブレーカー確認 → 台帳 start → run → on_fail 分岐 → 台帳 finish。

    Returns: {"status": "tripped|done|failed|notify|resume|retry", "code": int, ...}
    notify: 人手要時に呼ぶコールバック（app.py 統合時に _notify_line を渡す想定）。
    """
    ch_id = channel.get("channel_id", "")
    ch_folder = channel.get("folder", "")
    ch_name = channel.get("name", "") or "(unknown)"
    worker = WORKERS.get(candidate.domain)
    if worker is None:
        return {"status": "failed", "code": int(Exit.FAIL), "error": f"unknown domain {candidate.domain}"}

    folder = Path(candidate.folder)
    head_stage = worker.stages[0]

    # ブレーカー: 連続失敗が閾値以上ならスキップ＋通知（run() を呼ばない）
    if is_channel_tripped(ch_id):
        msg = (f"⛔ [{ch_name}] 自動投入を停止中（連続 {consecutive_failures(ch_id)} 回失敗 "
               f"≥ {BREAKER_THRESHOLD}）。手動で原因解消後に再開してください。")
        if notify:
            try: notify(msg)
            except Exception: pass
        return {"status": "tripped", "code": int(Exit.FAIL), "channel": ch_name,
                "consecutive_failures": consecutive_failures(ch_id)}

    # 台帳: start
    run_id = worker.record_start(
        vol=candidate.vol, channel_id=ch_id, channel_folder=ch_folder,
        channel_name=ch_name, video_name=folder.name,
    )

    # 実行
    try:
        code = worker.run(candidate.vol, folder, via_api=via_api, **kw)
    except Exception as e:
        worker.record_finish(run_id, Exit.FAIL, failed_stage=head_stage, summary=f"例外: {e}")
        if notify:
            try: notify(f"❌ [{ch_name}] vol.{candidate.vol} {candidate.domain} 例外: {str(e)[:120]}")
            except Exception: pass
        return {"status": "failed", "code": int(Exit.FAIL), "error": str(e)}

    # 成功
    if code == Exit.OK:
        worker.record_finish(run_id, code, summary=f"{candidate.domain} OK")
        return {"status": "done", "code": int(code), "vol": candidate.vol, "domain": candidate.domain}

    # 失敗 → on_fail 分岐
    action = worker.on_fail(code, head_stage)
    worker.record_finish(run_id, code, failed_stage=head_stage,
                         summary=f"{candidate.domain} {code.name} → {action.name}")
    result = {"status": action.name.lower(), "code": int(code),
              "vol": candidate.vol, "domain": candidate.domain, "action": action.name}

    if action == Action.AUTO_RESUME:
        # 前段へ差し戻し（例 qa→premiere）。実際の再開投入は app.py 統合時に
        # 既存 _schedule_resume / auto_resume に委ねる。ここでは差し戻し先を返すだけ。
        result["resume_stage"] = worker.resume_stage(head_stage)
        if notify:
            try: notify(f"↩ [{ch_name}] vol.{candidate.vol} {head_stage} 不良 → "
                        f"{result['resume_stage']} へ差し戻し要")
            except Exception: pass
    elif action == Action.NOTIFY:
        reason = {Exit.UNATTENDED_LOGIN: "手動ログイン要", Exit.QUOTA_EXHAUSTED: "quota 枯渇",
                  Exit.PREFLIGHT_FAIL: "preflight 失敗"}.get(code, "要対応")
        if notify:
            try: notify(f"⚠ [{ch_name}] vol.{candidate.vol} {candidate.domain} 中断（{reason}）")
            except Exception: pass
    elif action == Action.RETRY:
        # retry は既存 RETRY_POLICY に従い app.py 統合時に再投入。ここでは印のみ。
        pass
    return result


# ─── policy-aware 配分（P3-4。quota 残 × channel 優先度） ───
# upload を消費する stage（quota チェックが要る stage）。現状 WORKERS に upload は
# 無いが、将来 publish 系を autopilot に入れた時のガードとして定義しておく。
_QUOTA_CONSUMING_STAGES = {"upload"}


def channel_priority(channel: dict) -> int:
    """channel の優先度（大きいほど優先）。channels.json / per-channel 設定の
    `priority`（既定 100）。未設定は 100。"""
    try:
        return int(channel.get("priority", 100))
    except (TypeError, ValueError):
        return 100


def channel_quota_remaining(channel: dict) -> int:
    """channel の YouTube quota 残（unit）。app_youtube の台帳から算出。
    取得できなければ十分大きい値（=制約なし）を返す。"""
    folder = channel.get("folder", "")
    if not folder:
        return 10 ** 9
    try:
        import app_youtube as _yt
        used = _yt.quota_used_in_window(folder)
        cap = getattr(_yt, "DEFAULT_DAILY_QUOTA_CAP", 9600)
        return max(0, cap - used)
    except Exception:
        return 10 ** 9


def _quota_ok_for(channel: dict, domain: str) -> bool:
    """その domain を channel で動かして quota 的に問題ないか。
    quota を消費しない stage は常に True。"""
    if domain not in _QUOTA_CONSUMING_STAGES:
        return True
    try:
        import app_youtube as _yt
        per = getattr(_yt, "QUOTA_PER_UPLOAD", 1600)
    except Exception:
        per = 1600
    return channel_quota_remaining(channel) >= per


def prioritize(candidates: list[Candidate], channels: list[dict]) -> list[Candidate]:
    """候補を policy で並べ替え（priority 降順 → quota 残 多い順 → vol 昇順）。
    quota 枯渇チャンネルの quota 消費 stage は除外する。"""
    by_id = {c.get("channel_id", ""): c for c in channels}
    kept: list[tuple] = []
    for cand in candidates:
        ch = by_id.get(cand.channel_id, {})
        if not _quota_ok_for(ch, cand.domain):
            continue  # quota 枯渇 → この候補は今回スキップ
        kept.append((
            -channel_priority(ch),            # priority 降順
            -channel_quota_remaining(ch),     # quota 残 多い順
            cand.vol,                         # vol 昇順（古い vol を先に仕上げる）
            cand,
        ))
    kept.sort(key=lambda t: (t[0], t[1], t[2]))
    return [t[3] for t in kept]


def tick(channels: list[dict], *, notify: Optional[Callable[[str], None]] = None,
         via_api: bool = False, max_dispatch: int = 8,
         workers: Optional[dict] = None) -> dict:
    """1 周期: evaluate → policy 並べ替え → ブレーカー非該当を dispatch。

    policy-aware（P3-4）: channel 優先度 × quota 残で候補を並べ替え、quota 枯渇
    チャンネルの quota 消費 stage は除外。
    ⚠ APScheduler 定期ジョブから呼ぶ想定。**現状どこからも呼ばれていない**
    （app.py 未登録 = 無人稼働しない）。app.py 統合は別途 GO 後。
    """
    cands = evaluate(channels, dry_run=True, workers=workers)
    ordered = prioritize(cands, channels)
    by_id = {c.get("channel_id", ""): c for c in channels}
    results = []
    dispatched = 0
    skipped_quota = len(cands) - len(ordered)
    for cand in ordered:
        if dispatched >= max_dispatch:
            break
        ch = by_id.get(cand.channel_id)
        if ch is None:
            continue
        if is_channel_tripped(cand.channel_id):
            results.append({"status": "tripped", "vol": cand.vol, "domain": cand.domain})
            continue
        results.append(dispatch(cand, ch, via_api=via_api, notify=notify))
        dispatched += 1
    return {"evaluated": len(cands), "dispatched": dispatched,
            "skipped_quota": skipped_quota, "results": results}


if __name__ == "__main__":
    import json
    import sys
    # スモークテスト: STEPS と WORKERS の整合、依存解決の単体確認。
    print("=== app_orchestrator smoke ===")
    print("STEPS:", _pipe.STEPS)
    print("WORKERS:", list(WORKERS))
    qa = WORKERS["qa"]
    print("qa.prev_stage('qa') =", qa.prev_stage("qa"), "(期待: export)")
    print("qa.resume_stage('qa') =", qa.resume_stage("qa"), "(期待: premiere)")
    print("on_fail(FAIL,'qa') =", qa.on_fail(Exit.FAIL, "qa").name, "(期待: AUTO_RESUME)")
    print("on_fail(RETRYABLE,'qa') =", qa.on_fail(Exit.RETRYABLE, "qa").name, "(期待: RETRY)")
    print("on_fail(QUOTA_EXHAUSTED,'qa') =", qa.on_fail(Exit.QUOTA_EXHAUSTED, "qa").name, "(期待: NOTIFY)")
    # サーキットブレーカーのロジック単体確認（台帳に触れずモックで検証）
    print("--- breaker logic ---")
    def _mock_consecutive(runs):
        n = 0
        for r in runs:
            st = r.get("status")
            if st == "failed": n += 1
            elif st in ("done", "cancelled"): break
            else: continue
        return n
    # 新しい順: [failed,failed,failed] → 3（落ちる）
    assert _mock_consecutive([{"status":"failed"}]*3) == 3
    # [failed,done,failed] → 1（doneで連続が切れる）
    assert _mock_consecutive([{"status":"failed"},{"status":"done"},{"status":"failed"}]) == 1
    # [in_progress,failed,failed] → in_progressはskip、2
    assert _mock_consecutive([{"status":"in_progress"},{"status":"failed"},{"status":"failed"}]) == 2
    # [done,...] → 0（先頭成功）
    assert _mock_consecutive([{"status":"done"},{"status":"failed"}]) == 0
    print("breaker logic asserts: PASS（threshold=%d）" % BREAKER_THRESHOLD)
    # _qa_done / can_run: passed:true のみ done。passed:false は保留（候補にも done にもならない）
    print("--- qa_done logic ---")
    import tempfile
    with tempfile.TemporaryDirectory() as _td:
        _tp = Path(_td)
        assert _qa_done(_tp, 1) is False, "report 無し → not done"
        (_tp / "test_vol1.mp4").write_bytes(b"x")
        assert WORKERS["qa"].can_run(1, _tp) is True, "mp4 あり + report 無し → 候補"
        (_tp / "qa_report.json").write_text('{"passed": false, "issues": ["x"]}', encoding="utf-8")
        assert _qa_done(_tp, 1) is False, "passed:false → not done（保留）"
        assert WORKERS["qa"].can_run(1, _tp) is False, "passed:false → 候補外（再dispatchしない）"
        (_tp / "qa_report.json").write_text('{"passed": true}', encoding="utf-8")
        assert _qa_done(_tp, 1) is True, "passed:true → done"
    print("qa_done logic asserts: PASS")
    print("WORKERS に upload 無し（自動投稿しない）:", "upload" not in WORKERS)
    # policy-aware 配分の単体確認
    print("--- policy ---")
    chs = [
        {"channel_id": "a", "name": "A", "folder": "", "priority": 50},
        {"channel_id": "b", "name": "B", "folder": "", "priority": 200},
    ]
    cs = [Candidate(domain="qa", vol=3, folder="/x", channel_id="a"),
          Candidate(domain="qa", vol=1, folder="/y", channel_id="b")]
    order = prioritize(cs, chs)
    print("priority order (B=200 が先):", [(c.channel_id, c.vol) for c in order])
    assert order[0].channel_id == "b", "priority 降順が効いていない"
    # quota 枯渇ガード: upload 系は quota チェックされる（domain=upload は WORKERS に無いが関数単体で確認）
    print("_quota_ok_for(qa)=", _quota_ok_for({"folder": ""}, "qa"), "(期待 True: 非quota stage)")
    print("PLAN_WORKER 単独:", PLAN_WORKER.domain, "/ ALL_WORKERS に plan:", "plan" in ALL_WORKERS)
    # dry-run 評価（引数で channels JSON を渡せる）
    if len(sys.argv) > 1:
        chans = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        cands = evaluate(chans, dry_run=True)
        print("candidates:", [(c.domain, c.vol) for c in cands])
    print("=== OK ===")
