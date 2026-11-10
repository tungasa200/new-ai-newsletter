"""설정과 비밀값 로딩. 비밀값은 환경변수에서만 읽고 repr·로그에 남기지 않는다."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta, timezone
from pathlib import Path

import yaml

from ainews import __version__
from ainews.models import SourceConfig

KST = timezone(timedelta(hours=9), "KST")

DEFAULT_CONTACT = "https://github.com/tungasa200/new-ai-newsletter"


class ConfigError(Exception):
    """필요한 설정이 없다. 메시지에는 이름만 넣고 값은 넣지 않는다."""


@dataclass(frozen=True)
class Settings:
    # 비밀값 (repr 제외)
    anthropic_api_key: str | None = field(default=None, repr=False)
    gmail_address: str | None = field(default=None, repr=False)
    gmail_app_password: str | None = field(default=None, repr=False)
    recipients: tuple[str, ...] = field(default=(), repr=False)
    ops_email: str | None = field(default=None, repr=False)
    github_token: str | None = field(default=None, repr=False)
    # 일반 설정
    state_dir: Path = Path("state")
    out_dir: Path = Path("out")
    sources_path: Path = Path("sources.yaml")
    step_summary_path: Path | None = None  # GitHub Actions job 요약 파일(GITHUB_STEP_SUMMARY)
    daily_budget_usd: float = 1.00
    model_classify: str = "claude-haiku-5-5"
    model_edit: str = "claude-sonnet-5-5"
    model_fallback: str = "claude-haiku-5-5"
    send_empty: bool = False
    # 게재 기준(D-13, FR-SEL-06): 중요도 이 값 이상 또는 실무 적용성 이 값 이상. 하루 상한 건수.
    publish_min_importance: int = 5
    publish_min_practicality: int = 4
    publish_max_items: int = 5
    user_agent_contact: str = DEFAULT_CONTACT

    @property
    def user_agent(self) -> str:
        return f"ainews/{__version__} (+{self.user_agent_contact})"


def _bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env

    def get(name: str) -> str | None:
        value = env.get(name, "").strip()
        return value or None

    recipients = tuple(r.strip() for r in (get("RECIPIENT_EMAIL") or "").split(",") if r.strip())
    defaults = Settings()
    return Settings(
        anthropic_api_key=get("ANTHROPIC_API_KEY"),
        gmail_address=get("GMAIL_ADDRESS"),
        gmail_app_password=get("GMAIL_APP_PASSWORD"),
        recipients=recipients,
        ops_email=get("OPS_EMAIL"),
        github_token=get("GITHUB_TOKEN"),
        state_dir=Path(get("AINEWS_STATE_DIR") or defaults.state_dir),
        out_dir=Path(get("AINEWS_OUT_DIR") or defaults.out_dir),
        step_summary_path=Path(p) if (p := get("GITHUB_STEP_SUMMARY")) else None,
        daily_budget_usd=float(get("AINEWS_DAILY_BUDGET_USD") or defaults.daily_budget_usd),
        model_classify=get("AINEWS_MODEL_CLASSIFY") or defaults.model_classify,
        model_edit=get("AINEWS_MODEL_EDIT") or defaults.model_edit,
        model_fallback=get("AINEWS_MODEL_FALLBACK") or defaults.model_fallback,
        send_empty=_bool(get("AINEWS_SEND_EMPTY") or "false"),
        publish_min_importance=int(
            get("AINEWS_PUBLISH_MIN_IMPORTANCE") or defaults.publish_min_importance
        ),
        publish_min_practicality=int(
            get("AINEWS_PUBLISH_MIN_PRACTICALITY") or defaults.publish_min_practicality
        ),
        publish_max_items=int(get("AINEWS_PUBLISH_MAX_ITEMS") or defaults.publish_max_items),
        user_agent_contact=get("AINEWS_USER_AGENT_CONTACT") or defaults.user_agent_contact,
    )


def require(settings: Settings, *names: str) -> None:
    """설정 필드가 비어 있으면 ConfigError. 예: require(s, "gmail_address")."""
    missing = [n for n in names if not getattr(settings, n)]
    if missing:
        raise ConfigError(f"필요한 설정이 없다: {', '.join(missing)}")


def load_sources(path: Path) -> list[SourceConfig]:
    """sources.yaml의 `sources:` 목록을 읽는다. 파일이 없으면 빈 목록."""
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [SourceConfig.model_validate(s) for s in data.get("sources", [])]
