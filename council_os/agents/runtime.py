from __future__ import annotations

import ast
import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar
from uuid import UUID, uuid4

from pydantic import BaseModel, ValidationError

from council_os.agents.providers.base import Provider, QuotaExceededError
from council_os.agents.repair import build_repair_messages
from council_os.agents.roles import RoleConfig
from council_os.audit.event_log import append_event, new_event

T = TypeVar("T", bound=BaseModel)
_UNSET = object()


@dataclass(frozen=True)
class InvocationResult:
    parsed: BaseModel
    raw_text: str
    retries: int


class RuntimeErrorException(Exception):
    pass


class AgentRuntime:
    def __init__(
        self,
        provider: Provider,
        run_root: Path,
        run_id: UUID,
        store_full_prompts: bool = False,
        log_model_metadata: bool = True,
        log_raw_model_text: bool = False,
    ) -> None:
        self.provider = provider
        self.run_root = run_root
        self.run_id = run_id
        self.store_full_prompts = store_full_prompts
        self.log_model_metadata = log_model_metadata
        self.log_raw_model_text = log_raw_model_text

    def invoke_role(
        self,
        role: RoleConfig,
        messages: list[dict[str, str]],
        output_model: type[T],
        stage: str = "intake",
        prompt_metadata: dict[str, object] | None = None,
        *,
        max_attempts: int | None = None,
        max_repair_attempts: int | None = None,
        max_token_bumps: int | None = None,
        max_tokens_override: int | None = None,
        response_format_override: dict[str, object] | None | object = _UNSET,
        strict_json: bool = False,
        hard_fail_on_non_json: bool = False,
        extra_params: dict[str, object] | None = None,
    ) -> InvocationResult:
        request_id = str(uuid4())
        request_payload: dict[str, object] = {
            "model": role.model_name,
            "provider": role.model_provider,
            "params": {
                "temperature": role.temperature,
                "max_tokens": role.max_tokens,
                "prompt_version_hash": role.prompt_version_hash,
                "top_p": role.top_p,
            },
            "request_id": request_id,
            "tools_allowed": role.tools_allowed,
            "output_schema": output_model.__name__,
            "role_output_schema": role.output_schema,
        }
        if prompt_metadata is not None:
            request_payload["prompt_metadata"] = prompt_metadata
        if self.store_full_prompts:
            request_payload["messages"] = messages
        request_hash = hashlib.sha256(json.dumps(messages, sort_keys=True).encode()).hexdigest()
        append_event(
            self.run_root,
            new_event(
                self.run_id,
                stage=stage,
                actor={"kind": "llm", "role": role.role_name},
                event_type="llm_request",
                payload={"request": request_payload, "prompt_hash": request_hash},
            ),
        )

        attempt = 0
        token_bumps = 0
        repair_attempts = 0
        max_attempts = 5 if max_attempts is None else max_attempts
        max_token_bumps = 2 if max_token_bumps is None else max_token_bumps
        max_repair_attempts = 2 if max_repair_attempts is None else max_repair_attempts
        if output_model.__name__ == "PlanPackage":
            max_attempts = 1
            max_token_bumps = 0
            max_repair_attempts = 0
        current_messages = messages
        current_max_tokens = role.max_tokens if max_tokens_override is None else max_tokens_override
        response_format: dict[str, object] | None
        if response_format_override is not _UNSET:
            response_format = response_format_override  # type: ignore[assignment]
        else:
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": output_model.__name__,
                    "strict": True,
                    "schema": _strict_json_schema(output_model.model_json_schema()),
                },
            }
            if role.model_provider.lower() == "openrouter" and role.model_name.lower().startswith("anthropic/"):
                response_format = None
        while attempt < max_attempts:
            start = time.perf_counter()
            role_config: dict[str, object] = {
                "role_name": role.role_name,
                "model_name": role.model_name,
                "temperature": role.temperature,
                "max_tokens": current_max_tokens,
                "top_p": role.top_p,
                "response_format": response_format,
            }
            if extra_params:
                role_config.update(extra_params)
            try:
                response = self.provider.generate(
                    current_messages,
                    role_config=role_config,
                    allowed_tools=role.tools_allowed,
                )
            except QuotaExceededError as exc:
                elapsed_ms = round((time.perf_counter() - start) * 1000.0, 3)
                exc.stage = stage
                exc.role = role.role_name
                exc.request_id = request_id
                response_payload = {
                    "request_id": request_id,
                    "raw_output_hash": "",
                    "latency_ms": elapsed_ms,
                    "provider_request_id": "",
                    "token_usage": {},
                    "parse_valid": False,
                    "validation_error": str(exc),
                    "quota_exceeded": True,
                    "status_code": exc.status_code,
                }
                if not self.log_model_metadata:
                    response_payload["provider_request_id"] = ""
                    response_payload["token_usage"] = {}
                append_event(
                    self.run_root,
                    new_event(
                        self.run_id,
                        stage=stage,
                        actor={"kind": "llm", "role": role.role_name},
                        event_type="llm_response",
                        payload=response_payload,
                    ),
                )
                append_event(
                    self.run_root,
                    new_event(
                        self.run_id,
                        stage=stage,
                        actor={"kind": "llm", "role": role.role_name},
                        event_type="decision",
                        payload={
                            "request_id": request_id,
                            "parse_valid": False,
                            "validation_error": str(exc),
                            "quota_exceeded": True,
                            "status_code": exc.status_code,
                        },
                    ),
                )
                raise
            except Exception as exc:
                elapsed_ms = round((time.perf_counter() - start) * 1000.0, 3)
                response_payload = {
                    "request_id": request_id,
                    "raw_output_hash": "",
                    "latency_ms": elapsed_ms,
                    "provider_request_id": "",
                    "token_usage": {},
                    "parse_valid": False,
                    "validation_error": str(exc),
                }
                if not self.log_model_metadata:
                    response_payload["provider_request_id"] = ""
                    response_payload["token_usage"] = {}
                append_event(
                    self.run_root,
                    new_event(
                        self.run_id,
                        stage=stage,
                        actor={"kind": "llm", "role": role.role_name},
                        event_type="llm_response",
                        payload=response_payload,
                    ),
                )
                append_event(
                    self.run_root,
                    new_event(
                        self.run_id,
                        stage=stage,
                        actor={"kind": "llm", "role": role.role_name},
                        event_type="decision",
                        payload={"request_id": request_id, "parse_valid": False, "validation_error": str(exc)},
                    ),
                )
                if attempt >= max_attempts - 1:
                    raise RuntimeErrorException(f"Provider error: {exc}") from exc
                attempt += 1
                continue
            if response.metadata.get("offline_stub"):
                provider = response.metadata.get("provider", role.model_provider)
                model = response.metadata.get("model", role.model_name)
                raise RuntimeErrorException(
                    f"Provider returned offline stub for {provider}:{model}. "
                    "Missing credentials or running in offline mode."
                )
            elapsed_ms = round((time.perf_counter() - start) * 1000.0, 3)
            output_hash = hashlib.sha256(response.text.encode()).hexdigest()
            finish_reason = str(response.metadata.get("finish_reason") or "")
            truncated_by_reason = _finish_reason_truncated(finish_reason)
            try:
                if strict_json or hard_fail_on_non_json:
                    parsed_json = json.loads(response.text)
                else:
                    parsed_json = _safe_json_loads(response.text)
                parsed = output_model.model_validate(parsed_json)
                response_payload = {
                    "request_id": request_id,
                    "raw_output_hash": output_hash,
                    "latency_ms": elapsed_ms,
                    "provider_request_id": response.metadata.get("request_id", ""),
                    "token_usage": response.metadata.get("token_usage", {}),
                    "parse_valid": True,
                    "validation_error": "",
                }
                if not self.log_model_metadata:
                    response_payload["provider_request_id"] = ""
                    response_payload["token_usage"] = {}
                if self.log_raw_model_text:
                    response_payload["raw_output_text"] = response.text
                append_event(
                    self.run_root,
                    new_event(
                        self.run_id,
                        stage=stage,
                        actor={"kind": "llm", "role": role.role_name},
                        event_type="llm_response",
                        payload=response_payload,
                    ),
                )
                append_event(
                    self.run_root,
                    new_event(
                        self.run_id,
                        stage=stage,
                        actor={"kind": "llm", "role": role.role_name},
                        event_type="decision",
                        payload={"request_id": request_id, "parse_valid": True},
                    ),
                )
                return InvocationResult(parsed=parsed, raw_text=response.text, retries=attempt)
            except (json.JSONDecodeError, ValidationError) as exc:
                if isinstance(exc, json.JSONDecodeError) and hard_fail_on_non_json:
                    raise RuntimeErrorException(f"Non-JSON output with hard_fail_on_non_json: {exc}") from exc
                validation_error = str(exc)
                truncated = truncated_by_reason or _looks_truncated_error(validation_error)
                response_payload = {
                    "request_id": request_id,
                    "raw_output_hash": output_hash,
                    "latency_ms": elapsed_ms,
                    "provider_request_id": response.metadata.get("request_id", ""),
                    "token_usage": response.metadata.get("token_usage", {}),
                    "parse_valid": False,
                    "validation_error": validation_error,
                }
                if not self.log_model_metadata:
                    response_payload["provider_request_id"] = ""
                    response_payload["token_usage"] = {}
                if self.log_raw_model_text:
                    response_payload["raw_output_text"] = response.text
                append_event(
                    self.run_root,
                    new_event(
                        self.run_id,
                        stage=stage,
                        actor={"kind": "llm", "role": role.role_name},
                        event_type="llm_response",
                        payload=response_payload,
                    ),
                )
                append_event(
                    self.run_root,
                    new_event(
                        self.run_id,
                        stage=stage,
                        actor={"kind": "llm", "role": role.role_name},
                        event_type="decision",
                        payload={"request_id": request_id, "parse_valid": False, "validation_error": validation_error},
                    ),
                )
                if isinstance(exc, json.JSONDecodeError) and truncated and token_bumps < max_token_bumps:
                    current_max_tokens = min(int(current_max_tokens * 1.5), int(role.max_tokens * 4))
                    token_bumps += 1
                    attempt += 1
                    continue
                if repair_attempts >= max_repair_attempts or attempt >= max_attempts - 1:
                    raise RuntimeErrorException(f"Schema repair exhausted: {exc}") from exc
                current_messages = build_repair_messages(
                    messages,
                    response.text,
                    str(exc),
                    truncated=truncated,
                )
                repair_attempts += 1
                attempt += 1

        raise RuntimeErrorException("Unreachable runtime error")


