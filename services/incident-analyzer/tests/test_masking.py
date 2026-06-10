"""Tests for app.masking.redact, the scrub applied to log samples and pod events
before they cross the trust boundary. A false negative here is a real PII leak."""

from __future__ import annotations

import pytest

from app.masking import _luhn_ok, redact, redact_candidate
from app.models import IncidentCandidate, LogSample, PodSignal


def test_empty_and_none_safe():
    assert redact("") == ""
    assert redact("nothing sensitive here") == "nothing sensitive here"


def test_email_redacted():
    out = redact("payment failed for alice.123@example.com: declined")
    assert "alice.123@example.com" not in out
    assert "[REDACTED_EMAIL]" in out


def test_valid_card_redacted_invalid_left():
    # 4111111111111111 is a well-known Luhn-valid Visa test PAN.
    assert "[REDACTED_CARD]" in redact("card 4111111111111111 declined")
    # A 16-digit run that fails Luhn must be left alone (order id / counter).
    assert "[REDACTED_CARD]" not in redact("order 1111111111111111")


def test_card_grouped_digits():
    out = redact("PAN 4111 1111 1111 1111 on file")
    assert "[REDACTED_CARD]" in out
    assert "4111" not in out


def test_iban_redacted():
    out = redact("beneficiary IBAN FR7630006000011234567890189 rejected")
    assert "FR7630006000011234567890189" not in out
    assert "[REDACTED_IBAN]" in out


def test_jwt_redacted_before_other_rules():
    token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N"
    out = redact(f"Authorization: Bearer {token}")
    assert token not in out
    assert "[REDACTED_JWT]" in out


def test_bearer_token_redacted():
    out = redact("Authorization: Bearer abcDEF123456ghijkl")
    assert "abcDEF123456ghijkl" not in out
    assert "[REDACTED_TOKEN]" in out


def test_aws_access_key_redacted():
    out = redact("using key AKIAIOSFODNN7EXAMPLE for s3")
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert "[REDACTED_AWS_KEY]" in out


def test_secret_key_value_pair_redacted():
    for line in ("password=hunter2", "api_key: sk-abc123", "client_secret=topsecret"):
        out = redact(line)
        assert "[REDACTED]" in out, line
        assert "hunter2" not in out and "sk-abc123" not in out and "topsecret" not in out


def test_json_quoted_secret_redacted():
    # Quoted-JSON form ("key":"value") must be caught, not just key=value.
    for line, secret in (
        ('{"password":"hunter2"}', "hunter2"),
        ('{"token": "sk-abc123"}', "sk-abc123"),
        ("'api_key' = 'topsecret'", "topsecret"),
        ('"client_secret":"xyz789"', "xyz789"),
    ):
        out = redact(line)
        assert "[REDACTED]" in out, line
        assert secret not in out, f"{secret!r} leaked: {out}"


def test_underscore_prefixed_secret_keys_redacted():
    # Regression: a leading `\b` on the keyword refused to fire when the keyword
    # sits behind an underscore (`_` is a word char → no boundary), leaving the
    # most common key shapes leaking. These must all be masked now.
    cases = [
        ("db_password=hunter2", "hunter2"),
        ("aws_secret_access_key=AKIAabc/secretvalue123", "secretvalue123"),
        ("X_API_KEY=supersecret", "supersecret"),
        ("session_token=abcdef123456", "abcdef123456"),
        ("SECRET_KEY=django-insecure-abc123", "django-insecure-abc123"),
    ]
    for line, secret in cases:
        out = redact(line)
        assert "[REDACTED]" in out, line
        assert secret not in out, f"{secret!r} leaked: {out}"


def test_auth_substring_keys_not_false_positive():
    # Anchoring the keyword right before the separator keeps `auth` from firing
    # inside ordinary words; these carry no secret and must be left intact.
    for line in ("author=bob", "authenticated=true", "authority: high"):
        assert redact(line) == line


