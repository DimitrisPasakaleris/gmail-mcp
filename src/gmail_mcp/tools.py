import base64
import json
import logging
import math
import os
import re
import time
from datetime import datetime
from email import encoders
from email.message import EmailMessage
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate
from pathlib import Path
from typing import Any, Optional

import mimetypes

from googleapiclient.errors import HttpError

from .auth import get_gmail_service, AuthError

logger = logging.getLogger(__name__)

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
    """
    Retry a callable with exponential backoff and jitter.
    Retries on HttpError 429 (rate limit) and 503 (unavailable).
    Returns the result of fn() or raises the last exception.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            result = fn()
            if attempt > 0:
                logger.debug("Retry succeeded on attempt %d/%d", attempt + 1, max_retries)
            return result
        except HttpError as e:
            status = e.resp.status
            if status not in (429, 503):
                raise

            # Respect Retry-After header if present
            retry_after = e.resp.get("retry-after")
            if retry_after:
                try:
                    delay = float(retry_after)
                except ValueError:
                    delay = base_delay * (2 ** attempt)
            else:
                delay = base_delay * (2 ** attempt)

            # Add jitter: +/- 20%
            import random
            delay = delay * (0.8 + 0.4 * random.random())
            logger.warning("Rate-limited (%d), retrying in %.1fs (attempt %d/%d)",
                           status, delay, attempt + 1, max_retries)
            if attempt < max_retries - 1:
                time.sleep(delay)
            last_exc = e
        except (TimeoutError, ConnectionError, OSError) as e:
            # Transient network errors
            delay = base_delay * (2 ** attempt)
            import random
            delay = delay * (0.8 + 0.4 * random.random())
            logger.warning("Network error: %s, retrying in %.1fs (attempt %d/%d)",
                           e, delay, attempt + 1, max_retries)
            if attempt < max_retries - 1:
                time.sleep(delay)
            last_exc = e

    # All retries exhausted
    raise last_exc


# ── Helper functions ──────────────────────────────────────────────────────────
def _parse_recipients(header_value: str) -> list[str]:
    """
    Extract plain email addresses from a header value like
    'Alice <alice@example.com>, bob@example.com'.
    """
    addresses = []
    pattern = re.compile(r'<([^>]+)>|([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)')
    for match in pattern.finditer(header_value):
        addr = match.group(1) or match.group(2)
        if addr and addr not in addresses:
            addresses.append(addr.strip())
    return addresses


def _attachment_id_to_part(payload: dict, attachment_id: str) -> dict | None:
    """Recursively find the message part with matching body.attachmentId."""
    if "parts" in payload:
        for part in payload["parts"]:
            found = _attachment_id_to_part(part, attachment_id)
            if found:
                return found
    else:
        if payload.get("body", {}).get("attachmentId") == attachment_id:
            return payload
    return None


def _extract_body(payload: dict) -> tuple[str | None, str | None]:
    """Recursively find text/plain and text/html parts. Returns (plain, html)."""
    plain = None
    html = None

    if "parts" in payload:
        for part in payload["parts"]:
            p, h = _extract_body(part)
            plain = plain or p
            html = html or h
            if plain and html:
                break
    else:
        mime = payload.get("mimeType", "")
        data = payload.get("body", {}).get("data")
        if data:
            # Gmail API returns base64url without padding — add padding back
            padded = data + "=" * (-len(data) % 4)
            decoded = base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
            if mime == "text/plain":
                plain = decoded
            elif mime == "text/html":
                html = decoded

    return plain, html


def _safe_save_path(requested: str, config_dir: Path) -> Path:
    """
    Resolve and validate a save path against path traversal and write permissions.
    Returns the safe absolute path, or raises ValueError with a clear message.
    """
    # Resolve to absolute path
    path = Path(requested).resolve()

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
        except PermissionError as e:
            raise ValueError(f"Cannot create directory '{parent}': {e}")

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


def _build_mime_message(
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    bcc: str = "",
    attachments: Optional[list[str]] = None,
    in_reply_to: str = "",
    references: str = "",
    sender: str = "",
) -> tuple[str, str]:
    """
    Build a MIME email message, optionally with attachments.
    Returns (raw_encoded, from_address).

    raw_encoded is base64url-encoded without padding.
    """
    if attachments is None:
        attachments = []

    has_attachments = bool(attachments)

    if has_attachments:
        msg = MIMEMultipart('mixed')
    else:
        msg = EmailMessage()

    msg['To'] = to
    if cc:
        msg['Cc'] = cc
    if bcc:
        msg['Bcc'] = bcc
    msg['Subject'] = subject
    msg['Date'] = formatdate(localtime=True)

    if in_reply_to:
        msg['In-Reply-To'] = in_reply_to
    if references:
        msg['References'] = references

    if has_attachments:
        body_part = MIMEText(body, 'plain')
        msg.attach(body_part)

        for filepath in attachments:
            if not os.path.isfile(filepath):
                raise FileNotFoundError(f"Attachment file not found: {filepath}")
            filename = os.path.basename(filepath)

            file_size = os.path.getsize(filepath)
            if file_size > 25 * 1024 * 1024:
                raise ValueError(f"Attachment too large: {filepath} ({file_size} bytes, max 25MB)")

            mime_type, _ = mimetypes.guess_type(filepath)
            if not mime_type:
                mime_type = 'application/octet-stream'

            try:
                main_type, sub_type = mime_type.split('/')
            except ValueError:
                main_type, sub_type = 'application', 'octet-stream'

            with open(filepath, 'rb') as f:
                part = MIMEBase(main_type, sub_type)
                part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header('Content-Disposition', f'attachment; filename="{filename}"')
            part.replace_header('Content-Type', mime_type)
            msg.attach(part)
    else:
        msg.set_content(body)

    raw_bytes = msg.as_bytes()
    encoded = base64.urlsafe_b64encode(raw_bytes).decode('utf-8').rstrip('=')
    return encoded, sender


def _get_service():
    """Get the Gmail service, surfacing auth errors clearly."""
    try:
        return get_gmail_service()
    except AuthError as e:
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
    try:
        service = _get_service()
    except AuthError as e:
        return err(ERROR_AUTH_MISSING, str(e))
    except Exception as e:
        logger.exception("Unexpected error obtaining Gmail service")
        return err(ERROR_INTERNAL, str(e))

    try:
        response = _with_retry(
            lambda: service.users()
                .messages()
                .list(userId="me", q=query, maxResults=max_results)
                .execute()
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
                lambda mid=msg_id: service.users()
                    .messages()
                    .get(userId="me", id=mid, format="metadata",
                         metadataHeaders=["Subject", "From", "Date"])
                    .execute()
            )
        except HttpError as e:
            logger.debug("list_emails: skipping message %s due to HttpError: %s", msg_id, e)
            continue
        except Exception as e:
            logger.debug("list_emails: skipping message %s due to error: %s", msg_id, e)
            continue

        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        emails.append({
            "id": msg_id,
            "subject": headers.get("Subject", "(no subject)"),
            "from": headers.get("From", "unknown"),
            "date": headers.get("Date", ""),
            "snippet": msg.get("snippet", ""),
            "labels": msg.get("labelIds", []),
        })

    return ok({
        "total": int(response.get("resultSizeEstimate", len(emails))),
        "emails": emails,
    })


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
        thread = _with_retry(
            lambda: service.users()
                .threads()
                .get(userId="me", id=thread_id)
                .execute()
        )
    except HttpError as e:
        if e.resp.status == 404:
            return err(ERROR_THREAD_NOT_FOUND, f"Thread not found: {thread_id}")
        return err(ERROR_API, str(e))
    except Exception as e:
        return err(ERROR_API, str(e))

    messages_out = []
    for msg in thread.get("messages", []):
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        plain, _ = _extract_body(msg.get("payload", {}))
        messages_out.append({
            "id": msg["id"],
            "subject": headers.get("Subject", "(no subject)"),
            "from": headers.get("From", "unknown"),
            "date": headers.get("Date", ""),
            "snippet": plain or msg.get("snippet", ""),
        })

    return ok({
        "thread_id": thread_id,
        "total": len(messages_out),
        "messages": messages_out,
    })


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
    attachments: Optional[list[str]] = None,
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
        profile = _with_retry(
            lambda: service.users().getProfile(userId="me").execute()
        )
        sender = profile.get("emailAddress", "")
    except HttpError as e:
        return err(ERROR_API, f"Failed to get sender profile: {e}")
    except Exception as e:
        return err(ERROR_API, f"Failed to get sender profile: {e}")

    try:
        encoded, _ = _build_mime_message(
            to=to, subject=subject, body=body,
            cc=cc, bcc=bcc, attachments=attachments,
            in_reply_to=in_reply_to, references=references,
            sender=sender,
        )
    except FileNotFoundError as e:
        return err(ERROR_FILE_NOT_FOUND, str(e))
    except ValueError as e:
        return err(ERROR_VALIDATION, str(e))

    send_body = {"raw": encoded}

    try:
        sent = _with_retry(
            lambda: service.users().messages().send(userId="me", body=send_body).execute()
        )
    except HttpError as e:
        return err(ERROR_API, str(e))
    except Exception as e:
        return err(ERROR_API, str(e))

    return ok({
        "message_id": sent.get("id", ""),
        "thread_id": sent.get("threadId", ""),
        "to": to,
        "subject": subject,
    })


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
            lambda: service.users()
                .messages()
                .get(userId="me", id=in_reply_to, format="metadata",
                     metadataHeaders=["Subject", "From", "To", "Cc"])
                .execute()
        )
    except HttpError as e:
        if e.resp.status == 404:
            return err(ERROR_NOT_FOUND, f"Original message not found: {in_reply_to}")
        return err(ERROR_API, str(e))
    except Exception as e:
        return err(ERROR_API, str(e))

    headers = {h["name"]: h["value"] for h in original.get("payload", {}).get("headers", [])}

    if not actual_subject:
        original_subject = headers.get("Subject", "")
        if original_subject.lower().startswith("re:"):
            actual_subject = original_subject
        else:
            actual_subject = f"Re: {original_subject}"

    if reply_all and not cc:
        extra_ccs = []
        for hdr_val in [headers.get("To", ""), headers.get("Cc", "")]:
            if hdr_val:
                for r in _parse_recipients(hdr_val):
                    if r != to and r not in extra_ccs:
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
        in_reply_to=in_reply_to,
        references=in_reply_to,
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
            lambda: service.users()
                .messages()
                .get(userId="me", id=message_id, format="full")
                .execute()
        )
    except HttpError as e:
        if e.resp.status == 404:
            return err(ERROR_NOT_FOUND, f"Message not found: {message_id}")
        return err(ERROR_API, str(e))
    except Exception as e:
        return err(ERROR_API, str(e))

    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    plain_body, html_body = _extract_body(msg.get("payload", {}))

    internal_date = msg.get("internalDate", "")
    if internal_date:
        try:
            date_str = formatdate(int(internal_date) / 1000, usegmt=True)
        except Exception:
            date_str = internal_date
    else:
        date_str = headers.get("Date", "")

    return ok({
        "id": message_id,
        "subject": headers.get("Subject", "(no subject)"),
        "from": headers.get("From", "unknown"),
        "to": headers.get("To", "unknown"),
        "date": date_str,
        "body": plain_body,
        "html_body": html_body,
        "labels": msg.get("labelIds", []),
    })


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

    body = {"addLabelIds": add, "removeLabelIds": remove}
    try:
        updated = _with_retry(
            lambda: service.users()
                .messages()
                .modify(userId="me", id=message_id, body=body)
                .execute()
        )
    except HttpError as e:
        if e.resp.status == 404:
            return err(ERROR_NOT_FOUND, f"Message not found: {message_id}")
        return err(ERROR_API, str(e))
    except Exception as e:
        return err(ERROR_API, str(e))

    return ok({
        "message_id": message_id,
        "labels": updated.get("labelIds", []),
    })


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
            lambda: service.users()
                .messages()
                .get(userId="me", id=message_id, format="full")
                .execute()
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
                attachments.append({
                    "id": attachment_id,
                    "filename": filename,
                    "mime_type": payload.get("mimeType", "application/octet-stream"),
                    "size": int(payload.get("body", {}).get("size", 0)),
                })

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

    # Resolve filename from message payload (if available) — but don't fail
    # if it isn't, since list_attachments already gave us a working attachment ID.
    filename = "unknown"
    try:
        msg = _with_retry(
            lambda: service.users()
                .messages()
                .get(userId="me", id=message_id, format="metadata",
                     metadataHeaders=[])
                .execute()
        )
        # Extract filename from payload if present
        def _find_filename(payload: dict) -> str:
            if payload.get("filename"):
                return payload.get("filename", "")
            if "parts" in payload:
                for p in payload["parts"]:
                    f = _find_filename(p)
                    if f:
                        return f
            return ""

        filename = _find_filename(msg.get("payload", {})) or "unknown"
    except Exception:
        pass  # Non-fatal — we can still download with the attachment ID alone

    # Validate and resolve save path
    try:
        safe_path = _safe_save_path(save_path, Path.home())
        final_path = _unique_save_path(safe_path)
    except ValueError as e:
        return err(ERROR_FILE_NOT_FOUND, str(e))

    # Download attachment data — we trust list_attachments gave us a valid ID,
    # so we go straight to attachments().get() without payload verification.
    try:
        attachment = _with_retry(
            lambda: service.users()
                .messages()
                .attachments()
                .get(userId="me", messageId=message_id, id=attachment_id)
                .execute()
        )
    except HttpError as e:
        if e.resp.status == 404:
            return err(ERROR_ATTACHMENT_NOT_FOUND,
                       f"Attachment not found: {attachment_id} (may have been deleted or is inaccessible)")
        return err(ERROR_API, str(e))
    except Exception as e:
        return err(ERROR_API, str(e))

    data_b64 = attachment.get("data", "")
    if not data_b64:
        return err(ERROR_API, "Empty attachment data received from Gmail API")

    file_data = base64.urlsafe_b64decode(data_b64 + "=" * (-len(data_b64) % 4))

    with open(final_path, "wb") as f:
        f.write(file_data)

    return ok({
        "filename": filename,
        "path": str(final_path),
        "size": len(file_data),
    })


# ── Tool: create_draft ──────────────────────────────────────────────────────
def create_draft(
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    bcc: str = "",
    attachments: Optional[list[str]] = None,
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
        profile = _with_retry(
            lambda: service.users().getProfile(userId="me").execute()
        )
        sender = profile.get("emailAddress", "")
    except HttpError as e:
        return err(ERROR_API, f"Failed to get sender profile: {e}")
    except Exception as e:
        return err(ERROR_API, f"Failed to get sender profile: {e}")

    try:
        encoded, _ = _build_mime_message(
            to=to, subject=subject, body=body,
            cc=cc, bcc=bcc, attachments=attachments,
            sender=sender,
        )
    except FileNotFoundError as e:
        return err(ERROR_FILE_NOT_FOUND, str(e))
    except ValueError as e:
        return err(ERROR_VALIDATION, str(e))

    try:
        draft = _with_retry(
            lambda: service.users()
                .drafts()
                .create(userId="me", body={"message": {"raw": encoded}})
                .execute()
        )
    except HttpError as e:
        return err(ERROR_API, str(e))
    except Exception as e:
        return err(ERROR_API, str(e))

    msg = draft.get("message", {})
    return ok({
        "draft_id": draft.get("id", ""),
        "message_id": msg.get("id", ""),
    })


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
                    lambda mid=msg_id: service.users()
                        .messages()
                        .get(userId="me", id=mid, format="metadata",
                             metadataHeaders=["Subject", "From", "To", "Date"])
                        .execute()
                )
                headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
                drafts_list.append({
                    "id": draft_id,
                    "subject": headers.get("Subject", "(no subject)"),
                    "from": headers.get("From", "unknown"),
                    "to": headers.get("To", ""),
                    "date": headers.get("Date", ""),
                    "snippet": msg.get("snippet", ""),
                })
            except Exception:
                drafts_list.append({
                    "id": draft_id,
                    "subject": "(unable to fetch)",
                    "from": "unknown",
                    "to": "",
                    "date": "",
                    "snippet": "",
                })
        else:
            drafts_list.append({
                "id": draft_id,
                "subject": "(no message)",
                "from": "unknown",
                "to": "",
                "date": "",
                "snippet": "",
            })

    return ok({
        "total": len(drafts_list),
        "drafts": drafts_list,
    })


# ── Tool: update_draft ──────────────────────────────────────────────────────
def update_draft(
    draft_id: str,
    to: str = "",
    subject: str = "",
    body: str = "",
    cc: str = "",
    bcc: str = "",
    attachments: Optional[list[str]] = None,
) -> dict:
    """
    Update an existing draft. Only non-empty fields are replaced.
    Body is required — drafts must have content.

    Required:
      draft_id (str) — draft ID from list_drafts.
      body (str) — new body text. REQUIRED — drafts must have a body.

    Optional:
      to (str, default="") — new recipient. If empty, keeps existing.
      subject (str, default="") — new subject. If empty, keeps existing.
      cc (str, default="") — new CC.
      bcc (str, default="") — new BCC.
      attachments (list[str] | None, default=None) — replace attachments.

    Returns on success:
      {"draft_id": "...", "message_id": "..."}

    Workflow note: body is the only required field — all others are
      optional and retain their current values if not provided.
      Call list_drafts() first to get the draft_id.
    """
    if attachments is None:
        attachments = []

    if not draft_id or not draft_id.strip():
        return err(ERROR_VALIDATION, "draft_id cannot be empty")

    if not body:
        return err(ERROR_VALIDATION, "body is required when updating a draft")

    try:
        service = _get_service()
    except AuthError as e:
        return err(ERROR_AUTH_MISSING, str(e))
    except Exception as e:
        logger.exception("Unexpected error obtaining Gmail service")
        return err(ERROR_INTERNAL, str(e))

    try:
        current = _with_retry(
            lambda: service.users().drafts().get(userId="me", id=draft_id).execute()
        )
    except HttpError as e:
        if e.resp.status == 404:
            return err(ERROR_DRAFT_NOT_FOUND, f"Draft not found: {draft_id}")
        return err(ERROR_API, str(e))
    except Exception as e:
        return err(ERROR_API, str(e))

    msg = current.get("message", {})
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}

    final_to = to or headers.get("To", "")
    final_subject = subject or headers.get("Subject", "")

    try:
        profile = _with_retry(
            lambda: service.users().getProfile(userId="me").execute()
        )
        sender = profile.get("emailAddress", "")
    except HttpError as e:
        return err(ERROR_API, f"Failed to get sender profile: {e}")
    except Exception as e:
        return err(ERROR_API, f"Failed to get sender profile: {e}")

    try:
        encoded, _ = _build_mime_message(
            to=final_to, subject=final_subject, body=body,
            cc=cc, bcc=bcc, attachments=attachments,
            sender=sender,
        )
    except FileNotFoundError as e:
        return err(ERROR_FILE_NOT_FOUND, str(e))
    except ValueError as e:
        return err(ERROR_VALIDATION, str(e))

    try:
        updated = _with_retry(
            lambda: service.users()
                .drafts()
                .update(userId="me", id=draft_id, body={"message": {"raw": encoded}})
                .execute()
        )
    except HttpError as e:
        return err(ERROR_API, str(e))
    except Exception as e:
        return err(ERROR_API, str(e))

    updated_msg = updated.get("message", {})
    return ok({
        "draft_id": updated.get("id", ""),
        "message_id": updated_msg.get("id", ""),
    })


# ── Tool: delete_draft ──────────────────────────────────────────────────────
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
        _with_retry(
            lambda: service.users().drafts().delete(userId="me", id=draft_id).execute()
        )
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
    return ok({
        "requested": requested,
        "succeeded": succeeded,
        "failed": failed,
        "results": results,
    })


# ── Output size & file helpers ─────────────────────────────────────────────────
# Bytes per email estimate for threshold comparison (JSON-encoded)
_EMAIL_SIZE_ESTIMATE = 6000   # conservative: plain body + headers + markup
_SUMMARY_SIZE_ESTIMATE = 500  # compact summary (date + from + subject + snippet)
_DEFAULT_MAX_CHARS = 30000

# Exposed config dir path for use in save helpers
_CONFIG_DIR = Path.home() / ".config" / "gmail-mcp"


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
    ts = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
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
    body = d.get("body") or d.get("html_body", "") or ""
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
        lines.append(
            f"| {i} | {f['date']} | {f['from']} | "
            f"{f['subject']} | {f['snippet']} |"
        )
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
        # Truncate very long bodies for file size
        if len(body) > 5000:
            body = body[:5000] + "\n\n... *(truncated)*"

        lines.append(f"## Email {i}/{total} {ok_status}\n")
        lines.append(f"- **Date:** {d.get('date', '')}")
        lines.append(f"- **From:** {d.get('from', '')}")
        lines.append(f"- **To:** {d.get('to', '')}")
        lines.append(f"- **Subject:** {d.get('subject', '')}")
        lines.append(f"- **Labels:** {', '.join(d.get('labels', []))}")
        if body:
            lines.append(f"\n**Body:**\n\n{body}")
        lines.append("\n---\n")

    path.write_text("\n".join(lines), encoding="utf-8", errors="replace")
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
        "results": [_extract_email_fields(e) for e in results],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"saved": True, "path": str(path), "count": len(results), "size_bytes": path.stat().st_size}


def _build_jsonl_file(results: list[dict], path: Path) -> dict:
    """Save results as JSON Lines (one JSON per line)."""
    with path.open("w", encoding="utf-8") as f:
        for e in results:
            f.write(json.dumps(_extract_email_fields(e), ensure_ascii=False) + "\n")
    return {"saved": True, "path": str(path), "count": len(results), "size_bytes": path.stat().st_size}


def _build_csv_file(results: list[dict], path: Path) -> dict:
    """Save results as CSV with headers: date, from, subject, snippet, labels."""
    import csv, io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "from", "subject", "snippet", "labels", "id"])
    for e in results:
        f = _extract_email_fields(e)
        w.writerow([f["date"], f["from"], f["subject"], f["snippet"], "|".join(f["labels"]), f["id"]])
    path.write_text(buf.getvalue(), encoding="utf-8")
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
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    plain_body, html_body = _extract_body(msg.get("payload", {}))
    internal_date = msg.get("internalDate", "")
    if internal_date:
        try:
            date_str = formatdate(int(internal_date) / 1000, usegmt=True)
        except Exception:
            date_str = internal_date
    else:
        date_str = headers.get("Date", "")
    return {
        "id": msg.get("id", ""),
        "thread_id": msg.get("threadId", ""),
        "subject": headers.get("Subject", "(no subject)"),
        "from": headers.get("From", "unknown"),
        "to": headers.get("To", "unknown"),
        "date": date_str,
        "body": plain_body,
        "html_body": html_body,
        "labels": msg.get("labelIds", []),
        "snippet": msg.get("snippet", ""),
    }


# ── Tool: read_emails (batch via BatchHttpRequest) ────────────────────────────
def read_emails(
    message_ids: list[str],
    save_to_file: bool = False,
    max_chars: int = _DEFAULT_MAX_CHARS,
) -> dict:
    """
    CHOICE HINT: When the user asks to "read", "show", or "fetch" content from
    multiple emails (e.g. "read these 5 emails", "show me the latest 3 emails"),
    ALWAYS prefer this tool over making N calls to read_email(). It bundles all
    N Gmail API calls into a single HTTP request — far more efficient.

    Fetch full content of multiple emails in a single MCP request.

    Uses googleapiclient.http.BatchHttpRequest — all N calls are bundled
    into ONE HTTP round-trip, regardless of how many message_ids are passed.
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

    results: dict[str, dict] = {}

    def callback(request_id: str, response: dict, exception: Exception) -> None:
        if exception:
            results[request_id] = {"ok": False, "error": ERROR_API, "message": str(exception)}
        else:
            results[request_id] = {"ok": True, "data": _extract_email_data(response)}

    batch = service.new_batch_http_request(callback=callback)
    for mid in message_ids:
        batch.add(
            service.users().messages().get(userId="me", id=mid, format="full"),
            request_id=mid,
        )

    try:
        _with_retry(lambda: batch.execute(http=service._http))
    except Exception as e:
        return err(ERROR_API, str(e))

    out = []
    for mid in message_ids:
        r = results.get(mid, {"ok": False, "error": ERROR_API, "message": "No response received"})
        out.append({"id": mid, **r})

    # Auto-save if forced or estimated size exceeds threshold
    estimated_chars = _estimate_output_size(len(message_ids))
    if save_to_file or estimated_chars > max_chars:
        slug = _build_slug(query="batch-fetch", subject=out[0].get("data", {}).get("subject", "") if out else "")
        path = _make_email_path(len(message_ids), slug)
        saved = _save_results(out, path, "md", query=f"Batch fetch {len(message_ids)} emails")
        return ok({
            "saved": True,
            "path": saved["path"],
            "count": saved["count"],
            "size_bytes": saved["size_bytes"],
            "summary_table": _summary_table(out),
            "requested": len(message_ids),
            "succeeded": sum(1 for r in out if r.get("ok")),
            "failed": sum(1 for r in out if not r.get("ok")),
            "results": [_extract_email_fields(e) for e in out],
        })

    return _batch_ok(None, len(message_ids), out)


