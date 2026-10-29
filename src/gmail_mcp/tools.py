import base64
import json
import logging
import math
import mimetypes
import os
import threading
import time
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path
from typing import Any

from googleapiclient.errors import HttpError

from .auth import AuthError, get_gmail_service
from .config import config

logger = logging.getLogger(__name__)
_FILE_LOCK = threading.RLock()

# ── Error codes ──────────────────────────────────────────────────────────────
ERROR_AUTH_MISSING = "auth_missing"
ERROR_AUTH_EXPIRED = "auth_expired"
ERROR_API = "api_error"
ERROR_NOT_FOUND = "not_found"
ERROR_VALIDATION = "validation_error"
ERROR_ATTACHMENT_NOT_FOUND = "attachment_not_found"
ERROR_FILE_NOT_FOUND = "file_not_found"
ERROR_DRAFT_NOT_FOUND = "draft_not_found"
ERROR_INVALID_LABEL = "invalid_label"
ERROR_THREAD_NOT_FOUND = "thread_not_found"
ERROR_INTERNAL = "internal_error"


# ── Response helpers ─────────────────────────────────────────────────────────
def ok(data: Any) -> dict:
    return {"ok": True, "data": data}


def err(code: str, message: str) -> dict:
    return {"ok": False, "error": code, "message": message}


# ── Retry helper ─────────────────────────────────────────────────────────────
def _with_retry(fn, max_retries: int = 3, base_delay: float = 1.0):
    """Bounded retries for safe operations; callers disable retries for creates/sends."""
    import random
    from email.utils import parsedate_to_datetime

    if type(max_retries) is not int or max_retries < 1:
        raise ValueError("max_retries must be a positive integer")
    if not math.isfinite(base_delay) or base_delay < 0:
        raise ValueError("base_delay must be finite and nonnegative")
    for attempt in range(max_retries):
        try:
            return fn()
        except (HttpError, TimeoutError, ConnectionError, OSError) as exc:
            if isinstance(exc, HttpError) and exc.resp.status not in (429, 503):
                raise
            if attempt == max_retries - 1:
                raise
            delay = min(60.0, base_delay * (2 ** min(attempt, 10)))
            if isinstance(exc, HttpError) and (value := exc.resp.get("retry-after")):
                try:
                    try:
                        minimum = float(value)
                    except ValueError:
                        minimum = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
                    if math.isfinite(minimum) and minimum >= 0:
                        if minimum > 60:
                            raise exc
                        delay = minimum
                except (ValueError, TypeError, OverflowError):
                    pass
            delay = min(60.0, delay * (1 + 0.2 * random.random()))
            logger.warning("Transient API error; retrying in %.1fs", delay)
            time.sleep(delay)
    raise RuntimeError("Unreachable retry state")


# ── Helper functions ──────────────────────────────────────────────────────────
def _parse_recipients(header_value: str) -> list[str]:
    from email.utils import getaddresses

    return list(
        dict.fromkeys(address for _, address in getaddresses([header_value]) if address and "@" in address)
    )


def _headers(payload: dict) -> dict[str, str]:
    return {h["name"].lower(): h["value"] for h in payload.get("headers", [])}


def _attachment_id_to_part(payload: dict, attachment_id: str) -> dict | None:
    """Recursively find the message part with matching body.attachmentId."""
    if payload.get("body", {}).get("attachmentId") == attachment_id:
        return payload
    for part in payload.get("parts", []):
        found = _attachment_id_to_part(part, attachment_id)
        if found:
            return found
    return None


def _extract_body(payload: dict) -> tuple[str | None, str | None]:
    """Decode message text using its declared charset, excluding file attachments."""
    if payload.get("filename") or _headers(payload).get("content-disposition", "").lower().startswith(
        "attachment"
    ):
        return None, None
    plain, html = None, None
    for part in payload.get("parts", []):
        p, h = _extract_body(part)
        plain = plain if plain is not None else p
        html = html if html is not None else h
    data = payload.get("body", {}).get("data")
    mime = payload.get("mimeType", "").lower()
    if data is not None and mime in ("text/plain", "text/html"):
        from email.message import Message

        header = Message()
        header["Content-Type"] = _headers(payload).get("content-type", mime)
        charset = header.get_content_charset() or "utf-8"
        raw = base64.b64decode(data + "=" * (-len(data) % 4), altchars=b"-_", validate=True)
        try:
            decoded = raw.decode(charset, errors="replace")
        except LookupError:
            decoded = raw.decode("utf-8", errors="replace")
        if mime == "text/plain":
            plain = decoded
        else:
            html = decoded
    return plain, html


def _prepare_body(service, msg: dict) -> None:
    """Retrieve text bodies Gmail stores separately, without downloading attached files."""

    def hydrate(payload):
        if payload.get("filename") or _headers(payload).get("content-disposition", "").lower().startswith(
            "attachment"
        ):
            return
        body = payload.get("body", {})
        if (
            payload.get("mimeType") in ("text/plain", "text/html")
            and "data" not in body
            and body.get("attachmentId")
        ):
            response = _with_retry(
                lambda: (
                    service.users()
                    .messages()
                    .attachments()
                    .get(userId="me", messageId=msg["id"], id=body["attachmentId"])
                    .execute()
                )
            )
            body["data"] = response["data"]
        for part in payload.get("parts", []):
            hydrate(part)

    hydrate(msg.get("payload", {}))


def _safe_save_path(requested: str, config_dir: Path) -> Path:
    """
    Resolve and validate a save path against path traversal and write permissions.
    Returns the safe absolute path, or raises ValueError with a clear message.
    """
    # Resolve to absolute path
    path = Path(requested).expanduser().resolve()
    config_dir = config_dir.expanduser().resolve()

    # Guard against path traversal: disallow paths outside config dir or home
    home = Path.home().resolve()
    try:
        path.relative_to(config_dir)
        safe = True
    except ValueError:
        try:
            path.relative_to(home)
            safe = True
        except ValueError:
            safe = False

    if not safe:
        raise ValueError(
            f"Save path '{requested}' is outside the allowed directory. "
            f"Paths must be inside '{config_dir}' or your home directory."
        )

    # Check parent directory exists and is writable
    parent = path.parent
    if not parent.exists():
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise ValueError(f"Cannot create directory '{parent}': {e}") from e

    if parent.exists() and not os.access(parent, os.W_OK):
        raise ValueError(f"Directory '{parent}' is not writable.")

    return path


def _unique_save_path(path: Path) -> Path:
    """If path exists, append a counter suffix before the extension."""
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    n = 1
    while path.exists():
        path = parent / f"{stem} ({n}){suffix}"
        n += 1
    return path


MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
MAX_MESSAGE_BYTES = 35 * 1024 * 1024


def _build_mime_message(
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    bcc: str = "",
    attachments: list[str] | None = None,
    in_reply_to: str = "",
    references: str = "",
    sender: str = "",
) -> tuple[str, str]:
    """Build an RFC-compliant message; attachments have a combined 25 MiB limit."""
    msg = EmailMessage()
    for key, value in (
        ("To", to),
        ("Subject", subject),
        ("Cc", cc),
        ("Bcc", bcc),
        ("From", sender),
        ("In-Reply-To", in_reply_to),
        ("References", references),
    ):
        if value or key in ("To", "Subject"):
            msg[key] = value
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(body)
    total = 0
    for filepath in attachments or []:
        if not os.path.isfile(filepath):
            raise FileNotFoundError(f"Attachment file not found: {filepath}")
        with open(filepath, "rb") as stream:
            data = stream.read(MAX_ATTACHMENT_BYTES + 1)
        total += len(data)
        if total > MAX_ATTACHMENT_BYTES:
            raise ValueError("Attachments exceed the combined 25 MiB limit")
        mime = mimetypes.guess_type(filepath)[0] or "application/octet-stream"
        main, sub = mime.split("/", 1)
        msg.add_attachment(data, maintype=main, subtype=sub, filename=os.path.basename(filepath))
    mime_bytes = msg.as_bytes()
    if len(mime_bytes) > MAX_MESSAGE_BYTES:
        raise ValueError("MIME message exceeds the 35 MiB limit")
    encoded = base64.urlsafe_b64encode(mime_bytes).decode("ascii").rstrip("=")
    return encoded, sender


def _decode_attachment(encoded: str) -> bytes:
    if not isinstance(encoded, str):
        raise ValueError("Attachment data must be base64 text")
    if len(encoded) > ((MAX_ATTACHMENT_BYTES + 2) // 3) * 4:
        raise ValueError("Attachment exceeds the 25 MiB limit")
    try:
        data = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Invalid base64 attachment data") from exc
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise ValueError("Attachment exceeds the 25 MiB limit")
    return data


def _write_attachment(path: Path, data: bytes) -> Path:
    """Reserve the filename exclusively; never overwrite a competing download."""
    while True:
        target = _unique_save_path(path)
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return target


def _atomic_write(path: Path, content: str) -> None:
    import tempfile

    fd, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors="replace", newline="") as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _get_service():
    """Get the Gmail service, surfacing auth errors clearly."""
    try:
        return get_gmail_service()
    except AuthError:
        raise  # Let tools catch and return auth_missing


