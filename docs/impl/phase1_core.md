# Phase 1 実装プロンプト — ELF Core(Gateway / Registry / Trace / Cost / 互換shim)

Claude Code にこのファイルの内容をそのまま渡す。Step は順に実行し、各Stepの完了条件を満たしてから次へ進む。

---

## 事前に必ず読むもの

```
docs/01_要件定義.md
docs/02_基本設計.md
docs/03_詳細設計.md
docs/05_既存システム統合.md
CLAUDE.md
```

特に **CLAUDE.md の絶対ルール15項目**は実装中の全判断に優先する。

---

## Step 1-1: プロジェクト骨格とDB

`src/llmops/` に `02_基本設計.md` §4 のモジュール構成でパッケージを作る。この Step では中身は空でよいが、
**依存の向き(`sdk → gateway → prompt/registry/guard/observability → db`、`adapters` は SPI のみ)**を最初から守る。

実装対象:

1. `db/schema.sql` — `03_詳細設計.md` §2 の DDL をそのまま。Phase 1 で使うのは
   `traces` / `spans` / `prompt_versions` / `prompt_deployments` / `prompt_transitions` /
   `model_versions` / `budgets` / `cost_daily` / `audit_logs`。
   Phase 2-3 のテーブル(`eval_*` / `asset_catalog`)も**この時点で作っておく**(後からの ALTER を避ける)。
2. `db/connection.py` — SQLite接続。`PRAGMA journal_mode=WAL` / `PRAGMA foreign_keys=ON`。
   接続はコンテキストマネージャで返す。
3. `db/repository.py` — 全DB操作の集約。この Step では `init_schema()` と
   `insert_trace` / `update_trace` / `insert_span` / `update_span` / `upsert_cost_daily` のみ。
4. `config.py` — `config.yaml` を pydantic でロード。環境変数 `ELF_CONFIG` があればそのパスを優先。
   `ELF_STRICT_TRACE=1` で `trace.strict` を上書き。
5. `logging_utils.py` — DDE/CGMP の `logging_utils.py` と同じ方針。`trace_id` / `span_id` を
   `extra` で出せる構造化ログ。
6. `cli.py` — Typer アプリの骨格と `llmops init`(DB作成 + `prompts/` `models.yaml` 雛形生成)。

**完了条件**: `llmops init` が成功し、`sqlite3 data/llmops.sqlite3 ".tables"` に全テーブルが出る。

---

## Step 1-2: Adapter SPI と3実装

`03_詳細設計.md` §5.3 / §5.4 に従う。

1. `adapters/base.py` — `AdapterRequest` / `AdapterResponse` / `ProviderAdapter`(ABC)。
   **`gateway` / `registry` / `db` を import しないこと**(絶対ルール8)。
2. `adapters/mock.py` — `mode: echo` で入力をそのまま返す。`mode: json` で固定JSONを返す。
   トークン数は文字数から概算し、`cost_usd=0.0`。テストが決定的になるよう乱数を使わない。
3. `adapters/claude_cli.py` — `subprocess.run(command, input=text, capture_output=True, text=True, timeout=...)`。
   - `../content-generation-platform/src/cgmp/llm/client.py` の `strip_fence` / `extract_json` を
     **そのまま移植**する(絶対ルール10。改善しない)。移植先は `llmops/adapters/_parsing.py`。
   - 応答から拾うフィールドは `03_詳細設計.md` §5.4 のコード片のとおり。
     **必須は `result` のみ。他は `.get()` で欠損許容し、欠損時は WARN ログを出して継続**(絶対ルール9)。
   - `payload.get("is_error") is True` または returncode != 0 は失敗として扱う。
4. `adapters/anthropic_sdk.py` — `anthropic` が import できない場合、
   モジュールロード時ではなく**解決時に** `AdapterUnavailable` を送出する
   (未インストール環境で ELF 全体が壊れないこと)。実モデル名は `params.model_env` の
   環境変数から取得する(絶対ルール13)。
5. Adapterのレジストリ — 名前 → クラスの単純な dict。エントリポイント機構は使わない(依存を増やさない)。

**完了条件**: `pytest tests/test_adapters.py` が通る。`claude_cli` のテストは
`subprocess.run` をモックし、実APIを叩かない(絶対ルール14)。欠損フィールドのある応答JSONでも
例外にならないテストを含めること。

---

## Step 1-3: Prompt Registry

`03_詳細設計.md` §3 / §2.2 に従う。

