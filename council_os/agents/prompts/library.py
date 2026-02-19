from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID


class PromptLibrary:
    _brace_pattern = re.compile(r"{{\s*([A-Z0-9_]+)\s*}}")

    def __init__(self, root: Path | None = None, default_pack: str | None = None) -> None:
        self.root = root or Path(__file__).parent
        self._cache: dict[str, str] = {}
        self._pack_cache: dict[str, dict[str, str]] = {}
        self._default_pack = default_pack or "prompt_pack_vfinal_1.json"

    def load(self, name: str) -> str:
        if name in self._cache:
            return self._cache[name]
        path = self.root / name
        if not path.exists():
            raise FileNotFoundError(f"Prompt template not found: {path}")
        text = path.read_text(encoding="utf-8")
        self._cache[name] = text
        return text

    def load_pack(self, pack_name: str | None = None) -> dict[str, str]:
        pack_file = pack_name or self._default_pack
        if pack_file in self._pack_cache:
            return self._pack_cache[pack_file]
        path = self.root / pack_file
        if not path.exists():
            raise FileNotFoundError(f"Prompt pack not found: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        prompts = data.get("prompts")
        if not isinstance(prompts, dict):
            raise ValueError(f"Prompt pack missing prompts map: {path}")
        mapping = {str(key): str(value) for key, value in prompts.items()}
        self._pack_cache[pack_file] = mapping
        return mapping

    def pack_hash(self, pack_name: str | None = None) -> str:
        pack_file = pack_name or self._default_pack
        path = self.root / pack_file
        if not path.exists():
            return ""
        raw = path.read_text(encoding="utf-8")
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def render(self, name: str, **kwargs: Any) -> str:
        return self.load(name).format(**kwargs)

    def render_braced(self, name: str, **kwargs: Any) -> str:
        return self.render_braced_text(self.load(name), **kwargs)

    def render_braced_text(self, text: str, **kwargs: Any) -> str:
        mapping = self._coerce_values(kwargs)
        missing: set[str] = set()

        def replace(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in mapping:
                missing.add(key)
                return match.group(0)
            return mapping[key]

        rendered = self._brace_pattern.sub(replace, text)
        if missing:
            missing_list = ", ".join(sorted(missing))
            raise KeyError(f"Missing prompt variables: {missing_list}")
        return rendered

    def render_label(self, label: str, pack: str | None = None, **kwargs: Any) -> str:
        prompts = self.load_pack(pack)
        if label not in prompts:
            raise KeyError(f"Prompt label not found in pack: {label}")
        mapping = dict(kwargs)
        if "GLOBAL_CONTRACT_HEADER_V4" not in mapping:
            try:
                mapping["GLOBAL_CONTRACT_HEADER_V4"] = self.load("global_contract_header_v4.txt")
            except FileNotFoundError:
                pass
        filename = prompts[label]
        return self.render_braced_text(self.load(filename), **mapping)

    def task(self, name: str) -> str:
        return self.load(f"task_{name}_v1.txt")

    def _coerce_values(self, values: dict[str, Any]) -> dict[str, str]:
        coerced: dict[str, str] = {}
        for key, value in values.items():
            if isinstance(value, str):
                coerced[key] = value
            else:
                coerced[key] = json.dumps(value, ensure_ascii=False, default=self._json_default)
        return coerced

    @staticmethod
    def _json_default(value: Any) -> object:
        if isinstance(value, UUID):
            return str(value)
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, (set, frozenset)):
            return list(value)
        return str(value)
