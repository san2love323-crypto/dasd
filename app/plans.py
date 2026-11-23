"""라이센스 플랜(등급) 정의.

플랜은 서버가 사용할 수 있는 봇/웹 기능의 범위를 결정합니다.
`자동충전` 은 스탠다드(중간 등급)부터 열립니다.
"""

from __future__ import annotations

from dataclasses import dataclass

UNLIMITED = -1


@dataclass(frozen=True)
class Plan:
    key: str
    name: str
    tagline: str
    rank: int
    max_products: int
    max_stock_per_product: int
    delivery_types: tuple[str, ...]
    auto_charge: bool
    web_panel: bool
    max_guilds: int
    priority_support: bool

    # ---------------------------------------------------------------- helpers
    @property
    def unlimited_products(self) -> bool:
        return self.max_products == UNLIMITED

    @property
    def unlimited_stock(self) -> bool:
        return self.max_stock_per_product == UNLIMITED

    def allows_delivery(self, delivery_type: str) -> bool:
        return delivery_type in self.delivery_types

    def limit_text(self, limit: int) -> str:
        return "무제한" if limit == UNLIMITED else f"{limit:,}개"

    def features(self) -> dict:
        return {
            "auto_charge": self.auto_charge,
            "web_panel": self.web_panel,
            "role_delivery": "role" in self.delivery_types,
            "content_delivery": "content" in self.delivery_types,
            "manual_delivery": "manual" in self.delivery_types,
            "priority_support": self.priority_support,
        }

    def payload(self) -> dict:
        return {
            "key": self.key,
            "name": self.name,
            "tagline": self.tagline,
            "rank": self.rank,
            "max_products": self.max_products,
            "max_products_text": self.limit_text(self.max_products),
            "max_stock": self.max_stock_per_product,
            "max_stock_text": self.limit_text(self.max_stock_per_product),
            "max_guilds": self.max_guilds,
            "delivery_types": list(self.delivery_types),
            "auto_charge": self.auto_charge,
            "web_panel": self.web_panel,
            "priority_support": self.priority_support,
            "features": self.features(),
        }

    def summary_lines(self) -> list[str]:
        lines = [
            f"상품 {self.limit_text(self.max_products)}",
            f"상품 코드 {self.limit_text(self.max_stock_per_product)}",
            "자동충전 " + ("가능" if self.auto_charge else "불가"),
        ]
        if self.web_panel:
            lines.append("웹 패널 가능")
        labels = {"manual": "수동 전달", "content": "코드 전달", "role": "역할 지급"}
        lines.append("전달 " + " · ".join(labels[t] for t in self.delivery_types))
        return lines


PLANS: dict[str, Plan] = {
    "basic": Plan(
        key="basic",
        name="베이직",
        tagline="상점 오픈용 최소 구성",
        rank=1,
        max_products=10,
        max_stock_per_product=100,
        delivery_types=("manual",),
        auto_charge=False,
        web_panel=False,
        max_guilds=1,
        priority_support=False,
    ),
    "standard": Plan(
        key="standard",
        name="스탠다드",
        tagline="자동충전이 열리는 실전 등급",
        rank=2,
        max_products=30,
        max_stock_per_product=500,
        delivery_types=("manual", "content"),
        auto_charge=True,
        web_panel=True,
        max_guilds=1,
        priority_support=False,
    ),
    "premium": Plan(
        key="premium",
        name="프리미엄",
        tagline="역할 지급까지, 대형 운영",
        rank=3,
        max_products=100,
        max_stock_per_product=3000,
        delivery_types=("manual", "content", "role"),
        auto_charge=True,
        web_panel=True,
        max_guilds=2,
        priority_support=True,
    ),
    "platinum": Plan(
        key="platinum",
        name="플래티넘",
        tagline="무제한 + 멀티 서버",
        rank=4,
        max_products=UNLIMITED,
        max_stock_per_product=UNLIMITED,
        delivery_types=("manual", "content", "role"),
        auto_charge=True,
        web_panel=True,
        max_guilds=5,
        priority_support=True,
    ),
}

# 기존 발급 키/수기 입력 호환
ALIASES = {
    "free": "basic",
    "starter": "basic",
    "lite": "basic",
    "default": "standard",
    "normal": "standard",
    "pro": "premium",
    "plus": "premium",
    "business": "premium",
    "enterprise": "platinum",
    "unlimited": "platinum",
    "vip": "platinum",
}

FALLBACK_PLAN = PLANS["standard"]


def resolve_plan(value: str | None) -> Plan:
    """플랜 키/이름/별칭을 Plan 으로 변환한다. 알 수 없으면 스탠다드."""
    if not value:
        return FALLBACK_PLAN
    raw = str(value).strip().lower()
    if raw in PLANS:
        return PLANS[raw]
    if raw in ALIASES:
        return PLANS[ALIASES[raw]]
    for plan in PLANS.values():
        if raw in {plan.name.lower(), plan.name}:
            return plan
    return FALLBACK_PLAN


def plan_choices() -> list[tuple[str, str]]:
    """(표시명, key) 를 등급 순으로 반환."""
    return [
        (f"{plan.name} · {plan.tagline}", plan.key)
        for plan in sorted(PLANS.values(), key=lambda item: item.rank)
    ]


def public_catalog() -> list[dict]:
    return [plan.payload() for plan in sorted(PLANS.values(), key=lambda item: item.rank)]
