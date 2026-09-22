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

### N-012 カバレッジ計測対象を gateway / prompt / registry の3つに絞った
`pyproject.toml` の `fail_under = 80` は当初 `llmops` 全体を対象にしていたが、
CLAUDE.md の規定は「`gateway` / `prompt` / `registry` は行カバレッジ80%以上」であり、
全体80%ではない。全体を対象にすると Phase 1 で空のまま残る `eval/` `governance/` を
埋めるためだけのテストを書く圧力が生まれ、規定の意図(移行の安全網を厚くする)から外れる。

`[tool.coverage.run] source` をこの3パッケージに限定し、CI のコマンドを
`pytest -q --cov --cov-report=term-missing`(`--cov=llmops` から変更)にした。
`--cov` に値を渡すと `source` が上書きされるため。
他モジュールにテストを書かないという意味ではない(`adapters` / `db` / `sdk` にも書く)。
閾値による強制をこの3つに限る、という意味。

### N-013 `spans` に `meta_json` 列を1つだけ足した
`docs/impl/phase1_core.md` Step 1-4 は「`max_text_chars` 超過時は末尾を切り、
`meta` に `truncated: true`」を要求するが、`03_詳細設計.md` §2.1 の `spans` には
置き場が無い(`meta_json` を持つのは `traces` だけ)。設計内で完結させる案として
(a) 切り詰めマーカーを本文に混ぜる (b) `raw_response_json` に混ぜる も検討したが、
(a) は記録した入出力そのものを汚し、(b) は「Provider の生応答を丸ごと残す」という
§2.1 の設計理由を壊す。span 単位の付帯情報を置く列を1つ足すのが最小の変更と判断した。

ELF 自身の DB であり、既存5システムのスキーマではない(絶対ルール1の対象外)。
Phase 2-3 のテーブルを先に作ったのと同じ理由で、後からの ALTER を避けるため今入れた。

### N-014 `strip_fence` / `extract_json` は CGMP 版を移植した(DDE 版ではない)
実装指示(`docs/impl/phase1_core.md` Step 1-2)の指定どおり CGMP 版を採った。
DDE 版との差分は2点あり、いずれも CGMP 版が上位互換である:

| 箇所 | CGMP 版(採用) | DDE 版 |
|---|---|---|
| フェンス正規表現 | ` ```(?:json\|markdown\|md)? ` | ` ```(?:json)? ` |
| 最外ブロックの探索順 | `{}` → `[]` | `[]` → `{}` |

探索順は、説明文に `{` と `[` の両方が混ざるときだけ結果が変わる。DDE の3プロンプトは
いずれも JSON 配列を返すが、オブジェクト断片が先に現れる応答では挙動差が出る可能性がある。
Step 1-7 の移行時に、DDE の既存テストが通ることで実挙動を確認する。
**先回りして修正しない**(絶対ルール10。改善は評価スイートができてから)。

### N-015 例外階層の実体を `llmops/errors.py` に置いた(`gateway/errors.py` は再輸出)
設計 §4 のモジュール表では `gateway/errors.py` が実体だが、`adapters/*` が
`AdapterError` を送出する必要があり、そこから `gateway` を import すると
モジュール依存の絶対規約(絶対ルール8)に反する。
実体を共有モジュール `llmops/errors.py` に置き、`gateway/errors.py` は同じクラスを
再輸出するだけにした。設計書どおりの import パスも生きたまま、依存の向きも守れる。

これに伴い `tests/test_layering.py` の「adapters は他を import しない」判定を、
共有モジュール(`config` / `models` / `logging_utils` / `errors`)は許可する形に緩めた。
禁止したいのは上位レイヤへの依存であって、共有語彙の利用ではない。
Adapter に独自例外を持たせると、呼び出し側の `except LLMError` が壊れる。

### N-016 設計 §10 に無い例外を2つ足した
- `AdapterUnavailable`(`AdapterError` の下): optional extra 未インストール・未知の
  adapter 名・環境変数未設定を「解決時」に失敗させる。Step 1-2 が要求する。
  `AdapterError` の下に置いたので既存の `except LLMError` がそのまま効く。
- `PolicyViolation`(`LLMOpsError` の下): Step 1-5 の `complete_raw()` が
  `allow_raw_completion: false` のときに送出する。

### N-017 `LLMBudgetExceeded` を `QuotaExceeded` と `LLMError` の両方から継承させた
設計 §10 では `QuotaExceeded` の下にのみ置かれている。しかし既存 CGMP は
`class LLMBudgetExceeded(LLMError)` であり、`except LLMError` でも捕捉されていた。
ELF 側で `LLMError` を外すと、移行後に既存の捕捉が素通りして挙動が変わる
(絶対ルール: 既存を壊さない)。多重継承で両方を満たす。

