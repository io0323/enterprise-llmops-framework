-- ELF スキーマ。docs/03_詳細設計.md §2 の DDL をそのまま反映する。
-- Phase 2-3 で使うテーブル(eval_* / asset_catalog)も最初から作る(後からの ALTER を避ける)。
-- 変更する場合は設計書 §2 と同時に直すこと。

-- ============ §2.1 Observability ============
-- 論理的な1処理単位(記事1本の生成、DDEの1ラン、Harnessの1パイプライン実行)
CREATE TABLE IF NOT EXISTS traces (
    id            TEXT PRIMARY KEY,          -- UUID4
    system        TEXT NOT NULL,             -- 'cgmp' / 'dde' / 'harness' / 'elf'
    operation     TEXT NOT NULL,             -- 'article.generate' / 'topic.classify' 等
    external_id   TEXT,                      -- 呼び出し元のID(CGMPのrequest_id等)
    status        TEXT NOT NULL DEFAULT 'running', -- running/success/partial/failed
    started_at    TIMESTAMP NOT NULL,
    finished_at   TIMESTAMP,
    meta_json     TEXT                       -- 任意の付帯情報
);
CREATE INDEX IF NOT EXISTS idx_traces_system   ON traces(system, started_at);
CREATE INDEX IF NOT EXISTS idx_traces_external ON traces(external_id);