def test_quoted_secret_value_with_spaces_not_truncated():
    # Regression: the bare-token value class stopped at the first space, leaking
    # everything after it (password = "p@ss w0rd" → left `w0rd"`). A quoted value
    # must be consumed up to its closing quote.
    for line, secret in (
        ('password = "p@ss w0rd"', 'w0rd'),
        ("secret = 'p ss wd!'", "p ss wd!"),
        ('token: "a b c d"', "b c d"),
    ):
        out = redact(line)
        assert "[REDACTED]" in out, line
        assert secret not in out, f"{secret!r} leaked: {out}"


def test_private_key_pem_block_redacted():
    # A full PEM block (BEGIN…END) must be redacted as a unit so the base64 body
    # never leaks. Covers RSA / EC / OPENSSH / generic labels.
    for label in ("RSA PRIVATE KEY", "EC PRIVATE KEY", "OPENSSH PRIVATE KEY", "PRIVATE KEY"):
        pem = (
            f"-----BEGIN {label}-----\n"
            "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQDxleak\n"
            "c2VjcmV0LWtleS1tYXRlcmlhbC1oZXJl\n"
            f"-----END {label}-----"
        )
        out = redact(f"loaded key:\n{pem}\nstartup done")
        assert "[REDACTED_PRIVATE_KEY]" in out, label
        assert "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEA" not in out, label


def test_private_key_keyword_redacted():
    # Single-line private-key assignments (not PEM-wrapped) go through the kv rule.
    for line, secret in (("private_key=abc123def", "abc123def"), ("PRIVATE-KEY: zzz999", "zzz999")):
        out = redact(line)
        assert "[REDACTED]" in out, line
        assert secret not in out, f"{secret!r} leaked: {out}"


def test_keyword_suffixed_secret_keys_redacted():
    # Regression: keys with a trailing suffix (password2, client_secret_v2) were
    # missed; a bounded digit/qualifier suffix now catches them.
    cases = [
        ("password2=hunter2", "hunter2"),
        ("password1: firstpass", "firstpass"),
        ("client_secret_v2=topsecretv2", "topsecretv2"),
        ("api_key_old=staleapikey", "staleapikey"),
        ("secret_key_base=longbasevalue", "longbasevalue"),
    ]
    for line, secret in cases:
        out = redact(line)
        assert "[REDACTED]" in out, line
        assert secret not in out, f"{secret!r} leaked: {out}"


def test_config_keys_with_keyword_substring_not_redacted():
    # The bounded suffix must not over-fire on ordinary config knobs whose name
    # merely contains a keyword followed by an unlisted word (bucket/expiry).
    for line in ("token-bucket-size=100", "token_expiry_seconds=3600", "authenticated=true"):
        assert redact(line) == line, line


def test_unterminated_quoted_secret_redacted():
    # Regression: an unterminated quote slipped past both value branches; the
    # open-quote-to-EOL fallback now consumes it.
    for line, secret in (
        ('password="unterminated secret value', "unterminated secret value"),
        ("token='no closing quote here", "no closing quote here"),
    ):
        out = redact(line)
        assert "[REDACTED]" in out, line
        assert secret not in out, f"{secret!r} leaked: {out}"


def test_secret_keyword_in_prose_not_redacted():
    # No '='/':' after the keyword → not a key=value pair, leave prose alone.
    assert redact("the password is wrong") == "the password is wrong"


def test_ipv4_redacted():
    out = redact("connection from 192.168.1.42 refused")
    assert "192.168.1.42" not in out
    assert "[REDACTED_IP]" in out


def test_ipv4_does_not_grab_fragment_of_longer_dotted_run():
    # A 5-part version is not a 4-octet IPv4, don't redact a fragment of it.
    assert redact("five-part 1.2.3.4.5 build") == "five-part 1.2.3.4.5 build"


@pytest.mark.parametrize(
    "addr",
    [
        "2001:db8::1",
        "::1",
        "fe80::1ff:fe23:4567:890a",
        "2001:0db8:85a3:0000:0000:8a2e:0370:7334",
    ],
)
def test_ipv6_redacted(addr):
    out = redact(f"peer {addr} connected")
    assert addr not in out
    assert "[REDACTED_IPV6]" in out


