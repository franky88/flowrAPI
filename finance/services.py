from collections import defaultdict, deque
from datetime import date
from decimal import Decimal

from django.conf import settings
import httpx

from finance.models import AccountMonthConfig, Budget, BudgetRuleType, Category, Transaction, TxType, Workspace
from finance.utils import q2
from django.db.models.functions import Coalesce
from django.db.models import Sum, Case, When, Value, DecimalField


def resolve_budget_amount(rule_type: str, value: Decimal, income_base: Decimal) -> Decimal:
    if rule_type == "fixed":
        return q2(value)
    return q2(value / Decimal("100") * income_base)


def build_descendants_map(workspace: Workspace) -> dict:
    cats = Category.objects.filter(workspace=workspace).values("id", "parent_id")
    children: dict = defaultdict(list)
    all_ids = []

    for c in cats:
        cid = str(c["id"])
        pid = str(c["parent_id"]) if c["parent_id"] else None
        all_ids.append(cid)
        children[pid].append(cid)

    descendants = {}
    for cid in all_ids:
        seen = {cid}
        q = deque(children.get(cid, []))
        while q:
            x = q.popleft()
            if x in seen:
                continue
            seen.add(x)
            q.extend(children.get(x, []))
        descendants[cid] = seen

    return descendants


def _get_opening_and_income_base(
    workspace: Workspace,
    month: str,
    account_id: str | None,
) -> tuple[Decimal, Decimal]:
    if account_id:
        cfg = AccountMonthConfig.objects.filter(
            workspace=workspace,
            month=month,
            account_id=account_id,
        ).first()
        opening = cfg.opening_balance if cfg else Decimal("0.00")
        income_base = cfg.income_base if cfg else Decimal("0.00")
    else:
        agg = AccountMonthConfig.objects.filter(
            workspace=workspace,
            month=month,
        ).aggregate(
            total_opening=Coalesce(Sum("opening_balance"), Decimal("0.00")),
            total_income_base=Coalesce(Sum("income_base"), Decimal("0.00")),
        )
        opening = agg["total_opening"] or Decimal("0.00")
        income_base = agg["total_income_base"] or Decimal("0.00")

    return opening, income_base


def compute_kpis_for_range(
    workspace: Workspace,
    start: date,
    end: date,
    account_id: str | None = None,
) -> tuple[Decimal, Decimal, Decimal]:
    tx_qs = Transaction.objects.filter(
        workspace=workspace,
        date__gte=start,
        date__lt=end,
    )
    if account_id:
        tx_qs = tx_qs.filter(account_id=account_id)

    money = DecimalField(max_digits=12, decimal_places=2)

    totals = tx_qs.aggregate(
        income=Coalesce(
            Sum(Case(When(type=TxType.INCOME, then="amount"), default=Value(0), output_field=money)),
            Value(0),
            output_field=money,
        ),
        expense=Coalesce(
            Sum(Case(When(type=TxType.EXPENSE, then="amount"), default=Value(0), output_field=money)),
            Value(0),
            output_field=money,
        ),
    )

    income = totals["income"] or Decimal("0.00")
    expense = totals["expense"] or Decimal("0.00")
    return q2(income), q2(expense), q2(income - expense)


def compute_budget_rows(
    workspace: Workspace,
    month: str,
    expense_qs,
    mode: str,
    income_base: Decimal,
) -> tuple[list, Decimal, Decimal, dict]:
    money = DecimalField(max_digits=12, decimal_places=2)

    budgets = (
        Budget.objects
        .filter(workspace=workspace, month=month)
        .select_related("category")
        .order_by("category__name")
    )

    base_spent_by_cat: dict[str, Decimal] = {
        str(r["category"]): (r["spent"] or Decimal("0.00"))
        for r in expense_qs.values("category").annotate(
            spent=Coalesce(Sum("amount"), Value(0), output_field=money)
        )
    }

    descendants = build_descendants_map(workspace) if mode == "rollup" else {}

    rows = []
    total_budget = Decimal("0.00")
    total_spent = Decimal("0.00")

    for b in budgets:
        cat_id = str(b.category_id)
        budget_amt = resolve_budget_amount(b.rule_type, b.value, income_base)

        if mode == "leaf":
            spent = base_spent_by_cat.get(cat_id, Decimal("0.00"))
        else:
            subtree = descendants.get(cat_id, {cat_id})
            spent = sum(
                (base_spent_by_cat.get(x, Decimal("0.00")) for x in subtree),
                Decimal("0.00"),
            )

        remaining = budget_amt - spent
        total_budget += budget_amt
        total_spent += spent

        rows.append({
            "categoryId": cat_id,
            "categoryName": b.category.name,
            "ruleType": b.rule_type,
            "value": str(b.value),
            "budgetResolved": f"{budget_amt:.2f}",
            "spent": f"{spent:.2f}",
            "remaining": f"{remaining:.2f}",
            "isExceeded": remaining < Decimal("0.00"),
        })

    rows.sort(key=lambda r: Decimal(r["remaining"]))

    percent_budgets = [b for b in budgets if b.rule_type == BudgetRuleType.PERCENT]
    allocated_pct = sum((b.value for b in percent_budgets), Decimal("0.00"))
    remaining_pct = Decimal("100.00") - allocated_pct

    percent_summary = {
        "allocatedPercent": f"{allocated_pct:.2f}",
        "remainingPercent": f"{remaining_pct:.2f}",
        "isOverAllocated": remaining_pct < Decimal("0.00"),
    }

    return rows, total_budget, total_spent, percent_summary


def get_percent_budget_summary(workspace: Workspace, month: str) -> dict:
    budgets = Budget.objects.filter(
        workspace=workspace,
        month=month,
        rule_type=BudgetRuleType.PERCENT,
    )

    allocated = sum(b.value for b in budgets) if budgets.exists() else Decimal("0")
    remaining = Decimal("100") - allocated

    return {
        "allocated_percent": allocated,
        "remaining_percent": remaining,
        "is_over_allocated": remaining < 0,
    }


def get_clerk_user(user_id: str) -> dict | None:
    """
    Fetch a Clerk user by userId.
    Returns dict with firstName, lastName, emailAddresses, imageUrl, etc.
    """
    response = httpx.get(
        f"https://api.clerk.com/v1/users/{user_id}",
        headers={"Authorization": f"Bearer {settings.CLERK_SECRET_KEY}"},
    )
    if response.status_code == 200:
        return response.json()
    return None