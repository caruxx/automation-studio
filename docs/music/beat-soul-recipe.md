# Beat Soul 制作レシピ — 考え方から検証まで

2026-09-29。Fable、Claude、Codex、その他の制作ツールで共通利用する、単独で読める引き継ぎ書。

新チャンネルはPhotoshop・Premiere Pro・Media Encoderを使わず、画像エディタとFFmpegで制作する。工程・設定・選曲との接続は [Adobeなしの制作手順](beat-soul-ffmpeg-workflow.md) を参照。

## まず何を作るのか

アメリカのラグジュアリー層に、長時間聴き流してもらうBGMを作る。目標は「上質な質感を持ち、身体が軽く動くビート中心の曲」。冒頭で歌のフックを聴かせ、その後もベースとドラムの反復が進む。落ち着きは音色、音量差、声の混ざり方で作る。リズムの勢いを落とす方向には使わない。

参照は [Jungle - Lifting You // Bonnie Hill](https://www.youtube.com/watch?v=83Te_6OKRlc)。コピーする素材としてではなく、ビート、反復、声の扱い、音の質感を分析する対象として使った。歌詞と各曲のフックは新しく書いた。

ユーザーが判断した重要な違いは、次の３つだった。

- 歌い始めが遅く、初期離脱につながりそう。
- 雰囲気が落ち着きすぎて、ジャズやバラードのように感じる。
- 入り方が改善しても、参照楽曲の音楽性に近づいていない。

最終的にCome AroundのSynth Groove / String Grooveの２案について「楽曲いい感じや」と承認。その音楽性を維持して10曲へ展開した。

## どのように方向を修正したか

|段階|実際に使った方向・指定|ユーザーの反応／判断|次に変えたこと|
|---|---|---|---|
|初期４曲|90〜100 BPM。restrained downtempo、mellow lounge、gentle jazz-soul、soft kick、Rhodes、brushed drums、extended instrumental passages。歌詞はInstrumental Introから開始。|落ち着きすぎ、歌い始めが遅い。|ドラムとベースの存在感、16分の細かい動き、反復フックを増やした。|
|次の２曲|102〜106 BPMのdisco-soul / psychedelic soul。明確なバックビート、ギターの刻み、４小節イントロ。|歌が始まるまでの待ちが残る。|イントロをなくし、最初からサビの歌を置いた。|
|Keep the Feeling|112 BPM、upbeat disco-soul、vocals on first beat、chorus-first。|入り方はよいが、参照の雰囲気に近くない。「バラードじゃない」。|速度だけ上げる考えをやめ、ビートの作り方、低音の反復、声の音色と短い句の扱いへ戻った。|
|承認された２案|104 BPM、psychedelic electronic soul、beat-driven electronic funk、drum-loop、two-bar bass ostinato、短い重ね声。SynthとStringの２系統。v6のVariety Off、Style Influence 80。|「楽曲いい感じや」。|この共通骨格を固定し、タイトル、歌詞、フック、音色の枝、102〜107 BPMの範囲だけを変えて10曲作った。|

この表は実際の原稿とユーザーの反応に基づく。各要素を１つずつ比較した実験ではない。「Varietyを変えたことだけが改善の原因」など、単独の因果関係までは証明していない。

## Stylesを作るときの判断

### 1. 先にリズムと曲の動きを決める

「ラグジュアリー」「落ち着いた」「夜景」「温かい」だけでは、今回必要なビートを決められない。初期案は穏やかさを表す指定が多く、歌詞もゆっくり情景を描く形だった。これらが全体を静かな方向へ寄せたと考え、次のように書き換えた。

- **ジャンル**：psychedelic electronic soul / beat-driven electronic funk。
- **ドラム**：syncopated drum-loop groove / punchy dry backbeat / busy sixteenth-note percussion。
- **低音**：repeating two-bar bass ostinato。旋律的な低音という抽象表現より、短い反復の役割を具体化。
- **曲の進行**：full beat from first bar / hypnotic looping arrangement / buoyant rhythmic momentum。
- **サビの変化**：chorus layers without tempo changes。大きなテンポ感の落差を作る代わりに、声やパーカッションを重ねる。

BPMは指定値であり、完成音源の測定値ではない。また、速いBPMだけで音楽性は再現できない。今回112 BPMへ上げても、ユーザーは雰囲気の違いを指摘した。

### 2. 声を「曲を止める独唱」から「グルーヴの一部」へ

`short rhythmic layered falsetto phrases, blended male and female unison, sampled-vocal texture, clipped syllables, immediate vocal hook` を使った。長く伸ばすソロよりも、短い音節を声の層として反復する意図。

`airy` や `falsetto` だけでは穏やかな歌唱も生成されるため、短さ、リズム、重ね方、開始位置も同時に指定する。Vocal GenderやVoice Personaは固定していない。実際の声は生成結果から選ぶ。

### 3. 共通骨格を固定し、音色を２系統で展開

**Synth系**：rubbery analog synth bass、filter-envelope bass attack、muted guitar riff、filtered synth stabs、stereo psychedelic synth textures、saturated drum-loop production。

**String系**：driving electric bass loop、clipped guitar riff、rhythmic string-loop accents、short brass stabs、flute response phrases over a persistent beat、textured psychedelic production。

StringやFluteは、持続するビート上の短いアクセントや応答として指定した。壮大な弦の盛り上がりや、サックスの独奏中心のラウンジへ展開する意図ではない。

参照楽曲の制作要素は [アーティストのアルバムノート](https://music.apple.com/us/album/loving-in-stereo/1555318008) でも確認。Lifting YouのビートとMoog Oneベース、Bonnie Hillのビートと弦・管・フルート等が、２つの音色の枝を考える根拠になった。ただし、今回の生成曲が同じ楽器を忠実に再現したとまでは確認していない。

### 4. Excludeも共通で保存

```text
ballad, piano ballad, acoustic ballad, jazz lounge, crooning soloist, legato lead vocal, melisma, excessive vibrato, sentimental orchestral swell, sparse half-time drums, long intro, EDM build and drop
```

除外指定だけで方向を作らない。最初に「どんなビート、低音、声、反復を出すか」を肯定的に具体化して、その後で望まない方向を補助的に除外する。

## 歌詞をリズムに乗せる方法

1. 最初の４行でフックを作る。２〜５語程度の短い行を中心にして、強い動詞や動作を置く。例：Let it run / Through the night / Let it run / Till we get it right。
2. その４行を冒頭で２回繰り返し、最初のセクションをOpening Chorusにする。Instrumental Intro、長い語り、雰囲気作りのハミングから始めない。
3. Aメロも１行１句で、長い説明文を避ける。情景は移動、足取り、光、合図、視線など、リズムに合う小さな動作で描く。
4. Pre-Chorusは４つの短い句。新しい長文や静かな休憩を入れず、同じグルーヴ上で次のフックへつなぐ。
5. サビは同じフックの反復。長く伸ばす母音や大きな感情の独唱に頼らず、声の層や応答で変化を出す。
6. BridgeとGroove Breakでもベースとドラムを維持する。短い休憩は認めるが、疎なハーフタイムやピアノだけのバラード展開へ落とさない。

採用した原稿構成は、Opening Chorus 8小節 → Verse 8 → Pre 4 → Chorus 16 → Verse 8 → Pre 4 → Chorus 16 → Bridge 8 → Groove Break 8 → Final Chorus 16 → Outro 4。合計100小節。

100小節という原稿指示は展開の設計であり、SUNOがその小節数を厳密に守ったという検証ではない。完成尺は別途Customで指定する。

## SUNO設定と、設定を確認する理由

|項目|採用値|理由・確認|
|---|---|---|
|モデル|v6|承認された音源を作った時点のモデル。モデル更新時は少数で聴き比べてから大量生成。|
|モード|Advanced / lyrics_styles|人が用意したStylesとオリジナル歌詞を直接投入。|
|Duration|Custom・4:00、240秒|ユーザーの明示指定。Stylesの「4 minutes」だけで代用しない。|
|Style Influence|80|採用時の値。新しいブラウザ接続では50へ戻る場合があったため、毎回読む。|
|Variety|Off、数値0|v6でStylesの書き換えを抑え、保存した指示を維持するため。|
|Weirdness|50|採用時の値。極端な変化を追加せず固定。|
|Max Mode|Off|採用時の設定を維持。|
|Vocal Gender / Voice Persona|指定なし|声の短い句と重ね方をStylesで指示。|

[SUNOのv6公式説明](https://help.suno.com/en/articles/13924481) を参照してVarietyをOffにした。実UIで0を読み戻し、承認された４テイクでは保存Stylesが入力文と一致していた。Style Influenceの効果を他の変更から切り離した比較はしていない。

Studioには `--duration-seconds 240` を追加済み。各曲のフォーム投入時にCustomへ切り替え、Durationの入力欄が4:00、スライダーの内部値が240であることを両方確認する。不一致ならその曲を送信しない。保存JSONの各曲にも `duration_seconds: 240` を持たせた。

**JSONに値を書くだけで全設定が適用されるとは考えない。** Studio本体が自動適用するのはこのDuration項目。他の設定は、使用するツールの実UI操作・対応機能で設定し、送信前に読み戻す。今回の実行はUIで80 / 0を設定し、各送信前に検証した。

スライダーへの単純な値の代入やキー操作が反映されないケースもあった。今回確実に反映したのはネイティブアクセシビリティのIncrement / Decrement。使用ツールが違う場合も、操作の成功を値の読み戻しで判断する。

## 新しい制作担当へ渡す共通指示

以下は、Fable、Claude、Codexなどへそのまま渡せる。

```text
先に docs/music/beat-soul-recipe.md を読み、config/music_presets/beat-soul-4min.json と
docs/music/beat-soul-inputs.md を制作条件の基準にしてください。

ユーザーが承認したのは、Come AroundのSynth Groove / String Grooveを基礎とした、
ビート中心のpsychedelic electronic soul / electronic funkです。
ラグジュアリーBGMという用途を、静かなジャズ・バラードへ読み替えないでください。

共通のドラム、２小節の低音反復、短い重ね声、冒頭の歌フックは維持します。
新しい曲ではフックと歌詞を独自に書き、102〜107 BPMの範囲とSynth / Stringの枝で
変化を作ってください。歌詞は短い句、Opening Chorusから開始、100小節を目安に設計。

SUNOはv6、Advanced、Custom 4:00、Style Influence 80、Variety Off、Weirdness 50、
Max Mode Off。設定を実画面または利用ツールの対応機能で読み戻してから送信します。
モデルやUIの変更で同じ設定が利用できないときは、黙って別設定へ置き換えません。

新しい方向を試す場合は、まず２曲４テイクで聴き比べます。
すでに承認された方向で本数を増やす指示なら、承認された骨格から直接展開できます。
SUNOは１リクエスト２テイク。10曲の制作なら20候補から各曲１テイクを選びます。
生成リクエストを送っただけでは完了にしません。

保存後に全体デコード、実際の尺、冒頭の歌入り、テンポ感、長時間の聴きやすさを確認。
歌入りが遅ければ、同じ曲の別テイクを先に比較し、必要なら短い導入を整えます。
原本を保持し、編集箇所を記録します。未保存やブラウザ障害では再生成せず、まず
既存Workspaceの生成状況とダウンロードを確認してください。

制作に必要なのはこのガイドと原稿です。特定ツールの会話履歴や個人メモリに
依存せず、採用した条件・変更点・検証結果を次の担当にも残してください。
```

## 実行と確認の手順

2026-09-29追記：選曲は[ハートによるセミオート方式](semi-auto-selection.md)へ変更。生成・２テイクのダウンロードまで自動化し、人が聴き比べて各タイトルから１テイクを採用する。下記の過去の尺・歌入りによる10曲選定は制作検証の記録で、今後の自動採用ルールではない。採用の正本は各セットの`.music_review.json`。生成タイトルは永久台帳へ予約し、使用済みタイトルの再利用を防ぐ。

1. Workspaceのローカル指示を読み、SUNO処理が走っていないこと、対象チャンネルと保存先を確認する。この音楽プリセットを別チャンネルの既定へ無断で上書きしない。
2. 正規入口 `python3 Python/studio.py suno-auto --vol <対象vol> --dry-run` で解決結果を確認。今回、active channelの既定はSUKIMA・instrumental_fillerだったため、歌付きのこの制作は独立Workspaceで実行した。
3. 新しい日付・企画名のWorkspaceと出力先を用意する。保存した10曲を再利用する場合、`--songs-file config/music_presets/beat-soul-4min.json --mode lyrics_styles --duration-seconds 240` を使える。タイトル・歌詞を変える場合は別の原稿JSONを作り、基準を上書きしない。
4. Create前にモデルと全コントロールを設定し、読み戻す。今回の参考実行は `output/suno-groove-ten-20260929/run_batch.py`。初回は確認待ち、各曲ではDuration・Style Influence・Varietyを再検証する。次のバッチでは、前回の確認済みマーカーを使い回さない。
5. 同じSUNOブラウザを同時利用しない。既存CLIの内部リソースロックを利用し、同じ種類の外側ロックを重ねない。
6. 完成してからダウンロード。今回の保存前ポーリングでタブがクラッシュしたが、20テイクは存在した。生成の再送信をせず、ダウンロード専用の接続へ切り替えて20/20を保存した。

```bash
# _claudeルートで実行。既存作品を再生成する指示がある時だけ使用する。
python3 Python/suno_auto_create.py \
  --songs-file config/music_presets/beat-soul-4min.json \
  --mode lyrics_styles --duration-seconds 240 \
  --workspace <新しいWorkspace> --auto-download <新しい保存先>

# 作成済み曲の取得のみ。ブラウザ障害で生成を重複させないための入口。
python3 Python/suno_auto_create.py \
  --download-workspace <作成済みWorkspace> --download-dir <保存先>
```

このコマンドだけでStyle Influence等まで設定されるとは限らない。前述のCreate前の確認を組み合わせる。ブラウザ自動操作は、その環境で許可されたインターフェースを使う。

## 合格基準と、うまくいかない時の修正

|確認項目|基準|外れたとき|
|---|---|---|
|冒頭|最初の数秒から歌の短いフックとビートがある。|別テイクを比較。歌詞をOpening Chorusへ。明示的な即時フック指定を確認。必要な導入調整は原本を残して記録。|
|テンポ感|細かい刻みとベースの反復が進み、サビでもグルーヴが維持される。|BPMだけ上げない。drum-loop、16分、ostinato、clipped vocalを先に確認。|
|音楽性|Synth / Stringの音色の枝と、声を重ねた電子ソウルの骨格が残る。|Variety OffとStylesの実値を確認。独唱、ピアノ主体、ジャズ独奏へ寄っていないか比較。|
|尺|生成設定はCustom 4:00。納品は３分半〜４分程度。|ffprobeで原本を読む。設定値だけで完成尺を断言しない。短い曲を無音で埋めない。|
|保存|20候補が実ファイルで存在し、全体デコードに成功。各タイトルから１曲を選べる。|Workspaceと生成済み件数を確認し、まず取得を再試行。|
|聴き流し|音色は上質で、過剰な高音、音量差、急なドロップがない。|冒頭だけで判断せず、サビ・Bridge・終わりも聴く。|

今回、20ファイルの全体デコードに成功。原本尺は238.1335〜241.5735秒で、Customを4:00にしても全音源が厳密な240秒にはならなかった。尺と歌入りを基準に10曲を選び、完成尺は234.645〜240.014秒、合計39分50秒。

Turn the Cornerの２テイクは冒頭５秒の音声解析で歌詞が確認できなかった。後の区間を確認し、採用版のみ約4.53秒の導入を短縮した。原本は保持。他の９曲は原本をそのまま採用。

冒頭の確認は、原稿を渡さない５秒区間のAI音声文字起こしを補助として使った。正確な歌い始め時刻や、主観的な音楽品質の証明ではない。楽器名のAI推定には揺れもあったため、推定だけで「参照と同じ音」とは判断しない。最終的な音楽性の基準は、ユーザーが承認した音源との聴き比べ。

## 記録の場所

- [正確な12曲分のStyles・Exclude・Lyrics](beat-soul-inputs.md)
- [機械可読プリセット：採用条件、２つの基準曲、10曲の原稿と元音源URL](../../config/music_presets/beat-soul-4min.json)
- `output/suno-beat-soul-20260929/`：承認された２案の４テイクと、当時の設定・検証。
- `output/suno-groove-ten-20260929/`：20原本、選定10曲、ZIP、歌詞、送信設定の全10回分の記録、音声解析、編集履歴、実尺・デコード検証。

ガイド・入力原稿・プリセットはGitで保存する。音源と実行記録は制作素材として保存する。同じ音源を使う場合は保存済みMP3を使う。同じ条件で新しく生成した場合、メロディ、声、細部まで同一になる保証はないため、音源の選定までを再現手順に含める。
