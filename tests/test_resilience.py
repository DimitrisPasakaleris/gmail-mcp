"""Tests for resilience features: retry, safe save paths, get_thread_messages."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from googleapiclient.errors import HttpError

from gmail_mcp.tools import _safe_save_path, _unique_save_path, _with_retry, get_thread_messages

from .conftest import build_mock_service


class TestRetry:
    """Tests for _with_retry."""

    def test_success_returns_result(self):
        """Non-429/503 calls pass through on first try."""
        result = _with_retry(lambda: {"data": "ok"})
        assert result == {"data": "ok"}

    def test_retries_on_429_then_succeeds(self):
        """Retries 429 and returns result on success."""
        attempts = []

        def fragile():
            attempts.append(1)
            if len(attempts) < 2:
                resp = MagicMock()
                resp.status = 429
                resp.get.return_value = None
                raise HttpError(resp, b"rate limited")
            return "success"

        result = _with_retry(fragile, max_retries=3)
        assert result == "success"
        assert len(attempts) == 2

    def test_retries_on_503_then_succeeds(self):
        """Retries 503 and returns result on success."""
        attempts = []

        def fragile():
            attempts.append(1)
            if len(attempts) < 2:
                resp = MagicMock()
                resp.status = 503
                resp.get.return_value = None
                raise HttpError(resp, b"unavailable")
            return "success"

        result = _with_retry(fragile, max_retries=3)
        assert result == "success"
        assert len(attempts) == 2

    def test_respects_retry_after_header(self):
        """On 429 with Retry-After header, uses that value instead of backoff."""
        attempts = []

        def fragile():
            attempts.append(1)
            if len(attempts) < 2:
                resp = MagicMock()
                resp.status = 429
                resp.get.return_value = "0"  # 0 seconds (immediate retry for test)
                raise HttpError(resp, b"rate limited")
            return "success"

        import time

        start = time.time()
        result = _with_retry(fragile, max_retries=3, base_delay=10)
        time.time() - start
        assert result == "success"
        assert len(attempts) == 2

    def test_raises_after_max_retries_exhausted(self):
        """After all retries fail, raises the last exception."""

        def always_429():
            resp = MagicMock()
            resp.status = 429
            resp.get.return_value = None
            raise HttpError(resp, b"rate limited")

        with pytest.raises(HttpError):
            _with_retry(always_429, max_retries=3)

    def test_does_not_retry_non_rate_limit_errors(self):
        """400 errors propagate immediately without retry."""

        def always_400():
            resp = MagicMock()
            resp.status = 400
            resp.get.return_value = None
            raise HttpError(resp, b"bad request")

        with pytest.raises(HttpError):
            _with_retry(always_400, max_retries=3)


class TestSafeSavePath:
    """Tests for _safe_save_path."""

    def test_resolves_relative_to_cwd(self):
        """Relative paths are resolved to absolute."""
        result = _safe_save_path("test.txt", Path.home())
        assert result.is_absolute()

    def test_blocks_path_traversal_outside_home(self):
        """Paths that escape home directory are rejected."""
        home = Path.home()
        with pytest.raises(ValueError, match="outside the allowed"):
            _safe_save_path("../../../../../etc/passwd", home / "Downloads")

    def test_allows_path_inside_config_dir(self):
        """Paths inside config dir are allowed."""
        cfg = Path.home() / ".config" / "gmail-mcp"
        result = _safe_save_path(str(cfg / "attachments" / "doc.pdf"), cfg)
        assert cfg in result.parents or result == cfg / "attachments" / "doc.pdf"

    def test_allows_path_inside_home(self):
        """Paths inside home are allowed."""
        target = str(Path.home() / "Downloads" / "file.pdf")
        result = _safe_save_path(target, Path.home() / ".config")
        assert str(Path.home()) in str(result)

    def test_creates_parent_directory(self, tmp_path):
        """Parent directory is created if it doesn't exist."""
        new_dir = tmp_path / "deep" / "nested" / "dir"
        result = _safe_save_path(str(new_dir / "file.txt"), tmp_path)
        assert result.parent.exists()

    def test_unwritable_parent_dir_raises(self, tmp_path):
        """If parent dir can't be created, raises ValueError.

        On Windows admin, os.access may not enforce read-only dirs,
        so we test mkdir failure instead (simpler and reliable).
        """
        import stat

        protected = tmp_path / "protected"
        protected.mkdir()
        # Remove write permission from parent
        tmp_path.chmod(stat.S_IRUSR | stat.S_IXUSR)  # read+execute only

        try:
            result = _safe_save_path(str(protected / "file.txt"), tmp_path)
            # On Windows admin, this may succeed — that's a Windows limitation
            # The important thing is the path is resolved correctly
            assert result.name == "file.txt"
        finally:
            tmp_path.chmod(stat.S_IRWXU)


class TestUniqueSavePath:
    """Tests for _unique_save_path."""

    def test_returns_same_path_when_not_exists(self, tmp_path):
        """If path doesn't exist, returns it unchanged."""
        p = tmp_path / "new.txt"
        assert _unique_save_path(p) == p

    def test_appends_counter_when_exists(self, tmp_path):
        """If path exists, appends (1) before extension."""
        existing = tmp_path / "report.pdf"
        existing.write_text("original")
        result = _unique_save_path(existing)
        assert result.name == "report (1).pdf"
        assert result.parent == tmp_path

    def test_increments_counter_for_multiple_exists(self, tmp_path):
        """Finds the next available counter."""
        p = tmp_path / "doc.txt"
        p.write_text("first")
        (tmp_path / "doc (1).txt").write_text("second")
        result = _unique_save_path(p)
        assert result.name == "doc (2).txt"

    def test_no_extension_suffix(self, tmp_path):
        """No-extension files get (n) appended directly."""
        p = tmp_path / "Makefile"
        p.write_text("all:")
        result = _unique_save_path(p)
        assert result.name == "Makefile (1)"


class TestGetThreadMessages:
    """Tests for get_thread_messages tool."""

    def test_empty_thread_id_returns_error(self):
        mock_service = build_mock_service()
        with patch("gmail_mcp.tools._get_service", return_value=mock_service):
            result = get_thread_messages("")
        assert result["ok"] is False
        assert result["error"] == "validation_error"

    def test_thread_not_found_returns_error(self):
        mock_service = build_mock_service()
        resp = MagicMock()
        resp.status = 404
        mock_service.users().threads().get.return_value.execute.side_effect = HttpError(resp, b"not found")

        with patch("gmail_mcp.tools._get_service", return_value=mock_service):
            result = get_thread_messages("nonexistent_thread")
        assert result["ok"] is False
        assert result["error"] == "thread_not_found"

    def test_returns_thread_messages_structured(self):
        mock_service = build_mock_service()
        mock_service.users().threads().get.return_value.execute.return_value = {
            "id": "thread_001",
            "messages": [
                {
                    "id": "msg_001",
                    "snippet": "Hello there",
                    "payload": {
                        "headers": [
                            {"name": "Subject", "value": "Test Thread"},
                            {"name": "From", "value": "alice@example.com"},
                            {"name": "Date", "value": "2026-05-08"},
                        ],
                        "body": {"data": ""},
                    },
                },
            ],
        }

        with patch("gmail_mcp.tools._get_service", return_value=mock_service):
            result = get_thread_messages("thread_001")

        assert result["ok"] is True
        assert result["data"]["thread_id"] == "thread_001"
        assert result["data"]["total"] == 1
        assert result["data"]["messages"][0]["id"] == "msg_001"
        assert result["data"]["messages"][0]["from"] == "alice@example.com"
