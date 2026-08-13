#!/usr/bin/env python3
"""Unit tests for storage_rest_router.py."""

from __future__ import annotations

import unittest
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlsplit

import storage_rest_router as r


class JoinBackendUrlTests(unittest.TestCase):
    BASE = "https://ario.ionode.top"

    def test_origin_form_path(self) -> None:
        self.assertEqual(
            r.join_backend_url(self.BASE, "/bundles/123", ""),
            "https://ario.ionode.top/bundles/123",
        )

    def test_preserves_query(self) -> None:
        self.assertEqual(
            r.join_backend_url(self.BASE, "/x", "a=1&b=2"),
            "https://ario.ionode.top/x?a=1&b=2",
        )

    def test_rejects_absolute_http_url(self) -> None:
        with self.assertRaises(ValueError):
            r.join_backend_url(self.BASE, "http://evil.example/steal", "")

    def test_rejects_absolute_https_url(self) -> None:
        with self.assertRaises(ValueError):
            r.join_backend_url(self.BASE, "https://evil.example/steal", "")

    def test_rejects_scheme_relative(self) -> None:
        with self.assertRaises(ValueError):
            r.join_backend_url(self.BASE, "//evil.example/steal", "")

    def test_rejects_file_url(self) -> None:
        with self.assertRaises(ValueError):
            r.join_backend_url(self.BASE, "file:///etc/passwd", "")

    def test_rejects_metadata_url(self) -> None:
        with self.assertRaises(ValueError):
            r.join_backend_url(
                self.BASE,
                "http://169.254.169.254/latest/meta-data/",
                "",
            )

    def test_rejects_backslash(self) -> None:
        with self.assertRaises(ValueError):
            r.join_backend_url(self.BASE, "/foo\\bar", "")

    def test_urljoin_would_have_escaped_but_join_does_not(self) -> None:
        evil = "http://evil.example/steal"
        hijacked = urljoin(self.BASE + "/", evil)
        self.assertEqual(urlsplit(hijacked).netloc, "evil.example")
        with self.assertRaises(ValueError):
            r.join_backend_url(self.BASE, evil, "")

    def test_stays_on_backend_host(self) -> None:
        out = r.join_backend_url(self.BASE, "/a/b", "q=1")
        parts = urlsplit(out)
        self.assertEqual(parts.scheme, "https")
        self.assertEqual(parts.netloc, "ario.ionode.top")
        self.assertEqual(parts.path, "/a/b")


class NormalizeBackendTests(unittest.TestCase):
    def test_strips_trailing_slash(self) -> None:
        self.assertEqual(
            r._normalize_backend_base("https://example.com/"),
            "https://example.com",
        )

    def test_rejects_file_scheme(self) -> None:
        with self.assertRaises(ValueError):
            r._normalize_backend_base("file:///tmp")

    def test_rejects_ftp(self) -> None:
        with self.assertRaises(ValueError):
            r._normalize_backend_base("ftp://example.com")

    def test_rejects_missing_host(self) -> None:
        with self.assertRaises(ValueError):
            r._normalize_backend_base("https://")


class FilterHeadersTests(unittest.TestCase):
    def test_strips_hop_by_hop_and_host(self) -> None:
        out = r.filter_forward_headers(
            {
                "Host": "127.0.0.1:18080",
                "Connection": "keep-alive",
                "Accept": "application/json",
                "X-Request-Id": "abc",
            }
        )
        keys = {k.lower() for k in out}
        self.assertNotIn("host", keys)
        self.assertNotIn("connection", keys)
        self.assertEqual(out["Accept"], "application/json")
        self.assertEqual(out["X-Request-Id"], "abc")


class LoopbackTests(unittest.TestCase):
    def test_loopback_hosts(self) -> None:
        self.assertTrue(r._is_loopback_host("127.0.0.1"))
        self.assertTrue(r._is_loopback_host("::1"))
        self.assertTrue(r._is_loopback_host("localhost"))

    def test_non_loopback(self) -> None:
        self.assertFalse(r._is_loopback_host("0.0.0.0"))
        self.assertFalse(r._is_loopback_host("::"))
        self.assertFalse(r._is_loopback_host("192.168.1.1"))


class ParseHelpersTests(unittest.TestCase):
    def test_retry_statuses(self) -> None:
        self.assertEqual(r._parse_retry_statuses("404, 502 503"), {404, 502, 503})

    def test_listen_ipv4(self) -> None:
        self.assertEqual(r._parse_listen("127.0.0.1:18080"), ("127.0.0.1", 18080))

    def test_listen_ipv6(self) -> None:
        self.assertEqual(r._parse_listen("[::1]:18080"), ("::1", 18080))


class NoRedirectHandlerTests(unittest.TestCase):
    def test_redirect_raises_http_error(self) -> None:
        handler = r._NoRedirectHandler()
        req = urllib.request.Request("https://ario.ionode.top/x")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            handler.redirect_request(
                req,
                fp=None,
                code=302,
                msg="Found",
                headers={},
                newurl="http://127.0.0.1/",
            )
        self.assertEqual(ctx.exception.code, 302)


if __name__ == "__main__":
    unittest.main()
