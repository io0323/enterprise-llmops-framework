# NOTES — 設計外判断の記録

設計書に無い事項について選んだ最小実装と、その理由を記録する(CLAUDE.md 絶対ルール15)。

## 確定済みの判断

### N-001 ELF の実装言語を Python にした
5システム中 JVM が2・Python が3。課題(Promptハードコード・クライアント重複・コスト破棄)は
すべて Python 側に集中しており、統合対象の重心が Python にある。Kotlin で書くと統合のたびに
プロセス境界を跨ぐことになり、CLI バッチ前提の運用と噛み合わない。

### N-002 Gateway を常駐サーバにせず埋込ライブラリにした
APAP gateway と同形の HTTP サーバにすると、DDE/CGMP の CLI バッチ実行のたびに
別プロセスの起動・死活管理が必要になる。利用者1名の環境では運用負債が利益を上回る。
Core をサーバで包める構造(`gateway/` が I/O を持たない)にしてあるので、後から追加できる。

### N-003 テンプレートエンジンに Jinja2 を使わない
制御構文(if/for)が Prompt に入ると、変更の影響範囲がテストで追えなくなる。
`{{ var }}` と最小フィルタのみの自前レンダラにして、分岐が要る場合は Prompt を分ける方針にした。
依存も1つ減る。

### N-004 Fallback を1段に限定した
多段 Fallback は、設定ミス時に連鎖して大量呼び出しを起こす。`no_fallback=True` を付けて
再帰することで、構造的に2段目が発生しないようにした。

### N-005 JVM 側(PEP/APAP)との統合を「契約一致のみ」に留めた
5システムが長期テスト中の現在、プロセス統合による回帰リスクが利益を上回る。
Prompt 状態機械・`id@version`・renderHash 定義・論理モデル名体系を一致させておけば、
JVM 側のテストが終わった時点で選択肢 A(APAP 委譲)/B(PEP 統合)を後から選べる。
`docs/05_既存システム統合.md` §5.1 参照。

### N-006 `llm_logs`(CGMP)を即削除せず併存させる
`spans` が上位互換であることを実データで確認するまで、過去ログの参照先を失わないため。
Phase 1 完了後に INSERT を停止し、Phase 3 で移行スクリプトを用意する。

### N-007 `claude -p` 応答の必須フィールドを `result` のみにした
CLI のバージョン更新で応答スキーマが変わりうる。`total_cost_usd` や `usage` を必須にすると、
CLI 更新のたびに全システムが停止する。欠損は WARN ログのみで処理継続とし、
`raw_response_json` に生の応答を丸ごと残して後から再集計できるようにした。

### N-008 Prompt fragment を Phase 1 から入れる(当初保留から変更)
当初は「重複を許容し、3箇所以上で同一文言が出たら再検討」としていたが、CGMPの実コード調査で
共有ルール文字列が既に**7件**(`NO_CONTEXT` / `CONTEXT_RULE` / `NO_HALLUCINATION_RULE` /
`TIME_SENSITIVE_RULE` / `PROMOTION_RULE` / `EXCEPTION_RULE` / `STYLE_RULES`)あり、
outline/section/closing/sns の複数Promptから参照されていることが判明した。
複製すると1ルールの修正が複数ファイルの同時編集になり、外部化の利益が消える。

ただし機能は最小に絞る: **ネスト禁止・変数渡し禁止・1段のみ**。
パラメータ依存のテキスト(`term_rule()` / `tone_rule()`)は fragment にせず、
呼び出し側で組み立てて通常の変数として渡す。テンプレートに制御構文を持ち込まない方針(N-003)を守るため。

### N-009 Prompt は `llm/prompts.py` 以外にも散在していた
CGMPの `quality/rubric.py:71` `rubric_prompt()` と `formatter/sns.py:84` `sns_prompt()` にも
Prompt文字列がある。移行対象は3ファイル。
これらはPrompt定義と後処理ロジックが同居しているため、**Prompt文字列だけを移し、
後処理(スコア変換・文字数計算・ハッシュタグ整形)はCGMP側に残す**。

### N-010 `claude -p --output-format json` の実応答フィールドを実測・記録した(Phase 0)
測定日 2026-09-20 / Claude Code 2.1.108。`docs/impl/_claude_cli_response_sample.json` が実物、
`tests/fixtures/claude_cli_response_success.json` が Adapter テスト用のコピー。

実在したトップレベルフィールド:
`type` `subtype` `is_error` `duration_ms` `duration_api_ms` `num_turns` `result` `stop_reason`
`session_id` `total_cost_usd` `usage` `modelUsage` `permission_denials` `terminal_reason`
`fast_mode_state` `uuid`

`usage` の中身:
`input_tokens` `output_tokens` `cache_creation_input_tokens` `cache_read_input_tokens`
`server_tool_use` `service_tier` `cache_creation` `inference_geo` `iterations` `speed`

**設計は変更しない**(絶対ルール9 / N-007)。`03_詳細設計.md` §5.4 が拾う予定の
`total_cost_usd` / `duration_api_ms` / `num_turns` / `session_id` /
`usage.input_tokens` / `usage.output_tokens` / `usage.cache_read_input_tokens` /
`usage.cache_creation_input_tokens` は**全て実在した**。
設計に無かった `stop_reason` `terminal_reason` `modelUsage` 等は構造化カラムを増やさず
`raw_response_json` に残すのみとする(カラム追加は実データで必要性が示されてから)。

### N-011 サンプル応答の実モデル名を匿名化してコミットした
実応答の `modelUsage` は実モデル名をキーに持つ。絶対ルール13(実モデル名を成果物に書かない)に
抵触するため、キーのみ `<model-1>` へ置換して保存した。値・構造・他フィールドは実物のまま。
Adapter は `modelUsage` を読まない(§5.4 の取得対象外)ため、テストの有効性は損なわれない。

## 未決事項

- `spans` の保持期限。Phase 3 で決める。当面は無期限。
- fragment の `includes:` を front matter で明示させるか、本文の `{{ include.x }}` から
  自動解決させるか。Phase 1 は明示(宣言漏れをエラーにできるため)。運用してから再評価する。