# ── Tool: list_emails (batched — single API call) ────────────────────────────
def list_emails(query: str = "", max_results: int = 5) -> dict:
    """
    Search inbox for emails matching a query string.

    Returns a list of emails with their IDs, subjects, from addresses, dates,
    snippets, and labels. IDs can be passed to read_email or reply_email.

    Required: none — both params have defaults.
    Optional:
      query (str, default="") — Gmail search syntax.
        Examples: "from:alice@example.com", "subject:invoice is:unread",
        "after:2026/01/01 before:2026/01/31", "has:attachment".
        Leave empty to get most recent inbox emails.
      max_results (int, default=5) — how many emails to return. Max 500.

    Returns on success:
      {"total": N, "emails": [{"id": "...", "subject": "...", "from": "...",
         "date": "...", "snippet": "...", "labels": [...]}]}

    Workflow note: The "id" field in each email is a Gmail message ID —
      pass it to read_email, reply_email, or get_thread_messages.
      Leave query empty to get recent inbox. Use Gmail search syntax
      to filter: from:, subject:, after:, before:, is:unread, has:attachment.
    """
    if type(max_results) is not int or not 1 <= max_results <= 500:
        return err(ERROR_VALIDATION, "max_results must be an integer between 1 and 500")

    try:
        service = _get_service()
    except AuthError as e:
        return err(ERROR_AUTH_MISSING, str(e))
    except Exception as e:
        logger.exception("Unexpected error obtaining Gmail service")
        return err(ERROR_INTERNAL, str(e))

    try:
        response = _with_retry(
            lambda: service.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
        )
    except HttpError as e:
        return err(ERROR_API, str(e))
    except Exception as e:
        return err(ERROR_API, str(e))

    messages = response.get("messages", [])
    emails = []

    for msg_ref in messages:
        msg_id = msg_ref["id"]
        try:
            msg = _with_retry(
                lambda mid=msg_id: (
                    service.users()
                    .messages()
                    .get(userId="me", id=mid, format="metadata", metadataHeaders=["Subject", "From", "Date"])
                    .execute()
                )
            )
        except HttpError as e:
            logger.debug("list_emails: skipping message %s due to HttpError: %s", msg_id, e)
            continue
        except Exception as e:
            logger.debug("list_emails: skipping message %s due to error: %s", msg_id, e)
            continue

        headers = _headers(msg.get("payload", {}))
        emails.append(
            {
                "id": msg_id,
                "thread_id": msg.get("threadId", ""),
                "subject": headers.get("subject", "(no subject)"),
                "from": headers.get("from", "unknown"),
                "date": headers.get("date", ""),
                "snippet": msg.get("snippet", ""),
                "labels": msg.get("labelIds", []),
            }
        )

    return ok(
        {
            "total": int(response.get("resultSizeEstimate", len(emails))),
            "emails": emails,
        }
    )


# ── Tool: get_thread_messages ───────────────────────────────────────────────
def get_thread_messages(thread_id: str) -> dict:
    """
    List all messages in a thread by thread ID.

    Returns message IDs, subjects, from addresses, dates, and snippets.
    Use this to find the Gmail message ID needed by reply_email.

    Required:
      thread_id (str) — the Gmail thread ID. Get this from list_emails
        or read_email (look for "thread_id" in responses).

    Returns on success:
      {"thread_id": "...", "total": N, "messages": [
        {"id": "...", "subject": "...", "from": "...", "date": "...", "snippet": "..."}
      ]}

    Workflow note: reply_email requires a Gmail message ID in the in_reply_to
      parameter — NOT an email Message-ID header. Use get_thread_messages
      to find the right message_id first. Example flow:
        1. list_emails(query="subject:my thread") → get thread_id
        2. get_thread_messages(thread_id) → get message_id
        3. reply_email(thread_id=..., in_reply_to=message_id, ...)
    """
    if not thread_id or not thread_id.strip():
        return err(ERROR_VALIDATION, "thread_id cannot be empty")

    try:
        service = _get_service()
    except AuthError as e:
        return err(ERROR_AUTH_MISSING, str(e))
    except Exception as e:
        logger.exception("Unexpected error obtaining Gmail service")
        return err(ERROR_INTERNAL, str(e))

    try:
        thread = _with_retry(lambda: service.users().threads().get(userId="me", id=thread_id).execute())
    except HttpError as e:
        if e.resp.status == 404:
            return err(ERROR_THREAD_NOT_FOUND, f"Thread not found: {thread_id}")
        return err(ERROR_API, str(e))
    except Exception as e:
        return err(ERROR_API, str(e))

    messages_out = []
    for msg in thread.get("messages", []):
        headers = _headers(msg.get("payload", {}))
        plain, _ = _extract_body(msg.get("payload", {}))
        messages_out.append(
            {
                "id": msg["id"],
                "subject": headers.get("subject", "(no subject)"),
                "from": headers.get("from", "unknown"),
                "date": headers.get("date", ""),
                "snippet": plain or msg.get("snippet", ""),
            }
        )

    return ok(
        {
            "thread_id": thread_id,
            "total": len(messages_out),
            "messages": messages_out,
        }
    )


# ── Tool: send_email ────────────────────────────────────────────────────────
def send_email(
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    bcc: str = "",
    thread_id: str = "",
    in_reply_to: str = "",
    references: str = "",
    attachments: list[str] | None = None,
) -> dict:
    """
    Send a new email. Can thread into an existing conversation by including
    thread_id and in_reply_to/references headers. Supports CC, BCC, and
    file attachments.

    Required:
      to (str) — recipient email address.
      subject (str) — email subject line.
      body (str) — email body text.

    Optional:
      cc (str, default="") — CC recipients, comma-separated.
      bcc (str, default="") — BCC recipients, comma-separated.
      thread_id (str, default="") — Gmail thread ID to reply within.
        If provided with in_reply_to and references, the email threads
        into the existing conversation.
      in_reply_to (str, default="") — the Message-ID header of the
        message being replied to. Required for threading.
      references (str, default="") — References header for threading.
        Usually same as in_reply_to.
      attachments (list[str] | None, default=None) — absolute file paths
        to files to attach. Paths must be absolute or relative to cwd.

    Returns on success:
      {"message_id": "...", "thread_id": "...", "to": "...", "subject": "..."}

    Workflow note: For threading, you need both thread_id and in_reply_to.
      If you only have a thread_id but not in_reply_to, use
      get_thread_messages(thread_id) first to find a message_id.
      Attachments: paths must exist and be under 25MB.
      The body parameter is the email text — not a file path.
    """
    if attachments is None:
        attachments = []

    if not to or not to.strip():
        return err(ERROR_VALIDATION, "recipient 'to' cannot be empty")

    try:
        service = _get_service()
    except AuthError as e:
        return err(ERROR_AUTH_MISSING, str(e))
    except Exception as e:
        logger.exception("Unexpected error obtaining Gmail service")
        return err(ERROR_INTERNAL, str(e))

    try:
        profile = _with_retry(lambda: service.users().getProfile(userId="me").execute())
        sender = profile.get("emailAddress", "")
    except HttpError as e:
        return err(ERROR_API, f"Failed to get sender profile: {e}")
    except Exception as e:
        return err(ERROR_API, f"Failed to get sender profile: {e}")

    try:
        encoded, _ = _build_mime_message(
            to=to,
            subject=subject,
            body=body,
            cc=cc,
            bcc=bcc,
            attachments=attachments,
            in_reply_to=in_reply_to,
            references=references,
            sender=sender,
        )
    except OSError as e:
        return err(ERROR_FILE_NOT_FOUND, str(e))
    except ValueError as e:
        return err(ERROR_VALIDATION, str(e))

    send_body = {"raw": encoded}
    if thread_id:
        send_body["threadId"] = thread_id

    try:
        sent = _with_retry(
            lambda: service.users().messages().send(userId="me", body=send_body).execute(), max_retries=1
        )
    except HttpError as e:
        return err(
            ERROR_API,
            "Email delivery could not be confirmed. Check Gmail before retrying to avoid duplicates. Details: "
            + str(e),
        )
    except Exception as e:
        return err(
            ERROR_API,
            "Email delivery could not be confirmed. Check Gmail before retrying to avoid duplicates. Details: "
            + str(e),
        )

    return ok(
        {
            "message_id": sent.get("id", ""),
            "thread_id": sent.get("threadId", ""),
            "to": to,
            "subject": subject,
        }
    )


