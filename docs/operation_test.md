# 運用テスト — Operation Test Catalog

最終更新: 2026-05-03

## 1. テストの種類と用途

| 種類 | 目的 | 頻度 | 所要時間 |
|---|---|---|---|
| **スモークテスト** | API / モジュールが配線されているか | 毎日 / リリース後 | ~10 秒 |
| **チャンネル別 dry-run** | 個別チャンネルの設定が正しいか | チャンネル追加時 | ~30 秒 |
| **段階別動作テスト** | 1 stage だけを抜き出して検証 | 機能改修時 | 2〜30 分 |
| **E2E 通し** | 実際に SUNO/Premiere/YouTube まで通す | 月次 / 重要変更後 | 1〜3 時間 |

スモークテスト以外は、副作用がある（vol が実際に生成される / YouTube に upload される）ため、必要に応じて実行。

---

## 2. スモークテスト（推奨）

### 実行方法

```bash
cd /path/to/_claude
python3 Python/scripts/p1p2p3_smoke.py
```

オプション:
- `--base http://localhost:8888` — サーバ URL を指定
- `--skip-render` — render queue を呼ばない
- `--skip-publish` — 公開ゲートを呼ばない

サーバが 8888 以外で動いている場合は自動で 8889 / 8890 をフォールバック検索。

### 終了コード
- `0` 全 PASS
- `1` 1 件以上 FAIL
- `2` サーバ接続失敗

### カバー範囲（22 ケース）

| 項目 | 内容 |
|---|---|
| **P1-1** | `UnattendedLoginRequired` exception の存在 |
| **P1-2** | `EXIT_RETRYABLE=76` + `RETRY_POLICY` 定義 |
| **P1-3** | `--channel-id` / `--channel-folder` CLI args |
| **P1-4** | `EXIT_QUOTA_EXHAUSTED=77` + cap 設定 |
| **P1-5** | `_preflight_premiere()` returns (bool, str) |
| **P1-6** | `docs/runbook.md` 配置 |
| **P1-7** | `GET /api/runs/active` |
| **P2-1** | `GET /api/render-queue` + throughput |
| **P2-2** | channels.json registry が >= 1 件 |
| **P2-3** | `?channel_id=__none__` フィルタ |
| **P2-4** | 同 slot を 30 分自動ずらし（実投入 → 確認 → 削除） |
| **P2-5** | step_thumbnail in STEPS |
| **P2-6** | auto_resume フィールド受入 |
| **P2-7** | `POST /api/youtube/publish-now/{name}` 存在 |
| **P3-1** | ledger list / stats / migration dry-run |
| **P3-2** | `_load_benchmark_axes()` |
| **P3-3** | step_qa が export → meta 間に挿入 |
| **P3-4** | policy-aware helpers 定義 |
| **P3-5** | `GET /api/token-health` |

---

## 3. 個別動作テスト

### 3.1 channel registry resolution（P1-3 / P2-2）

新規チャンネル追加時に必ず実行:

```bash
# 1. registry 内容確認
curl -s http://localhost:8888/api/channels | python3 -m json.tool | head -30

# 2. dry-run で channel-id 解決を確認（vol 999 は実存しないので fail で OK）
python3 Python/app_pipeline.py 999 --dry-run --channel-id <id>
# 期待: 📌 channel: id=<id> name=<name> folder=<path>

# 3. 起動時 validation ログ
grep "registry" /tmp/claude-502/*/tasks/*.output | tail -5
# 期待: [registry] active channel: '...' (id='...') ✓
```

### 3.2 render queue（P2-1）

並列ジョブが直列消化されるか:

