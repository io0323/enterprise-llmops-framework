# Phase 2 実装プロンプト — Evaluation / Regression Gate

前提: Phase 1 の完了条件を全て満たしていること(DDEとCGMPがELF経由で動き、Promptが外部化済み)。

このPhaseの目的は一つ。**Prompt変更を、評価を通さずに本番へ出せなくすること**(19章 §8 Evaluation Driven)。

---

## 事前に読むもの

```
docs/01_要件定義.md §4.4
docs/03_詳細設計.md §2.5(eval_* テーブル)
CLAUDE.md
```

---

## Step 2-1: 評価スイート定義と決定的評価

`evals/<suite_id>.yaml` を定義形式とする。

```yaml
id: cgmp-section
prompt_id: cgmp.section
model: chat-standard
judge_model: judge

# 決定的評価(純コード。LLMを呼ばない)。これが落ちたらJudgeへ進まない
deterministic:
  - type: non_empty
  - type: max_chars
    value: 1400                  # front matter の max_chars + 余裕
  - type: forbidden_patterns
    patterns: ["```", "http://", "https://"]
  - type: no_outside_context     # constraints.forbid_outside_context の検証
    context_var: context
    # context に無い「数値・年号・カタカナ固有名詞」を抽出して報告する
  - type: json_schema            # as_json のPromptのみ
    schema_file: schemas/outline.json

# LLM-as-Judge(決定的評価を通った場合のみ)
judge:
  - metric: groundedness
    prompt_id: judge.groundedness
    weight: 0.4
  - metric: relevance
    prompt_id: judge.relevance
    weight: 0.3
  - metric: readability
    prompt_id: judge.readability
    weight: 0.3

thresholds:
  deterministic_pass_rate: 1.0   # 決定的評価は全件合格が必須
  min_score: 0.75                # 加重平均の下限
  regression_tolerance: 0.02     # ベースライン比の許容低下
