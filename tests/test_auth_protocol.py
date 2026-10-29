"""Credential isolation and a real, offline MCP stdio round trip."""

import asyncio
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gmail_mcp import auth, server


def test_mac_is_not_headless_without_display(monkeypatch):
    monkeypatch.setattr(auth.sys, "platform", "darwin")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert not auth._is_headless()


def test_invalid_filesystem_token_has_actionable_auth_error():
    auth.config.token_path.parent.mkdir(parents=True)
    auth.config.token_path.write_text("invalid json", encoding="utf-8")
    with pytest.raises(auth.AuthError, match="Cached token") as exc:
        auth._load_token_filesystem()
    assert exc.value.requires_auth


def test_token_replace_failure_keeps_previous_token_and_cleans_temp(monkeypatch):
    auth.config.token_path.parent.mkdir(parents=True)
    auth.config.token_path.write_text("old token", encoding="utf-8")
    credentials = MagicMock()
    credentials.to_json.return_value = '{"token":"new"}'
    monkeypatch.setattr(auth.os, "replace", MagicMock(side_effect=PermissionError("locked")))
    with pytest.raises(PermissionError):
        auth._save_token_filesystem(credentials)
    assert auth.config.token_path.read_text(encoding="utf-8") == "old token"
    assert list(auth.config.token_path.parent.iterdir()) == [auth.config.token_path]


@pytest.mark.skipif(sys.platform == "win32", reason="Windows uses filesystem ACLs")
def test_token_files_are_owner_only():
    credentials = MagicMock()
    credentials.to_json.return_value = '{"token":"new"}'
    auth._save_token_filesystem(credentials)
    assert auth.config.token_path.stat().st_mode & 0o777 == 0o600


def test_keyring_invalid_shape_becomes_auth_error(monkeypatch):
    import keyring

    monkeypatch.setattr(keyring, "get_password", lambda *_: json.dumps(["invalid credentials"]))
    with pytest.raises(auth.AuthError):
        auth._load_token_keyring()


def test_oauth_flow_does_not_print_to_protocol_stdout(monkeypatch):
    auth.config.credentials_path.parent.mkdir(parents=True)
    auth.config.credentials_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(auth, "_is_headless", lambda: False)
    flow = MagicMock()
    monkeypatch.setattr(auth.InstalledAppFlow, "from_client_secrets_file", lambda *_: flow)
    auth._attempt_auth_flow()
    assert flow.run_local_server.call_args.kwargs["authorization_prompt_message"] == ""


def test_services_are_reused_per_thread_and_not_shared(monkeypatch):
    monkeypatch.setattr(auth, "_get_valid_credentials", lambda: MagicMock())
    monkeypatch.setattr(auth, "_build_service", lambda _: MagicMock())
    barrier = threading.Barrier(2)

    def get_twice():
        first = auth.get_gmail_service()
        barrier.wait(timeout=10)
        return first, auth.get_gmail_service()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(get_twice) for _ in range(2)]
        results = [future.result(timeout=15) for future in futures]
    assert all(first is second for first, second in results)
    assert results[0][0] is not results[1][0]


def test_logging_uses_stderr_and_creates_custom_log_directory(monkeypatch):
    captured = {}
    monkeypatch.setattr(server.logging, "basicConfig", lambda **kwargs: captured.update(kwargs))
    server._setup_logging()
    try:
        streams = [handler.stream for handler in captured["handlers"]]
        assert sys.stderr in streams and sys.stdout not in streams
        assert auth.config.log_path.is_file()
    finally:
        for handler in captured["handlers"]:
            handler.close()


def test_actual_stdio_initialize_and_list_all_25_tools():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def check():
        # No tool is invoked, and no real credential path is accessible to the child.
        env = {**os.environ, "GMAIL_MCP_CONFIG_DIR": str(Path.home() / "stdio-config")}
        params = StdioServerParameters(command=sys.executable, args=["-m", "gmail_mcp"], env=env)
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            assert len(result.tools) == 25
            assert {tool.name for tool in result.tools} >= {
                "send_email",
                "reply_email",
                "update_draft",
                "search_and_save",
            }

    asyncio.run(asyncio.wait_for(check(), timeout=30))


def test_readonly_log_directory_does_not_stop_protocol(monkeypatch):
    captured = {}
    monkeypatch.setattr(server.logging, "basicConfig", lambda **kwargs: captured.update(kwargs))
    monkeypatch.setattr(server, "RotatingFileHandler", MagicMock(side_effect=PermissionError("readonly")))
    server._setup_logging()
    assert len(captured["handlers"]) == 1
    assert captured["handlers"][0].stream is sys.stderr


def test_missing_oauth_credentials_fails_without_starting_browser(monkeypatch):
    start = MagicMock()
    monkeypatch.setattr(auth.InstalledAppFlow, "from_client_secrets_file", start)
    with pytest.raises(auth.AuthError, match=r"credentials\.json not found"):
        auth._attempt_auth_flow()
    start.assert_not_called()


def test_headless_oauth_fails_with_setup_instructions(monkeypatch):
    auth.config.credentials_path.parent.mkdir(parents=True)
    auth.config.credentials_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(auth, "_is_headless", lambda: True)
    with pytest.raises(auth.AuthError, match="python -c"):
        auth._attempt_auth_flow()


def test_refresh_saves_new_token_without_browser(monkeypatch):
    creds = MagicMock(valid=False, expired=True, refresh_token="refresh")
    monkeypatch.setattr(auth, "_load_token_filesystem", lambda: creds)
    save = MagicMock()
    browser = MagicMock()
    monkeypatch.setattr(auth, "_save_token_filesystem", save)
    monkeypatch.setattr(auth, "_attempt_auth_flow", browser)
    assert auth._get_valid_credentials() is creds
    creds.refresh.assert_called_once()
    save.assert_called_once_with(creds)
    browser.assert_not_called()


def test_auth_verification_failure_does_not_cache_service(monkeypatch):
    monkeypatch.setattr(auth, "_get_valid_credentials", lambda: MagicMock())
    service = MagicMock()
    service.users().getProfile.return_value.execute.side_effect = OSError("offline")
    monkeypatch.setattr(auth, "_build_service", lambda _: service)
    with pytest.raises(auth.AuthError, match="verification failed"):
        auth.get_gmail_service()
    assert auth._service_cache is None
