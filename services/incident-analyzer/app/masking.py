"""Best-effort redaction of sensitive data before it leaves the cluster for the LLM.

`redact()` scrubs well-known shapes (cards, IBANs, emails, tokens, IPs) with
conservative regexes, replacing each with a typed placeholder so the model keeps
the structure without the secret. Patterns run most-specific-first so a JWT or
email is consumed before the generic IP / digit rules fire on its substrings.
It reduces exposure but does not guarantee zero leakage.
"""

from __future__ import annotations

import re


def _luhn_ok(digits: str) -> bool:
    """Luhn checksum, keeps the card rule from eating arbitrary long digit runs
    (order IDs, counters) that happen to be card-length."""
    total, alt = 0, False
    for ch in reversed(digits):
        d = ord(ch) - 48
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def _redact_card(m: re.Match) -> str:
    raw = m.group(0)
    digits = re.sub(r"\D", "", raw)
    if 13 <= len(digits) <= 19 and _luhn_ok(digits):
        return "[REDACTED_CARD]"
    return raw  # not a valid PAN, leave it (likely an ID / counter)


# (compiled pattern, replacement), order matters; most specific first.
# Replacement may be a string or a callable, matching re.sub's contract.
_RULES: list[tuple[re.Pattern, object]] = [
    # PEM private-key blocks, matched whole (across lines) so the body is redacted
    # as a unit. First, as the most specific shape.
    (
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        "[REDACTED_PRIVATE_KEY]",
    ),
    # JSON Web Tokens (header.payload.signature, base64url).
    (re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"), "[REDACTED_JWT]"),
    # AWS access key IDs.
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED_AWS_KEY]"),
    # Authorization: Bearer <token> / Basic <token>.
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 [REDACTED_TOKEN]"),
    # Email addresses.
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[REDACTED_EMAIL]"),
    # IBAN: 2-letter country + 2 check digits + up to 30 alnum.
    (re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"), "[REDACTED_IBAN]"),
    # Payment card numbers (13–19 digits, optionally grouped), Luhn-validated.
    # Ends on a bare digit so a trailing space/dash isn't swallowed into the match.
    (re.compile(r"\b(?:\d[ -]?){12,18}\d\b"), _redact_card),
    # secret-ish key=value / key: value pairs (key/value may be quoted, so JSON
    # "password":"x" is caught too). Three points:
    #   - Anchor the key with a non-word lookbehind + lazy prefix, not `\b`, so
    #     underscore-joined keys fire (db_password, aws_secret_access_key).
    #   - A bounded suffix (digits + a small qualifier list: _v2/_old/_base) catches
    #     password2 / client_secret_v2 without re-firing on authenticated=/authority:.
    #   - The value takes a quoted span to its closing quote before a bare token
    #     (so "p@ss w0rd" keeps its tail), with an open-quote-to-EOL fallback for
    #     truncated lines.
    (
        re.compile(
            r"(?i)[\"']?(?<![A-Za-z0-9_])"
            r"([A-Za-z0-9_]*?(?:password|passwd|pwd|private[_-]?key|secret[_-]?key|secret|"
            r"token|api[_-]?key|access[_-]?key|client[_-]?secret|authorization|auth)"
            r"\d*(?:[_-](?:v?\d+|old|new|prev|previous|prod|production|dev|staging|test|base|backup))*)"
            r"[\"']?\s*[=:]\s*"
            r"(?:\"[^\"]*\"|'[^']*'|[^\s,;&\"']+|\"[^\"\n]*|'[^'\n]*)"
        ),
        r"\1=[REDACTED]",
    ),
    # IPv4. The digit/dot boundaries stop it grabbing a 4-group fragment of a
    # longer dotted run (version strings, dotted phones); that run is left to a later rule.
    (re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])"), "[REDACTED_IP]"),
    # IPv6, full or `::`-compressed. The start boundary forbids matching inside an
    # identifier, so `std::vector` scope operators and `HH:MM:SS` timestamps are left alone.
    (
        re.compile(
            r"""(?<![\w.:])(?:
                  (?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}              # 1:2:3:4:5:6:7:8
                | (?:[0-9A-Fa-f]{1,4}:){1,7}:                           # 1::  …  1:2:3:4:5:6:7::
                | (?:[0-9A-Fa-f]{1,4}:){1,6}:[0-9A-Fa-f]{1,4}           # 1::8 … 1:2:3:4:5:6::8
                | (?:[0-9A-Fa-f]{1,4}:){1,5}(?::[0-9A-Fa-f]{1,4}){1,2}
                | (?:[0-9A-Fa-f]{1,4}:){1,4}(?::[0-9A-Fa-f]{1,4}){1,3}
                | (?:[0-9A-Fa-f]{1,4}:){1,3}(?::[0-9A-Fa-f]{1,4}){1,4}
                | (?:[0-9A-Fa-f]{1,4}:){1,2}(?::[0-9A-Fa-f]{1,4}){1,5}
                | [0-9A-Fa-f]{1,4}:(?::[0-9A-Fa-f]{1,4}){1,6}           # 1::3:4:5:6:7:8
                | :(?::[0-9A-Fa-f]{1,4}){1,7}                           # ::1 … ::2:3:…
                )(?![0-9A-Fa-f:.])""",
            re.VERBOSE,
        ),
        "[REDACTED_IPV6]",
    ),
    # Phone numbers: international (+CC) or an explicitly formatted national number.
    # Bare unformatted digit runs are left alone (indistinguishable from IDs/counters).
    (
        re.compile(
            r"(?<![\w+])(?:"
            r"\+\d(?:[ .\-]?\d){7,15}"                              # +33612345678, +1-555-123-4567
            r"|\(\d{2,4}\)[ .\-]?\d{2,4}(?:[ .\-]?\d{2,4}){1,3}"    # (555) 123-4567
            r"|0\d(?:[ .\-]\d{2,4}){3,5}"                           # 06 12 34 56 78, 06.12.34.56.78
            r")(?!\w)"
        ),
        "[REDACTED_PHONE]",
    ),
]


def redact(text: str) -> str:
    """Return `text` with known sensitive shapes replaced by typed placeholders."""
    if not text:
        return text
    for pattern, repl in _RULES:
        text = pattern.sub(repl, text)
    return text


def redact_candidate(candidate) -> None:
    """Scrub PII from a candidate's free-text fields in place (log samples, pod
    events). The candidate is returned verbatim in /analyze and surfaced to
    GitHub / Backstage, so it needs the same scrub the LLM context already gets.
    Structured slugs are left untouched. redact() is idempotent, so re-masking in
    _incident_context afterwards is a safe no-op."""
    for s in candidate.log_samples:
        s.samples = [redact(m) for m in s.samples]
    for p in candidate.pod_signals:
        p.recent_events = [redact(e) for e in p.recent_events]
