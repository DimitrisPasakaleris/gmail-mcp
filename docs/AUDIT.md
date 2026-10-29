# Reliability audit — 2026-10-29

The audit reviewed all 25 tools, authentication, configuration, packaging, documentation,
and CI. Gmail responses and failures were simulated; no mail was sent, drafted, deleted,
relabeled, or downloaded from a real account during the audit.

## Findings and corrections

| Area | Observed problem | Correction |
| --- | --- | --- |
| Installation | A fresh install selected MCP 2.x, which does not expose this FastMCP import | Bound MCP below 2 in every dependency declaration |
| Protocol | Console logging and Google's OAuth prompt wrote to stdout | Send logs to stderr, silence the OAuth prompt, tolerate unavailable file logging |
| Authentication | A global httplib2 transport was shared by workers; token writes could be interrupted | Per-thread services, serialized credentials, atomic token replacement |
| OAuth | Desktop macOS was classified as headless; malformed tokens lacked useful errors | Platform-aware detection and actionable authentication errors |
| Replies | Gmail IDs were used as RFC reply headers, and threadId was omitted from sends | Read Message-ID/References, pass threadId, validate the original thread |
| Recipients | Case-sensitive headers and reply-all could include the account itself | Normalize header names, parse addresses, exclude self and duplicates |
| Delivery | A lost API response could cause an automatic duplicate send/draft | Attempt non-idempotent writes once and direct clients to check Gmail before retrying |
| Drafts | Body edits lost existing recipients and files | Read raw MIME, preserve omitted headers and attached/inline parts |
| Batches | Requests exceeded Gmail limits; duplicate message IDs conflicted; callbacks lost input order | Chunk requests and use input positions as callback IDs |
| Labels | Custom names were passed where Gmail expects IDs | Resolve exact custom names or IDs through labels.list |
| Bodies | Text attachments could replace message text; declared charsets and external body IDs were ignored | MIME-aware decoding and retrieval of separately stored body data |
| Downloads | Empty files were rejected, sizes/base64 unchecked, and names could collide | Strict decoding, size limits, exclusive file creation, correct attachment-name lookup |
| Search | Only the first page was exported | Follow page tokens up to the requested limit, rejecting repeated tokens |
| Export | Full exports truncated bodies or saved only summaries | Preserve complete text and structured fields, including per-message errors |
| Append | Markdown tails disappeared; JSON totals stayed stale; malformed files raised exceptions | Preserve existing content, validate shape, maintain totals, replace atomically |
| Concurrency | Simultaneous appends could overwrite one another | Serialize read/modify/write within the server process |
| Configuration | Export paths ignored OS defaults/overrides; README setup did not start OAuth | Use configured paths and document explicit authentication and client launch |
| Validation | Invalid limits/formats reached authentication or silently selected another format | Validate inputs and return structured errors |

## Compatibility and operating notes

- SDK 1.x is supported. SDK 2.x requires a separate migration.
- `send_email` and `create_draft` do not automatically retry API submissions. A timeout
  can occur after Gmail accepted the request; check Gmail before manually repeating it.
- Safe operations retry transient network failures and HTTP 429/503 up to three attempts.
  Valid `Retry-After` values are respected. Delays above 60 seconds surface the error
  immediately instead of waiting or retrying too early.
- `update_draft` requires a new nonempty body. Empty To/Subject/Cc/Bcc arguments retain
  existing values. Omitted attachments retain files; an explicit empty list removes them.
- JSON and JSONL retain the full extracted message record with an `ok` flag. CSV includes
  complete bodies, IDs and errors, with labels encoded as a JSON array. Markdown contains
  full body sections and readable error details. These are content exports, not raw MIME
  mailbox backups; attachment bytes are retrieved through the download tools.
- Search exports reject an already existing explicit path. Explicit `read_emails_full`
  exports can replace an existing destination. Appends preserve existing records and content.
- Automatic exports use the configured directory's `emails/` child. Paths may otherwise
  be inside the user's home or configured directory. Downloads never overwrite existing files.
- Concurrent appends are safe within one running server. Multiple server processes should
  write to different export files; cross-process coordination is outside this implementation.

## Validation

Run from a clean installation with development dependencies:

```bash
python -m pip install -e ".[dev]"
pytest -m "not integration" --cov=gmail_mcp
ruff check .
ruff format --check .
mypy src
python -m pip check
```

The regression suite exercises a real child MCP process through initialize and tools/list,
without invoking Gmail tools. Other checks use simulated Gmail responses, callback order,
partial failures, disk errors, token errors, and concurrent operations. Tests isolate the
home/configuration directories. Live integration remains opt-in through `GMAIL_TEST_REAL=1`.

The audit environment also ran `pip-audit` against installed dependencies and Bandit
against the application source. No known dependency vulnerabilities or medium/high
Bandit findings were reported at the time of the audit. This does not guarantee the
absence of vulnerabilities or functional bugs.

GitHub Actions runs the offline suite on Python 3.11/3.12 on Windows, macOS, and Linux,
alongside Ruff and mypy. Live OAuth, OS keyring integrations and actual Gmail behavior
still require a separately authorized account-based check.