def _strict_json_schema(schema: dict[str, object]) -> dict[str, object]:
    # OpenAI/compat providers require "required" to include all properties when strict=true.
    normalized = json.loads(json.dumps(schema))

    def walk(node: object) -> None:
        if isinstance(node, dict):
            properties = node.get("properties")
            if isinstance(properties, dict):
                node["required"] = list(properties.keys())
                if "additionalProperties" not in node:
                    node["additionalProperties"] = False
                for child in properties.values():
                    walk(child)
            if node.get("additionalProperties") is True:
                node["additionalProperties"] = False
            for key in ("items", "additionalProperties"):
                if key in node:
                    walk(node[key])
            for key in ("anyOf", "oneOf", "allOf"):
                value = node.get(key)
                if isinstance(value, list):
                    for child in value:
                        walk(child)
            defs = node.get("$defs")
            if isinstance(defs, dict):
                for child in defs.values():
                    walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(normalized)
    return normalized


def _safe_json_loads(text: str) -> dict[str, object] | list[object]:
    last_exc: json.JSONDecodeError | None = None
    stripped = _strip_code_fences(text)
    extracted: str | None
    try:
        extracted = _extract_first_json(stripped)
    except json.JSONDecodeError:
        extracted = None

    candidates = [text, stripped]
    if extracted and extracted not in candidates:
        candidates.append(extracted)

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_exc = exc

    if extracted is None:
        extracted = stripped

    parsed = _try_literal_eval(extracted)
    if parsed is not None:
        return parsed

    repaired = _repair_json_like(extracted)
    try:
        return json.loads(repaired)
    except json.JSONDecodeError as exc:
        last_exc = exc

    parsed = _try_literal_eval(repaired)
    if parsed is not None:
        return parsed

    if last_exc is not None:
        raise last_exc
    raise json.JSONDecodeError("No JSON object found", text, 0)