同じ理由で `LLMOpsError` の基底を `RuntimeError` にした(既存 `LLMError(RuntimeError)` 互換)。

### N-018 front matter の `status` は初回登録時のみ有効にした
`llmops sync` が新 version を作るとき、front matter の `status` をそのまま採ると、
ファイルを編集して `status: published` と書くだけで状態機械と評価ゲート(Phase 2)を
迂回できてしまう。一方、常に `draft` から始めると、**既に本番で動いている** Prompt を
移行する初回登録でも submit → approve → publish を踏む必要があり、
「移行で挙動を変えない」(docs/05)と噛み合わない。

折衷として、**初回登録(version 1)に限り front matter の `status` を尊重**し、
2回目以降の新 version は必ず `draft` から始める(宣言があれば WARN)。
移行は宣言どおりに入り、以後の**変更**は必ずゲートを通る。

`version` も同様に front matter の値は採番に使わない(DB の採番が正)。
宣言と食い違う場合は WARN のみ。版番号の真実を2箇所に持たせないため。

### N-019 `prompt_versions.body` には fragment 展開後の本文を格納する
設計 §2.2 は「front matter 除去後の本文テンプレート」としか書いておらず、
fragment 展開の前後どちらかが読み取れない。展開後を採った理由:

- `content_hash` は展開後で計算すると設計に明記されている。body も展開後にそろえると、
  「その版が実際に何を送ったか」が1行で再現できる(再現性が資産管理の目的そのもの)
- 展開前だと、実行のたびに fragment ファイルの現在値を読む必要があり、
  「古い版を resolve したのに fragment だけ最新」というズレが起きる
- `llmops prompt diff` が、実際に送られるテキストの差分になる

fragment 自身も `_fragments.<name>` として別途版管理されるので、履歴は失われない。

### N-020 互換shim に DDE 版の `call()` も持たせた
設計 §5.2 は CGMP 版のシグネチャ(`call_json` / `call_text` / `ensure_budget` /
`calls_used` / `remaining_calls`)だけを挙げているが、DDE の `LLMClient` は
`call(prompt, task, batch_size)` という別シグネチャで、`llm/batch.py` の3箇所が使っている。
Step 1-7 を「import 文の変更だけ」で済ませるには shim が両方を持つ必要がある。

`section_seq`(CGMP)と `batch_size`(DDE)は既存 `llm_logs` の列だった。
ELF の `spans` には対応列が無いので、`meta_json` に残す(情報を落とさない)。

### N-021 adhoc 呼び出しの既定モデルを `config.yaml` に置いた(`gateway.default_model`)
互換shim は Prompt 文字列を直接受け取るため、論理モデル名を自分で決める必要がある。
コードに書くと絶対ルール12(閾値・モデル名をハードコードしない)に反するので設定に置いた。
優先順位は「呼び出し引数 → アプリ側 config の `llmops.model` → `gateway.default_model`」。
DDE は `llmops.model: dde-batch` を指定してタイムアウト300秒を維持する(Step 1-7)。

### N-022 `LLMOps.from_runtime(system=...)` は Tracer / Gateway にも system を伝える
伝えないと `traces.system` と `cost_daily.system` が ELF 自身の名前になり、
`llmops report cost --by system` が「全部 elf」になって横断観測の目的を満たさない。
テストで固定した(`test_dde_call_records_system`)。

### N-023 `calls_per_trace_limit` を system 別に上書きできるようにした
`guard.calls_per_trace_limit: 10` は CGMP 絶対ルール3(1記事10回)を ELF 側で強制するもの。
一方 DDE は「1ラン = 1 trace」で、1ランに expand / classify / cluster_naming が何十回も入る。
同じ上限を当てると DDE が動かなくなる(既存を壊す)。

`guard.calls_per_trace_limit_by_system` を足し、`dde: 0`(無制限)を既定にした。
コードに例外を書かず設定で持つ(絶対ルール12)。CGMP は従来どおり 10 が効く。

### N-024 DDE の移行手順(ゴールデンファイル)の実施記録
1. **移行前**に `build_expand_prompt` / `build_classify_prompt` /
   `build_cluster_naming_prompt` の出力を `tests/fixtures/golden/dde_*.txt` へ保存
   (この時点で `prompts.py` には一切触れていない)
2. `prompts/dde/*.md` は **テンプレート文字列を機械変換して生成**した
   (`{{`→`{` / `}}`→`}` / `{x}`→`{{ x }}`)。手で書き写すと差分が入るため
3. `tests/test_golden_prompts.py` でバイト一致を検証してから呼び出し側を切替
4. 文言は一字も変えていない

この過程で ELF 側のバグを1つ見つけた: `loader.split_front_matter` が
`splitlines()` を使っており、本文末尾の改行を落としていた。バイト一致検証が
無ければ気付かないまま、全 Prompt の末尾1バイトが変わっていた。
**ゴールデン検証は「文言を変えない」ためだけでなく、移行基盤自体のバグ検出にも効く。**

