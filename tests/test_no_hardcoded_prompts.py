"""ELF 自身が Prompt をコードに持たないことの保証(絶対ルール11)。

`src/llmops/` の Python コードに長い文字列リテラルがあったら失敗させる。
これを破ると本プロジェクトの存在理由が消えるので、CI で機械的に止める。

docstring は対象外(仕様の説明であって Prompt ではない)。SQL も対象外だが、
**SQL と判定するのは `db/repository.py` の中だけ**にしてある。どこでも SQL 例外を
認めると「SELECT から始まる Prompt」で素通りできてしまうため。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import llmops

PKG_ROOT = Path(llmops.__file__).resolve().parent

#: これを超える文字列リテラルを Prompt 候補とみなす(実装指示 Step 1-6)
MAX_LITERAL_CHARS = 200

#: SQL を書いてよい唯一のファイル(リポジトリパターン。CLAUDE.md コーディング規約)
SQL_FILE = PKG_ROOT / "db" / "repository.py"

_SQL_RE = re.compile(
    r"^\s*(SELECT|INSERT|UPDATE|DELETE|CREATE|WITH|PRAGMA|ALTER|DROP)\b", re.IGNORECASE
)


def _source_files() -> list[Path]:
    return sorted(p for p in PKG_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """docstring として使われている定数ノードの id を集める。"""
    found: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = getattr(node, "body", [])
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            found.add(id(body[0].value))
    return found


def _long_literals(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstrings = _docstring_nodes(tree)
    allow_sql = path == SQL_FILE

    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in docstrings or len(node.value) <= MAX_LITERAL_CHARS:
            continue
        if allow_sql and _SQL_RE.match(node.value):
            continue
        found.append((node.lineno, node.value[:80].replace("\n", "\\n")))
    return found


def test_no_long_string_literals_in_source() -> None:
    violations = [
        f"{path.relative_to(PKG_ROOT)}:{line}: {excerpt}…"
        for path in _source_files()
        for line, excerpt in _long_literals(path)
    ]
    assert not violations, (
        f"{MAX_LITERAL_CHARS} 文字を超える文字列リテラルがあります。"
        "Prompt 本文は prompts/**/*.md へ(絶対ルール11):\n" + "\n".join(violations)
    )


def test_detector_catches_a_planted_prompt(tmp_path: Path) -> None:
    """検査が実際に効いていること(空振りするテストにしない)。"""
    planted = tmp_path / "planted.py"
    planted.write_text(
        '"""docstring は見逃す。" * 1"""\n' f'PROMPT = "{"あ" * 300}"\n', encoding="utf-8"
    )
    assert _long_literals(planted)


def test_docstrings_are_not_flagged(tmp_path: Path) -> None:
    path = tmp_path / "doc.py"
    path.write_text(f'"""{"あ" * 300}"""\n', encoding="utf-8")
    assert not _long_literals(path)


def test_sql_is_allowed_only_in_repository(tmp_path: Path) -> None:
    """SQL の例外は repository.py 限定。他のファイルでは通らない。"""
    sql = "SELECT " + ", ".join(f"col{i}" for i in range(60)) + " FROM spans"
    assert len(sql) > MAX_LITERAL_CHARS

    elsewhere = tmp_path / "other.py"
    elsewhere.write_text(f'QUERY = """{sql}"""\n', encoding="utf-8")
    assert _long_literals(elsewhere)


def test_prompt_templates_live_in_markdown() -> None:
    """雛形 Prompt は .md のデータファイルとして同梱されていること。"""
    templates = PKG_ROOT / "templates" / "prompts"
    assert list(templates.rglob("*.md"))
    assert not list(templates.rglob("*.py"))
