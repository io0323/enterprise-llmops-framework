#!/bin/sh
# 週次レポートを1本生成する(Phase 3 Step 3-7)。
#
# ELF は常駐しない。launchd が週に1度この1行を叩くだけで、プロセスは増やさない
# (docs/02 §9 の判断と一貫させる。NOTES.md N-047)。
#
# 環境変数で上書きできる:
#   ELF_HOME    ELF リポジトリのパス(既定: このスクリプトの1つ上)
#   LLMOPS_BIN  llmops 実行ファイル(既定: PATH 上の llmops)
#   ELF_CONFIG  config.yaml のパス(既定: $ELF_HOME/config.yaml)
set -eu

ELF_HOME="${ELF_HOME:-$(cd "$(dirname "$0")/.." && pwd)}"
LLMOPS_BIN="${LLMOPS_BIN:-llmops}"
ELF_CONFIG="${ELF_CONFIG:-$ELF_HOME/config.yaml}"
export ELF_CONFIG

exec "$LLMOPS_BIN" report weekly \
    --since 7d \
    --out "weekly_$(date +%Y%m%d).md"
