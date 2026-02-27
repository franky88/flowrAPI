from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class PlanLimits:
    max_accounts: Optional[int]        # None = unlimited
    max_months_history: Optional[int]  # None = unlimited
    max_categories: Optional[int]      # None = unlimited
    can_export: bool
    can_use_api: bool


PLAN_LIMITS: dict[str, PlanLimits] = {
    "free": PlanLimits(
        max_accounts=1,
        max_months_history=3,
        max_categories=10,
        can_export=False,
        can_use_api=False,
    ),
    "pro": PlanLimits(
        max_accounts=None,
        max_months_history=None,
        max_categories=None,
        can_export=True,
        can_use_api=False,
    ),
    "enterprise": PlanLimits(
        max_accounts=None,
        max_months_history=None,
        max_categories=None,
        can_export=True,
        can_use_api=True,
    ),
}


def get_limits(plan: str) -> PlanLimits:
    """Return limits for the given plan slug. Defaults to free if unknown."""
    return PLAN_LIMITS.get(plan, PLAN_LIMITS["free"])