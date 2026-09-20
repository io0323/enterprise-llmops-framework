# Enterprise LLMOps Framework(ELF)

稼働中の5システム(PEP・APAP・AI Execution Harness・DDE・CGMP)を横断する **LLMOps Control Plane**。
Prompt・Model・評価・観測・コストを一元管理する。**6個目のアプリケーションではない。**

利用者は開発者1名のみ・非公開・有料サービス不使用。

---

## なぜ作るか

5システムが動き始めた結果、次の状態になっている(実コード調査の結果)。

| # | 現状 | 影響 |
|---|---|---|
| 1 | Promptが `cgmp/llm/prompts.py`(313行)・`dde/llm/prompts.py`(98行)にPython文字列としてハードコード | 変更にコード修正が必要。品質の履歴が残らない |
| 2 | `llm/client.py` がDDEとCGMPで同一実装のコピー | 片方の修正がもう片方に伝播しない |
| 3 | `claude -p --output-format json` が返す `total_cost_usd` / `usage` / `session_id` / `num_turns` を**全部捨てている**(`result` しか読んでいない) | コストもトークンも分からない |
| 4 | 実行ログに Prompt版・モデルが残らない(`llm_logs.task` のみ) | 「この出力はどの版で出たのか」が再現不能 |
| 5 | Prompt変更の評価ゲートが無い | 品質劣化が本番でしか分からない |
| 6 | 観測がCGMP・DDE・Harnessの別DBに分散 | 横断のコスト把握が不能 |

ELFはこの6点を、**既存システムの構造を壊さずに**解消する。

## 何を作らないか

最大のリスクは「6個目の車輪の再発明」。以下は既存実装が正であり、ELFは**触らない**。

| 機能 | 既存の正 |
|---|---|
| RAG(チャンク・embedding・検索) | CGMP `src/cgmp/rag/` |
| Agent実行・承認ゲート・パイプライン状態管理 | Harness `backend/app/harness/` |
| Routing / Cache / Circuit Breaker / Cost Engine | APAP `modules/apap-routing` ほか |
| Prompt資産管理(JVM側) | PEP `prompt-engine`(ELFは**状態機械と識別子体系を一致させるのみ**) |

## ドキュメント

| ファイル | 内容 |
|---|---|
| [`docs/01_要件定義.md`](docs/01_要件定義.md) | 現状分析(実コード調査)・課題・FR/NFR |
| [`docs/02_基本設計.md`](docs/02_基本設計.md) | 構成・ライフサイクルマッピング・モジュール・トレードオフ |
| [`docs/03_詳細設計.md`](docs/03_詳細設計.md) | DBスキーマ・Prompt形式・公開I/F・CLI・設定 |
| [`docs/04_運用ガイド.md`](docs/04_運用ガイド.md) | 日次/週次/月次のルーチン |
| [`docs/05_既存システム統合.md`](docs/05_既存システム統合.md) | 移行手順(ファイル・行番号レベル)・リスク |
| [`CLAUDE.md`](CLAUDE.md) | 実装ルール(Claude Code が参照) |
| [`NOTES.md`](NOTES.md) | 設計外判断の記録 |
| [`docs/impl/`](docs/impl/) | Claude Code 実装プロンプト(Phase 1-3) |

`prompts/` は Claude Code 用ではなく **Prompt Registry の実体**(`prompts/<system>/<name>.md`)。実装指示は `docs/impl/` にある。

元になった Vendor Agnostic 19章設計は `Enterprise_LLMOps_Framework設計書.md`(別途)。本リポジトリの各ドキュメントは該当章を `[§n]` で参照している。

---

## アーキテクチャ

```
[DDE]     \
[CGMP]     >-- llmops.sdk --> Gateway --> Adapter --> claude -p / SDK / mock
[Harness] /                      |
                                 +--> Prompt Registry (prompts/**/*.md)
                                 +--> Model Registry  (models.yaml)
                                 +--> Guard           (Quota / Budget)
                                 +--> Tracer          (data/llmops.sqlite3)
```

