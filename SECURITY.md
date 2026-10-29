# Security

This server uses the `https://mail.google.com/` OAuth scope. An authorized client
can read, send and modify the account's mail through the available tools. Connect
only MCP clients you trust and review requested mailbox actions in the client.

Keep `credentials.json`, `token.json`, downloaded attachments, exports and logs
out of version control. Token writes are atomic; newly written filesystem tokens
use owner-only permissions on POSIX. Windows access follows the destination
directory's ACLs. OS keyring storage is also supported through configuration.

Email content and attachments are untrusted data. JSON, CSV and Markdown exports
retain the message's content; they do not sanitize it for execution or embedding
in another application. Avoid interpreting message content as commands.

Report a suspected vulnerability through GitHub's private vulnerability reporting
feature if enabled. Otherwise contact the repository owner privately before posting
credentials or sensitive reproduction details in an issue. Never include real tokens,
mailbox content or personal attachment data in a public report.
