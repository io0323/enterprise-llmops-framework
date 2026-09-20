# Phase 3 実装プロンプト — Governance / Deployment / Cost 統制

前提: Phase 2 の完了条件(評価ゲートが実際に publish を止められる状態)を満たしていること。

このPhaseの目的は、**資産が増えても管理不能にならない状態**を作ること(19章 §12 §13 §14)。

---

## Step 3-1: Deployment(canary / rollback)

Phase 1 で `prompt_deployments` テーブルと publish/rollback の骨格は作ってある。ここに段階展開を足す。

1. `governance/deploy.py` —
   - `canary(prompt_id, version, percent)` — `canary_version` / `canary_percent` を設定
   - Gateway の `prompt.resolve()` が canary_percent の確率で canary_version を返す
     (**決定的にしたい場合のため、`trace_id` のハッシュで振り分ける**。
     同一trace内で版が混ざると評価不能になるため、trace単位で固定する)
   - `promote(prompt_id)` — canary を active に昇格し、canary設定をクリア
   - `rollback(prompt_id, to=None)` — 直前の published 版へ戻す。`prompt_transitions` から履歴を引く
2. canary 中のスコア比較: `llmops report quality --compare-canary <prompt_id>` で
   active版とcanary版のspanを分けて集計する。
3. **自動昇格は実装しない**。判定材料を出すところまでにし、昇格は人間が叩く
   (CGMP絶対ルール2「自動公開の実装禁止」と同じ思想)。

**完了条件**: `llmops prompt canary cgmp.section --version 5 --percent 10` の後、
spanの `prompt_version` が概ね9:1で分かれる。`llmops prompt rollback cgmp.section` が5分以内に完了する。

---

## Step 3-2: Cost 統制の強化

Phase 1 で予算判定と `cost_daily` は入っている。ここに最適化と可視化を足す。

1. `guard/quota.py` の拡張 —
   - scope別予算(`global` / `system`)の両方を判定。厳しい方を適用
   - 警告閾値(50/80/100%)到達時に `audit_logs` へ記録し、ログにWARN
   - 月次の予測消化率(当月ペース × 残日数)を `llmops budget show` に出す
2. `llmops report cost` の拡張 —
   - `--by prompt` でPrompt別コスト。どのPromptが高いかを可視化する
   - **¥/解決タスク**(trace単位のコスト)を主指標として出す(19章 §13.2)。
     span単価が安くても、失敗リトライが多ければtrace単価は高い。ここを見ないと誤最適化する
   - 失敗spanのコスト(捨てたコスト)を別建てで出す
3. Caching(19章 §13)—
   - `llmops cache` は**実装しない**。`claude -p` は同一プロンプトでも毎回課金対象ではなく、
     キャッシュ層を挟むと「古い誤答の配信」リスクだけが増える。
     必要性が出た時点で再検討し、判断を `NOTES.md` に記録すること。
4. Model Routing(19章 §13)—
   - `models.yaml` に `chat-fast` は既にある。タスク別の使い分けは**Prompt front matter の
     `model:` で宣言**する方式に留め、動的ルーティング判定は実装しない
     (判定自体がLLM呼び出しになり、本末転倒になりやすい)

---

## Step 3-3: AI 資産台帳(Asset Catalog)

19章 §14。所有者不在・用途不明の資産が増えることを防ぐ。

1. `governance/catalog.py` —
   - 登録対象: `prompt` / `model` / `eval_suite`(Phase 4で `agent` を追加)
   - `last_used_at` は `spans` から導出(バッチで更新)
   - `llmops catalog list` — 資産一覧に 所有者 / 用途 / リスク / 最終利用日 / 直近30日の呼び出し回数
   - `llmops catalog stale --days 90` — 90日使われていない資産を列挙
   - `llmops catalog set-owner <type> <id> --owner io --risk medium --purpose "..."`
2. `llmops sync` の時点で、**所有者未設定の資産があれば警告**を出す。
   ブロックはしない(作業が止まるため)が、`llmops catalog list --no-owner` で必ず拾えるようにする。
3. リスクレベル(low/medium/high)の用途: Phase 3 では表示のみ。
   将来 high の資産に追加の承認要件を課す余地を残す。

---

## Step 3-4: 監査とPolicy

