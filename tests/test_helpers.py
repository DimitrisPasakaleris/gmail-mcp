import base64
from gmail_mcp.tools import _parse_recipients, _extract_body, _attachment_id_to_part


class TestParseRecipients:
    def test_name_addr_format(self):
        result = _parse_recipients("Alice <alice@example.com>")
        assert result == ["alice@example.com"]

    def test_bare_email(self):
        result = _parse_recipients("bob@example.com")
        assert result == ["bob@example.com"]

    def test_mixed_comma_separated(self):
        result = _parse_recipients("Alice <alice@example.com>, bob@example.com")
        assert result == ["alice@example.com", "bob@example.com"]

    def test_multiple_name_addr(self):
        result = _parse_recipients("Alice <alice@example.com>, Bob <bob@example.com>")
        assert result == ["alice@example.com", "bob@example.com"]

    def test_duplicates_deduplicated(self):
        result = _parse_recipients("Alice <alice@example.com>, alice@example.com")
        assert result == ["alice@example.com"]

    def test_empty_string(self):
        result = _parse_recipients("")
        assert result == []

    def test_no_at_symbol(self):
        result = _parse_recipients("not-an-email")
        assert result == []

    def test_whitespace_stripped(self):
        result = _parse_recipients("  alice@example.com  ")
        assert result == ["alice@example.com"]


class TestExtractBody:
    def _body_payload(self, text: str, mime: str = "text/plain") -> dict:
        enc = base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")
        return {"mimeType": mime, "body": {"data": enc}}

    def test_simple_text_plain(self):
        payload = self._body_payload("Hello world")
        plain, html = _extract_body(payload)
        assert plain == "Hello world"
        assert html is None

    def test_simple_text_html(self):
        payload = self._body_payload("<p>HTML</p>", "text/html")
        plain, html = _extract_body(payload)
        assert plain is None
        assert html == "<p>HTML</p>"

    def test_multipart_alternative(self):
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(b"plain").decode().rstrip("=")}},
                {"mimeType": "text/html", "body": {"data": base64.urlsafe_b64encode(b"<p>html</p>").decode().rstrip("=")}},
            ],
        }
        plain, html = _extract_body(payload)
        assert plain == "plain"
        assert html == "<p>html</p>"

    def test_deeply_nested(self):
        deep = {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {"mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(b"deep plain").decode().rstrip("=")}},
                    ],
                },
            ],
        }
        plain, html = _extract_body(deep)
        assert plain == "deep plain"

    def test_no_matching_part(self):
        payload = {"mimeType": "image/png", "body": {}}
        plain, html = _extract_body(payload)
        assert plain is None
        assert html is None

    def test_empty_payload(self):
        plain, html = _extract_body({})
        assert plain is None
        assert html is None


class TestAttachmentIdToPart:
    def test_found_at_top_level(self):
        payload = {
            "parts": [
                {"filename": "doc.pdf", "body": {"attachmentId": "att_abc", "size": 100}},
            ]
        }
        result = _attachment_id_to_part(payload, "att_abc")
        assert result is not None
        assert result["filename"] == "doc.pdf"

    def test_found_deeply_nested(self):
        payload = {
            "parts": [
                {
                    "parts": [
                        {
                            "parts": [
                                {"filename": "deep.txt", "body": {"attachmentId": "att_deep", "size": 50}},
                            ]
                        }
                    ]
                }
            ]
        }
        result = _attachment_id_to_part(payload, "att_deep")
        assert result is not None
        assert result["filename"] == "deep.txt"

    def test_not_found(self):
        payload = {
            "parts": [
                {"filename": "other.txt", "body": {"attachmentId": "att_other", "size": 10}}
            ]
        }
        result = _attachment_id_to_part(payload, "att_nonexistent")
        assert result is None

    def test_no_attachment_id(self):
        payload = {"parts": [{"filename": "no_id.txt", "body": {"size": 10}}]}
        result = _attachment_id_to_part(payload, "att_missing")
        assert result is None

    def test_empty_parts(self):
        result = _attachment_id_to_part({}, "att_any")
        assert result is None
