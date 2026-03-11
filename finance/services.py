import calendar
from collections import defaultdict, deque
from datetime import date
from decimal import Decimal
from typing import Optional

from django.conf import settings
import httpx

from finance.models import AccountMonthConfig, Budget, BudgetRuleType, Category, Transaction, TxType, Workspace
from finance.utils import month_range, q2
from django.db.models.functions import Coalesce
from django.db.models import Q, Sum, Case, When, Value, DecimalField


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

def get_intelligence_report(workspace, month: str, account_id: Optional[str] = None) -> dict:
    today = date.today()
    year, mo = int(month[:4]), int(month[5:7])
    days_in_month = calendar.monthrange(year, mo)[1]
    month_start = date(year, mo, 1)
    month_end = date(year, mo, days_in_month)

    # Clamp today to month boundaries
    if today < month_start:
        effective_today = month_start
    elif today > month_end:
        effective_today = month_end
    else:
        effective_today = today

    days_elapsed = (effective_today - month_start).days + 1
    days_remaining = days_in_month - days_elapsed

    # ── MonthConfig: AccountMonthConfig needs workspace + month (+ optional account) ──
    config_qs = AccountMonthConfig.objects.filter(workspace=workspace, month=month)
    if account_id:
        config_qs = config_qs.filter(account_id=account_id)

    # Aggregate across all matching configs (or first if account-scoped)
    opening_balance = Decimal("0")
    income_base = Decimal("0")
    for cfg in config_qs:
        opening_balance += cfg.opening_balance
        income_base += cfg.income_base

    # ── Transactions: filter by date range, not month field ──
    start, end = month_range(month)  # returns (date, date) — uses your existing util
    tx_qs = Transaction.objects.filter(workspace=workspace, date__gte=start, date__lt=end)
    if account_id:
        tx_qs = tx_qs.filter(account_id=account_id)

    money = DecimalField(max_digits=14, decimal_places=2)

    agg = tx_qs.aggregate(
        total_income=Coalesce(
            Sum("amount", filter=Q(type=TxType.INCOME)),  # "INCOME"
            Value(0), output_field=money
        ),
        total_expense=Coalesce(
            Sum("amount", filter=Q(type=TxType.EXPENSE)),  # "EXPENSE"
            Value(0), output_field=money
        ),
    )
    total_income = agg["total_income"]
    total_expense = agg["total_expense"]
    current_balance = opening_balance + total_income - total_expense

    # ── Burn Rate ──
    daily_burn = (total_expense / days_elapsed) if days_elapsed > 0 else Decimal("0")
    days_until_zero = None
    if daily_burn > 0 and current_balance > 0:
        days_until_zero = int(current_balance / daily_burn)

    # ── Forecast Balance ──
    projected_remaining_income = max(income_base - total_income, Decimal("0"))
    projected_additional_expense = daily_burn * days_remaining
    forecast_balance = current_balance + projected_remaining_income - projected_additional_expense

    # ── Budget Risks ──
    budgets = Budget.objects.filter(workspace=workspace, month=month).select_related("category")

    spending_qs = (
        tx_qs.filter(type=TxType.EXPENSE)
        .values("category_id")
        .annotate(spent=Coalesce(Sum("amount"), Value(0), output_field=money))
    )
    spending_map = {str(r["category_id"]): r["spent"] for r in spending_qs}

    budget_risks = []
    for b in budgets:
        monthly_budget = b.value if b.rule_type == BudgetRuleType.FIXED else (b.value / Decimal("100")) * income_base
        spent = spending_map.get(str(b.category_id), Decimal("0"))

        daily_cat_burn = spent / days_elapsed if days_elapsed > 0 else Decimal("0")
        projected_spend = (daily_cat_burn * days_in_month).quantize(Decimal("0.01"))
        projected_overrun = max(projected_spend - monthly_budget, Decimal("0")).quantize(Decimal("0.01"))

        if projected_spend > monthly_budget:
            overrun_pct = (projected_spend - monthly_budget) / monthly_budget * 100 if monthly_budget else Decimal("0")
            risk_level = "critical" if overrun_pct >= 20 else "warning"
        else:
            risk_level = "ok"

        budget_risks.append({
            "categoryId": str(b.category_id),
            "categoryName": b.category.name,
            "monthlyBudget": str(monthly_budget.quantize(Decimal("0.01"))),
            "spent": str(spent),
            "projectedSpend": str(projected_spend),
            "projectedOverrun": str(projected_overrun),
            "riskLevel": risk_level,
            "isCurrentlyExceeded": spent > monthly_budget,
        })

    # ── Income Volatility ──
    volatility = _compute_income_volatility(workspace, month, account_id)

    return {
        "month": month,
        "asOf": str(effective_today),
        "daysElapsed": days_elapsed,
        "daysRemaining": days_remaining,
        "daysInMonth": days_in_month,
        "currentBalance": str(current_balance.quantize(Decimal("0.01"))),
        "forecastBalance": str(forecast_balance.quantize(Decimal("0.01"))),
        "dailyBurnRate": str(daily_burn.quantize(Decimal("0.01"))),
        "daysUntilZero": days_until_zero,
        "budgetRisks": sorted(budget_risks, key=lambda x: x["riskLevel"] == "ok"),
        "incomeVolatility": volatility,
    }


def _compute_income_volatility(workspace, current_month: str, account_id=None) -> dict:
    year, mo = int(current_month[:4]), int(current_month[5:7])

    months = []
    for i in range(1, 4):
        m = mo - i
        y = year
        if m <= 0:
            m += 12
            y -= 1
        months.append(f"{y:04d}-{m:02d}")

    money = DecimalField(max_digits=14, decimal_places=2)
    monthly_incomes = []

    for m in months:
        s, e = month_range(m)
        qs = Transaction.objects.filter(
            workspace=workspace,
            date__gte=s,
            date__lt=e,
            type=TxType.INCOME,
        )
        if account_id:
            qs = qs.filter(account_id=account_id)
        total = qs.aggregate(
            t=Coalesce(Sum("amount"), Value(0), output_field=money)
        )["t"]
        monthly_incomes.append({"month": m, "income": str(total)})

    values = [Decimal(x["income"]) for x in monthly_incomes]
    if len(values) < 2 or all(v == 0 for v in values):
        return {
            "months": monthly_incomes,
            "stdDev": "0.00",
            "cvPercent": "0.00",
            "label": "insufficient_data",
        }

    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    std_dev = variance.sqrt()
    cv = (std_dev / mean * 100) if mean > 0 else Decimal("0")

    label = "stable"
    if cv > 40:
        label = "highly_volatile"
    elif cv > 20:
        label = "volatile"

    return {
        "months": monthly_incomes,
        "stdDev": str(std_dev.quantize(Decimal("0.01"))),
        "cvPercent": str(cv.quantize(Decimal("0.01"))),
        "label": label,
    }