# Send OTP email via Resend API (preferred) or Gmail SMTP fallback.

from __future__ import annotations

import smtplib
import ssl
import urllib.error
import urllib.request
from email.message import EmailMessage
import json
from smtplib import SMTPAuthenticationError, SMTPException

from .settings import get_api_settings


class SmtpNotConfiguredError(Exception):
    # raised when GMAIL_USER / GMAIL_APP_PASSWORD are empty.
    pass


def _send_via_resend(to_addr: str, code: str) -> bool:
    s = get_api_settings()
    api_key = (s.resend_api_key or "").strip()
    from_email = (s.resend_from_email or "").strip()
    if not api_key or not from_email:
        return False

    payload = {
        "from": from_email,
        "to": [to_addr],
        "subject": "Your MananAI sign-in code",
        "text": (
            f"Your MananAI one-time code is: {code}\n\n"
            "It expires in a few minutes. If you didn't request this, ignore this email."
        ),
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=s.gmail_smtp_timeout_seconds) as resp:
            if 200 <= getattr(resp, "status", 0) < 300:
                return True
    except urllib.error.HTTPError as e:
        details = ""
        try:
            details = e.read().decode("utf-8", errors="ignore")
        except Exception:
            details = ""
        raise RuntimeError(
            f"Resend API rejected the request ({e.code}). Check RESEND_API_KEY and RESEND_FROM_EMAIL. {details}"
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Could not reach Resend API: {e.reason!s}") from e
    return False


def send_otp_email(to_addr: str, code: str) -> None:
    s = get_api_settings()
    if _send_via_resend(to_addr, code):
        return

    # google app passwords often get copied with spaces; SMTP wants 16 chars, no spaces.
    app_pw = (s.gmail_app_password or "").replace(" ", "").strip()
    user = (s.gmail_user or "").strip()
    if not user or not app_pw:
        raise SmtpNotConfiguredError("Set GMAIL_USER and GMAIL_APP_PASSWORD in adaptive_dsa_agent/.env")

    msg = EmailMessage()
    msg["Subject"] = "Your MananAI sign-in code"
    msg["From"] = user
    msg["To"] = to_addr
    msg.set_content(
        f"Your MananAI one-time code is: {code}\n\n"
        "It expires in a few minutes. If you didn't request this, ignore this email."
    )

    host = s.gmail_smtp_host
    port = s.gmail_smtp_port
    timeout = s.gmail_smtp_timeout_seconds
    context = ssl.create_default_context()

    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=context, timeout=timeout) as server:
                server.login(user, app_pw)
                server.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=timeout) as server:
                server.starttls(context=context)
                server.login(user, app_pw)
                server.send_message(msg)
    except SMTPAuthenticationError as e:
        raise RuntimeError(
            "Gmail rejected the sign-in. Use a 16-character App Password (Google Account -> "
            "Security -> 2-Step Verification -> App passwords), not your normal password. "
            f"Details: {e!s}"
        ) from e
    except (OSError, SMTPException) as e:
        # timeouts, connection refused, TLS issues — turn into a short message.
        raise RuntimeError(
            f"Could not reach Gmail SMTP at {host}:{port} within {timeout}s. "
            "If port 587 is blocked, try GMAIL_SMTP_PORT=465 in .env. "
            f"Error: {e!s}"
        ) from e
