"""
Transactional email — Resend HTTP API or SMTP relay.

Two backends, picked by env. Resend takes priority when
RESEND_API_KEY is set; otherwise we fall back to stdlib smtplib +
SMTP_* vars (SES / SendGrid / Postmark / Mailgun / Gmail App Password).

Without either configured, send_email() returns False and the caller
falls back to whatever in-app surface they had before (e.g. flashing
the invite URL for the owner to copy manually).
"""

from __future__ import annotations

import html as _html
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Optional

logger = logging.getLogger(__name__)


def _resend_configured() -> bool:
    key = (os.getenv("RESEND_API_KEY") or "").strip()
    sender = (os.getenv("RESEND_FROM") or "").strip()
    return bool(key) and bool(sender) and not key.startswith("your_")


def _smtp_configured() -> bool:
    host = (os.getenv("SMTP_HOST") or "").strip()
    sender = (os.getenv("SMTP_FROM") or "").strip()
    return bool(host) and bool(sender) and not host.startswith("your_")


def is_email_configured() -> bool:
    """True when either Resend or SMTP can deliver mail."""
    return _resend_configured() or _smtp_configured()


def _send_via_resend(
    *,
    to: str,
    subject: str,
    body_text: str,
    body_html: Optional[str],
    reply_to: Optional[str],
    list_unsubscribe_url: Optional[str] = None,
) -> bool:
    import requests

    payload = {
        "from": os.getenv("RESEND_FROM"),
        "to": [to],
        "subject": subject,
        "text": body_text,
    }
    if body_html:
        payload["html"] = body_html
    if reply_to:
        payload["reply_to"] = reply_to
    if list_unsubscribe_url:
        # One-Click List-Unsubscribe (RFC 8058 + Gmail/Yahoo 2024
        # bulk-sender requirements). Both headers must be present
        # for Gmail to render its native "Unsubscribe" button;
        # without it, marketing mail gets flagged as spam.
        payload["headers"] = {
            "List-Unsubscribe": f"<{list_unsubscribe_url}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        }

    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {os.getenv('RESEND_API_KEY')}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=20,
        )
        if 200 <= resp.status_code < 300:
            return True
        logger.warning(
            "Resend send failed: %s %s", resp.status_code, resp.text[:200]
        )
        return False
    except Exception as exc:
        logger.warning("Resend send raised: %s", exc)
        return False


def _send_via_smtp(
    *,
    to: str,
    subject: str,
    body_text: str,
    body_html: Optional[str],
    reply_to: Optional[str],
    list_unsubscribe_url: Optional[str] = None,
) -> bool:
    host = os.getenv("SMTP_HOST")
    port = int(os.getenv("SMTP_PORT") or "587")
    username = os.getenv("SMTP_USER") or ""
    password = os.getenv("SMTP_PASS") or ""
    sender = os.getenv("SMTP_FROM") or username
    use_tls = (os.getenv("SMTP_TLS") or "true").lower() in ("true", "1", "yes")
    use_ssl = (os.getenv("SMTP_SSL") or "false").lower() in ("true", "1", "yes")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    if reply_to:
        msg["Reply-To"] = reply_to
    if list_unsubscribe_url:
        # Same RFC 8058 headers as the Resend path — see _send_via_resend.
        msg["List-Unsubscribe"] = f"<{list_unsubscribe_url}>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg.set_content(body_text)
    if body_html:
        msg.add_alternative(body_html, subtype="html")

    try:
        if use_ssl:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, timeout=20, context=ctx) as server:
                if username and password:
                    server.login(username, password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=20) as server:
                server.ehlo()
                if use_tls:
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
                if username and password:
                    server.login(username, password)
                server.send_message(msg)
        return True
    except Exception as exc:
        logger.warning("SMTP send failed: %s", exc)
        return False