# ── Tool: mark_emails_read (batchModify) ──────────────────────────────────────
def mark_emails_read(message_ids: list[str]) -> dict:
    """
    CHOICE HINT: When the user asks to "mark as read" or "mark read" multiple
    emails, ALWAYS prefer this tool over making N calls to mark_read(). It uses
    Gmail's batchModify API — ONE API call regardless of how many message IDs.

    Mark multiple emails as read in a single MCP request.

    Uses messages().batchModify() — ONE Gmail API call regardless of
    how many message_ids are passed (up to 1000 per call).

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

    body = {"removeLabelIds": ["UNREAD"], "ids": message_ids}
    try:
        _with_retry(
            lambda: service.users().messages().batchModify(userId="me", body=body).execute()
        )
    except HttpError as e:
        return err(ERROR_API, str(e))
    except Exception as e:
        return err(ERROR_API, str(e))

    results = [{"id": mid, "ok": True} for mid in message_ids]
    return _batch_ok(None, len(message_ids), results)


# ── Tool: archive_emails (batchModify) ───────────────────────────────────────
def archive_emails(message_ids: list[str]) -> dict:
    """
    CHOICE HINT: When the user asks to "archive" or "remove from inbox" multiple
    emails, ALWAYS prefer this tool over making N calls to archive_email(). It uses
    Gmail's batchModify API — ONE API call regardless of how many message IDs.

    Archive multiple emails in a single MCP request.

    Uses messages().batchModify() — ONE Gmail API call regardless of
    how many message_ids are passed (up to 1000 per call).

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

    body = {"removeLabelIds": ["INBOX"], "ids": message_ids}
    try:
        _with_retry(
            lambda: service.users().messages().batchModify(userId="me", body=body).execute()
        )
    except HttpError as e:
        return err(ERROR_API, str(e))
    except Exception as e:
        return err(ERROR_API, str(e))

    results = [{"id": mid, "ok": True} for mid in message_ids]
    return _batch_ok(None, len(message_ids), results)


