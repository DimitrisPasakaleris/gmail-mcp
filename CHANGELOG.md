# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