- **Gateway**: 全LLM呼び出しの単一通過点。Prompt解決・Quota判定・実行・再試行・Fallback・Trace記録
- **Prompt Registry**: Markdown + YAML front matter。状態機械は PEP と同一(Draft→Review→Approved→Published→Deprecated→Archived)
- **Model Registry**: 論理モデル名(`chat-standard` / `judge` 等)→ Adapter解決。実モデル名はコードにも設定にも書かない(APAP の Vendor Neutral 規約に準拠)
- **Tracer**: trace(処理1件)/ span(呼び出し1回)。span には `prompt_id@version` / `render_hash` / `logical_model` / usage / cost を刻印

---

## セットアップ

### 前提

```bash
python --version                          # 3.11+
claude --version                          # Claude Code が動くこと
claude -p "ping" --output-format json     # headlessモードが動くこと(必須)
sqlite3 --version
```

### インストール

```bash
conda create -n llmops python=3.11 -y
conda activate llmops
cd /path/to/enterprise-llmops-framework
pip install -e ".[dev]"
```

optional extra:
- `.[eval]` — 決定的評価のJSON Schema検証(Phase 2)
- `.[sdk-anthropic]` — Harness互換の `anthropic_sdk` Adapter

### 初期化

```bash
llmops init      # DB作成 + prompts/ と models.yaml の雛形生成
llmops sync      # prompts/**/*.md と models.yaml をDBへ取込(版を採番)
llmops model health
```

---

## 使い方

### 利用システム側(埋込)

```python
from llmops.sdk import LLMOps

ops = LLMOps.load(system="cgmp")

with ops.trace("article.generate", external_id=request_id):
    result = ops.complete(
        prompt_id="cgmp.section",
        variables={"title": title, "section_heading": heading, "context": ctx},
        task="section",
    )
    body = result.text          # cost_usd / input_tokens / render_hash も result から取れる
```

既存コードからの移行は **import 1行の差し替え**から始められる(`docs/05` §2.2)。

```python
from llmops.sdk.compat import LLMClient, LLMBudgetExceeded   # 既存と同一シグネチャ
```

### CLI

```bash
# Prompt
llmops prompt list --system cgmp
llmops prompt diff cgmp.section 3 4
llmops prompt publish cgmp.section          # 評価ゲートを通らないと失敗する(Phase 2以降)
llmops prompt rollback cgmp.section

# 観測
llmops trace show <request_id>              # 使用Prompt版・モデル・入出力全文・所要時間
llmops report cost --since 30d --by system  # 3システム横断のコスト内訳
llmops budget set --scope harness --monthly 10.0

# 評価(Phase 2)
llmops eval run cgmp-section --baseline
llmops eval add-case cgmp-section --from-span <span_id>   # 本番失敗を回帰ケース化
```

---

## 実装状況

リポジトリ: https://github.com/io0323/enterprise-llmops-framework

| Phase | 内容 | 状態 |
|---|---|---|
| Phase 0 | リポジトリ初期化 / 開発環境 / CI / 骨格 | 未着手 |
| Phase 1 | Gateway / Model Registry / Prompt Registry / Trace / Cost / 互換shim | 未着手 |
| Phase 2 | Evaluation / Regression / 決定的評価 / Judge / Groundedness | 未着手 |
| Phase 3 | Governance / 資産台帳 / canary / rollback / 監査 | 未着手 |
| Phase 4 | JVM側(PEP/APAP)統合 | 対象外(契約一致のみ) |

実装は `docs/impl/phase0_bootstrap.md` から順に Claude Code で実行する。
各Phaseのキックオフプロンプトは [`docs/impl/README.md`](docs/impl/README.md) にある。

---

## 移行順序

```
ELF Core(mock Adapterで自己完結)
  → DDE(注入点1箇所。手順の検証)
  → CGMP(注入点8ファイル。最大の効果)
  → Harness(trace注入 + SDK Adapter)
  → Phase 2 評価ゲート
```

小さい順に移行し、各ステップで既存テストが全て通ることを条件とする。詳細とリスクは `docs/05_既存システム統合.md`。