# ── Tool: trash_emails (batchModify) ─────────────────────────────────────────
def trash_emails(message_ids: list[str]) -> dict:
    """
    CHOICE HINT: When the user asks to "trash" or "delete" multiple emails,
    ALWAYS prefer this tool over making N calls to trash_email(). It uses
    Gmail's batchModify API — ONE API call regardless of how many message IDs.

    Move multiple emails to trash in a single MCP request.

    Uses messages().batchModify() — ONE Gmail API call regardless of
    how many message_ids are passed (up to 1000 per call).

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

    body = {"addLabelIds": ["TRASH"], "ids": message_ids}
    try:
        _with_retry(
            lambda: service.users().messages().batchModify(userId="me", body=body).execute()
        )
    except HttpError as e:
        return err(ERROR_API, str(e))
    except Exception as e:
        return err(ERROR_API, str(e))

    results = [{"id": mid, "ok": True} for mid in message_ids]
    return _batch_ok(None, len(message_ids), results)


# ── Tool: add_labels (batchModify) ───────────────────────────────────────────
def add_labels(message_ids: list[str], label: str) -> dict:
    """
    CHOICE HINT: When the user asks to "add a label" to multiple emails,
    ALWAYS prefer this tool over making N calls to add_label(). It uses
    Gmail's batchModify API — ONE API call regardless of how many message IDs.

    Add a label to multiple emails in a single MCP request.

    Uses messages().batchModify() — ONE Gmail API call regardless of
    how many message_ids are passed (up to 1000 per call).

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

    body = {"addLabelIds": [label], "ids": message_ids}
    try:
        _with_retry(
            lambda: service.users().messages().batchModify(userId="me", body=body).execute()
        )
    except HttpError as e:
        return err(ERROR_API, str(e))
    except Exception as e:
        return err(ERROR_API, str(e))

    results = [{"id": mid, "ok": True} for mid in message_ids]
    return _batch_ok(None, len(message_ids), results)


