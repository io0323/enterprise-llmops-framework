"""回帰判定(このPhaseの成果物)。

verdict の決め方は**「迷ったら止める」**で統一する(NOTES.md N-026)。
黙って通すより、fail か「スコア欠損」として残す。

- `fail`         : 決定的評価の合格率不足 / degraded 混入 / スコアが出せない / 下限割れ
- `regressed`    : ベースライン比で `regression_tolerance` を超えて低下した
- `wiring_check` : mock を経由した実行。**品質判定をしていない**(pass でも fail でもない)
- `pass`         : 上記のいずれでもない

`wiring_check` を `pass` と別の値にしているのは、`fail` と書くと「品質が悪い」と
誤読されるため。実態は「判定していない」。publish ゲートは `pass` のみを通すので、
どちらにしても公開の根拠にはならない。
"""

from __future__ import annotations

from dataclasses import dataclass

from llmops.eval.suite import Thresholds

PASS = "pass"
FAIL = "fail"
REGRESSED = "regressed"
#: 配線確認(mock 経由)の実行。**品質判定をしていない**ので pass でも fail でもない。
#: publish ゲートは verdict == 'pass' のみを通すので、これが根拠になることはない
WIRING_CHECK = "wiring_check"

#: mock を通った run。品質の判定には使わない(配線確認)
MODE_WIRING_CHECK = "wiring_check"
MODE_EVALUATION = "evaluation"


@dataclass(frozen=True)
class Judgement:
    verdict: str
    reason: str
    baseline_run_id: str | None = None
    baseline_score: float | None = None

    @property
    def ok(self) -> bool:
        return self.verdict == PASS


def judge_run(
    *,
    score: float | None,
    deterministic_pass_rate: float,
    thresholds: Thresholds,
    degraded_spans: int,
    fail_on_degraded: bool,
    baseline_score: float | None = None,
    baseline_run_id: str | None = None,
    mode: str = MODE_EVALUATION,
) -> Judgement:
    """1 run の verdict を決める。

    判定の順序に意味がある。**止める理由を先に見る。**
    """
    # 1. 縮退実行の結果が混ざっていたら、中身を見ずに fail
    #    (mock が「それらしい偽の結果」を返してスコアが付く事故の再発防止。N-026)
    if fail_on_degraded and degraded_spans > 0:
        return Judgement(
            FAIL,
            f"縮退実行(Fallback)の span が {degraded_spans} 件混ざっています。"
            "その結果で品質は判定しません",
        )

    # 2. 決定的評価は全件合格が既定(スイートで下げられる)
    if deterministic_pass_rate < thresholds.deterministic_pass_rate:
        return Judgement(
            FAIL,
            f"決定的評価の合格率 {deterministic_pass_rate:.2%} が"
            f" 閾値 {thresholds.deterministic_pass_rate:.2%} を下回りました",
        )

    # 3. スコアが出せなかった(Judge が全件失敗など)。合格にはしない
    if score is None:
        return Judgement(
            FAIL, "スコアを算出できませんでした(採点できたケースがありません)"
        )

    # 4. 配線確認モードは合否を出さない。baseline にも publish の根拠にもしない。
    #    ここまでの 1-3(degraded / 決定的評価 / スコア欠損)は配線の異常なので先に見る
    if mode == MODE_WIRING_CHECK:
        return Judgement(
            WIRING_CHECK,
            "配線確認モード(mock Adapter を経由)。配線は動いていますが、"
            "品質の判定はしていません(baseline にも publish の根拠にもなりません)",
        )

    # 5. ベースラインがある場合は劣化を見る
    if baseline_score is not None:
        drop = baseline_score - score
        if drop > thresholds.regression_tolerance:
            return Judgement(
                REGRESSED,
                f"ベースライン {baseline_score:.4f} から {drop:.4f} 低下しました"
                f"(許容 {thresholds.regression_tolerance:.4f})",
                baseline_run_id,
                baseline_score,
            )

    # 6. 下限割れ
    if score < thresholds.min_score:
        return Judgement(
            FAIL,
            f"スコア {score:.4f} が下限 {thresholds.min_score:.4f} を下回りました",
            baseline_run_id,
            baseline_score,
        )

    if baseline_score is None:
        return Judgement(
            PASS, f"初版(ベースラインなし)。下限 {thresholds.min_score:.4f} を満たしました"
        )
    return Judgement(
        PASS,
        f"ベースライン {baseline_score:.4f} 比で許容範囲内です",
        baseline_run_id,
        baseline_score,
    )
