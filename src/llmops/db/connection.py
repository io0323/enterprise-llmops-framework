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


def init_schema(conn: sqlite3.Connection) -> None:
    """schema.sql を適用する(CREATE ... IF NOT EXISTS のため冪等)。"""
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
