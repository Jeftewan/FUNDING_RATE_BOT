"""Email service for sending magic link authentication emails."""
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

log = logging.getLogger("bot.email")


def send_magic_link(email: str, link: str, config) -> bool:
    """Send a magic link login email. Returns True on success."""

    subject = "Iniciar sesion — Funding Rate Bot"
    html = f"""
    <div style="font-family:monospace;background:#0a0b0d;color:#c8ccd0;padding:40px;text-align:center">
      <h2 style="color:#fff;font-size:18px">Funding Rate Bot</h2>
      <p style="color:#888;font-size:14px">Haz click para iniciar sesion:</p>
      <a href="{link}" style="display:inline-block;background:#3b82f6;color:#fff;padding:12px 32px;
         border-radius:8px;text-decoration:none;font-size:14px;margin:20px 0">
        Iniciar Sesion
      </a>
      <p style="color:#555;font-size:11px;margin-top:24px">
        Este enlace expira en 15 minutos.<br>
        Si no solicitaste este email, ignoralo.
      </p>
    </div>
    """

    # Try SendGrid first
    if config.SENDGRID_API_KEY:
        return _send_sendgrid(email, subject, html, config)

    # Fallback to SMTP
    if config.SMTP_HOST:
        return _send_smtp(email, subject, html, config)

    log.error("No email service configured (SENDGRID_API_KEY or SMTP_HOST)")
    return False


def _send_sendgrid(to: str, subject: str, html: str, config) -> bool:
    try:
        import requests
        res = requests.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={
                "Authorization": f"Bearer {config.SENDGRID_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "personalizations": [{"to": [{"email": to}]}],
                "from": {"email": config.MAIL_FROM},
                "subject": subject,
                "content": [{"type": "text/html", "value": html}],
            },
            timeout=10,
        )
        if res.status_code in (200, 201, 202):
            log.info(f"Magic link sent to {to} via SendGrid")
            return True
        log.error(f"SendGrid error {res.status_code}: {res.text}")
        return False
    except Exception as e:
        log.error(f"SendGrid exception: {e}")
        return False


def _send_smtp(to: str, subject: str, html: str, config) -> bool:
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = config.MAIL_FROM
        msg["To"] = to
        msg.attach(MIMEText(html, "html"))

        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=10) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            if config.SMTP_USER:
                server.login(config.SMTP_USER, config.SMTP_PASS)
            server.sendmail(config.MAIL_FROM, to, msg.as_string())

        log.info(f"Magic link sent to {to} via SMTP")
        return True
    except Exception as e:
        log.error(f"SMTP exception: {e}")
        return False
