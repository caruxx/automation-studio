#!/usr/bin/env python3
"""ベンチマーク分析・提案層の公開窓口（D6: ベンチ二系統正規化）。

下流（app.py / app_pipeline 等）は、競合分析を起点とする分析・提案関数を
**このモジュール経由で参照する**ことで参照先を一本化する:
  - analyze_with_claude        競合データ → バズ/トレンド/方向性の総合分析
  - propose_with_analysis      分析 → タイトル/説明/タグのメタ提案
  - propose_suno_prompt        分析 → SUNO プロンプト提案
  - analyze_thumbnail_elements サムネ画像 → 視覚要素抽出(Vision)

D6 の目的は「app_competitor(取得層) と分析層の二重参照を解消し、分析の窓口を
単一モジュールに正規化する」こと。実体は現状 app_competitor.py に置くが、
competitor を YouTube API 取得層へ純化する物理移動を段階的に進める間も、
この窓口名（app_benchmark_analyze）は不変に保つため、下流は移動の影響を受けない。

循環 import なし: competitor は本モジュールを import しない（自身の関数を直接使う）。
"""
from app_competitor import (  # noqa: F401  分析窓口としての再エクスポート（D6）
    analyze_with_claude,
    propose_with_analysis,
    propose_suno_prompt,
    analyze_thumbnail_elements,
)

__all__ = [
    "analyze_with_claude",
    "propose_with_analysis",
    "propose_suno_prompt",
    "analyze_thumbnail_elements",
]
