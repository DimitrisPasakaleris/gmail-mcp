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
- **Batch operations** that bundle up to 100 Gmail calls per MCP request
  (`read_emails`, `add_labels`, `archive_emails`, …) for fast bulk workflows.
- **File-export tools** (`read_emails_full`, `search_and_save`, `append_emails`)
  save results to `.md` / `.json` / `.jsonl` / `.csv` so MCP output limits
  aren't hit when working with hundreds of emails.
- **Resilient**: exponential-backoff retry on 429/503, respects `Retry-After`,
  recovers from transient network failures.
- **Secure token storage** via OS keyring (optional) or filesystem (default).
- **Tested**: ~700 lines of unit tests, mocked Google client, no network in CI.

## Quick start

```bash
pip install -e .                      # install in editable mode
# (or once published: pipx install gmail-mcp)
```

Then:

1. Get a `credentials.json` from Google Cloud Console — see
   [`docs/google-cloud-setup.md`](docs/google-cloud-setup.md).
2. Place it at the OS config dir (`~/.config/gmail-mcp/credentials.json` on
   Linux/macOS, `%APPDATA%\gmail-mcp\credentials.json` on Windows).
3. Run `gmail-mcp` once in a terminal — it opens a browser for the OAuth flow
   and caches the token.
4. Register with Claude Desktop:

   ```bash
   mcp install src/gmail_mcp/server.py --name gmail-mcp
   ```

   Or, if you installed via pip, the `gmail-mcp` console script works too:

   ```bash
   mcp install $(which gmail-mcp) --name gmail-mcp
   ```

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

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for dev setup, tests, and the PR
checklist. Bug reports and feature requests welcome via GitHub Issues.

## License

[MIT](LICENSE).
