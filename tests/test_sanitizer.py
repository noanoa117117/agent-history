import json
import unittest

from agent_history.sanitizer import (
    REDACTED_EMAIL,
    REDACTED_IP,
    REDACTED_PRIVATE_KEY,
    REDACTED_SECRET,
    normalize_json,
    sanitize_text,
)


class SanitizerTests(unittest.TestCase):
    def test_secrets_email_ip_and_private_key_are_redacted(self):
        text = "\n".join(
            [
                "admin@corp.internal",
                "203.0.113.9",
                "Authorization: Bearer abcdef123456",
                "api_key: abcdef1234567890",
                "ghp_abcdefghijklmnopqrstuvwxyz1234567890",
                "-----BEGIN PRIVATE KEY-----",
                "fake-key-material",
                "-----END PRIVATE KEY-----",
            ]
        )
        result = sanitize_text(text)
        self.assertTrue(result.changed)
        self.assertIn(REDACTED_EMAIL, result.text)
        self.assertIn(REDACTED_IP, result.text)
        self.assertIn(REDACTED_SECRET, result.text)
        self.assertIn(REDACTED_PRIVATE_KEY, result.text)
        self.assertNotIn("abcdef123456", result.text)
        self.assertNotIn("ghp_abcdefghijklmnopqrstuvwxyz1234567890", result.text)

    def test_examples_and_known_placeholders_are_preserved(self):
        text = "Ubuntu 24.04.1.2 <ACCOUNT_ID> <INTERNAL_IP> <CLIENT_SECRET> user@example.com cockpit.example.com"
        result = sanitize_text(text)
        self.assertFalse(result.changed)
        self.assertEqual(result.text, text)

    def test_clean_text_is_unchanged(self):
        result = sanitize_text("Cloudflare Tunnel on localhost:9090")
        self.assertFalse(result.changed)
        self.assertEqual(result.text, "Cloudflare Tunnel on localhost:9090")

    def test_private_key_header_alone_is_redacted(self):
        result = sanitize_text("-----BEGIN PRIVATE KEY-----")
        self.assertEqual(result.text, REDACTED_PRIVATE_KEY)

    def test_prefixed_shell_style_labels_are_redacted(self):
        for text in [
            "GITHUB_TOKEN=dummy-value-1",
            "DB_PASSWORD=dummy-value-2",
            "export SLACK_API_TOKEN=dummy-value-3",
            "my_secret = dummy-value-4",
            "secret_key: dummy-value-5",
            "X-Auth-Token: dummy-value-6",
        ]:
            with self.subTest(text=text):
                result = sanitize_text(text)
                self.assertTrue(result.changed)
                self.assertIn(REDACTED_SECRET, result.text)
                self.assertNotIn("dummy-value", result.text)

    def test_label_like_prose_is_not_redacted(self):
        for text in [
            "see the password reset page",
            "PasswordAuthentication no",
            "the tokenizer splits words",
        ]:
            with self.subTest(text=text):
                self.assertFalse(sanitize_text(text).changed)

    def test_zero_padded_version_strings_are_not_treated_as_addresses(self):
        for text in ["Ubuntu 24.04.1.2", "22.04.3.1", "release 8.04.10.2"]:
            with self.subTest(text=text):
                self.assertEqual(sanitize_text(text).text, text)

    def test_documentation_addresses_are_redacted(self):
        self.assertEqual(sanitize_text("203.0.113.9").text, REDACTED_IP)
        self.assertEqual(sanitize_text("host 198.51.100.7 down").text, f"host {REDACTED_IP} down")

    def test_cookie_private_key_and_embedded_json_are_redacted(self):
        for text in [
            "Cookie: session=dummy-value-1",
            "Set-Cookie: sid=dummy-value-2; Path=/",
            "private_key: dummy-value-3",
            'payload={"password":"dummy-value-4","api_key":"dummy-value-5"}',
        ]:
            with self.subTest(text=text):
                result = sanitize_text(text)
                self.assertTrue(result.changed)
                self.assertNotIn("dummy-value", result.text)
                self.assertIn(REDACTED_SECRET, result.text)

    def test_json_cookie_and_private_key_object_keys_are_redacted(self):
        result = normalize_json(
            '{"headers":{"Cookie":"dummy-value-1","Set-Cookie":"dummy-value-2"},'
            '"private_key":"dummy-value-3","note":"keep"}'
        )
        payload = json.loads(result.text)
        self.assertEqual(payload["headers"]["Cookie"], REDACTED_SECRET)
        self.assertEqual(payload["headers"]["Set-Cookie"], REDACTED_SECRET)
        self.assertEqual(payload["private_key"], REDACTED_SECRET)
        self.assertEqual(payload["note"], "keep")

    def test_json_secret_keys_are_redacted(self):
        result = normalize_json('{"GITHUB_TOKEN":"dummy-value","note":"ok"}')
        self.assertTrue(result.changed)
        payload = json.loads(result.text)
        self.assertEqual(payload["GITHUB_TOKEN"], REDACTED_SECRET)
        self.assertEqual(payload["note"], "ok")
