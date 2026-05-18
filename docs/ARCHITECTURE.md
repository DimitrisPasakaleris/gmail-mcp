# Architecture

A high-level map of how the package is wired, for contributors who want to
make changes without spelunking through 2,300 lines of `tools.py`.

## Module map

```
src/gmail_mcp/
├── __init__.py     # package marker, exposes __version__
├── __main__.py     # enables `python -m gmail_mcp`
├── server.py       # FastMCP wiring + console-script entry (main())
├── tools.py        # all 25 MCP tools, retry helper, MIME builders
├── auth.py         # OAuth flow, token storage, service caching
└── config.py       # dataclass + OS-aware paths + GMAIL_MCP_CONFIG_DIR
```

## Request flow

```
MCP client (Claude Desktop, Code, …)
        │
        │  stdio / SSE
        ▼
FastMCP runtime  ──────  server.build_server()  ◀─── tool functions imported from tools.py
        │
        │  invokes the matching Python callable
        ▼
tools.<tool_name>(...)
        │
        ├──► get_gmail_service()      ◀── auth.py
        │           │
        │           ├── load token (filesystem or keyring)
        │           ├── refresh if expired
        │           ├── run OAuth flow if missing
        │           └── return cached googleapiclient service
        │
        └──► _with_retry(lambda: service.users()...execute())
                    │
                    ├── retry on HttpError 429 / 503 (respects Retry-After)
                    ├── retry on TimeoutError / ConnectionError / OSError
                    └── exponential backoff with ±20% jitter

Tools always return `{"ok": True, "data": ...}` or `{"ok": False, "error": "...", "message": "..."}`.
```

## Tool families

| Family | Suffix pattern | API mechanism | Use when |
|--------|----------------|---------------|----------|
| **Single** | `read_email`, `add_label`, `archive_email`, … | 1 Gmail API call per tool invocation | One message at a time |
| **Batch** | `read_emails`, `add_labels`, `archive_emails`, … | `batchModify` (1 call) or `BatchHttpRequest` (1 round-trip, up to 100 calls) | Operating on many message IDs at once |
| **File export** | `read_emails_full`, `search_and_save`, `append_emails` | Same as batch, but writes the result to disk | Result set is too big for the MCP output channel |

The single/batch pairs exist deliberately — they are **not** code duplication.
Single tools are the obvious common case; batch tools let an MCP client process
many messages without N round-trips of latency.

## Error code taxonomy

Defined as constants near the top of `tools.py`:

| Code | When |
|------|------|
| `auth_missing` | `credentials.json` absent or first-time auth failed (raises `AuthError`) |
| `auth_expired` | Token refresh failed; user needs to re-auth |
| `api_error` | Gmail API returned an error (4xx/5xx that isn't retried) |
| `not_found` | Message / thread / draft does not exist |
| `validation_error` | Bad input from the MCP client (empty recipient, malformed ID, …) |
| `attachment_not_found` | The attachment ID isn't present on the message |
| `file_not_found` | Local file expected (e.g., for `update_draft` attachments) is missing |
| `draft_not_found` | Draft ID does not exist |
| `invalid_label` | Label name doesn't resolve to an existing label |
| `thread_not_found` | Thread ID does not exist |
| `internal_error` | Unexpected exception not covered by the above |

## Configuration resolution

`config.py` resolves paths in this order:

1. `GMAIL_MCP_CONFIG_DIR` env var, if set.
2. OS default: `%APPDATA%\gmail-mcp` (Windows), `~/Library/Application Support/gmail-mcp` (macOS), `~/.config/gmail-mcp` (Linux).

Three files live in that directory:

- `credentials.json` — OAuth client (you supply this, never committed to git).
- `token.json` — cached user token (created by the OAuth flow; alternative: OS keyring).
- `gmail-mcp.log` — rotating log file (5 MB × 3 backups).

## Service caching

`auth._service_cache` holds the authenticated `googleapiclient` resource at
module level. First tool call builds it; subsequent calls reuse it. Tests
clear it via `auth.clear_service_cache()`.

## Why FastMCP

`FastMCP` from the official `mcp` SDK gives us:

- Auto-derived tool schema from Python type hints + docstrings.
- stdio transport baked in for Claude Desktop integration.
- No request/response plumbing to write.

The tool functions stay plain `def foo(...) -> dict:` — easy to unit-test
without spinning up the MCP runtime.
