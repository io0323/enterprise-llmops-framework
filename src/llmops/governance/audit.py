"""監査ログの参照側(Step 3-4)。

書き込みは Phase 1-2 で各所に入っている。ここは**読む側**。
「誰が・いつ・何をしたか」を後から辿れることが目的で、集計はしない
(数えると「多いか少ないか」の話になり、1件ずつ見る動機が下がる)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: 記録している主なイベント。`llmops audit tail --event` の補完用でもある
KNOWN_EVENTS = (
    "prompt.sync",
    "prompt.review",
    "prompt.approved",
    "prompt.published",
    "prompt.deprecated",
    "prompt.archived",
    "prompt.draft",
    "prompt.publish.forced",
    "prompt.rollback",
    "prompt.promote",
    "prompt.canary",
    "prompt.canary.stop",
    "model.sync",
    "quota.exceeded",
    "policy.violation",
    "policy.unpublished_execution",
    "raw_completion",
    "eval.run",
    "eval.add_case",
    "catalog.set_owner",
    "retention.apply",
    "budget.set",
    "report.weekly",
    "budget.warn",
)

#: 見つけたら必ず目を通すべきイベント(週次レポートで強調する)
ATTENTION_EVENTS = (
    "policy.violation",
    "policy.unpublished_execution",
    "prompt.publish.forced",
    "quota.exceeded",
    "raw_completion",
)


@dataclass(frozen=True)
class AuditEntry:
    created_at: str
    event: str
    actor: str | None
    subject: str | None
    detail: str | None

    @property
    def needs_attention(self) -> bool:
        return self.event in ATTENTION_EVENTS


def tail(
    repo: Any, *, event: str | None = None, since: str | None = None, limit: int = 50
) -> list[AuditEntry]:
    rows = repo.list_audit_logs(event=event, limit=limit)
    entries = [
        AuditEntry(
            created_at=str(row["created_at"] or ""),
            event=str(row["event"]),
            actor=None if row["actor"] is None else str(row["actor"]),
            subject=None if row["subject"] is None else str(row["subject"]),
            detail=None if row["detail_json"] is None else str(row["detail_json"]),
        )
        for row in rows
    ]
    if since is not None:
        entries = [entry for entry in entries if entry.created_at >= since]
    return entries


def attention_counts(repo: Any, *, since: str) -> dict[str, int]:
    """要注意イベントの件数(週次レポート用)。"""
    counts: dict[str, int] = {}
    for event in ATTENTION_EVENTS:
        found = [
            row
            for row in repo.list_audit_logs(event=event, limit=500)
            if str(row["created_at"] or "") >= since
        ]
        if found:
            counts[event] = len(found)
    return counts