# ── Tool: reply_email ────────────────────────────────────────────────────────
def reply_email(
    to: str,
    body: str,
    thread_id: str,
    in_reply_to: str,
    subject: str = "",
    cc: str = "",
    bcc: str = "",
    reply_all: bool = False,
) -> dict:
    """
    Convenience wrapper to reply to an existing thread.

    Automatically prepends "Re: " to the subject if missing.
    Can CC all original recipients with reply_all=True.

    Required:
      to (str) — recipient address.
      body (str) — reply text.
      thread_id (str) — Gmail thread ID to reply within.
      in_reply_to (str) — Gmail message ID being replied to.
        GET THIS FROM get_thread_messages(thread_id) — do not guess.

    Optional:
      subject (str, default="") — if empty, "Re: " is prepended to the
        original message's subject.
      cc (str, default="") — additional CC recipients.
      bcc (str, default="") — BCC recipients.
      reply_all (bool, default=False) — if True, auto-CCs all other
        recipients from the original message (To and CC headers).

    Returns on success:
      {"message_id": "...", "thread_id": "...", "to": "...", "subject": "..."}

    Workflow note: CRITICAL — in_reply_to must be a Gmail message ID
      (e.g. "19e09c7f2e8d487d"), NOT an email Message-ID header
      (e.g. "<CAC4dBmc...@mail.gmail.com>"). To find the right ID:
        1. Call get_thread_messages(thread_id)
        2. Pick the message ID from the "messages" array
        3. Pass that to reply_email's in_reply_to parameter
    """
    if not to or not to.strip():
        return err(ERROR_VALIDATION, "recipient 'to' cannot be empty")

    if not thread_id or not in_reply_to:
        return err(ERROR_VALIDATION, "thread_id and in_reply_to are required for reply_email")

    actual_cc = cc
    actual_subject = subject

    try:
        service = _get_service()
    except AuthError as e:
        return err(ERROR_AUTH_MISSING, str(e))
    except Exception as e:
        logger.exception("Unexpected error obtaining Gmail service")
        return err(ERROR_INTERNAL, str(e))

    try:
        original = _with_retry(
            lambda: (
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=in_reply_to,
                    format="metadata",
                    metadataHeaders=["Subject", "From", "To", "Cc", "Message-ID", "References"],
                )
                .execute()
            )
        )
    except HttpError as e:
        if e.resp.status == 404:
            return err(ERROR_NOT_FOUND, f"Original message not found: {in_reply_to}")
        return err(ERROR_API, str(e))
    except Exception as e:
        return err(ERROR_API, str(e))

    headers = _headers(original.get("payload", {}))

    if original.get("threadId") and original["threadId"] != thread_id:
        return err(ERROR_VALIDATION, "Original message does not belong to the supplied thread")
    rfc_message_id = headers.get("message-id", "")
    if not rfc_message_id:
        return err(
            ERROR_VALIDATION,
            "Original message has no Message-ID header; a threaded reply cannot be constructed",
        )
    references_header = " ".join(filter(None, (headers.get("references", ""), rfc_message_id)))
    own_address = ""
    if reply_all:
        try:
            own_address = (
                _with_retry(lambda: service.users().getProfile(userId="me").execute())
                .get("emailAddress", "")
                .lower()
            )
        except Exception as exc:
            return err(ERROR_API, f"Could not determine reply-all sender: {exc}")

    if not actual_subject:
        original_subject = headers.get("subject", "")
        if original_subject.lower().startswith("re:"):
            actual_subject = original_subject
        else:
            actual_subject = f"Re: {original_subject}"

    if reply_all:
        extra_ccs: list[str] = []
        for hdr_val in [cc, headers.get("to", ""), headers.get("cc", "")]:
            if hdr_val:
                for r in _parse_recipients(hdr_val):
                    if r.lower() not in {a.lower() for a in _parse_recipients(to)} | {
                        own_address
                    } and r.lower() not in {a.lower() for a in extra_ccs}:
                        extra_ccs.append(r)
        if extra_ccs:
            actual_cc = ", ".join(extra_ccs)

    return send_email(
        to=to,
        subject=actual_subject,
        body=body,
        cc=actual_cc,
        bcc=bcc,
        thread_id=thread_id,
        in_reply_to=rfc_message_id,
        references=references_header,
    )


# ── Tool: read_email ────────────────────────────────────────────────────────
def read_email(message_id: str) -> dict:
    """
    Fetch the full content of an email by its Gmail message ID.

    Returns subject, from, to, date, body (plain text), html_body, and labels.

    Required:
      message_id (str) — the Gmail message ID from list_emails or
        get_thread_messages. NOT an email Message-ID header.

    Returns on success:
      {"id": "...", "subject": "...", "from": "...", "to": "...",
       "date": "...", "body": "plain text...", "html_body": "HTML...",
       "labels": ["INBOX", "IMPORTANT"]}

    Workflow note: body may be None if the email is HTML-only.
      html_body will be present for HTML emails. Labels include system
      labels like INBOX, SENT, TRASH, UNREAD and custom labels.
      Use list_attachments(message_id) to find downloadable files.
    """
    if not message_id or not message_id.strip():
        return err(ERROR_VALIDATION, "message_id cannot be empty")

    try:
        service = _get_service()
    except AuthError as e:
        return err(ERROR_AUTH_MISSING, str(e))
    except Exception as e:
        logger.exception("Unexpected error obtaining Gmail service")
        return err(ERROR_INTERNAL, str(e))

    try:
        msg = _with_retry(
            lambda: service.users().messages().get(userId="me", id=message_id, format="full").execute()
        )
    except HttpError as e:
        if e.resp.status == 404:
            return err(ERROR_NOT_FOUND, f"Message not found: {message_id}")
        return err(ERROR_API, str(e))
    except Exception as e:
        return err(ERROR_API, str(e))

    headers = _headers(msg.get("payload", {}))
    try:
        _prepare_body(service, msg)
        plain_body, html_body = _extract_body(msg.get("payload", {}))
    except Exception as exc:
        return err(ERROR_API, f"Invalid message body: {exc}")

    internal_date = msg.get("internalDate", "")
    if internal_date:
        try:
            date_str = formatdate(int(internal_date) / 1000, usegmt=True)
        except Exception:
            date_str = internal_date
    else:
        date_str = headers.get("date", "")

    return ok(
        {
            "id": message_id,
            "subject": headers.get("subject", "(no subject)"),
            "from": headers.get("from", "unknown"),
            "to": headers.get("to", "unknown"),
            "cc": headers.get("cc", ""),
            "thread_id": msg.get("threadId", ""),
            "date": date_str,
            "body": plain_body,
            "html_body": html_body,
            "labels": msg.get("labelIds", []),
        }
    )


# ── Label helpers ────────────────────────────────────────────────────────────
def _modify_labels(message_id: str, add: list[str], remove: list[str]) -> dict:
    """Shared implementation for label modification. Returns updated labels."""
    if not message_id or not message_id.strip():
        return err(ERROR_VALIDATION, "message_id cannot be empty")

    try:
        service = _get_service()
    except AuthError as e:
        return err(ERROR_AUTH_MISSING, str(e))
    except Exception as e:
        logger.exception("Unexpected error obtaining Gmail service")
        return err(ERROR_INTERNAL, str(e))

    try:
        add = [_resolve_label(service, label) for label in add]
        remove = [_resolve_label(service, label) for label in remove]
    except ValueError as exc:
        return err(ERROR_INVALID_LABEL, str(exc))
    except Exception as exc:
        return err(ERROR_API, str(exc))
    body = {"addLabelIds": add, "removeLabelIds": remove}
    try:
        updated = _with_retry(
            lambda: service.users().messages().modify(userId="me", id=message_id, body=body).execute()
        )
    except HttpError as e:
        if e.resp.status == 404:
            return err(ERROR_NOT_FOUND, f"Message not found: {message_id}")
        return err(ERROR_API, str(e))
    except Exception as e:
        return err(ERROR_API, str(e))

    return ok(
        {
            "message_id": message_id,
            "labels": updated.get("labelIds", []),
        }
    )


def archive_email(message_id: str) -> dict:
    """
    Archive an email — removes it from INBOX without deleting it.
    Archived emails are still searchable and accessible via search.

    Required:
      message_id (str) — Gmail message ID from list_emails.

    Returns on success:
      {"message_id": "...", "labels": ["IMPORTANT", "SENT"]}}
      (INBOX is removed from labels)

    Workflow note: This is non-destructive. The email stays in Gmail
      with all other labels intact. Use list_emails(query="is:inbox")
      to confirm it's gone from the inbox view.
    """
    return _modify_labels(message_id, add=[], remove=["INBOX"])


def trash_email(message_id: str) -> dict:
    """
    Move an email to trash. Emails in trash are auto-deleted after 30 days.

    Required:
      message_id (str) — Gmail message ID from list_emails.

    Returns on success:
      {"message_id": "...", "labels": [..., "TRASH"]}}

    Workflow note: To permanently delete, trash the email first —
      Gmail auto-purges trash after 30 days.
    """
    return _modify_labels(message_id, add=["TRASH"], remove=[])


def mark_read(message_id: str) -> dict:
    """
    Mark an email as read — removes the UNREAD label.

    Required:
      message_id (str) — Gmail message ID from list_emails.

    Returns on success:
      {"message_id": "...", "labels": ["INBOX", "IMPORTANT"]}}
      (UNREAD is removed from labels)

    Workflow note: Use this after reading an important email to
      clear the unread indicator.
    """
    return _modify_labels(message_id, add=[], remove=["UNREAD"])


def add_label(message_id: str, label: str) -> dict:
    """
    Add a label (system or custom) to an email.

    Required:
      message_id (str) — Gmail message ID.
      label (str) — label name. System labels: STARRED, IMPORTANT,
        SENT, DRAFT, TRASH, SPAM. Custom labels: any name you've
        created in Gmail settings.

    Returns on success:
      {"message_id": "...", "labels": ["INBOX", "IMPORTANT", "..."]}}

    Workflow note: Label names are case-sensitive in Gmail.
      "STARRED" works, "starred" may not. Custom label names
      must exactly match how they appear in Gmail.
    """
    if not label or not label.strip():
        return err(ERROR_VALIDATION, "label cannot be empty")
    return _modify_labels(message_id, add=[label], remove=[])


def remove_label(message_id: str, label: str) -> dict:
    """
    Remove a label from an email.

    Required:
      message_id (str) — Gmail message ID.
      label (str) — label name to remove.

    Returns on success:
      {"message_id": "...", "labels": ["INBOX", "IMPORTANT"]}}
      (the label is removed from labels array)

    Workflow note: Removes the label from the message's label set.
      No error if the label was not present.
    """
    if not label or not label.strip():
        return err(ERROR_VALIDATION, "label cannot be empty")
    return _modify_labels(message_id, add=[], remove=[label])