def send_email(
    *,
    to: str,
    subject: str,
    body_text: str,
    body_html: Optional[str] = None,
    reply_to: Optional[str] = None,
    list_unsubscribe_url: Optional[str] = None,
) -> bool:
    """Send a transactional email. Returns True on success, False
    when no backend is configured or the send raised. Never raises —
    callers fall back to their in-app surface on False.

    `list_unsubscribe_url` — when provided, adds RFC 8058 one-click
    unsubscribe headers (`List-Unsubscribe` + `List-Unsubscribe-Post`).
    Required by Gmail and Yahoo bulk-sender rules (Feb 2024) for any
    promotional/marketing mail. Transactional emails (password reset,
    verification, invites) should NOT pass this — they're service-of-
    account mail and the headers would mislead users into thinking
    they can unsubscribe from account-essential notifications.
    """
    if not to or "@" not in to:
        return False

    if _resend_configured():
        return _send_via_resend(
            to=to, subject=subject, body_text=body_text,
            body_html=body_html, reply_to=reply_to,
            list_unsubscribe_url=list_unsubscribe_url,
        )
    if _smtp_configured():
        return _send_via_smtp(
            to=to, subject=subject, body_text=body_text,
            body_html=body_html, reply_to=reply_to,
            list_unsubscribe_url=list_unsubscribe_url,
        )
    return False


def render_password_reset_email(*, user_name: str, reset_url: str) -> tuple[str, str, str]:
    """Subject + plain-text + HTML body for a password-reset request."""
    subject = "Reset your DarInsights password"
    # Plain-text body uses raw values; only the HTML body needs escaping.
    text = (
        f"Hi {user_name or 'there'},\n\n"
        "We received a request to reset the password on your DarInsights account.\n"
        "Click the link below to set a new password. The link expires in 60 minutes.\n\n"
        f"{reset_url}\n\n"
        "If you didn't request a password reset, you can safely ignore this email — "
        "your password stays unchanged."
    )
    # HTML escape every user-controlled value before f-string interpolation.
    # `quote=True` (the default) escapes both " and ', which we need because
    # values land inside attribute contexts like href="…" as well as text.
    safe_name = _html.escape(user_name or "there")
    safe_url = _html.escape(reset_url)
    html = f"""\
<!DOCTYPE html>
<html><body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif; color: #191929; max-width: 560px; margin: 0 auto; padding: 32px 24px;">
  <h1 style="font-size: 22px; margin: 0 0 16px;">Reset your password</h1>
  <p style="line-height: 1.55; color: #444; margin: 0 0 12px;">Hi {safe_name},</p>
  <p style="line-height: 1.55; color: #444; margin: 0 0 24px;">
    We received a request to reset the password on your DarInsights account.
    Click below to set a new password. <strong>The link expires in 60 minutes.</strong>
  </p>
  <p style="margin: 0 0 24px;">
    <a href="{safe_url}" style="display: inline-block; padding: 12px 22px; background: #3EDFCB; color: #191929; text-decoration: none; border-radius: 8px; font-weight: 600;">Reset password →</a>
  </p>
  <p style="line-height: 1.55; color: #888; font-size: 13px; margin: 0 0 6px;">Or copy this link:</p>
  <p style="line-height: 1.45; color: #555; font-size: 12px; word-break: break-all; background: #fafafe; padding: 10px 12px; border-radius: 6px; margin: 0 0 24px;">{safe_url}</p>
  <p style="line-height: 1.55; color: #888; font-size: 12px; margin: 0;">If you didn't request a password reset, you can safely ignore this email — your password stays unchanged.</p>
</body></html>"""
    return subject, text, html


def render_email_verification_email(
    *,
    user_name: str,
    verify_url: str,
) -> tuple[str, str, str]:
    """Subject + plain-text + HTML body for an email-verification link.

    Mirrors render_password_reset_email styling so the visual identity
    stays consistent across transactional mail. Expiration matches
    the 24h token TTL set in the issue helper."""
    subject = "Verify your email — DarInsights"
    text = (
        f"Hi {user_name or 'there'},\n\n"
        "Welcome to DarInsights! Click the link below to confirm your email "
        "address. The link expires in 24 hours.\n\n"
        f"{verify_url}\n\n"
        "Verifying isn't required to use the free tier — but it's needed "
        "before you can buy credits or upgrade your plan.\n\n"
        "If you didn't sign up for DarInsights, you can safely ignore this email."
    )
    # HTML-escape every user-controlled value before f-string interpolation.
    safe_name = _html.escape(user_name or "there")
    safe_url = _html.escape(verify_url)
    html = f"""\
<!DOCTYPE html>
<html><body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif; color: #191929; max-width: 560px; margin: 0 auto; padding: 32px 24px;">
  <h1 style="font-size: 22px; margin: 0 0 16px;">Confirm your email</h1>
  <p style="line-height: 1.55; color: #444; margin: 0 0 12px;">Hi {safe_name},</p>
  <p style="line-height: 1.55; color: #444; margin: 0 0 24px;">
    Welcome to DarInsights! Click below to confirm your email address.
    <strong>The link expires in 24 hours.</strong>
  </p>
  <p style="margin: 0 0 24px;">
    <a href="{safe_url}" style="display: inline-block; padding: 12px 22px; background: #3EDFCB; color: #191929; text-decoration: none; border-radius: 8px; font-weight: 600;">Verify email →</a>
  </p>
  <p style="line-height: 1.55; color: #888; font-size: 13px; margin: 0 0 6px;">Or copy this link:</p>
  <p style="line-height: 1.45; color: #555; font-size: 12px; word-break: break-all; background: #fafafe; padding: 10px 12px; border-radius: 6px; margin: 0 0 24px;">{safe_url}</p>
  <p style="line-height: 1.55; color: #888; font-size: 12px; margin: 0 0 6px;">
    Verifying isn't required to use the free tier — but it's needed before you can buy credits or upgrade your plan.
  </p>
  <p style="line-height: 1.55; color: #888; font-size: 12px; margin: 0;">If you didn't sign up for DarInsights, you can safely ignore this email.</p>
</body></html>"""
    return subject, text, html


