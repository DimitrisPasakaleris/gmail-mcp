# Tool reference

Every MCP tool exposed by `gmail-mcp`, grouped by family. All tools return
either `{"ok": true, "data": ...}` or `{"ok": false, "error": "...", "message": "..."}`.

## Single-message tools

### list_emails

Search the inbox.

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `query` | str | `""` | Gmail search syntax — `"from:alice is:unread"`, `"after:2026/01/01"`, `"has:attachment"`. Empty returns recent inbox. |
| `max_results` | int | `5` | Up to 500. |

Returns `{"total": N, "emails": [{"id", "subject", "from", "date", "snippet", "labels"}, ...]}`.

### get_thread_messages

List all messages in a thread.

| Param | Type | Required |
|-------|------|----------|
| `thread_id` | str | yes — from `list_emails` or `read_email` |

### read_email

Fetch a full message by ID.

| Param | Type | Required |
|-------|------|----------|
| `message_id` | str | yes |

### send_email

Send a message.

| Param | Type | Required | Notes |
|-------|------|----------|-------|
| `to` | str | yes | Comma-separated for multiple recipients. |
| `subject` | str | yes | |
| `body` | str | yes | Plain text. |
| `cc` | str | no | |
| `bcc` | str | no | |
| `thread_id` | str | no | Attach to an existing thread. |
| `in_reply_to` | str | no | Sets `In-Reply-To` header. |
| `references` | str | no | Sets `References` header. |
| `attachments` | list[str] | no | Local file paths. |

### reply_email

Reply to an existing thread (threading headers wired automatically).

| Param | Type | Required |
|-------|------|----------|
| `to` | str | yes |
| `body` | str | yes |
| `thread_id` | str | yes |
| `in_reply_to` | str | yes |
| `subject` | str | no — defaults to `Re: <original>` |
| `cc` / `bcc` | str | no |
| `reply_all` | bool | no — default `False`, set `True` to CC all original recipients |

### archive_email / trash_email / mark_read

Each takes `message_id` (str). `archive_email` removes the `INBOX` label;
`trash_email` moves to Trash; `mark_read` removes `UNREAD`.

### add_label / remove_label

| Param | Type |
|-------|------|
| `message_id` | str |
| `label` | str — system label (`IMPORTANT`, `STARRED`, `CATEGORY_PERSONAL`) or a custom name |

### create_draft

| Param | Type | Required |
|-------|------|----------|
| `to` | str | yes |
| `subject` | str | yes |
| `body` | str | yes |
| `cc` / `bcc` | str | no |
| `attachments` | list[str] | no |

### list_drafts

| Param | Type | Default |
|-------|------|---------|
| `max_results` | int | `5` |

### update_draft

Only non-empty fields replace the existing draft.

| Param | Type | Required |
|-------|------|----------|
| `draft_id` | str | yes |
| `body` | str | yes |
| `to` / `subject` / `cc` / `bcc` | str | no |
| `attachments` | list[str] | no |

### delete_draft

| Param | Type |
|-------|------|
| `draft_id` | str |

### list_attachments

Returns attachment metadata (name, size, attachment_id, mime type) for a message.

| Param | Type |
|-------|------|
| `message_id` | str |

### download_attachment

| Param | Type | Notes |
|-------|------|-------|
| `message_id` | str | |
| `attachment_id` | str | From `list_attachments`. |
| `save_path` | str | Local path. Parent dir must exist. 25 MB ceiling per file. |

## Batch tools

These accept arrays of IDs and bundle all operations into **one MCP request**.
Label operations use Gmail's `batchModify` (1 API call regardless of how many
IDs). Read operations use `BatchHttpRequest` (1 HTTP round-trip, up to 100
sub-requests).

### read_emails

| Param | Type |
|-------|------|
| `message_ids` | list[str] |
| `save_to_file` | bool, optional — force save |
| `max_chars` | int, default `30000` — auto-save threshold |

Auto-save behavior: if the estimated inline output exceeds `max_chars`, the
result is written to `~/.config/gmail-mcp/emails/<timestamp>-<slug>.md` and
the response carries a compact summary + file path instead of full bodies.

### mark_emails_read / archive_emails / trash_emails

| Param | Type |
|-------|------|
| `message_ids` | list[str] |

### add_labels

| Param | Type |
|-------|------|
| `message_ids` | list[str] |
| `label` | str (case-sensitive) |

### download_attachments

| Param | Type |
|-------|------|
| `items` | list[dict] — each dict has `message_id`, `attachment_id`, `save_path` |

Example:

```json
{
  "items": [
    {"message_id": "msg_001", "attachment_id": "att_123", "save_path": "~/Downloads/report.pdf"},
    {"message_id": "msg_002", "attachment_id": "att_456", "save_path": "~/Downloads/invoice.pdf"}
  ]
}
```

### Batch response shape

```json
{
  "ok": true,
  "data": {
    "requested": 5,
    "succeeded": 4,
    "failed": 1,
    "results": [
      {"id": "msg_001", "ok": true, "data": {"...": "..."}},
      {"id": "msg_002", "ok": false, "error": "not_found", "message": "..."}
    ]
  }
}
```

## File-export tools

Save email data to disk and return only a compact confirmation — safe for
hundreds of emails without hitting MCP output limits.

### read_emails_full

| Param | Type | Default |
|-------|------|---------|
| `message_ids` | list[str] | required |
| `save_path` | str | required |
| `format` | str | `"md"` (also: `"json"`, `"jsonl"`, `"csv"`) |

Returns path + count + total size; no email bodies inline.

### search_and_save

`list_emails` + `read_emails_full` in one call.

| Param | Type | Default |
|-------|------|---------|
| `query` | str | required |
| `max_results` | int | `100` |
| `save_path` | str | auto-generated if empty |
| `format` | str | `"md"` |

Example: `search_and_save("from:PiraeusBank after:2026/03/01", max_results=100, format="md")`.

### append_emails

Append more emails to an existing `.md`, `.json`, or `.jsonl` file. Useful
for pagination loops.

| Param | Type |
|-------|------|
| `message_ids` | list[str] |
| `save_path` | str — path to the existing file |
