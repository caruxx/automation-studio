# SUNO 現行 DOM 採取・セレクタ突合（2026-10-04）

- 採取日時: 2026-10-04 15:10–15:17 JST。画面ごとに逐次追記し、最終照合を同日実施。
- 表示言語: 英語。`document.documentElement.lang = "en"`。日本語 UI へは変更していない。
- Chrome: 154.0.8037.95（`/Applications/Google Chrome.app/Contents/Info.plist` の CFBundleShortVersionString）。
- 採取方法: この Mac の正式 Google Chrome、CUA Chrome extension 接続、調査専用タブ 1448698461。`tab.playwright.evaluate` 内の `querySelectorAll` / `getAttribute` で DOM 属性採取、Playwright locator の `count()` で照合。AX は操作先と表示確認の補助。スクリーンショット推測なし。
- 記号: `∅` は属性なし、`""` は空文字。role 欄は明示 role（button/input 等の暗黙 role とは区別）。入力値・プロフィール内容は非採取。動的 ID と CSS ハッシュは観測値であり、固定セレクタとして推奨しない。
- 対象ソース開始時 SHA-256: `Python/suno_auto_create.py` = `66e5b25c5c7b2beae1dd541f77503338290ba80144056a63bb8346a848cd227e`、`Python/suno_fast_dl.py` = `01b01b75ba5f5f1fb287a44439def699c5b920092f721af368a4a3f1a68302f2`。

## 1. /create

URL: `https://suno.com/create`。初期状態で Songs / Advanced 選択済み。ログイン画面なし。操作はフォームの `More Options Variety` の展開のみ（`aria-expanded=false → true`）。初期表示の既存 workspace は WobbleDay_Vol3_BC_4min_20261004。

| 要素 | タグ名 | role | aria-label | placeholder | data-testid | 表示テキスト | 識別に使える祖先・付記 |
|---|---|---|---|---|---|---|---|
| シンプル | button | tab | ∅ | ∅ | ∅ | Simple | `div[role=tablist][aria-label="Create form mode"]`、aria-selected=false |
| アドバンスド | button | tab | ∅ | ∅ | ∅ | Advanced | 同上、aria-selected=true、1件 |
| サウンド | button | tab | ∅ | ∅ | ∅ | Sounds | `div[role=tablist][aria-label="What to create"]`、aria-selected=false。Songs/Speech と同群で、Simple/Advanced と別群 |
| 歌詞の種別: 書く / プロンプト / インスト | 未検出 | 未検出 | 未検出 | 未検出 | 未検出 | 該当切替なし | SUNO 本体の `[role=radio]` / `[role=radiogroup]` は0件。空歌詞はインストという表示 `Start writing lyrics, or leave this empty for instrumental` あり。別モデルでの状態は未確認 |
| 歌詞エディタ | div | textbox | Lyrics editor | ∅ | ∅ | 空欄、案内は別要素 | `contenteditable=true`、class=`lyrics-editor-content touch-pan-y`、祖先 `.lyrics-editor-scroll` / `.lyrics-editor-scroll-scope`、1件 |
| 歌詞補助プロンプト | textarea | ∅ | Cowriter prompt | "" | ∅ | 値非採取 | 祖先 `.cowrite-animated-border`。歌詞本体とは別要素 |
| Styles 入力 | textarea | ∅ | ∅ | slow pace, haunting whispers, hard drill, latin rap, rock metal | ∅ | 値非採取 | 祖先 `div[data-testid="create-form-styles-wrapper"]`、配下 textarea 1件。placeholder は推薦スタイル列 |
| Exclude 入力 | input | ∅ | ∅ | Exclude styles | ∅ | 値非採取 | 親 `div.css-8l9j3e.e1tffvjs0`、祖先 `.css-gwrmef.eodlc7e0`、1件 |
| その他のオプション開閉 | div | button | ∅ | ∅ | ∅ | `More Options` と `Variety`（textContent=`More OptionsVariety`、accessible name=`More Options Variety`） | `aria-expanded` false→true、class=`css-1gs0dqv e1jccsq71`、親 `.css-1xt3ue1.e1jccsq70`。曲行の More options と区別が必要 |
| 曲名 | input（2件） | ∅ | ∅ | Song Title (Optional) | ∅ | 値非採取 | 親 `div.css-2nnn4s.emvnm751`、同一 placeholder 2件。AX で表示される入力は1件で、DOM単純一致は一意でない |
| Custom 尺切替 | button | ∅ | ∅ | ∅ | ∅ | Custom / Auto | 親 `.css-o6pnyv.eodlc7e2`、祖先 `.css-gwrmef.eodlc7e0` 内に Duration。Auto が選択表示 |
| Custom 尺入力 | 未確認 | 未確認 | 未確認 | 未確認 | 未確認 | 未確認 | Auto 状態で duration/mm:ss 系 input は0件。生成設定に関わる Custom 切替は実施せず、入力属性は未確認 |
| モデル選択 | button | ∅ | ∅ | ∅ | ∅ | v6 | `aria-expanded=false`、id=`base-ui-_r_b_`、親 `.relative.flex.w-fit.flex-col.rounded-full`。メニュー・選択変更は実施せず |
| 作成ボタン | button | ∅（暗黙button） | Create song | ∅ | ∅ | Create | `type=button`、id=`base-ui-_r_23_`、disabled。親 `.relative.flex.items-center.justify-stretch.gap-4`。クリックなし |
| ログイン済み指標 | button | ∅（暗黙button） | Profile menu button | ∅ | profile-menu-button | "" | aria-expanded=false、祖先 `.group/profile-row`。1件、開かず |

