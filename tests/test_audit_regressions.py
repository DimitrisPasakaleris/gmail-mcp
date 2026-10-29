"""Offline regressions for threading, MIME preservation, batching and exports."""

import base64
import csv
import json
from concurrent.futures import ThreadPoolExecutor
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import MagicMock

import httplib2
import pytest
from googleapiclient.errors import HttpError

from gmail_mcp import tools

from .conftest import build_mock_service, sample_message_payload


def decode_message(raw):
    return BytesParser(policy=policy.default).parsebytes(
        base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    )


def http_error(status=503, retry_after=None):
    headers = {"status": str(status)}
    if retry_after is not None:
        headers["retry-after"] = retry_after
    return HttpError(httplib2.Response(headers), b'{"error":{"message":"temporary failure"}}')


@pytest.fixture
def service(monkeypatch):
    service = build_mock_service()
    monkeypatch.setattr(tools, "_get_service", lambda: service)
    return service


def install_batches(service, respond):
    """Execute callbacks in reverse order to catch request-ID/order assumptions."""
    sizes = []

    def factory(callback):
        requests = []
        batch = MagicMock()
        batch.add.side_effect = lambda request, request_id: requests.append((request_id, request))

        def execute(**_):
            sizes.append(len(requests))
            for request_id, request in reversed(requests):
                response, exception = respond(request)
                callback(request_id, response, exception)

        batch.execute.side_effect = execute
        return batch

    service.new_batch_http_request.side_effect = factory
    return sizes


def test_reply_uses_rfc_message_id_and_gmail_thread(service):
    original = sample_message_payload()
    original["payload"]["headers"] = [
        {"name": "subject", "value": "Original"},
        {"name": "message-id", "value": "<original@example.com>"},
        {"name": "references", "value": "<ancestor@example.com>"},
        {"name": "to", "value": "Me <me@example.com>, Other <other@example.com>"},
        {"name": "cc", "value": "OTHER@example.com, sender@example.com"},
    ]
    service.users().messages().get.return_value.execute.return_value = original
    service.users().getProfile.return_value.execute.return_value = {"emailAddress": "ME@example.com"}
    result = tools.reply_email("sender@example.com", "Reply", "thread_001", "msg_001", reply_all=True)
    assert result["ok"]
    payload = service.users().messages().send.call_args.kwargs["body"]
    message = decode_message(payload["raw"])
    assert payload["threadId"] == "thread_001"
    assert message["In-Reply-To"] == "<original@example.com>"
    assert str(message["References"]) == "<ancestor@example.com> <original@example.com>"
    assert message["From"] == "ME@example.com"
    assert message["Cc"] == "other@example.com"
    assert message["Subject"] == "Re: Original"


def test_reply_rejects_message_from_another_thread(service):
    service.users().messages().get.return_value.execute.return_value = sample_message_payload()
    result = tools.reply_email("a@example.com", "reply", "other-thread", "msg_001")
    assert result["error"] == tools.ERROR_VALIDATION
    service.users().messages().send.assert_not_called()


@pytest.mark.parametrize("operation", [tools.send_email, tools.create_draft])
@pytest.mark.parametrize("failure", [TimeoutError("response lost"), http_error()])
def test_non_idempotent_operations_never_retry(service, operation, failure):
    request = (
        service.users().messages().send if operation is tools.send_email else service.users().drafts().create
    )
    request.return_value.execute.side_effect = failure
    result = operation("a@example.com", "Subject", "Body")
    assert not result["ok"]
    assert "Check Gmail before retrying" in result["message"]
    assert request.call_count == 1


@pytest.mark.parametrize("attachments", [None, []])
def test_draft_body_edit_preserves_headers_thread_and_optional_files(service, attachments):
    original = EmailMessage()
    for key, value in {
        "To": "a@example.com",
        "Cc": "cc@example.com",
        "Bcc": "bcc@example.com",
        "Subject": "Original",
        "From": "me@example.com",
        "In-Reply-To": "<old@example.com>",
        "References": "<old@example.com>",
    }.items():
        original[key] = value
    original.set_content("old text")
    original.add_attachment(
        b"precious bytes", maintype="application", subtype="octet-stream", filename="keep.bin"
    )
    service.users().drafts().get.return_value.execute.return_value = {
        "message": {"raw": base64.urlsafe_b64encode(original.as_bytes()).decode(), "threadId": "thread"},
    }
    service.users().drafts().update.return_value.execute.return_value = {
        "id": "draft",
        "message": {"id": "updated"},
    }
    assert tools.update_draft("draft", body="new text", attachments=attachments)["ok"]
    payload = service.users().drafts().update.call_args.kwargs["body"]["message"]
    edited = decode_message(payload["raw"])
    assert payload["threadId"] == "thread"
    for header in ("To", "Cc", "Bcc", "Subject", "From", "In-Reply-To", "References"):
        assert edited[header] == original[header]
    assert edited.get_body(preferencelist=("plain",)).get_content().strip() == "new text"
    files = list(edited.iter_attachments())
    if attachments is None:
        assert len(files) == 1 and files[0].get_payload(decode=True) == b"precious bytes"
    else:
        assert files == []