# ── Tool: download_attachments (batch via BatchHttpRequest) ────────────────────
def download_attachments(items: list[dict]) -> dict:
    """
    CHOICE HINT: When the user asks to "download" attachments from multiple emails
    or "download all files" from an email thread, ALWAYS prefer this tool over
    making N calls to download_attachment(). It bundles all N Gmail API calls into
    a single HTTP request — far more efficient.

    Download multiple email attachments in a single MCP request.

    Uses googleapiclient.http.BatchHttpRequest — all N calls are bundled
    into ONE HTTP round-trip, regardless of how many items are passed.
    Handles up to 100 attachments per call automatically.

    Required:
      items (list[dict]) — each dict must contain:
        - message_id (str): Gmail message ID
        - attachment_id (str): attachment ID from list_attachments
        - save_path (str): local file path to save to

    Returns on success:
      {"ok": true, "data": {
        "requested": N, "succeeded": N, "failed": 0,
        "results": [{"message_id": "...", "attachment_id": "...", "ok": true, "data": {...}}, ...]
      }}

    Workflow note: Use this instead of N download_attachment() calls.
      Paths are validated against path traversal. Files are renamed
      if they already exist (e.g., "report (1).pdf").
    """
    if not items:
        return _batch_ok(None, 0, [])

    try:
        service = _get_service()
    except AuthError as e:
        return err(ERROR_AUTH_MISSING, str(e))
    except Exception as e:
        logger.exception("Unexpected error obtaining Gmail service")
        return err(ERROR_INTERNAL, str(e))

    results: dict[str, dict] = {}
    config_dir = Path.home()

    def callback(request_id: str, response: dict, exception: Exception) -> None:
        if exception:
            results[request_id] = {"ok": False, "error": ERROR_API, "message": str(exception)}
        else:
            results[request_id] = {"ok": True, "data": response}

    batch = service.new_batch_http_request(callback=callback)

    # Pre-validate all save paths before executing
    validated = []
    out = []
    for item in items:
        mid = item.get("message_id", "")
        att_id = item.get("attachment_id", "")
        save_path = item.get("save_path", "")

        if not mid or not att_id or not save_path:
            out.append({
                "message_id": mid, "attachment_id": att_id,
                "ok": False, "error": ERROR_VALIDATION,
                "message": "Each item needs message_id, attachment_id, and save_path"
            })
            continue

        try:
            safe_path = _safe_save_path(save_path, config_dir)
            final_path = _unique_save_path(safe_path)
        except ValueError as e:
            out.append({
                "message_id": mid, "attachment_id": att_id,
                "ok": False, "error": ERROR_FILE_NOT_FOUND, "message": str(e)
            })
            continue

        validated.append((mid, att_id, str(final_path)))
        batch.add(
            service.users().messages().attachments().get(
                userId="me", messageId=mid, id=att_id
            ),
            request_id=f"{mid}:{att_id}",
        )

    if validated:
        try:
            _with_retry(lambda: batch.execute(http=service._http))
        except Exception as e:
            return err(ERROR_API, str(e))

    # Decode and write files
    for mid, att_id, final_path in validated:
        rid = f"{mid}:{att_id}"
        r = results.get(rid, {"ok": False, "error": ERROR_API, "message": "No response received"})
        if r.get("ok"):
            data_b64 = r["data"].get("data", "")
            if not data_b64:
                r = {"ok": False, "error": ERROR_API, "message": "Empty attachment data"}
            else:
                try:
                    file_data = base64.urlsafe_b64decode(data_b64 + "=" * (-len(data_b64) % 4))
                    with open(final_path, "wb") as f:
                        f.write(file_data)
                    r = {"ok": True, "data": {"filename": "", "path": final_path, "size": len(file_data)}}
                except Exception as e:
                    r = {"ok": False, "error": ERROR_API, "message": str(e)}

        out.append({"message_id": mid, "attachment_id": att_id, **r})

    return _batch_ok(None, len(items), out)


