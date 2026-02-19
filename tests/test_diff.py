from __future__ import annotations

from council_os.artifacts.diff import diff_artifacts


def test_diff_artifacts_equal() -> None:
    left = {"a": 1, "b": [1, 2]}
    right = {"a": 1, "b": [1, 2]}
    result = diff_artifacts(left, right)
    assert result["equal"] is True
    assert result["changes"] == []


def test_diff_artifacts_reports_changes() -> None:
    left = {"a": 1, "b": {"x": 1}}
    right = {"a": 2, "b": {"x": 1, "y": 3}}
    result = diff_artifacts(left, right)
    assert result["equal"] is False
    assert any(change["path"] == "/a" for change in result["changes"])
    assert any(change["path"] == "/b/y" for change in result["changes"])
