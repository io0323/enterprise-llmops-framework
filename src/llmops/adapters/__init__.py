"""Provider Adapter(SPI)。

CLAUDE.md「モジュール依存の絶対規約」により、本パッケージは gateway / prompt / registry /
db / sdk を import しない。参照してよいのは `adapters/base.py` の SPI のみ。
"""