def render_team_invite_email(
    *,
    owner_name: str,
    invitee_email: str,
    invite_url: str,
) -> tuple[str, str, str]:
    """Build subject + plain-text + HTML body for a team invite."""
    # Subject line is a header value, not HTML — but we still strip
    # newlines defensively so an attacker can't inject Bcc:/etc. headers
    # via the owner's display name (header injection).
    safe_subject_owner = (owner_name or "Someone").replace("\n", " ").replace("\r", " ")
    subject = f"{safe_subject_owner} invited you to join their team on DarInsights"
    text = (
        f"{owner_name} has invited you to join their DarInsights team as {invitee_email}.\n\n"
        "Team members share the same workspaces, plan, and credit wallet — you'll see "
        "everything they're working on once you accept.\n\n"
        f"Accept the invite: {invite_url}\n\n"
        "If you didn't expect this invite, you can safely ignore this email."
    )
    # HTML-escape every user-controlled value before f-string interpolation.
    # owner_name is from the inviting user (not necessarily the recipient).
    # invitee_email is also user-supplied. Both must be escaped to prevent
    # the inviter sneaking HTML into mail sent to other people.
    safe_owner = _html.escape(owner_name or "Someone")
    safe_invitee = _html.escape(invitee_email or "")
    safe_url = _html.escape(invite_url)
    html = f"""\
<!DOCTYPE html>
<html><body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif; color: #191929; max-width: 560px; margin: 0 auto; padding: 32px 24px;">
  <h1 style="font-size: 22px; margin: 0 0 16px;">You're invited to join a team</h1>
  <p style="line-height: 1.55; color: #444; margin: 0 0 12px;">
    <strong>{safe_owner}</strong> has invited you to join their DarInsights team
    as <code style="background: #f6f6fa; padding: 2px 6px; border-radius: 4px;">{safe_invitee}</code>.
  </p>
  <p style="line-height: 1.55; color: #444; margin: 0 0 24px;">
    Team members share the same workspaces, plan, and credit wallet — you'll see everything they're working on once you accept.
  </p>
  <p style="margin: 0 0 24px;">
    <a href="{safe_url}" style="display: inline-block; padding: 12px 22px; background: #3EDFCB; color: #191929; text-decoration: none; border-radius: 8px; font-weight: 600;">Accept invite →</a>
  </p>
  <p style="line-height: 1.55; color: #888; font-size: 13px; margin: 0 0 6px;">Or copy this link:</p>
  <p style="line-height: 1.45; color: #555; font-size: 12px; word-break: break-all; background: #fafafe; padding: 10px 12px; border-radius: 6px; margin: 0 0 24px;">{safe_url}</p>
  <p style="line-height: 1.55; color: #888; font-size: 12px; margin: 0;">If you didn't expect this invite, you can safely ignore this email.</p>
</body></html>"""
    return subject, text, html


