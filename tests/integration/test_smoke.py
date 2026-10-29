"""Smoke tests for Gmail MCP.

The bulk of this file runs as plain unit checks (no network, no credentials).
The single tool round-trip at the bottom is marked `integration` and only
runs when GMAIL_TEST_REAL=1 — it hits real Gmail.
"""

from __future__ import annotations

import os

import pytest

from gmail_mcp import __version__, auth, tools
from gmail_mcp import config as config_module

PUBLIC_TOOLS = (
    "list_emails",
    "get_thread_messages",
    "read_email",
    "send_email",
    "reply_email",
    "archive_email",
    "trash_email",
    "mark_read",
    "add_label",
    "remove_label",
    "create_draft",
    "list_drafts",
    "update_draft",
    "delete_draft",
    "list_attachments",
    "download_attachment",
    "read_emails",
    "mark_emails_read",
    "archive_emails",
    "trash_emails",
    "add_labels",
    "download_attachments",
    "read_emails_full",
    "search_and_save",
    "append_emails",
)


def test_package_version_is_set() -> None:
    assert __version__ and isinstance(__version__, str)


def test_config_defaults() -> None:
    cfg = config_module.config
    assert cfg.token_storage == "filesystem"
    assert cfg.scopes == ["https://mail.google.com/"]
    assert cfg.service_name == "gmail-mcp"


def test_service_cache_starts_empty() -> None:
    assert auth._service_cache is None


@pytest.mark.parametrize("name", PUBLIC_TOOLS)
def test_tool_is_exported(name: str) -> None:
    assert hasattr(tools, name), f"tools.{name} missing"


def test_all_tools_count() -> None:
    assert len(PUBLIC_TOOLS) == 25


def test_response_helpers() -> None:
    assert tools.ok({"x": 1}) == {"ok": True, "data": {"x": 1}}
    assert tools.err("code", "msg") == {"ok": False, "error": "code", "message": "msg"}


def test_error_codes_defined() -> None:
    for name in (
        "ERROR_AUTH_MISSING",
        "ERROR_AUTH_EXPIRED",
        "ERROR_API",
        "ERROR_NOT_FOUND",
        "ERROR_VALIDATION",
        "ERROR_ATTACHMENT_NOT_FOUND",
        "ERROR_FILE_NOT_FOUND",
        "ERROR_DRAFT_NOT_FOUND",
        "ERROR_INVALID_LABEL",
        "ERROR_THREAD_NOT_FOUND",
        "ERROR_INTERNAL",
    ):
        assert hasattr(tools, name), f"{name} not defined"


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("GMAIL_TEST_REAL") != "1",
    reason="requires GMAIL_TEST_REAL=1 and valid credentials",
)
def test_list_emails_round_trip() -> None:
    """Hit the real Gmail API. Requires `credentials.json` + completed OAuth flow."""
    result = tools.list_emails(query="newer_than:1d", max_results=1)
    assert result["ok"] is True, result
    assert "emails" in result["data"]