# ── Tool: list_attachments ───────────────────────────────────────────────────
def list_attachments(message_id: str) -> dict:
    """
    List all file attachments in an email with their IDs, filenames,
    MIME types, and sizes.

    Required:
      message_id (str) — Gmail message ID from list_emails.

    Returns on success:
      {"attachments": [
        {"id": "...", "filename": "report.pdf",
         "mime_type": "application/pdf", "size": 204800}
      ]}

    Workflow note: The attachment "id" field is a Gmail attachment ID —
      pass it to download_attachment along with the message_id and
      a save_path to download the file. Returns empty list if the
      email has no attachments.
    """
    if not message_id or not message_id.strip():
        return err(ERROR_VALIDATION, "message_id cannot be empty")

    try:
        service = _get_service()
    except AuthError as e:
        return err(ERROR_AUTH_MISSING, str(e))
    except Exception as e:
        logger.exception("Unexpected error obtaining Gmail service")
        return err(ERROR_INTERNAL, str(e))

    try:
        msg = _with_retry(
            lambda: service.users().messages().get(userId="me", id=message_id, format="full").execute()
        )
    except HttpError as e:
        if e.resp.status == 404:
            return err(ERROR_NOT_FOUND, f"Message not found: {message_id}")
        return err(ERROR_API, str(e))
    except Exception as e:
        return err(ERROR_API, str(e))

    attachments = []

    def _collect_parts(payload: dict) -> None:
        """
        Recursively collect all attachment parts from a message payload.
        Handles nested multipart structures by recursing into 'parts'.
        """
        if "parts" in payload:
            for p in payload["parts"]:
                _collect_parts(p)
        else:
            filename = payload.get("filename")
            attachment_id = payload.get("body", {}).get("attachmentId")
            if filename and attachment_id:
                attachments.append(
                    {
                        "id": attachment_id,
                        "filename": filename,
                        "mime_type": payload.get("mimeType", "application/octet-stream"),
                        "size": int(payload.get("body", {}).get("size", 0)),
                    }
                )

    _collect_parts(msg.get("payload", {}))

    return ok({"attachments": attachments})


# ── Tool: download_attachment ────────────────────────────────────────────────
def download_attachment(
    message_id: str,
    attachment_id: str,
    save_path: str,
) -> dict:
    """
    Download a file attachment from an email and save it to a local path.

    Required:
      message_id (str) — Gmail message ID from list_emails.
      attachment_id (str) — attachment ID from list_attachments.
      save_path (str) — local file path to save to. Must be inside
        your home directory or the gmail-mcp config directory.
        Parent directories are created automatically.
        If the file already exists, a counter suffix is added
        (e.g. "report (1).pdf") to avoid overwriting.

    Returns on success:
      {"filename": "report.pdf", "path": "C:/Users/.../report.pdf",
       "size": 204800}

    Workflow note: Paths are validated — path traversal attempts
      (e.g. "../../etc/passwd") are rejected. Use absolute paths
      or paths relative to your home directory. Supported file
      types: any MIME type. Max attachment size: 25MB.
    """
    if not message_id or not message_id.strip():
        return err(ERROR_VALIDATION, "message_id cannot be empty")
    if not attachment_id or not attachment_id.strip():
        return err(ERROR_VALIDATION, "attachment_id cannot be empty")
    if not save_path or not save_path.strip():
        return err(ERROR_VALIDATION, "save_path cannot be empty")

    try:
        service = _get_service()
    except AuthError as e:
        return err(ERROR_AUTH_MISSING, str(e))
    except Exception as e:
        logger.exception("Unexpected error obtaining Gmail service")
        return err(ERROR_INTERNAL, str(e))

    filename = "unknown"
    try:
        msg = _with_retry(
            lambda: service.users().messages().get(userId="me", id=message_id, format="full").execute()
        )
        part = _attachment_id_to_part(msg.get("payload", {}), attachment_id)
        if part:
            filename = part.get("filename") or "unknown"
    except Exception:
        logger.debug("Attachment filename lookup unavailable")

    # Validate and resolve save path
    try:
        safe_path = _safe_save_path(save_path, config.token_path.parent)
        final_path = _unique_save_path(safe_path)
    except (ValueError, OSError) as e:
        return err(ERROR_FILE_NOT_FOUND, str(e))

    # Download attachment data — we trust list_attachments gave us a valid ID,
    # so we go straight to attachments().get() without payload verification.
    try:
        attachment = _with_retry(
            lambda: (
                service.users()
                .messages()
                .attachments()
                .get(userId="me", messageId=message_id, id=attachment_id)
                .execute()
            )
        )
    except HttpError as e:
        if e.resp.status == 404:
            return err(
                ERROR_ATTACHMENT_NOT_FOUND,
                f"Attachment not found: {attachment_id} (may have been deleted or is inaccessible)",
            )
        return err(ERROR_API, str(e))
    except Exception as e:
        return err(ERROR_API, str(e))

    try:
        if "data" not in attachment:
            return err(ERROR_API, "Attachment response is missing data")
        file_data = _decode_attachment(attachment["data"])
        final_path = _write_attachment(final_path, file_data)
    except ValueError as exc:
        return err(ERROR_VALIDATION, str(exc))
    except OSError as exc:
        return err(ERROR_FILE_NOT_FOUND, f"Could not save attachment: {exc}")

    return ok(
        {
            "filename": filename,
            "path": str(final_path),
            "size": len(file_data),
        }
    )


# ── Tool: create_draft ──────────────────────────────────────────────────────
def create_draft(
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    bcc: str = "",
    attachments: list[str] | None = None,
) -> dict:
    """
    Create a draft email saved in Gmail's Drafts folder — not sent immediately.
    Review it in Gmail before sending manually.

    Required:
      to (str) — recipient email address.
      subject (str) — email subject line.
      body (str) — email body text.

    Optional:
      cc (str, default="") — CC recipients.
      bcc (str, default="") — BCC recipients.
      attachments (list[str] | None, default=None) — file paths to attach.

    Returns on success:
      {"draft_id": "...", "message_id": "..."}

    Workflow note: Drafts are saved to Gmail, not stored locally.
      Use list_drafts() to see all drafts. Use update_draft(draft_id, ...)
      to modify a draft. Use delete_draft(draft_id) to remove.
      The draft stays in Gmail until manually sent or deleted.
    """
    if attachments is None:
        attachments = []

    if not to or not to.strip():
        return err(ERROR_VALIDATION, "recipient 'to' cannot be empty")

    try:
        service = _get_service()
    except AuthError as e:
        return err(ERROR_AUTH_MISSING, str(e))
    except Exception as e:
        logger.exception("Unexpected error obtaining Gmail service")
        return err(ERROR_INTERNAL, str(e))

    try:
        profile = _with_retry(lambda: service.users().getProfile(userId="me").execute(), max_retries=1)
        sender = profile.get("emailAddress", "")
    except HttpError as e:
        return err(ERROR_API, f"Failed to get sender profile: {e}")
    except Exception as e:
        return err(ERROR_API, f"Failed to get sender profile: {e}")

    try:
        encoded, _ = _build_mime_message(
            to=to,
            subject=subject,
            body=body,
            cc=cc,
            bcc=bcc,
            attachments=attachments,
            sender=sender,
        )
    except OSError as e:
        return err(ERROR_FILE_NOT_FOUND, str(e))
    except ValueError as e:
        return err(ERROR_VALIDATION, str(e))

    try:
        draft = _with_retry(
            lambda: (
                service.users().drafts().create(userId="me", body={"message": {"raw": encoded}}).execute()
            ),
            max_retries=1,
        )
    except HttpError as e:
        return err(
            ERROR_API,
            "Draft creation could not be confirmed. Check Gmail before retrying to avoid duplicates. Details: "
            + str(e),
        )
    except Exception as e:
        return err(
            ERROR_API,
            "Draft creation could not be confirmed. Check Gmail before retrying to avoid duplicates. Details: "
            + str(e),
        )

    msg = draft.get("message", {})
    return ok(
        {
            "draft_id": draft.get("id", ""),
            "message_id": msg.get("id", ""),
        }
    )


# ── Tool: list_drafts ────────────────────────────────────────────────────────
def list_drafts(max_results: int = 5) -> dict:
    """
    List the user's draft emails from Gmail's Drafts folder.

    Optional:
      max_results (int, default=5) — max drafts to return.

    Returns on success:
      {"total": N, "drafts": [
        {"id": "...", "subject": "...", "from": "...", "to": "...",
         "date": "...", "snippet": "..."}
      ]}

    Workflow note: Each draft has a "id" (draft_id) used by update_draft
      and delete_draft. The "subject" field shows the draft's subject line.
      Use update_draft(draft_id, body="new content") to edit a draft.
    """
    if type(max_results) is not int or not 1 <= max_results <= 500:
        return err(ERROR_VALIDATION, "max_results must be an integer between 1 and 500")

    try:
        service = _get_service()
    except AuthError as e:
        return err(ERROR_AUTH_MISSING, str(e))
    except Exception as e:
        logger.exception("Unexpected error obtaining Gmail service")
        return err(ERROR_INTERNAL, str(e))

    try:
        response = _with_retry(
            lambda: service.users().drafts().list(userId="me", maxResults=max_results).execute()
        )
    except HttpError as e:
        return err(ERROR_API, str(e))
    except Exception as e:
        return err(ERROR_API, str(e))

    drafts_list = []
    for draft_ref in response.get("drafts", []):
        draft_id = draft_ref.get("id", "")
        msg_ref = draft_ref.get("message", {})
        msg_id = msg_ref.get("id", "")

        if msg_id:
            try:
                msg = _with_retry(
                    lambda mid=msg_id: (
                        service.users()
                        .messages()
                        .get(
                            userId="me",
                            id=mid,
                            format="metadata",
                            metadataHeaders=["Subject", "From", "To", "Date"],
                        )
                        .execute()
                    )
                )
                headers = _headers(msg.get("payload", {}))
                drafts_list.append(
                    {
                        "id": draft_id,
                        "subject": headers.get("subject", "(no subject)"),
                        "from": headers.get("from", "unknown"),
                        "to": headers.get("to", ""),
                        "date": headers.get("date", ""),
                        "snippet": msg.get("snippet", ""),
                    }
                )
            except Exception:
                drafts_list.append(
                    {
                        "id": draft_id,
                        "subject": "(unable to fetch)",
                        "from": "unknown",
                        "to": "",
                        "date": "",
                        "snippet": "",
                    }
                )
        else:
            drafts_list.append(
                {
                    "id": draft_id,
                    "subject": "(no message)",
                    "from": "unknown",
                    "to": "",
                    "date": "",
                    "snippet": "",
                }
            )

    return ok(
        {
            "total": len(drafts_list),
            "drafts": drafts_list,
        }
    )


