from __future__ import annotations

import smtplib
from pathlib import Path
from typing import Any

import pytest

from ainews.mail import smtp
from ainews.models import RenderedMail
from ainews.settings import ConfigError, Settings, load_settings, load_sources, require


def test_load_settings_defaults_and_secrets_hidden() -> None:
    s = load_settings(
        {"RECIPIENT_EMAIL": " a@x.invalid, ,b@x.invalid ", "GMAIL_APP_PASSWORD": "pw"}
    )
    assert s.recipients == ("a@x.invalid", "b@x.invalid")
    assert s.daily_budget_usd == 1.0
    assert s.model_classify == "claude-haiku-5-5"
    assert s.send_empty is False
    assert "pw" not in repr(s) and "a@x.invalid" not in repr(s)


def test_load_settings_overrides() -> None:
    s = load_settings({"AINEWS_DAILY_BUDGET_USD": "0.5", "AINEWS_SEND_EMPTY": "true"})
    assert s.daily_budget_usd == 0.5
    assert s.send_empty is True


def test_require_names_only() -> None:
    with pytest.raises(ConfigError, match="gmail_address") as exc:
        require(Settings(gmail_app_password="secret"), "gmail_address", "gmail_app_password")
    assert "secret" not in str(exc.value)


def test_load_sources(tmp_path: Path) -> None:
    assert load_sources(tmp_path / "none.yaml") == []
    p = tmp_path / "sources.yaml"
    p.write_text(
        "sources:\n  - {name: A, category: official, kind: changelog, method: rss, url: 'https://a'}\n",
        encoding="utf-8",
    )
    [src] = load_sources(p)
    assert src.name == "A" and src.enabled and src.max_items == 10


class FakeSMTP:
    instances: list[FakeSMTP] = []

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        FakeSMTP.instances.append(self)

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *a: object) -> None:
        pass

    def close(self) -> None:
        pass

    def login(self, user: str, pw: str) -> None:
        self.login_user = user

    def send_message(self, msg: Any, from_addr: str, to_addrs: list[str]) -> None:
        self.sent.append({"msg": msg, "to": to_addrs})


def test_send_uses_envelope_only_recipients(settings: Settings) -> None:
    FakeSMTP.instances.clear()
    mail = RenderedMail(subject="제목", html="<p>본문</p>", text="본문")
    smtp.send(mail, settings, smtp_factory=FakeSMTP)  # type: ignore[arg-type]
    [sent] = FakeSMTP.instances[0].sent
    assert sent["to"] == list(settings.recipients)
    raw = sent["msg"].as_string()
    for r in settings.recipients:
        assert r not in raw  # 헤더에 수신자 주소가 드러나지 않는다(FR-MAIL-03)
    assert sent["msg"].get_content_type() == "multipart/alternative"


def test_send_requires_credentials() -> None:
    with pytest.raises(ConfigError):
        smtp.send(RenderedMail(subject="s", html="h", text="t"), Settings())


def test_send_not_retried_after_data(settings: Settings) -> None:
    """DATA를 보낸 뒤 타임아웃은 서버가 이미 받았을 수 있어 다시 보내지 않는다."""

    class TimeoutSMTP(FakeSMTP):
        def send_message(self, msg: Any, from_addr: str, to_addrs: list[str]) -> None:
            raise TimeoutError("no reply after DATA")

    FakeSMTP.instances.clear()
    with pytest.raises(smtp.MailError):
        smtp.send(RenderedMail(subject="s", html="h", text="t"), settings, smtp_factory=TimeoutSMTP)  # type: ignore[arg-type]
    assert len(FakeSMTP.instances) == 1


def test_send_error_hides_recipients(settings: Settings) -> None:
    class RefusingSMTP(FakeSMTP):
        def send_message(self, msg: Any, from_addr: str, to_addrs: list[str]) -> None:
            raise smtplib.SMTPRecipientsRefused({a: (550, b"no") for a in to_addrs})

    with pytest.raises(smtp.MailError) as exc:
        smtp.send(
            RenderedMail(subject="s", html="h", text="t"), settings, smtp_factory=RefusingSMTP
        )  # type: ignore[arg-type]
    assert exc.value.__cause__ is None and exc.value.__context__ is not None
    assert all(r not in str(exc.value) for r in settings.recipients)