def _strip_code_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return stripped


def _extract_first_json(text: str) -> str:
    start = None
    for idx, ch in enumerate(text):
        if ch in "{[":
            start = idx
            break
    if start is None:
        raise json.JSONDecodeError("No JSON object found", text, 0)
    stack: list[str] = []
    in_string = False
    escape = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == "\"":
                in_string = False
            continue
        if ch == "\"":
            in_string = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if not stack:
                continue
            stack.pop()
            if not stack:
                return text[start : idx + 1]
    return text[start:]


def _try_literal_eval(text: str) -> dict[str, object] | list[object] | None:
    try:
        value = ast.literal_eval(text)
    except (SyntaxError, ValueError):
        return None
    if isinstance(value, (dict, list)):
        return value
    return None


def _repair_json_like(text: str) -> str:
    repaired = text.strip()
    repaired = _strip_code_fences(repaired)
    repaired = _remove_trailing_commas(repaired)
    repaired = _quote_unquoted_keys(repaired)
    return repaired


def _remove_trailing_commas(text: str) -> str:
    return re.sub(r",\\s*([}\\]])", r"\\1", text)


def _quote_unquoted_keys(text: str) -> str:
    result: list[str] = []
    in_string = False
    string_char = ""
    escape = False
    i = 0
    while i < len(text):
        ch = text[i]
        if in_string:
            result.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == string_char:
                in_string = False
            i += 1
            continue

        if ch in ("\"", "'"):
            in_string = True
            string_char = ch
            result.append(ch)
            i += 1
            continue

        if ch.isalpha() or ch == "_":
            start = i
            j = i + 1
            while j < len(text) and (text[j].isalnum() or text[j] == "_"):
                j += 1
            k = j
            while k < len(text) and text[k].isspace():
                k += 1
            if k < len(text) and text[k] == ":":
                p = len(result) - 1
                while p >= 0 and result[p].isspace():
                    p -= 1
                if p >= 0 and result[p] in "{,":
                    key = text[start:j]
                    result.append(f"\"{key}\"")
                    i = j
                    continue
            result.append(ch)
            i += 1
            continue

        result.append(ch)
        i += 1

    return "".join(result)


def _looks_truncated_error(error: str) -> bool:
    lowered = error.lower()
    return "unterminated string" in lowered or "expecting value" in lowered or "unexpected eof" in lowered


def _finish_reason_truncated(reason: str) -> bool:
    lowered = reason.lower()
    return lowered in {"length", "max_tokens", "token_limit"} or "max_tokens" in lowered
