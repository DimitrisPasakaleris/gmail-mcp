import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from .config import config

logger = logging.getLogger(__name__)

# Module-level service cache — initialized lazily on first tool call.
_service_cache: Any = None


class AuthError(Exception):
    """Raised when authentication fails in a non-recoverable way."""

    def __init__(self, message: str, requires_auth: bool = False):
        super().__init__(message)
        self.requires_auth = requires_auth


def _is_headless() -> bool:
    """Return True if we appear to be running in a headless environment (no DISPLAY)."""
    return (
        sys.platform != "win32"
        and not os.environ.get("DISPLAY")
        and not os.environ.get("WAYLAND_DISPLAY")
    )


def _migrate_legacy_token() -> None:
    """If token.json exists in the legacy cwd location, move it to config dir."""
    legacy_token = Path.cwd() / "token.json"
    if legacy_token.exists() and not config.token_path.exists():
        config.token_path.parent.mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.copy2(legacy_token, config.token_path)
        logger.info("Migrated token.json from %s to %s", legacy_token, config.token_path)


def _load_token_filesystem() -> Credentials | None:
    _migrate_legacy_token()
    if not config.token_path.exists():
        return None
    return Credentials.from_authorized_user_file(str(config.token_path), config.scopes)


def _save_token_filesystem(creds: Credentials) -> None:
    config.token_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config.token_path, "w") as f:
        f.write(creds.to_json())
    logger.info("Token saved to %s", config.token_path)


def _load_token_keyring() -> Credentials | None:
    try:
        import keyring
    except ImportError:
        return None
    raw = keyring.get_password(config.service_name, "token")
    if not raw:
        return None
    info = json.loads(raw)
    return Credentials.from_authorized_user_info(info, config.scopes)


def _save_token_keyring(creds: Credentials) -> None:
    try:
        import keyring
    except ImportError:
        return
    info = json.loads(creds.to_json())
    keyring.set_password(config.service_name, "token", json.dumps(info))
    logger.info("Token saved to keyring")


def _build_service(creds: Credentials) -> Any:
    return build("gmail", "v1", credentials=creds)


def _attempt_auth_flow() -> Credentials:
    """
    Attempt OAuth flow. In headless environments (no browser),
    raises AuthError immediately instead of blocking.
    """
    if not config.credentials_path.exists():
        raise AuthError(
            f"credentials.json not found at {config.credentials_path}. "
            "Download it from Google Cloud Console > APIs & Services > Credentials, "
            "then place it in the gmail-mcp config directory.",
            requires_auth=True,
        )

    if _is_headless():
        raise AuthError(
            "OAuth browser authentication is required but no browser is available.\n"
            "To authenticate:\n"
            f"  1. Copy your credentials.json to: {config.credentials_path}\n"
            "  2. Run this command in a terminal with a browser:\n"
            "     python -c \"from gmail_mcp.auth import get_gmail_service; get_gmail_service()\"\n"
            "  3. Follow the browser prompts to authorize.\n"
            "  4. Restart Claude Desktop.",
            requires_auth=True,
        )

    logger.info("Starting OAuth browser flow...")
    flow = InstalledAppFlow.from_client_secrets_file(
        str(config.credentials_path), config.scopes
    )
    creds = flow.run_local_server(port=0)
    logger.info("OAuth flow completed successfully.")
    return creds


def _get_valid_credentials() -> Credentials:
    """Load existing credentials or start OAuth. May raise AuthError."""
    creds: Credentials | None = None

    if config.token_storage == "filesystem":
        creds = _load_token_filesystem()
    elif config.token_storage == "keyring":
        creds = _load_token_keyring()

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            logger.info("Refreshing expired token...")
            creds.refresh(Request())
            if config.token_storage == "filesystem":
                _save_token_filesystem(creds)
            elif config.token_storage == "keyring":
                _save_token_keyring(creds)
            return creds
        except Exception as e:
            logger.warning("Token refresh failed: %s. Starting new auth.", e)
            creds = None

    # No valid credentials — try OAuth
    creds = _attempt_auth_flow()

    if config.token_storage == "filesystem":
        _save_token_filesystem(creds)
    elif config.token_storage == "keyring":
        _save_token_keyring(creds)

    return creds


def get_gmail_service() -> Any:
    """
    Returns a cached authenticated Gmail API service object.
    Handles token loading, refresh, and initial auth flow.

    If credentials are missing or expired and OAuth is needed but
    unavailable (headless), raises AuthError with requires_auth=True
    so callers can surface a clear message to the user.

    Never blocks waiting for a browser — will fail fast in headless
    environments instead of hanging.
    """
    global _service_cache

    if _service_cache is not None:
        return _service_cache

    creds = _get_valid_credentials()

    # Verify we got a valid email address
    try:
        temp_svc = _build_service(creds)
        profile = temp_svc.users().getProfile(userId="me").execute()
        email = profile.get("emailAddress", "unknown")
        logger.info("Authenticated as %s", email)
    except Exception as e:
        logger.error("Failed to verify auth: %s", e)
        raise AuthError(f"Authentication verification failed: {e}")

    _service_cache = temp_svc
    return _service_cache


def clear_service_cache() -> None:
    """Clear the cached service. Useful for testing or token refresh."""
    global _service_cache
    _service_cache = None
    logger.info("Service cache cleared")
