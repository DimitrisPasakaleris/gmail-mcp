# Google Cloud setup

Getting a `credentials.json` you can hand to `gmail-mcp`. ~5 minutes.

## 1. Create a project

1. Open the [Google Cloud Console](https://console.cloud.google.com/).
2. Top bar → project picker → **New Project**.
3. Name it something like `gmail-mcp` and create.

## 2. Enable the Gmail API

1. In the new project, go to **APIs & Services → Library**.
2. Search for **Gmail API** and click **Enable**.

## 3. Configure the OAuth consent screen

1. **APIs & Services → OAuth consent screen**.
2. User type: **External**, click **Create**.
3. Fill in the required fields (app name, support email, developer email).
   Anything sensible works — this is your own app.
4. **Scopes** step: click **Add or remove scopes**, search for and add
   `https://mail.google.com/`.
   - Note: this is a *restricted* scope. You don't need verification while
     the app is in *Testing* status and you're listed as a test user.
5. **Test users** step: add your own Google account email. Without this you
   will get an `access_denied` error during the OAuth flow.
6. Save and continue back to the dashboard.

## 4. Create an OAuth client

1. **APIs & Services → Credentials**.
2. **+ Create credentials → OAuth client ID**.
3. **Application type: Desktop app**. Name it `gmail-mcp` (or anything).
4. **Create**, then **Download JSON** on the dialog that appears.

## 5. Put the file where gmail-mcp expects it

Rename the downloaded file to `credentials.json` and place it at:

| OS | Path |
|----|------|
| Linux | `~/.config/gmail-mcp/credentials.json` |
| macOS | `~/Library/Application Support/gmail-mcp/credentials.json` |
| Windows | `%APPDATA%\gmail-mcp\credentials.json` |

Or override the directory with `GMAIL_MCP_CONFIG_DIR=/custom/path`.

## 6. Run the OAuth flow

```bash
gmail-mcp
```

(Or `python -m gmail_mcp` if you didn't install the console script.)

A browser tab opens. Sign in with the test-user account, accept the scopes,
and you'll be redirected to a `localhost` URL that shows "The authentication
flow has completed". You can close that tab.

`token.json` is now cached in the same config directory. The server is ready.

## Troubleshooting

- **"This app isn't verified"** — expected while in Testing status. Click
  **Advanced → Go to gmail-mcp (unsafe)**. It's your own OAuth client; the
  warning exists because you (the developer) haven't gone through Google's
  verification process. For personal use this is fine.
- **`access_denied`** — your Google account is not in the **Test users** list
  for this OAuth consent screen. Re-check step 3.5.
- **`invalid_client`** — the `credentials.json` is for the wrong project, or
  the file is corrupted. Re-download.

See [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) for runtime issues after the
OAuth flow succeeds.
