"""Gmail SMTP 발송(D-04, FR-MAIL-02·03, FR-EDT-06).

수신자는 봉투(RCPT)에만 넣고 헤더에는 넣지 않는다(BCC와 같은 효과). 로그에 주소를 남기지 않는다.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from collections.abc import Callable
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from ainews.models import RenderedMail
from ainews.settings import Settings, require
from ainews.util.retry import retry

log = logging.getLogger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
FROM_NAME = "AI Daily"


class MailError(Exception):
    """발송 실패. 메시지에 주소를 담지 않는다."""


def is_transient_smtp(exc: BaseException) -> bool:
    if isinstance(exc, smtplib.SMTPResponseException):
        return 400 <= exc.smtp_code < 500
    if isinstance(exc, smtplib.SMTPAuthenticationError | smtplib.SMTPRecipientsRefused):
        return False
    return isinstance(exc, smtplib.SMTPServerDisconnected | smtplib.SMTPConnectError | OSError)


def build_message(mail: RenderedMail, sender: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = mail.subject
    msg["From"] = formataddr((FROM_NAME, sender))
    msg["To"] = formataddr((FROM_NAME, sender))
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg.set_content(mail.text)
    msg.add_alternative(mail.html, subtype="html")
    return msg


def send(
    mail: RenderedMail,
    settings: Settings,
    *,
    smtp_factory: Callable[[], smtplib.SMTP] | None = None,
) -> None:
    require(settings, "gmail_address", "gmail_app_password", "recipients")
    sender = settings.gmail_address or ""
    msg = build_message(mail, sender)
    factory = smtp_factory or (
        lambda: smtplib.SMTP_SSL(
            SMTP_HOST, SMTP_PORT, context=ssl.create_default_context(), timeout=30
        )
    )

    def connect() -> smtplib.SMTP:
        smtp = factory()
        try:
            smtp.login(sender, settings.gmail_app_password or "")
        except BaseException:
            smtp.close()
            raise
        return smtp

    # 재시도는 접속·로그인까지만 한다. DATA를 보낸 뒤의 오류(응답 타임아웃 등)는 서버가
    # 이미 받았을 수 있어서 다시 보내면 중복 발송이 된다.
    try:
        with retry(connect, is_transient=is_transient_smtp) as smtp:
            smtp.send_message(msg, from_addr=sender, to_addrs=list(settings.recipients))
    except (smtplib.SMTPException, OSError) as exc:
        # SMTP 오류 메시지·인자에는 수신자 주소가 들어갈 수 있어 원래 예외를 떼고 올린다.
        code = getattr(exc, "smtp_code", "")
        raise MailError(f"메일 발송 실패: {type(exc).__name__} {code}".rstrip()) from None
    log.info("메일 발송 완료: 수신자 %d명", len(settings.recipients))
