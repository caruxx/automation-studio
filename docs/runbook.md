# Runbook — 無人運用時のトラブル対応

Discord で通知が来たときに、運営者がこの手順書を見れば**最短で復旧コマンド**にたどり着けることを目的とした 1 ページ運用ガイド。

最終更新: 2026-05-03

## 通知メッセージとアクションの対応表

Discord 通知の **絵文字** で症状を識別し、該当セクションへ。

| 絵文字 | 症状 | セクション |
|---|---|---|
| 🔐 | ブラウザ手動ログイン要 | [#unattended-login](#unattended-login) |
| 📊 | YouTube クオータ枯渇 | [#youtube-quota](#youtube-quota) |
| ⚠️ preflight | Premiere/CEP 未起動 | [#preflight](#preflight) |
| ❌ | 一般失敗 / retry 上限到達 | [#general-failure](#general-failure) |

通知本文末尾には常に `再開: python3 app_pipeline.py <vol> --from <stage>` の形で**コピペ可能な復旧コマンド**が含まれている。手順書で背景を理解 → そのコマンドを実行、で完了する設計。

---

<a id="unattended-login"></a>
## 🔐 ブラウザ手動ログインが必要

### 何が起きているか
SUNO / Flow / YouTube OAuth のいずれかで Playwright プロファイル (`~/.flow-playwright-profile`) が切れたか、初回認証が未完了の状態。`APP_NO_INTERACTIVE=1` で起動した子プロセスは `UnattendedLoginRequired` 例外で即座に exit する設計のため、**長時間ハングではなく早期失敗 + Discord 通知** が来ている。

### 復旧手順

1. 通知に出ているサービス（SUNO / Flow / YouTube）に応じて以下を実行:

| サービス | 復旧コマンド | 補足 |
|---|---|---|
| **SUNO** | `cd Python && python3 suno_auto_create.py --prompt "test" --count 1` | ブラウザが立ち上がるので **手動でログイン** → 1 曲生成が始まれば閉じて OK |
| **YouTube** | `cd Python && python3 app_youtube.py /path/to/vol_folder --auth-only` | OAuth 同意画面が開く → ブラウザアカウントを選択 → トークンが `<channel_folder>/.youtube_token.json` に保存される |
| **Flow** | `cd Python && python3 flow_automation.py --login-only` | Google アカウントで Flow にログイン |

2. 通知に含まれる**再開コマンド**をそのまま実行:
   ```bash
   python3 app_pipeline.py <vol> --from <stage>
   ```

### よくある間違い
- **トークンを共有 PC 間でコピーしないこと**: `<channel_folder>/.youtube_token.json` は Google Drive 経由で 2 PC 間自動同期される。手動コピー不要。
- **Playwright プロファイルは Mac ローカル**: Drive 同期されない。各 PC で初回ログインが必要。

### SLO 目安
- 通知から復旧着手まで: **30 分以内**（次のスケジュールに間に合わせるため）

---

<a id="youtube-quota"></a>
## 📊 YouTube クオータ枯渇

### 何が起きているか
`<channel_folder>/.youtube_quota.json` の累積コストが 24h ウィンドウで上限（デフォルト 10000 unit）に到達。さらに upload を試みると **API 側で 403 quotaExceeded が返る前に**ローカル側で stop している。

### 復旧手順

**待つだけで OK。** 24h 経過すると `record_upload_quota` のローテーションで古い event が prune され、自動的に枠が空く。

ただし以下の条件を確認:

1. **本当にクオータを使い切ったか** — 他のスクリプト・複数チャンネル・別 GCP プロジェクトの併用で過剰消費していないか:
   ```bash
   cat <channel_folder>/.youtube_quota.json | python3 -m json.tool
   ```

2. **クオータ上限を引き上げたい場合** — Google Cloud Console > APIs & Services > YouTube Data API v3 > Quotas でリクエスト（審査 1〜2 週）。

3. **手動で再開するなら** 24h 経過を確認後:
   ```bash
   python3 app_pipeline.py <vol> --from upload
   ```

### 環境変数で閾値を調整
- `APP_YT_DAILY_QUOTA_CAP` (デフォルト `10000`)
- `APP_YT_QUOTA_PER_UPLOAD` (デフォルト `100`)
- `APP_YT_QUOTA_WINDOW_HOURS` (デフォルト `24`)

---

<a id="preflight"></a>
## ⚠️ preflight 失敗（Premiere Pro 未起動）

### 何が起きているか
pipeline 開始前のチェックで **Premiere Pro プロセスが見つからない** か、**Premiere Link CEP パネルが応答していない**。 Premiere は Mac 1 台に 1 セッションしか動かないため、SUNO 等の長時間処理を始めてから「実は Premiere が落ちていた」と気付く事故を防ぐための早期検知。

### 復旧手順

1. **Premiere Pro 2026 を起動**
2. メニューから **「ウィンドウ > 拡張機能 > Premiere Link」** を選択（パネルが開く）
3. パネルが緑色の "Connected" 表示になったら復旧コマンドを実行:
   ```bash
   python3 app_pipeline.py <vol>
   ```

### CEP パネルが見つからない場合
```bash
bash cep_extension/install.sh
```
を実行してから Premiere を再起動。

### preflight チェックを意図的にスキップしたいケース
テスト用や Premiere 不要な工程だけ流すとき:
```bash
APP_PREFLIGHT_DISABLE=1 python3 app_pipeline.py <vol> --only meta
```

---

<a id="general-failure"></a>
## ❌ 一般失敗 / retry 上限到達

### 何が起きているか
stage 単位の retry 層 (`RETRY_POLICY`) が試行回数を使い切った状態。最終的な失敗理由は Discord 通知本文末尾の `(<返り値>)` を確認。

### 段階別の典型的な原因と対応

| stage | 典型原因 | 対応 |
|---|---|---|
| `plan` | Claude CLI タイムアウト | `claude --version` で疎通確認 → 再投入 |
| `suno` | Workspace 命名重複 / DL タイムアウト | `~/.flow-playwright-profile` を保ったまま `--from suno` で再開 |
| `rename` | ffmpeg なし | `brew install ffmpeg` |
| `premiere` | Project ファイル不在 | `<channel_folder>/template.prproj` の有無を確認 |
| `export` | Media Encoder 未起動 / ディスク満杯 | AME 起動 + `df -h` で空き確認 |
| `meta` | Claude CLI レートリミット | 数分待ってから `--from meta` で再投入 |
| `upload` | quotaExceeded（→ 📊 セクションへ） / 動画違反 | YouTube Studio で動画詳細を確認 |

### デフォルトの再開コマンド
```bash
python3 app_pipeline.py <vol> --from <stage>
```

### retry を一時無効化（デバッグ）
```bash
APP_RETRY_DISABLE=1 python3 app_pipeline.py <vol> --from <stage>
```

---

## チャンネル並列運用時の注意

P1-3 以降、複数チャンネルのジョブが同時に走っても `dashboard_config.json` を競合書き換えしない設計（`--channel-folder` 引数 + `APP_CHANNEL_FOLDER` env で各サブプロセスが独立）。よって Discord 通知の **`[<channel_name>]`** プレフィックスでどのチャンネルのジョブか即判別可能。

通知例:
```
❌ [Channel A] vol.78 の 6/6 YouTube アップロード で retry 上限に到達しました
再開: python3 app_pipeline.py 78 --from upload
```

→ チャンネル A の vol.78、upload stage で停止 → このチャンネルだけ手動再開すれば OK。他チャンネルのジョブは独立に進行している。

---

## sentinel exit code 一覧

`app_pipeline.py` および子スクリプトが使う特殊 exit code:

| code | 意味 | 上位の処理 |
|---|---|---|
| `0` | 成功 | 次の stage へ |
| `1` | 一般失敗 | retry policy に従う |
| `75` | unattended login required | retry なし、Discord 通知 |
| `76` | retryable failure (transient) | 指数バックオフで retry |
| `77` | YouTube quota exhausted | retry なし、Discord 通知（24h 待ち） |
| `78` | preflight failure | retry なし、Discord 通知（手動 Premiere 起動要） |