### N-025 DDE の `llm_logs` は検証対象から外し、`spans` に移した
DDE の e2e テストは `llm_logs` の行数で「LLM が何回呼ばれたか」を検証していた。
互換shim は `repo` を使わない(設計 §5.2)ため、移行後は `llm_logs` に行が入らない。
同じ性質(呼び出し回数・成否・バッチであること)を ELF の `spans` に対して検証する形へ
書き換えた。表を消してはいない(N-006 と同じ扱い。過去ログの参照先は残す)。

テストの隔離も足した: DDE の e2e は `ELF_CONFIG` を一時ディレクトリの設定に向け、
実物の `data/llmops.sqlite3` を汚さないようにしてある。
(この隔離を入れる前に1回テストを流してしまい、実DBに 34 trace / 85 span が
混入した。SQL で除去し `cost_daily` を `spans` から再構築済み。)

### N-026 本番モデルの `fallback_to` を外した(mock へ落とすと偽の結果が返る)
「未決事項」に挙げていたリスクが CGMP の移行中に**実際に起きた**。

`chat-standard` の Fallback 先は `chat-fallback`(mock, mode: echo)だった。
`claude -p` の応答が壊れたケースで Fallback が走り、mock がプロンプトをそのまま返す。
CGMP の rubric プロンプトには出力例の JSON が含まれているため、`extract_json` が
その例を拾ってしまい、**評価スコア 84.29 という偽の値が通った**
(本来は「LLM評価をスキップ」になるべきケース)。

対処: `models.yaml` の `chat-standard` / `dde-batch` から `fallback_to` を削除した。
DDE も CGMP も移行前は Fallback を持っておらず、これで既存挙動と一致する。
`chat-fallback` の定義自体は残す(Fallback 機構のテスト用)。

FR-014(Provider失敗時のFallback)は Gateway の機能として実装済みで、
`tests/test_gateway.py::test_fallback_marks_degraded` が受入条件を満たしている。
本番の論理モデルでそれを有効にするかは別の判断であり、
**Fallback 先が本物の代替 Provider になるまでは有効にしない**。

### N-027 CGMP 移行で見つかった互換shim の挙動差分(3件)
互換shim は「既存 LLMClient と同一シグネチャ」だけでは不十分で、
**同一の副作用**が要る。CGMP の既存テストが以下を検出した:

1. `_call` が `ensure_budget` を通っていなかった。既存実装は呼び出し前に必ず
   残枠を確保していた(再試行ぶん込み)。抜けていると上限0でも呼び出しが走る
2. 呼び出し上限を ELF 側の値で判定していた。CGMP は `pipeline.py` と
   `outline/generator.py` でも `writer.llm_calls_per_article_limit` を見て事前検算するため、
   shim だけ別の値を使うと両者が食い違う。アプリ側の値を優先するようにした
3. request_id ごとに毎回新しい trace を作っていた。既存の `llm_logs` は
   request_id 単位で実行を跨いで積算されるため、毎回新規だと「再開すると枠が戻る」

いずれも「シグネチャが同じでテストが通る」だけでは見つからない。
**既存テストをそのまま通すことが最も確実な検出手段だった。**

### N-028 互換shim に Registry 版の `complete_json` / `complete_text` を足した
CGMP の移行 Step 2 で、呼び出し側を `prompt_id` 指定に切り替える必要がある。
DDE は `LLMOps` へ乗り換えたが、CGMP は記事単位の呼び出し枠管理
(`calls_used` / `ensure_budget` / `call_limit`)を `LLMClient` に依存しており、
`LLMOps` へ移すとその accounting を CGMP 側に作り直すことになる。

そこで shim に Registry 版の入口を足した。`call_json` / `call_text`
(Prompt 文字列を直接渡す移行用の入口。span に版が刻まれない)は残し、
新しい呼び出しは `complete_json` / `complete_text` を使う。

結果として CGMP の差分は「Prompt 組み立て → 変数辞書」の置き換えだけになり、
呼び出し枠の扱いは1行も変わっていない。`allow_raw_completion` は
両システムの Prompt 外部化が終わった時点で `false` に戻した。

### N-029 CGMP の `prompts/cgmp/*.md` はセンチネル置換で機械生成した
CGMP の Prompt は f-string の中に条件分岐と関数呼び出しが混ざっており、
DDE のような機械変換(`{x}` → `{{ x }}`)ができない。手で書き写すと差分が入る。

そこで **現行関数にセンチネル値を渡して出力を得て、そのセンチネルを
`{{ 変数 }}` / `{{ include.断片 }}` へ置換する**方法を採った。
生成スクリプトは使い捨て(移行は1回きりなのでリポジトリに残さない)。
正しさの保証はゴールデンのバイト一致テスト(`tests/test_golden_prompts.py`)。