```

実装:

1. `eval/deterministic.py` — 上記 `type` を実装。**LLMを一切呼ばない**。
   - `no_outside_context` が本プロジェクトの肝。CGMP絶対ルール9 / 19章 §6 Grounding の機械検証。
     実装方針: 生成文から数値・年号・カタカナ語・英数字トークンを抽出し、
     `context` 変数の文字列に出現しないものを違反候補として列挙する。
     完全な判定は不可能なので**再現率優先(見逃しを減らす)**とし、誤検出は人が潰す前提にする。
     誤検出率が高すぎて使われなくなるのが最悪なので、`allowlist` を YAML で持てるようにする。
2. `eval/runner.py` — スイート実行。
   - `config.eval.deterministic_first: true` のとき、決定的評価が落ちたケースは
     **Judgeを呼ばずに fail 確定**(コスト節約。CGMP絶対ルール6と同方針)
   - 各ケースの生成呼び出しも Judge 呼び出しも**通常の Gateway 経由**にする。
     評価専用の抜け道を作らない(評価自体のコストも `spans` に載る)
   - 結果を `eval_runs` / `eval_results` に記録
3. CLI: `llmops eval list` / `llmops eval run <suite_id> [--version N]` / `llmops eval report <run_id>`

**完了条件**: `llmops eval run cgmp-section --model mock-echo` が動き、`eval_runs` に verdict が入る。
決定的評価が落ちたケースで Judge の span が発生していないことを assert するテストがある。

---

## Step 2-2: LLM-as-Judge

1. Judge用Promptを `prompts/judge/*.md` として**Registryに登録する**(絶対ルール11)。
   Judge も版管理・状態管理の対象。`judge.groundedness` / `judge.relevance` / `judge.readability`。
2. Judge の出力は必ず構造化(`{"score": 0.0-1.0, "reason": "...", "violations": [...]}`)。
   スコアだけでなく `reason` を必ず保存する(`eval_results.detail`)。理由が無いスコアは改善に使えない。
3. Judge は論理モデル `judge` を使う(`models.yaml`)。本番生成モデルと分離しておく
   (自己評価バイアスの回避 / 将来の差し替え余地。`docs/02` §9)。
4. `eval/judge.py` — パース失敗時はそのケースを `error` とし、runを止めない。
   スコア欠損は平均から除外し、除外件数をレポートに出す。

**注意**: Judge のスコアは絶対値として信用しない。**版間の相対比較**にのみ使う。
これをドキュメントとレポート出力の両方に明記すること(過信の防止)。

---

## Step 2-3: Groundedness(RAG向け)

CGMPのRAG出力に対する検証。19章 §6。

1. `eval/groundedness.py` — 生成文を文単位に分割し、各文について
   `context` 内のどのチャンクに帰属するかを Judge に問う。
   出力: 帰属率(context由来の文 / 全文)と、未帰属文のリスト。
2. CGMPの `rag/retriever.py` が返すチャンクを評価入力に含められるよう、
   評価ケースの `vars_json` に `context` と `context_sources` を持たせる。
3. **RAG本体は再実装しない**(絶対ルール3)。評価だけを行う。

---

## Step 2-4: Regression Gate(このPhaseの成果物)

1. `eval/regression.py` —
   - ベースライン = 現在 published の版に対する最新の eval_run
   - 新版のスコアがベースライン比で `regression_tolerance` を超えて低下 → `verdict='regressed'`
   - 決定的評価の合格率が `deterministic_pass_rate` 未満 → `verdict='fail'`
   - ベースラインが存在しない(初版)場合は `min_score` のみで判定
2. `prompt/registry.py` の `publish()` に評価ゲートを組み込む(Phase 1 で作った差し込み口)。
   publish が成功する条件:
   1. 状態が `approved`
   2. 対象版に対する eval_run が存在する
   3. その eval_run の `prompt_version` が publish 対象版と一致する(古い評価を使い回させない)
   4. `verdict == 'pass'`
   条件を満たさない場合 `EvaluationFailed` を送出し、**CLIは非ゼロ終了**する。
3. `--force` は残すが、使用時は必ず `audit_logs` に `prompt.publish.forced` を記録し、
   理由(`--reason`)を必須にする。
4. `prompt_transitions.eval_run_id` に根拠となった run を記録する。

**完了条件**:
- 意図的にスコアを下げたPrompt版で `llmops prompt publish` が非ゼロ終了する
- 古い eval_run しか無い状態で publish が拒否される
- `--force --reason "..."` で通り、監査ログに残る

---

## Step 2-5: 閉ループ(最重要)

評価スイートが陳腐化すると、このPhase全体が飾りになる。**本番の失敗を評価ケースに変換する導線**を必ず作る。

1. `llmops eval add-case <suite_id> --from-span <span_id>` —
   span の `request_text` と変数(復元できる範囲)からケースを起こし、
   `eval_cases.origin='production-failure'` / `source_span_id` を記録する。
2. `llmops trace list --status failed --since 7d` で失敗spanを拾えるようにする。
3. `llmops eval report` に「ケースの出自内訳(manual / production-failure)」を出す。
   production-failure 由来が増えていないスイートは陳腐化のサイン。
4. `docs/04_運用ガイド.md` の週次ルーチンにこの操作を組み込む。

---

## Step 2-6: 品質レポート

`llmops report quality --since 7d`:

- Prompt別・版別のスコア推移
- 決定的評価の違反内訳(どのルールで落ちているか)
- `degraded=1`(Fallback経由)で生成されたコンテンツの件数と割合
- 未帰属文の多い記事トップN(Groundedness低下の検出)
- Judge のパース失敗率(Judge自体の健全性)

---

## この Phase でやらないこと

- canary / rollback の自動判定(Phase 3)
- 資産台帳・監査の本格運用(Phase 3)
- Judge を人間評価でキャリブレーションする仕組み(19章 §8.3。利用者1名の環境では
  「気になったケースを手で見る」で代替。仕組み化は必要になってから)
