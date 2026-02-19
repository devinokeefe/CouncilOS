from __future__ import annotations


def build_repair_messages(
    original_messages: list[dict[str, str]],
    invalid_text: str,
    validation_error: str,
    truncated: bool = False,
) -> list[dict[str, str]]:
    snippet = _truncate_text(invalid_text, limit=4000)
    original_context = _truncate_text(
        "\n\n".join(f"{m['role']}: {m['content']}" for m in original_messages),
        limit=6000,
    )
    truncation_note = ""
    if truncated:
        truncation_note = (
            "The previous output appears truncated. Regenerate the full JSON from scratch, "
            "keeping field values concise.\n\n"
        )
    return [
        {"role": "system", "content": "Output JSON only. Do not wrap in markdown fences."},
        {
            "role": "user",
            "content": (
                f"{truncation_note}"
                "Repair this JSON so it validates against the requested schema.\n\n"
                f"Validation error:\n{validation_error}\n\n"
                f"Original task context:\n{original_context}\n\n"
                f"Invalid JSON to repair:\n{snippet}"
            ),
        },
    ]


def _truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2 :]
    return f"{head}\n...TRUNCATED...\n{tail}"
