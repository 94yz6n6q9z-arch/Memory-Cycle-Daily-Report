"""v3.4.6.1 — Cloudflare bot-detection / TLS-fingerprint hint in RSS fetch failure paths.

Validates:
- _has_cloudflare_headers: detects cf-ray, cf-mitigated, Server: cloudflare
- _is_cloudflare_challenge_body: detects characteristic interstitial markers (≥2 of them)
- _classify_blocking_response: 403/429/503 + CF signature → actionable hint string
- _safe_parse_rss_feed: 403 with CF headers raises RuntimeError WITH the hint
- 200 status but CF challenge body → still raises with the hint
- Generic 403 (no CF) → no hint, just the raw HTTPError text
- _safe_parse_rss_feed records the hinted error into the DiagnosticsCollector
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import requests

from src.data.news import (
    _classify_blocking_response,
    _has_cloudflare_headers,
    _is_cloudflare_challenge_body,
    _safe_parse_rss_feed,
)
from src.diagnostics import DiagnosticsCollector


class TestHasCloudflareHeaders(unittest.TestCase):
    def test_cf_ray_present(self):
        self.assertTrue(_has_cloudflare_headers({"CF-Ray": "abc123-LAX"}))

    def test_cf_mitigated_present(self):
        self.assertTrue(_has_cloudflare_headers({"cf-mitigated": "challenge"}))

    def test_server_cloudflare(self):
        self.assertTrue(_has_cloudflare_headers({"Server": "cloudflare"}))

    def test_no_cloudflare_markers(self):
        self.assertFalse(_has_cloudflare_headers({"Server": "nginx", "X-Powered-By": "PHP"}))

    def test_empty_headers(self):
        self.assertFalse(_has_cloudflare_headers({}))
        self.assertFalse(_has_cloudflare_headers(None))


class TestIsCloudflareChallengeBody(unittest.TestCase):
    def test_classic_interstitial(self):
        body = """
        <html><head><title>Just a moment...</title></head>
        <body>Checking your browser before accessing the site.
        Performance & security by Cloudflare</body></html>
        """
        self.assertTrue(_is_cloudflare_challenge_body(body))

    def test_modern_challenge(self):
        body = """
        <script src="/cdn-cgi/challenge-platform/h/g/orchestrate/chl_page/v1?ray=..."></script>
        Enable JavaScript and cookies to continue.
        """
        self.assertTrue(_is_cloudflare_challenge_body(body))

    def test_single_marker_not_enough(self):
        # "Just a moment..." alone could appear on a legit page
        body = "<html>Just a moment while we redirect you</html>"
        self.assertFalse(_is_cloudflare_challenge_body(body))

    def test_legit_html_not_flagged(self):
        body = "<rss><channel><title>News feed</title></channel></rss>"
        self.assertFalse(_is_cloudflare_challenge_body(body))

    def test_empty_body(self):
        self.assertFalse(_is_cloudflare_challenge_body(""))
        self.assertFalse(_is_cloudflare_challenge_body(None))


class TestClassifyBlockingResponse(unittest.TestCase):
    def _mock_resp(self, status, headers=None, text=""):
        return SimpleNamespace(status_code=status, headers=headers or {}, text=text)

    def test_403_with_cf_ray_returns_tls_hint(self):
        resp = self._mock_resp(403, {"cf-ray": "abc"}, "")
        hint = _classify_blocking_response(resp)
        self.assertIn("Cloudflare", hint)
        self.assertIn("curl-cffi", hint)

    def test_403_with_cf_body_returns_tls_hint(self):
        body = "Just a moment... checking your browser challenge-platform /cdn-cgi/"
        resp = self._mock_resp(403, {}, body)
        hint = _classify_blocking_response(resp)
        self.assertIn("curl-cffi", hint)

    def test_503_with_cf_headers(self):
        resp = self._mock_resp(503, {"server": "cloudflare"}, "")
        hint = _classify_blocking_response(resp)
        self.assertIn("Cloudflare", hint)

    def test_429_with_cf_headers(self):
        resp = self._mock_resp(429, {"cf-ray": "xyz"}, "")
        hint = _classify_blocking_response(resp)
        self.assertIn("Cloudflare", hint)

    def test_403_no_cf_no_hint(self):
        # Plain 403 (dead URL, no CF) → empty hint
        resp = self._mock_resp(403, {"server": "nginx"}, "")
        self.assertEqual(_classify_blocking_response(resp), "")

    def test_200_no_hint(self):
        resp = self._mock_resp(200, {"cf-ray": "xyz"}, "")
        self.assertEqual(_classify_blocking_response(resp), "")

    def test_none_response(self):
        self.assertEqual(_classify_blocking_response(None), "")


class TestSafeParseRSSFeedSurfacesHint(unittest.TestCase):
    """End-to-end: a CF-blocked 403 surfaces the hint via the
    DiagnosticsCollector status.error field that gets rendered in the
    email's Detail/error column."""

    def test_403_cf_block_surfaces_hint_in_diagnostics(self):
        from datetime import datetime, timezone
        cutoff = datetime.now(timezone.utc)

        # Mock a 403 response with Cloudflare headers
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.headers = {"cf-ray": "abc-LAX", "Server": "cloudflare"}
        mock_resp.text = "Just a moment... checking your browser"
        http_error = requests.HTTPError("403 Client Error: Forbidden")
        http_error.response = mock_resp
        mock_resp.raise_for_status = MagicMock(side_effect=http_error)

        collector = DiagnosticsCollector()
        with patch("src.data.news.smart_get", return_value=mock_resp):
            items = _safe_parse_rss_feed(
                source_name="Test CF-Blocked Feed",
                feed_url="https://example.cloudflare-fronted.com/feed.xml",
                cutoff=cutoff,
                source_type="rss",
                collector=collector,
            )

        # No items returned
        self.assertEqual(items, [])
        # Status recorded as fail
        self.assertEqual(len(collector.statuses), 1)
        status = collector.statuses[0]
        self.assertEqual(status.health, "fail")
        # Error string contains the actionable hint
        self.assertIn("Cloudflare", status.error)
        self.assertIn("curl-cffi", status.error)

    def test_legit_403_no_cf_no_hint(self):
        from datetime import datetime, timezone
        cutoff = datetime.now(timezone.utc)

        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.headers = {"Server": "nginx"}
        mock_resp.text = ""
        http_error = requests.HTTPError("403 Client Error: Forbidden")
        http_error.response = mock_resp
        mock_resp.raise_for_status = MagicMock(side_effect=http_error)

        collector = DiagnosticsCollector()
        with patch("src.data.news.smart_get", return_value=mock_resp):
            _safe_parse_rss_feed(
                source_name="Test Dead URL",
                feed_url="https://example.com/feed.xml",
                cutoff=cutoff,
                source_type="rss",
                collector=collector,
            )

        self.assertEqual(len(collector.statuses), 1)
        status = collector.statuses[0]
        self.assertEqual(status.health, "fail")
        self.assertNotIn("Cloudflare", status.error or "")
        self.assertNotIn("curl-cffi", status.error or "")

    def test_200_with_cf_challenge_body_surfaces_hint(self):
        from datetime import datetime, timezone
        cutoff = datetime.now(timezone.utc)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {}
        mock_resp.text = (
            "<html><body>Just a moment... checking your browser "
            "challenge-platform /cdn-cgi/ ray id: abc</body></html>"
        )
        mock_resp.content = mock_resp.text.encode()
        mock_resp.raise_for_status = MagicMock()

        collector = DiagnosticsCollector()
        with patch("src.data.news.smart_get", return_value=mock_resp):
            _safe_parse_rss_feed(
                source_name="Test CF 200 Challenge",
                feed_url="https://example.com/feed.xml",
                cutoff=cutoff,
                source_type="rss",
                collector=collector,
            )

        self.assertEqual(len(collector.statuses), 1)
        status = collector.statuses[0]
        self.assertEqual(status.health, "fail")
        self.assertIn("Cloudflare", status.error)


if __name__ == "__main__":
    unittest.main()
