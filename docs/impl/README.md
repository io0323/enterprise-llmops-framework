# Claude Code 実装プロンプト集 — ELF

`docs/impl/` は **Claude Code に投げる実装指示**。`prompts/` は Prompt Registry の実体なので混同しないこと。

| ファイル | 内容 | 前提 |
|---|---|---|
| `phase0_bootstrap.md` | リポジトリ初期化・開発環境・CI・骨格 | なし |
| `phase1_core.md` | Gateway / Registry / Trace / Cost / 互換shim / DDE・CGMP移行 | Phase 0 完了 |
| `phase2_evaluation.md` | 評価・回帰ゲート・Judge・Groundedness | Phase 1 完了 |
| `phase3_governance.md` | 統制・canary/rollback・資産台帳・Harness統合 | Phase 2 完了 |

---

## 使い方

プロジェクトルートで `claude` を起動し、以下を**そのまま貼る**。`CLAUDE.md` は自動で読まれるが、
明示的に参照させたほうが絶対ルールの遵守率が上がる。

### Phase 0 キックオフ

```
このリポジトリは Enterprise LLMOps Framework(ELF)です。
設計は完了済み、実装はこれから。リモートは
https://github.com/io0323/enterprise-llmops-framework (未連携)。

まず CLAUDE.md の絶対ルール15項目を読んでください。これは以降の全判断に優先します。
特に次の3点は破ると設計の前提が崩れます:
- 既存5システム(PEP/APAP/Harness/DDE/CGMP)のコードとDBスキーマを、
  docs/05_既存システム統合.md の記載外で変更しない
- 既存機能を再実装しない(RAGはCGMP、Agent実行はHarness、RoutingはAPAPが正)
- Prompt本文をPythonコードに書かない

その上で docs/impl/phase0_bootstrap.md を開き、Step 0-1 から順に実行してください。
各Stepの完了条件を満たしてから次へ進むこと。確認質問はせず、
設計に無い判断は最小実装を選んで NOTES.md に理由を記録してください。
```

### Phase 1 キックオフ

```
Phase 0 は完了しています。docs/impl/phase1_core.md を開き、Step 1-1 から順に実装してください。

このPhaseの本質は「既存の動いているシステムを壊さずに、LLM呼び出しを1点に集約し、
Prompt をコードから出すこと」です。新機能を作ることではありません。

Phase 0 の実測結果を前提にしてください:
- docs/03_詳細設計.md §5.4 が拾う予定のフィールドは claude -p の実応答に全て実在する
  (total_cost_usd / duration_api_ms / num_turns / session_id /
   usage.{input,output,cache_read_input,cache_creation_input}_tokens)。設計変更は不要
- 実応答は tests/fixtures/claude_cli_response_success.json にある。
  Step 1-2 の claude_cli Adapter テストはこれを使うこと。新たに手書きしない
- 上記に加えて欠損版・is_error版・不正JSON版の fixture を作り、
  「必須は result のみ、他は .get() で欠損許容」(絶対ルール9)を検証すること
- fixture の modelUsage は実モデル名を匿名化済み(N-011)。
  Adapter はこのフィールドを読まない。読む実装にしないこと(絶対ルール13)

Step 1-7(DDE移行)と Step 1-8(CGMP移行)で既存リポジトリを触りますが、
その前に必ず docs/05_既存システム統合.md を読み直してください。
Prompt 移行では「ゴールデンファイルでバイト一致を検証してから切り替える」
「文言改善を同時にやらない」を必ず守ること。

なお pyproject の coverage fail_under は現在グローバル80%です。Phase 1 では
governance/ や eval/ が空のまま残るため、これが「カバレッジを満たすためだけの
テスト」を誘発します。CLAUDE.md の規定は gateway / prompt / registry の80%なので、
計測対象をその3つに絞る設定へ変更し、判断を NOTES.md に記録してください。

各Stepの完了条件を満たしたらテストを通し、コミットしてから次へ進んでください。
```

### Phase 2 / Phase 3

同様に `docs/impl/phase2_evaluation.md` / `phase3_governance.md` を指定する。
Phase 2 のキックオフでは次を添えると精度が上がる:

```
Phase 2 の目的は一つです。Prompt変更を、評価を通さずに本番へ出せなくすること。
機能を増やすことではありません。Step 2-5(閉ループ)を飛ばすと
このPhase全体が数ヶ月で飾りになるので、必ず実装してください。
```

---

## 途中で止まった / 脱線したときの戻し方

```
CLAUDE.md の絶対ルールと、今やっている Step の完了条件を読み直してください。
今の実装はどの Step のどの完了条件に向かっていますか。
完了条件に含まれない作業をしているなら、そこで止めてください。
```

よくある脱線と、その場で使える指摘:

| 脱線 | 指摘 |
|---|---|
| 既存コードを「ついでに」リファクタし始める | 絶対ルール1・10。5システムは長期テスト中。移行手順の記載外は触らない |
| `strip_fence` / `extract_json` を改善し始める | 絶対ルール10。そのまま移植する。改善はPhase 2以降 |
| Jinja2 を入れようとする | 技術スタック変更禁止 + NOTES.md N-003。制御構文をPromptに持ち込まない |
| Prompt文字列をPython定数として書く | 絶対ルール11。これを破ると本プロジェクトの存在理由が消える |
| Trace記録失敗で例外を投げる実装にする | 絶対ルール4。観測が本体の可用性を下げるのは本末転倒 |
| キャッシュ・多段Fallback・動的Routingを作り始める | Phase 3 で「実装しない」と明記済み。必要性が実データで示されてから |
| RAG / Agent実行 / 承認ゲートを実装し始める | 絶対ルール3。既存に委譲する範囲 |
| 「確認していいですか」と聞いてくる | 絶対ルール15。最小実装を選んで NOTES.md に記録する |

---

## コミット方針

Phase / Step の境界で、**テストが通ってから**コミットする。

```
docs:  設計ドキュメントの追加・更新
feat:  機能実装
fix:   不具合修正
test:  テスト追加
chore: 設定・CI・依存
refactor: 内部構造の変更(挙動不変)
```

既存リポジトリ(DDE / CGMP)を触る Step 1-7 / 1-8 は、**ELF側とは別リポジトリのコミット**になる。
それぞれのリポジトリで、そのリポジトリの規約に従ってコミットすること。