def render_upsell_lto_email(
    *,
    user_name: str,
    headline: str,
    upgrade_url: str,
    hours_left: int,
    unsubscribe_url: Optional[str] = None,
) -> tuple[str, str, str]:
    """Subject + plain-text + HTML body for the limited-time-offer
    email. Fired once when a Free user crosses the prompt threshold;
    a partner to the in-app modal so users who close the tab still
    hear about the offer within the same 24h window.

    `headline` is the source-aware copy resolved by the resolver
    (e.g. "Need more workspaces?"). Falls back to a sensible default
    if empty.

    `unsubscribe_url` is a signed-token link to /unsubscribe/<token>;
    omitted only in legacy callers / unit tests. Including it makes
    the email CAN-SPAM compliant: marketing emails MUST carry a
    clear opt-out path in both plain-text and HTML bodies.
    """
    headline = (headline or "Ready to unlock everything?").strip()
    # Subject mirrors the headline so users can decide at a glance
    # whether to open it. Strip newlines defensively to avoid header
    # injection if `headline` ever comes from a user-controlled
    # source — today it doesn't, but cost is one cheap call.
    safe_subject_headline = headline.replace("\n", " ").replace("\r", " ")
    subject = f"{safe_subject_headline} (limited-time offer)"
    text_lines = [
        f"Hi {user_name or 'there'},",
        "",
        headline,
        "",
        "You've been bumping into our paid features. Upgrade now and unlock "
        "every cap — more workspaces, unlimited audits, multi-engine answer "
        "monitoring, and white-label reports.",
        "",
        f"Your special offer expires in about {hours_left} hour"
        f"{'s' if hours_left != 1 else ''}.",
        "",
        f"See plans: {upgrade_url}",
        "",
        "If you'd rather stick with the free tier, no need to do anything — "
        "the offer simply lapses on its own.",
    ]
    if unsubscribe_url:
        text_lines += [
            "",
            "—",
            f"Don't want these emails? Unsubscribe: {unsubscribe_url}",
        ]
    text = "\n".join(text_lines)

    safe_name = _html.escape(user_name or "there")
    safe_headline = _html.escape(headline)
    safe_url = _html.escape(upgrade_url)
    safe_unsub = _html.escape(unsubscribe_url) if unsubscribe_url else ""
    unsubscribe_footer = (
        f"""
  <hr style="border:0; border-top:1px solid #eee; margin:32px 0 16px;">
  <p style="line-height: 1.55; color: #888; font-size: 11.5px; margin: 0;">
    Don't want these emails?
    <a href="{safe_unsub}" style="color: #888; text-decoration: underline;">Unsubscribe</a>.
    Account emails (password resets, receipts, team invites) are unaffected.
  </p>"""
        if unsubscribe_url else ""
    )
    html = f"""\
<!DOCTYPE html>
<html><body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif; color: #191929; max-width: 560px; margin: 0 auto; padding: 32px 24px;">
  <p style="display: inline-block; padding: 4px 10px; border-radius: 999px; background: rgba(124,92,255,0.12); color: #7c5cff; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em; margin: 0 0 16px;">Limited time</p>
  <h1 style="font-size: 22px; margin: 0 0 16px;">{safe_headline}</h1>
  <p style="line-height: 1.55; color: #444; margin: 0 0 12px;">Hi {safe_name},</p>
  <p style="line-height: 1.55; color: #444; margin: 0 0 24px;">
    You've been bumping into our paid features. Upgrade now to unlock
    every cap — more workspaces, unlimited audits, multi-engine answer
    monitoring, and white-label reports.
  </p>
  <p style="line-height: 1.55; color: #92400e; background: #fef3c7; padding: 12px 14px; border-radius: 10px; margin: 0 0 24px; font-size: 14px;">
    <strong>Your special offer expires in about {hours_left} hour{'s' if hours_left != 1 else ''}.</strong>
  </p>
  <p style="margin: 0 0 24px;">
    <a href="{safe_url}" style="display: inline-block; padding: 12px 22px; background: #3EDFCB; color: #191929; text-decoration: none; border-radius: 8px; font-weight: 600;">See plans →</a>
  </p>
  <p style="line-height: 1.55; color: #888; font-size: 13px; margin: 0 0 6px;">Or copy this link:</p>
  <p style="line-height: 1.45; color: #555; font-size: 12px; word-break: break-all; background: #fafafe; padding: 10px 12px; border-radius: 6px; margin: 0 0 24px;">{safe_url}</p>
  <p style="line-height: 1.55; color: #888; font-size: 12px; margin: 0;">If you'd rather stick with the free tier, no need to do anything — the offer simply lapses on its own.</p>{unsubscribe_footer}
</body></html>"""
    return subject, text, html