# ── Tool: update_draft ──────────────────────────────────────────────────────
def update_draft(
    draft_id: str,
    to: str = "",
    subject: str = "",
    body: str = "",
    cc: str = "",
    bcc: str = "",
    attachments: list[str] | None = None,
) -> dict:
    """Update a draft body, preserving omitted headers, threading and attachments.

    body is required. Empty to/subject/cc/bcc retain existing values.
    attachments=None preserves existing files; [] explicitly removes them.
    """
    from copy import deepcopy
    from email import policy
    from email.parser import BytesParser

    if not draft_id or not draft_id.strip():
        return err(ERROR_VALIDATION, "draft_id is required when updating a draft")
    if not body:
        return err(ERROR_VALIDATION, "body is required when updating a draft")
    try:
        service = _get_service()
    except AuthError as exc:
        return err(ERROR_AUTH_MISSING, str(exc))
    except Exception as exc:
        return err(ERROR_INTERNAL, str(exc))
    try:
        current = _with_retry(
            lambda: service.users().drafts().get(userId="me", id=draft_id, format="raw").execute()
        )
        message = current.get("message", {})
        raw = message.get("raw")
        original = None
        if raw:
            original = BytesParser(policy=policy.default).parsebytes(
                base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
            )
            headers = {name.lower(): value for name, value in original.items()}
        else:
            payload = message.get("payload", {})
            headers = _headers(payload)
            if payload.get("parts") and attachments is None:
                return err(ERROR_API, "Raw draft MIME unavailable; refusing to discard existing parts")
        sender = _with_retry(lambda: service.users().getProfile(userId="me").execute()).get(
            "emailAddress", ""
        )
        encoded, _ = _build_mime_message(
            to=to or headers.get("to", ""),
            subject=subject or headers.get("subject", ""),
            body=body,
            cc=cc or headers.get("cc", ""),
            bcc=bcc or headers.get("bcc", ""),
            attachments=attachments,
            sender=headers.get("from", sender),
            in_reply_to=headers.get("in-reply-to", ""),
            references=headers.get("references", ""),
        )
        rebuilt = BytesParser(policy=policy.default).parsebytes(
            base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        )
        if attachments is None and original is not None:

            def retained_parts(message):
                for part in message.iter_parts():
                    if part.get_content_disposition() in ("attachment", "inline") or part.get_filename():
                        yield part
                    elif part.is_multipart():
                        yield from retained_parts(part)

            parts = list(retained_parts(original))
            if parts:
                rebuilt.make_mixed()
                for part in parts:
                    rebuilt.attach(deepcopy(part))
        mime_bytes = rebuilt.as_bytes()
        if len(mime_bytes) > MAX_MESSAGE_BYTES:
            return err(ERROR_VALIDATION, "Updated draft exceeds the MIME message size limit")
        encoded = base64.urlsafe_b64encode(mime_bytes).decode("ascii").rstrip("=")
        update = {"raw": encoded}
        if message.get("threadId"):
            update["threadId"] = message["threadId"]
        updated = _with_retry(
            lambda: (
                service.users().drafts().update(userId="me", id=draft_id, body={"message": update}).execute()
            )
        )
        return ok({"draft_id": updated.get("id", ""), "message_id": updated.get("message", {}).get("id", "")})
    except HttpError as exc:
        return err(ERROR_DRAFT_NOT_FOUND if exc.resp.status == 404 else ERROR_API, str(exc))
    except FileNotFoundError as exc:
        return err(ERROR_FILE_NOT_FOUND, str(exc))
    except (ValueError, OSError) as exc:
        return err(ERROR_VALIDATION, str(exc))
    except Exception as exc:
        return err(ERROR_API, str(exc))


def delete_draft(draft_id: str) -> dict:
    """
    Delete a draft from Gmail's Drafts folder.

    Required:
      draft_id (str) — draft ID from list_drafts.

    Returns on success:
      {"draft_id": "..."}

    Workflow note: This permanently removes the draft. No undo.
      Call list_drafts() first to confirm the draft_id before deleting.
    """
    if not draft_id or not draft_id.strip():
        return err(ERROR_VALIDATION, "draft_id cannot be empty")

    try:
        service = _get_service()
    except AuthError as e:
        return err(ERROR_AUTH_MISSING, str(e))
    except Exception as e:
        logger.exception("Unexpected error obtaining Gmail service")
        return err(ERROR_INTERNAL, str(e))

    try:
        _with_retry(lambda: service.users().drafts().delete(userId="me", id=draft_id).execute())
    except HttpError as e:
        if e.resp.status == 404:
            return err(ERROR_DRAFT_NOT_FOUND, f"Draft not found: {draft_id}")
        return err(ERROR_API, str(e))
    except Exception as e:
        return err(ERROR_API, str(e))

    return ok({"draft_id": draft_id})


# ── Batch helpers ─────────────────────────────────────────────────────────────
def _batch_ok(data: Any, requested: int, results: list[dict]) -> dict:
    succeeded = sum(1 for r in results if r.get("ok"))
    failed = requested - succeeded
    return ok(
        {
            "requested": requested,
            "succeeded": succeeded,
            "failed": failed,
            "results": results,
        }
    )


# ── Output size & file helpers ─────────────────────────────────────────────────
# Bytes per email estimate for threshold comparison (JSON-encoded)
_EMAIL_SIZE_ESTIMATE = 6000  # conservative: plain body + headers + markup
_SUMMARY_SIZE_ESTIMATE = 500  # compact summary (date + from + subject + snippet)
_DEFAULT_MAX_CHARS = 30000

# Exposed config dir path for use in save helpers
_CONFIG_DIR = config.token_path.parent


def _get_email_dir() -> Path:
    """Return the emails output directory, creating it if needed."""
    d = _CONFIG_DIR / "emails"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _estimate_output_size(n: int, full: bool = True) -> int:
    """Estimate serialized output size for N emails."""
    return n * (_EMAIL_SIZE_ESTIMATE if full else _SUMMARY_SIZE_ESTIMATE)


def _build_slug(subject: str = "", query: str = "") -> str:
    """Build a short filename slug from subject or query."""
    raw = query or subject
    # Strip non-alphanumeric, truncate to 30 chars
    slug = "".join(c if c.isalnum() else "-" for c in raw).strip("-")
    return slug[:30] or "emails"


def _make_email_path(count: int, slug: str = "") -> Path:
    """Generate a unique file path with timestamp."""
    from uuid import uuid4

    ts = datetime.now().strftime("%Y-%m-%d-%H-%M-%S") + "-" + uuid4().hex[:12]
    slug = slug or "emails"
    base = f"{ts}-{slug}-{count}"
    email_dir = _get_email_dir()
    path = email_dir / f"{base}.md"
    n = 1
    while path.exists():
        path = email_dir / f"{base}_{n}.md"
        n += 1
    return path


def _markdown_escape(text: str) -> str:
    """Escape pipe and backtick chars that break markdown tables."""
    return text.replace("|", "\\|").replace("`", "\\`").replace("\r\n", "\n")


def _extract_email_fields(e: dict) -> dict:
    """Get a compact representation of an email for summary output."""
    d = e.get("data", {})
    return {
        "id": d.get("id", ""),
        "date": d.get("date", "")[:10],
        "from": _markdown_escape(d.get("from", "")[:40]),
        "subject": _markdown_escape(d.get("subject", "")[:60]),
        "snippet": _markdown_escape(d.get("snippet", "")[:120]),
        "labels": d.get("labels", []),
    }


def _summary_table(results: list[dict]) -> str:
    """Build a markdown summary table from results."""
    lines = [
        "| # | Date | From | Subject | Snippet |",
        "|---|------|------|---------|---------|",
    ]
    for i, e in enumerate(results, 1):
        f = _extract_email_fields(e)
        lines.append(f"| {i} | {f['date']} | {f['from']} | {f['subject']} | {f['snippet']} |")
    return "\n".join(lines)