def test_read_batches_chunk_and_preserve_duplicates_errors_and_order(service):
    service.users().messages().get.side_effect = lambda **kwargs: kwargs["id"]
    sizes = install_batches(
        service,
        lambda mid: (sample_message_payload(msg_id=mid), None) if mid != "bad" else (None, http_error(404)),
    )
    ids = [str(i) for i in range(204)] + ["0", "bad"]
    results = tools._fetch_messages(service, ids)
    assert sizes == [100, 100, 6]
    assert [r["id"] for r in results] == ids
    assert sum(bool(r["ok"]) for r in results) == 205
    assert results[-1]["error"] == tools.ERROR_API


def test_bulk_modify_chunks_1000_and_reports_partial_failure(service):
    service.users().messages().batchModify.return_value.execute.side_effect = [{}, http_error(400), {}]
    ids = [str(i) for i in range(2001)]
    result = tools.mark_emails_read(ids)
    assert result["data"]["succeeded"] == 1001
    assert result["data"]["failed"] == 1000
    assert [
        len(call.kwargs["body"]["ids"]) for call in service.users().messages().batchModify.call_args_list
    ] == [1000, 1000, 1]


def test_custom_label_names_resolve_to_ids(service):
    service.users().labels().list.return_value.execute.return_value = {
        "labels": [{"id": "Label_1", "name": "Projects/Active"}]
    }
    assert tools.add_labels(["one"], "Projects/Active")["ok"]
    assert service.users().messages().batchModify.call_args.kwargs["body"]["addLabelIds"] == ["Label_1"]
    assert tools.add_label("one", "Projects/Active")["ok"]
    assert service.users().messages().modify.call_args.kwargs["body"]["addLabelIds"] == ["Label_1"]
    assert tools.add_labels(["one"], "Missing")["error"] == tools.ERROR_INVALID_LABEL


def test_download_batches_keep_order_and_never_overwrite(service):
    service.users().messages().attachments().get.side_effect = lambda **kwargs: kwargs["id"]
    sizes = install_batches(
        service, lambda aid: ({"data": base64.urlsafe_b64encode(aid.encode()).decode()}, None)
    )
    target = Path.home() / "download.bin"
    target.write_bytes(b"keep")
    items = [{"message_id": "msg", "attachment_id": str(i), "save_path": str(target)} for i in range(101)]
    items.insert(1, {"message_id": "invalid"})
    result = tools.download_attachments(items)["data"]
    assert sizes == [100, 1]
    assert result["succeeded"] == 101 and result["failed"] == 1
    assert not result["results"][1]["ok"]
    paths = [Path(r["data"]["path"]) for r in result["results"] if r["ok"]]
    assert len(set(paths)) == 101
    assert [p.read_bytes() for p in paths] == [str(i).encode() for i in range(101)]
    assert target.read_bytes() == b"keep"


@pytest.mark.parametrize("data", ["!!not-base64!!", "é"])
def test_bad_download_data_returns_error_without_writing(service, data):
    service.users().messages().attachments().get.return_value.execute.return_value = {"data": data}
    target = Path.home() / "invalid.txt"
    assert tools.download_attachment("msg", "att", str(target))["error"] == tools.ERROR_VALIDATION
    assert not target.exists()


def test_empty_attachment_is_valid_and_filename_matches_requested_part(service):
    msg = sample_message_payload()
    msg["payload"]["parts"] = [
        {"filename": "first.txt", "body": {"attachmentId": "first"}},
        {"filename": "second.txt", "body": {"attachmentId": "second"}},
    ]
    service.users().messages().get.return_value.execute.return_value = msg
    service.users().messages().attachments().get.return_value.execute.return_value = {"data": ""}
    result = tools.download_attachment("msg", "second", str(Path.home() / "file.txt"))
    assert result["data"]["filename"] == "second.txt"
    assert result["data"]["size"] == 0


def test_body_respects_charset_and_ignores_text_attachments():
    payload = {
        "parts": [
            {"filename": "attachment.txt", "mimeType": "text/plain", "body": {"data": "ZmlsZQ"}},
            {
                "mimeType": "text/plain",
                "parts": [],
                "headers": [{"name": "Content-Type", "value": "text/plain; charset=iso-8859-1"}],
                "body": {"data": base64.urlsafe_b64encode("café".encode("latin1")).decode()},
            },
        ]
    }
    assert tools._extract_body(payload) == ("café", None)


