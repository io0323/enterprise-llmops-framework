"""SQLite 接続。WAL モード・外部キー有効(CLAUDE.md 技術スタック)。

ELF は自分の DB(`data/llmops.sqlite3`)だけを持ち、既存5システムの DB には触らない。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

MEMORY = ":memory:"


def read_schema() -> str:
    return SCHEMA_PATH.read_text(encoding="utf-8")


def connect(db_path: Path | str) -> sqlite3.Connection:
    """接続を開いて PRAGMA を適用する。スキーマ適用は `init_schema()` が行う。

    ``:memory:`` を渡すとインメモリDBになる(テスト用)。WAL はインメモリでは
    使えないため、その場合だけ設定を省く。
    """
    is_memory = str(db_path) == MEMORY
    if not is_memory:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if not is_memory:
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


#: 既存DBへ後から足した列(表, 列, 型定義)。
#: `CREATE TABLE IF NOT EXISTS` は**既存の表に列を足さない**ため、明示的に当てる。
#: 追加は常に nullable か定数 DEFAULT にすること(SQLite の ALTER の制約)。
MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    # Phase 2: 評価の健全性を後から選別できるようにするための列(NOTES.md N-033)
    ("eval_runs", "mode", "TEXT NOT NULL DEFAULT 'evaluation'"),
    ("eval_runs", "trace_id", "TEXT"),
    ("eval_runs", "judge_model", "TEXT"),
    ("eval_runs", "errors", "INTEGER NOT NULL DEFAULT 0"),
    ("eval_runs", "degraded_spans", "INTEGER NOT NULL DEFAULT 0"),
    ("eval_runs", "note", "TEXT"),
    ("eval_results", "kind", "TEXT NOT NULL DEFAULT 'deterministic'"),
    ("eval_results", "status", "TEXT NOT NULL DEFAULT 'ok'"),
    # created_at は CURRENT_TIMESTAMP を既定にできない(ALTER の制約)。
    # 書き込み側が必ず明示的に入れるので既定なしで足す
    ("eval_results", "created_at", "TIMESTAMP"),
    # Phase 3: 実課金とサブスク換算を分ける(NOTES.md N-045)
    ("model_versions", "billable", "INTEGER NOT NULL DEFAULT 1"),
    ("spans", "billable", "INTEGER NOT NULL DEFAULT 1"),
    ("cost_daily", "billable", "INTEGER NOT NULL DEFAULT 1"),
)

#: 列を**足した直後だけ**流す補正SQL(既存行の初期値が実態と違う場合)。
#: 列追加と同じ条件で1回だけ走るので冪等。
BACKFILLS: dict[tuple[str, str], tuple[str, ...]] = {
    # 既存行は既定の 1(実課金)で入る。過去の claude_cli / mock の行は
    # サブスク換算・無課金なので 0 に直す。これをしないと、課金していない
    # 過去のコストが予算を食ったままになる(N-045 の発端そのもの)
    ("spans", "billable"): (
        "UPDATE spans SET billable = 0 WHERE adapter IN ('claude_cli', 'mock')",
    ),
    ("model_versions", "billable"): (
        "UPDATE model_versions SET billable = 0 WHERE adapter IN ('claude_cli', 'mock')",
    ),
    # cost_daily は adapter を持たないので、論理モデル名で突き合わせる
    ("cost_daily", "billable"): (
        "UPDATE cost_daily SET billable = 0 WHERE logical_model IN"
        " (SELECT logical_name FROM model_versions WHERE adapter IN ('claude_cli', 'mock'))",
    ),
}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def apply_migrations(conn: sqlite3.Connection) -> list[str]:
    """不足している列を足す。既にあれば何もしない(冪等)。"""
    applied: list[str] = []
    for table, column, decl in MIGRATIONS:
        if not _table_exists(conn, table):
            continue  # この後の schema.sql が列つきで作る
        if column in _column_names(conn, table):
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        for statement in BACKFILLS.get((table, column), ()):
            conn.execute(statement)
        applied.append(f"{table}.{column}")
    if applied:
        conn.commit()
    return applied


def init_schema(conn: sqlite3.Connection) -> None:
    """schema.sql を適用する(CREATE ... IF NOT EXISTS のため冪等)。

    既存DBには先に不足列を足してから流す。ELF の DB は ELF 自身のものなので
    変更してよい(絶対ルール1 が守るのは既存5システムの DB)。
    """
    apply_migrations(conn)
    conn.executescript(read_schema())
    conn.commit()


@contextmanager
def session(db_path: Path | str) -> Iterator[sqlite3.Connection]:
    """接続をコンテキストマネージャで返す。抜けるときに commit / close する。

    例外時は rollback してから送出する。
    """
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    return [str(row["name"]) for row in rows]
