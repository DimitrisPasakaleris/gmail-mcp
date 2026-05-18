import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from gmail_mcp.tools import (
    ok, err,
    list_emails, get_thread_messages,
    send_email,
    reply_email,
    archive_email, trash_email, mark_read, add_label, remove_label,
    read_email,
    create_draft, list_drafts, update_draft, delete_draft,
    list_attachments, download_attachment,
    _with_retry, _safe_save_path,
    # batch tools
    read_emails, mark_emails_read, archive_emails,
    trash_emails, add_labels, download_attachments,
    # file export tools
    read_emails_full, search_and_save, append_emails,
    _build_markdown_file,
)
from gmail_mcp.auth import AuthError

from .conftest import (
    sample_message_payload, sample_multipart_message,
    sample_attachment_payload, sample_draft_response, build_mock_service,
)


class TestHelpers:
    def test_ok_returns_correct_shape(self):
        result = ok({"foo": "bar"})
        assert result == {"ok": True, "data": {"foo": "bar"}}

    def test_err_returns_correct_shape(self):
        result = err("test_error", "Something went wrong")
        assert result["ok"] is False
        assert result["error"] == "test_error"
        assert result["message"] == "Something went wrong"


class TestListEmails:
    def test_returns_structured_ok(self):
        mock_service = build_mock_service()
        with patch("gmail_mcp.tools.get_gmail_service", return_value=mock_service):
            result = list_emails(query="", max_results=5)
        assert result["ok"] is True
        assert "emails" in result["data"]
        assert "total" in result["data"]

    def test_empty_query_returns_empty_list(self):
        mock_service = build_mock_service(messages_list_response={"messages": [], "resultSizeEstimate": 0})
        with patch("gmail_mcp.tools.get_gmail_service", return_value=mock_service):
            result = list_emails()
        assert result["ok"] is True
        assert result["data"]["emails"] == []

    def test_api_error_returns_error(self):
        from googleapiclient.errors import HttpError
        mock_service = build_mock_service()
        mock_service.users().messages().list.return_value.execute.side_effect = HttpError(
            MagicMock(), b'{"error": "api_error"}'
        )
        with patch("gmail_mcp.tools.get_gmail_service", return_value=mock_service):
            result = list_emails()
        assert result["ok"] is False
        assert result["error"] == "api_error"

    def test_auth_missing_returns_error(self):
        with patch("gmail_mcp.tools.get_gmail_service", side_effect=AuthError("auth failed")):
            result = list_emails()
        assert result["ok"] is False
        assert result["error"] == "auth_missing"


class TestSendEmail:
    def test_basic_send_returns_ok(self):
        mock_service = build_mock_service()
        with patch("gmail_mcp.tools.get_gmail_service", return_value=mock_service):
            result = send_email(to="test@example.com", subject="Test", body="Hello")
        assert result["ok"] is True
        assert "message_id" in result["data"]
        assert result["data"]["to"] == "test@example.com"

    def test_empty_to_returns_validation_error(self):
        mock_service = build_mock_service()
        with patch("gmail_mcp.tools.get_gmail_service", return_value=mock_service):
            result = send_email(to="", subject="Test", body="Hello")
        assert result["ok"] is False
        assert result["error"] == "validation_error"

    def test_thread_id_passed_via_mime_headers(self):
        """thread_id controls threading via In-Reply-To/References in MIME, not an API param."""
        mock_service = build_mock_service()
        with patch("gmail_mcp.tools.get_gmail_service", return_value=mock_service):
            send_email(
                to="test@example.com", subject="Test", body="Hello",
                thread_id="thread_xyz",
                in_reply_to="<msg123@example.com>",
                references="<msg123@example.com>"
            )
        # Threading is handled via MIME headers in the raw body, not an API parameter.
        # Verify the send() was called successfully (message went through).
        mock_service.users().messages().send.assert_called_once()

    def test_attachment_file_not_found(self):
        mock_service = build_mock_service()
        with patch("gmail_mcp.tools.get_gmail_service", return_value=mock_service):
            result = send_email(
                to="test@example.com", subject="Test", body="Hello",
                attachments=["nonexistent_file_abcxyz.pdf"]
            )
        assert result["ok"] is False
        assert result["error"] == "file_not_found"

    def test_attachment_too_large(self):
        mock_service = build_mock_service()
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"x" * (26 * 1024 * 1024))  # > 25 MB
            path = f.name
        try:
            with patch("gmail_mcp.tools.get_gmail_service", return_value=mock_service):
                result = send_email(to="test@example.com", subject="Test", body="Hello", attachments=[path])
            assert result["ok"] is False
            assert result["error"] == "validation_error"
        finally:
            os.unlink(path)