@pytest.mark.parametrize("fmt", ["md", "json", "jsonl", "csv"])
def test_exports_keep_full_body_headers_and_errors(fmt):
    body = "text | `value`\n" * 1000 + "THE END"
    results = [
        {"ok": True, "data": {"id": "msg", "subject": "A | B", "from": "a" * 100, "body": body}},
        tools.err("api_error", "read failed"),
    ]
    target = Path.home() / f"export.{fmt}"
    tools._save_results(results, target, fmt)
    content = target.read_text(encoding="utf-8")
    if fmt == "md":
        assert body in content and "read failed" in content
    else:
        if fmt == "json":
            records = json.loads(content)["results"]
        elif fmt == "jsonl":
            records = [json.loads(line) for line in content.splitlines()]
        else:
            with target.open(encoding="utf-8", newline="") as stream:
                records = list(csv.DictReader(stream))
        assert records[0]["body"] == body
        assert records[0]["from"] == "a" * 100
        assert records[0]["subject"] == "A | B"
        assert records[1]["message"] == "read failed"


@pytest.mark.parametrize("suffix", ["md", "json", "jsonl"])
def test_append_preserves_existing_content_and_full_new_body(service, monkeypatch, suffix):
    target = Path.home() / f"append.{suffix}"
    initial = (
        "Before\n---\nAFTER LAST DIVIDER\n"
        if suffix == "md"
        else ('{"results":[{"body":"existing"}],"total":1}' if suffix == "json" else '{"body":"existing"}')
    )
    target.write_text(initial, encoding="utf-8")
    body = "b" * 6000 + "END"
    monkeypatch.setattr(tools, "_fetch_messages", lambda *_: [{"ok": True, "data": {"body": body}}])
    assert tools.append_emails(["new"], str(target))["ok"]
    content = target.read_text(encoding="utf-8")
    if suffix == "md":
        assert content.startswith(initial) and body in content
    else:
        records = (
            json.loads(content)["results"]
            if suffix == "json"
            else [json.loads(line) for line in content.splitlines()]
        )
        assert [r["body"] for r in records] == ["existing", body]
        if suffix == "json":
            assert json.loads(content)["total"] == 2


def test_bad_json_append_never_changes_file(service, monkeypatch):
    monkeypatch.setattr(tools, "_fetch_messages", lambda *_: [])
    target = Path.home() / "bad.json"
    target.write_text("{invalid", encoding="utf-8")
    assert tools.append_emails(["msg"], str(target))["error"] == tools.ERROR_VALIDATION
    assert target.read_text(encoding="utf-8") == "{invalid"


def test_search_paginates_without_exceeding_requested_limit(service, monkeypatch):
    service.users().messages().list.return_value.execute.side_effect = [
        {
            "messages": [{"id": str(i)} for i in range(500)],
            "nextPageToken": "next",
            "resultSizeEstimate": 600,
        },
        {"messages": [{"id": "500"}, {"id": "501"}]},
    ]
    fetch = MagicMock(return_value=[])
    monkeypatch.setattr(tools, "_fetch_messages", fetch)
    assert tools.search_and_save("query", 501, str(Path.home() / "search.json"), "json")["ok"]
    assert len(fetch.call_args.args[1]) == 501
    assert service.users().messages().list.call_args.kwargs["maxResults"] == 1
    assert service.users().messages().list.call_args.kwargs["pageToken"] == "next"


def test_single_large_message_auto_saves_based_on_actual_size(service, monkeypatch):
    monkeypatch.setattr(tools, "_fetch_messages", lambda *_: [{"ok": True, "data": {"body": "x" * 40000}}])
    result = tools.read_emails(["msg"])
    assert result["data"]["saved"]
    assert "x" * 40000 in Path(result["data"]["path"]).read_text(encoding="utf-8")


@pytest.mark.parametrize("value", [0, -1, 501, True, 2.5])
@pytest.mark.parametrize("operation", [tools.list_emails, tools.list_drafts])
def test_invalid_list_limits_do_not_authenticate(monkeypatch, value, operation):
    get_service = MagicMock()
    monkeypatch.setattr(tools, "_get_service", get_service)
    assert operation(max_results=value)["error"] == tools.ERROR_VALIDATION
    get_service.assert_not_called()


@pytest.mark.parametrize("max_retries,delay", [(0, 1), (-1, 1), (2, -1), (2, float("nan"))])
def test_retry_rejects_invalid_settings(max_retries, delay):
    with pytest.raises(ValueError):
        tools._with_retry(lambda: None, max_retries, delay)


