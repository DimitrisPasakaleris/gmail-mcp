import base64
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def isolated_filesystem(monkeypatch, tmp_path, request):
    """Never create test files or read tokens in the developer's real home."""
    from gmail_mcp import auth, tools

    if request.node.get_closest_marker("integration") and os.environ.get("GMAIL_TEST_REAL") == "1":
        yield
        return

    home = tmp_path / "home"
    (home / "tmp").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(tempfile, "tempdir", str(home / "tmp"))
    monkeypatch.chdir(home)
    monkeypatch.setattr(tools, "_CONFIG_DIR", home / "config")
    for name in ("token_path", "credentials_path", "log_path"):
        monkeypatch.setattr(auth.config, name, home / "config" / getattr(auth.config, name).name)
    auth.clear_service_cache()
    yield
    auth.clear_service_cache()


def sample_message_payload(
    msg_id: str = "msg_001",
    subject: str = "Test Subject",
    from_addr: str = "sender@example.com",
    to_addr: str = "recipient@example.com",
    body_text: str = "Hello, world!",
    labels: list | None = None,
    snippet: str = "Hello, world!",
) -> dict:
    if labels is None:
        labels = ["INBOX"]
    body_encoded = base64.urlsafe_b64encode(body_text.encode()).decode().rstrip("=")
    return {
        "id": msg_id,
        "threadId": "thread_001",
        "snippet": snippet,
        "labelIds": labels,
        "internalDate": "1700000000000",
        "payload": {
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": from_addr},
                {"name": "To", "value": to_addr},
                {"name": "Date", "value": "Thu, 01 Jan 2026 10:00:00 +0000"},
            ],
            "body": {"data": body_encoded},
            "mimeType": "text/plain",
        },
    }


def sample_multipart_message(
    plain_body: str = "Plain body",
    html_body: str = "<p>HTML body</p>",
) -> dict:
    return {
        "id": "msg_002",
        "snippet": "Multipart email",
        "labelIds": ["INBOX"],
        "internalDate": "1700000000000",
        "payload": {
            "headers": [
                {"name": "Subject", "value": "Multipart Email"},
                {"name": "From", "value": "alice@example.com"},
                {"name": "To", "value": "bob@example.com"},
            ],
            "mimeType": "multipart/alternative",
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": base64.urlsafe_b64encode(plain_body.encode()).decode().rstrip("=")},
                },
                {
                    "mimeType": "text/html",
                    "body": {"data": base64.urlsafe_b64encode(html_body.encode()).decode().rstrip("=")},
                },
            ],
        },
    }


def sample_attachment_payload() -> dict:
    return {
        "id": "msg_003",
        "snippet": "Email with attachment",
        "labelIds": ["INBOX"],
        "internalDate": "1700000000000",
        "payload": {
            "headers": [
                {"name": "Subject", "value": "With Attachment"},
                {"name": "From", "value": "alice@example.com"},
                {"name": "To", "value": "bob@example.com"},
            ],
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": base64.urlsafe_b64encode(b"See attached file.").decode().rstrip("=")},
                },
                {
                    "mimeType": "text/plain",
                    "filename": "report.txt",
                    "body": {"attachmentId": "att_001", "size": 1024},
                },
            ],
        },
    }


def sample_draft_response(draft_id: str = "draft_001", msg_id: str = "msg_draft_001") -> dict:
    return {
        "id": draft_id,
        "message": {
            "id": msg_id,
            "threadId": "thread_draft_001",
        },
    }


def build_mock_service(
    messages_list_response: dict | None = None,
    messages_get_response: dict | None = None,
    profile_response: dict | None = None,
    send_response: dict | None = None,
    modify_response: dict | None = None,
    attachment_response: dict | None = None,
    labels: list | None = None,
) -> MagicMock:
    if labels is None:
        labels = ["INBOX", "IMPORTANT"]

    if profile_response is None:
        profile_response = {"emailAddress": "me@example.com"}
    if messages_list_response is None:
        messages_list_response = {"messages": [{"id": "msg_001"}], "resultSizeEstimate": 1}
    if messages_get_response is None:
        messages_get_response = sample_message_payload()
    if send_response is None:
        send_response = {"id": "sent_msg_001", "threadId": "thread_001"}
    if modify_response is None:
        modify_response = {"id": "msg_001", "labelIds": labels}
    if attachment_response is None:
        attachment_response = {"data": base64.urlsafe_b64encode(b"file content").decode()}

    service = MagicMock()

    profile_mock = MagicMock()
    profile_mock.execute.return_value = profile_response
    service.users().getProfile.return_value = profile_mock

    list_mock = MagicMock()
    list_mock.execute.return_value = messages_list_response
    service.users().messages().list.return_value = list_mock

    get_mock = MagicMock()
    get_mock.execute.return_value = messages_get_response
    service.users().messages().get.return_value = get_mock

    send_mock = MagicMock()
    send_mock.execute.return_value = send_response
    service.users().messages().send.return_value = send_mock

    modify_mock = MagicMock()
    modify_mock.execute.return_value = modify_response
    service.users().messages().modify.return_value = modify_mock

    draft_create_mock = MagicMock()
    draft_create_mock.execute.return_value = sample_draft_response()
    service.users().drafts().create.return_value = draft_create_mock

    draft_list_mock = MagicMock()
    draft_list_mock.execute.return_value = {"drafts": []}
    service.users().drafts().list.return_value = draft_list_mock

    draft_get_mock = MagicMock()
    draft_get_mock.execute.return_value = sample_draft_response()
    service.users().drafts().get.return_value = draft_get_mock

    draft_update_mock = MagicMock()
    draft_update_mock.execute.return_value = sample_draft_response(draft_id="draft_001", msg_id="msg_updated")
    service.users().drafts().update.return_value = draft_update_mock

    draft_delete_mock = MagicMock()
    draft_delete_mock.execute.return_value = None
    service.users().drafts().delete.return_value = draft_delete_mock

    att_mock = MagicMock()
    att_mock.execute.return_value = attachment_response
    service.users().messages().attachments().get.return_value = att_mock

    return service


@pytest.fixture
def mock_service():
    return build_mock_service()


@pytest.fixture
def sample_message():
    return sample_message_payload()


@pytest.fixture
def sample_multipart():
    return sample_multipart_message()


@pytest.fixture
def sample_attachment():
    return sample_attachment_payload()


@pytest.fixture
def sample_draft():
    return sample_draft_response()


@pytest.fixture
def real_gmail_enabled():
    return os.environ.get("GMAIL_TEST_REAL") == "1"


# Re-export _with_retry for test use