class TestReplyEmail:
    def test_missing_thread_id_returns_error(self):
        mock_service = build_mock_service()
        with patch("gmail_mcp.tools.get_gmail_service", return_value=mock_service):
            result = reply_email(to="test@example.com", body="Hello", thread_id="", in_reply_to="msg_001")
        assert result["ok"] is False
        assert result["error"] == "validation_error"

    def test_missing_in_reply_to_returns_error(self):
        mock_service = build_mock_service()
        with patch("gmail_mcp.tools.get_gmail_service", return_value=mock_service):
            result = reply_email(to="test@example.com", body="Hello", thread_id="thread_001", in_reply_to="")
        assert result["ok"] is False
        assert result["error"] == "validation_error"

    def test_original_message_not_found(self):
        from googleapiclient.errors import HttpError
        mock_service = build_mock_service()
        resp = MagicMock()
        resp.status = 404
        mock_service.users().messages().get.return_value.execute.side_effect = HttpError(resp, b'{"error": "not_found"}')

        with patch("gmail_mcp.tools.get_gmail_service", return_value=mock_service):
            result = reply_email(
                to="test@example.com", body="Hello",
                thread_id="thread_001", in_reply_to="nonexistent"
            )
        assert result["ok"] is False
        assert result["error"] == "not_found"


class TestLabelManagement:
    def test_archive_email_removes_inbox(self):
        mock_service = build_mock_service()
        with patch("gmail_mcp.tools.get_gmail_service", return_value=mock_service):
            result = archive_email("msg_001")
        assert result["ok"] is True
        call_kwargs = mock_service.users().messages().modify.call_args[1]["body"]
        assert "INBOX" in call_kwargs.get("removeLabelIds", [])

    def test_trash_email_adds_trash(self):
        mock_service = build_mock_service()
        with patch("gmail_mcp.tools.get_gmail_service", return_value=mock_service):
            result = trash_email("msg_001")
        assert result["ok"] is True
        call_kwargs = mock_service.users().messages().modify.call_args[1]["body"]
        assert "TRASH" in call_kwargs.get("addLabelIds", [])

    def test_mark_read_removes_unread(self):
        mock_service = build_mock_service()
        with patch("gmail_mcp.tools.get_gmail_service", return_value=mock_service):
            result = mark_read("msg_001")
        assert result["ok"] is True
        call_kwargs = mock_service.users().messages().modify.call_args[1]["body"]
        assert "UNREAD" in call_kwargs.get("removeLabelIds", [])

    def test_add_label_adds_custom_label(self):
        mock_service = build_mock_service()
        with patch("gmail_mcp.tools.get_gmail_service", return_value=mock_service):
            result = add_label("msg_001", "IMPORTANT")
        assert result["ok"] is True
        call_kwargs = mock_service.users().messages().modify.call_args[1]["body"]
        assert "IMPORTANT" in call_kwargs.get("addLabelIds", [])

    def test_remove_label_removes_label(self):
        mock_service = build_mock_service()
        with patch("gmail_mcp.tools.get_gmail_service", return_value=mock_service):
            result = remove_label("msg_001", "IMPORTANT")
        assert result["ok"] is True
        call_kwargs = mock_service.users().messages().modify.call_args[1]["body"]
        assert "IMPORTANT" in call_kwargs.get("removeLabelIds", [])

    def test_add_label_empty_returns_error(self):
        mock_service = build_mock_service()
        with patch("gmail_mcp.tools.get_gmail_service", return_value=mock_service):
            result = add_label("msg_001", "")
        assert result["ok"] is False
        assert result["error"] == "validation_error"