@pytest.mark.parametrize("header", ["5", "-1", "nan", "junk"])
def test_retry_delay_is_bounded_and_respects_valid_header(monkeypatch, header):
    sleep = MagicMock()
    monkeypatch.setattr(tools.time, "sleep", sleep)
    call = MagicMock(side_effect=[http_error(429, header), "ok"])
    assert tools._with_retry(call) == "ok"
    delay = sleep.call_args.args[0]
    assert 0 <= delay <= 60
    if header == "5":
        assert delay >= 5


def test_retry_does_not_sleep_or_retry_past_large_server_delay(monkeypatch):
    sleep = MagicMock()
    monkeypatch.setattr(tools.time, "sleep", sleep)
    call = MagicMock(side_effect=http_error(429, "600"))
    with pytest.raises(HttpError):
        tools._with_retry(call)
    assert call.call_count == 1
    sleep.assert_not_called()


def test_concurrent_appends_preserve_every_message(service, monkeypatch):
    target = Path.home() / "concurrent.json"
    target.write_text('{"results":[],"total":0}', encoding="utf-8")
    monkeypatch.setattr(tools, "_fetch_messages", lambda _, ids: [{"ok": True, "data": {"id": ids[0]}}])
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda i: tools.append_emails([str(i)], str(target)), range(12)))
    assert all(result["ok"] for result in results)
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["total"] == 12
    assert {record["id"] for record in data["results"]} == {str(i) for i in range(12)}


def test_draft_body_edit_keeps_nested_inline_image(service):
    original = EmailMessage()
    original["To"] = "a@example.com"
    original.set_content("plain")
    original.add_alternative('<img src="cid:picture">', subtype="html")
    original.get_payload()[1].add_related(
        b"image bytes", maintype="image", subtype="png", cid="<picture>", disposition="inline"
    )
    service.users().drafts().get.return_value.execute.return_value = {
        "message": {"raw": base64.urlsafe_b64encode(original.as_bytes()).decode()}
    }
    assert tools.update_draft("draft", body="updated body")["ok"]
    payload = service.users().drafts().update.call_args.kwargs["body"]["message"]
    edited = decode_message(payload["raw"])
    assert any(
        part.get("Content-ID") == "<picture>" and part.get_payload(decode=True) == b"image bytes"
        for part in edited.walk()
    )


@pytest.mark.parametrize("batch", [False, True])
def test_large_text_body_fetched_by_attachment_id(service, batch):
    msg = sample_message_payload()
    msg["payload"]["body"] = {"attachmentId": "large-body"}
    service.users().messages().attachments().get.return_value.execute.return_value = {
        "data": base64.urlsafe_b64encode(b"FULL LARGE BODY").decode()
    }
    service.users().messages().get.return_value.execute.return_value = msg
    if batch:
        install_batches(service, lambda _: (msg, None))
        result = tools._fetch_messages(service, ["msg_001"])[0]
    else:
        result = tools.read_email("msg_001")
    assert result["data"]["body"] == "FULL LARGE BODY"
    assert service.users().messages().attachments().get.call_args.kwargs["messageId"] == "msg_001"


def test_search_does_not_overwrite_existing_file_or_authenticate(monkeypatch):
    target = Path.home() / "existing.md"
    target.write_text("keep me", encoding="utf-8")
    auth = MagicMock()
    monkeypatch.setattr(tools, "_get_service", auth)
    assert tools.search_and_save("query", save_path=str(target))["error"] == tools.ERROR_VALIDATION
    assert target.read_text(encoding="utf-8") == "keep me"
    auth.assert_not_called()


def test_atomic_export_failure_keeps_previous_content(monkeypatch):
    target = Path.home() / "export.json"
    target.write_text("keep me", encoding="utf-8")
    monkeypatch.setattr(tools.os, "replace", MagicMock(side_effect=OSError("disk error")))
    with pytest.raises(OSError):
        tools._save_results([], target, "json")
    assert target.read_text(encoding="utf-8") == "keep me"
    assert list(target.parent.glob("*.tmp")) == []


def test_download_size_limit_is_enforced_before_writing(service, monkeypatch):
    monkeypatch.setattr(tools, "MAX_ATTACHMENT_BYTES", 3)
    service.users().messages().attachments().get.return_value.execute.return_value = {
        "data": base64.urlsafe_b64encode(b"four").decode()
    }
    target = Path.home() / "oversize.bin"
    assert tools.download_attachment("msg", "att", str(target))["error"] == tools.ERROR_VALIDATION
    assert not target.exists()