### /create の読取照合カウント

- `getByRole('tab', name='Advanced', exact=True)`=1、Custom tab=0。
- `[contenteditable="true"][role="textbox"][aria-label="Lyrics editor"]`=1。
- `[data-testid="create-form-styles-wrapper"] textarea`=1、Exclude styles placeholder=1、Song Title (Optional) placeholder=2。
- `getByRole('button', name='Create', exact=True)`=0、name='Create song'=1。表示テキストと accessible name が異なる。
- `getByRole('button', name=/^(More Options|More options|Advanced Options|その他のオプション)$/)`=17（曲行の More options）。フォーム見出しは要約 Variety が付くため一致しない。
- `button[role="radio"]` を Write/書く で絞ると0件。現行入力経路のフォールバック可否は末尾の突合表に記載する。
- 日本語の「作成」「曲名」「その他のオプション」の実 DOM 値は未確認。英語値からの翻訳を事実として記載しない。

## 2. /me/workspaces

URL: `https://suno.com/me/workspaces`。Library 内の Workspaces タブ選択済み。初期ロード中の0件を最終結果とせず、読み込み後に再採取。既存カードの属性読取のみ。

| 要素 | タグ名 | role | aria-label | placeholder | data-testid | 表示テキスト | 識別に使える祖先・付記 |
|---|---|---|---|---|---|---|---|
| 既存カード | div | button | ∅ | ∅ | ∅ | WobbleDay_Vol3_BC_4min_20261004 / 20 songs · 6h, 4m ago | class=`group css-1ix5az0 e836jyc6`、親 `div.css-1af1jwx.e836jyc8`。textContentでは名前と曲数の間が空白なしで連結 |
| カード名（補助採取） | span | 非採取 | 非採取 | 非採取 | 非採取 | WobbleDay_Vol3_BC_4min_20261004 | class=`css-1haxbqe e836jyc3`、祖先 `div[role=button]`。完全一致getByText=1件 |
| カード補助テキスト（補助採取） | span | 非採取 | 非採取 | 非採取 | 非採取 | 20 songs · 6h, 4m ago | class=`css-mdiur3 e836jyc4`、同カード内 |
| カード画像（altのみ補助採取） | img | 非採取 | 非採取 | 非採取 | 非採取 | 非採取 | `alt=""`。`Cover image for ...` は付いていない |
| アーカイブ展開 | 未確認 | 未確認 | 未確認 | 未確認 | 未確認 | Archived / アーカイブ を検出できず | 読み込み後も全 `button,[role=button],summary` と葉テキストを検索して0件。アーカイブ有無・表示条件不明のため不一致と断定せず |

