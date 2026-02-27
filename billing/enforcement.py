from datetime import datetime
from django.db.models import Count
from billing.models import Subscription
from billing.plans import get_limits
from billing.exceptions import PlanLimitExceeded


class PlanEnforcer:
    """
    Central enforcement service. Instantiate with a user_id, then call
    check_* methods before any write operation.

    All checks raise PlanLimitExceeded on failure.
    """

    def __init__(self, user_id: str):
        self.user_id = user_id
        self._subscription: Subscription | None = None

    @property
    def subscription(self) -> Subscription:
        if self._subscription is None:
            self._subscription, _ = Subscription.objects.get_or_create(
                user_id=self.user_id,
                defaults={"plan": "free", "status": "active"},
            )
        return self._subscription

    @property
    def limits(self):
        return get_limits(self.subscription.effective_plan)

    # ------------------------------------------------------------------
    # Checks
    # ------------------------------------------------------------------

    def check_can_create_account(self) -> None:
        if self.limits.max_accounts is None:
            return
        from finance.models import Account, Workspace, WorkspaceMember
        count = (
            Account.objects
            .filter(workspace__members__user_id=self.user_id)
            .distinct()
            .count()
        )
        if count >= self.limits.max_accounts:
            raise PlanLimitExceeded(
                message=(
                    f"Your plan allows a maximum of {self.limits.max_accounts} account(s). "
                    f"Upgrade to Pro to add more."
                ),
                limit_key="max_accounts",
            )

    def check_can_create_category(self) -> None:
        if self.limits.max_categories is None:
            return
        from finance.models import Category
        count = (
            Category.objects
            .filter(workspace__members__user_id=self.user_id)
            .distinct()
            .count()
        )
        if count >= self.limits.max_categories:
            raise PlanLimitExceeded(
                message=(
                    f"Your plan allows a maximum of {self.limits.max_categories} categories. "
                    f"Upgrade to Pro to add more."
                ),
                limit_key="max_categories",
            )

    def check_can_access_month(self, month: str) -> None:
        """
        month: 'YYYY-MM'
        Blocks access to months outside the rolling history window.
        """
        if self.limits.max_months_history is None:
            return

        now = datetime.now()
        current_yyyymm = now.strftime("%Y-%m")

        # Parse both as (year, month) tuples for arithmetic
        cy, cm = map(int, current_yyyymm.split("-"))
        ty, tm = map(int, month.split("-"))

        # How many months in the past is the target month?
        months_ago = (cy - ty) * 12 + (cm - tm)

        # Future months are always allowed
        if months_ago < 0:
            return

        if months_ago >= self.limits.max_months_history:
            raise PlanLimitExceeded(
                message=(
                    f"Your plan only allows access to the last "
                    f"{self.limits.max_months_history} months of history. "
                    f"Upgrade to Pro for full history."
                ),
                limit_key="max_months_history",
            )

    def check_can_export(self) -> None:
        if not self.limits.can_export:
            raise PlanLimitExceeded(
                message="Exporting data is not available on your current plan. Upgrade to Pro.",
                limit_key="can_export",
            )

    def check_can_use_api(self) -> None:
        if not self.limits.can_use_api:
            raise PlanLimitExceeded(
                message="API access is not available on your current plan. Upgrade to Enterprise.",
                limit_key="can_use_api",
            )

    # ------------------------------------------------------------------
    # Usage summary (for GET /subscription/)
    # ------------------------------------------------------------------

    def get_usage_summary(self) -> dict:
        from finance.models import Account, Category

        account_count = (
            Account.objects
            .filter(workspace__members__user_id=self.user_id)
            .distinct()
            .count()
        )
        category_count = (
            Category.objects
            .filter(workspace__members__user_id=self.user_id)
            .distinct()
            .count()
        )

        lim = self.limits

        return {
            "accounts": {
                "used": account_count,
                "limit": lim.max_accounts,
            },
            "categories": {
                "used": category_count,
                "limit": lim.max_categories,
            },
            "months_history": {
                "limit": lim.max_months_history,
            },
            "features": {
                "can_export": lim.can_export,
                "can_use_api": lim.can_use_api,
            },
        }