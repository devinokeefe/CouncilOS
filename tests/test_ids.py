from __future__ import annotations

import pytest

from council_os.artifacts.ids import validate_namespaced_id


def test_validate_namespaced_id_valid() -> None:
    validate_namespaced_id("R1", "R")
    validate_namespaced_id("AT22", "AT")


def test_validate_namespaced_id_invalid() -> None:
    with pytest.raises(ValueError):
        validate_namespaced_id("REQ1", "R")
