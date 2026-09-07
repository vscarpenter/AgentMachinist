"""Adversarial diagnostic examples use synthetic credentials only."""

import pytest

from machinist.diagnostics import sanitize_diagnostic


@pytest.mark.parametrize(
    "text,secret,context",
    [
        (
            "fatal: https://synthetic-user:sentinel-url-secret@example.test/team/repo",
            "sentinel-url-secret",
            "example.test/team/repo",
        ),
        (
            "https://synthetic-user:p%40ss%3Aencoded-sentinel@example.test:8443/repo",
            "encoded-sentinel",
            "example.test:8443/repo",
        ),
        (
            "Authorization: Bearer sentinel-bearer-secret\nHTTP 403 Forbidden",
            "sentinel-bearer-secret",
            "HTTP 403 Forbidden",
        ),
        (
            "proxy-authorization: Basic sentinel-basic-secret\nHTTP 407",
            "sentinel-basic-secret",
            "HTTP 407",
        ),
        (
            '{"Authorization": "Bearer sentinel-json-secret", "status": 401}',
            "sentinel-json-secret",
            '"status": 401',
        ),
        (
            "GH_TOKEN=sentinel-env-secret command failed",
            "sentinel-env-secret",
            "command failed",
        ),
        (
            "password='sentinel quoted secret' authentication failed",
            "sentinel quoted secret",
            "authentication failed",
        ),
        (
            'client_secret="sentinel \\"quoted\\" secret"; retry later',
            "sentinel",
            "retry later",
        ),
        (
            "https://example.test/api?access_token=sentinel-query-secret&issue=42",
            "sentinel-query-secret",
            "&issue=42",
        ),
        (
            '{"apiKey": "sentinel-api-secret", "status": "denied"}',
            "sentinel-api-secret",
            '"status": "denied"',
        ),
        (
            "AWS_SECRET_ACCESS_KEY: sentinel-access-secret\npermission denied",
            "sentinel-access-secret",
            "permission denied",
        ),
        (
            "GITLAB_TOKEN=sentinel-first; password=sentinel-second\nHTTP 401",
            "sentinel-",
            "HTTP 401",
        ),
        (
            r"password=sentinel\ escaped\ value; retry later",
            "escaped",
            "retry later",
        ),
        (
            "TOKEN=[REDACTED]sentinel-tail\nHTTP 401",
            "sentinel-tail",
            "HTTP 401",
        ),
    ],
)
def test_recognized_credentials_are_redacted_without_losing_context(
    text, secret, context
):
    result = sanitize_diagnostic(text)

    assert secret not in result
    assert "[REDACTED]" in result
    assert context in result


def test_unterminated_quoted_secret_is_still_redacted():
    result = sanitize_diagnostic('fatal: TOKEN="sentinel incomplete value\nsecond line')

    assert result.startswith("fatal: TOKEN=")
    assert "sentinel" not in result
    assert "second line" not in result


def test_redaction_happens_before_truncation():
    text = 'token="sentinel-' + "x" * 5000 + '"\nHTTP 403 Forbidden'

    result = sanitize_diagnostic(text, limit=80)

    assert "sentinel" not in result
    assert "HTTP 403 Forbidden" in result
    assert "truncated" not in result


def test_output_is_bounded_including_the_truncation_notice():
    result = sanitize_diagnostic("fatal: unavailable\n" + "detail " * 2000, limit=120)

    assert len(result) == 120
    assert result.startswith("fatal: unavailable\n")
    assert "truncated" in result


@pytest.mark.parametrize("limit", [0, 1, 5, 12])
def test_small_limits_remain_bounded(limit):
    assert len(sanitize_diagnostic("x" * 200, limit=limit)) <= limit


def test_negative_limit_is_rejected():
    with pytest.raises(ValueError, match="limit"):
        sanitize_diagnostic("error", limit=-1)


def test_terminal_controls_are_removed_before_credentials_are_recognized():
    text = (
        "\x1b[31mHTTP 403\x1b[0m\n"
        "AUTHORI\x1b[1mZATION: Bearer sentinel-colored-secret\x1b[0m\n"
        "\x1b]8;;https://sentinel-link-secret@example.test\x1b\\"
        "help\x1b]8;;\x1b\\\n"
        "\x1b]52;c;sentinel-clipboard-secret\x07"
        "\x1bPsentinel-device-command\x1b\\"
        "\x9b31mretry\x9b0m\x00\x08\x7f\u202e now\r\n\tcheck access"
    )

    result = sanitize_diagnostic(text)

    assert "sentinel" not in result
    assert "HTTP 403\n" in result
    assert "help\nretry now\n\tcheck access" in result
    assert not any(control in result for control in ("\x1b", "\x9b", "\x00", "\u202e"))


def test_plain_multiline_errors_and_public_values_remain_useful():
    text = (
        "fatal: couldn't find remote ref main\n"
        "\tCheck origin: https://example.test/team/repo\n"
        "exit_code=128 branch=main tokenizer=available public_key=abc"
    )

    assert sanitize_diagnostic(text) == text


def test_exceptions_and_bytes_are_rendered_without_python_escape_artifacts():
    assert sanitize_diagnostic(ValueError("invalid branch")) == "invalid branch"
    assert sanitize_diagnostic(b"\x1b[31mHTTP 403\x1b[0m\nretry") == "HTTP 403\nretry"


def test_repeated_sanitizing_does_not_remove_recovery_context():
    result = sanitize_diagnostic("GH_TOKEN=sentinel-secret\nHTTP 403; check access")

    assert sanitize_diagnostic(result) == result
