#!/usr/bin/env python3
"""Tests Cloudflare "email protection" decoding in both api/yc.py and yc_scraper.py.

Cloudflare's email obfuscation replaces a real mailto with "[email protected]" plus a
data-cfemail="<hex>" attribute: byte 0 is an XOR key, every later byte is a masked character.
Confirmed live on 2026-10-22 against 10/160 Fall 2026 YC company homepages (e.g. claimkit.co,
herdr.dev) -- without decoding it, those real, site-published addresses were invisible to
EMAIL_RE and the tool silently fell back to a lower-reliability pattern guess instead.

Stdlib only (unittest + unittest.mock), matching this repo's stdlib-only policy.
Run: python3 tests/test_cf_email_decode.py
"""
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "api"))

import yc  # api/yc.py -- the live serverless handler
import yc_scraper  # the CLI, duplicates the same logic on purpose (see README)


def cf_encode(email, key=0x2A):
    """Inverse of Cloudflare's obfuscation, for building test fixtures."""
    return bytes([key] + [b ^ key for b in email.encode("ascii")]).hex()


class CfDecodeTests(unittest.TestCase):
    """Unit tests for the raw decode routine, run against both modules to catch drift between
    the two intentionally-duplicated implementations (api/yc.py vs yc_scraper.py)."""

    def test_decodes_real_shaped_address_both_modules(self):
        hexstr = cf_encode("rachel@claimkit.co")
        self.assertEqual(yc.cf_decode(hexstr), "rachel@claimkit.co")
        self.assertEqual(yc_scraper.cf_decode(hexstr), "rachel@claimkit.co")

    def test_different_xor_key(self):
        hexstr = cf_encode("hey@herdr.dev", key=0x7)
        self.assertEqual(yc.cf_decode(hexstr), "hey@herdr.dev")
        self.assertEqual(yc_scraper.cf_decode(hexstr), "hey@herdr.dev")

    def test_malformed_hex_does_not_raise(self):
        for bad in ("", "a", "zz", "2a", "2a0", "not-hex-at-all"):
            self.assertIsNone(yc.cf_decode(bad))
            self.assertIsNone(yc_scraper.cf_decode(bad))


class SiteEmailsIntegrationTests(unittest.TestCase):
    """Exercises the full site_emails() pipeline (fetch -> plain-text + cf-decoded candidates ->
    domain/BAD_EMAIL filtering) with a mocked fetch, so it runs offline and deterministically."""

    def _page_with(self, email, plain_email=None):
        hexstr = cf_encode(email)
        plain = f'<a href="mailto:{plain_email}">contact</a>' if plain_email else ""
        return (
            f'<html><body>{plain}'
            f'<a href="/cdn-cgi/l/email-protection#{hexstr}" class="__cf_email__" '
            f'data-cfemail="{hexstr}">[email&#160;protected]</a></body></html>'
        )

    def test_api_yc_recovers_cf_protected_email_on_matching_domain(self):
        page = self._page_with("rachel@claimkit.co")
        with mock.patch.object(yc, "get", return_value=page):
            self.assertEqual(yc.site_emails("https://claimkit.co", "claimkit.co"), ["rachel@claimkit.co"])

    def test_yc_scraper_recovers_cf_protected_email_on_matching_domain(self):
        page = self._page_with("rachel@claimkit.co")
        with mock.patch.object(yc_scraper, "get", return_value=page):
            self.assertEqual(
                yc_scraper.site_emails("https://claimkit.co", "claimkit.co"), ["rachel@claimkit.co"]
            )

    def test_cf_protected_email_on_other_domain_is_dropped(self):
        # A support widget / embedded third-party script can carry its own cf-protected address;
        # the domain filter (same protection plain-text emails already get) must still apply.
        page = self._page_with("support@some-other-vendor.com")
        with mock.patch.object(yc, "get", return_value=page):
            self.assertEqual(yc.site_emails("https://claimkit.co", "claimkit.co"), [])

    def test_plain_text_and_cf_protected_emails_combine_deduped(self):
        page = self._page_with("rachel@claimkit.co", plain_email="hello@claimkit.co")
        with mock.patch.object(yc, "get", return_value=page):
            self.assertEqual(
                yc.site_emails("https://claimkit.co", "claimkit.co"),
                ["hello@claimkit.co", "rachel@claimkit.co"],
            )

    def test_garbage_cfemail_attribute_does_not_crash_or_leak(self):
        page = '<span class="__cf_email__" data-cfemail="not-valid-hex">[email protected]</span>'
        with mock.patch.object(yc, "get", return_value=page):
            self.assertEqual(yc.site_emails("https://claimkit.co", "claimkit.co"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
