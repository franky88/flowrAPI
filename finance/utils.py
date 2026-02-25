from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from django.forms import ValidationError


def month_range(month: str):
    """
    month: "YYYY-MM" -> (start_date, end_date) where end_date is exclusive
    """
    try:
        y, m = map(int, month.split("-"))
        start = date(y, m, 1)
        end = date(y + (m == 12), (m % 12) + 1, 1)
        return start, end
    except Exception:
        raise ValidationError('Invalid month format. Use "YYYY-MM".')


def prev_month_yyyymm(month: str) -> str:
    y, m = map(int, month.split("-"))
    if m == 1:
        return f"{y-1}-12"
    return f"{y}-{m-1:02d}"


def q2(d: Decimal) -> Decimal:
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def pct_change(curr: Decimal, prev: Decimal) -> Decimal | None:
    """Returns % change or None if prev is zero (avoid divide-by-zero)."""
    if prev == 0:
        return None
    return q2((curr - prev) / prev * Decimal("100"))