def _build_markdown_file(results: list[dict], path: Path, query: str = "") -> dict:
    """Save full email results to a markdown file. Returns save metadata."""
    total = len(results)
    lines = []

    # Header
    title = query or "Gmail Search Results"
    lines.append(f"# {title}\n")
    lines.append(f"**Total emails:** {total}  ")
    lines.append(f"**Saved:** {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")

    # Summary table
    lines.append("## Summary\n")
    lines.append(_summary_table(results))
    lines.append("\n---\n")

    # Full emails
    for i, e in enumerate(results, 1):
        d = e.get("data", {})
        ok_status = "✅" if e.get("ok") else f"❌ {e.get('error', 'unknown error')}"
        body = (d.get("body") or d.get("html_body") or "").strip()

        lines.append(f"## Email {i}/{total} {ok_status}\n")
        if not e.get("ok"):
            lines.append(f"**Error:** {e.get('message', e.get('error', 'unknown error'))}")
        lines.append(f"- **Date:** {d.get('date', '')}")
        lines.append(f"- **From:** {d.get('from', '')}")
        lines.append(f"- **To:** {d.get('to', '')}")
        lines.append(f"- **Subject:** {d.get('subject', '')}")
        lines.append(f"- **Labels:** {', '.join(d.get('labels', []))}")
        if body:
            lines.append(f"\n**Body:**\n\n{body}")
        lines.append("\n---\n")

    _atomic_write(path, "\n".join(lines))
    return {
        "saved": True,
        "path": str(path),
        "count": total,
        "size_bytes": path.stat().st_size,
    }


def _build_json_file(results: list[dict], path: Path, query: str = "") -> dict:
    """Save results as pretty-printed JSON."""
    payload = {
        "query": query,
        "saved_at": datetime.now().isoformat(),
        "total": len(results),
        "results": [_export_record(e) for e in results],
    }
    _atomic_write(path, json.dumps(payload, indent=2, ensure_ascii=False))
    return {"saved": True, "path": str(path), "count": len(results), "size_bytes": path.stat().st_size}


def _export_record(result: dict) -> dict:
    """Flatten successful records without shortening or escaping stored content."""
    if result.get("ok"):
        return {**result.get("data", {}), "ok": True}
    return {key: value for key, value in result.items() if key != "data"}


def _build_jsonl_file(results: list[dict], path: Path) -> dict:
    content = "".join(json.dumps(_export_record(e), ensure_ascii=False) + "\n" for e in results)
    _atomic_write(path, content)
    return {"saved": True, "path": str(path), "count": len(results), "size_bytes": path.stat().st_size}


def _build_csv_file(results: list[dict], path: Path) -> dict:
    """Save complete fields, bodies, identifiers and per-message errors as CSV."""
    import csv
    import io

    fields = [
        "id",
        "thread_id",
        "date",
        "from",
        "to",
        "cc",
        "subject",
        "snippet",
        "labels",
        "body",
        "html_body",
        "ok",
        "error",
        "message",
    ]
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for result in results:
        record = _export_record(result)
        record["labels"] = json.dumps(record.get("labels", []), ensure_ascii=False)
        writer.writerow(record)
    _atomic_write(path, buf.getvalue())
    return {"saved": True, "path": str(path), "count": len(results), "size_bytes": path.stat().st_size}


def _save_results(results: list[dict], path: Path, fmt: str, query: str = "") -> dict:
    """Dispatch to the correct format saver."""
    if fmt == "md":
        return _build_markdown_file(results, path, query)
    elif fmt == "json":
        return _build_json_file(results, path, query)
    elif fmt == "jsonl":
        return _build_jsonl_file(results, path)
    elif fmt == "csv":
        return _build_csv_file(results, path)
    else:
        return _build_markdown_file(results, path, query)


def _extract_email_data(msg: dict) -> dict:
    """Extract structured data from a full message payload for read_emails."""
    headers = _headers(msg.get("payload", {}))
    plain_body, html_body = _extract_body(msg.get("payload", {}))
    internal_date = msg.get("internalDate", "")
    if internal_date:
        try:
            date_str = formatdate(int(internal_date) / 1000, usegmt=True)
        except Exception:
            date_str = internal_date
    else:
        date_str = headers.get("date", "")
    return {
        "id": msg.get("id", ""),
        "subject": headers.get("subject", "(no subject)"),
        "from": headers.get("from", "unknown"),
        "to": headers.get("to", "unknown"),
        "cc": headers.get("cc", ""),
        "thread_id": msg.get("threadId", ""),
        "date": date_str,
        "body": plain_body,
        "html_body": html_body,
        "labels": msg.get("labelIds", []),
        "snippet": msg.get("snippet", ""),
    }


# ── Tool: read_emails (batch via BatchHttpRequest) ────────────────────────────
def _fetch_messages(service, message_ids: list[str]) -> list[dict]:
    """Execute bounded read batches, retaining duplicates, order and partial errors."""
    results: dict[str, dict] = {}

    def callback(request_id, response, exception):
        if exception:
            results[request_id] = {"ok": False, "error": ERROR_API, "message": str(exception)}
        else:
            try:
                _prepare_body(service, response)
                results[request_id] = {"ok": True, "data": _extract_email_data(response)}
            except Exception as exc:
                results[request_id] = err(ERROR_API, f"Invalid message response: {exc}")

    for start in range(0, len(message_ids), 100):
        batch = service.new_batch_http_request(callback=callback)
        for index in range(start, min(start + 100, len(message_ids))):
            batch.add(
                service.users().messages().get(userId="me", id=message_ids[index], format="full"),
                request_id=str(index),
            )
        try:
            _with_retry(lambda current=batch: current.execute(http=service._http))
        except Exception as exc:
            for index in range(start, min(start + 100, len(message_ids))):
                results.setdefault(str(index), err(ERROR_API, str(exc)))
    return [
        {"id": mid, **results.get(str(index), err(ERROR_API, "No response received"))}
        for index, mid in enumerate(message_ids)
    ]


def _resolve_label(service, label: str) -> str:
    system = {
        "INBOX",
        "SPAM",
        "TRASH",
        "UNREAD",
        "STARRED",
        "IMPORTANT",
        "SENT",
        "DRAFT",
        "CHAT",
        "CATEGORY_PERSONAL",
        "CATEGORY_SOCIAL",
        "CATEGORY_PROMOTIONS",
        "CATEGORY_UPDATES",
        "CATEGORY_FORUMS",
    }
    if label in system:
        return label
    labels = _with_retry(lambda: service.users().labels().list(userId="me").execute()).get("labels", [])
    for item in labels:
        if label in (item.get("id"), item.get("name")):
            return item["id"]
    raise ValueError(f"Gmail label not found: {label}")


def _bulk_modify(service, message_ids: list[str], add: list[str], remove: list[str]) -> dict:
    out: list[dict] = []
    try:
        add = [_resolve_label(service, label) for label in add]
        remove = [_resolve_label(service, label) for label in remove]
    except ValueError as exc:
        return err(ERROR_INVALID_LABEL, str(exc))
    except Exception as exc:
        return err(ERROR_API, str(exc))
    for start in range(0, len(message_ids), 1000):
        ids = message_ids[start : start + 1000]
        body = {"ids": ids, "addLabelIds": add, "removeLabelIds": remove}
        try:
            _with_retry(
                lambda current=body: (
                    service.users().messages().batchModify(userId="me", body=current).execute()
                )
            )
            out.extend({"id": mid, "ok": True} for mid in ids)
        except Exception as exc:
            out.extend({"id": mid, **err(ERROR_API, str(exc))} for mid in ids)
    return _batch_ok(None, len(message_ids), out)


def read_emails(
    message_ids: list[str],
    save_to_file: bool = False,
    max_chars: int = _DEFAULT_MAX_CHARS,
) -> dict:
    """
    CHOICE HINT: When the user asks to "read", "show", or "fetch" content from
    multiple emails (e.g. "read these 5 emails", "show me the latest 3 emails"),
    ALWAYS prefer this tool over making N calls to read_email(). It bundles all
    Gmail API calls into bounded HTTP batches.

    Fetch full content of multiple emails in a single MCP request.

    Uses googleapiclient.http.BatchHttpRequest — calls are bundled
    into batches of up to 100 per HTTP round-trip.
    Handles up to 100 messages per call automatically.

    Optional:
      save_to_file (bool, default=False) — force save all results to a
        .md file regardless of size. Useful for large fetches.
      max_chars (int, default=30000) — if estimated output exceeds this,
        auto-saves to .md and returns a compact summary with the file path.

    Returns on success (small):
      {"ok": true, "data": {
        "requested": N, "succeeded": N, "failed": 0,
        "results": [{"id": "...", "ok": true, "data": {...}}, ...]
      }}

    Returns on success (large / save_to_file):
      {"ok": true, "data": {
        "saved": true,
        "path": "C:\\Users\\...\\gmail-mcp\\emails\\2026-05-11-piraeus-49.md",
        "count": 49,
        "size_bytes": 184523,
        "summary_table": "## Summary\n| # | Date | From | Subject | ...|",
        "requested": 49,
        "succeeded": 49,
        "failed": 0,
        "results": [{"id": "...", "ok": true, "data": {"date": "...", "from": "...", "subject": "...",
          "snippet": "...", "labels": [...]}}  ...]
      }}

    Workflow note: Use this instead of N calls to read_email() to bundle
      all fetches into a single MCP request. Each result has the same
      shape as a read_email() response.
    """
    if not message_ids:
        return _batch_ok(None, 0, [])

    try:
        service = _get_service()
    except AuthError as e:
        return err(ERROR_AUTH_MISSING, str(e))
    except Exception as e:
        logger.exception("Unexpected error obtaining Gmail service")
        return err(ERROR_INTERNAL, str(e))

    out = _fetch_messages(service, message_ids)

    # Auto-save if forced or estimated size exceeds threshold
    estimated_chars = max(_estimate_output_size(len(message_ids)), len(json.dumps(out, ensure_ascii=False)))
    if save_to_file or estimated_chars > max_chars:
        slug = _build_slug(
            query="batch-fetch", subject=out[0].get("data", {}).get("subject", "") if out else ""
        )
        try:
            path = _make_email_path(len(message_ids), slug)
            saved = _save_results(out, path, "md", query=f"Batch fetch {len(message_ids)} emails")
        except OSError as exc:
            return err(ERROR_FILE_NOT_FOUND, f"Could not save export: {exc}")
        return ok(
            {
                "saved": True,
                "path": saved["path"],
                "count": saved["count"],
                "size_bytes": saved["size_bytes"],
                "summary_table": _summary_table(out),
                "requested": len(message_ids),
                "succeeded": sum(1 for r in out if r.get("ok")),
                "failed": sum(1 for r in out if not r.get("ok")),
                "results": [_extract_email_fields(e) for e in out],
            }
        )

    return _batch_ok(None, len(message_ids), out)


