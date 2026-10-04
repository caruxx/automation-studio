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

## 5. 数値調整項目（追加採取）

### 5.1 採取条件・開始状態

- Task 6b、2026-10-04 17:28 JST開始。このMacの正式Google Chrome、CUA Chrome extension接続、新規調査タブ `1448698493`、URL `https://suno.com/create`。既存の節1–4は保持。
- `tab.playwright.evaluate` のDOM `getAttribute` / inputの `.value` とPlaywright locatorの操作・countで採取。属性を画像から推測しない。`∅`=属性なし、`""`=空文字。動的ID/CSSハッシュは観測用。
- 開始状態: `lang=en`、Songs/Advanced選択、モデル`v6`、More Optionsの`aria-expanded=false`、モデルメニュー`aria-expanded=false`。ログイン画面なし（email/password input数0、profile-menu-button数1。値・プロフィール本文は非採取）。
- 調査対象は作成フォームの数値項目。曲一覧ページ番号、playbar、拡張機能の項目は対象外。Create・再生・DLなし、本文欄への入力なし、モデル変更なし。
- 開始時の設定値は各項目で記録し、最小限の変更直後に復元・DOM読み戻しを行う。モデル/モード別の表示条件は切り替えず、観測できた範囲のみ記載。

### 5.2 More Optionsと数値項目の初期採取（17:29 JST）

- 実操作した一意な見出し: `page.locator('div[role="button"][aria-expanded]').filter(has_text=re.compile(r'^More Options'))`。CUA JSでは`filter({hasText:/^More Options/})`、count=1、click成功、`aria-expanded=false → true`。曲行のbuttonと区別できる。既存ソースのrole/name前方一致の問題は節末尾で再照合。
- DurationのAuto/Customは共通親`div.css-o6pnyv.eodlc7e2`、祖父`div.css-gwrmef.eodlc7e0`内の別子に表示ラベルDuration。`button`、明示role/aria-label/data-testid=∅、type=button、tabindex=0。開始時`Auto[data-selected="true"]`、`Custom[data-selected="false"]`。Duration textboxは0件。
- Weirdness開始50、Style Influence開始50、Variety開始0（Off）。各`get_by_role('slider', name=項目名, exact=True)`=1件。
- raw DOMの`[role="slider"][aria-label="Variety"]`は2件（もう1件はNormal/1、Background musicと同じ親）。ロールlocatorでは1件となるため、raw CSSの先頭や件数だけで操作しない。別モードへの切替は未実施。

|項目|タグ名 / role / aria-label|aria-valuemin / max / now / text|type / min / max / step / placeholder / data-testid|開始表示|
|---|---|---|---|---|
|Weirdness|div / slider / Weirdness|0 / 100 / 50 / ∅|すべて∅|50%、slider内補助文Expected results|
|Style Influence|div / slider / Style Influence|0 / 100 / 50 / ∅|すべて∅|50%、slider内補助文Moderate|
|Variety（操作対象）|div / slider / Variety|0 / 4 / 0 / Off|すべて∅|Off、slider内補助文Exact style|

3スライダーとも`tabindex=0`、`aria-labelledby=∅`。各親は`div.css-gwrmef.eodlc7e0`で、子の順序は①ラベルspanと補助svgを含むdiv、②`div[role=slider][aria-label=項目名]`、③表示値div（50%/Off）。CSSクラスはハッシュを含むため固定推奨しない。ラベルとの位置関係は共有親内の兄弟で、明示aria-labelによる単独特定が可能。作成フォームのMore Options展開が表示条件。現在Songs/Advanced/v6のみを確認。

### 5.3 Duration（Custom尺、17:29–17:31 JST）

開始状態はAuto。`get_by_role('button', name='Custom', exact=True)`=1をclickするとAuto/Custom両ボタンがDOMから消え、入力とスライダーが各1件出現した。Custom直後の初期表示は`3:00`、180秒。

|要素|表示ラベル|タグ / role / aria-label|aria-valuemin / max / now / text（Custom直後）|type / min / max / step / placeholder / data-testid|その他|
|---|---|---|---|---|---|---|
|尺入力|Duration|input / ∅（暗黙textbox）/ Duration|すべて∅|text / ∅ / ∅ / ∅ / ∅ / ∅|inputmode=decimal、valueプロパティ=`3:00`、pattern/maxlength/tabindex/aria-labelledby=∅|
|尺スライダー|Duration|div / slider / Duration|10 / 360 / 180 / 3 minutes|すべて∅|tabindex=0、aria-disabled=false、aria-labelledby=∅|

