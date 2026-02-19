from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from uuid import UUID

from council_os.agents.prompts.library import PromptLibrary


class _Color(Enum):
    RED = "red"


class _Obj:
    def __str__(self) -> str:
        return "obj"


def test_prompt_library_coerce_values_json_defaults() -> None:
    lib = PromptLibrary()
    uid = UUID("12345678-1234-5678-1234-567812345678")
    dt = datetime(2026, 2, 15, 12, 30, 45, tzinfo=UTC)
    d = date(2026, 2, 15)
    path = Path("C:/tmp/file.txt")

    values = {
        "plain": "hello",
        "uuid": uid,
        "dt": dt,
        "d": d,
        "path": path,
        "enum": _Color.RED,
        "set": {"a", "b"},
        "num": 3,
        "obj": _Obj(),
    }
    coerced = lib._coerce_values(values)

    assert coerced["plain"] == "hello"
    assert json.loads(coerced["uuid"]) == str(uid)
    assert json.loads(coerced["dt"]) == dt.isoformat()
    assert json.loads(coerced["d"]) == d.isoformat()
    assert json.loads(coerced["path"]) == str(path)
    assert json.loads(coerced["enum"]) == "red"
    assert sorted(json.loads(coerced["set"])) == ["a", "b"]
    assert json.loads(coerced["num"]) == 3
    assert json.loads(coerced["obj"]) == "obj"


def test_prompt_library_render_braced_text_handles_diverse_types() -> None:
    lib = PromptLibrary()
    uid = UUID("87654321-4321-8765-4321-876543218765")
    dt = datetime(2026, 2, 15, 12, 30, 45, tzinfo=UTC)
    d = date(2026, 2, 15)
    path = Path("C:/tmp/data.txt")
    values = {
        "UUID": uid,
        "DT": dt,
        "DATE": d,
        "PATH": path,
        "ENUM": _Color.RED,
        "SET": {"x", "y"},
        "FSET": frozenset({"a", "b"}),
        "BYTES": b"blob",
        "DEC": Decimal("1.25"),
        "OBJ": _Obj(),
        "NESTED": {"key": [1, 2, {"inner": "v"}]},
    }
    template = " ".join([f"{key}={{{{{key}}}}}" for key in values.keys()])
    rendered = lib.render_braced_text(template, **values)
    assert "{{" not in rendered
