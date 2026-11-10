"""Claude 비용 추적과 하루 상한(NFR-01, D-07)."""

from __future__ import annotations

from dataclasses import dataclass, field

# USD / 백만 토큰 (입력, 출력). 출처: 결정기록 D-06(2026-11-11 확인).
PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
}
CACHE_WRITE_FACTOR = 1.25
CACHE_READ_FACTOR = 0.10
# 가격표에 없는 모델은 가장 비싼 값으로 계산한다(상한을 늦게 막는 쪽보다 일찍 막는 쪽이 안전).
_FALLBACK_PRICE = max(PRICES.values())
# 호출 전 사전 추정용: 문자 수 / 3 ≈ 토큰 수(한국어·영어 섞인 입력의 보수적 근사)
CHARS_PER_TOKEN_ESTIMATE = 3


class BudgetExceeded(Exception):
    pass


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


def price_of(model: str, usage: Usage) -> float:
    in_price, out_price = PRICES.get(model, _FALLBACK_PRICE)
    input_cost = (
        usage.input_tokens
        + usage.cache_creation_input_tokens * CACHE_WRITE_FACTOR
        + usage.cache_read_input_tokens * CACHE_READ_FACTOR
    ) * in_price
    return (input_cost + usage.output_tokens * out_price) / 1_000_000


@dataclass
class CostTracker:
    budget_usd: float
    spent_before: float = 0.0  # 같은 KST 날짜의 앞선 실행에서 쓴 금액
    run_cost: float = 0.0
    tokens: dict[str, int] = field(default_factory=dict)

    @property
    def spent(self) -> float:
        return self.spent_before + self.run_cost

    @property
    def remaining(self) -> float:
        return self.budget_usd - self.spent

    def record(self, model: str, usage: Usage) -> float:
        cost = price_of(model, usage)
        self.run_cost += cost
        for key, n in (
            (f"{model}:input", usage.input_tokens + usage.cache_creation_input_tokens),
            (f"{model}:cache_read", usage.cache_read_input_tokens),
            (f"{model}:output", usage.output_tokens),
        ):
            if n:
                self.tokens[key] = self.tokens.get(key, 0) + n
        return cost

    @staticmethod
    def estimate(model: str, input_tokens: int, output_tokens: int) -> float:
        return price_of(model, Usage(input_tokens=input_tokens, output_tokens=output_tokens))

    def can_afford(self, estimate_usd: float) -> bool:
        return estimate_usd <= self.remaining

    def check(self, estimate_usd: float) -> None:
        if not self.can_afford(estimate_usd):
            raise BudgetExceeded(
                f"하루 상한 ${self.budget_usd:.2f} 초과 예상: 사용 ${self.spent:.4f}"
                f" + 예상 ${estimate_usd:.4f}"
            )