# ── Tool: mark_emails_read (batchModify) ──────────────────────────────────────
def mark_emails_read(message_ids: list[str]) -> dict:
    """
    CHOICE HINT: When the user asks to "mark as read" or "mark read" multiple
    emails, ALWAYS prefer this tool over making N calls to mark_read(). It uses
    Gmail's batchModify API in chunks of at most 1000 message IDs.

    Mark multiple emails as read in a single MCP request.

    Uses messages().batchModify() with up to 1000 IDs per Gmail API call.

    Required:
      message_ids (list[str]) — Gmail message IDs.

    Returns on success:
      {"ok": true, "data": {
        "requested": N, "succeeded": N, "failed": 0,
        "results": [{"id": "...", "ok": true}, ...]
      }}

    Workflow note: Use this instead of N calls to mark_read() to bundle
      all operations into a single MCP request.
    """
    if not message_ids:
        return _batch_ok(None, 0, [])

    try:
        service = _get_service()
    except AuthError as e:
        return err(ERROR_AUTH_MISSING, str(e))
    except Exception as e:
        logger.exception("Unexpected error obtaining Gmail service")
        return err(ERROR_INTERNAL, str(e))

    return _bulk_modify(service, message_ids, [], ["UNREAD"])


def archive_emails(message_ids: list[str]) -> dict:
    """
    CHOICE HINT: When the user asks to "archive" or "remove from inbox" multiple
    emails, ALWAYS prefer this tool over making N calls to archive_email(). It uses
    Gmail's batchModify API in chunks of at most 1000 message IDs.

    Archive multiple emails in a single MCP request.

    Uses messages().batchModify() with up to 1000 IDs per Gmail API call.

    Required:
      message_ids (list[str]) — Gmail message IDs.

    Returns on success:
      {"ok": true, "data": {
        "requested": N, "succeeded": N, "failed": 0,
        "results": [{"id": "...", "ok": true}, ...]
      }}

    Workflow note: Non-destructive. Use this instead of N archive_email() calls.
    """
    if not message_ids:
        return _batch_ok(None, 0, [])

    try:
        service = _get_service()
    except AuthError as e:
        return err(ERROR_AUTH_MISSING, str(e))
    except Exception as e:
        logger.exception("Unexpected error obtaining Gmail service")
        return err(ERROR_INTERNAL, str(e))

    return _bulk_modify(service, message_ids, [], ["INBOX"])


def trash_emails(message_ids: list[str]) -> dict:
    """
    CHOICE HINT: When the user asks to "trash" or "delete" multiple emails,
    ALWAYS prefer this tool over making N calls to trash_email(). It uses
    Gmail's batchModify API in chunks of at most 1000 message IDs.

    Move multiple emails to trash in a single MCP request.

    Uses messages().batchModify() with up to 1000 IDs per Gmail API call.

    Required:
      message_ids (list[str]) — Gmail message IDs.

    Returns on success:
      {"ok": true, "data": {
        "requested": N, "succeeded": N, "failed": 0,
        "results": [{"id": "...", "ok": true}, ...]
      }}

    Workflow note: Use this instead of N trash_email() calls.
    """
    if not message_ids:
        return _batch_ok(None, 0, [])

    try:
        service = _get_service()
    except AuthError as e:
        return err(ERROR_AUTH_MISSING, str(e))
    except Exception as e:
        logger.exception("Unexpected error obtaining Gmail service")
        return err(ERROR_INTERNAL, str(e))

    return _bulk_modify(service, message_ids, ["TRASH"], [])


def add_labels(message_ids: list[str], label: str) -> dict:
    """
    CHOICE HINT: When the user asks to "add a label" to multiple emails,
    ALWAYS prefer this tool over making N calls to add_label(). It uses
    Gmail's batchModify API in chunks of at most 1000 message IDs.

    Add a label to multiple emails in a single MCP request.

    Uses messages().batchModify() with up to 1000 IDs per Gmail API call.

    Required:
      message_ids (list[str]) — Gmail message IDs.
      label (str) — label name. System labels: STARRED, IMPORTANT, etc.
        Custom labels must exactly match how they appear in Gmail.

    Returns on success:
      {"ok": true, "data": {
        "requested": N, "succeeded": N, "failed": 0,
        "results": [{"id": "...", "ok": true}, ...]
      }}

    Workflow note: Use this instead of N add_label() calls.
      Label names are case-sensitive.
    """
    if not message_ids:
        return _batch_ok(None, 0, [])
    if not label or not label.strip():
        return err(ERROR_VALIDATION, "label cannot be empty")

    try:
        service = _get_service()
    except AuthError as e:
        return err(ERROR_AUTH_MISSING, str(e))
    except Exception as e:
        logger.exception("Unexpected error obtaining Gmail service")
        return err(ERROR_INTERNAL, str(e))

    return _bulk_modify(service, message_ids, [label], [])


def download_attachments(items: list[dict]) -> dict:
    """Download attachments in batches of 100, preserving input order and unique files."""
    if not items:
        return _batch_ok(None, 0, [])
    try:
        service = _get_service()
    except AuthError as exc:
        return err(ERROR_AUTH_MISSING, str(exc))
    except Exception as exc:
        return err(ERROR_INTERNAL, str(exc))
    results: dict[str, dict] = {}
    out = []
    valid = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            out.append(err(ERROR_VALIDATION, "Attachment item must be an object"))
            continue
        mid, aid, path = (item.get(k, "") for k in ("message_id", "attachment_id", "save_path"))
        out.append({"message_id": mid, "attachment_id": aid})
        if not all(isinstance(value, str) and value.strip() for value in (mid, aid, path)):
            out[index].update(
                err(ERROR_VALIDATION, "Each item needs message_id, attachment_id, and save_path")
            )
            continue
        try:
            safe = _safe_save_path(path, config.token_path.parent)
        except (ValueError, OSError) as exc:
            out[index].update(err(ERROR_FILE_NOT_FOUND, str(exc)))
            continue
        valid.append((index, mid, aid, safe))

    def callback(request_id, response, exception):
        results[request_id] = err(ERROR_API, str(exception)) if exception else ok(response)

    for start in range(0, len(valid), 100):
        chunk = valid[start : start + 100]
        batch = service.new_batch_http_request(callback=callback)
        for index, mid, aid, _ in chunk:
            batch.add(
                service.users().messages().attachments().get(userId="me", messageId=mid, id=aid),
                request_id=str(index),
            )
        try:
            _with_retry(lambda current=batch: current.execute(http=service._http))
        except Exception as exc:
            for index, *_ in chunk:
                results.setdefault(str(index), err(ERROR_API, str(exc)))
        for index, _, _, path in chunk:
            result = results.get(str(index), err(ERROR_API, "No response received"))
            if result.get("ok"):
                try:
                    encoded = result["data"]["data"]
                    data = _decode_attachment(encoded)
                    saved = _write_attachment(path, data)
                    result = ok({"filename": saved.name, "path": str(saved), "size": len(data)})
                except (ValueError, KeyError, TypeError) as exc:
                    result = err(ERROR_VALIDATION, str(exc))
                except OSError as exc:
                    result = err(ERROR_FILE_NOT_FOUND, str(exc))
            out[index].update(result)
    return _batch_ok(None, len(items), out)


def read_emails_full(
    message_ids: list[str],
    save_path: str,
    format: str = "md",
) -> dict:
    """
    Read multiple emails and always save the full results to a file.

    Unlike read_emails() which returns data inline, this tool always writes
    to disk and returns only a compact confirmation — making it safe to use
    even for hundreds of emails without hitting MCP output limits.

    Required:
      message_ids (list[str]) — Gmail message IDs to fetch.
      save_path (str) — local path to save the file. Must be inside your
        home directory (path traversal is rejected).

    Optional:
      format (str, default="md") — output format. One of:
        "md"   — Markdown with summary table + full body per email
        "json" — pretty-printed JSON
        "jsonl" — one JSON object per line
        "csv"  — complete fields, text/HTML bodies, identifiers and errors

    Returns on success:
      {"ok": true, "data": {
        "saved": true,
        "path": "C:\\Users\\...\\my-emails.md",
        "count": 49,
        "format": "md",
        "total_size_bytes": 184523
      }}

    Workflow note: Use this instead of read_emails() when you know the
      result will be large. Always saves — never returns data inline.
    """
    if format not in ("md", "json", "jsonl", "csv"):
        return err(ERROR_VALIDATION, "format must be md, json, jsonl, or csv")
    if not message_ids:
        return ok({"saved": False, "path": "", "count": 0, "format": format, "total_size_bytes": 0})

    # Validate path
    config_dir = config.token_path.parent
    try:
        save_p = _safe_save_path(save_path, config_dir)
    except (ValueError, OSError) as e:
        return err(ERROR_FILE_NOT_FOUND, str(e))

    try:
        service = _get_service()
    except AuthError as e:
        return err(ERROR_AUTH_MISSING, str(e))
    except Exception as e:
        logger.exception("Unexpected error obtaining Gmail service")
        return err(ERROR_INTERNAL, str(e))

    out = _fetch_messages(service, message_ids)

    # Ensure parent dir exists and write file
    try:
        saved = _save_results(out, save_p, format, query=f"Exported {len(message_ids)} emails")
    except OSError as exc:
        return err(ERROR_FILE_NOT_FOUND, f"Could not save export: {exc}")

    return ok(
        {
            "saved": True,
            "path": saved["path"],
            "count": saved["count"],
            "succeeded": sum(bool(item.get("ok")) for item in out),
            "failed": sum(not item.get("ok") for item in out),
            "format": format,
            "total_size_bytes": saved["size_bytes"],
        }
    )


