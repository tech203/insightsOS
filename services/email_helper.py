"""
SMTP-based transactional email.

No external service dependency — stdlib smtplib + email.message. Set
the SMTP_* env vars and we route messages through whatever provider
you've connected (SES, SendGrid SMTP relay, Postmark SMTP, Mailgun
SMTP, even a Gmail App Password for low-volume).

Without SMTP_HOST set, send_email() returns False and the caller
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


def is_email_configured() -> bool:
    """True when at minimum SMTP_HOST + SMTP_FROM are set. We don't
    require user/pass since some relays (internal SMTP, AWS SES from
    a whitelisted IP) work without them."""
    host = (os.getenv("SMTP_HOST") or "").strip()
    sender = (os.getenv("SMTP_FROM") or "").strip()
    return bool(host) and bool(sender) and not host.startswith("your_")


def send_email(
    *,
    to: str,
    subject: str,
    body_text: str,
    body_html: Optional[str] = None,
    reply_to: Optional[str] = None,
) -> bool:
    """Send a transactional email. Returns True on success, False
    when SMTP isn't configured or the send raised. Never raises —
    callers fall back to their in-app surface on False."""
    if not is_email_configured():
        return False

    if not to or "@" not in to:
        return False

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
