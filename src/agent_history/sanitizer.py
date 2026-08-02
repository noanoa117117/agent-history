"""Small, conservative secret and personal-data sanitizer."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Tuple


REDACTED_SECRET = "<REDACTED_SECRET>"
REDACTED_EMAIL = "<REDACTED_EMAIL>"
REDACTED_IP = "<REDACTED_IP>"
REDACTED_PRIVATE_KEY = "<REDACTED_PRIVATE_KEY>"


@dataclass(frozen=True)
class SanitizedText:
    text: str
    changed: bool


_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----.*?-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----",
    re.IGNORECASE | re.DOTALL,
)
_PRIVATE_KEY_HEADER_RE = re.compile(
    r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(
    r"(?i)(\bBearer\s+)(?!<[^>\s]+>)[A-Za-z0-9._~+/=-]+"
)
_AWS_KEY_RE = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")
# The label may carry a prefix joined by an underscore, dot, or dash, so that
# shell-style names such as GITHUB_TOKEN or DB_PASSWORD are recognized. A plain
# \b would not match there, because an underscore is a word character.
_LABELED_SECRET_RE = re.compile(
    r"(?ix)"
    r"(?P<label>(?<![A-Za-z0-9])"
    r"(?:api[_ -]?key|secret[_ -]?key|access[_ -]?token|refresh[_ -]?token"
    r"|client[_ -]?secret|token|secret|password|passwd)\b)"
    r"(?P<separator>\s*[:=]\s*)"
    r"(?P<quote>['\"]?)"
    r"(?P<value>(?!<[^>\n]+>)[^\s,;\"'&]+)"
)
_URL_SECRET_RE = re.compile(
    r"(?i)(?P<prefix>[?&](?:token|api[_-]?key|key|secret|access[_-]?token|password)=)"
    r"(?P<value>(?!<[^>\n]+>)[^&#\s]+)"
)
_EMAIL_RE = re.compile(
    r"(?<![\w.+-])(?P<email>[A-Za-z0-9][A-Za-z0-9._%+-]*@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})(?![\w.-])"
)
_IP_RE = re.compile(
    r"(?<![\w.])(?P<ip>(?:\d{1,3}\.){3}\d{1,3})(?![\w.])"
)
# Matches a JSON object key that is, or ends with, a secret-looking name:
# "token", "api_key", "GITHUB_TOKEN", "db.password".
_SECRET_KEY_RE = re.compile(
    r"(?:^|[_.-])"
    r"(?:api[_-]?key|secret[_-]?key|access[_-]?token|refresh[_-]?token"
    r"|client[_-]?secret|token|secret|password|passwd)$",
    re.IGNORECASE,
)


def _is_placeholder(value: str) -> bool:
    return value.startswith("<") and value.endswith(">")


def _replace_labeled_secret(match: re.Match[str]) -> str:
    value = match.group("value")
    if _is_placeholder(value):
        return match.group(0)
    return f"{match.group('label')}{match.group('separator')}{match.group('quote')}{REDACTED_SECRET}{match.group('quote')}"


def _replace_url_secret(match: re.Match[str]) -> str:
    return f"{match.group('prefix')}{REDACTED_SECRET}"


def _replace_email(match: re.Match[str]) -> str:
    email = match.group("email")
    local, domain = email.rsplit("@", 1)
    lowered_domain = domain.lower()
    if lowered_domain == "example.com" or lowered_domain.endswith(".example.com"):
        return email
    # Do not alter an email-looking token inside an angle-bracket placeholder.
    before = match.string[max(0, match.start() - 1) : match.start()]
    after = match.string[match.end() : match.end() + 1]
    if before == "<" and after == ">":
        return email
    return REDACTED_EMAIL


def _valid_ipv4(value: str) -> bool:
    octets = value.split(".")
    if len(octets) != 4 or not all(0 <= int(part) <= 255 for part in octets):
        return False
    # A zero-padded group means a dotted version string such as "24.04.1.2",
    # not an address: real IPv4 notation does not pad octets.
    return not any(len(part) > 1 and part.startswith("0") for part in octets)


def _replace_ip(match: re.Match[str]) -> str:
    value = match.group("ip")
    if not _valid_ipv4(value):
        return value
    before = match.string[max(0, match.start() - 1) : match.start()]
    after = match.string[match.end() : match.end() + 1]
    if before == "<" and after == ">":
        return value
    return REDACTED_IP


def sanitize_text(text: str) -> SanitizedText:
    """Redact high-confidence secrets, emails, and IPv4 addresses.

    Known documentation placeholders, example.com addresses, and zero-padded
    dotted numbers such as "24.04.1.2" are deliberately retained so examples
    remain useful. Other dotted quads are redacted even when they are really
    version numbers: over-redaction is the safe direction here.
    """

    sanitized = _PRIVATE_KEY_RE.sub(REDACTED_PRIVATE_KEY, text)
    sanitized = _PRIVATE_KEY_HEADER_RE.sub(REDACTED_PRIVATE_KEY, sanitized)
    sanitized = _BEARER_RE.sub(lambda match: f"{match.group(1)}{REDACTED_SECRET}", sanitized)
    sanitized = _AWS_KEY_RE.sub(REDACTED_SECRET, sanitized)
    sanitized = _LABELED_SECRET_RE.sub(_replace_labeled_secret, sanitized)
    sanitized = _URL_SECRET_RE.sub(_replace_url_secret, sanitized)
    sanitized = _EMAIL_RE.sub(_replace_email, sanitized)
    sanitized = _IP_RE.sub(_replace_ip, sanitized)
    return SanitizedText(sanitized, sanitized != text)


def parse_json(text: str, *, label: str = "JSON") -> Any:
    """Validate and parse JSON without treating it as a JSON string value."""

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is invalid JSON: {exc.msg}") from exc


def _sanitize_json_value(value: Any) -> Tuple[Any, bool]:
    if isinstance(value, str):
        result = sanitize_text(value)
        return result.text, result.changed
    if isinstance(value, list):
        sanitized_items = []
        changed = False
        for item in value:
            sanitized_item, item_changed = _sanitize_json_value(item)
            sanitized_items.append(sanitized_item)
            changed = changed or item_changed
        return sanitized_items, changed
    if isinstance(value, dict):
        sanitized_values = {}
        changed = False
        for key, item in value.items():
            if isinstance(key, str) and _SECRET_KEY_RE.search(key):
                if isinstance(item, str) and not _is_placeholder(item):
                    sanitized_values[key] = REDACTED_SECRET
                    changed = True
                    continue
            sanitized_item, item_changed = _sanitize_json_value(item)
            sanitized_values[key] = sanitized_item
            changed = changed or item_changed
        return sanitized_values, changed
    return value, False


def normalize_json(text: str, *, label: str = "JSON", sanitize: bool = True) -> SanitizedText:
    """Validate JSON and serialize it once, optionally sanitizing string values."""

    parsed = parse_json(text, label=label)
    if sanitize:
        parsed, changed = _sanitize_json_value(parsed)
    else:
        changed = False
    normalized = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
    return SanitizedText(normalized, changed)
