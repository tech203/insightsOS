"""Email template HTML-escaping regression tests.

The render_*_email() helpers in services/email_helper.py interpolate
user-controlled values (display name, recipient address, signed URLs)
into HTML via f-strings. Without escaping, a user could:

  - Sign up with name "<script>alert(1)</script>" → their own password
    reset email contains live JS (most webmail strips it, but the
    risk is real and varies by client).
  - Set name to '"><a href="https://attacker.com">Click</a><span x="'
    and invite a teammate → that teammate gets a phishing link in an
    email that ostensibly came from us.
  - Inject closing tags to break the email layout entirely.

These tests guarantee every render_*_email() helper escapes its inputs
before they hit the HTML body.
"""
import pytest

from services.email_helper import (
    render_password_reset_email,
    render_email_verification_email,
    render_team_invite_email,
)


# Common XSS / HTML-injection payloads. If any of these survives intact
# in the HTML output, the template is broken.
DANGEROUS_PAYLOADS = [
    '<script>alert(1)</script>',
    '"><script>alert(1)</script>',
    '</p><a href="https://attacker.com">Click</a><p>',
    '"><img src=x onerror=alert(1)>',
    "'><svg onload=alert(1)>",
]


def _assert_escaped(html: str, payload: str, field: str):
    """Helper: fail loudly if the dangerous payload appears verbatim."""
    assert payload not in html, (
        f"Email HTML contains UNESCAPED {field} payload {payload!r}.\n"
        f"This is an XSS / HTML-injection vulnerability — every "
        f"interpolated user value must be html.escape()d before "
        f"landing in the f-string."
    )


@pytest.mark.parametrize("payload", DANGEROUS_PAYLOADS)
def test_password_reset_email_escapes_user_name(payload):
    _, _, html = render_password_reset_email(
        user_name=payload, reset_url="https://app.example.com/reset/x",
    )
    _assert_escaped(html, payload, "user_name")


@pytest.mark.parametrize("payload", DANGEROUS_PAYLOADS)
def test_password_reset_email_escapes_reset_url(payload):
    _, _, html = render_password_reset_email(
        user_name="Marc", reset_url=f"https://app.example.com/reset/{payload}",
    )
    _assert_escaped(html, payload, "reset_url")


@pytest.mark.parametrize("payload", DANGEROUS_PAYLOADS)
def test_email_verification_escapes_user_name(payload):
    _, _, html = render_email_verification_email(
        user_name=payload, verify_url="https://app.example.com/verify/x",
    )
    _assert_escaped(html, payload, "user_name")


@pytest.mark.parametrize("payload", DANGEROUS_PAYLOADS)
def test_email_verification_escapes_verify_url(payload):
    _, _, html = render_email_verification_email(
        user_name="Marc", verify_url=f"https://app.example.com/verify/{payload}",
    )
    _assert_escaped(html, payload, "verify_url")


@pytest.mark.parametrize("payload", DANGEROUS_PAYLOADS)
def test_team_invite_escapes_owner_name(payload):
    _, _, html = render_team_invite_email(
        owner_name=payload,
        invitee_email="bob@example.com",
        invite_url="https://app.example.com/team/accept/x",
    )
    _assert_escaped(html, payload, "owner_name")


@pytest.mark.parametrize("payload", DANGEROUS_PAYLOADS)
def test_team_invite_escapes_invitee_email(payload):
    _, _, html = render_team_invite_email(
        owner_name="Acme",
        invitee_email=payload,
        invite_url="https://app.example.com/team/accept/x",
    )
    _assert_escaped(html, payload, "invitee_email")


@pytest.mark.parametrize("payload", DANGEROUS_PAYLOADS)
def test_team_invite_escapes_invite_url(payload):
    _, _, html = render_team_invite_email(
        owner_name="Acme",
        invitee_email="bob@example.com",
        invite_url=f"https://app.example.com/team/accept/{payload}",
    )
    _assert_escaped(html, payload, "invite_url")


def test_team_invite_subject_strips_header_injection():
    """Subject is a header value — newlines would let an attacker
    inject Bcc:, Cc:, or arbitrary headers via display name."""
    subject, _, _ = render_team_invite_email(
        owner_name="Attacker\r\nBcc: secrets@evil.com",
        invitee_email="bob@example.com",
        invite_url="https://app.example.com/team/accept/x",
    )
    assert "\n" not in subject and "\r" not in subject


def test_safe_inputs_render_unmodified():
    """Sanity check: ordinary names / URLs survive escaping unchanged."""
    _, text, html = render_password_reset_email(
        user_name="Marc O'Brien", reset_url="https://app.example.com/reset/abc",
    )
    # Plain text body is not HTML-escaped (it's not HTML).
    assert "Marc O'Brien" in text
    # HTML body escapes the apostrophe to &#x27;.
    assert "Marc O&#x27;Brien" in html or "Marc O'Brien" in html
    assert "https://app.example.com/reset/abc" in html