# ── Tool: search_and_save ─────────────────────────────────────────────────────
def search_and_save(
    query: str,
    max_results: int = 100,
    save_path: str = "",
    format: str = "md",
) -> dict:
    """
    One-shot search + save: search Gmail and write results directly to a file.

    Combines list_emails + read_emails_full into a single tool call.
    No data is returned inline — just a compact summary + file path.

    Required:
      query (str) — Gmail search syntax
        (e.g. "from:PiraeusBank after:2026/03/01 has:attachment").

    Optional:
      max_results (int, default=100) — max emails to fetch.
      save_path (str) — local file path. If empty, auto-generates one in
        the configured gmail-mcp directory under emails/.  Overwrite is rejected — use a fresh
        path or let the tool auto-generate.
      format (str, default="md") — "md", "json", "jsonl", or "csv".

    Returns on success:
      {"ok": true, "data": {
        "saved": true,
        "path": "C:\\Users\\...\\emails\\2026-05-11-piraeus-47.md",
        "count": 47,
        "format": "md",
        "total_size_bytes": 184523,
        "query": "from:PiraeusBank after:2026/03/01",
        "search_summary": "47 emails matched your search query"
      }}

    Workflow note: Use this when you want to search Gmail and immediately
      persist results to a file without worrying about MCP output limits.
    """
    if format not in ("md", "json", "jsonl", "csv"):
        return err(ERROR_VALIDATION, "format must be md, json, jsonl, or csv")
    if type(max_results) is not int or not 1 <= max_results <= 5000:
        return err(ERROR_VALIDATION, "max_results must be between 1 and 5000")
    if not query:
        return err(ERROR_VALIDATION, "query must not be empty")

    # Determine save path
    config_dir = config.token_path.parent
    if save_path:
        try:
            save_p = _safe_save_path(save_path, config_dir)
            if save_p.exists():
                return err(
                    ERROR_VALIDATION, "Export path already exists; choose a new path or use append_emails"
                )
        except (ValueError, OSError) as e:
            return err(ERROR_FILE_NOT_FOUND, str(e))
    else:
        slug = _build_slug(query=query)
        try:
            save_p = _make_email_path(max_results, slug).with_suffix(f".{format}")
        except OSError as exc:
            return err(ERROR_FILE_NOT_FOUND, f"Could not create export directory: {exc}")

    try:
        service = _get_service()
    except AuthError as e:
        return err(ERROR_AUTH_MISSING, str(e))
    except Exception as e:
        logger.exception("Unexpected error obtaining Gmail service")
        return err(ERROR_INTERNAL, str(e))

    message_ids: list[str] = []
    seen_ids = set()
    seen_tokens = set()
    page_token = None
    total_found = 0
    try:
        while len(message_ids) < max_results:
            params = {"userId": "me", "q": query, "maxResults": min(500, max_results - len(message_ids))}
            if page_token:
                params["pageToken"] = page_token
            response = _with_retry(
                lambda current=params: service.users().messages().list(**current).execute()
            )
            total_found = response.get("resultSizeEstimate", total_found)
            for message in response.get("messages", []):
                if message["id"] not in seen_ids:
                    seen_ids.add(message["id"])
                    message_ids.append(message["id"])
                    if len(message_ids) == max_results:
                        break
            page_token = response.get("nextPageToken")
            if not page_token:
                break
            if page_token in seen_tokens:
                raise ValueError("Gmail returned a repeated pagination token")
            seen_tokens.add(page_token)
    except Exception as exc:
        return err(ERROR_API, str(exc))

    if not message_ids:
        return ok(
            {
                "saved": False,
                "path": "",
                "count": 0,
                "format": format,
                "total_size_bytes": 0,
                "query": query,
                "search_summary": "No emails matched your search query",
            }
        )

    # Step 2: fetch full content
    out = _fetch_messages(service, message_ids)

    # Step 3: save to file
    try:
        with _FILE_LOCK:
            if save_p.exists():
                return err(
                    ERROR_VALIDATION, "Export path already exists; choose a new path or use append_emails"
                )
            saved = _save_results(out, save_p, format, query=query)
    except OSError as exc:
        return err(ERROR_FILE_NOT_FOUND, f"Could not save export: {exc}")

    return ok(
        {
            "saved": True,
            "path": saved["path"],
            "count": saved["count"],
            "succeeded": sum(bool(item.get("ok")) for item in out),
            "failed": sum(not item.get("ok") for item in out),
            "format": format,
            "total_size_bytes": saved["size_bytes"],
            "query": query,
            "search_summary": f"{saved['count']} emails matched your search query (found {total_found} total)",
        }
    )


# ── Tool: append_emails ───────────────────────────────────────────────────────
def append_emails(
    message_ids: list[str],
    save_path: str,
) -> dict:
    """
    Append more emails to an existing .md, .json, or .jsonl file.

    Use this for pagination — fetch 50 emails, save them, then call this
    tool to append the next 50 to the same file. Works with .md, .json,
    and .jsonl files created by read_emails_full or search_and_save.

    Required:
      message_ids (list[str]) — Gmail message IDs to fetch and append.
      save_path (str) — path to the existing file. File must exist.
        Supported extensions: .md, .json, .jsonl

    Returns on success:
      {"ok": true, "data": {
        "appended": true,
        "path": "C:\\Users\\...\\my-emails.md",
        "count": 50,
        "total_size_bytes": 320000
      }}
    """
    if not message_ids:
        return ok({"appended": False, "path": save_path, "count": 0, "total_size_bytes": 0})

    config_dir = config.token_path.parent
    try:
        save_p = _safe_save_path(save_path, config_dir)
    except (ValueError, OSError) as e:
        return err(ERROR_FILE_NOT_FOUND, str(e))

    if not save_p.exists():
        return err(
            ERROR_FILE_NOT_FOUND,
            f"File not found: {save_path}. Create it first with read_emails_full or search_and_save.",
        )

    suffix = save_p.suffix.lower()
    if suffix not in (".md", ".json", ".jsonl"):
        return err(
            ERROR_VALIDATION, f"append_emails only supports .md, .json, and .jsonl files. Got: {suffix}"
        )

    try:
        service = _get_service()
    except AuthError as e:
        return err(ERROR_AUTH_MISSING, str(e))
    except Exception as e:
        logger.exception("Unexpected error obtaining Gmail service")
        return err(ERROR_INTERNAL, str(e))

    out = _fetch_messages(service, message_ids)

    try:
        with _FILE_LOCK:
            existing = save_p.read_text(encoding="utf-8")
            if suffix == ".md":
                new_content = existing + "\n" + _build_markdown_body_section(out, len(message_ids))
            elif suffix == ".json":
                data = json.loads(existing)
                if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                    return err(ERROR_VALIDATION, "Append target JSON must have a top-level 'results' array.")
                data["results"].extend(_export_record(e) for e in out)
                data["total"] = len(data["results"])
                new_content = json.dumps(data, indent=2, ensure_ascii=False)
            else:
                # Validate the existing JSON Lines before changing it.
                for line in existing.splitlines():
                    if line.strip():
                        json.loads(line)
                new_content = existing + ("\n" if existing and not existing.endswith("\n") else "")
                new_content += "".join(json.dumps(_export_record(e), ensure_ascii=False) + "\n" for e in out)
            _atomic_write(save_p, new_content)
            size = save_p.stat().st_size
    except (ValueError, UnicodeError) as exc:
        return err(ERROR_VALIDATION, f"Invalid append target: {exc}")
    except OSError as exc:
        return err(ERROR_FILE_NOT_FOUND, f"Could not update export: {exc}")
    return ok(
        {
            "appended": True,
            "path": str(save_p),
            "count": len(out),
            "total_size_bytes": size,
        }
    )


def _build_markdown_body_section(results: list[dict], total: int) -> str:
    """Build just the email sections for appending to an existing .md file (no header)."""
    lines = []
    start_num = 1
    for i, e in enumerate(results):
        d = e.get("data", {})
        body = d.get("body") or d.get("html_body", "") or ""
        lines.append(f"## Email {start_num + i}/{total}")
        if not e.get("ok"):
            lines.append(f"**Error:** {e.get('message', e.get('error', 'unknown error'))}")
        lines.append(f"**Date:** {d.get('date', '')}")
        lines.append(f"**From:** {d.get('from', '')}")
        lines.append(f"**Subject:** {d.get('subject', '')}")
        lines.append(f"**Labels:** {', '.join(d.get('labels', []))}")
        lines.append(f"\n**Body:**\n\n{body}")
        lines.append("\n---\n")
    return "\n".join(lines)
