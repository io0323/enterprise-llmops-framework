"""保持期限とアーカイブ(Step 3-5)。

`spans.request_text` / `response_text` は全文記録のため肥大化する。

**メタデータ(コスト・トークン・render_hash・Prompt版)は消さない。**
これらが消えると長期の傾向分析ができなくなる。消すのは本文だけ。
削除する前に必ず JSONL でアーカイブへ書き出す(消してから「見たかった」は取り返せない)。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from llmops.logging_utils import get_logger

logger = get_logger(__name__)


@dataclass
class RetentionPlan:
    """適用予定(`--dry-run` で見せるもの)。"""

    text_cutoff: str
    span_cutoff: str
    strip_ids: list[str] = field(default_factory=list)
    delete_ids: list[str] = field(default_factory=list)
    archive_path: Path | None = None

    @property
    def empty(self) -> bool:
        return not self.strip_ids and not self.delete_ids


def _cutoff(days: int, *, now: datetime | None = None) -> str:
    moment = (now or datetime.now(UTC)) - timedelta(days=days)
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def plan(repo: Any, config: Any, *, now: datetime | None = None) -> RetentionPlan:
    """何を消すかを決める。この時点では何も変更しない。"""
    text_cutoff = _cutoff(config.retention.span_text_days, now=now)
    span_cutoff = _cutoff(config.retention.span_days, now=now)

    delete_ids = [str(row["id"]) for row in repo.spans_older_than(span_cutoff)]
    delete_set = set(delete_ids)
    strip_ids = [
        str(row["id"])
        for row in repo.spans_older_than(text_cutoff, with_text_only=True)
        if str(row["id"]) not in delete_set
    ]
    return RetentionPlan(text_cutoff=text_cutoff, span_cutoff=span_cutoff,
                         strip_ids=strip_ids, delete_ids=delete_ids)


def archive(repo: Any, config: Any, span_ids: list[str], *, today: str | None = None) -> Path:
    """消す前に JSONL で書き出す。"""
    stamp = today or date.today().isoformat()
    directory: Path = config.archive_dir
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"spans_{stamp}.jsonl"

    with path.open("a", encoding="utf-8") as handle:
        for span_id in span_ids:
            row = repo.get_span(span_id)
            if row is None:
                continue
            handle.write(json.dumps(dict(row), ensure_ascii=False, default=str) + "\n")
    return path


def apply(
    repo: Any, config: Any, *, dry_run: bool = False, now: datetime | None = None
) -> RetentionPlan:
    """保持期限を適用する。`dry_run` なら計画だけ返す。"""
    result = plan(repo, config, now=now)
    if dry_run or result.empty:
        return result

    to_archive = result.strip_ids + result.delete_ids
    result.archive_path = archive(repo, config, to_archive)

    stripped = repo.strip_span_text(result.strip_ids)
    deleted = repo.delete_spans(result.delete_ids)
    repo.insert_audit_log(
        event="retention.apply",
        detail={
            "stripped": stripped,
            "deleted": deleted,
            "text_cutoff": result.text_cutoff,
            "span_cutoff": result.span_cutoff,
            "archive": str(result.archive_path),
        },
    )
    logger.info(
        "保持期限を適用しました: 本文削除 %d 件 / 行削除 %d 件 → %s",
        stripped, deleted, result.archive_path,
    )
    return result
