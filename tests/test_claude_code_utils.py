from __future__ import annotations

from legoflow_curator.create import claude_code_utils


def test_get_claude_permission_mode_avoids_bypass_permissions_as_root(monkeypatch) -> None:
    monkeypatch.setattr(claude_code_utils.os, "geteuid", lambda: 0)

    assert claude_code_utils.get_claude_permission_mode() == "acceptEdits"


def test_get_claude_permission_mode_preserves_non_root_auto_approval(monkeypatch) -> None:
    monkeypatch.setattr(claude_code_utils.os, "geteuid", lambda: 1000)

    assert claude_code_utils.get_claude_permission_mode() == "bypassPermissions"
