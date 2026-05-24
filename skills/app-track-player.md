# app-track-player: 楽曲メディアプレイヤー + いいね + 削除

動画詳細「楽曲」タブ内で、フォルダ直下 / `music/` / `original_music/` の MP3 を
ブラウザ上で再生・評価・削除するための仕組み。

## 機能

| 機能 | 挙動 |
|------|------|
| 🎵 プレビュー再生 | HTML `<audio controls>` でストリーム再生（lazy load） |
| 単一再生 | 次の曲を再生すると他の audio が自動で `pause()` |
| ♥ いいね | ± ボタンでカウント更新 → **ファイル名先頭に `z` を付与**（状態はファイル名に直接反映） |
| 🗑 削除 | 確認モーダル → ローカルを物理削除 |
| 場所バッジ | ROOT（直下） / MUSIC（処理済み） / ORIG（バックアップ） を色分け |

## いいね仕様（ファイル名ステート）

| いいね数 | ファイル名例 |
|---------|-------------|
| 0 | `song.mp3` |
| 1 | `z_song.mp3` |
| 2 | `zz_song.mp3` |
| 3 | `zzz_song.mp3` |

z プレフィックスの数 = いいね数。
[app-rename-audio](./app-rename-audio.md) や JSX 側（z_ 優先再生）と契約が揃っている。

## API

```
GET  /api/videos/{name}/tracks
  → [{ filename, base_name, likes, size, location, rel_path }]
  走査対象: 動画フォルダ直下 + music/ + original_music/

GET  /api/videos/{name}/track-file/{rel_path}
  → audio/mpeg 配信（パストラバーサル対策あり）

POST /api/videos/{name}/track-like
  body: { rel_path, delta }   // or { rel_path, set_to: N }
  → ファイルを `z^new_count _ base_name` にリネーム、新 rel_path を返却

DELETE /api/videos/{name}/track?rel_path=...
  → ファイル物理削除
```

## UI 実装上の注意

### 単一再生ロジック

```js
<audio onplay="_onTrackPlay(this)">
function _onTrackPlay(current){
  document.querySelectorAll('.tr-audio').forEach(a=>{
    if(a!==current && !a.paused) a.pause();
  });
}
```

### いいねリネーム後のリロード

リネームすると `rel_path` が変わるため、いいね操作後に
`loadTrackList(name)` で一覧を再取得して onclick ハンドラを新 path に更新する。

### 削除の安全性

- バックエンドは `rel_path` を resolve した上で `folder.resolve()` を親に持つか検証（パストラバーサル防止）
- 拡張子が `.mp3/.wav/.m4a/.flac/.aac` のみ許可
- フロントで確認ダイアログ「※ ローカルから物理削除されます。元に戻せません。」

## 関連スキル
- [app-suno-download.md](./app-suno-download.md) — ここで集まった MP3 の取得元
- [app-rename-audio.md](./app-rename-audio.md) — いいね数を維持しながらリネーム
