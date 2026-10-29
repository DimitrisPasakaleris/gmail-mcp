# Gmail MCP

[![CI](https://github.com/DimitrisPasakaleris/gmail-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/DimitrisPasakaleris/gmail-mcp/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-Model_Context_Protocol-orange.svg)](https://modelcontextprotocol.io)

An MCP server that lets Claude (Desktop, Code, or any MCP client) read, search,
send, label, and manage your Gmail through OAuth2. 25 tools, batch operations,
file-export for large result sets, retry-with-backoff, and OS-keyring token
storage.

## Highlights

- **25 tools** across single, batch, and file-export families — see [`docs/TOOLS.md`](docs/TOOLS.md).
- **Batch operations** with automatic chunks of 100 message reads/downloads
  or 1000 label updates per Gmail request, preserving order and partial failures.
- **File-export tools** (`read_emails_full`, `search_and_save`, `append_emails`)
  save results to `.md` / `.json` / `.jsonl` / `.csv` so MCP output limits
  aren't hit when working with hundreds of emails.
- **Resilient**: bounded retry on safe operations, respecting `Retry-After`.
  Sending mail and creating drafts are attempted once to avoid duplicates.
- **Secure token storage** via OS keyring (optional) or filesystem (default).
- **Tested**: offline regression tests, mocked Gmail responses, and an actual MCP
  startup/tool-listing test. CI covers Windows, macOS, and Linux.

## Quick start

```bash
pip install -e .                      # install in editable mode
# (or once published: pipx install gmail-mcp)
```

Then:

1. Get a `credentials.json` from Google Cloud Console — see
   [`docs/google-cloud-setup.md`](docs/google-cloud-setup.md).
2. Place it at the [OS config directory](#configuration): Linux uses
   `~/.config/gmail-mcp/`; macOS uses `~/Library/Application Support/gmail-mcp/`;
   Windows uses `%APPDATA%\gmail-mcp\`.
3. Authenticate in a terminal with a browser, using the Python environment
   where you installed this package:

   ```bash
   python -c "from gmail_mcp.auth import get_gmail_service; get_gmail_service()"
   ```

   This opens Google's sign-in page and caches the token. Server startup is lazy:
   launching `gmail-mcp` alone waits for MCP requests and does not start sign-in.
4. Add this server to your MCP client's configuration. For Claude Desktop,
   merge the following into `claude_desktop_config.json`:

   ```json
   {
     "mcpServers": {
       "gmail-mcp": {
         "command": "/absolute/path/to/your/python",
         "args": ["-m", "gmail_mcp"]
       }
     }
   }
   ```

   Replace `command` with the Python executable from the installed environment.
   On Windows, use an escaped path such as `C:\\path\\to\\venv\\Scripts\\python.exe`.
   Other MCP clients can launch the same command and arguments, or `gmail-mcp`.
5. Restart Claude Desktop. Try: *"List my 5 most recent unread emails."*

## Configuration

Set environment variables (or edit `src/gmail_mcp/config.py`):

| Variable | Default | Effect |
|----------|---------|--------|
| `GMAIL_MCP_CONFIG_DIR` | OS-specific (see below) | Overrides where `credentials.json`, `token.json`, and `gmail-mcp.log` live. |

OS-specific config dir defaults:

- **Linux**: `~/.config/gmail-mcp/`
- **macOS**: `~/Library/Application Support/gmail-mcp/`
- **Windows**: `%APPDATA%\gmail-mcp\`

To switch to OS keyring storage, set `Config.token_storage = "keyring"` in
`config.py` and delete the cached `token.json` so the OAuth flow re-runs.

## Tools at a glance

| Family | Tools |
|--------|-------|
| **Single** | `list_emails`, `get_thread_messages`, `read_email`, `send_email`, `reply_email`, `archive_email`, `trash_email`, `mark_read`, `add_label`, `remove_label`, `create_draft`, `list_drafts`, `update_draft`, `delete_draft`, `list_attachments`, `download_attachment` |
| **Batch** | `read_emails`, `mark_emails_read`, `archive_emails`, `trash_emails`, `add_labels`, `download_attachments` |
| **File export** | `read_emails_full`, `search_and_save`, `append_emails` |

Full parameter reference and examples: [`docs/TOOLS.md`](docs/TOOLS.md).

## Documentation

- [`docs/google-cloud-setup.md`](docs/google-cloud-setup.md) — step-by-step OAuth client setup.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — module map, request flow, design decisions.
- [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) — common errors and fixes.
- [`docs/TOOLS.md`](docs/TOOLS.md) — every tool, every parameter, every response.
- [`docs/AUDIT.md`](docs/AUDIT.md) — reliability audit, compatibility changes, and validation limits.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for dev setup, tests, and the PR
checklist. Bug reports and feature requests welcome via GitHub Issues.

## License

[MIT](LICENSE).