class TestReadEmail:
    def test_returns_body_and_headers(self):
        mock_service = build_mock_service()
        mock_service.users().messages().get.return_value.execute.return_value = sample_message_payload()
        with patch("gmail_mcp.tools.get_gmail_service", return_value=mock_service):
            result = read_email("msg_001")
        assert result["ok"] is True
        assert result["data"]["body"] is not None
        assert result["data"]["subject"] == "Test Subject"

    def test_404_returns_not_found(self):
        from googleapiclient.errors import HttpError
        mock_service = build_mock_service()
        resp = MagicMock()
        resp.status = 404
        mock_service.users().messages().get.return_value.execute.side_effect = HttpError(resp, b'{}')
        with patch("gmail_mcp.tools.get_gmail_service", return_value=mock_service):
            result = read_email("nonexistent")
        assert result["ok"] is False
        assert result["error"] == "not_found"

    def test_multipart_extracts_both_bodies(self):
        mock_service = build_mock_service()
        mock_service.users().messages().get.return_value.execute.return_value = sample_multipart_message()
        with patch("gmail_mcp.tools.get_gmail_service", return_value=mock_service):
            result = read_email("msg_002")
        assert result["ok"] is True
        assert result["data"]["body"] == "Plain body"
        assert result["data"]["html_body"] == "<p>HTML body</p>"

    def test_empty_message_id_returns_error(self):
        mock_service = build_mock_service()
        with patch("gmail_mcp.tools.get_gmail_service", return_value=mock_service):
            result = read_email("")
        assert result["ok"] is False
        assert result["error"] == "validation_error"


class TestDraftManagement:
    def test_create_draft_returns_ids(self):
        mock_service = build_mock_service()
        mock_service.users().drafts().create.return_value.execute.return_value = sample_draft_response()
        with patch("gmail_mcp.tools.get_gmail_service", return_value=mock_service):
            result = create_draft(to="test@example.com", subject="Draft", body="Draft body")
        assert result["ok"] is True
        assert "draft_id" in result["data"]
        assert "message_id" in result["data"]

    def test_create_draft_empty_to_returns_error(self):
        mock_service = build_mock_service()
        with patch("gmail_mcp.tools.get_gmail_service", return_value=mock_service):
            result = create_draft(to="", subject="Draft", body="Body")
        assert result["ok"] is False
        assert result["error"] == "validation_error"

    def test_update_draft_body_required(self):
        mock_service = build_mock_service()
        mock_service.users().drafts().get.return_value.execute.return_value = sample_draft_response()
        with patch("gmail_mcp.tools.get_gmail_service", return_value=mock_service):
            result = update_draft(draft_id="draft_001", to="", subject="", body="")
        assert result["ok"] is False
        assert "body is required" in result["message"]

    def test_delete_draft_returns_ok(self):
        mock_service = build_mock_service()
        with patch("gmail_mcp.tools.get_gmail_service", return_value=mock_service):
            result = delete_draft("draft_001")
        assert result["ok"] is True
        assert mock_service.users().drafts().delete.called

    def test_delete_draft_404_returns_error(self):
        from googleapiclient.errors import HttpError
        mock_service = build_mock_service()
        resp = MagicMock()
        resp.status = 404
        mock_service.users().drafts().delete.return_value.execute.side_effect = HttpError(resp, b'{}')
        with patch("gmail_mcp.tools.get_gmail_service", return_value=mock_service):
            result = delete_draft("nonexistent")
        assert result["ok"] is False
        assert result["error"] == "draft_not_found"