照合: `div[role=button]`=21件。対象名の `Cover image for ...` を持つカード=0件、ページ全体の `img[alt^="Cover image for"]`=0件。一方、対象名完全一致の span を持つ `div[role=button]`=1件。画像alt経路は不一致だが、既存のspanフォールバックは一意に一致する。

## 3. 既存ワークスペースの曲一覧

カード名を1回クリックして開いた URL: `https://suno.com/create?wid=7fcecbfa-9573-4066-922a-74d64e45f3a5`。対象名は WobbleDay_Vol3_BC_4min_20261004。Filters (3) の表示だけを開閉し、選択項目は触っていない。再生・一時停止・ページ移動・ダウンロード・フィルター解除は行っていない。

| 要素 | タグ名 | role | aria-label | placeholder | data-testid | 表示テキスト | 識別に使える祖先・付記 |
|---|---|---|---|---|---|---|---|
| 曲行 | div | group | Last Crumb at the Cafe（サンプル） | ∅ | clip-row | 4:00 / Last Crumb at the Cafe / スタイル等（本文省略） | class=`clip-row css-1p7z1wh eambqzg0 suno-fast-downloaded`、祖先 `.css-u0rgu7.e3xclhq0`。draggable/data-clip-id/data-idは∅。DOMに17行、AX末尾にUntitledプレースホルダー3件 |
| 曲リンク | a | ∅（暗黙link） | ∅ | ∅ | ∅ | Last Crumb at the Cafe | `href="/song/7df76eec-794e-4249-ab80-54ab65726326"`、親 `.clip-title-wrapper.css-1n53hb0.eambqzg12`、祖先 `[data-testid=clip-row]`。サンプル行内1件 |
| 再生 | div | button | Play Last Crumb at the Cafe | ∅ | ∅ | 4:00 | class=`clip-image-container cursor-pointer css-16jurbr eambqzg2`、親 `.css-8yp4m0.eambqzg5`、祖先 `[data-testid=clip-row]`。サンプル行内 `_PLAY_BUTTON`=1件 |
| 一時停止 | 未確認 | 未確認 | 未確認 | 未確認 | 未確認 | 未確認 | `_PAUSE_BUTTON` は0件。停止状態で採取し、再生操作は禁止のため Pause 状態を作っていない |
| ページ番号 | input | ∅ | Current page number | ∅ | ∅ | 現在1（AXの表示値） | class=`css-7qsfl3 e14u0cw31`、親 `.css-3tlztn.e14u0cw30`、1件 |
| 次ページ | button | ∅ | Next page | ∅ | ∅ | "" | 同じページ操作親。`type=button`、disabled属性なし。1件、未クリック |
| 前ページ | button | ∅ | Previous page | ∅ | ∅ | "" | 同じページ操作親。`type=button`、`disabled=""`。1件、未クリック |
| 曲数表示 | div | ∅ | ∅ | ∅ | ∅ | 20 songs | 子要素なし。親 `.css-14o6w4f.e17zo7t25` → `.css-1yg2fsg.e17zo7t24` → `.clip-browser-list-scroller`。英語正規表現に1件一致 |
| フィルター開閉 | button | combobox | Filters (3) | ∅ | ∅ | Filters (3) | type=button、aria-expanded false→true→false。親 `.css-4ot296.e6zpnnu0`。`button[aria-label^="Filters"]:visible`=1件 |
| フィルターメニュー | div | listbox | ∅ | ∅ | ∅ | 下記項目を含む | 表示中listbox=1件。optionの祖先として識別可能 |
| 選択済みフィルター1 | div | option | Hide disliked clips | ∅ | ∅ | Hide Disliked | class=`hxc-menu-item`、`aria-selected=true`、祖先 `[role=listbox]` |
| 選択済みフィルター2 | div | option | Hide Stems | ∅ | ∅ | Hide Stems | 同上 |
| 選択済みフィルター3 | div | option | Hide Clips from Edit Mode | ∅ | ∅ | Hide Clips from Edit Mode | 同上 |