分岐の扱い:
- `{CONTEXT_RULE if contexts else ""}` → **Prompt を2つに分けた**
  (`cgmp.section` / `cgmp.section_no_context`)。設計 §3.2「分岐が要るなら Prompt を分ける」
  片方だけ直す事故を防ぐため、2ファイルが context_rule 以外で一致することもテストしている
- `term_rule()` / `tone_rule()` → 引数で文面が変わるので fragment にできない。
  CGMP 側(`llm/variables.py`)に残し、通常の変数として渡す(N-008 / docs/05 §2.2)

### N-030 fragment 展開時にファイル末尾の改行を落とす
fragment は本文の行中に差し込まれる断片なので、ファイル末尾の改行を残すと
参照元に空行が1つ増える。CGMP のゴールデン検証で全 Prompt が1バイト違いで落ちて発覚した。
`PromptRegistry.sync` で `rstrip("\n")` する。

DDE のときに見つけた「本文末尾の改行を落としていた」(N-024)と合わせて、
**ゴールデン検証は移行基盤自体のバグを2件検出した。**

### N-031 互換shim の trace 名をアプリ側で付けられるようにした
当初は固定で `compat.llm_client` にしていた。実データの確認中に、テスト由来の
混入行を SQL で消そうとして **同じ operation 名だった実運用の trace まで消す**
という事故を起こした(実際に1記事ぶんの記録を失った)。

`operation` を `llmops:` セクション(または引数)で指定できるようにし、
CGMP は `article.generate` を使う。これで:

- 「何の処理の trace か」が後から判る(`llmops trace list` が意味を持つ)
- テスト由来の行と実運用の行を operation で区別できる

**教訓**: 記録の名前が全部同じだと、記録を消す操作が危険になる。
観測基盤は「後から選別できる」ことまで含めて設計する必要がある。

### N-032 互換shim が開いた trace をプロセス終了時に閉じる
実データの確認で、CGMP の trace が `status=running` / `finished_at=NULL` のまま
残ることが判った。shim は span 記録のために trace を自動で作るが、
「記事の生成が終わった」ことを知る手段が無いため閉じられなかった。

`llmops trace list --status failed`(運用ガイド §2 の週次ルーチン)が
機能しないので、`atexit` で未確定の trace を閉じるようにした。
DDE も CGMP も CLI バッチなので、プロセス終了 = 処理の終わりで妥当。

status は span の成否から導出する(全部成功なら success、1つでも失敗していれば partial)。
アプリ側の完了判定(CGMP の `runs.status` など)とは別物であることを docstring に明記した。
明示的に閉じたい場合は `llm.finish(request_id, status)` を呼ぶ。

常駐プロセス(Harness)は `ops.trace()` のコンテキストマネージャを使うため、
この経路には乗らない。

### N-033 `eval_runs` / `eval_results` に列を足した(設計 §2.5 に無いもの)
Phase 2 の設計判断は全て「**偽の合格を作らない**」に寄せた(N-026 の教訓)。
そのために「後から選別できる」情報を列として持たせた:

| 表 | 列 | 何のため |
|---|---|---|
| eval_runs | `mode` | 配線確認(mock 経由)の run を baseline / publish 根拠から外す |
| eval_runs | `trace_id` | degraded と実コストを spans から導出する |
| eval_runs | `judge_model` | 誰が採点したかを残す |
| eval_runs | `errors` | 採点できなかった件数(平均から除外した数) |
| eval_runs | `degraded_spans` | 縮退実行が混ざった run を後から特定する |
| eval_runs | `note` | verdict の理由(なぜ止めたか) |
| eval_results | `kind` | 決定的評価と Judge を混ぜて平均しない |
| eval_results | `status` | `error`(採点できなかった)と スコア0 を区別する |

`CREATE TABLE IF NOT EXISTS` は既存の表に列を足さないので、
`db/connection.py` に明示的なマイグレーション表(`MIGRATIONS`)を置いた。
ELF の DB は ELF 自身のものなので変更してよい(絶対ルール1 が守るのは既存5システムの DB)。

### N-034 配線確認(mock 経由)の run に専用の verdict を作った
「mock を評価経路に入れさせない」という要求に対し、当初は `fail` にしていたが、
`fail` は「品質が悪い」と読まれる。実態は**「品質を判定していない」**なので、
`verdict='wiring_check'` という別の値にした。

- publish ゲートは `verdict == 'pass'` のみを通すので、公開の根拠にはならない
- `latest_eval_run` は `mode='evaluation'` で絞るので、baseline にもならない
- CLI の終了コードは 0(配線は動いているため)。ただし出力に大きく明示する

**2重に塞いである**(verdict と mode)。片方を将来変えても、もう片方が残る。

