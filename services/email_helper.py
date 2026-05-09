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
) -> bool:
    """Send a transactional email. Returns True on success, False
    when no backend is configured or the send raised. Never raises —
    callers fall back to their in-app surface on False."""
    if not to or "@" not in to:
        return False

    if _resend_configured():
        return _send_via_resend(
            to=to, subject=subject, body_text=body_text,
            body_html=body_html, reply_to=reply_to,
        )
    if _smtp_configured():
        return _send_via_smtp(
            to=to, subject=subject, body_text=body_text,
            body_html=body_html, reply_to=reply_to,
        )
    return False


def render_password_reset_email(*, user_name: str, reset_url: str) -> tuple[str, str, str]:
    """Subject + plain-text + HTML body for a password-reset request."""
    subject = "Reset your DarInsights password"
    text = (
        f"Hi {user_name or 'there'},\n\n"
        "We received a request to reset the password on your DarInsights account.\n"
        "Click the link below to set a new password. The link expires in 60 minutes.\n\n"
        f"{reset_url}\n\n"
        "If you didn't request a password reset, you can safely ignore this email — "
        "your password stays unchanged."
    )
    html = f"""\
<!DOCTYPE html>
<html><body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif; color: #191929; max-width: 560px; margin: 0 auto; padding: 32px 24px;">
  <h1 style="font-size: 22px; margin: 0 0 16px;">Reset your password</h1>
  <p style="line-height: 1.55; color: #444; margin: 0 0 12px;">Hi {user_name or 'there'},</p>
  <p style="line-height: 1.55; color: #444; margin: 0 0 24px;">
    We received a request to reset the password on your DarInsights account.
    Click below to set a new password. <strong>The link expires in 60 minutes.</strong>
  </p>
  <p style="margin: 0 0 24px;">
    <a href="{reset_url}" style="display: inline-block; padding: 12px 22px; background: #3EDFCB; color: #191929; text-decoration: none; border-radius: 8px; font-weight: 600;">Reset password →</a>
  </p>
  <p style="line-height: 1.55; color: #888; font-size: 13px; margin: 0 0 6px;">Or copy this link:</p>
  <p style="line-height: 1.45; color: #555; font-size: 12px; word-break: break-all; background: #fafafe; padding: 10px 12px; border-radius: 6px; margin: 0 0 24px;">{reset_url}</p>
  <p style="line-height: 1.55; color: #888; font-size: 12px; margin: 0;">If you didn't request a password reset, you can safely ignore this email — your password stays unchanged.</p>
</body></html>"""
    return subject, text, html


def render_team_invite_email(
    *,
    owner_name: str,
    invitee_email: str,
    invite_url: str,
) -> tuple[str, str, str]:
    """Build subject + plain-text + HTML body for a team invite."""
    subject = f"{owner_name} invited you to join their team on DarInsights"
    text = (
        f"{owner_name} has invited you to join their DarInsights team as {invitee_email}.\n\n"
        "Team members share the same workspaces, plan, and credit wallet — you'll see "
        "everything they're working on once you accept.\n\n"
        f"Accept the invite: {invite_url}\n\n"
        "If you didn't expect this invite, you can safely ignore this email."
    )
    html = f"""\
<!DOCTYPE html>
<html><body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif; color: #191929; max-width: 560px; margin: 0 auto; padding: 32px 24px;">
  <h1 style="font-size: 22px; margin: 0 0 16px;">You're invited to join a team</h1>
  <p style="line-height: 1.55; color: #444; margin: 0 0 12px;">
    <strong>{owner_name}</strong> has invited you to join their DarInsights team
    as <code style="background: #f6f6fa; padding: 2px 6px; border-radius: 4px;">{invitee_email}</code>.
  </p>
  <p style="line-height: 1.55; color: #444; margin: 0 0 24px;">
    Team members share the same workspaces, plan, and credit wallet — you'll see everything they're working on once you accept.
  </p>
  <p style="margin: 0 0 24px;">
    <a href="{invite_url}" style="display: inline-block; padding: 12px 22px; background: #3EDFCB; color: #191929; text-decoration: none; border-radius: 8px; font-weight: 600;">Accept invite →</a>
  </p>
  <p style="line-height: 1.55; color: #888; font-size: 13px; margin: 0 0 6px;">Or copy this link:</p>
  <p style="line-height: 1.45; color: #555; font-size: 12px; word-break: break-all; background: #fafafe; padding: 10px 12px; border-radius: 6px; margin: 0 0 24px;">{invite_url}</p>
  <p style="line-height: 1.55; color: #888; font-size: 12px; margin: 0;">If you didn't expect this invite, you can safely ignore this email.</p>
</body></html>"""
    return subject, text, html