### 曲一覧の読取照合

- サンプルUUIDで曲行を絞ると1行、その行内リンク1件・再生候補1件。別テイクの同名曲があるのでタイトルだけの特定は避ける。
- 曲数20に対して現在DOMのclip-rowは17。AXにはUntitledプレースホルダー3件もあるが、差分の原因は未確定。17を総曲数として扱わない。今回、全曲取得・全ページ巡回の機能検証はしていない。
- Filtersの数値3と `listbox [role=option][aria-selected=true]` の3件が一致。閉じた後も `aria-label="Filters (3)"` で、解除・設定変更なし。
- 英語の Play / Current page number / 20 songs を確認。日本語表示時の再生・一時停止・ページ番号・曲数は未確認であり、英語一致をもって日本語対応済みとは判定しない。

### 補足採取（同じ /create?wid=...、設定変更なし）

- `More Options` 見出しの実 `innerText` は `More Options\nVariety`。Pythonと同じNFKC・空白圧縮・小文字化では `more options variety` となり、`more options` の完全一致候補は0件。
- `_set_custom_duration` の `get_by_role("button", name=/^(More Options|その他のオプション)/i)` と同じlocatorは18件（フォーム見出し1、曲行のaria-label=`More options`が17）。この画面ではフォーム見出しが閉じており、Custom/Duration textbox/Duration slider のロール解決はそれぞれ0件。
- 曲名inputは2件だが、Playwright `isVisible()` はDOM順に `[false, true]`。Createから3階層の最小共通祖先 `div.css-1ofqeah.e1kr3w888` 内でも2件。現行helperは不可視候補を除外していない。
- Lyrics primary=1、data-lexical fallback=0、Create testid fallback=0。歌詞本体は `contenteditable=true`、`aria-disabled`なし。
- Styles見出しを現行コードと同じ候補タグ・正規化完全一致で探すとネストした4要素が該当するが、1～4階層の祖先探索はすべて同一textarea 1件へ収束。workspace移動後の推薦placeholderは `drumstep, r&b neo-soul, dark basslines, heavy alternative rock, flutter` に変わった。Stylesはplaceholder固定より見出し/祖先testidが適切。
- 全17曲行で、行内 `/song/` リンク1件、`_PLAY_BUTTON`1件。`is_suno_logged_in` が見る Createテキストのbuttonは1件、textareaは5件。プロフィール指標とも整合するが、未ログイン画面での誤検知試験は未実施。

## 4. セレクタ突合表

**判定単位は対象要素の現行処理経路（主経路とfallbackを合わせたもの）。** 0件の旧UI用fallbackも明記するが、別経路で一意に解決できる場合は重複して不一致へ加算しない。集合取得の曲行・選択済みfilterは意図した複数件。曲名のように単一入力が目的で不可視候補を含む場合は一意性不一致とする。日本語の実DOMは3つの指定項目群で別判定する。`∅`は該当セレクタ実装なし。