# ── Tool: read_emails_full ─────────────────────────────────────────────────────
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
        "csv"  — CSV with headers: date, from, subject, snippet, labels, id

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
    if not message_ids:
        return ok({"saved": False, "path": "", "count": 0, "format": format, "total_size_bytes": 0})

    try:
        service = _get_service()
    except AuthError as e:
        return err(ERROR_AUTH_MISSING, str(e))
    except Exception as e:
        logger.exception("Unexpected error obtaining Gmail service")
        return err(ERROR_INTERNAL, str(e))

    # Validate path
    config_dir = Path.home()
    try:
        save_p = _safe_save_path(save_path, config_dir)
    except ValueError as e:
        return err(ERROR_FILE_NOT_FOUND, str(e))

    results: dict[str, dict] = {}

    def callback(request_id: str, response: dict, exception: Exception) -> None:
        if exception:
            results[request_id] = {"ok": False, "error": ERROR_API, "message": str(exception)}
        else:
            results[request_id] = {"ok": True, "data": _extract_email_data(response)}

    batch = service.new_batch_http_request(callback=callback)
    for mid in message_ids:
        batch.add(
            service.users().messages().get(userId="me", id=mid, format="full"),
            request_id=mid,
        )

    try:
        _with_retry(lambda: batch.execute(http=service._http))
    except Exception as e:
        return err(ERROR_API, str(e))

    out = []
    for mid in message_ids:
        r = results.get(mid, {"ok": False, "error": ERROR_API, "message": "No response received"})
        out.append({"id": mid, **r})

    # Ensure parent dir exists and write file
    save_p.parent.mkdir(parents=True, exist_ok=True)
    saved = _save_results(out, save_p, format, query=f"Exported {len(message_ids)} emails")

    return ok({
        "saved": True,
        "path": saved["path"],
        "count": saved["count"],
        "format": format,
        "total_size_bytes": saved["size_bytes"],
    })


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
        ~/.config/gmail-mcp/emails/.  Overwrite is rejected — use a fresh
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
    if not query:
        return err(ERROR_VALIDATION, "query must not be empty")

    try:
        service = _get_service()
    except AuthError as e:
        return err(ERROR_AUTH_MISSING, str(e))
    except Exception as e:
        logger.exception("Unexpected error obtaining Gmail service")
        return err(ERROR_INTERNAL, str(e))

    # Determine save path
    config_dir = Path.home()
    if save_path:
        try:
            save_p = _safe_save_path(save_path, config_dir)
        except ValueError as e:
            return err(ERROR_FILE_NOT_FOUND, str(e))
    else:
        slug = _build_slug(query=query)
        save_p = _make_email_path(max_results, slug).with_suffix(f".{format}")
        if format != "md":
            save_p = save_p.with_suffix(f".{format}")

    try:
        # Step 1: search for message IDs
        results = _with_retry(
            lambda: service.users().messages().list(
                userId="me",
                q=query,
                maxResults=max_results,
            ).execute()
        )
    except Exception as e:
        return err(ERROR_API, str(e))

    messages = results.get("messages", [])
    message_ids = [m["id"] for m in messages]
    total_found = results.get("resultSizeEstimate", len(message_ids))

    if not message_ids:
        return ok({
            "saved": False,
            "path": "",
            "count": 0,
            "format": format,
            "total_size_bytes": 0,
            "query": query,
            "search_summary": "No emails matched your search query",
        })

    # Step 2: fetch full content
    results_data: dict[str, dict] = {}

    def callback(request_id: str, response: dict, exception: Exception) -> None:
        if exception:
            results_data[request_id] = {"ok": False, "error": ERROR_API, "message": str(exception)}
        else:
            results_data[request_id] = {"ok": True, "data": _extract_email_data(response)}

    batch = service.new_batch_http_request(callback=callback)
    for mid in message_ids:
        batch.add(
            service.users().messages().get(userId="me", id=mid, format="full"),
            request_id=mid,
        )

    try:
        _with_retry(lambda: batch.execute(http=service._http))
    except Exception as e:
        return err(ERROR_API, str(e))

    out = []
    for mid in message_ids:
        r = results_data.get(mid, {"ok": False, "error": ERROR_API, "message": "No response received"})
        out.append({"id": mid, **r})

    # Step 3: save to file
    save_p.parent.mkdir(parents=True, exist_ok=True)
    saved = _save_results(out, save_p, format, query=query)

    return ok({
        "saved": True,
        "path": saved["path"],
        "count": saved["count"],
        "format": format,
        "total_size_bytes": saved["size_bytes"],
        "query": query,
        "search_summary": f"{saved['count']} emails matched your search query (found {total_found} total)",
    })


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

    config_dir = Path.home()
    try:
        save_p = _safe_save_path(save_path, config_dir)
    except ValueError as e:
        return err(ERROR_FILE_NOT_FOUND, str(e))

    if not save_p.exists():
        return err(ERROR_FILE_NOT_FOUND, f"File not found: {save_path}. Create it first with read_emails_full or search_and_save.")

    suffix = save_p.suffix.lower()
    if suffix not in (".md", ".json", ".jsonl"):
        return err(ERROR_VALIDATION, f"append_emails only supports .md, .json, and .jsonl files. Got: {suffix}")

    try:
        service = _get_service()
    except AuthError as e:
        return err(ERROR_AUTH_MISSING, str(e))
    except Exception as e:
        logger.exception("Unexpected error obtaining Gmail service")
        return err(ERROR_INTERNAL, str(e))

    results_data: dict[str, dict] = {}

    def callback(request_id: str, response: dict, exception: Exception) -> None:
        if exception:
            results_data[request_id] = {"ok": False, "error": ERROR_API, "message": str(exception)}
        else:
            results_data[request_id] = {"ok": True, "data": _extract_email_data(response)}

    batch = service.new_batch_http_request(callback=callback)
    for mid in message_ids:
        batch.add(
            service.users().messages().get(userId="me", id=mid, format="full"),
            request_id=mid,
        )

    try:
        _with_retry(lambda: batch.execute(http=service._http))
    except Exception as e:
        return err(ERROR_API, str(e))

    out = []
    for mid in message_ids:
        r = results_data.get(mid, {"ok": False, "error": ERROR_API, "message": "No response received"})
        out.append({"id": mid, **r})

    # Read existing content and append
    if suffix == ".md":
        existing = save_p.read_text(encoding="utf-8")
        # For .md: find the last --- divider and append after it
        last_divider = existing.rfind("\n---\n")
        if last_divider >= 0:
            new_content = existing[:last_divider + 5] + "\n" + _build_markdown_body_section(out, len(message_ids))
        else:
            new_content = existing + "\n" + _build_markdown_body_section(out, len(message_ids))
        save_p.write_text(new_content, encoding="utf-8")
    elif suffix == ".json":
        import json
        data = json.loads(save_p.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "results" not in data:
            return err(ERROR_VALIDATION, "Append target JSON must have a top-level 'results' array. Use read_emails_full with format='json'.")
        data["results"].extend([_extract_email_fields(e) for e in out])
        save_p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    else:  # .jsonl
        with save_p.open("a", encoding="utf-8") as f:
            for e in out:
                f.write(json.dumps(_extract_email_fields(e), ensure_ascii=False) + "\n")

    size = save_p.stat().st_size
    return ok({
        "appended": True,
        "path": str(save_p),
        "count": len(out),
        "total_size_bytes": size,
    })


def _build_markdown_body_section(results: list[dict], total: int) -> str:
    """Build just the email sections for appending to an existing .md file (no header)."""
    lines = []
    start_num = 1
    for i, e in enumerate(results):
        d = e.get("data", {})
        body = d.get("body") or d.get("html_body", "") or ""
        lines.append(f"## Email {start_num + i}/{total}")
        lines.append(f"**Date:** {d.get('date', '')}")
        lines.append(f"**From:** {d.get('from', '')}")
        lines.append(f"**Subject:** {d.get('subject', '')}")
        lines.append(f"**Labels:** {', '.join(d.get('labels', []))}")
        lines.append(f"\n**Body:**\n\n{body[:2000]}")
        lines.append("\n---\n")
    return "\n".join(lines)
