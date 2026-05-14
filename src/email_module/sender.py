"""
Email sending via Gmail SMTP.

Uses Gmail's app-password authentication (NOT regular Gmail password).
Setup:
1. Enable 2FA on the Gmail account
2. Generate an app password at https://myaccount.google.com/apppasswords
3. Set GMAIL_APP_PASSWORD env / GitHub Secret with that password
"""

from __future__ import annotations

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

from src.config import (
    SMTP_HOST,
    SMTP_PORT,
    SMTP_USER,
    SMTP_PASSWORD,
    EMAIL_TO,
    EMAIL_FROM_NAME,
)

logger = logging.getLogger(__name__)


def send_email(subject: str, html_body: str, recipient: str = None) -> bool:
    """Send an HTML email via Gmail SMTP. Returns True on success."""

    to_addr = recipient or EMAIL_TO
    if not (SMTP_USER and SMTP_PASSWORD and to_addr):
        logger.error(
            "Email sending requires GMAIL_ADDRESS, GMAIL_APP_PASSWORD, "
            "and EMAIL_RECIPIENT environment variables."
        )
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr((EMAIL_FROM_NAME, SMTP_USER))
    msg["To"] = to_addr

    # Attach a basic plain-text fallback so spam filters are happier
    plain_fallback = (
        "This is the Memory Cycle Daily Brief in HTML format. "
        "If you're seeing this, your email client doesn't support HTML. "
        "Please open in a modern browser-based mail reader."
    )
    msg.attach(MIMEText(plain_fallback, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
        logger.info(f"Email sent successfully to {to_addr}")
        return True
    except Exception as e:
        logger.error(f"Failed to send email: {e}")
        return False
