"""단계 사이를 오가는 공통 데이터 모델. Claude 구조화 출력 스키마도 여기서 나온다.

이 파일은 공용(AN-backend1 담당)이다. 필드를 바꿔야 하면 AN-backend1에 요청한다.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, Field, field_validator, model_validator

from ainews.util.urlnorm import is_web_url, normalize_url, strip_utm, url_id

Category = Literal["official", "community"]
Kind = Literal["changelog", "release", "blog", "news", "forum"]
Method = Literal["rss", "api", "html", "sitemap", "file_diff"]
ItemType = Literal["release", "feature_change", "tip", "research", "industry", "policy"]
Freshness = Literal["current", "caution", "outdated"]  # 현행 / 주의 / 구식

SUMMARY_MAX = 1500


def _web_url(v: str) -> str:
    if not is_web_url(v):  # 메일 href에 javascript:·data: 등이 들어가지 않게 한다
        raise ValueError("http(s) URL만 허용한다")
    return v


WebUrl = Annotated[str, AfterValidator(_web_url)]


class SourceConfig(BaseModel):
    """sources.yaml의 소스 하나(FR-SRC-03). 소스별 추가 값은 options에 둔다."""

    name: str
    category: Category
    kind: Kind
    method: Method
    url: str
    lookback_hours: int | None = None  # 지정하면 직전 실행과 무관하게 이 기간을 본다
    max_items: int = 10
    enabled: bool = True
    options: dict[str, Any] = Field(default_factory=dict)


class Item(BaseModel):
    """수집 항목 하나(FR-SRC-07). 만들 때는 Item.new()를 쓴다."""

    id: str
    source: str
    category: Category
    kind: Kind
    title: str
    url: WebUrl  # 원본 링크(utm_*만 뺀다). 메일에 이 값을 싣는다
    key: str  # 정규화 URL. 중복 제거·발송 이력 비교용, id는 이 값의 해시
    published_at: dt.datetime | None = None  # UTC
    summary: str = ""
    score: int | None = None
    comments: int | None = None
    body: str | None = None  # 본문 보강(상위 후보만)

    @field_validator("summary")
    @classmethod
    def _cap_summary(cls, v: str) -> str:
        return v[:SUMMARY_MAX]

    @classmethod
    def new(cls, *, url: str, **fields: Any) -> Item:
        key = normalize_url(url)
        link = strip_utm(url) if is_web_url(url) else key  # key는 늘 https다
        return cls(id=url_id(key), url=link, key=key, **fields)

    @property
    def is_changelog(self) -> bool:
        """신선도 판정 근거로 쓰는 공식 변경 기록인지."""
        return self.category == "official" and self.kind in ("changelog", "release")


class SourceStat(BaseModel):
    source: str
    ok: bool
    count: int = 0
    error: str | None = None
    newest_at: dt.datetime | None = None


class CollectResult(BaseModel):
    items: list[Item] = Field(default_factory=list)
    stats: list[SourceStat] = Field(default_factory=list)


class Judgment(BaseModel):
    """1단계(Haiku) 분류·채점 결과(FR-SEL-01)."""

    item_id: str
    relevant: bool
    type: ItemType
    importance: int = Field(ge=1, le=5)
    practicality: int = Field(ge=1, le=5)
    event_key: str  # 같은 사건을 묶는 키(FR-DUP-02)


class Candidate(BaseModel):
    """편집 단계로 넘기는 사건 하나. items[0]이 대표(공식 소스 우선)."""

    event_key: str
    items: list[Item]
    judgment: Judgment
    is_update: bool = False


class SourceRef(BaseModel):
    """게재 항목의 출처 링크 하나."""

    title: str
    url: WebUrl
    kind: Category


class CodeBlock(BaseModel):
    lang: str
    code: str


class DeepItem(BaseModel):
    """해설형 게재 항목 하나(D-10 보완, D-13). tip이면 steps·code_blocks·applies_to도 쓴다."""

    kind: Literal["news", "tip"]
    title: str
    background: str
    details: list[str]  # 문단
    how_to_apply: str
    caveats: list[str] = Field(default_factory=list)
    freshness: Freshness
    sources: list[SourceRef] = Field(min_length=1)
    event_key: str
    importance: int = Field(ge=1, le=5)
    practicality: int = Field(ge=1, le=5)
    is_update: bool = False  # 이미 보낸 사건의 후속 소식
    # tip 전용(news면 비워 둔다)
    steps: list[str] = Field(default_factory=list)
    code_blocks: list[CodeBlock] = Field(default_factory=list)
    applies_to: str | None = None  # 도구·버전


class ReleaseLine(BaseModel):
    tool: str
    version: str | None = None
    one_line: str
    url: WebUrl
    event_key: str | None = None  # 없으면 url로 중복을 가린다


class Digest(BaseModel):
    """최종 다이제스트(FR-EDT-02). 구식(outdated) 항목은 검증 단계에서 뺀다."""

    date: dt.date
    summary: str
    items: list[DeepItem] = Field(default_factory=list)
    releases: list[ReleaseLine] = Field(default_factory=list)

    @model_validator(mode="after")
    def _drop_outdated(self) -> Digest:
        self.items = [i for i in self.items if i.freshness != "outdated"]
        return self

    @property
    def is_empty(self) -> bool:
        return not (self.items or self.releases)


class KnowledgeUpdate(BaseModel):
    """knowledge/current.md 갱신안(D-09)."""

    markdown: str
    changes: list[str]
    source_urls: list[str]


class RenderedMail(BaseModel):
    subject: str
    html: str
    text: str