### N-035 カバレッジ計測対象に `llmops.eval` を足した
N-012 で「CLAUDE.md の規定どおり gateway / prompt / registry の3つ」に絞ったが、
Phase 2 で追加した `eval` は**「偽の合格を作らない」ことが仕事**のモジュールであり、
ここが緩むと Phase 2 全体が無意味になる。規定の意図(移行・公開の安全網を厚くする)に
照らして対象に入れた。現在 93%。

### N-036 Judge の応答は「スキーマ検証まで通って初めて成功」にした
N-026 の直接原因は、`extract_json` が **Prompt 内の出力例 JSON** を拾ったこと。
「JSON として読めた」を成功とすると、同じ事故が評価基盤で再発する。

`eval/judge.py::parse_judge_payload` は次を全て満たしたときだけスコアを作る:

- オブジェクトであること(1要素の配列は実機で頻出するので解いて許す。2要素以上は拒否)
- `score` が**数値**であること(`bool` は除く。文字列 "0.0〜1.0の数値" は拒否)
- `score` が 0.0-1.0 の範囲にあること
- `reason` が空でないこと(理由の無いスコアは改善に使えない)

満たさない場合は採点**失敗**として `eval_results.status='error'` に記録し、
**スコアの平均から除外する**(0点にしない)。run は止めない。
`tests/test_eval_judge.py` に、N-026 で実際に通ってしまった rubric の出力例を
「採点にならない」ことの回帰テストとして入れてある。

### N-037 Judge が落ちても代替モデルへ逃がさない
`eval.forbid_judge_fallback: true`(既定)で、judge 論理モデルに `fallback_to` が
設定されていたら**評価の実行そのものを拒否**する。

理由: 代替モデルで穴埋めされたスコアは「誰が採点したか」が分からない。
`eval_runs.judge_model` に記録した値と実際の採点者が食い違う記録は、
比較の土台として使えない。黙って別モデルの点数を採用するくらいなら実行しない。

### N-038 評価のときだけ published 以外の版を実行できるようにした
**Phase 2 の設計に穴があった。** 実 CLI で通して初めて分かった:

- Gateway は `status != published` を拒否する(FR-021 / 設計 §6)
- 評価ゲートは「この版に対する eval_run があること」を要求する(Step 2-4)
- publish 前の候補版は `approved` なので、**評価しようとすると Gateway が拒否する**

つまり「評価してから publish」が原理的に成立しない状態だった。

`CompletionRequest.allow_unpublished` を足し、評価の生成呼び出しだけが True を渡す。
**緩めるのは状態チェックだけ**で、Guard / Trace / Cost は通常どおり通る
(「評価専用の抜け道を作らない」= Gateway を迂回しない、という意図は守っている)。

アプリの通常経路が従来どおり拒否することもテストで固定した
(`test_gateway_still_refuses_unpublished_in_the_app_path`)。

### N-039 CLI の想定内エラーはトレースバックを出さない
評価ゲートで止まるのも予算上限で止まるのも「正しく止まった」状態であって、
バグではない。`LLMOpsError` 系は1行のメッセージで返し、終了コード1にする。
予期しない例外はトレースバックのまま出す(そちらは調査が要る)。

`[project.scripts]` の入口を `llmops.cli:app` から `llmops.cli:run_cli` に変えた。

### N-040 未公開版の実行は「呼び出し側の申告」では許さない
N-038 で足した `CompletionRequest.allow_unpublished` は、published ゲートを
迂回できる経路である。フラグを立てれば通るなら、ゲートは実質無い。

`Gateway._authorize_unpublished` が許すのは次の2つだけ:

1. `gateway.allow_unpublished: true`(開発時の設定。本番は false)
2. **その trace が評価実行のものであること**。`eval_runs` に `trace_id` が
   一致する行があり、かつ `prompt_id` が一致するかで判定する

`allow_unpublished=True` が渡されても、2の裏付けが無ければ `PolicyViolation` にする。
**「評価経路かどうか」を呼び出し側の申告ではなく DB の実体で決める。**
実体を作るには `eval_runs` に行を入れる必要があり、それ自体が記録として残る。

どちらの経路で通した場合も:
- `audit_logs` に `policy.unpublished_execution`(どの run・どの prompt・どの版・理由)
- span の `meta_json` に `unpublished_execution: <status>` の印

`llmops policy check` の `no_unpublished_in_production` が、この印を数えて
**評価実行の裏付けが無いものだけ**を違反として挙げる。

### N-041 週次レポートを `governance` に置いた(`observability` ではない)
集計対象がコスト(observability)・Policy(guard)・台帳と監査(governance)に跨る。
`observability` に置くと `observability → guard → observability` の循環になり、
依存の向きの絶対規約に反する(`tests/test_layering.py` が検出した)。
週次レポートは「統制のための読み物」なので governance が置き場として正しい。

