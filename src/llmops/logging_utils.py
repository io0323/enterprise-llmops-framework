"""trace_id / span_id 付きの構造化ログ(CLAUDE.md コーディング規約)。

DDE / CGMP の `logging_utils.py` と同じ方針。違いは注入するコンテキストが
run_id / request_id ではなく trace_id / span_id であること。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)s [trace=%(trace_id)s span=%(span_id)s] %(name)s: %(message)s"


class ContextFilter(logging.Filter):
    """レコードに trace_id / span_id を注入する。未設定時は ``-`` を入れる。"""

    def __init__(self, trace_id: str = "-", span_id: str = "-") -> None:
        super().__init__()
        self.trace_id = trace_id
        self.span_id = span_id

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "trace_id"):
            record.trace_id = self.trace_id
        if not hasattr(record, "span_id"):
            record.span_id = self.span_id
        return True


def setup_logging(
    trace_id: str = "-",
    span_id: str = "-",
    level: int | str = logging.INFO,
    log_file: Path | str | None = None,
) -> None:
    """標準 logging を構造化フォーマットで初期化する(冪等)。

    `log_file` を渡すとファイルにも出す。書けない場合でも標準エラーへの出力は維持する
    (ログが書けないことでアプリを止めない)。
    """
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    context = ContextFilter(trace_id, span_id)
    formatter = logging.Formatter(_FORMAT)

    stream = logging.StreamHandler(stream=sys.stderr)
    stream.setFormatter(formatter)
    stream.addFilter(context)
    root.addHandler(stream)

    if log_file is None:
        return
    try:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
    except OSError as exc:  # pragma: no cover - 環境依存
        root.warning("ログファイルを開けませんでした(標準エラーのみ継続): %s", exc)
        return
    file_handler.setFormatter(formatter)
    file_handler.addFilter(context)
    root.addHandler(file_handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
