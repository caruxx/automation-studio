# Wobble Day 楽曲制作仕様

2026-10-02。対象はWobble Dayのみ。「Wobble Dayの曲を作って」という一度の依頼から、必要項目の解決、Macの正式ChromeでのSuno生成、保存、実ファイル検証まで担当を引き継げる仕様。

## 正本と現在の決定

- 正本: [Notion チャンネル設計図](https://app.notion.com/p/3ed83149ac8d81fcbc37fb5c15ce71bd)。本仕様を整備した際の最終編集時刻は`2026-10-02T03:25:55.350Z`。実行ごとに再取得し、更新日時・取得日時・今回使う決定の要約を保存する。
- [閲覧版](https://wobble-day-blueprint.caru-x.chatgpt.site)は補助。取得不可・更新差・採用状態が不明なら正本を優先し、推測で補わない。
- 新制作は**歌なしのインストジャズ**。ジャズホップを含めて変化をつけ、固定BPM・固定編成・旧groove vocalへ戻さない。
- 最重要条件は**サムネで期待する気分と再生直後の音の一致**。気分未指定時だけ「穏やかに揺れる／軽快に弾む、どちら？」と聞く。
- A改訂版の丸く柔らかい造形、**薄めのA演奏ベースの色・線**を参照する。基準のLibrary fileは`libfile_faced43b52b08191a6439ff2dd88835f`、親から渡されたversionは`0`。最新版の採用状態をNotionで確認する。濃い15種と未採用の新サムネ3案は最終referenceへ勝手に昇格しない。
- チャンネル全体の順序は、サムネ確定→動画AI→その絵に合う楽曲。**楽曲だけの依頼では、既存の選択済み画像を確認する。新しい画像・動画を勝手に作らない。** 選択がない場合は画像referenceを確認するまで、少数試作を含めCreateへ進めない。
- 過去の歌入り曲は保存したまま扱う。約5匹、種、担当楽器は固定の最終仕様ではない。

## 一度の依頼から解決する情報

入口は[専用skill](../../skills/wobble-day-music.md)。親・ローカルとも同じ[requestテンプレート](../../config/music_requests/wobble-day-request.template.json)を使う。これはSunoの`--songs-file`用presetではない。

通常の呼出先はこのMacの`DEV/_claude`プロジェクト。別workspaceの担当へ渡す場合は下の`repo`パスも渡してから専用skillを読ませる。全プロジェクト共通のskill登録や既定設定は行わない。

|項目|解決・確認方法|
|---|---|
|依頼ID・実行範囲|`request_id`を一意に決める。新規生成／既存DLのみ／仕様整備を区別。中断後も同じIDで記録を読む。|
|気分|穏やか／軽快、または本人が具体的に指定した気分。既存回答を使い、繰り返し聞かない。|
|曲名|本人指定を保持。任せる依頼なら今回の場面から自然な題名を起草。旧vocal曲名を再利用しない。重複時は最終入力名を記録し、固定指定の名前を無断変更しない。|
|希望尺|1音源の秒数。動画全体の長さと区別し、許容差は依頼にあれば使う。希望値と生成後の実尺を別々に記録する。|
|指定数|「音源を何件」か「何タイトル・各何テイク」かを解決。指定がない時はCreate禁止。過去の20音源を既定にしない。|
|現在UIの単位|1回のCreateが何音源になるかを、そのモデル・画面で確認し記録。過去は2音源。現在も2なら20音源=10回。奇数指定を勝手に切り上げず、余剰生成の可否を確認する。|
|参考画像|正本のA造形とA演奏ベースを実際に開き、今回選択済みサムネ/背景も見る。絵の楽器・姿勢・表情から期待する音を一文にする。親の観察だけなら誰が見たかを明記し、ローカルで見たと偽らない。|
|Styles・構成|ジャンル、編成、リズム、音色、冒頭の入り、密度、展開を具体化。歌詞は作らず、旧歌詞を空にし、InstrumentalをUIで確認する。|
|保存先|run専用フォルダの絶対パス。既存Drive保存先が指定されていれば引き継ぐ。Chrome一時DL先と最終納品先を分け、移動/複製後もhashを照合する。|
|生成設定|model、画面のmode、instrumental、lyrics欄、duration、Styles、Exclude、Style Influence、Variety、Weirdness、Max等、実在する項目を読む。未対応はN/Aとして理由を記録。|

上記のうち会話・既存資料で解決できる内容は担当が埋める。本人の意思が必要な気分・数・未選択画像・余剰クレジット等だけをまとめて確認する。明示された生成依頼と数が揃っていれば、通常のCreateごとに再許可は取らない。新契約・credential・persistent access・security設定変更は別途確認する。

**実行ゲート:** `mode=generate_and_download`、`authorization.generate_authorized=true`、実際の本人指示の記録、正整数の`authorized_audio_count`と`count.requested_audio_count`の一致、今回の気分・希望尺・選択済み画像がすべて揃うまで、予約・フォーム投入・Createへ進まない。`documentation_only`は仕様整備だけ、`download_only`は対象の既存IDだけを回収する。trueの値を埋める根拠は本人の依頼であり、テンプレートを完成させるために許可を捏造しない。

尺の許容差が未指定なら、希望値と実測値・差分を報告し、独自の±秒基準で「指定尺合格」としない。「厳密に240秒」等の要求から外れた場合は不適合として保存し、編集・再生成の追加判断を本人へ戻す。ファイル検証完了とは区別する。

## 親からローカルへの受渡し

親は`request.json`と原稿を渡す。Notion/Libraryにアクセスできないローカルには、最新版の該当節、更新日時、選択済みの実画像（利用可能な添付またはローカルパス）、採用状態を渡す。期限付き署名URLを恒久referenceとして保存しない。

```text
repo: /Users/caruvi/Library/CloudStorage/GoogleDrive-abe_kota@caruvistar.jp/共有ドライブ/DEV/_claude
Wobble Dayの楽曲制作。skills/wobble-day-music.md と docs/music/wobble-day-production.md を使用。
runのrequest.jsonを読み、指定済み回答を引き継ぐ。
気分・音源数・希望尺・選択画像の不足だけを確認。正式Chrome CUAでSuno入力→設定読戻し→
指定数の生成→既存IDのDL→実ファイル検証まで行い、result.jsonと検証reportを返す。
楽曲だけの依頼なので画像・動画制作や公開へ進めない。受理不明のCreateは再送しない。
```

今回の整備依頼自体は`mode=documentation_only`、`generate_authorized=false`、音源数未指定。新生成・旧cloud20再開・background監視を開始しない。

## Macの正式Chrome CUAで実行する手順

### ローカル台帳・排他の接続

`Python/scripts/wobble_suno_bridge.py`はブラウザ・ネットワーク・Createを操作しない。既存の永久タイトル台帳とSUNO単一リソースlockを、正式Chrome CUAの前後で明示的に使う入口であり、adapter単体でCUA操作が自動化されたことにはならない。

予約前にrunの`request.json`を`mode=generate_and_download`、`authorization.generate_authorized=true`へ具体化し、本人の生成指示を`authorization.user_instruction`へ残す。`authorized_audio_count`と`count.requested_audio_count`を一致させ、現在UIで観測した`outputs_per_create_observed`と空でない`count.ui_evidence`、余剰を生まない`planned_create_count`、同数の`requested_title_count`を埋める。原稿配列は1 Createにつき1件で、各要素に空でない`title` / `styles`、`instrumental: true`、空の`lyrics`を持たせる。要素に`duration_seconds`を持たせる場合はrequestの希望尺と一致させる。20音源等の既定値は補わない。

```bash
python3 Python/scripts/wobble_suno_bridge.py reserve \
  --request /absolute/run/request.json \
  --songs /absolute/run/songs-draft.json \
  --output /absolute/run/songs-reserved.json
```

`reserve`は気分、希望尺、選択済み画像と選択根拠、生成許可、音源数とCreate計算を検査してから全titleを`app_music_catalog.prepare_submission`へ渡す。結果は新規ファイルへ原子的に保存し、既存outputを上書きしない。出力先が同一directory内のatomic hard-linkを扱えるかを予約前にprobeし、未対応なら台帳を変更せず拒否する。その場合はDrive FileProvider直下を避け、Macローカルのrun directoryで実行して最終記録を複製する。再開時は原稿を再予約せず、保存済み`songs-reserved.json`を使う。

`reserved`かつ`create_blocked=false`を確認してから、出力の`songs`だけをCUA入力に使う。`reserved_create_blocked`、`reservation_partial`、errorではCreateしない。同じ依頼に別の出力名を付けて再予約せず、保存済み結果を引き継ぐ。

本人が曲名を固定した原稿には`title_fixed: true`を付ける。台帳が重複回避で別titleを返した場合、予約結果は`create_blocked: true`となる。変更前後を本人へ示し、明示的な曲名判断を得るまでCreateしてはいけない。予約自体は受理不明の再利用を避けるため残す。`mark_submitted`はadapterでは行わず、Create受理確認後に保存済みsongを既存`app_music_catalog.mark_submitted`へ渡す工程として保留する。

CUA操作中のlockは別のforegroundのTTY付きexec sessionで保持する。stdinを閉じると解放されるため`< /dev/null`や終了した短命コマンドで代用しない。

```bash
python3 Python/scripts/wobble_suno_bridge.py hold-lock \
  --request /absolute/run/request.json \
  --max-seconds 1800
```

stdoutの`{"status":"ready", ...}`とPIDを確認してから別toolの正式Chrome CUAを使う。同じexec sessionの生存をCreate/DL操作前に確認し、終了・timeoutなら次の操作を止める。CUA終了時はstdinへ改行を送り`released`を確認する。継続が必要ならUI操作を止めて再取得する。新しい常駐サービスやbackground監視は作らない。stdin EOFでも解放し、指定時間で必ずtimeout解放する（上限3600秒）。holderは監視・生成・台帳変更を行わない。

`hold-lock`はread-only UI事前確認より先に起動できる。生成時はproject / request ID / browser route、本人指示、生成許可、相互一致する正整数の許可音源数と依頼音源数だけを事前検査し、UI観測値・画像・原稿は`reserve`までに揃える。`download_only`では本人指示と、空でなく重複のない`existing_clip_ids`を要求する。

通常のCUA作業ではbridgeを直接起動し、`parallel_guard`等で包んだり`AUTOMATION_RESOURCE_LOCK_HELD`を手で設定したりしない。`ready`の`lock_mode=acquired_here`と`lock_owned_by_process=true`を確認する。`inherited_parent`はbridge自身がflockを取得・解放していない表示であり、実際の親processがlockを所有し続ける場合だけ有効。単独のlock取得証拠には使わない。

1. 対象runの記録を読み、同じ依頼の生成済み曲を先に照合する。Suno利用中の他作業と重ねない。既存単一リソースは`resource_lock.ResourceLock('suno-browser')`。CUAは既存CLIの内部ロックを自動取得しないため、上記のforeground bridgeでCUA作業中のロックを保持する。ロックファイルの存在だけを所有の証拠にしない。
2. CUAの現行tool documentationを読み、Chromeを明示して観測済みのSunoタブを取得するか、Chromeに`https://suno.com/create`のタブを作る。実際の引数・UI操作はその実行時のtool仕様に従う。private browser sessionファイル、cookie、credentialを読まない。
3. 正しいSuno Workspace、残高、モデル、1回の生成数、曲リストを画面で確認し、今回のworkspace URLを記録する。旧`groove_twenty_20261001`は過去曲の確認先であり、新Wobbleの既定にしない。
4. 曲名の重複回避は既存`app_music_catalog.prepare_submission(content)`をCreate前に1回使い、返る`title`と`_music_request_id`を原稿へ保存する。上記bridgeの`reserve`がこの既存moduleを呼ぶ。これはローカル台帳への書込みであり、Suno送信ではない。中断後は保存済み予約を使い、同じ原稿を再予約しない。`mark_submitted`は受理確認後に使う。固定指定のタイトルが予約で変わった場合は、Create前に本人へ差分を確認する。
5. 原稿のtitle/Styles/Exclude/構成と希望尺を入力し、Instrumental状態と歌詞欄を確認する。各Create前に**実画面から**値を読み戻し、原稿との一致、対象workspace、残高・費用、残り回数を記録する。送信直前のreadbackは曲ごとに残す。
6. 予定分だけCreateする。クリック時刻、受理状態、対応する新曲ID/URLを記録する。生成中・完了・失敗・受理不明を分け、受理不明や残高だけ動いた時は既存曲を照合するまで次へ進めない。余分な再生成は別の依頼として扱う。
7. 生成完了した対象曲IDだけを公式Chromeの現在利用可能なDL操作で保存する。既存の正規Chrome UIにSuno Fast拡張が表示される場合は、対象ID・曲数・保存先を読んだうえで利用可。新拡張の導入や権限変更、旧CLI/headless/CDP/私有APIへの回帰は含まない。
8. 保存ファイルを曲IDに対応づけ、以下の検証を行って`result.json`へ返す。元音源を削除・上書きしない。音質不合格でも勝手にCreateを増やさない。

旧設定の`v6 / SIA80 / Variety0 / Weirdness50 / MaxOff / Custom240`は歌入りgroove制作時の値。Wobbleインストの既定ではない。今回の要件と現在UIから決め、readbackを記録する。

## 保存と完了判定

runフォルダには`request.json`、`songs.json`（確定原稿）、`progress.json`（Create前後とID）、`download-manifest.json`、`download-validation.json`、`result.json`を残す。スクリーンショットは設定/曲ID/費用など必要な範囲だけ。認証情報を保存しない。

manifest最小例（パスはmanifest位置からの相対または絶対パス）:

```json
{
  "expected_audio_count": 2,
  "tracks": [
    {"clip_id": "実際の曲ID1", "title": "実際の曲名", "path": "audio/take1.mp4", "mapping_evidence": "progress.json:曲ID1のDL操作・ファイル名の対応"},
    {"clip_id": "実際の曲ID2", "title": "実際の曲名", "path": "audio/take2.mp4", "mapping_evidence": "progress.json:曲ID2のDL操作・ファイル名の対応"}
  ]
}
```

```bash
python3 Python/scripts/verify_suno_downloads.py /absolute/run/download-manifest.json \
  --output /absolute/run/download-validation.json
```

|判定|必要な証拠|
|---|---|
|生成完了|対象Workspaceに必要なunique曲IDが実在し、生成完了状態。送信成功・残高減少だけでは不足。|
|DL検証完了|期待音源数とmanifest件数一致、重複なし、非空の実ファイル、サイズ安定、SHA256、音声stream、ffprobe実尺/codec、ffmpeg全音声decode成功。曲ID入りコンテナはID一致。IDが埋まっていなければCUA操作との対応証拠が必要で、自動ID照合済みと同一視しない。|
|納品先検証完了|最終保存先に実在しhash一致。Chrome Downloads確認だけでDrive保存済みと言わない。|
|Wobble音の確認|選択画像を見て冒頭15〜30秒を聴く。全曲の歌声/ハミング/語りの混入、編成・ノリ、途中/終端も確認。ソフトウェアdecodeはこれを証明しない。未試聴はpendingとする。|
|採用完了|本人の選択が必要な時は選択待ち。全候補取得と最終採用は別。短いテイクの自動採用や原本削除をしない。|

`result.json`の最小項目: request_id、status、workspace_url、requested_audio_count、create_count、generated_count、download_verified_count、delivery_verified_count、quality_review_status、selection_status、cost（before/after/delta/source/不明理由）、tracks（ID/URL/title/原稿/settings/path/bytes/hash/実尺/検証結果）、exceptions、次の本人入力。状態は`needs_input / ready / generating / acceptance_unknown / generated / download_partial / download_verified / quality_review_pending / awaiting_selection / complete`等で具体的に返す。費用・実尺の不明値を0にしない。

生成済み曲の取得失敗はDLのみ再開する。`exit 79 / awaiting_selection`を生成失敗として再投入しない。音源だけの納品で採用が依頼範囲外なら、その旨を結果に残して終了できる。

## 既存実装との境界

- `studio.py`の既定`--count`は20生成リクエストで、過去UIの2音源/回なら40音源になり得る。Wobbleの指定音源数として使わない。`instrumental_filler`もnative Instrumentalの選択ではなくLyrics欄のfiller投入である。
- `studio.py suno-auto`は既存Playwright生成→DL→後処理へ進む経路。Wobbleの正式Chrome運用へは接続されていないため実行しない。今回追加したのは**エージェントの明示的な入口分岐とファイル契約**。
- `wobble_suno_bridge.py`は既存`app_music_catalog`の予約と`ResourceLock`を局所的に呼ぶ。Chrome CUAとは担当エージェントがrequest/予約JSONとforeground実行sessionで接続する。ブラウザ操作やCreateを内蔵したCLIではない。
- 既存のハート選曲登録は`safe_file`がMP3のみを受け付ける。Chromeで得られたMP4をそのまま登録しない。音楽だけの納品は原本とmanifestで完了可能。Studioで選曲する追加依頼では原本を保全し、明示した変換/登録を別工程として検証する。
- グローバルchannel設定、`routes.json`、既存beat-soul preset、他project、Chrome拡張設定は変更しない。

## 今回確認した根拠

- 2026-09-29のレシピ・Custom240・全テイク取得・選択待ち契約は`beat-soul-recipe.md`と`semi-auto-selection.md`。音楽性は旧歌入りのため新instへ流用しない。
- Chrome実績: `output/suno-groove-twenty-20261001-chrome/progress.json`、`validation.json`、`clips-current.json`。今回、`/Users/caruvi/Downloads/groove_twenty_20261001`の20 MP4について全件ID/タイトル、bytes/SHA256、ffprobe、全decodeを再照合。238.28〜241.2秒。記録残高9815→9715、100 credits。これは旧制作の実績で、将来の単価・Wobble音質の証明ではない。
- 旧20の10回すべてに均一な設定readbackは残っていない。Drive複製と聴感の合否も今回の確認範囲外。今後は各Createのreadbackと最終保存先を上の契約で残す。
