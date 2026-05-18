"""Gmail MCP server entry point.

Wires the Gmail tools into a FastMCP server and starts the runtime.
Use the `gmail-mcp` console script (from pyproject.toml) or
`python -m gmail_mcp` to launch.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from mcp.server.fastmcp import FastMCP

from .config import config
from .tools import (
    list_emails,
    get_thread_messages,
    read_email,
    send_email,
    reply_email,
    archive_email,
    trash_email,
    mark_read,
    add_label,
    remove_label,
    create_draft,
    list_drafts,
    update_draft,
    delete_draft,
    list_attachments,
    download_attachment,
    # batch tools
    read_emails,
    mark_emails_read,
    archive_emails,
    trash_emails,
    add_labels,
    download_attachments,
    # file export tools
    read_emails_full,
    search_and_save,
    append_emails,
)


def _setup_logging() -> None:
    """Configure structured logging to file and console."""
    config.token_path.parent.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        handlers=[
            RotatingFileHandler(
                config.log_path, encoding="utf-8", maxBytes=5 * 1024 * 1024, backupCount=3
            ),
            logging.StreamHandler(sys.stdout),
        ],
    )

    # Reduce noise from googleapiclient
    logging.getLogger("googleapiclient.discovery").setLevel(logging.WARNING)
    logging.getLogger("google.auth.transport.requests").setLevel(logging.WARNING)

    logger = logging.getLogger(__name__)
    logger.info("Gmail MCP server starting")
    logger.info("Config directory: %s", config.token_path.parent)
    logger.info("Credentials:     %s", config.credentials_path)
    logger.info("Token:           %s", config.token_path)
    logger.info("Log:             %s", config.log_path)


def build_server() -> FastMCP:
    """Construct the FastMCP instance with all Gmail tools registered."""
    mcp = FastMCP("Gmail MCP")

    tools = (
        list_emails,
        get_thread_messages,
        read_email,
        send_email,
        reply_email,
        archive_email,
        trash_email,
        mark_read,
        add_label,
        remove_label,
        create_draft,
        list_drafts,
        update_draft,
        delete_draft,
        list_attachments,
        download_attachment,
        read_emails,
        mark_emails_read,
        archive_emails,
        trash_emails,
        add_labels,
        download_attachments,
        read_emails_full,
        search_and_save,
        append_emails,
    )
    for tool in tools:
        mcp.add_tool(tool)

    logging.getLogger(__name__).info("All %d tools registered", len(tools))
    return mcp


def main() -> None:
    """Console-script entry point: configure logging and run the MCP server."""
    _setup_logging()
    logger = logging.getLogger(__name__)
    mcp = build_server()
    logger.info("Server entering mcp.run()")
    mcp.run()


if __name__ == "__main__":
    main()