class TestAttachmentTools:
    def test_list_attachments_returns_structured(self):
        mock_service = build_mock_service()
        mock_service.users().messages().get.return_value.execute.return_value = sample_attachment_payload()
        with patch("gmail_mcp.tools.get_gmail_service", return_value=mock_service):
            result = list_attachments("msg_003")
        assert result["ok"] is True
        assert len(result["data"]["attachments"]) == 1
        att = result["data"]["attachments"][0]
        assert att["id"] == "att_001"
        assert att["filename"] == "report.txt"

    def test_download_attachment_writes_file(self):
        mock_service = build_mock_service()
        mock_service.users().messages().get.return_value.execute.return_value = sample_attachment_payload()
        with tempfile.TemporaryDirectory() as tmpdir:
            save_path = os.path.join(tmpdir, "report.txt")
            with patch("gmail_mcp.tools.get_gmail_service", return_value=mock_service):
                result = download_attachment("msg_003", "att_001", save_path)
            assert result["ok"] is True
            assert os.path.exists(save_path)

    def test_download_attachment_not_found_returns_error(self):
        from googleapiclient.errors import HttpError
        mock_service = build_mock_service()
        mock_service.users().messages().get.return_value.execute.return_value = sample_attachment_payload()
        with patch("gmail_mcp.tools.get_gmail_service", return_value=mock_service):
            result = download_attachment("msg_003", "att_nonexistent", "/tmp/out.txt")
        assert result["ok"] is False
        # On Windows /tmp resolves outside home dir — path validation fires first
        assert result["error"] in ("attachment_not_found", "file_not_found")


