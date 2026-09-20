# Phase 0 実装プロンプト — リポジトリ初期化と開発環境

Phase 1 に入る前の下準備。**コードは1行も書かない**(骨格と設定のみ)。

リモート: `https://github.com/io0323/enterprise-llmops-framework`(未連携)

---

## Step 0-1: 設計ドキュメントの読み込み

実装に入る前に、以下を全て読む。読まずに進むと、既存5システムとの契約を壊す。

```
CLAUDE.md                      ← 絶対ルール15項目。全判断に優先する
docs/01_要件定義.md            ← 現状分析(実コード調査結果)・FR/NFR
docs/02_基本設計.md            ← 構成・モジュール・トレードオフ
docs/03_詳細設計.md            ← DBスキーマ・Prompt形式・公開I/F・CLI
docs/05_既存システム統合.md    ← 移行手順(ファイル・行番号レベル)
NOTES.md                       ← 既に確定している設計外判断(N-001〜N-009)
```

読み終えたら、**理解の要約を3行で述べてから**次のStepへ進む。要約に次の3点が含まれていない場合は読み直すこと:

1. ELF が新規実装する範囲と、既存に委譲する範囲の境界
2. Prompt 外部化における「ゴールデンファイルによるバイト一致検証」の位置付け
3. Trace 記録が best-effort である理由

---

## Step 0-2: Git リポジトリ初期化

```bash
cd /Users/io/projects/GitHub/engine/需要発見エンジン/enterprise-llmops-framework
git init -b main
git remote add origin https://github.com/io0323/enterprise-llmops-framework.git
```

**注意**:
- `.gitignore` は既にある。`data/` `logs/` `output/` `.env` が除外されていることを確認する
- 空ディレクトリはGitに乗らない。`src/llmops/` `tests/` `evals/` `prompts/_fragments/` に
  `.gitkeep` を置く(`data/` `logs/` `output/` は gitignore 対象なので不要)
- `.DS_Store` が混入していないか `git status` で確認する

初回コミット:

```bash
git add .
git commit -m "docs: ELF設計書一式(要件定義/基本設計/詳細設計/統合設計/実装プロンプト)"
git push -u origin main
```

**push が失敗した場合**: リモートが空でない(README等が既にある)可能性がある。
`git pull --rebase origin main` してから push し直す。強制pushはしない。

---

## Step 0-3: 開発環境

```bash
conda create -n llmops python=3.11 -y
conda activate llmops
pip install -e ".[dev]"
```

確認:

```bash
python -c "import typer, yaml, pydantic; print('deps OK')"
claude --version
claude -p "ping" --output-format json      # headlessが動くこと(必須)
sqlite3 --version
```

**`claude -p` が動かない場合はここで止める。** LLM層が丸ごと成立しない。

### `claude -p` の応答フィールド実測(重要)

Phase 1 の `claude_cli` Adapter は、この応答から `total_cost_usd` / `usage` / `session_id` /
`num_turns` / `duration_api_ms` を拾う。**実際に何が返るかをこの時点で確認し、記録する**:

```bash
claude -p "1+1は?" --output-format json | python3 -m json.tool | tee docs/impl/_claude_cli_response_sample.json
```

結果を見て:
- 存在するフィールドを `NOTES.md` に記録する
- **存在しないフィールドがあっても設計を変えない**。絶対ルール9のとおり `.get()` で欠損許容し、
  `raw_response_json` に生応答を丸ごと保存する方針は維持する
- サンプルJSONは `tests/fixtures/` にもコピーし、Adapterのテストに使う

---

## Step 0-4: CI(GitHub Actions)

既存5システムにはCIが無い。ELFは**移行の安全網**であり、ここだけはCIを入れる。

`.github/workflows/ci.yml`:

```yaml
name: CI
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: "3.11"}
      - run: pip install -e ".[dev,eval]"
      - run: ruff check src tests
      - run: mypy src/llmops
      - run: pytest -q --cov=llmops --cov-report=term-missing
```

**CI で `claude` は使えない**(CLIが無い)。したがって:
- 全テストが `mock` Adapter と `subprocess.run` のモックで完結すること(絶対ルール14)
- `claude` を必要とするテストがあれば、それは設計違反。書き直す

Phase 1 完了までは `mypy` を `continue-on-error: true` にしてよい。Phase 1 完了時に外す。

---

## Step 0-5: 空パッケージの骨格

`docs/02_基本設計.md` §4 のモジュール構成で、**空の `__init__.py` だけ**を作る。
中身はPhase 1で実装する。この時点で依存の向きを固定しておくのが目的。

```
src/llmops/
├── __init__.py
├── cli.py                (Typer app のみ)
├── config.py             (空)
├── models.py             (空)
├── logging_utils.py      (空)
├── gateway/__init__.py
├── adapters/__init__.py
├── prompt/__init__.py
├── registry/__init__.py
├── observability/__init__.py
├── guard/__init__.py
├── eval/__init__.py
├── governance/__init__.py
├── db/__init__.py
└── sdk/__init__.py
```

`tests/test_layering.py` を**この時点で書く**(中身が空でも通る):

```python
"""モジュール依存の絶対規約(CLAUDE.md)を機械検証する。

sdk → gateway → (prompt/registry/guard/observability) → db
                        ↓
                   adapters(SPIのみ。上位を import しない)
"""
```

検証内容:
- `llmops.adapters.*` が `gateway` / `prompt` / `registry` / `db` / `sdk` を import していない
- `llmops.db.*` が上位レイヤを import していない
- 循環importが無い

**先にこのテストを書く理由**: 後から入れると必ず違反が溜まっていて、直すのが面倒になる。
空のうちに入れておけば、違反した瞬間にCIが落ちる。

---

## Phase 0 完了条件

- [ ] 設計ドキュメントを読み、3行要約を述べた
- [ ] `git push` が成功し、GitHubにドキュメント一式が上がっている
- [ ] `pip install -e ".[dev]"` が成功し、`llmops --help` が(空でも)動く
- [ ] `claude -p "ping" --output-format json` の実応答を記録した
- [ ] CI が green(テストは `test_layering.py` のみでよい)
- [ ] `git log --oneline` が2コミット以上

完了したら Phase 1(`docs/impl/phase1_core.md`)へ進む。

---

## この Phase でやらないこと

- ビジネスロジックの実装(Phase 1)
- 既存5システムのファイルを開く・変更する(Phase 1 Step 1-7 以降)
- `prompts/**/*.md` の中身を書く(Phase 1 Step 1-3 の雛形生成で作る)
- README の実装状況テーブルの更新(Phase 1 完了時にまとめて)
