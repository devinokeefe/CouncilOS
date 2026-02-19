from __future__ import annotations

import json
import os
from dataclasses import dataclass
from http.client import IncompleteRead
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from council_os.agents.providers.base import ProviderResponse, QuotaExceededError

DEFAULT_TIMEOUT_SEC = 900.0


@dataclass
class OpenRouterProvider:
    api_key: str | None = None
    base_url: str | None = None
    http_referer: str | None = None
    app_title: str | None = None
    circuit_breaker_tripped: bool = False
    circuit_breaker_reason: str | None = None

    def _offline_stub(
        self,
        model: str,
        role_config: dict[str, object],
        allowed_tools: list[str],
        message_count: int,
    ) -> ProviderResponse:
        payload: dict[str, Any] = {
            "role": str(role_config.get("role_name", "unknown")),
            "message_count": message_count,
            "seed": 0,
            "tools_allowed": allowed_tools,
        }
        return ProviderResponse(
            text=json.dumps(payload),
            metadata={
                "provider": "openrouter",
                "model": model,
                "token_usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                "offline_stub": True,
            },
        )

    def generate(
        self,
        messages: list[dict[str, str]],
        role_config: dict[str, object],
        allowed_tools: list[str],
    ) -> ProviderResponse:
        if self.circuit_breaker_tripped:
            reason = self.circuit_breaker_reason or "Quota exceeded"
            raise QuotaExceededError(reason, provider="openrouter")
        model = str(role_config.get("model_name", "openai/gpt-4o-mini"))
        if "mock" in model.lower():
            payload: dict[str, Any] = {
                "role": str(role_config.get("role_name", "unknown")),
                "message_count": len(messages),
                "seed": 0,
                "tools_allowed": allowed_tools,
            }
            return ProviderResponse(
                text=json.dumps(payload),
                metadata={
                    "provider": "openrouter",
                    "model": model,
                    "token_usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                    "offline_stub": True,
                },
            )

        api_key = self.api_key or os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError(
                "OPENROUTER_API_KEY is required for non-mock OpenRouter models. "
                "Set the environment variable and retry."
            )

        temperature = float(role_config.get("temperature", 0.0))
        max_tokens = int(role_config.get("max_tokens", 2000))
        top_p_raw = role_config.get("top_p")
        top_p = float(top_p_raw) if top_p_raw is not None else None
        response_format = role_config.get("response_format")
        timeout_raw = role_config.get("timeout_sec")
        if timeout_raw is None:
            timeout_raw = os.getenv("OPENROUTER_TIMEOUT_SEC")
        timeout_sec: float | None
        if timeout_raw is None or str(timeout_raw).strip() == "":
            timeout_sec = DEFAULT_TIMEOUT_SEC
        else:
            try:
                timeout_sec = max(1.0, float(timeout_raw))
            except (TypeError, ValueError):
                timeout_sec = DEFAULT_TIMEOUT_SEC
        base_url = (self.base_url or os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")).rstrip("/")

        req_payload: dict[str, object] = {
            "model": model,
            "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        stream = role_config.get("stream")
        if stream is None:
            stream = False
        req_payload["stream"] = bool(stream)
        if top_p is not None:
            req_payload["top_p"] = top_p
        if response_format is not None:
            req_payload["response_format"] = response_format

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        referer = (
            self.http_referer
            or os.getenv("OPENROUTER_HTTP_REFERER")
            or os.getenv("OPENROUTER_REFERER")
            or os.getenv("OPENROUTER_SITE_URL")
        )
        if referer:
            headers["HTTP-Referer"] = referer
        title = self.app_title or os.getenv("OPENROUTER_APP_TITLE") or os.getenv("OPENROUTER_TITLE")
        if title:
            headers["X-Title"] = title

        body = json.dumps(req_payload).encode("utf-8")
        request = Request(
            f"{base_url}/chat/completions",
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout_sec) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            try:
                detail_bytes = exc.read()
            except IncompleteRead as read_exc:
                detail_bytes = read_exc.partial or b""
            detail = detail_bytes.decode("utf-8", errors="replace")
            retry_without_schema = False
            provider_name = ""
            try:
                parsed_detail = json.loads(detail)
                error_obj = parsed_detail.get("error", {}) if isinstance(parsed_detail, dict) else {}
                metadata = error_obj.get("metadata", {}) if isinstance(error_obj, dict) else {}
                provider_name = str(metadata.get("provider_name", ""))
                raw_detail = metadata.get("raw", "")
                raw_text = raw_detail if isinstance(raw_detail, str) else detail
            except Exception:
                raw_text = detail

            raw_text_lc = raw_text.lower()
            if exc.code in {402, 403, 429}:
                if "key limit exceeded" in raw_text_lc or "quota" in raw_text_lc or "rate limit" in raw_text_lc:
                    reason = f"OpenRouter quota exceeded ({exc.code})"
                    self.circuit_breaker_tripped = True
                    self.circuit_breaker_reason = reason
                    raise QuotaExceededError(
                        reason,
                        status_code=exc.code,
                        provider="openrouter",
                        detail=detail,
                    ) from exc

            if response_format is not None and exc.code == 400:
                if "invalid_json_schema" in raw_text_lc:
                    retry_without_schema = True
                if "additionalproperties" in raw_text_lc:
                    retry_without_schema = True
                if "response_format" in raw_text_lc or "json_schema" in raw_text_lc:
                    retry_without_schema = True
                if provider_name.lower() == "anthropic":
                    retry_without_schema = True

            if retry_without_schema:
                req_payload.pop("response_format", None)
                body = json.dumps(req_payload).encode("utf-8")
                request = Request(
                    f"{base_url}/chat/completions",
                    data=body,
                    headers=headers,
                    method="POST",
                )
                try:
                    with urlopen(request, timeout=timeout_sec) as response:
                        raw = response.read().decode("utf-8")
                except HTTPError as retry_exc:
                    try:
                        retry_detail_bytes = retry_exc.read()
                    except IncompleteRead as read_exc:
                        retry_detail_bytes = read_exc.partial or b""
                    retry_detail = retry_detail_bytes.decode("utf-8", errors="replace")
                    raise RuntimeError(
                        f"OpenRouter request failed ({retry_exc.code}): {retry_detail}"
                    ) from retry_exc
                except IncompleteRead as retry_exc:
                    raise RuntimeError(
                        "OpenRouter response ended early while retrying without schema."
                    ) from retry_exc
            else:
                raise RuntimeError(f"OpenRouter request failed ({exc.code}): {detail}") from exc
        except IncompleteRead as exc:
            raise RuntimeError("OpenRouter response ended early while reading.") from exc
        except TimeoutError as exc:
            raise RuntimeError("OpenRouter request timed out.") from exc

        parsed = json.loads(raw)
        output_text = ""
        finish_reason = None
        stop_reason = None

        def extract_text(value: Any) -> str:
            if isinstance(value, str):
                return value
            if isinstance(value, dict):
                for key in ("text", "value", "content", "output_text", "output"):
                    if key in value:
                        extracted = extract_text(value[key])
                        if extracted:
                            return extracted
                return ""
            if isinstance(value, list):
                parts: list[str] = []
                for item in value:
                    extracted = extract_text(item)
                    if extracted:
                        parts.append(extracted)
                return "".join(parts)
            return ""

        choices = parsed.get("choices", [])
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                finish_reason = first.get("finish_reason")
                stop_reason = first.get("stop_reason")
                message = first.get("message", {})
                if isinstance(message, dict):
                    content = message.get("content")
                    output_text = extract_text(content).strip()
                    if not output_text:
                        tool_calls = message.get("tool_calls")
                        if isinstance(tool_calls, list) and tool_calls:
                            call = tool_calls[0]
                            if isinstance(call, dict):
                                func = call.get("function")
                                if isinstance(func, dict):
                                    args = func.get("arguments")
                                    output_text = extract_text(args).strip()
                    if not output_text:
                        func_call = message.get("function_call")
                        if isinstance(func_call, dict):
                            args = func_call.get("arguments")
                            output_text = extract_text(args).strip()
                if not output_text and isinstance(first.get("text"), str):
                    output_text = first["text"].strip()
        if not output_text:
            output_text = extract_text(parsed.get("output_text")).strip()
        if not output_text:
            output_text = extract_text(parsed.get("output")).strip()
        if not output_text:
            output_text = extract_text(parsed.get("content")).strip()
        if not output_text:
            raise RuntimeError("OpenRouter response did not contain output text")

        usage = parsed.get("usage", {})
        usage_dict = usage if isinstance(usage, dict) else {}
        return ProviderResponse(
            text=output_text,
            metadata={
                "provider": "openrouter",
                "model": model,
                "request_id": str(parsed.get("id", "")),
                "finish_reason": finish_reason or stop_reason,
                "stop_reason": stop_reason,
                "token_usage": {
                    "prompt_tokens": int(usage_dict.get("prompt_tokens", usage_dict.get("input_tokens", 0))),
                    "completion_tokens": int(usage_dict.get("completion_tokens", usage_dict.get("output_tokens", 0))),
                    "total_tokens": int(usage_dict.get("total_tokens", 0)),
                },
            },
        )
