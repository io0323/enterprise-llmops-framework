"""既存 Harness 互換の SDK Adapter(optional extra `sdk-anthropic`)。

**有料APIの導入ではない**(CLAUDE.md 絶対ルール2)。Harness が既に使っている経路を
ELF から記録・制限できるようにするためだけに存在する。ELF の既定 `models.yaml` は
`claude_cli` と `mock` のみを使う。

未インストール環境では、モジュールのロード時ではなく **解決時(invoke / health)** に
`AdapterUnavailable` を送出する。import に失敗すると ELF 全体が壊れるため。

実モデル名はコードにも設定にも書かない。`params.model_env` が指す環境変数から取る
(絶対ルール13 / APAP の Vendor Neutral 規約)。
"""

from __future__ import annotations

import os
from typing import Any

from llmops.adapters.base import AdapterRequest, AdapterResponse, ProviderAdapter
from llmops.errors import AdapterError, AdapterUnavailable

DEFAULT_MAX_TOKENS = 4096


def _load_sdk() -> Any:
    """SDK を遅延 import する。未インストールなら `AdapterUnavailable`。"""
    try:
        import anthropic  # noqa: PLC0415 - 遅延 import が本 Adapter の要件
    except ImportError as exc:
        raise AdapterUnavailable(
            "optional extra `sdk-anthropic` が未インストールのため解決できません "
            '(pip install -e ".[sdk-anthropic]")'
        ) from exc
    return anthropic


class AnthropicSdkAdapter(ProviderAdapter):
    name = "anthropic_sdk"

    def __init__(self, client: Any | None = None) -> None:
        """`client` は利用システムが構成済みの SDK クライアント(任意)。

        Harness は API キーを自前の Settings(.env)から渡して SDK クライアントを作っている。
        その構成をそのまま使えるように受け取る(ELF が API キーの在り処を知らずに済む)。
        渡されなければ呼び出し時に SDK の既定(環境変数)で生成する。
        """
        self._client = client

    def invoke(self, req: AdapterRequest) -> AdapterResponse:
        # SDK の有無を先に見る(未インストールを「環境変数が無い」と取り違えさせない)
        client = self._client if self._client is not None else _load_sdk().Anthropic()
        model = self._model(req.params)
        max_tokens = int(req.params.get("max_tokens", DEFAULT_MAX_TOKENS))

        try:
            message = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": req.text}],
            )
        except Exception as exc:  # noqa: BLE001 - SDK の例外階層に依存しない
            raise AdapterError(f"SDK 呼び出しに失敗しました: {exc}") from exc

        raw = self._to_dict(message)
        usage: dict[str, Any] = raw.get("usage") or {}
        return AdapterResponse(
            text=self._text_of(raw),
            raw=raw,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            cache_read_tokens=usage.get("cache_read_input_tokens"),
            cache_write_tokens=usage.get("cache_creation_input_tokens"),
            # 単価は models.yaml / 環境変数から Cost 側が当てる。Adapter は金額を知らない
            cost_usd=None,
            resolved_target=self._target_label(req.params),
        )

    @staticmethod
    def _model(params: dict[str, Any]) -> str:
        """実モデル名を環境変数から解決する(絶対ルール13)。"""
        env_name = params.get("model_env")
        if not env_name:
            raise AdapterUnavailable(
                "params.model_env が未設定です(実モデル名は環境変数から解決する)"
            )
        model = os.environ.get(str(env_name))
        if not model:
            raise AdapterUnavailable(f"環境変数 {env_name} が未設定のため解決できません")
        return model

    @staticmethod
    def _target_label(params: dict[str, Any]) -> str:
        """span に残す識別子。実モデル名は残さず、どの環境変数で解決したかを残す。"""
        return f"anthropic_sdk:${params.get('model_env')}"

    @staticmethod
    def _to_dict(message: Any) -> dict[str, Any]:
        for attr in ("model_dump", "to_dict", "dict"):
            method = getattr(message, attr, None)
            if callable(method):
                result = method()
                if isinstance(result, dict):
                    return result
        plain = _plain(message)
        return plain if isinstance(plain, dict) else {"repr": repr(message)}

    @staticmethod
    def _text_of(raw: dict[str, Any]) -> str:
        blocks = raw.get("content") or []
        parts = [
            str(block.get("text", ""))
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "".join(parts)

    def health(self) -> bool:
        if self._client is not None:
            return True
        try:
            _load_sdk()
        except AdapterUnavailable:
            return False
        return True


def _plain(value: Any) -> Any:
    """属性アクセス型の応答(SDK の型を持たないオブジェクト)を dict / list に落とす。

    SDK のバージョンによっては `model_dump` を持たない応答型がありうる。
    その場合でも `content` / `usage` を取り出せるようにする(応答形の差で落とさない)。
    """
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    attrs = getattr(value, "__dict__", None)
    if isinstance(attrs, dict):
        return {str(k): _plain(v) for k, v in attrs.items() if not str(k).startswith("_")}
    return repr(value)