### N-042 canary の振り分けを trace_id のハッシュにした
乱数だと、1つの trace(=1記事の生成)の中で版が混ざりうる。混ざるとその出力が
どちらの版のものか言えず、比較にならない。`sha256(trace_id) % 100 < percent` で
**同じ trace は必ず同じ版**になるようにした。trace_id が無い場合(CLI の単発実行)
だけ乱数に落ちる。

### N-043 `policy check` の warn は「消す」のではなく「残す」
移行で初回登録した版(`docs/05` の手順で published として登録したもの)には
評価実行の裏付けが無い。`production-versions-need-evidence` がこれを warn で挙げる。

形だけの評価スイートを作って warn を消すことはしない。**「裏付けが無い」は事実であり、
消すべきなのは警告ではなく状態のほう**(Phase 2 の「偽の合格を作らない」と同じ考え方)。
CI が見るのは `block` のみ(現在0件)。warn は週次レポートで棚卸しする。

### N-044 Harness の統合(Step 3-6)
`shopping-sns-auto-operation/backend` の LLM 呼び出しを ELF 経由にした。
`cost_guard.py`・承認ゲート・`job_queue.py`・パイプラインの状態遷移には触れていない。

**Prompt の正本をどちらに置くか。** Harness は Prompt を自前の DB(`prompt_versions`)で
版管理しており、Learning Agent が改善版(gen-v2…)を提案し、人が API で activate する。
これは Harness の HITL であり ELF が吸収するものではない(絶対ルール3)。一方で
「DB で activate すれば ELF の評価ゲートを通らずに本番の文面が変わる」状態を残すと、
Phase 2 の目的(評価を通さずに publish できない)が Harness だけ抜け落ちる。そこで:

- v1 の3本を `prompts/harness/{generator,evaluator,learning}.md` に登録した
  (`{x}` → `{{ x }}` の機械変換のみ。ゴールデン5件でバイト一致を確認済み。
  ゴールデンは Harness の .venv で**移行前の**現行関数を直接呼んで採取した)
- Harness は従来どおり DB の本文でレンダリングし、その本文を `expected_text` として
  ELF に渡す。ELF は自分のレンダリング結果とバイト比較し、違えば **LLM を呼ぶ前に**
  `PromptMismatch` にする(課金も span も発生しない)
- 結果として、Learning の提案版を Harness で activate しただけでは生成が止まる。
  **ELF に登録 → 評価 → publish してから activate する**、が新しい手順になる。
  止まり方は「候補1件の生成失敗」(generation.py が捕捉して次へ進む)で、
  エラーメッセージが登録手順を示す。黙って古い版で生成するより止まるほうを選んだ

**呼び出し口の形。** Agent → `LlmClient.complete(job_id, agent, model, prompt, max_tokens)`
の形は変えていない。既存テストの Fake がこのキーワード引数をそのまま受けるため。
Prompt の ELF 上の身元(prompt_id / 変数)は `RegisteredPrompt`(`str` のサブクラス)に
載せて運ぶ。文字列としては従来の本文そのものなので、書き出し API や Fake からは区別がつかない。

