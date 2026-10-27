"""段階 5 より前の step_thumbnail が使っていたサムネ評価ループ（段階 7 で app_pipeline.py から archive へ移した）。
現在の選定は Python/thumbnail_selection.py。この関数はどこからも呼ばれていない。単体では import できない（app_pipeline の名前を前提にしている）。
"""


def _run_thumbnail_eval_loop(vol: int, folder: Path, out_dir: Path,
                             final_path: Path, prompt_base: str,
                             gen_refs: list | None = None):
    """D5: 自己評価付き反復サムネ生成ループ（APP_THUMB_EVAL_LOOP=1 時のみ）。

    既存の codex 生成を generate_fn、score_thumbnails を score_fn に包んで
    app_image_eval_loop.run_eval_loop に注入する。合格画像（無ければ best）を
    thumbnail.png に昇格する。

    戻り:
      True / False = ループは走った（昇格できたか否か。いずれも upload は止めない）
      None         = ループ自体が使えない（モジュール import 失敗等）→ 呼び出し側で従来生成にフォールバック
    """
    try:
        from app_image_eval_loop import run_eval_loop, GenResult, ScoreResult
        from app_thumbnail_scoring import (
            score_thumbnails, judge_auto_approval, status_counts, best_of,
        )
    except Exception as e:
        print(f"  ⚠ eval-loop モジュール import 失敗: {e}")
        return None

    cfg = _load_dashboard_config()
    persona = (cfg.get("persona") or "").strip()
    channel_name = (cfg.get("channel_name") or "").strip()
    cli = _load_suno_config().get("claude_cli", "claude")

    # 採点用の文脈（title=youtube_title.txt → fallback folder 名 / concept=concept.txt）
    video_title = ""
    title_file = folder / "youtube_title.txt"
    if title_file.exists():
        try:
            lines = [ln for ln in title_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
            video_title = lines[0].strip() if lines else ""
        except Exception:
            video_title = ""
    if not video_title:
        video_title = folder.name
    video_concept = ""
    concept_file = folder / "concept.txt"
    if concept_file.exists():
        try:
            video_concept = concept_file.read_text(encoding="utf-8").strip()
        except Exception:
            video_concept = ""

    # 生成参照: 各チャンネルルート/「ベンチマーク」フォルダから 3 枚（既定）を共有ヘルパで収集
    #   （Picked 1 枚固定をやめ、フォルダ指定＋複数枚＋2台 Mac 移植解決に統一）。
    # 採点用の競合参照（ref_paths）とサムネ軸は従来どおり Picked / キャッシュから。
    ref_paths: list = []
    gen_refs = list(gen_refs or [])
    thumbnail_axis: dict = {}
    try:
        _eval_cfg = _load_dashboard_config()
        try:
            _eval_refcount = int(os.environ.get("APP_THUMBNAIL_REFCOUNT")
                                 or os.environ.get("APP_BGIMAGE_REFCOUNT", "3"))
        except ValueError:
            _eval_refcount = 3
        if not gen_refs:
            gen_refs = _gather_reference_images(_eval_cfg, folder, _eval_refcount)
    except Exception as e:
        print(f"  ⚠ eval-loop 生成参照（ベンチマーク3枚）の収集失敗: {e}")
    try:
        import app_benchmark_thumbnail as _bt
        picked = _bt.get_picked_paths(limit=4) or []
        ref_paths = [Path(p) for p in picked if Path(p).exists()]
        tc = _bt.load_cache() or {}
        thumbnail_axis = ((tc.get("analysis") or {}).get("aggregate") or {}) or {}
    except Exception as e:
        print(f"  ⚠ eval-loop 競合参照/軸の取得失敗（参照なしで継続）: {e}")

    n_per = _eval_loop_env_int("APP_THUMB_N_PER_ATTEMPT", 4)
    codex_model = os.environ.get("APP_THUMBNAIL_IMAGE_MODEL", "gpt-image-2")
    codex_size = os.environ.get("APP_THUMBNAIL_IMAGE_SIZE", "1536x1024")
    codex_quality = os.environ.get("APP_THUMBNAIL_IMAGE_QUALITY", "medium")
    codex_parallel = (os.environ.get("APP_THUMBNAIL_CODEX_MAX_PARALLEL") or str(n_per))

    def _glob_names() -> set:
        names = set()
        for ext in ("*.png", "*.jpg", "*.jpeg", "*.webp"):
            for p in out_dir.glob(ext):
                names.add(p.name)
        return names

    def generate_fn(concept_hint: str, axis: dict, attempt: int):
        body = concept_hint or prompt_base
        avoid = list((axis or {}).get("avoid") or [])
        if avoid:
            body = f"{body} Avoid: {', '.join(str(a) for a in avoid[:5])}."
        prompt_text = f"{body}::vol{vol}_thumb_a{attempt}"
        before = _glob_names()
        codex_cmd = [
            sys.executable, "-u", str(BASE / "codex_imagegen.py"),
            "--output-dir", str(out_dir),
            "--max-parallel", codex_parallel,
            "--model", codex_model, "--size", codex_size, "--quality", codex_quality,
            "--n", str(n_per),
            "--prompt", prompt_text,
        ]
        for _gr in gen_refs:
            codex_cmd += ["--reference-image", str(_gr)]
        try:
            p = subprocess.Popen(codex_cmd, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, bufsize=1)
            stdout, _ = p.communicate(timeout=900)
            rc = p.returncode
        except subprocess.TimeoutExpired:
            try:
                p.terminate()
            except Exception:
                pass
            return GenResult([], infra_error="timeout")
        except Exception as e:
            return GenResult([], infra_error=f"launch:{type(e).__name__}")
        new_files = []
        for ext in ("*.png", "*.jpg", "*.jpeg", "*.webp"):
            new_files.extend(p2 for p2 in out_dir.glob(ext) if p2.name not in before)
        new_files.sort(key=lambda p2: p2.stat().st_mtime)
        if not new_files:
            low = (stdout or "").lower()
            if "quota" in low or "rate limit" in low or "insufficient_quota" in low:
                return GenResult([], infra_error="quota")
            if "unauthor" in low or " 401" in low or "auth" in low:
                return GenResult([], infra_error="auth")
            return GenResult([], infra_error=f"exit={rc}")
        return GenResult(new_files)

    def score_fn(files: list):
        try:
            sc = score_thumbnails(
                generated_paths=list(files),
                competitor_paths=ref_paths[:4],
                video_title=video_title,
                video_concept=video_concept,
                channel_name=channel_name,
                persona=persona,
                cli_cmd=cli,
                timeout=600,
            )
        except Exception as e:
            return ScoreResult([], error=f"{type(e).__name__}: {str(e)[:160]}")
        return ScoreResult(sc.get("evaluations") or [], error=sc.get("error") or "")

    max_attempts = _eval_loop_env_int("APP_THUMB_MAX_ATTEMPTS", 2)
    print(f" eval-loop 開始（max_attempts={max_attempts}, n/attempt={n_per}, "
          f"採点用ref={len(ref_paths)} 枚, 生成用ベンチマーク参照={len(gen_refs)} 枚）")
    res = run_eval_loop(
        generate_fn=generate_fn, score_fn=score_fn,
        judge_fn=judge_auto_approval, status_counts_fn=status_counts, best_of_fn=best_of,
        concept_hint_base=prompt_base, thumbnail_axis=thumbnail_axis,
        score_threshold=_eval_loop_env_int("APP_THUMB_SCORE_THRESHOLD", 80),
        ctr_threshold=_eval_loop_env_float("APP_THUMB_CTR_THRESHOLD", 7.0),
        similarity_max=_eval_loop_env_int("APP_THUMB_SIMILARITY_MAX", 25),
        required_pass=_eval_loop_env_int("APP_THUMB_REQUIRED_PASS", 1),
        max_attempts=max_attempts,
        max_total_generated=_eval_loop_env_int("APP_THUMB_MAX_TOTAL_GEN", 8),
        max_vision_calls=_eval_loop_env_int("APP_THUMB_MAX_VISION_CALLS", 3),
        log_fn=print,
    )

    # 採用判定: 合格なら approved（その中の best）、不合格は ADOPT フラグ次第で best を採用
    adopt_on_fail = (os.environ.get("APP_THUMB_EVAL_ON_FAIL_ADOPT") or "").strip() in ("1", "true", "yes")
    chosen_name = None
    if res.passed:
        approved_set = set(res.approved_files)
        approved_evals = [e for e in res.all_evaluations if e.get("filename") in approved_set]
        pick = best_of(approved_evals) or res.best
        chosen_name = (pick or {}).get("filename")
    elif adopt_on_fail and res.best:
        chosen_name = res.best.get("filename")
        print("  ⚠ eval-loop 不合格だが APP_THUMB_EVAL_ON_FAIL_ADOPT=1 → best を採用")

    if not chosen_name:
        print(f"  ⚠ eval-loop: 昇格画像なし（passed={res.passed}, abort={res.abort_reason!r}）"
              " → thumbnail.png 未設定（候補は thumbnail_candidates に保管・手動承認可）")
        return False

    src = out_dir / chosen_name
    if not src.exists():
        matches = [p for p in out_dir.glob("*") if p.name == chosen_name]
        src = matches[0] if matches else None
    if not src or not src.exists():
        print(f"  ⚠ eval-loop: 採用ファイル {chosen_name} が見つからず → 未昇格")
        return False
    try:
        import shutil as _sh
        _sh.copy2(src, final_path)
        tag = "合格" if res.passed else "best(fail-adopt)"
        print(f" eval-loop: {src.name} → {final_path.name}（{tag}・"
              f"attempts={res.attempts_used}, gen={res.total_generated}, vision={res.vision_calls}）")
    except Exception as e:
        print(f"  ⚠ eval-loop: thumbnail.png コピー失敗: {e}")
        return False
    return True