@pytest.mark.parametrize(
    "line",
    [
        "std::vector<int> created",      # C++ scope, not an address
        "ActiveRecord::Base loaded",     # Ruby scope
        "namespace Foo::Bar::Baz used",
        "timestamp 12:34:56 logged",     # time, no ::
        "MAC aa:bb:cc:dd:ee:ff seen",    # MAC, no ::
    ],
)
def test_ipv6_false_positives_left_alone(line):
    assert redact(line) == line


@pytest.mark.parametrize(
    "phone",
    [
        "06 12 34 56 78",   # national, spaces
        "06.12.34.56.78",   # national, dots
        "06-12-34-56-78",   # national, dashes
        "(555) 123-4567",   # parenthesized area code
        "+33612345678",     # E.164 (no separators)
        "+1-555-123-4567",  # E.164 with separators
    ],
)
def test_phone_without_plus_redacted(phone):
    out = redact(f"reach me at {phone} anytime")
    assert "[REDACTED_PHONE]" in out
    for digit_run in phone.replace("+", "").replace("(", "").replace(")", "").split():
        assert digit_run not in out


@pytest.mark.parametrize(
    "line",
    [
        "epoch 1718409600 now",          # bare 10-digit run (timestamp)
        "order 1234567890 placed",       # bare order id
        "request count 999999999 high",  # counter
        "status 200 ok",                 # short number
    ],
)
def test_bare_digit_runs_not_treated_as_phone(line):
    assert redact(line) == line


def test_phone_redacted():
    out = redact("customer phone +33612345678 on record")
    assert "+33612345678" not in out
    assert "[REDACTED_PHONE]" in out


def test_realistic_leaky_line_fully_scrubbed():
    line = (
        "payment authorization failed for cardholder bob.456@example.com "
        "using card 4111111111111111 from 10.0.0.5: decline code 51"
    )
    out = redact(line)
    for secret in ("bob.456@example.com", "4111111111111111", "10.0.0.5"):
        assert secret not in out, f"{secret!r} leaked through masking: {out}"


@pytest.mark.parametrize(
    "digits,ok",
    [
        ("4111111111111111", True),
        ("79927398713", True),
        ("1111111111111111", False),
        ("0000000000000000", True),
    ],
)
def test_luhn_ok(digits, ok):
    assert _luhn_ok(digits) is ok


# --- redact_candidate: scrub the candidate before it leaves the service ---

def test_redact_candidate_masks_samples_and_events():
    c = IncidentCandidate(
        namespace="payments",
        signal_class="log-volume",
        dominant_error_type="payout-failed",
        log_samples=[
            LogSample(
                error_type="payout-failed",
                count=3,
                component="payout-worker",
                samples=["payout to IBAN FR7630006000011234567890189 failed for bob.456@example.com"],
            )
        ],
        pod_signals=[
            PodSignal(pod="payout-worker-0", recent_events=["OOMKilled while paging alice.123@example.com"])
        ],
    )
    redact_candidate(c)

    sample = c.log_samples[0].samples[0]
    assert "FR7630006000011234567890189" not in sample
    assert "bob.456@example.com" not in sample
    assert "[REDACTED_IBAN]" in sample and "[REDACTED_EMAIL]" in sample

    event = c.pod_signals[0].recent_events[0]
    assert "alice.123@example.com" not in event
    assert "[REDACTED_EMAIL]" in event
    # Structured identifiers are left intact (parity with the LLM context).
    assert c.log_samples[0].error_type == "payout-failed"
    assert c.log_samples[0].component == "payout-worker"


def test_redact_candidate_is_idempotent():
    # Re-masking an already-scrubbed candidate must be a no-op (placeholders have no PII shape).
    c = IncidentCandidate(
        namespace="payments",
        signal_class="log-volume",
        dominant_error_type="x",
        log_samples=[LogSample(error_type="x", count=1, samples=["card 4111111111111111 declined"])],
    )
    redact_candidate(c)
    once = c.log_samples[0].samples[0]
    redact_candidate(c)
    assert c.log_samples[0].samples[0] == once