-- LLM呼び出し1回。FR-030/031/032 の中核
CREATE TABLE IF NOT EXISTS spans (
    id                TEXT PRIMARY KEY,
    trace_id          TEXT NOT NULL REFERENCES traces(id) ON DELETE CASCADE,
    parent_span_id    TEXT REFERENCES spans(id),
    seq               INTEGER NOT NULL,      -- trace内の連番
    task              TEXT NOT NULL,         -- 'outline'/'section'/'closing'/'judge' 等
    -- 資産の刻印(FR-032): これが無いと再現不能
    prompt_id         TEXT,
    prompt_version    INTEGER,
    render_hash       TEXT,                  -- レンダリング後テキストのSHA-256(先頭16桁)
    logical_model     TEXT NOT NULL,         -- 'chat-standard' 等
    model_version     INTEGER,
    adapter           TEXT NOT NULL,         -- 'claude_cli'/'anthropic_sdk'/'mock'
    resolved_target   TEXT,                  -- Adapterが実際に使った識別子(コマンド/モデル名)
    -- 入出力(絶対ルール: 全文記録)
    request_text      TEXT NOT NULL,
    response_text     TEXT,
    raw_response_json TEXT,                  -- Adapterの生応答(metaごと保存)
    -- 実行結果
    success           INTEGER NOT NULL,
    degraded          INTEGER NOT NULL DEFAULT 0,  -- Fallbackで得た結果か
    billable          INTEGER NOT NULL DEFAULT 1,  -- 1=実課金, 0=サブスク換算(予算の対象外)
    attempt           INTEGER NOT NULL DEFAULT 1,
    error_type        TEXT,
    error_message     TEXT,
    -- 計測(FR-031)
    duration_ms       INTEGER,
    api_duration_ms   INTEGER,
    input_tokens      INTEGER,
    output_tokens     INTEGER,
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    cost_usd          REAL,
    num_turns         INTEGER,
    provider_session  TEXT,                  -- claude -p の session_id 等
    -- 設計 §2.1 に無い追加列。Phase 1 実装指示 Step 1-4 が span 単位の
    -- `meta.truncated` を要求するが §2.1 に置き場が無いため追加した(NOTES.md N-013)。
    meta_json         TEXT,
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_spans_trace  ON spans(trace_id, seq);
CREATE INDEX IF NOT EXISTS idx_spans_prompt ON spans(prompt_id, prompt_version);
CREATE INDEX IF NOT EXISTS idx_spans_date   ON spans(created_at);

-- ============ §2.2 Prompt ============
CREATE TABLE IF NOT EXISTS prompt_versions (
    prompt_id     TEXT NOT NULL,             -- 'cgmp.section'(ドット区切り: system.name)
    version       INTEGER NOT NULL,
    status        TEXT NOT NULL,             -- draft/review/approved/published/deprecated/archived
    body          TEXT NOT NULL,             -- front matter除去後の本文テンプレート
    front_matter  TEXT NOT NULL,             -- YAML原文
    var_schema    TEXT,                      -- JSON Schema(変数定義)
    content_hash  TEXT NOT NULL,             -- fragment展開後のbody + front_matter のSHA-256
                                             -- (§3.1: fragment変更が参照元の新versionを生む)
    source_path   TEXT NOT NULL,             -- prompts/cgmp/section.md
    owner         TEXT,
    note          TEXT,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (prompt_id, version)
);

-- 現在有効な版へのポインタ。publish/rollback はこの1行の更新で完結(FR-050)
CREATE TABLE IF NOT EXISTS prompt_deployments (
    prompt_id       TEXT PRIMARY KEY,
    active_version  INTEGER NOT NULL,
    canary_version  INTEGER,                 -- NULL ならCanaryなし
    canary_percent  INTEGER NOT NULL DEFAULT 0,  -- 0-100
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_by      TEXT
);

CREATE TABLE IF NOT EXISTS prompt_transitions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    prompt_id   TEXT NOT NULL,
    version     INTEGER NOT NULL,
    from_status TEXT,
    to_status   TEXT NOT NULL,
    actor       TEXT,
    reason      TEXT,
    eval_run_id TEXT,                        -- publish時の根拠となった評価実行
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ============ §2.3 Model ============
CREATE TABLE IF NOT EXISTS model_versions (
    logical_name   TEXT NOT NULL,            -- 'chat-standard'/'chat-fast'/'judge'
    version        INTEGER NOT NULL,
    adapter        TEXT NOT NULL,
    params_json    TEXT NOT NULL,            -- Adapterへ渡すパラメータ
    price_json     TEXT,                     -- {"input_per_1k":0.0,"output_per_1k":0.0}
    fallback_to    TEXT,                     -- 別の論理モデル名
    status         TEXT NOT NULL DEFAULT 'active', -- active/deprecated/blocked
    billable       INTEGER NOT NULL DEFAULT 1,  -- 1=実課金, 0=サブスク換算(予算の対象外)
    config_hash    TEXT NOT NULL,            -- models.yaml該当エントリのハッシュ
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (logical_name, version)
);

-- ============ §2.4 Cost / Guard ============
CREATE TABLE IF NOT EXISTS budgets (
    id            TEXT PRIMARY KEY,          -- 'global' / 'cgmp' / 'dde' ...
    scope         TEXT NOT NULL,             -- 'global'/'system'
    period        TEXT NOT NULL,             -- 'monthly'/'daily'
    limit_usd     REAL NOT NULL,
    hard_limit    INTEGER NOT NULL DEFAULT 1,  -- 1=超過時に拒否, 0=警告のみ
    warn_percents TEXT NOT NULL DEFAULT '[50,80,100]',
    enabled       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS cost_daily (
    day        TEXT NOT NULL,                -- 'YYYY-MM-DD'
    system     TEXT NOT NULL,
    logical_model TEXT NOT NULL,
    calls      INTEGER NOT NULL DEFAULT 0,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd   REAL NOT NULL DEFAULT 0,
    -- 予算(budgets)が見るのは billable = 1 の行だけ。0 はサブスク換算で追加課金が無い
    billable   INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (day, system, logical_model)
);

-- ============ §2.5 Evaluation(Phase 2)============
CREATE TABLE IF NOT EXISTS eval_suites (
    id          TEXT PRIMARY KEY,            -- 'cgmp-outline'
    prompt_id   TEXT NOT NULL,
    definition  TEXT NOT NULL,               -- evals/*.yaml 原文
    thresholds_json TEXT NOT NULL,           -- 合否閾値
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS eval_cases (
    id          TEXT PRIMARY KEY,
    suite_id    TEXT NOT NULL REFERENCES eval_suites(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    vars_json   TEXT NOT NULL,               -- Promptへ渡す変数
    expect_json TEXT,                        -- 期待値/制約
    origin      TEXT,                        -- 'manual'/'production-failure'
    source_span_id TEXT,                     -- 本番失敗から起票した場合
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS eval_runs (
    id             TEXT PRIMARY KEY,
    suite_id       TEXT NOT NULL,
    prompt_id      TEXT NOT NULL,
    prompt_version INTEGER NOT NULL,
    logical_model  TEXT NOT NULL,
    total          INTEGER NOT NULL,
    passed         INTEGER NOT NULL,
    score          REAL,                     -- 0.0-1.0
    baseline_run_id TEXT,
    verdict        TEXT NOT NULL,            -- pass/fail/regressed/error
    cost_usd       REAL,
    started_at     TIMESTAMP,
    finished_at    TIMESTAMP,
    -- 設計 §2.5 に無い追加列(NOTES.md N-033)。いずれも
    -- 「偽の合格を作らせない」ために、後から選別できる形で残すもの
    --   mode: 'evaluation' / 'wiring_check'(mock を通った run。baseline にしない)
    mode           TEXT NOT NULL DEFAULT 'evaluation',
    --   trace_id: この run の LLM 呼び出しを束ねる trace。degraded と実コストを導出する
    trace_id       TEXT,
    judge_model    TEXT,
    --   errors: Judge のパース失敗など「採点できなかった」件数。平均から除外した数
    errors         INTEGER NOT NULL DEFAULT 0,
    --   degraded_spans: 縮退実行(Fallback)で得た span の数。1以上なら verdict は fail
    degraded_spans INTEGER NOT NULL DEFAULT 0,
    note           TEXT
);
CREATE INDEX IF NOT EXISTS idx_eval_runs_prompt ON eval_runs(prompt_id, prompt_version);

CREATE TABLE IF NOT EXISTS eval_results (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL REFERENCES eval_runs(id) ON DELETE CASCADE,
    case_id     TEXT NOT NULL,
    metric      TEXT NOT NULL,               -- 'schema'/'forbidden'/'groundedness'/'relevance'
    score       REAL,
    passed      INTEGER NOT NULL,
    detail      TEXT,
    span_id     TEXT,                        -- 評価のためのLLM呼び出し
    -- 設計 §2.5 に無い追加列(NOTES.md N-033)
    --   kind: 'deterministic' / 'judge'。決定的評価と Judge を混ぜて平均しないため
    kind        TEXT NOT NULL DEFAULT 'deterministic',
    --   status: 'ok' / 'error'。error は「採点できなかった」。スコア0とは別物
    status      TEXT NOT NULL DEFAULT 'ok',
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_eval_results_run ON eval_results(run_id, case_id);

-- ============ §2.6 Governance(Phase 3)============
CREATE TABLE IF NOT EXISTS asset_catalog (
    asset_type   TEXT NOT NULL,              -- 'prompt'/'model'/'eval_suite'/'agent'
    asset_id     TEXT NOT NULL,
    owner        TEXT NOT NULL,
    purpose      TEXT,
    systems      TEXT,                       -- 利用システム(カンマ区切り)
    risk_level   TEXT NOT NULL DEFAULT 'low',-- low/medium/high
    last_used_at TIMESTAMP,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (asset_type, asset_id)
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event      TEXT NOT NULL,                -- 'prompt.publish'/'quota.exceeded'/'policy.violation'
    actor      TEXT,
    subject    TEXT,
    detail_json TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_audit_event ON audit_logs(event, created_at);