- 親`div.css-gwrmef.eodlc7e0`の直接の子は、①Durationのspanとsvgを含むdiv、②Duration slider、③Duration input。inputとsliderは兄弟で、単独のaria-labelでは2件になる。roleとnameの組を使う。inputにmin/max/step属性はなく、範囲はsliderのARIAで**10–360秒（0:10–6:00）**と確認。上下限・範囲外の実入力は未検証。
- 実測: `fill('4:01')`直後はinput.value=`4:01`だがsliderは`aria-valuenow=180`、`aria-valuetext='3 minutes'`のまま。`press('Tab')`後に241 / `4 minutes, 1 second`へ更新し、input.value=`4:01`を保持。秒の生数値入力やEnter確定は未検証。今回の表記は分をゼロ埋めしない`m:ss`。
- 設定手順（実操作済み、Python表記）: More Optionsを5.2の一意なselectorで展開 → `page.get_by_role('button', name='Custom', exact=True).click()` → `field = page.get_by_role('textbox', name='Duration', exact=True)` → `field.fill('4:01')` → `field.press('Tab')`。既にCustomならボタンは存在しないため、まず可視textboxの有無を読む。
- 読み戻し（実操作済み）: `input[aria-label="Duration"]`のDOM `.value`と`get_by_role('slider', name='Duration', exact=True).get_attribute('aria-valuenow')` / `aria-valuetext`。CUAではread-only evaluateで`.value`取得。標準Playwrightの`input_value()`へ置換するコード自体は今回未実行だが、同じDOMプロパティを読む現行ソースの目的と整合する。値241秒との二重照合に成功。
- **復元済み**: Duration固有のリセットsvgへクリックイベントを送り、`Auto[data-selected=true]`、`Custom[data-selected=false]`、Duration input=0 / slider=0を17:31:19 JSTに読み戻した。Weirdness/Style Influence/Varietyは50/50/0のまま。

復元経路の注意（実測を区別）:

1. Custom状態ではAutoボタンがない。リセット対象は `div:has(> input[aria-label="Duration"]) > div > svg[data-base-ui-tooltip-trigger]`（count=1）。タグsvg、role/aria-label/tabindex/data-testid=∅。動的idは`base-ui-_r_3j_`で固定利用しない。
2. 通常のlocator `.click()`は今回**復元不成立**。既存拡張パネルがアイコンに重なり、`elementFromPoint`でも別要素を確認。フォーム値は4:01/241のままで、同じクリックの再試行はしていない。拡張の設定変更・パネル開閉は行っていない。
3. 復元を目的に`field.fill('')` → Tabを1回試したが4:01/241へ戻り、空欄でAutoにはならなかった。試験尺の投入は4:01の1回だけ。
4. CUAの調査タブ限定CDP capability `Runtime.evaluate` で、上記の一意なsvgに `dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true}))` を送る方法でAutoに戻った。DOM要素へのUIイベントのみで、内部アプリ状態・storage・Cookieはアクセスしていない。標準Playwrightの`locator.dispatch_event('click')`相当は**未検証**として扱う。通常クリックが遮られていない環境での復元も未検証。
5. Durationスライダー自体の矢印/ドラッグ操作、step、任意値全域、生成結果への反映は未検証。今回の設定検証はinput→Tab→sliderの読み戻しまで。

### 5.4 Weirdness（17:32 JST）

- DOM属性・構造は5.2の表のとおり。開始`aria-valuenow=50`、表示`50%`、範囲0–100%。`step`属性なし。実測の矢印1回は**1ポイント**。
- 検証済みselector: `s = page.get_by_role('slider', name='Weirdness', exact=True)`、count=1。`s.press('ArrowRight')`でフォーカスされ、**50→51**、表示`51%`に即時更新。Enter/blurなしでDOMに反映した。
- 読み戻し: `s.get_attribute('aria-valuenow')`（数値文字列）、`aria-valuetext`は∅なので数値として使わない。DOMで`e.nextElementSibling.textContent`の`51%`も確認。
- 復元: `s.press('ArrowLeft')`で**51→50**、表示`50%`を17:32:03 JSTに確認。復元成功。
- 実証できた設定は1ステップ増減。任意の目標値へ差分回数だけpressする方式、Home/End、ドラッグ、直接value設定は未検証。表示条件はSongs/Advanced/v6、More Options展開中。他モデル・別モード・音声追加時は未検証。

### 5.5 Style Influence（17:32 JST）