| 画面 | 要素 | 現行 DOM の事実 | .py の現行セレクタ（ファイル:行） | 判定（一致・不一致・未確認） | 不一致時の修正案 |
|---|---|---|---|---|---|
| /create | Advanced | tab Advanced=1、aria-selected=true | `Python/suno_auto_create.py:3351` `[role="tab"]` + Advanced/アドバンスド正規化完全一致（候補3203、検証3355–3359） | 一致 | — |
| /create | Simple / Sounds | SimpleとSoundsは存在、別tablist | `Python/suno_auto_create.py:3548`–3558 の現行inject経路はAdvancedのみ。Simple/Sounds専用選択は∅ | 未確認 | 対応実装がないため突合対象なし。DOM属性は上表で確認済み |
| /create | 歌詞種別 Write と直接編集fallback | Write radio=0、編集可能な歌詞本体=1 | `Python/suno_auto_create.py:3370` `button[role="radio"]` + Write/書く；3371–3375で歌詞本体へfallback | 一致 | radio単体は0件。現行v6経路は直接編集fallbackで成立 |
| /create | 歌詞種別 Prompt / Instrumental | 切替radio群なし。空欄はインストという案内あり | `Python/suno_auto_create.py:3548`–3558 の現行injectに専用選択は∅。旧経路の`Instrumental`/`Lyrics` button候補は2799、2807–2812 | 未確認 | 旧モデル切替を行っていない。現行生成経路に旧切替を追加する根拠なし |
| /create | 歌詞エディタ | primary=1、Lyrics editorのaria経路=1、data-lexical経路=0 | `Python/suno_auto_create.py:3393` `div.lyrics-editor-content[contenteditable="true"][role="textbox"]`；3394 `[contenteditable="true"][role="textbox"][data-lexical]`；3405–3409 aria候補 | 一致 | —（旧fallback0件は主経路の障害ではない） |
| /create | Styles | 各見出し候補の祖先内で同一textarea 1件へ収束 | `Python/suno_auto_create.py:3581` `_find_section_control(..., "textarea")`；3258–3286 見出し候補タグ/8階層探索、3210–3214/3276–3282 Sounds用placeholder除外 | 一致 | —。`[data-testid="create-form-styles-wrapper"] textarea`も1件 |
| /create | Exclude | input placeholder=`Exclude styles` 1件（オプション展開時） | `Python/suno_auto_create.py:3608`–3613 `input[placeholder]` + Exclude styles/スタイルを除外完全一致、候補3208 | 一致 | — |
| /create | その他のオプション開閉（完全一致経路） | `More Options\nVariety`、正規化完全一致0件 | `Python/suno_auto_create.py:3428`–3432 `[aria-expanded]` + `_MORE_OPTIONS_TEXTS`完全一致（候補3206） | 不一致 | Createフォーム内の`[role="button"][aria-expanded]`に限定し、見出しのMore Options/その他のオプション接頭辞または見出し子要素で特定。可視候補1件を要求 |
| /create | 曲名入力候補の一意性 | placeholder=`Song Title (Optional)` 2件、可視性false/true。Create共通祖先内にも2件 | `Python/suno_auto_create.py:3462`–3489 `input[placeholder]`、曲名/任意またはsong title接頭辞、Createから最大12階層。3492–3517で候補を順次入力 | 不一致 | 同helperで可視・編集可能候補に絞り1件を要求。現行はhidden失敗後に次候補へ進むため入力失敗は未確定、待機遅延懸念 |
| /create | Custom尺用オプション開閉（接頭辞経路） | role/nameの同じregexが18件。closed時Custom/Durationは0件 | `Python/suno_auto_create.py:3644` `get_by_role("button", name=re.compile(r"^(More Options\|その他のオプション)", re.I))`；3647–3648で`.click()` | 不一致 | 上記のフォーム限定helperを共用。曲行aria-label=`More options`を除外し、単一候補を検証して開く |
| /create | Custom尺切替 | 展開時button Customを1要素観測。Auto選択。切替は未実行 | `Python/suno_auto_create.py:3646` /3651 `get_by_role("button", name="Custom", exact=True)` | 一致 | —（Customの設定変更はしていない） |
| /create | Custom尺入力・slider | Auto状態で入力なし。属性・Custom選択後の一意性は未確認 | `Python/suno_auto_create.py:3645` slider Duration exact、3649 textbox Duration exact、3659 slider Duration exact | 未確認 | 設定を変えずに観測できるCustom状態で追加採取が必要 |
| /create | モデル選択 | button v6、aria-expanded=false | `Python/suno_auto_create.py:1215`–1230、4133 のmodelはLLM用。SUNOモデル選択DOMセレクタは両ファイルに∅ | 未確認 | 専用実装との突合対象なし。モデル変更なし |
| /create | 作成ボタン（英語） | aria-label=`Create song` 1件、text=`Create`、testidなし | `Python/suno_auto_create.py:3789`–3794 `button[aria-label]` Create song/曲を作成 → button text 作成/Create；3813 `button[data-testid="create-button"]`.first | 一致 | aria主経路1。testid旧fallback0、role name完全一致Createは0だが現行コードはその検索を使わない |
| /create | ログイン判定 | ログイン済みprofile testid1、Createテキストbutton1、textarea5 | `Python/suno_auto_create.py:2282`–2292 suno.com URL + button text Create/作成 またはtextarea存在 | 一致 | 当該ログイン済み画面で成立。未ログイン誤判定の有無は今回の対象外 |
| /me/workspaces | 既存カード | cover画像alt経路0、完全一致名前spanを持つカード1 | `Python/suno_auto_create.py:1355`–1361 `div[role="button"]:has(img[alt="Cover image for {name}"])` → `div[role="button"]` + `span:text-is("{name}")` | 一致 | span経路で一意。cover画像はalt空文字なので第一候補は現DOMに一致しない |
| /me/workspaces | カード最終fallback | 対象名getByText exact=1 | `Python/suno_auto_create.py:1400` `get_by_text(workspace_name, exact=True).first`。曖昧fallbackは1375–1385でprefix/残余曲数判定 | 一致 | exact spanが成立するので曖昧経路は不要。実行していない |
| /me/workspaces | アーカイブ展開 | Archived/アーカイブが採取DOMに0件。表示条件不明 | `Python/suno_auto_create.py:1439`–1443 `button,[role="button"],summary` + `/^Archived(?:\s\|$)/i`、aria-expanded確認 | 未確認 | アーカイブ要素が見える状態で追加採取。日本語表記も未確認 |
| /create?wid | 曲行 | `[data-testid="clip-row"]`17件。集合取得であり複数は意図どおり | `Python/suno_fast_dl.py:332`–335 `_WORKSPACE_ROWS_DOM` clip-row；`Python/suno_auto_create.py:2176` /2214/2242 clip-row または `div[draggable="true"]` | 一致 | —。DOM上の17件と総曲数20件は区別する |
| /create?wid | 曲リンクとUUID | 各17行にリンク1件。サンプルhref=/song/7df76eec-794e-4249-ab80-54ab65726326 | `Python/suno_fast_dl.py:337`–342 `a[href*="/song/"]` + UUID regex；`Python/suno_auto_create.py:2216`–2224 同リンク、img/src、data-clip-id/data-id代替 | 一致 | —。サンプルはhref経路で成立し、画像URL等の代替は採取不要 |
| /create?wid | 特定曲行 | UUIDで絞ると1件。曲名は別テイクと重複 | `Python/suno_fast_dl.py:671`–673 clip-row.filter(has=`a[href*="/song/{song_id}"]`).first | 一致 | — |
| /create?wid | 再生（英語） | div role=button、aria=`Play Last Crumb at the Cafe`。各行1件 | `Python/suno_fast_dl.py:664` `_PLAY_BUTTON = button[aria-label^="Play " i], [role="button"][aria-label^="Play " i]` | 一致 | buttonタグ側ではなくrole側が一致。未クリック |
| /create?wid | 一時停止（英語） | Pause接頭辞0件。再生状態を作っていない | `Python/suno_fast_dl.py:665` `_PAUSE_BUTTON = button[aria-label^="Pause " i], [role="button"][aria-label^="Pause " i]` | 未確認 | 既に再生中の状態を別途読み取れる時に確認。今回の操作禁止を維持 |
| /create?wid | ページ番号（英語） | input aria=`Current page number` 1件、ページ1 | `Python/suno_fast_dl.py:350`–354 英語input/日本語input/英語spinbutton、最初の可視要素 | 一致 | 日本語候補はコード内にあるが、現DOMでの日本語一致は未確認 |
| /create?wid | 次/前ページ | Next page=1、Previous page=1、前のみdisabled | `Python/suno_fast_dl.py:355`–361 `button,[role=button]` + Next page/次のページ、Previous page/前のページ完全一致 | 一致 | ページ移動の動作は未実行 |
| /create?wid | 曲数（英語） | 子なしdiv=`20 songs` 1件、ページ操作と共通祖先を持つ | `Python/suno_fast_dl.py:388`–403 leaf検索、`/^([\d,]+)\s+songs?$/i`、405以降で祖先検証 | 一致 | 日本語の総曲数文字列は推測しない |
| /create?wid | Filters開閉 | button aria=`Filters (3)`、可視1件 | `Python/suno_fast_dl.py:499` `button[aria-label^="Filters"]:visible`、502–511 label fullmatch | 一致 | 英語表示でのみ確認 |
| /create?wid | フィルター解除対象の特定 | 可視listbox1件、選択済みoption3件、表示件数3と一致 | `Python/suno_fast_dl.py:521`–526 `[role="listbox"]:visible`、`[role="option"][aria-selected="true"]` | 一致 | 対象の属性のみ確認。解除クリック・機能呼び出しは未実行 |
| /create?wid（日本語） | 再生/一時停止aria-label（Step 3-1） | 英語セッションのみ。日本語の実aria-labelは採取できず | `Python/suno_fast_dl.py:664`–665 Play/Pause英語prefixのみ | 未確認 | 日本語表示の既存セッションで観測してから候補追加。推測の「再生」「一時停止」を確定値にしない |
| /create?wid（日本語） | clip-row・ページ番号・曲数（Step 3-2） | 英語ではclip-row、Current page number、20 songs。日本語表示での実値は未採取 | `Python/suno_fast_dl.py:332`、350–351、402。ページ番号のみ日本語候補あり | 未確認 | 日本語UIで3要素を再採取。英語onlyの曲数regexの日本語適合性は未確定 |
| /create（日本語） | 作成・曲名placeholder・その他のオプション（Step 3-3） | 英語実値はCreate song/Create、Song Title (Optional)、More Options + Variety。日本語実値は未採取 | `Python/suno_auto_create.py:3206`–3207、3462–3468、3789–3794（日本語候補あり） | 未確認 | 言語設定は変更せず、日本語UIの実値を別途採取。現コード候補をDOMの事実として転記しない |

### 集計とTask 7への入力

- 不一致 **3件**: その他のオプション完全一致経路、Custom尺用オプション接頭辞経路、曲名入力候補の一意性。対象UI要素は2種類。
- 最初の2件はDOM件数とコードの分岐から失敗条件を確認（クリック実行での再現はしていない）。曲名は候補順次処理があるため、失敗確定ではなく一意性と待機遅延の懸念。
- 未確認 **9行**: Simple/Soundsの専用経路、Prompt/Instrumentalの専用経路、Custom尺入力/slider、SUNOモデル選択経路、アーカイブ展開、Pause状態、日本語の指定3項目群。属性未採取・実装不存在・言語未確認の理由は各行に記載。
- 旧UIfallbackの0件（Write radio、lyrics data-lexical、Create testid、workspace cover alt）は上表に記載済み。現在の主経路/代替経路が成立するので修正必須の3件には含めない。
- 最小の修正候補は、Createフォーム内に限定したオプション見出しhelperの共用、および曲名候補の可視・編集可能1件への限定。ソース修正・生成・設定変更は実施していない。Task 7の承認取得・実装は司令塔の担当。
