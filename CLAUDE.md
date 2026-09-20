# CLAUDE.md — Enterprise LLMOps Framework(ELF)

## プロジェクト概要

既に稼働中の5システム(PEP・APAP・AI Execution Harness・DDE・CGMP)を横断する **Control Plane**。
Prompt・Model・評価・観測・コストを一元管理する。**6個目のアプリケーションではない。**

利用者は開発者1名のみ。非公開。有料サービス不使用。

設計ドキュメント(実装前に必ず全て読むこと):
- `docs/01_要件定義.md`
- `docs/02_基本設計.md`
- `docs/03_詳細設計.md`
- `docs/05_既存システム統合.md` ← **既存コードを触る前に必読**

## 技術スタック(変更禁止)

- Python 3.11+ / Typer / SQLite(WAL・外部キー有効)
- 必須依存は `typer` / `pyyaml` / `pydantic>=2` のみ
- LLM: `claude -p --output-format json`(subprocess)。Anthropic SDKは optional extra
- テンプレートエンジンは**使わない**(Jinja2禁止)。自前の最小レンダラ

## 絶対ルール

1. **既存5システムのコードとDBスキーマを、移行手順(`docs/05`)の記載外で変更しない**。5システムは長期テスト中であり、回帰が最大のリスク
2. **有料APIの導入禁止**。提案もしないこと。`anthropic_sdk` Adapterは既存Harness互換のためだけに存在し、未インストール環境では解決不能エラーにする
3. **既存機能を再実装しない**。RAG本体はCGMP、Agent実行・承認ゲート・パイプライン状態管理はHarness、Routing/Cache/CBはAPAPが正。ELFは観測・登録・評価・制御のみ
4. **Trace記録の失敗でアプリ処理を止めない**。`tracer` の例外は捕捉してWARNログのみ。例外送出は `ELF_STRICT_TRACE=1` のときだけ
5. **span は LLM 呼び出しの「前」に INSERT する**。プロセスが落ちても「呼び出そうとした」記録を残す
6. **Guard(Quota/予算)は呼び出しの「前」に判定する**。事後判定では止められない
7. **Fallback は1段のみ**。`no_fallback=True` を付けて再帰し、連鎖を構造的に防ぐ
8. **Adapter は Gateway / Registry / DB を import しない**。SPI(`adapters/base.py`)のみ参照する
9. **`claude -p` の応答で必須は `result` のみ**。他フィールドは `.get()` で欠損許容し、欠損時はWARNログを出して処理継続。CLIのバージョン差で落とさない
10. **既存実装からの移植コードを「ついでに改善」しない**。`strip_fence` / `extract_json` は `cgmp/llm/client.py` からそのまま移植する。改善は評価スイート(Phase 2)を用意してから
11. **Prompt本文をPythonコードに書かない**。`prompts/**/*.md` のみ。これを破ると本プロジェクトの存在理由が消える
12. **閾値・重み・上限・モデル名をハードコードしない**。`config.yaml` / `models.yaml` / front matter から読む
13. **実モデル名・Provider製品名をコード・設定・コメントに書かない**(APAP規約に合わせる)。環境変数経由で解決する
14. **実APIを叩くテストを書かない**。`mock` Adapter と `subprocess.run` のモックで完結させる
15. **確認質問はしない**。設計に無い事項は最小実装を選び `NOTES.md` に判断理由を記録する

## コーディング規約

- 型ヒント必須 / DTOは `dataclass`(設定ロードのみ pydantic)
- DB操作は `db/repository.py` に集約(リポジトリパターン。DDE/CGMPと同じ)
- ログは標準logging。`trace_id` / `span_id` 付き構造化
- 例外は `03_詳細設計.md` §10 の階層に従う。`LLMError` と `LLMBudgetExceeded` は**名前を変えない**(既存コードが捕捉している)
- テスト: pytest。`gateway` / `prompt` / `registry` は行カバレッジ80%以上

## モジュール依存の絶対規約

```
sdk → gateway → (prompt / registry / guard / observability) → db
                         ↓
                    adapters(SPIのみ。上位を import しない)
```

逆向きの import を作らない。`adapters/*` が `gateway` を import していたら設計違反。

## フェーズ管理

- **Phase 1**: Gateway / Model Registry / Prompt Registry / Trace / Cost / 互換shim
  - 完了条件: DDEとCGMPが ELF 経由で動き、既存テストが全て通る。`spans` に cost/token が入る。両システムから `llm/client.py` と `llm/prompts.py` が消える
- **Phase 2**: Evaluation / Regression / 決定的評価 / Judge / Groundedness
  - 完了条件: `llmops prompt publish` が評価ゲートで失敗しうる状態になる
- **Phase 3**: Governance / 資産台帳 / canary / rollback / 監査 / レポート拡充
  - 完了条件: 全AI資産に所有者が付き、`llmops prompt rollback` が1コマンドで済む
- **Phase 4**(将来): JVM側統合。本リポジトリでは契約一致まで。実装は対象外

各フェーズの完了条件を満たすまで次に進まない。

## 移行作業の鉄則

既存システムのPromptを `prompts/**/*.md` へ移す際:

1. 移行前に現行関数の出力をゴールデンファイルとして保存する
2. 移行後 `llmops prompt render` の結果が**バイト一致**することをテストする
3. 一致を確認してから呼び出し側を切り替える
4. **文言を同時に改善しない**

DDE → CGMP → Harness の順で移行する(小さい順に手順を検証するため)。

## 動作確認コマンド

```bash
pytest -q
llmops init
llmops sync
llmops model health
llmops run cgmp.section --vars tests/fixtures/section_vars.json --model mock-echo
llmops trace list --since 1d
sqlite3 data/llmops.sqlite3 "SELECT task,prompt_id,prompt_version,cost_usd,input_tokens FROM spans ORDER BY created_at DESC LIMIT 10;"
```

## 参照する既存コードの場所

| 対象 | パス |
|---|---|
| CGMP LLMClient(移植元) | `../content-generation-platform/src/cgmp/llm/client.py` |
| CGMP Prompt(移行元・313行) | `../content-generation-platform/src/cgmp/llm/prompts.py` |
| DDE LLMClient / Prompt | `../demand-discovery-engine/src/dde/llm/` |
| Harness(Prompt既にファイル化済み) | `~/projects/GitHub/Python/shopping-sns-auto-operation/backend/prompts/` |
| PEP(Prompt状態機械の参照) | `~/projects/GitHub/engine/prompt-engine/` |
| APAP(論理モデル名・Vendor Neutral規約の参照) | `~/projects/GitHub/engine/apap-engine/` |