- DOM属性・構造は5.2の表のとおり。開始`aria-valuenow=50`、表示`50%`、範囲0–100%。`step`属性なし。実測の矢印1回は**1ポイント**。
- 検証済みselector: `s = page.get_by_role('slider', name='Style Influence', exact=True)`、count=1。`s.press('ArrowRight')`でフォーカスされ、**50→51**、表示`51%`に即時更新。Enter/blurは不要だった。
- 読み戻し: `s.get_attribute('aria-valuenow')`とDOMの`e.nextElementSibling.textContent`。`aria-valuetext`は∅。
- 復元: `s.press('ArrowLeft')`で**51→50**、表示`50%`を17:32:20 JSTに確認。復元成功。
- 任意目標までの複数press、Home/End、ドラッグ、直接value設定は未検証。表示条件はSongs/Advanced/v6、More Options展開中。他モデル・別モード・音声追加時は未検証。

### 5.6 Variety（17:32 JST）

- DOM属性・構造は5.2の表のとおり。開始`aria-valuenow=0`、`aria-valuetext=Off`、表示`Off`。範囲は**0–4の段階値**。%ではない。`step`属性なしだが矢印1回の実測差は1。
- 検証済みselector: `s = page.get_by_role('slider', name='Variety', exact=True)`、count=1。`s.press('ArrowRight')`で**0/Off→1/Normal**。Enter/blurなしでDOMと表示の両方に反映。
- 読み戻し: `s.get_attribute('aria-valuenow')` + `s.get_attribute('aria-valuetext')`、DOMの`e.nextElementSibling.textContent`もNormalで一致。
- 復元: `s.press('ArrowLeft')`で**1/Normal→0/Off**、17:32:37 JSTに数値・aria-valuetext・表示Offの一致を確認。復元成功。
- 2–4の表示名、Home/End、ドラッグ、任意値への直接設定は未検証。初期非対象のVariety（raw DOMでNormal/1）は操作していない。表示条件はSongs/Advanced/v6、More Options展開中。他モデル・別モード・音声追加時は未検証。

### 5.7 非表示DOM内の補足数値項目（17:33 JST、設定操作なし）

作成フォーム共通祖先`div.css-1ofqeah.e1kr3w888`内を追加点検した。現在表示されている4種類とは別に、以下の非表示コントロールがDOMに存在する。表示モード・モデルを変えていないため、存在の採取と設定の動作検証を区別する。

|項目|ラベル / タグ / role / aria-label|aria-valuemin / max / now / text|type / min / max / step / placeholder / data-testid|採取時の値と可視性|
|---|---|---|---|---|---|
|BPM（非表示）|祖父内のBPM / input / ∅（暗黙spinbutton）/ ∅|すべて∅|number / 1 / 300 / ∅ / Auto / ∅|`.value=""`、isVisible=false。placeholderはAuto、1–300 BPMが属性上の範囲|
|別フォームのVariety（非表示）|Variety / div / slider / Variety|0 / 4 / 1 / Normal|すべて∅|Normal/1、isVisible=false|

- BPM: inputの親は`div.css-o6pnyv.eodlc7e2`、祖父`div.css-gwrmef.eodlc7e0`の別子にラベルBPM。aria-labelledby/tabindex=∅。採取用CSS候補は `input[type="number"][placeholder="Auto"][min="1"][max="300"]`。**設定・確定・復元・値変化は未検証**。非表示のため操作せず、表示条件も未確定。`.value`で空文字を読めたことだけ確認済み。
- 非表示Variety: 表示中のVarietyと同じラベル構造、共通上位にVocal Gender / Background music / Varietyを含む別コンテナ。どのモードに属するかは切替未実施で未確定。開始時から1/Normalで変更なし。**設定手順は未検証**。raw CSS全件採取時は対象の取り違えに注意。
- raw CSSのVarietyをDOM順で`isVisible()`確認すると`[true,false]`。`get_by_role('slider', name='Variety', exact=True)`は可視側1件のみ。フォーム内のその他の`input[type=number/range]`・`[role=spinbutton/slider]`は、Autoに復元した状態ではWeirdness/Style Influence/可視Variety/BPM/非表示Varietyの5要素だった。Durationのtext入力とsliderはCustom時だけ出現。
- フォーム外のnumber inputは拡張パネル内、rangeはplaybarだったため対象から除外。値は取得せず、操作なし。Vocal Gender、Max Mode、Personalizeは選択ボタンで、今回の数値設定対象外。

### 5.8 モデルメニュー（17:33 JST、選択変更なし）