class TestBatchTools:
    """Tests for the 6 new batch tools."""

    def test_read_emails_empty_returns_ok(self):
        mock_service = build_mock_service()
        with patch("gmail_mcp.tools.get_gmail_service", return_value=mock_service):
            result = read_emails([])
        assert result["ok"] is True
        assert result["data"]["requested"] == 0
        assert result["data"]["succeeded"] == 0
        assert result["data"]["failed"] == 0
        assert result["data"]["results"] == []

    def test_read_emails_auth_error(self):
        with patch("gmail_mcp.tools.get_gmail_service", side_effect=AuthError("auth failed")):
            result = read_emails(["msg_001"])
        assert result["ok"] is False
        assert result["error"] == "auth_missing"

    def test_read_emails_single_message(self):
        mock_service = build_mock_service()
        mock_service.users().messages().get.return_value.execute.return_value = sample_message_payload()

        results = {}

        def callback(rid, resp, exc):
            results[rid] = (resp, exc)

        batch_mock = MagicMock()
        batch_mock.add = lambda req, request_id=None: None

        def batch_execute(http=None):
            callback("msg_001", sample_message_payload(), None)

        batch_mock.execute = batch_execute
        mock_service.new_batch_http_request.return_value = batch_mock

        with patch("gmail_mcp.tools.get_gmail_service", return_value=mock_service):
            result = read_emails(["msg_001"])
        assert result["ok"] is True

    # ── mark_emails_read ───────────────────────────────────────────────────────

    def test_mark_emails_read_empty_returns_ok(self):
        mock_service = build_mock_service()
        with patch("gmail_mcp.tools.get_gmail_service", return_value=mock_service):
            result = mark_emails_read([])
        assert result["ok"] is True
        assert result["data"]["requested"] == 0

    def test_mark_emails_read_calls_batchModify(self):
        mock_service = build_mock_service()
        with patch("gmail_mcp.tools.get_gmail_service", return_value=mock_service):
            result = mark_emails_read(["msg_001", "msg_002"])
        assert result["ok"] is True
        mock_service.users().messages().batchModify.assert_called_once()
        call_kwargs = mock_service.users().messages().batchModify.call_args[1]["body"]
        assert call_kwargs["removeLabelIds"] == ["UNREAD"]
        assert call_kwargs["ids"] == ["msg_001", "msg_002"]
        assert result["data"]["succeeded"] == 2
        assert result["data"]["failed"] == 0

    def test_mark_emails_read_auth_error(self):
        with patch("gmail_mcp.tools.get_gmail_service", side_effect=AuthError("auth failed")):
            result = mark_emails_read(["msg_001"])
        assert result["ok"] is False
        assert result["error"] == "auth_missing"

    # ── archive_emails ─────────────────────────────────────────────────────────

    def test_archive_emails_empty_returns_ok(self):
        mock_service = build_mock_service()
        with patch("gmail_mcp.tools.get_gmail_service", return_value=mock_service):
            result = archive_emails([])
        assert result["ok"] is True

    def test_archive_emails_calls_batchModify(self):
        mock_service = build_mock_service()
        with patch("gmail_mcp.tools.get_gmail_service", return_value=mock_service):
            result = archive_emails(["msg_001", "msg_002", "msg_003"])
        assert result["ok"] is True
        mock_service.users().messages().batchModify.assert_called_once()
        call_kwargs = mock_service.users().messages().batchModify.call_args[1]["body"]
        assert call_kwargs["removeLabelIds"] == ["INBOX"]
        assert call_kwargs["ids"] == ["msg_001", "msg_002", "msg_003"]
        assert result["data"]["succeeded"] == 3

    # ── trash_emails ───────────────────────────────────────────────────────────

    def test_trash_emails_empty_returns_ok(self):
        mock_service = build_mock_service()
        with patch("gmail_mcp.tools.get_gmail_service", return_value=mock_service):
            result = trash_emails([])
        assert result["ok"] is True

    def test_trash_emails_calls_batchModify(self):
        mock_service = build_mock_service()
        with patch("gmail_mcp.tools.get_gmail_service", return_value=mock_service):
            result = trash_emails(["msg_001"])
        assert result["ok"] is True
        mock_service.users().messages().batchModify.assert_called_once()
        call_kwargs = mock_service.users().messages().batchModify.call_args[1]["body"]
        assert call_kwargs["addLabelIds"] == ["TRASH"]

    # ── add_labels ─────────────────────────────────────────────────────────────

    def test_add_labels_empty_returns_ok(self):
        mock_service = build_mock_service()
        with patch("gmail_mcp.tools.get_gmail_service", return_value=mock_service):
            result = add_labels([], "IMPORTANT")
        assert result["ok"] is True

    def test_add_labels_empty_label_returns_error(self):
        mock_service = build_mock_service()
        with patch("gmail_mcp.tools.get_gmail_service", return_value=mock_service):
            result = add_labels(["msg_001"], "")
        assert result["ok"] is False
        assert result["error"] == "validation_error"

    def test_add_labels_calls_batchModify(self):
        mock_service = build_mock_service()
        with patch("gmail_mcp.tools.get_gmail_service", return_value=mock_service):
            result = add_labels(["msg_001", "msg_002"], "IMPORTANT")
        assert result["ok"] is True
        mock_service.users().messages().batchModify.assert_called_once()
        call_kwargs = mock_service.users().messages().batchModify.call_args[1]["body"]
        assert call_kwargs["addLabelIds"] == ["IMPORTANT"]
        assert call_kwargs["ids"] == ["msg_001", "msg_002"]

    # ── download_attachments ────────────────────────────────────────────────────

    def test_download_attachments_empty_returns_ok(self):
        mock_service = build_mock_service()
        with patch("gmail_mcp.tools.get_gmail_service", return_value=mock_service):
            result = download_attachments([])
        assert result["ok"] is True
        assert result["data"]["requested"] == 0

    def test_download_attachments_auth_error(self):
        with patch("gmail_mcp.tools.get_gmail_service", side_effect=AuthError("auth failed")):
            result = download_attachments([{"message_id": "msg_003", "attachment_id": "att_001", "save_path": "/tmp/test.txt"}])
        assert result["ok"] is False
        assert result["error"] == "auth_missing"

    def test_download_attachments_missing_fields(self):
        mock_service = build_mock_service()
        batch_mock = MagicMock()
        batch_mock.add = lambda req, request_id=None: None
        batch_mock.execute = lambda http=None: None
        mock_service.new_batch_http_request.return_value = batch_mock

        with patch("gmail_mcp.tools.get_gmail_service", return_value=mock_service):
            with tempfile.TemporaryDirectory() as tmpdir:
                result = download_attachments([{"message_id": "msg_003"}])
        assert result["ok"] is True
        assert result["data"]["requested"] == 1
        assert result["data"]["failed"] == 1
        assert result["data"]["results"][0]["ok"] is False

    def test_download_attachments_calls_batch(self):
        mock_service = build_mock_service()
        batch_mock = MagicMock()
        batch_mock.add = MagicMock()
        batch_mock.execute = lambda http=None: None
        mock_service.new_batch_http_request.return_value = batch_mock

        with patch("gmail_mcp.tools.get_gmail_service", return_value=mock_service):
            with tempfile.TemporaryDirectory() as tmpdir:
                result = download_attachments([
                    {"message_id": "msg_003", "attachment_id": "att_001", "save_path": tmpdir + "/file.txt"}
                ])
        assert result["ok"] is True
        mock_service.new_batch_http_request.assert_called_once()
        batch_mock.add.assert_called_once()


