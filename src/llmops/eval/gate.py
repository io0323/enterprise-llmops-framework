"""publish の評価ゲート(Step 2-4。このPhaseの成果物)。

Phase 1 で `PromptRegistry` に作った差し込み口(`eval_gate`)の中身。

publish が成功する条件(実装指示 Step 2-4):

1. 状態が `approved`(Registry 側で判定済み)
2. 対象版に対する `eval_run` が存在する
3. その run の `prompt_version` が publish 対象版と一致する(古い評価を使い回させない)
4. `verdict == 'pass'`

さらに N-026 の再発防止として:

5. その run が `mode='evaluation'` であること。配線確認(mock 経由)は根拠にしない
6. その run に degraded span が無いこと(縮退実行の結果で公開判断をしない)

条件を満たさなければ `EvaluationFailed` を送出する。CLI は非ゼロ終了する。
"""

from __future__ import annotations

from typing import Any

from llmops.errors import EvaluationFailed
from llmops.eval.regression import MODE_EVALUATION, PASS
from llmops.logging_utils import get_logger

logger = get_logger(__name__)


class PublishGate:
    """`PromptRegistry(eval_gate=...)` に渡す呼び出し可能オブジェクト。"""

    def __init__(self, repo: Any) -> None:
        self.repo = repo

    def __call__(self, prompt_id: str, version: int) -> str | None:
        run = self.repo.latest_eval_run(prompt_id, version, mode=MODE_EVALUATION)
        if run is None:
            raise EvaluationFailed(
                f"{prompt_id}@{version}: この版に対する評価実行がありません。"
                f"`llmops eval run <suite>` を実行してから publish してください"
                "(配線確認モードの実行は根拠になりません)"
            )

        run_id = str(run["id"])
        if int(run["prompt_version"]) != int(version):
            # latest_eval_run が版で絞っているので通常は起きない。取り違えの保険
            raise EvaluationFailed(
                f"{prompt_id}@{version}: 評価実行 {run_id} の対象版は"
                f" {run['prompt_version']} です。古い評価は使えません"
            )

        if str(run["verdict"]) != PASS:
            raise EvaluationFailed(
                f"{prompt_id}@{version}: 直近の評価実行 {run_id} の verdict は"
                f" '{run['verdict']}' です(理由: {run['note'] or '記録なし'})。"
                " publish できません"
            )

        if int(run["degraded_spans"] or 0) > 0:
            raise EvaluationFailed(
                f"{prompt_id}@{version}: 評価実行 {run_id} に縮退実行(Fallback)の span が"
                f" {run['degraded_spans']} 件含まれています。その結果では公開を判断しません"
            )

        logger.info("評価ゲート通過: %s@%s (run=%s)", prompt_id, version, run_id)
        return run_id
