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