1. `governance/audit.py` — Phase 1-2 で記録してきた `audit_logs` を読む側を実装。
   - `llmops audit tail [--event prompt.publish] [--since 30d]`
   - 記録対象の再確認: `prompt.*`(状態遷移・publish・forced publish・rollback・canary)/
     `quota.exceeded` / `policy.violation` / `budget.warn` / `model.blocked`
2. `guard/policy.py` — 宣言的Policyの最小実装。`policies.yaml`:

```yaml
policies:
  - id: no-unpublished-in-production
    when: {config_env: production}
    forbid: {prompt_status: [draft, review, approved]}
  - id: no-raw-completion
    forbid: {raw_completion: true}
  - id: high-risk-requires-owner
    when: {risk_level: high}
    require: {owner: present}
```

   違反時の挙動は `action: block | warn` で指定。既定は `block`。
   **Policyをドキュメントだけで終わらせず、Gatewayが実行時に評価する**(19章 §11「文書だけのPolicyにしない」)。

3. `llmops policy check` — 現在の全資産をPolicyに照らして違反を列挙する(CI用)。

---

## Step 3-5: 保持期限とアーカイブ

`spans.request_text` / `response_text` は全文記録のため肥大化する(NOTES.md 未決事項 N-007関連)。

1. `config.yaml` に `retention:` を追加:
   ```yaml
   retention:
     span_text_days: 180        # 本文を消し、メタデータ(token/cost/hash)は残す
     span_days: 730             # span行自体の削除
     archive_dir: "data/archive"
   ```
2. `llmops retention apply [--dry-run]` — 期限超過span の本文を NULL にし、
   `archived=1` を立てる。削除前に JSONL で `archive_dir` へ書き出す。
3. **メタデータ(コスト・トークン・render_hash・prompt版)は消さない**。
   これらが消えると長期の傾向分析ができなくなる。消すのは本文だけ。

---

## Step 3-6: Harness の統合

`docs/05_既存システム統合.md` §4。Phase 1-2 の仕組みが揃ってから着手する。

1. `backend/app/harness/pipeline.py` に `ops.trace("pipeline.daily", external_id=run_id)` を入れ、
   各ステップを子spanにする
2. Agent呼び出しを `adapters/anthropic_sdk.py` 経由にする
3. `backend/prompts/{generator,evaluator,learning}/*-v1.txt` に front matter を足して
   `prompts/harness/*.md` へ移す(`gen-v1.txt` → `harness.generator@1`)
4. `app/harness/cost_guard.py` は**残す**。ELFの予算は横断上位として二重防御にする
5. `budgets` に `scope='system', id='harness'` を必ず設定する
   (Harnessは有料APIを使う唯一のシステムであり、ELFの予算機能が実質的な意味を持つ唯一の場所)
6. 承認ゲート(`waiting_approval`)とパイプライン状態管理には**触らない**(絶対ルール3)

---

## Step 3-7: 運用の自動化

1. `llmops report weekly --out output/weekly_$(date +%Y%m%d).md` —
   コスト・品質・stale資産・Policy違反・degraded件数を1枚にまとめる
2. DDE/CGMPと同じく APScheduler での定期実行を用意するが、**ELF自身は常駐しない**。
   既存システムのスケジューラから `llmops report weekly` を叩く形にする
   (プロセスを増やさない。`docs/02` §9 の判断と一貫させる)

---

## Phase 3 完了条件

- 全AI資産に所有者が設定され、`llmops catalog list --no-owner` が0件
- `llmops prompt rollback` が1コマンド・5分以内で完了する
- `llmops policy check` がCIで実行でき、違反0件
- `llmops report weekly` が1枚のMarkdownで週次の状態を示す
- Harnessが ELF 経由で動き、3システム横断のコストが `llmops report cost --by system` で見える

---

## この Phase でもやらないこと

- JVM側(PEP/APAP)のコード変更。`docs/05` §5.1 の選択肢A/Bは、
  5システムの長期テストが終わった時点で再評価する(NOTES.md N-005)
- Web UI。CLI + Markdown で足りる(DDEがREST APIを廃止した判断と同じ)
- 多段Fallback / 動的Model Routing / セマンティックキャッシュ。
  いずれも「効果が不確かな割に壊れ方が複雑」な機能。必要性が実データで示されてから
