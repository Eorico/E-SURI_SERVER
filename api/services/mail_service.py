"""Sending mail, and the two messages this system sends.

Confirmation codes and password-reset links are minted, signed and sent by this
server. The token is this server's to issue and to verify, so the wording, the
sender and the expiry all live in the repository rather than in somebody else's
dashboard.

`Mailer` is a Protocol so the service can be exercised without a mail server;
`NullMailer` is what runs in tests and on a development machine.
"""

from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol

from api.core.config import settings
from api.core.exceptions import UpstreamError
from api.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class Message:
    to: str
    subject: str
    text: str
    html: str


class Mailer(Protocol):
    def send(self, message: Message) -> None: ...


class SmtpMailer:
    """Delivers over SMTP. Defaults to Gmail's submission port with STARTTLS."""

    def send(self, message: Message) -> None:
        if not settings.SMTP_USERNAME or not settings.SMTP_PASSWORD:
            raise UpstreamError(
                "Email is enabled but SMTP_USERNAME / SMTP_PASSWORD are not set."
            )

        sender = settings.SMTP_FROM_EMAIL or settings.SMTP_USERNAME

        email = EmailMessage()
        email["Subject"] = message.subject
        email["From"] = f"{settings.SMTP_FROM_NAME} <{sender}>"
        email["To"] = message.to
        email.set_content(message.text)
        email.add_alternative(message.html, subtype="html")

        context = ssl.create_default_context()
        try:
            if settings.SMTP_STARTTLS:
                with smtplib.SMTP(
                    settings.SMTP_HOST, settings.SMTP_PORT, timeout=settings.SMTP_TIMEOUT_SECONDS
                ) as server:
                    server.starttls(context=context)
                    server.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
                    server.send_message(email)
            else:
                # Port 465: TLS from the first byte, no STARTTLS handshake.
                with smtplib.SMTP_SSL(
                    settings.SMTP_HOST,
                    settings.SMTP_PORT,
                    timeout=settings.SMTP_TIMEOUT_SECONDS,
                    context=context,
                ) as server:
                    server.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
                    server.send_message(email)
        except smtplib.SMTPAuthenticationError as exc:
            # The most common misconfiguration by far, and the error Gmail
            # returns is not self-explanatory.
            logger.exception("smtp_auth_failed")
            raise UpstreamError(
                "The mail server rejected our credentials. For Gmail, SMTP_PASSWORD "
                "must be a 16-character App Password, not the account password."
            ) from exc
        except Exception as exc:  # pragma: no cover - network dependent
            logger.exception("smtp_send_failed")
            raise UpstreamError("Could not send that email. Try again shortly.") from exc

        # The address is logged, the link never is: a log is not a safe place
        # for a credential that signs someone in.
        logger.info("email_sent", extra={"to": message.to, "subject": message.subject})


class NullMailer:
    """Writes the link to the log instead of mailing it.

    What runs with `EMAIL_ENABLED=false`, so a development machine cannot send
    to a real address. The link is logged here on purpose — without a mailbox
    there is no other way to follow it.
    """

    def __init__(self) -> None:
        self.sent: list[Message] = []

    def send(self, message: Message) -> None:
        self.sent.append(message)
        logger.info(
            "email_suppressed",
            extra={
                "to": message.to,
                "subject": message.subject,
                "body": message.text,
                "hint": "EMAIL_ENABLED is false; set it to true to send for real.",
            },
        )


def build_mailer() -> Mailer:
    return SmtpMailer() if settings.EMAIL_ENABLED else NullMailer()


