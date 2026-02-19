from __future__ import annotations

import json
import hashlib
from typing import Any, Type

from pydantic import BaseModel


def _prompt_input_hash(payload: dict[str, object]) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_json_schema_messages(
    output_model: Type[BaseModel],
    *,
    task: str,
    context: str,
) -> tuple[list[dict[str, str]], dict[str, object]]:
    schema = json.dumps(output_model.model_json_schema(), indent=2)
    prompt_meta: dict[str, object] = {
        "task": task,
        "context": context,
        "schema_json": schema,
    }
    prompt_meta["prompt_input_hash"] = _prompt_input_hash(prompt_meta)
    system_text = (
        "You produce strict JSON that must validate against the provided JSON Schema. "
        "No markdown, no explanations."
    )
    user_text = (
        f"Task:\n{task}\n\n"
        f"Context:\n{context}\n\n"
        f"Output JSON Schema:\n{schema}\n\n"
        "Return only one JSON object that satisfies this schema."
    )
    return (
        [
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_text},
        ],
        prompt_meta,
    )