1. `prompt/loader.py` — `prompts/**/*.md` を走査。YAML front matter(`---` 区切り)と本文を分離。
   prompt_id はパスから導出(`prompts/cgmp/section.md` → `cgmp.section`)。
   front matter の `id` がパス由来と食い違う場合はエラー。
2. `prompt/render.py` — **Jinja2を使わない**(絶対ルール、N-003)。
   - 構文は `{{ var }}` / `{{ var | filter }}` / `{{ include.<name> }}` の3つのみ。制御構文なし。
   - fragment(`03_詳細設計.md` §3.1): `prompts/_fragments/*.md` を front matter の
     `includes:` で宣言し、`{{ include.<name> }}` で展開。**ネスト禁止・変数渡し禁止**。
     `content_hash` は**展開後テキスト**で計算する(fragment変更が参照元の新versionを生む)。
   - フィルタは `join(sep)` / `default(v)` / `upper` / `trim` の4つだけ。
   - 未定義変数は `MissingVariable` を送出(空文字で黙って通さない)。
   - `variables`(JSON Schema)で型検証。`jsonschema` が無い環境では
     required と基本型のみの簡易検証にフォールバックする。
   - `render_hash` = レンダリング後テキストの SHA-256 先頭16桁。
     **同一入力で必ず同一値**になること(PEP の renderHash と同定義)。
3. `prompt/registry.py` — `sync()`(ファイル → DB、`content_hash` 差分で新version採番)、
   `resolve(prompt_id, version=None)`(未指定なら `prompt_deployments` の active、
   canary設定があれば `canary_percent` の確率で canary_version)、
   状態遷移(`submit` / `approve` / `publish` / `deprecate` / `archive` / `rollback`)。
   - 状態機械は `02_基本設計.md` §5.2。許可されない遷移は `InvalidTransition`。
   - 全遷移を `prompt_transitions` と `audit_logs` に記録する。
   - Phase 1 では `publish` の評価ゲートは**まだ無い**(Phase 2で追加)。
     ただし `--force` フラグと評価チェックの差し込み口だけ用意しておく。
4. `prompt/catalog.py` — 一覧・タグ検索・最終利用日(spans から導出)。
5. CLI: `llmops sync` / `prompt list` / `prompt show` / `prompt diff` / `prompt render` /
   `prompt submit|approve|publish|deprecate|rollback`。

**完了条件**:
- `llmops sync` で雛形Promptが登録される
- `llmops prompt render <id> --vars f.json` が2回連続で同じ `render_hash` を出す
- 状態機械の不正遷移テストが通る

---

## Step 1-4: Model Registry と Gateway

1. `registry/model_registry.py` — `models.yaml` をロードし、エントリごとに `config_hash` を計算。
   既存最新版とハッシュが違えば**新version採番**(`03_詳細設計.md` §2.3)。
   `resolve(logical_name)` は status が `active` の最新版のみ返す(`blocked` は `ModelNotFound`)。
2. `guard/quota.py` — 呼び出し**前**判定(絶対ルール6):
   - trace単位の呼び出し回数上限(`guard.calls_per_trace_limit`)→ `LLMBudgetExceeded`
   - 予算(`budgets` と `cost_daily` の当月合計)→ `QuotaExceeded`
   - `hard_quota: false` のときは WARN ログのみで通す
   - 判定結果が拒否の場合、`audit_logs` に `quota.exceeded` を記録
3. `observability/tracer.py` —
   - `start_trace` / `end_trace` / `start_span` / `end_span`
   - **span は呼び出し前に INSERT**(絶対ルール5)。`end_span` で UPDATE。
   - 全ての書き込みを try/except で包み、失敗は WARN ログのみ(絶対ルール4)。
     `config.trace.strict` が true のときだけ送出。
   - `max_text_chars` 超過時は末尾を切り、`meta` に `truncated: true`。
4. `observability/cost.py` — span確定時に `cost_daily` を UPSERT。
   `cost_usd` が応答に含まれていればそれを正とし、無ければ `price` × トークン数で概算(概算フラグを立てる)。
5. `gateway/gateway.py` — `03_詳細設計.md` §6 の擬似コードどおり:
   Prompt解決 → 変数検証・レンダリング → モデル解決 → Guard → 実行(retry)→ Fallback(**1段のみ**、絶対ルール7)。
6. `gateway/errors.py` — `03_詳細設計.md` §10 の例外階層。
   **`LLMError` と `LLMBudgetExceeded` は名前を変えない**(既存コードが捕捉している)。
7. CLI: `llmops model list|show|health` / `llmops run <prompt_id> --vars f.json`。