# --------------------------------------------------------------------------- #
# The two messages                                                             #
# --------------------------------------------------------------------------- #
def _wrap(heading: str, lead: str, action: str, link: str, footer: str) -> str:
    """One plain, table-free layout. Mail clients are not browsers."""
    return f"""\
<!doctype html>
<html>
  <body style="margin:0;padding:24px;background:#eef1ec;font-family:Arial,Helvetica,sans-serif;color:#1d3b31;">
    <div style="max-width:520px;margin:0 auto;background:#ffffff;border-radius:14px;padding:32px;">
      <p style="margin:0 0 4px;font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:#6b7f72;">E-SURI</p>
      <h1 style="margin:0 0 16px;font-size:22px;line-height:1.25;">{heading}</h1>
      <p style="margin:0 0 24px;font-size:14px;line-height:1.6;">{lead}</p>
      <p style="margin:0 0 24px;">
        <a href="{link}" style="display:inline-block;background:#1d3b31;color:#ffffff;text-decoration:none;padding:12px 22px;border-radius:8px;font-size:14px;font-weight:bold;">{action}</a>
      </p>
      <p style="margin:0 0 8px;font-size:12px;color:#6b7f72;">If the button does not work, paste this into your browser:</p>
      <p style="margin:0 0 24px;font-size:12px;word-break:break-all;color:#36604f;">{link}</p>
      <p style="margin:0;font-size:12px;line-height:1.6;color:#6b7f72;">{footer}</p>
    </div>
  </body>
</html>"""


def confirmation_message(to: str, name: str, code: str, link: str) -> Message:
    """The registration email.

    The code leads, because the form the person just left is waiting for it.
    The link is kept underneath for anyone reading on a phone who would rather
    tap than retype six digits.
    """
    greeting = f"Hello {name}," if name else "Hello,"
    text = "\n".join(
        [
            greeting,
            "",
            "An E-SURI account was created for this address. Enter this code to",
            "finish setting it up:",
            "",
            f"    {code}",
            "",
            "The code expires shortly and can only be used once.",
            "",
            "If you would rather not type it, this link does the same thing:",
            link,
            "",
            "If you did not ask for this account, ignore this message - nothing",
            "happens until the code or the link is used.",
            "",
        ]
    )
    html = f"""<!doctype html>
<html>
  <body style="margin:0;padding:24px;background:#eef1ec;font-family:Arial,Helvetica,sans-serif;color:#1d3b31;">
    <div style="max-width:520px;margin:0 auto;background:#ffffff;border-radius:14px;padding:32px;">
      <p style="margin:0 0 4px;font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:#6b7f72;">E-SURI</p>
      <h1 style="margin:0 0 16px;font-size:22px;line-height:1.25;">Confirm your account</h1>
      <p style="margin:0 0 24px;font-size:14px;line-height:1.6;">{greeting} enter this code on the page you just left to finish setting up your account.</p>
      <p style="margin:0 0 8px;text-align:center;">
        <span style="display:inline-block;background:#eef1ec;color:#1d3b31;padding:16px 28px;border-radius:10px;font-size:30px;font-weight:bold;letter-spacing:.32em;font-family:'Courier New',monospace;">{code}</span>
      </p>
      <p style="margin:0 0 24px;text-align:center;font-size:12px;color:#6b7f72;">Expires shortly &middot; can only be used once</p>
      <p style="margin:0 0 8px;font-size:12px;color:#6b7f72;">Would rather tap than type? This link does the same thing:</p>
      <p style="margin:0 0 24px;font-size:12px;word-break:break-all;color:#36604f;">
        <a href="{link}" style="color:#36604f;">{link}</a>
      </p>
      <p style="margin:0;font-size:12px;line-height:1.6;color:#6b7f72;">If you did not ask for this account, ignore this message - nothing happens until the code or the link is used.</p>
    </div>
  </body>
</html>"""
    return Message(to=to, subject="Your E-SURI confirmation code", text=text, html=html)


def password_reset_message(to: str, link: str) -> Message:
    text = (
        "Hello,\n\n"
        "Someone asked to reset the password for this E-SURI account. Use this "
        "link to choose a new one:\n\n"
        f"{link}\n\n"
        "The link can only be used once and expires shortly.\n\n"
        "If this was not you, ignore this message — your password has not "
        "changed, and nothing happens until the link is used.\n"
    )
    html = _wrap(
        heading="Reset your E-SURI password",
        lead=(
            "Someone asked to reset the password for this account. Use the button below "
            "to choose a new one. The link can only be used once and expires shortly."
        ),
        action="Choose a new password",
        link=link,
        footer=(
            "If this was not you, ignore this message — your password has not changed, "
            "and nothing happens until the link is used."
        ),
    )
    return Message(to=to, subject="Reset your E-SURI password", text=text, html=html)