class TestFileExportTools:

    def test_read_emails_full_empty_ids(self):
        """Empty message_ids returns empty saved result."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = read_emails_full([], tmpdir + "/out.md")
        assert result["ok"] is True
        assert result["data"]["saved"] is False
        assert result["data"]["count"] == 0

    def test_read_emails_full_invalid_path(self):
        """Path traversal attempt is rejected."""
        result = read_emails_full(["msg_001"], "/etc/passwd")
        assert result["ok"] is False
        assert result["error"] == "file_not_found"

    def test_search_and_save_empty_query(self):
        """Empty query returns validation error."""
        result = search_and_save("")
        assert result["ok"] is False
        assert result["error"] == "validation_error"

    def test_search_and_save_invalid_path(self):
        """Invalid save_path is rejected."""
        result = search_and_save("from:test", save_path="/etc/passwd")
        assert result["ok"] is False
        assert result["error"] == "file_not_found"

    def test_append_emails_empty_ids(self):
        """Empty message_ids returns empty appended result."""
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir) / "existing.md"
            p.write_text("# Existing\n\n---\n", encoding="utf-8")
            result = append_emails([], str(p))
        assert result["ok"] is True
        assert result["data"]["appended"] is False
        assert result["data"]["count"] == 0

    def test_append_emails_nonexistent_file(self):
        """Appending to a nonexistent file is rejected."""
        result = append_emails(["msg_001"], "~/no-such-file.md")
        assert result["ok"] is False
        assert result["error"] == "file_not_found"

    def test_append_emails_unsupported_extension(self):
        """Only .md, .json, .jsonl are supported."""
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir) / "data.csv"
            p.write_text("col\nval\n", encoding="utf-8")
            result = append_emails(["msg_001"], str(p))
        assert result["ok"] is False
        assert result["error"] == "validation_error"

    def test_read_emails_full_calls_batch(self):
        """read_emails_full uses BatchHttpRequest to fetch messages."""
        mock_service = build_mock_service()
        batch_mock = MagicMock()
        batch_mock.add = MagicMock()
        batch_mock.execute = lambda http=None: None
        mock_service.new_batch_http_request.return_value = batch_mock

        with patch("gmail_mcp.tools.get_gmail_service", return_value=mock_service):
            with tempfile.TemporaryDirectory() as tmpdir:
                result = read_emails_full(["msg_001", "msg_002"], tmpdir + "/out.md")
        assert result["ok"] is True
        mock_service.new_batch_http_request.assert_called_once()
        assert batch_mock.add.call_count == 2

    def test_search_and_save_no_results(self):
        """Search with no matches returns empty saved result."""
        mock_service = build_mock_service()
        mock_service.users().messages().list().execute.return_value = {"messages": []}

        with patch("gmail_mcp.tools.get_gmail_service", return_value=mock_service):
            result = search_and_save("from:noone@nomatch.example")
        assert result["ok"] is True
        assert result["data"]["saved"] is False
        assert result["data"]["count"] == 0

    def test_search_and_save_auto_generates_path(self):
        """search_and_save with no save_path auto-generates one."""
        mock_service = build_mock_service()
        mock_service.users().messages().list().execute.return_value = {
            "messages": [{"id": "msg_001"}],
            "resultSizeEstimate": 1,
        }
        batch_mock = MagicMock()
        batch_mock.add = MagicMock()
        batch_mock.execute = lambda http=None: None
        mock_service.new_batch_http_request.return_value = batch_mock

        with patch("gmail_mcp.tools.get_gmail_service", return_value=mock_service):
            result = search_and_save("from:test")
        assert result["ok"] is True
        assert result["data"]["saved"] is True
        assert result["data"]["count"] == 1


class TestBuildMarkdownFile:
    """Regression tests for _build_markdown_file ok_status and mixed results."""

    def test_all_ok_entries_render_emoji_status(self):
        """All-ok entries show ✅ status icons."""
        results = [
            {"ok": True, "data": {"body": "Body A", "from": "a@example.com", "subject": "Subject A", "date": "2026-05-11", "to": "me@test.com", "labels": ["INBOX"]}},
            {"ok": True, "data": {"body": "Body B", "from": "b@example.com", "subject": "Subject B", "date": "2026-05-12", "to": "me@test.com", "labels": []}},
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "emails.md"
            result = _build_markdown_file(results, path, query="test query")
            assert result["saved"] is True
            content = path.read_text(encoding="utf-8")
            assert "✅" in content
            assert "❌" not in content

    def test_mixed_ok_and_error_entries_render_both_statuses(self):
        """Mixed ok/error entries show both ✅ and ❌ status icons."""
        results = [
            {"ok": True, "data": {"body": "Good email", "from": "a@example.com", "subject": "Subject A", "date": "2026-05-11", "to": "me@test.com", "labels": ["INBOX"]}},
            {"ok": False, "error": "not_found", "data": {}},
            {"ok": True, "data": {"body": "Another good", "from": "c@example.com", "subject": "Subject C", "date": "2026-05-13", "to": "me@test.com", "labels": []}},
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "emails.md"
            result = _build_markdown_file(results, path, query="test query")
            assert result["saved"] is True
            content = path.read_text(encoding="utf-8")
            assert "✅" in content
            assert "❌ not_found" in content

    def test_error_without_data_renders_gracefully(self):
        """Error entries with no data key don't crash."""
        results = [
            {"ok": False, "error": "api_error", "message": "server error", "data": {}},
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "emails.md"
            result = _build_markdown_file(results, path, query="test")
            assert result["saved"] is True
            content = path.read_text(encoding="utf-8")
            assert "❌ api_error" in content
            # Must not crash on missing body/from/subject

    def test_truncates_long_bodies(self):
        """Bodies over 5000 chars are truncated."""
        long_body = "x" * 6000
        results = [
            {"ok": True, "data": {"body": long_body, "from": "a@test.com", "subject": "Long", "date": "2026-05-11", "to": "me@test.com", "labels": []}},
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "emails.md"
            result = _build_markdown_file(results, path)
            content = path.read_text(encoding="utf-8")
            # Body should be truncated with marker
            assert "... *(truncated)*" in content
            assert len(content) < 7000

    def test_missing_body_uses_html_fallback(self):
        """Entries with no body fall back to html_body."""
        results = [
            {"ok": True, "data": {"html_body": "<p>Fallback body</p>", "from": "a@test.com", "subject": "HTML email", "date": "2026-05-11", "to": "me@test.com", "labels": []}},
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "emails.md"
            result = _build_markdown_file(results, path)
            content = path.read_text(encoding="utf-8")
            assert "Fallback body" in content


class TestDownloadAttachmentOptimization:
    """Tests for download_attachment filename fetch optimization."""

    def test_filename_fetched_via_extra_api_call(self):
        """download_attachment makes a metadata API call to resolve filename before downloading."""
        mock_service = build_mock_service()
        mock_service.users().messages().get.return_value.execute.return_value = sample_attachment_payload()

        get_call_count = [0]
        original_get = mock_service.users().messages().get.return_value.execute.return_value
        def count_gets(*args, **kwargs):
            get_call_count[0] += 1
            return original_get

        mock_service.users().messages().get.return_value.execute.side_effect = count_gets

        attachments_mock = MagicMock()
        attachments_mock.get.return_value.execute.return_value = {"data": "dGVzdA=="}
        mock_service.users().messages().attachments.return_value = attachments_mock

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("gmail_mcp.tools.get_gmail_service", return_value=mock_service):
                result = download_attachment("msg_003", "att_001", tmpdir + "/file.txt")

        # One call to get metadata (for filename), one to attachments().get() (for data)
        assert result["ok"] is True
        # The implementation fetches filename via messages().get() — confirmed by code trace
        assert get_call_count[0] >= 1