**完了条件**:
- `llmops run <id> --vars f.json --model mock-echo` が成功し、`traces` と `spans` に行が入る
- span に `prompt_id` / `prompt_version` / `render_hash` / `logical_model` / `adapter` が全て入っている
- Fallback のテスト: 失敗する mock → `chat-fallback` に落ち、`degraded=1` が記録される
- Quota のテスト: 上限到達で呼び出し前に例外。**Adapterが呼ばれていない**ことを assert する

---

## Step 1-5: SDK と互換shim

`03_詳細設計.md` §5.1 / §5.2 に従う。

1. `sdk/client.py` — `LLMOps.load(system=...)` / `trace()`(コンテキストマネージャ、
   `with` を抜けると `finished_at` と status が確定。例外時は `failed`)/ `complete()` / `complete_raw()`。
   - `complete_raw()` は `gateway.allow_raw_completion: false` のとき `PolicyViolation` を送出し、
     `audit_logs` に記録する。
2. `sdk/compat.py` — **既存 `cgmp/llm/client.py` の `LLMClient` と同一シグネチャ**。
   - `call_json` / `call_text` / `ensure_budget` / `calls_used` / `remaining_calls`
   - `LLMError` / `LLMBudgetExceeded` を再輸出
   - prompt文字列を直接受け取るため、span は `prompt_id=NULL` の adhoc として記録する
     (移行 Step 1 用。Step 2 で prompt_id 付きに置き換わる)
   - `repo` 引数は受け取るが使わない(既存呼び出し側の互換のため)。使わないことをdocstringに明記

**完了条件**: `tests/test_compat.py` で、既存CGMPの `LLMClient` を使うコード片が
`llmops.sdk.compat.LLMClient` に差し替えても同じ挙動になることを検証できる。

---

## Step 1-6: レポートと規約テスト

1. `observability/report.py` — Markdown生成。
   - `llmops report cost --since 30d --by system|model|prompt`
   - 出力に含める: 呼び出し回数 / 入出力トークン / cost合計 / 成功率 / degraded件数 / P50・P95レイテンシ
2. `llmops trace list` / `llmops trace show <trace_id|external_id>` —
   span を時系列で表示し、各spanの `prompt_id@version` / model / duration / cost / success を出す。
   `--full` で入出力全文。
3. `llmops budget show|set`
4. `tests/test_no_hardcoded_prompts.py` — `src/llmops/` 配下に、
   **200文字を超える文字列リテラル**が存在したら失敗する(ELF自身がPromptをコードに持たないことの保証)。
5. `tests/test_layering.py` — import解析で依存の向きを検証:
   `adapters/*` が `gateway` / `prompt` / `registry` / `db` を import していないこと。

**完了条件**: `pytest -q` が全て通る。カバレッジが `gateway` / `prompt` / `registry` で80%以上。

---

## Step 1-7: DDE の移行(手順の検証)

`docs/05_既存システム統合.md` §3。DDEは注入点が1箇所のみで、手順検証に最適。

1. `demand-discovery-engine/src/dde/pipeline.py:150` の `LLMClient` を
   `llmops.sdk.compat.LLMClient` に差し替え、`system="dde"` を渡す
2. DDEの `config.yaml` に `llmops:` セクションを追記
3. **DDEの既存テストが全て通ることを確認**
   **タイムアウトに注意**: DDEの既存設定は `timeout_sec: 300`(CGMPは600)。
   挙動を変えないため、DDEのPromptは `models.yaml` の `dde-batch`(300秒)を使う。
   front matter の `model: dde-batch` で宣言すること。

4. `dde/llm/prompts.py`(98行)の各関数について:
   - 代表入力での出力を `tests/fixtures/golden/dde_*.txt` として保存
   - `prompts/dde/*.md` へ移行
   - `llmops prompt render` の結果が**バイト一致**することをテスト(絶対ルール、移行の鉄則)
   - 一致確認後に呼び出し側を `ops.complete(prompt_id=...)` へ切替
5. `dde/llm/client.py` と `dde/llm/prompts.py` を削除。`dde/llm/batch.py` は**残す**(ELFの責務ではない)

**完了条件**: `cd demand-discovery-engine && pytest -q` が通り、
`grep -rn 'subprocess.run(\["claude"' src/` が0件。`llmops report cost --by system` に `dde` が出る。

---

## Step 1-8: CGMP の移行

`docs/05_既存システム統合.md` §2。8ファイルが依存しているが、生成は2箇所に集中している。

