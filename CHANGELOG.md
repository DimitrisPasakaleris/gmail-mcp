# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- Keep MCP stdio output free of logs and OAuth prompts; use stderr if file logging is unavailable.
- Constrain all installer metadata to MCP SDK 1.x, which provides the FastMCP API used here.
- Pass Gmail thread IDs and RFC Message-ID/References correctly; resolve headers case-insensitively
  and exclude the authenticated user from automatic reply-all recipients.
- Preserve omitted draft recipients, threading, attachments, and nested inline images during edits.
- Avoid automatic retries of sends and draft creation after ambiguous failures; report recovery guidance.
- Bound retry delays and attempts, honor valid Retry-After values, and avoid sleeping after the last attempt.
- Chunk reads/downloads at 100 requests and modifications at 1000 IDs, preserving duplicates,
  result order and partial failures. Resolve custom label names to Gmail IDs.
- Decode declared MIME charsets, skip attached text files, and fetch separately stored text bodies.
- Validate attachment data and size; allow empty files and reserve unique download names exclusively.
- Preserve complete bodies and fields in exports, update JSON totals on append, and retain Markdown tails.
- Write tokens and exports atomically; serialize concurrent appends within a server process.
- Use configured paths consistently; validate result limits and formats; correct authentication/client setup docs.
- Use a separate Google API transport per worker thread and recognize desktop macOS as browser-capable.

### Added
- Regression coverage for protocol startup, threading, ambiguous sends, draft edits, batch boundaries,
  pagination, attachment safety, complete exports, token storage, and concurrent operations.
- Isolated offline test files and credentials, plus an audit report documenting checks and limitations.

### Compatibility
- JSON/JSONL/CSV exports now contain complete records and error status instead of shortened summary fields.
- `update_draft(attachments=None)` keeps files; `attachments=[]` removes them. Empty recipient/subject
  parameters retain their existing values.

## [1.0.0] - 2026-05-17

### Added
- Initial open-source release.
- 25 MCP tools across three families:
  - **Single-message tools**: `list_emails`, `get_thread_messages`, `read_email`,
    `send_email`, `reply_email`, `archive_email`, `trash_email`, `mark_read`,
    `add_label`, `remove_label`, `create_draft`, `list_drafts`, `update_draft`,
    `delete_draft`, `list_attachments`, `download_attachment`.
  - **Batch tools**: `read_emails`, `mark_emails_read`, `archive_emails`,
    `trash_emails`, `add_labels`, `download_attachments`.
  - **File-export tools**: `read_emails_full`, `search_and_save`, `append_emails`.
- OAuth2 flow with filesystem **and** OS keyring token storage backends.
- Automatic legacy `token.json` migration from CWD to the OS config directory.
- Exponential-backoff retry with jitter for HTTP 429/503 and transient network errors;
  respects the `Retry-After` header when present.
- Path-traversal-safe attachment saving with 25 MB per-file ceiling.
- `gmail-mcp` console script and `python -m gmail_mcp` module entry.
- `src/`-layout Python package, MIT licensed, packaged with hatchling.
- GitHub Actions CI (ruff + mypy + pytest on Python 3.11/3.12 across
  Linux, macOS, Windows).

### Changed
- Exception mapping in tool handlers: generic `Exception` during service
  acquisition is now reported as `internal_error` instead of being masked as
  `auth_missing`. Only `AuthError` produces `auth_missing`.

### Security
- `.gitignore` excludes `credentials.json`, `client_secret_*.json`,
  `token.json`, `*.log`, and the built `*.mcpb` bundle.
- OAuth scope is `https://mail.google.com/` (full Gmail access) — documented
  in `SECURITY.md`.

[Unreleased]: https://github.com/DimitrisPasakaleris/gmail-mcp/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/DimitrisPasakaleris/gmail-mcp/releases/tag/v1.0.0