- 改善指示ブロック(「# 改善指示 …」)の文言は Harness の Python に残し、変数
  `improvement` で渡す。ELF のテンプレートは制御構文を持たないため(N-024 と同じ扱い)。
  文言を ELF 側へ移すのは、評価スイートが揃ってから(移行の鉄則4)
- `retry=False`。Harness は再試行していなかった。有料APIの呼び出し回数を増やさない
- 応答本文は ELF の結果(strip_fence 済み)ではなく raw の text ブロックを連結して返す。
  応答の解釈は従来どおり各 Agent の `strip_code_fence` に任せる
- 挙動差1点: 応答が空のとき、ELF は `LLMError("LLM応答が空です")` を送出する
  (従来は空文字が返り JSON 解析で失敗していた)。Learning ではステータスが
  `invalid_llm_response` ではなくステップ失敗になる。実害が小さいので吸収していない

**論理モデル。** Agent ごとに `harness-generator / -evaluator / -learning`。
Harness は Agent ごとに実モデルと max_tokens(learning だけ 2048)が違うため。
実モデル名は環境変数(`MODEL_GENERATOR` など。Harness の Settings と同名)から解決する。
Harness の Settings は .env を環境変数に展開しないので、`LlmClient` が Settings の値を
環境変数へ書き込んでから呼ぶ。API キーは Harness が作った SDK クライアントを
`Gateway.use_adapter()` で渡す(ELF は API キーの在り処を知らない)。
単価は Harness の標準単価表と同じ値を `models.yaml` に書いた。以前の
「環境変数 ELF_PRICE_* で上書き」というコメントは**実装が無い記述**だったので消した。

**ステップの記録。** docs/05 §4.2 の `tr.child(step)` は spans に行を作らない。
spans は Guard の呼び出し回数とコスト集計の単位で、LLM 以外の行を混ぜると両方が狂うため。
ステップ内の span の meta に `step` を付け、ステップ一覧(名前・所要時間・成否)は
traces.meta_json の `steps` に残す。

**スレッド。** Harness は API(BackgroundTasks)とスケジューラが別スレッドで LLM を呼ぶ。
ELF の SQLite 接続は作ったスレッドでしか使えないので、Harness 側の ELF ハンドルは
スレッドローカルにした(進行中 trace もスレッドごとになり、並行実行で混ざらない)。

**Guard / 予算。** `calls_per_trace_limit_by_system.harness: 0`。候補数は Harness の
strategy.yaml が持つので ELF で二重に数えない。有料APIの歯止めは回数ではなく金額で、
`budgets` に `system=harness` 月20 USD(hard)を設定した(= Harness の月3000円 / 150円)。
cost_guard(Harness の DB の LlmUsage を見る)と二重防御になる。LlmUsage の記録は従来どおり。

**CI と導入。** ELF は別リポジトリで Harness の CI には入らない。
- ELF 経由のテスト(6件)は `pytest.importorskip("llmops")` でスキップされる。
  mypy は `llmops.*` を ignore_missing_imports にした。ELF に `py.typed` を足したので、
  ELF を入れた環境では Harness の mypy が ELF の型で検査される
- テストは ELF の記録先を一時DBに差し替える(autouse fixture)。本番の ELF DB を汚さない
- 導入: `uv pip install --python .venv/bin/python -e <ELF のパス>`。
  **`uv sync` は既定で余分なパッケージを消す**ので、実行後は入れ直すか `uv sync --inexact` を使う。
  入っていなければ `LlmClient` は `ElfUnavailableError` で止まる(API を直接叩く経路は残していない)

**確認。** 実 DB のコピー上で、偽の SDK クライアントを使って Harness の生成→評価を1サイクル
流し、`report cost --by system` に cgmp / dde / elf / harness が並ぶことを確かめた。
実 DB には流していない(課金を伴う実行と、偽の結果での汚染を避けるため)。
実 DB に harness の行が載るのは、次にパイプラインが実際に走ったとき。

### N-045 予算は「実課金ぶん」だけを見る(`billable`)
Step 3-6 で Harness(唯一の実課金システム)が ELF 経由になった直後に、予算統制が
**逆立ちしている**ことが分かった。

- `claude -p`(CGMP/DDE)の `span.cost_usd` はサブスク利用の換算値で、追加請求は無い
- それが `cost_daily` に積み上がり、`global_monthly_usd: 20.0` を食う
- CGMP は実測で1記事あたり約 0.67 USD。月30記事で global に到達する
- global は hard quota。その時点で、**実際に課金している Harness まで止まる**

課金していない処理が、課金している処理を止める。金額の桁ではなく性質が違うものを
同じ器で比べていたのが原因なので、性質を宣言できるようにした。

**決めたこと。** `models.yaml` の論理モデルに `billable: true/false` を持たせ、
`budgets` の判定対象を `billable: true` のコストだけにした。

- 未宣言のときは adapter から決める(`claude_cli` / `mock` は false)。
  ただし**既定は true(課金される側)**。新しい Provider を足して宣言を忘れても、
  予算を素通りして黙って請求が伸びる、という向きには倒れない。
  逆向きの失敗(課金しないものを課金扱いして止める)は、止まった時点で気付ける
- リポジトリの `models.yaml` では全モデルに明示的に宣言した。宣言漏れはテストで落とす
- `billable: false` のコストも **cost_daily / spans に従来どおり記録する**。
  予算の対象から外すだけで、可視性は落とさない
- `report cost` / 週次レポートに「うち実課金」列と「予算」列(対象 / 対象外 / 混在)を
  足した。合計も実課金とサブスク換算に分けて出す。
  `budget show` は、当月のサブスク換算ぶんを参考値として併記する
- 呼び出し**回数**の上限(`calls_per_trace_limit`)は課金と無関係なので従来どおり全てに効く。
  CGMP 絶対ルール3(1記事10回)はコストの話ではない

**混ぜなかったもの。** サブスク換算コストにも上限を置きたくなるが、`budgets` には
入れない。意味の違う数字を同じテーブルに入れると、どちらの上限に当たったのかが
判らなくなる(今回の問題の再生産)。必要になった時点で別枠として設計する。

**`global_monthly_usd` の意味が変わった。** 「ELF が記録した全コストの上限」から
「**実課金の上限**」へ。値は 20.0 のまま据え置いた。実課金は現状 Harness だけで、
Harness 側の月次上限(3000円 / 150円 = 20 USD)と一致しているため。

**既存DBの移行。** `budgets` テーブルの行は変更不要(`global` / `harness` ともに
意味が「実課金の上限」に変わるだけで、値は妥当)。直すのは過去のコスト行のほうで、
これは列追加と同時に自動で当たる(`db/connection.py` の `BACKFILLS`)。

1. `llmops` を新しい版にする
2. 何らかの ELF コマンドを1回実行する(DB を開いた時点で移行が走る)。
   `spans` / `cost_daily` / `model_versions` に `billable` 列が足され、
   過去行のうち `claude_cli` / `mock` のものが 0 に直る
3. `llmops sync` で `models.yaml` の宣言を DB に反映する(新 version が採番される)
4. 確認: `llmops budget show` の使用済みが実課金ぶんだけになっていること、
   `llmops report cost --by system` の「うち実課金」が Harness 以外 0 であること

移行は列を足した瞬間だけ走るので冪等。既に列がある DB では何もしない。

### N-046 サブスク利用にハード上限を付けず、異常検知にした
N-045 で予算の対象を実課金ぶんに限定した結果、`claude_cli` 系(CGMP/DDE)には
月次の歯止めが無くなった。`calls_per_trace_limit` は **trace 単位**なので、
trace そのものが大量に作られるケース(スケジューラの二重起動、失敗ジョブの
無限リトライ)は捉えられない。

**ハード上限は付けない。** 理由は N-045 と同じ形の間違いになるため:

- サブスク利用のコストは、止めても**お金は1円も減らない**。
  止める行為に金銭的な根拠が無い
- それでも止めれば、「課金していない処理が、金銭的な理由で止まる」。
  N-045 で直した逆転を、global 予算の代わりに専用上限という形で作り直すだけ
- しかも止まるのは本番の生成処理(CGMP の記事生成、DDE のラン)。
  暴走していない通常実行まで、月末に近づくほど止まりやすくなる
- 本当に困るのは「金額」ではなく「**意図しない使われ方**」。
  それは上限値ではなく、普段との差でしか判定できない

**代わりにしたこと。** 異常検知を週次レポートと `report cost` に入れた。

- 判定は「直近 N 日の**中央値**に対する倍率」。平均ではなく中央値にしたのは、
  1日の跳ね(まとめて記事を作った日)が基準を押し上げて、翌週の本当の異常を
  隠してしまうため
- **コストだけでなく trace 生成数も同じ基準で見る**。1回あたりが安い処理が
  暴走した場合、コストが目立つ前に件数のほうが先に外れる
- 見るのは予算の対象外(`billable: false`)のコストだけ。実課金ぶんは budgets が
  止めるので、ここで二重に騒がない
- 閾値は `config.yaml` の `anomaly:`(絶対ルール12)。既定は
  中央値の3倍 / 日額5 USD以上 / 日次20 trace以上
- **検出しても止めない。** 警告として出すだけで、止める判断は人がする

**下限(`min_cost_usd` / `min_traces`)を併用した理由。** 倍率だけで見ると、
静かな日の 0.01 USD → 1.00 USD が「100倍」として毎週出る。そうなったレポートは
読まれなくなり、本物の異常も一緒に無視される。**鳴りっぱなしのレポートは、
鳴らないレポートより害が大きい**(Phase 2 の「偽の合格を作らない」と同じ理由で、
ここでは「偽の警告を作らない」)。既定を控えめにしてあるのはそのため。

見落としのほうは許容している。この検知は最後の砦ではなく、週次で人が見る材料。
本当に止めたいものがあれば、それはコードの不具合なので上限ではなく実装を直す。

## 未決事項

- `spans` の保持期限。Phase 3 で決める。当面は無期限。
- ~~**`chat-fallback` が `mock` であることの是非**~~ → N-026 で解消(fallback_to を外した)。
  以下は経緯として残す。`models.yaml` の当初設計では
  `chat-standard` / `dde-batch` の Fallback 先は `mock`(echo)である。
  本番で `claude_cli` が落ちると、mock がプロンプトをそのまま返す。JSON 期待の
  呼び出し(DDE の全3種・CGMP の outline/closing)は `extract_json` が失敗して
  結局 `AllProvidersFailed` になるので実害は小さいが、本文期待の呼び出し
  (CGMP の section)は**プロンプトそのものが本文として返る**。`degraded=1` は
  刻まれるものの、長期テスト中のデータに混入する余地がある。
  Phase 1 では設計どおりにしてある(FR-014 の受入条件が「mock へ切替わること」のため)。
  実運用で `degraded` が観測されたら、Fallback 先を「明示的に失敗する Adapter」に
  変えるか、本文期待の呼び出しだけ Fallback を無効にするかを再検討する。
- fragment の `includes:` を front matter で明示させるか、本文の `{{ include.x }}` から
  自動解決させるか。Phase 1 は明示(宣言漏れをエラーにできるため)。運用してから再評価する。
