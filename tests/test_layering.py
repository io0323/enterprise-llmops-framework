"""モジュール依存の絶対規約(CLAUDE.md)を機械検証する。

sdk → gateway → (prompt/registry/guard/observability) → db
                        ↓
                   adapters(SPIのみ。上位を import しない)

空のうちに入れておくためのテスト(docs/impl/phase0_bootstrap.md Step 0-5)。
違反した瞬間に CI が落ちる状態を、実装が入る前に作っておく。
"""

from __future__ import annotations

import ast
from pathlib import Path

import llmops

PKG_ROOT = Path(llmops.__file__).resolve().parent

# レイヤ番号。小さいほど下位。上位を import してはならない。
LAYERS: dict[str, int] = {
    "db": 0,
    "prompt": 1,
    "registry": 1,
    "guard": 1,
    "observability": 1,
    "gateway": 2,
    "eval": 3,
    "governance": 3,
    "sdk": 3,
}

# どのレイヤからも import してよい共有モジュール(設定・DTO・ログ・例外)。
# errors は adapters も送出するため、gateway ではなくここに置く(NOTES.md N-015)。
SHARED: frozenset[str] = frozenset({"config", "models", "logging_utils", "errors"})

# adapters は SPI のみ。llmops の他サブパッケージを一切 import しない(絶対ルール8)。
ADAPTERS = "adapters"

ALL_SUBPACKAGES: frozenset[str] = frozenset(LAYERS) | {ADAPTERS}


def _module_name(path: Path) -> str:
    """ファイルパスを llmops.* のモジュール名へ変換する。"""
    rel = path.relative_to(PKG_ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(["llmops", *parts])


def _top_component(module: str) -> str | None:
    """llmops.<component>... の <component> を返す。llmops 自身なら None。"""
    parts = module.split(".")
    if parts[0] != "llmops" or len(parts) < 2:
        return None
    return parts[1]


def _imported_llmops_modules(path: Path) -> set[str]:
    """ファイルが import している llmops.* モジュール名を返す(相対importも解決)。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    self_module = _module_name(path)
    is_package = path.name == "__init__.py"
    found: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "llmops" or alias.name.startswith("llmops."):
                    found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = node.module or ""
                if base == "llmops" or base.startswith("llmops."):
                    found.add(base)
                    for alias in node.names:
                        found.add(f"{base}.{alias.name}")
            else:
                # 相対import: 自モジュールの位置から基点パッケージを求める
                anchor = self_module.split(".")
                if not is_package:
                    anchor.pop()
                up = node.level - 1
                if up:
                    anchor = anchor[:-up] if up < len(anchor) else ["llmops"]
                base = ".".join([*anchor, node.module] if node.module else anchor)
                found.add(base)
                for alias in node.names:
                    found.add(f"{base}.{alias.name}")
    return found


def _source_files() -> list[Path]:
    return sorted(p for p in PKG_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def _edges() -> list[tuple[str, str, Path]]:
    """(importする側のcomponent, importされる側のcomponent, ファイル) の一覧。"""
    out: list[tuple[str, str, Path]] = []
    for path in _source_files():
        src = _top_component(_module_name(path))
        if src is None:
            src = "<root>"
        for imported in _imported_llmops_modules(path):
            dst = _top_component(imported)
            if dst is None:
                continue
            if src != dst:
                out.append((src, dst, path))
    return out


def test_package_skeleton_exists() -> None:
    """設計(02_基本設計.md §4)のサブパッケージが揃っていること。"""
    for name in sorted(ALL_SUBPACKAGES):
        assert (PKG_ROOT / name / "__init__.py").is_file(), f"missing package: llmops.{name}"
    for name in sorted(SHARED | {"cli"}):
        assert (PKG_ROOT / f"{name}.py").is_file(), f"missing module: llmops.{name}"


def test_adapters_import_only_spi() -> None:
    """adapters は gateway / prompt / registry / db / sdk 等を import しない(絶対ルール8)。

    共有モジュール(`errors` / `logging_utils` 等)は許可する。Adapter は
    `AdapterError` を送出し WARN ログを出す必要があり、そこを禁じると各 Adapter が
    独自の例外型を持つことになって、呼び出し側の `except LLMError` が壊れる。
    禁止したいのは「上位レイヤへの依存」であって共有語彙の利用ではない。
    """
    violations = [
        f"{path}: llmops.{dst}"
        for src, dst, path in _edges()
        if src == ADAPTERS and dst not in SHARED
    ]
    assert not violations, "adapters が上位レイヤを import している:\n" + "\n".join(violations)


def test_db_does_not_import_upper_layers() -> None:
    """db は最下層。他のサブパッケージを import しない。"""
    violations = [
        f"{path}: llmops.{dst}"
        for src, dst, path in _edges()
        if src == "db" and dst in ALL_SUBPACKAGES
    ]
    assert not violations, "db が上位レイヤを import している:\n" + "\n".join(violations)


def test_layer_order_is_respected() -> None:
    """下位レイヤが上位レイヤを import していないこと。共有モジュールは例外。"""
    violations: list[str] = []
    for src, dst, path in _edges():
        if dst in SHARED or src not in LAYERS or dst not in LAYERS:
            continue
        if LAYERS[dst] > LAYERS[src]:
            violations.append(
                f"{path}: llmops.{src}(L{LAYERS[src]}) → llmops.{dst}(L{LAYERS[dst]})"
            )
    assert not violations, "逆向きの依存がある:\n" + "\n".join(violations)


def test_no_import_cycles() -> None:
    """サブパッケージ間に循環importが無いこと。"""
    graph: dict[str, set[str]] = {}
    for src, dst, _ in _edges():
        graph.setdefault(src, set()).add(dst)

    visiting: set[str] = set()
    done: set[str] = set()
    cycles: list[str] = []

    def visit(node: str, trail: list[str]) -> None:
        if node in done:
            return
        if node in visiting:
            start = trail.index(node)
            cycles.append(" → ".join([*trail[start:], node]))
            return
        visiting.add(node)
        for nxt in sorted(graph.get(node, ())):
            visit(nxt, [*trail, nxt])
        visiting.discard(node)
        done.add(node)

    for node in sorted(graph):
        visit(node, [node])

    assert not cycles, "循環importがある:\n" + "\n".join(cycles)
