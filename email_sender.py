"""
Email Sender Utility
====================

SUMMARY
-------
Shared module for sending consolidated analysis reports via email.
Used by run_all.py and BulkBlock.py to deliver reports with file
attachments over SMTP.

WORKFLOW
--------
1. Load SMTP configuration from environment variables.
2. Build MIME message with subject, body text, and file attachments.
3. Connect to SMTP server with TLS encryption.
4. Authenticate and send email.
5. Return True on success, False on failure.

DATA SOURCES
------------
Configuration only — loaded from environment variables:
    EMAIL_SMTP_SERVER   — SMTP server (default: smtp.gmail.com)
    EMAIL_SMTP_PORT     — SMTP port (default: 587)
    EMAIL_USE_TLS       — 'true' or 'false' (default: true)
    EMAIL_FROM          — Sender email address
    EMAIL_SENDER_NAME   — Display name (default: Market Analysis Bot)
    EMAIL_TO            — Comma-separated recipient addresses
    EMAIL_USERNAME      — SMTP login username (defaults to EMAIL_FROM)
    EMAIL_PASSWORD      — SMTP password / app-specific password
    EMAIL_SUBJECT_PREFIX— Subject prefix (default: Daily Market Analysis Report)

OUTPUT
------
No files — sends email only.

USAGE
-----
This is a library module, not meant to be run directly.

    from email_sender import send_report
    send_report(
        subject="Daily Report — 02-May-2026",
        body_text="Please find attached the daily analysis reports.",
        attachments=["report.xlsx", "chart.html"],
    )

Group run (via run_all.py):
    Called automatically at the end of run_all.py (unless --no-email).

DEPENDENCIES
------------
smtplib, email.mime (stdlib only — no external packages)
"""

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders


def load_config():
    """Load email configuration from environment variables."""
    cfg = {
        "smtp_server": os.environ.get("EMAIL_SMTP_SERVER", "smtp.gmail.com"),
        "smtp_port": int(os.environ.get("EMAIL_SMTP_PORT", 587)),
        "use_tls": os.environ.get("EMAIL_USE_TLS", "true").lower() == "true",
        "sender_email": os.environ.get("EMAIL_FROM", ""),
        "sender_name": os.environ.get("EMAIL_SENDER_NAME", "Market Analysis Bot"),
        "recipients": [],
        "username": "",
        "password": os.environ.get("EMAIL_PASSWORD", ""),
        "subject_prefix": os.environ.get("EMAIL_SUBJECT_PREFIX",
                                         "Daily Market Analysis Report"),
    }

    # Recipients from comma-separated env var
    env_to = os.environ.get("EMAIL_TO", "")
    cfg["recipients"] = [r.strip() for r in env_to.split(",") if r.strip()]

    # Username defaults to sender email
    cfg["username"] = os.environ.get("EMAIL_USERNAME", cfg["sender_email"])

    return cfg


def send_report(subject=None, body_text=None, attachments=None):
    """Send an email with file attachments.

    Args:
        subject:     Email subject line. Falls back to config subject_prefix + date.
        body_text:   Plain-text email body.
        attachments: List of file paths to attach.

    Returns:
        True if sent successfully, False otherwise.
    """
    cfg = load_config()

    password = cfg["password"]
    if not password:
        print("  WARNING: EMAIL_PASSWORD env var not set. Skipping email send.")
        return False

    sender = cfg["sender_email"]
    recipients = cfg["recipients"]
    if not sender or not recipients:
        print("  WARNING: sender_email or recipients not configured. Skipping email.")
        return False

    if subject is None:
        import datetime
        subject = "%s — %s" % (
            cfg["subject_prefix"],
            datetime.date.today().strftime("%d-%b-%Y"),
        )

    if body_text is None:
        body_text = "Please find attached the daily market analysis reports."

    # Build message
    msg = MIMEMultipart()
    msg["From"] = "%s <%s>" % (cfg["sender_name"], sender)
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.attach(MIMEText(body_text, "plain"))

    # Attach files
    attached_count = 0
    for filepath in (attachments or []):
        if not os.path.exists(filepath):
            print("  WARNING: Attachment not found: %s" % filepath)
            continue
        filename = os.path.basename(filepath)
        with open(filepath, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment", filename=filename)
        msg.attach(part)
        attached_count += 1

    if attached_count == 0:
        print("  WARNING: No valid attachments found. Skipping email.")
        return False

    # Send
    try:
        server = smtplib.SMTP(cfg["smtp_server"], cfg["smtp_port"], timeout=30)
        if cfg["use_tls"]:
            server.starttls()
        server.login(cfg["username"], password)
        server.sendmail(sender, recipients, msg.as_string())
        server.quit()

        print("  Email sent to: %s (%d attachments)" % (
            ", ".join(recipients), attached_count))
        return True

    except Exception as e:
        print("  ERROR sending email: %s" % e)
        return False