1. **Step 1**: `pipeline.py:18` と `publish/service.py:37` の import を差し替え、`system="cgmp"` を渡す
   → この時点で CGMP の既存テストが全て通ること
2. **Step 2**: Prompt文字列を `prompts/cgmp/*.md` へ。**`llm/prompts.py` だけでなく
   `quality/rubric.py:71` と `formatter/sns.py:84` にも Prompt がある**(`docs/05` §2.2 の表)。
   - 共有ルール7件は `prompts/_fragments/*.md` へ切り出す
   - `term_rule()` / `tone_rule()` は fragment にせず、呼び出し側で組み立てて変数で渡す
   - `rubric.py` / `sns.py` の後処理ロジック(スコア変換・文字数計算・ハッシュタグ整形)は
     **CGMP側に残す**。移すのはPrompt文字列だけ
   - DDEと同じゴールデンファイル手順を踏む
3. **Step 3**: `cgmp/llm/client.py` と `cgmp/llm/prompts.py` を削除。
   `config.yaml` の `llm:` セクション削除。`llm_logs` は**残すが INSERT を停止**。
   CGMP の `CLAUDE.md` の「LLM呼び出しパターン」節を ELF 参照に書き換える

**Phase 1 完了条件**:
- `cd content-generation-platform && pytest -q` が通る
- 両システムで `grep -rn 'subprocess.run(\["claude"' src/` が0件
- `llmops report cost --since 7d --by system` に `dde` と `cgmp` が両方出る
- `llmops trace show <cgmp_request_id>` で、1記事の生成が1 trace・N span として辿れ、
  各spanに Prompt版とモデルが刻印されている

---

## この Phase でやらないこと

- 評価(Phase 2)。`publish` の評価ゲートは差し込み口だけ作って中身は空
- canary / 資産台帳 / 監査レポート(Phase 3)
- Harness の移行(Phase 1 完了後)
- JVM側(PEP/APAP)への変更(対象外)
- 既存Promptの文言改善(絶対ルール10。評価スイートができてから)


---

## Phase 1 実施記録(2026-09-20 / 21)

各 Step の完了条件を満たした状態。実装中に見つかった設計との差分は ELF の
`NOTES.md`(N-012 〜 N-030)に記録してある。

| Step | 状態 | 備考 |
|---|---|---|
| 1-1 DB / config / logging / init | ✅ | 14テーブル。`spans.meta_json` のみ設計に無い追加列(N-013) |
| 1-2 Adapter SPI と3実装 | ✅ | 実測応答から欠損版・is_error版・不正JSON版の fixture を派生 |
| 1-3 Prompt Registry | ✅ | 自前レンダラ。状態機械は PEP 準拠。評価ゲートは差し込み口のみ |
| 1-4 Model Registry / Gateway | ✅ | Fallback 1段・Guard は呼び出し前・span は呼び出し前 INSERT |
| 1-5 SDK と互換shim | ✅ | CGMP 版 + DDE 版の両シグネチャ(N-020) |
| 1-6 レポートと規約テスト | ✅ | カバレッジ gateway/prompt/registry で 94% |
| 1-7 DDE 移行 | ✅ | 498 tests green。Prompt 3件を外部化 |
| 1-8 CGMP 移行 | ✅ | 808 tests green。Prompt 5件 + 共有ルール断片6件を外部化 |

### 移行で実際に効いたもの

**ゴールデンファイルによるバイト一致検証**が、移行基盤自体のバグを2件検出した。

1. `loader.split_front_matter` が本文末尾の改行を落としていた(N-024)
2. fragment 展開でファイル末尾の改行が余分に入っていた(N-030)

どちらも「全 Prompt が1バイトだけ変わる」類の差分で、レビューでは気付けない。
移行の鉄則(移行前にゴールデンを取る → バイト一致を確認してから切り替える)は、
文言を守るためだけでなく、基盤のバグ検出としても機能した。

**既存テストをそのまま通すこと**が、互換shim の挙動差分を3件検出した(N-027)。
シグネチャを合わせるだけでは不十分で、副作用(呼び出し前の残枠確保・
どの値で上限判定するか・trace の同一性)まで揃える必要があった。

**`chat-fallback` が mock だったこと**による偽の評価スコアを、CGMP の既存テストが
検出した(N-026)。本番モデルの `fallback_to` は外した。

### この Phase で作られなかったもの(意図的)

- 評価スイートの中身(Phase 2)。`publish` の評価ゲートは差し込み口のみ
- canary / 資産台帳 / 監査レポート(Phase 3)
- Harness の移行(Phase 1 完了後)