```bash
# 6 チャンネル分の疑似ジョブを enqueue
for i in 1 2 3 4 5 6; do
  curl -s -X POST http://localhost:8888/api/render-queue/enqueue \
    -H 'Content-Type: application/json' \
    -d "{\"channel_folder\":\"/tmp/test_ch$i\",\"channel_name\":\"ch$i\",\"vol\":$((100+i)),\"video_name\":\"$((100+i))_test\",\"stage\":\"premiere\"}" &
done; wait

# 状態確認: pending=6, running=1
curl -s http://localhost:8888/api/render-queue | python3 -m json.tool | head -10

# クリーンアップ（worker が folder 不在で error にする → 自然消化を待つ）
sleep 30
curl -s http://localhost:8888/api/render-queue/throughput | python3 -m json.tool
```

### 3.3 公開ゲート（P2-7）

```bash
# per-channel publish_delay_hours を 24 に設定
curl -s -X PUT http://localhost:8888/api/config/dashboard \
  -H 'Content-Type: application/json' \
  -d '{"publish_delay_hours": 24}'

# 設定確認
curl -s http://localhost:8888/api/config | python3 -c "
import json,sys; d=json.load(sys.stdin)
print('publish_delay_hours:', d.get('dashboard',{}).get('publish_delay_hours'))"

# upload 後に scheduled_publish_at が自動セットされるか
# → 実 upload を伴うので E2E テストでのみ実行
```

### 3.4 token health（P3-5）

```bash
# 全チャンネルを一括点検
curl -s http://localhost:8888/api/token-health | python3 -m json.tool

# 警告があれば Discord に手動送信して通知文言を確認
curl -s -X POST http://localhost:8888/api/token-health/notify
```

### 3.5 ledger migration（P3-1）

新規導入時の既存 vol 取り込み:

```bash
# dry-run（必ず先に実行）
curl -s -X POST http://localhost:8888/api/runs/ledger/migrate \
  -H 'Content-Type: application/json' -d '{}' | python3 -m json.tool > /tmp/migration_dryrun.json
cat /tmp/migration_dryrun.json | head -50
# would_insert を目視確認 → OK なら次へ

# 実適用（idempotent: 重複は skip される）
curl -s -X POST http://localhost:8888/api/runs/ledger/migrate \
  -H 'Content-Type: application/json' -d '{"apply":true}'

# 反映確認
curl -s http://localhost:8888/api/runs/ledger?status=reconstructed | python3 -c "
import json,sys; d=json.load(sys.stdin); print(f'reconstructed: {d[\"count\"]} runs')"
```

---

## 4. E2E 通しテスト（半自動）

実 SUNO/Premiere/YouTube を経由する。**コスト発生 + 30 分以上**かかるので慎重に。

### 4.1 1 チャンネル × 1 vol（テストチャンネル推奨）

```bash
# 1. テスト用チャンネルでの dashboard 設定
curl -s -X POST http://localhost:8888/api/active-channel/<test_channel_id>

# 2. preflight: Premiere Pro が起動 + Premiere Link パネル開いている
osascript -e 'tell application "Adobe Premiere Pro 2026" to activate'
# UI で「ウィンドウ > 拡張機能 > Premiere Link」を選択

# 3. dry-run
APP_USE_RENDER_QUEUE=0 python3 Python/app_pipeline.py 99 --dry-run --channel-id <test_channel_id>

# 4. 実行
APP_NO_INTERACTIVE=1 APP_USE_RENDER_QUEUE=1 \
  python3 Python/app_pipeline.py 99 --auto --channel-id <test_channel_id>

# 5. 各 stage の artifact 確認
ls <test_channel_folder>/99_*/
# 期待: plan.json, music/, audio/, *.prproj, timecode.txt, subtitles_*.srt,
#       *.mp4, qa_report.json, youtube_title.txt, ..., thumbnail.png,
#       youtube_upload.json
```

### 4.2 並列 6 チャンネル（最大負荷）

スケジューラに 6 チャンネル分 vol_create を登録し、同時刻起動でも破綻しないことを確認:

```bash
# UI > マスタ設定 > 自動実行スケジュール で 6 件登録
# 全て月曜 9:00 開始 + auto_resume:true + channel_id を変える
# → P2-4 が 9:00 / 9:30 / 10:00 ... と自動分散するはず

# 月曜直前に確認
curl -s http://localhost:8888/api/schedule/jobs | python3 -c "
import json,sys
d=json.load(sys.stdin)
for j in sorted(d['jobs'], key=lambda x: (x['trigger'].get('hour',0), x['trigger'].get('minute',0))):
    if j['type'] == 'vol_create':
        t=j['trigger']
        print(f\"  {t.get('day_of_week')} {t['hour']:02d}:{t['minute']:02d} ch={j.get('channel_name','?')}\")"
```

### 4.3 失敗復旧シミュレーション

意図的に失敗させて auto_resume が動くか:

```bash
# 1. SUNO ログイン切れを疑似
mv ~/.flow-playwright-profile ~/.flow-playwright-profile.bak

# 2. auto_resume=true のジョブを 1 件 run-now
curl -s -X POST http://localhost:8888/api/schedule/run-now/<job_id>

# 3. 監視
watch -n 5 'curl -s http://localhost:8888/api/runs/ledger/stats?days=1 | python3 -m json.tool'
# 期待:
#   - 1 回目失敗 → ledger に failed run + Discord 通知（🔐 unattended）
#   - auto_resume が抑止される（exit 75 のため）
# 4. プロファイルを戻す
mv ~/.flow-playwright-profile.bak ~/.flow-playwright-profile
```

---

## 5. 定期実行の推奨セット

`schedule_jobs.json` への登録例（チャンネル数 6 想定）:

```json
{
  "jobs": [
    {
      "type": "token_health",
      "name": "token health daily",
      "enabled": true,
      "trigger": {"kind": "cron", "day_of_week": "*", "hour": 8, "minute": 0}
    },
    {
      "type": "benchmark_refresh",
      "name": "benchmark weekly",
      "enabled": true,
      "trigger": {"kind": "cron", "day_of_week": "mon", "hour": 6, "minute": 0}
    },
    {
      "type": "vol_create",
      "name": "ch_a weekly Mon 9:00",
      "channel_id": "ch_a",
      "trigger": {"kind": "cron", "day_of_week": "mon", "hour": 9, "minute": 0},
      "auto_resume": true,
      "auto_resume_delay_min": 30,
      "auto_resume_max_attempts": 3,
      "balance_slots": true
    }
  ]
}
```

slot balancing を有効にしておけば、6 チャンネル分の vol_create を全て月曜 9:00 で登録すると、自動で 9:00 / 9:30 / 10:00 / 10:30 / 11:00 / 11:30 に分散される。

---

## 6. リリース前チェックリスト

| チェック | コマンド / 操作 |
|---|---|
| ✅ スモーク 22/22 PASS | `python3 Python/scripts/p1p2p3_smoke.py` |
| ✅ 起動 hooks ログ | `[scheduler] / [registry] / [ledger] / [render-queue]` 全部出る |
| ✅ token health の警告ゼロ（or 想定内） | `curl /api/token-health` |
| ✅ ledger migration が idempotent | 2 回連続実行で `total_inserted=0` |
| ✅ schedule_jobs.json バックアップ | `cp ~/.config/orzz/schedule_jobs.json{,.bak}` |
| ✅ Premiere Pro 起動 + CEP パネル開いている | `pgrep -fi "Adobe Premiere Pro"` + 目視 |
| ✅ Google Drive 同期完了 | Finder の同期インジケータが空 |
| ✅ ディスク空き容量 | `df -h` で 50 GB 以上 |

---

## 7. CI 化（将来）

スモークテストは CI ジョブ化が容易（`exit 0/1/2` で判定）。GitHub Actions 等に組み込む場合の最小構成:

```yaml
- name: Smoke test
  run: |
    bash Python/start.sh &
    SERVER_PID=$!
    sleep 10
    python3 Python/scripts/p1p2p3_smoke.py --skip-render
    kill $SERVER_PID
```

ただし Premiere / Playwright が必要な test は self-hosted runner（Mac）が必要。