`get_by_role('button', name='v6', exact=True).click()`で開き、`role=menu`/`role=menuitemradio`の表示テキストを採取。モデル項目は次の3件。`role=menuitemradio`、タグdiv、v6のみ`aria-checked=true`、他false。

|モデル|同じ項目内の補助表示|
|---|---|
|v6|Pro / Powerful. Versatile. Refined. Our best model yet.|
|v6-wild|Pro / Best for experimental ideas.|
|v6-mini|A free, more efficient version of premium v6 models.|

別項目として`Create Custom Model` / `Beta` / `Create a model based on your uploads (100 Credits)`（role=menuitem）が存在。開いた時点で読むことのできた一覧であり、選択やモデル作成は行っていない。`get_by_role('menu').press('Escape')`で閉じ、モデル表示v6・`aria-expanded=false`を確認。モデル別数値項目の出現条件は未検証。

### 5.9 項目一覧・検証状態

|項目|範囲と単位|開始時の値|設定手順|読み戻し|表示条件|検証状態|
|---|---|---|---|---|---|---|
|Duration（Custom入力）|ARIA上10–360秒、入力m:ss|Auto。Custom切替直後3:00/180秒|一意なMore Options→Custom→textbox Durationにfill('4:01')→Tab|input.value=4:01とslider aria-valuenow=241、valuetextも一致|Songs/Advanced/v6、More Options展開、Custom選択|設定・Tab確定・Auto復元済み。Enter、生秒入力、境界外、標準Playwrightのリセットdispatch_eventは未検証|
|Duration（連動slider）|10–360秒|Custom直後180|上のtextbox経由で241へ同期。sliderへの直接操作は未検証|role=slider/name=Durationのaria-valuenow / aria-valuetext|同上|同期読み戻し済み。直接矢印/ドラッグ/step未検証|
|Weirdness|0–100%、矢印実測1ポイント|50%|slider/name exact→ArrowRightで51→ArrowLeftで50|aria-valuenowと次の兄弟の表示値|Songs/Advanced/v6、More Options展開|1ステップ設定・即時反映・復元済み。任意値への一括設定は未検証|
|Style Influence|0–100%、矢印実測1ポイント|50%|slider/name exact→ArrowRightで51→ArrowLeftで50|aria-valuenowと次の兄弟の表示値|同上|1ステップ設定・即時反映・復元済み。任意値への一括設定は未検証|
|Variety（表示中）|0–4段階、矢印実測1|0/Off|slider/name exact→ArrowRightで1/Normal→ArrowLeftで0/Off|aria-valuenowとaria-valuetext、表示値|同上|1ステップ設定・復元済み。2–4の表示名と直接設定は未検証|
|BPM（非表示）|min/max属性上1–300 BPM|空文字、placeholder Auto|未検証。現在の非表示欄に入力しない|CSS候補count=1、DOM .value空文字を採取|現在Songs/Advanced/v6では非表示。出現条件未確定|属性と空値のみ採取。操作・確定・範囲挙動は未検証|
|Variety（別フォーム・非表示）|0–4段階|1/Normal|未検証。raw CSSで可視側と重複|aria-valuenow / aria-valuetextを採取|現在非表示。Background musicと共通上位、モード未確定|属性のみ、変更なし|

### 5.10 `_set_custom_duration`現行セレクタとの突合

