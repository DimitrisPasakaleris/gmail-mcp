import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

TokenStorage = Literal["filesystem", "keyring"]


def _default_config_dir() -> Path:
    """Return OS-appropriate config directory, taking GMAIL_MCP_CONFIG_DIR env var override."""
    if env_dir := os.environ.get("GMAIL_MCP_CONFIG_DIR"):
        return Path(env_dir)

    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path.home() / ".config"

    return base / "gmail-mcp"


@dataclass
class Config:
    """Global configuration for the Gmail MCP server."""

    token_storage: TokenStorage = "filesystem"
    credentials_path: Path = field(default_factory=lambda: _default_config_dir() / "credentials.json")
    token_path: Path = field(default_factory=lambda: _default_config_dir() / "token.json")
    log_path: Path = field(default_factory=lambda: _default_config_dir() / "gmail-mcp.log")
    scopes: list[str] = field(default_factory=lambda: ["https://mail.google.com/"])
    service_name: str = "gmail-mcp"

    def __post_init__(self):
        # Resolve to absolute paths
        self.credentials_path = Path(self.credentials_path).expanduser().resolve()
        self.token_path = Path(self.token_path).expanduser().resolve()
        self.log_path = Path(self.log_path).expanduser().resolve()


config = Config()
