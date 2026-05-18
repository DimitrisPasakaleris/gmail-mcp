# Troubleshooting

Common failure modes after the OAuth flow has succeeded once. For Google Cloud
console setup problems, see [`google-cloud-setup.md`](google-cloud-setup.md).

## Where to look first

```
~/.config/gmail-mcp/gmail-mcp.log         # Linux
~/Library/Application Support/gmail-mcp/gmail-mcp.log   # macOS
%APPDATA%\gmail-mcp\gmail-mcp.log         # Windows
```

The log rotates at 5 MB with 3 backups. Bump the level to DEBUG by setting
the root logger level in `server.py:_setup_logging()` if you need more
detail while reproducing an issue.

## "credentials.json not found"

The server can't find your OAuth client file.

- Confirm it lives in the OS config dir (see paths above).
- If you put it elsewhere, set `GMAIL_MCP_CONFIG_DIR=/path/to/dir`.
- The file must be named **exactly** `credentials.json` (rename if Google
  Cloud Console gave you something like `client_secret_*.json`).

## OAuth browser doesn't open

You're probably on a headless machine. The server detects this and bails
with a clear message before hanging.

To work around:

1. Run the OAuth flow once on a machine with a browser — same Google account.
2. Copy the resulting `token.json` to the headless machine's config dir.
3. Run the server there. The cached refresh token does not require a browser.

## "Token has been expired or revoked"

The cached `token.json` is stale.

1. Delete `token.json` from the config dir.
2. Run `gmail-mcp` once interactively. A browser opens for re-auth.
3. The new token is saved automatically.

If this keeps happening, check whether the OAuth app is still in *Testing*
status — Google force-expires test-user tokens after 7 days. Move to *In
Production* on the OAuth consent screen if you want a stable token.

## 429 / 503 errors persist after retries

The server already retries 429 and 503 up to 3 times with exponential backoff.
If you still see them, you're hitting Gmail's rate limit:

- **Quota**: 1 billion quota units per day, 250 units/user/sec.
- **Common offenders**: a tight loop over `read_email` (5 units each).
- **Fix**: switch to the batch equivalent (`read_emails`) which is one HTTP
  round-trip up to 100 messages, or insert a small sleep between calls.

## Keyring storage fails silently

If `Config.token_storage = "keyring"` is set but the OS keyring isn't
available (e.g., Linux without `dbus`/`gnome-keyring`), the `keyring`
package raises and the server falls back to no-op. Symptoms: token is
re-requested on every run.

- **Linux**: install `gnome-keyring` or `kwallet` and ensure `dbus` is
  running; alternatively `pip install keyrings.alt` for a file-based fallback.
- **macOS**: should work out of the box (uses the system keychain).
- **Windows**: should work out of the box (uses Credential Manager).

When in doubt, switch back to `"filesystem"` storage and chmod the file.

## "ImportError: cannot import name 'AuthError'"

You're probably running an old version of the file or have the wrong package
on `sys.path`. Verify with:

```bash
python -c "from gmail_mcp.auth import AuthError; print('ok')"
```

If that fails, your editable install is broken — re-run
`pip install -e ".[dev]"` from the project root.

## Tests fail with "ModuleNotFoundError: No module named 'gmail_mcp'"

The package isn't installed. From the project root:

```bash
pip install -e ".[dev]"
```

The `-e` (editable) flag is important — without it, edits in `src/gmail_mcp/`
don't show up in the test run.

## Attachment downloads fail with `file_not_found`

The `save_path` you passed has a parent directory that doesn't exist, or you
don't have write permission. The server creates files but not directories.
Pre-create the directory, or use an existing one.

## "OAuth scope" mismatch warnings

If you've narrowed the scope in `config.py` after a previous OAuth flow ran,
the cached token still carries the **old** scopes. Delete `token.json` and
re-authenticate to mint a fresh token with the new scope set.