|ファイル:行|現行処理 / セレクタ|今回の事実と判定|修正案（未実装）|
|---|---|---|---|
|`Python/suno_auto_create.py:3644`|role=button、name前方一致More Options/その他のオプション|**不一致（一意性）**。同条件18件、今回も再確認|`div[role=button][aria-expanded]`をMore Options接頭辞textで絞ると1件・展開成功。可能ならフォーム内にスコープしcount=1を要求。日本語値は未確認|
|`Python/suno_auto_create.py:3647`|Duration sliderとCustomの両countが0ならoptions.click|**分岐条件は観測状態と整合**。閉状態で両role locator=0。失敗原因はクリック先18件の一意性|一意な見出しのaria-expanded=false時だけclickし、trueを読み戻す。変更は未実装|
|`Python/suno_auto_create.py:3649`|`get_by_role('textbox', name='Duration', exact=True)`|**一致**。Custom時input type=text、aria-label=Duration、count=1|維持可能。Auto時0件、Custom時可視を検証する|
|`Python/suno_auto_create.py:3651`|`get_by_role('button', name='Custom', exact=True)`、1件要求後click|**一致**。Auto時1件、click後textbox/slider出現。Custom選択後はAuto/Customボタンが消える|現在の可視textbox有無で分岐を維持。復元用にAutoボタンを探す方法は成立しない|
|`Python/suno_auto_create.py:3656`|`f'{seconds // 60}:{seconds % 60:02d}'`→fill|**一致**。241秒に対応する4:01を受理。分の先頭0なし|今回の範囲では表示形式変更不要。全範囲・0分表記の正規化は未検証|
|`Python/suno_auto_create.py:3658`|field.press('Tab')|**一致、今回の試行では確定に必要**。fill直後slider180、Tab後241|blur前に成功と判定しない。Enter代替は未検証|
|`Python/suno_auto_create.py:3659`|`get_by_role('slider', name='Duration', exact=True)`|**一致**。Custom時1件、Auto時0件、aria-valuenowが秒数|維持可能。input側はm:ss、slider側は秒で二重照合|
|`Python/suno_auto_create.py:3660`|aria-valuenow取得|**一致**。Tab後241、aria-valuetext='4 minutes, 1 second'|数値比較を維持。valuetextは補助に限定|
|`Python/suno_auto_create.py:3661`|input_value()==displayかつfloat(aria-valuenow)==seconds|**DOM値の照合内容は一致**。今回input.value=4:01と241を確認。関数全体・Python input_value()自体は未実行|今回の実測から文字列比較の変更は不要。将来表記正規化が異なる場合のみ秒換算を検討|
|`Python/suno_auto_create.py:3640`|bool除外、正の整数秒のみ検証|**不足候補**。現行UIの下限10/上限360を入力前に検証していない|sliderのaria-valuemin/maxから事前検証を追加する案。境界外入力は未実施のためクランプ/エラー挙動は断定しない|
|`Python/suno_auto_create.py:3428`|共用helper `_ensure_more_options_open` は正規化完全一致|**不一致（前回所見継続）**。見出しにVariety等のサマリが付く|共用するならhelper側もフォームに限定した一意な見出し判定へ修正。既存helperへの置換だけでは解決しない|

この調査では関数・ソースの変更や生成を行っていない。DOMに対する個々の操作を検証したもので、`_set_custom_duration`全体の実行成功や生成結果の尺を保証するものではない。

### 5.11 最終復元・タブ閉鎖

- 17:34 JST、More Optionsを閉じる前の最終読取: Auto=true / Custom=false、Duration inputなし、Weirdness50、Style Influence50、Variety0/Off、Advanced=true、モデルv6・menu expanded=false。非表示項目は操作していない。
- 17:34:15 JST、More Optionsを開始時の閉状態へ復元し、aria-expanded=false、可視Custom=0、Duration textbox=0 / slider=0、可視Weirdness slider=0を確認。
- 17:34:19 JST、調査タブ1448698493を`close()`で閉鎖。既存の他タブは選択・操作していない。
- **元へ戻せなかった項目: なし。**


## 6. 日本語表示での数値項目（2026-10-05 追加採取）

以下はユーザーが2026-10-05に実機の日本語表示（`lang=ja`）で追加採取した事実。Task 13ではSUNOへの接続や実機での再確認は行っていない。

|項目|英語表示|日本語表示|
|---|---|---|
|尺の切替ボタン|`Auto` / `Custom`|`Auto` / `カスタム`。`Auto`は日本語表示でも`Auto`|
|尺の入力欄（`input type=text, inputmode=decimal`）の`aria-label`|`Duration`|`長さ`|
|尺のスライダー（`role=slider`）の`aria-label`|`Duration`|`長さ`。`aria-valuemin=10` / `aria-valuemax=360`、Custom切替直後の`aria-valuenow=180`、`aria-valuetext="3 分"`|
|Weirdnessスライダーの`aria-label`|`Weirdness`|`奇抜さ`（0–100）|
|Style Influenceスライダーの`aria-label`|`Style Influence`|`スタイルの影響`（0–100）|
|Varietyスライダーの`aria-label`|`Variety`|`バリエーション`（0–4）。`aria-valuetext`は`オフ` / `標準`など。不可視の同名スライダーがもう1件ある点は英語表示と同じ|

日本語表示で動作確認済みの項目（ユーザー提供の採取結果）:

- 「その他のオプション」の開閉。
- 曲名入力（`placeholder="曲名(任意)"`）。
- Styles入力。
- Createボタン（`aria-label="曲を作成"`）。
- ログイン判定。

上記の動作確認済み項目はTask 13の変更対象外。数値項目は表示言語を判定せず、各項目の英語名・日本語名のいずれかに完全一致するrole要素のうち、可視のものがちょうど1件であることを要求する。
