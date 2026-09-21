"""閉ループ — 本番の失敗を評価ケースに変換する(Step 2-5。最重要)。

評価スイートが陳腐化すると Phase 2 全体が飾りになる。
`llmops eval add-case <suite_id> --from-span <span_id>` がその導線。

span から変数を復元できる範囲には限界がある(Gateway は変数そのものを
保存していない。保存しているのはレンダリング後のテキスト)。
そこで **復元できなかった変数は空文字で登録し、その事実を呼び出し側へ返す**。
黙って「それらしい値」を作らない — 偽のケースは偽の合格を生む(N-026 と同じ問題)。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from llmops.errors import EvaluationFailed
from llmops.logging_utils import get_logger
from llmops.observability.tracer import new_id

logger = get_logger(__name__)

ORIGIN_PRODUCTION_FAILURE = "production-failure"
ORIGIN_MANUAL = "manual"


@dataclass
class AddedCase:
    case_id: str
    name: str
    variables: dict[str, Any]
    missing_variables: list[str] = field(default_factory=list)


def add_case_from_span(
    runtime: Any, suite_id: str, span_id: str, *, name: str | None = None
) -> AddedCase:
    """span からケースを起こす。

    Raises:
        EvaluationFailed: span が無い / スイートが無い / 既に同じ span から作られている。
    """
    repo = runtime.repo
    span = repo.get_span(span_id)
    if span is None:
        raise EvaluationFailed(f"span が見つかりません: {span_id}")

    suite_row = repo.get_eval_suite(suite_id)
    if suite_row is None:
        raise EvaluationFailed(
            f"評価スイートが DB にありません: {suite_id}(先に `llmops eval list` で取り込む)"
        )
    if repo.case_exists_for_span(suite_id, span_id):
        raise EvaluationFailed(f"この span からのケースは既にあります: {span_id}")

    prompt_id = span["prompt_id"]
    if prompt_id is not None and str(prompt_id) != str(suite_row["prompt_id"]):
        raise EvaluationFailed(
            f"span の prompt_id({prompt_id})がスイートの対象"
            f"({suite_row['prompt_id']})と違います"
        )

    variables, missing = _restore_variables(runtime, span)
    case_id = new_id()
    case_name = name or _default_name(span)
    repo.insert_eval_case(
        case_id=case_id,
        suite_id=suite_id,
        name=case_name,
        vars_json=json.dumps(variables, ensure_ascii=False),
        expect_json=json.dumps(
            {
                "source": "production-failure",
                "span_success": int(span["success"]),
                "error_type": span["error_type"],
                "error_message": span["error_message"],
                "restored_variables": sorted(k for k in variables if k not in missing),
                "missing_variables": missing,
            },
            ensure_ascii=False,
        ),
        origin=ORIGIN_PRODUCTION_FAILURE,
        source_span_id=span_id,
    )
    repo.insert_audit_log(
        event="eval.add_case",
        actor=runtime.config.system,
        subject=f"{suite_id}:{case_id}",
        detail={"span_id": span_id, "missing_variables": missing},
    )
    if missing:
        logger.warning(
            "span から復元できなかった変数があります(空文字で登録): %s", ", ".join(missing)
        )
    return AddedCase(
        case_id=case_id, name=case_name, variables=variables, missing_variables=missing
    )


def _default_name(span: Any) -> str:
    stamp = str(span["created_at"] or datetime.now(UTC).strftime("%Y-%m-%d"))[:10]
    status = "failed" if not int(span["success"]) else "review"
    return f"{span['task']}-{status}-{stamp}"


def _restore_variables(runtime: Any, span: Any) -> tuple[dict[str, Any], list[str]]:
    """Prompt の変数スキーマと span の `request_text` から変数を復元する。

    Gateway が保存しているのは**レンダリング後のテキスト**なので、変数そのものは
    厳密には戻せない。ここでは:

    - Prompt の `variables.required` を見て、必要な変数名を列挙する
    - 復元できないものは空文字にし、名前を `missing` として返す
    - レンダリング後テキスト全体を `_rendered_prompt` として残し、人が補えるようにする

    「それらしい値」を推測して埋めない。偽のケースは偽の合格を生むため。
    """
    variables: dict[str, Any] = {}
    missing: list[str] = []

    prompt_id = span["prompt_id"]
    if prompt_id is not None and span["prompt_version"] is not None:
        try:
            resolved = runtime.prompts.resolve(str(prompt_id), int(span["prompt_version"]))
        except Exception as exc:  # noqa: BLE001 - 復元は best-effort
            logger.warning("Prompt 版を解決できませんでした: %s", exc)
        else:
            schema = resolved.var_schema or {}
            required = schema.get("required") or []
            for key in required:
                variables[str(key)] = ""
                missing.append(str(key))

    variables["_rendered_prompt"] = str(span["request_text"] or "")
    variables["_source_span_id"] = str(span["id"])
    return variables, missing


def add_manual_case(
    runtime: Any, suite_id: str, variables: dict[str, Any], *, name: str
) -> AddedCase:
    """手で書いたケースを追加する(初期ケースの投入用。origin='manual')。"""
    repo = runtime.repo
    if repo.get_eval_suite(suite_id) is None:
        raise EvaluationFailed(f"評価スイートが DB にありません: {suite_id}")

    case_id = new_id()
    repo.insert_eval_case(
        case_id=case_id,
        suite_id=suite_id,
        name=name,
        vars_json=json.dumps(variables, ensure_ascii=False),
        origin=ORIGIN_MANUAL,
    )
    repo.insert_audit_log(
        event="eval.add_case",
        actor=runtime.config.system,
        subject=f"{suite_id}:{case_id}",
        detail={"origin": ORIGIN_MANUAL, "name": name},
    )
    return AddedCase(case_id=case_id, name=name, variables=variables)
