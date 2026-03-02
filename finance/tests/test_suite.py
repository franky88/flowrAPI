# finance/tests/test_suite.py
"""
Full backend test suite.
Run: python manage.py test finance.tests.test_suite -v 2
"""

from decimal import Decimal
from datetime import date
from unittest.mock import patch, MagicMock

from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework import status
from django.db.models import Sum
from django.db.models.functions import Coalesce

from finance.models import (
    Workspace, WorkspaceMember, Account, Category,
    Transaction, Budget, AccountMonthConfig,
    TxType, BudgetRuleType,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

USER_ID = "user_clerk_test_001"
OTHER_USER_ID = "user_clerk_test_002"
BASE_URL = "/finance/v1"  # matches ROOT path('finance/v1/', include('finance.urls'))


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------

def make_workspace(user_id=USER_ID, name="Test Workspace"):
    ws = Workspace.objects.create(name=name)
    WorkspaceMember.objects.create(workspace=ws, user_id=user_id, role="owner")
    return ws


def make_account(workspace, name="Checking"):
    return Account.objects.create(workspace=workspace, name=name)


def make_category(workspace, name="Groceries", parent=None):
    return Category.objects.create(workspace=workspace, name=name, parent=parent)


def make_transaction(workspace, account, category,
                     tx_type=TxType.EXPENSE, amount="500.00", tx_date=None):
    return Transaction.objects.create(
        workspace=workspace,
        account=account,
        category=category,
        type=tx_type,
        amount=Decimal(amount),
        date=tx_date or date(2025, 6, 15),
        created_by=USER_ID,
    )


def make_budget(workspace, category, month="2025-06",
                rule_type=BudgetRuleType.FIXED, value="1000.00"):
    return Budget.objects.create(
        workspace=workspace,
        category=category,
        month=month,
        rule_type=rule_type,
        value=Decimal(value),
    )


def make_month_config(workspace, account, month="2025-06",
                      income_base="50000.00", opening_balance="10000.00"):
    return AccountMonthConfig.objects.create(
        workspace=workspace,
        account=account,
        month=month,
        income_base=Decimal(income_base),
        opening_balance=Decimal(opening_balance),
    )


def _mock_user(user_id=USER_ID):
    user = MagicMock()
    user.id = user_id
    user.is_authenticated = True
    return user


# ---------------------------------------------------------------------------
# Base API test — patches resolve_workspace + PlanEnforcer
# ---------------------------------------------------------------------------

class BaseAPITest(TestCase):
    def setUp(self):
        self.ws = make_workspace()
        self.acc = make_account(self.ws)
        self.cat = make_category(self.ws)
        self.client = APIClient()
        self.client.force_authenticate(user=_mock_user())

        # Patch at the mixin level — covers both views and get_serializer_context
        patcher = patch("finance.views.WorkspaceMixin.get_workspace", return_value=self.ws)
        self.mock_get_workspace = patcher.start()
        self.addCleanup(patcher.stop)

        # Also patch resolve_workspace for APIViews that call it directly
        patcher2 = patch("finance.views.resolve_workspace", return_value=self.ws)
        self.mock_resolve = patcher2.start()
        self.addCleanup(patcher2.stop)

        # Patch enforcer property directly
        enforcer_mock = MagicMock()
        enforcer_mock.check_can_access_month.return_value = None
        enforcer_mock.check_can_create_account.return_value = None
        enforcer_mock.check_can_create_category.return_value = None

        from billing.exceptions import PlanEnforcementMixin
        patcher3 = patch.object(
            PlanEnforcementMixin,
            "enforcer",
            new_callable=lambda: property(lambda self_inner: enforcer_mock),
        )
        self.mock_enforcer = patcher3.start()
        self.addCleanup(patcher3.stop)

    def url(self, path):
        """Build URL using the correct finance/v1 prefix."""
        return f"{BASE_URL}/workspaces/{self.ws.id}/{path}"


# ---------------------------------------------------------------------------
# Model Tests
# ---------------------------------------------------------------------------

class WorkspaceModelTest(TestCase):
    def test_create_workspace_and_member(self):
        ws = make_workspace()
        self.assertEqual(ws.members.count(), 1)
        member = ws.members.first()
        self.assertEqual(member.user_id, USER_ID)
        self.assertEqual(member.role, "owner")

    def test_workspace_unique_member_constraint(self):
        from django.db import IntegrityError
        ws = make_workspace()
        with self.assertRaises(IntegrityError):
            WorkspaceMember.objects.create(workspace=ws, user_id=USER_ID, role="editor")


class AccountModelTest(TestCase):
    def setUp(self):
        self.ws = make_workspace()

    def test_create_account(self):
        acc = make_account(self.ws)
        self.assertEqual(acc.name, "Checking")
        self.assertEqual(acc.workspace, self.ws)

    def test_unique_account_name_per_workspace(self):
        from django.db import IntegrityError
        make_account(self.ws, "Savings")
        with self.assertRaises(IntegrityError):
            make_account(self.ws, "Savings")

    def test_same_name_different_workspace(self):
        ws2 = make_workspace(user_id=OTHER_USER_ID, name="Other WS")
        make_account(self.ws, "Savings")
        acc2 = make_account(ws2, "Savings")
        self.assertIsNotNone(acc2.id)


class CategoryModelTest(TestCase):
    def setUp(self):
        self.ws = make_workspace()

    def test_create_parent_and_child(self):
        parent = make_category(self.ws, "Food")
        child = make_category(self.ws, "Groceries", parent=parent)
        self.assertEqual(child.parent, parent)
        self.assertIn(child, parent.children.all())

    def test_child_deleted_parent_stays(self):
        parent = make_category(self.ws, "Food")
        child = make_category(self.ws, "Groceries", parent=parent)
        child.delete()
        parent.refresh_from_db()
        self.assertEqual(Category.objects.filter(workspace=self.ws).count(), 1)

    def test_parent_set_null_on_parent_delete(self):
        parent = make_category(self.ws, "Food")
        child = make_category(self.ws, "Groceries", parent=parent)
        parent.delete()
        child.refresh_from_db()
        self.assertIsNone(child.parent)


class TransactionModelTest(TestCase):
    def setUp(self):
        self.ws = make_workspace()
        self.acc = make_account(self.ws)
        self.cat = make_category(self.ws)

    def test_create_income_transaction(self):
        tx = make_transaction(self.ws, self.acc, self.cat, TxType.INCOME, "20000.00")
        self.assertEqual(tx.type, TxType.INCOME)
        self.assertEqual(tx.amount, Decimal("20000.00"))

    def test_create_expense_transaction(self):
        tx = make_transaction(self.ws, self.acc, self.cat, TxType.EXPENSE, "500.00")
        self.assertEqual(tx.type, TxType.EXPENSE)

    def test_transaction_belongs_to_workspace(self):
        tx = make_transaction(self.ws, self.acc, self.cat)
        self.assertEqual(tx.workspace, self.ws)


class BudgetModelTest(TestCase):
    def setUp(self):
        self.ws = make_workspace()
        self.cat = make_category(self.ws)

    def test_fixed_budget(self):
        b = make_budget(self.ws, self.cat, rule_type=BudgetRuleType.FIXED, value="900.00")
        self.assertEqual(b.rule_type, BudgetRuleType.FIXED)
        self.assertEqual(b.value, Decimal("900.00"))

    def test_percent_budget(self):
        b = make_budget(self.ws, self.cat, rule_type=BudgetRuleType.PERCENT, value="20.00")
        self.assertEqual(b.rule_type, BudgetRuleType.PERCENT)

    def test_unique_budget_per_category_month(self):
        from django.db import IntegrityError
        make_budget(self.ws, self.cat, month="2025-06")
        with self.assertRaises(IntegrityError):
            make_budget(self.ws, self.cat, month="2025-06")

    def test_same_category_different_months(self):
        make_budget(self.ws, self.cat, month="2025-06")
        b2 = make_budget(self.ws, self.cat, month="2025-07")
        self.assertIsNotNone(b2.id)


class AccountMonthConfigModelTest(TestCase):
    def setUp(self):
        self.ws = make_workspace()
        self.acc = make_account(self.ws)

    def test_create_config(self):
        cfg = make_month_config(self.ws, self.acc)
        self.assertEqual(cfg.income_base, Decimal("50000.00"))
        self.assertEqual(cfg.opening_balance, Decimal("10000.00"))

    def test_unique_per_account_month(self):
        from django.db import IntegrityError
        make_month_config(self.ws, self.acc)
        with self.assertRaises(IntegrityError):
            make_month_config(self.ws, self.acc)


# ---------------------------------------------------------------------------
# Cashflow computation logic tests (no HTTP)
# ---------------------------------------------------------------------------

class CashflowComputationTest(TestCase):
    def setUp(self):
        self.ws = make_workspace()
        self.acc = make_account(self.ws)
        self.cat = make_category(self.ws)
        make_month_config(self.ws, self.acc, month="2025-06",
                          income_base="50000.00", opening_balance="10000.00")

    def _compute(self, month="2025-06"):
        from finance.views import _get_opening_and_income_base
        from finance.utils import month_range

        start, end = month_range(month)  # end is exclusive (first day of next month)
        opening, income_base = _get_opening_and_income_base(
            self.ws, month, str(self.acc.id)
        )
        qs = Transaction.objects.filter(
            workspace=self.ws,
            date__gte=start,
            date__lt=end,          # ← exclusive, matches the view
            account=self.acc,
        )
        income = qs.filter(type=TxType.INCOME).aggregate(
            s=Coalesce(Sum("amount"), Decimal("0"))
        )["s"]
        expense = qs.filter(type=TxType.EXPENSE).aggregate(
            s=Coalesce(Sum("amount"), Decimal("0"))
        )["s"]
        return opening, income_base, income, expense

    def test_no_transactions_returns_opening_balance(self):
        opening, _, income, expense = self._compute()
        self.assertEqual(opening + income - expense, Decimal("10000.00"))

    def test_income_increases_closing_balance(self):
        make_transaction(self.ws, self.acc, self.cat, TxType.INCOME,
                         "20000.00", date(2025, 6, 5))
        opening, _, income, expense = self._compute()
        self.assertEqual(opening + income - expense, Decimal("30000.00"))

    def test_expense_decreases_closing_balance(self):
        make_transaction(self.ws, self.acc, self.cat, TxType.EXPENSE,
                         "3000.00", date(2025, 6, 10))
        opening, _, income, expense = self._compute()
        self.assertEqual(opening + income - expense, Decimal("7000.00"))

    def test_mixed_transactions(self):
        make_transaction(self.ws, self.acc, self.cat, TxType.INCOME,
                         "50000.00", date(2025, 6, 1))
        make_transaction(self.ws, self.acc, self.cat, TxType.EXPENSE,
                         "2000.00", date(2025, 6, 5))
        make_transaction(self.ws, self.acc, self.cat, TxType.EXPENSE,
                         "3000.00", date(2025, 6, 20))
        opening, _, income, expense = self._compute()
        # 10000 + 50000 - 5000 = 55000
        self.assertEqual(opening + income - expense, Decimal("55000.00"))

    def test_transactions_outside_month_excluded(self):
        # These must NOT be counted — the date filter uses month_range
        make_transaction(self.ws, self.acc, self.cat, TxType.INCOME,
                         "99999.00", date(2025, 5, 31))   # May — excluded
        make_transaction(self.ws, self.acc, self.cat, TxType.INCOME,
                         "99999.00", date(2025, 7, 1))    # July — excluded
        opening, _, income, expense = self._compute()
        self.assertEqual(opening + income - expense, Decimal("10000.00"))


# ---------------------------------------------------------------------------
# Budget resolution logic tests (no HTTP)
# ---------------------------------------------------------------------------

class BudgetResolutionTest(TestCase):
    def setUp(self):
        self.ws = make_workspace()
        self.acc = make_account(self.ws)
        self.cat = make_category(self.ws, "Groceries")
        make_month_config(self.ws, self.acc, month="2025-06",
                          income_base="50000.00", opening_balance="0")

    def test_fixed_budget_resolves_exactly(self):
        b = make_budget(self.ws, self.cat, rule_type=BudgetRuleType.FIXED, value="5000.00")
        self.assertEqual(b.value, Decimal("5000.00"))

    def test_percent_budget_resolves_against_income_base(self):
        b = make_budget(self.ws, self.cat, rule_type=BudgetRuleType.PERCENT, value="20.00")
        resolved = (b.value / Decimal("100")) * Decimal("50000.00")
        self.assertEqual(resolved, Decimal("10000.00"))

    def test_percent_budget_10pct(self):
        b = make_budget(self.ws, self.cat, rule_type=BudgetRuleType.PERCENT, value="10.00")
        resolved = (b.value / Decimal("100")) * Decimal("50000.00")
        self.assertEqual(resolved, Decimal("5000.00"))

    def test_budget_remaining_positive(self):
        b = make_budget(self.ws, self.cat, rule_type=BudgetRuleType.FIXED, value="5000.00")
        make_transaction(self.ws, self.acc, self.cat, TxType.EXPENSE,
                         "2000.00", date(2025, 6, 5))
        spent = Transaction.objects.filter(
            workspace=self.ws, category=self.cat,
            type=TxType.EXPENSE, date__startswith="2025-06"
        ).aggregate(s=Coalesce(Sum("amount"), Decimal("0")))["s"]
        remaining = b.value - spent
        self.assertEqual(remaining, Decimal("3000.00"))
        self.assertGreaterEqual(remaining, Decimal("0"))

    def test_budget_exceeded(self):
        b = make_budget(self.ws, self.cat, rule_type=BudgetRuleType.FIXED, value="1000.00")
        make_transaction(self.ws, self.acc, self.cat, TxType.EXPENSE,
                         "1500.00", date(2025, 6, 5))
        spent = Transaction.objects.filter(
            workspace=self.ws, category=self.cat,
            type=TxType.EXPENSE, date__startswith="2025-06"
        ).aggregate(s=Coalesce(Sum("amount"), Decimal("0")))["s"]
        self.assertLess(b.value - spent, Decimal("0"))


# ---------------------------------------------------------------------------
# API — Accounts
# ---------------------------------------------------------------------------

class AccountViewSetTest(BaseAPITest):
    def test_list_accounts(self):
        res = self.client.get(self.url("accounts/"))
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        names = [a["name"] for a in res.data]
        self.assertIn("Checking", names)

    def test_create_account(self):
        res = self.client.post(self.url("accounts/"), {"name": "Savings"})
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data["name"], "Savings")

    def test_create_duplicate_account_fails(self):
        self.client.post(self.url("accounts/"), {"name": "Savings"})
        res = self.client.post(self.url("accounts/"), {"name": "Savings"})
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_delete_account(self):
        acc2 = make_account(self.ws, "ToDelete")
        res = self.client.delete(self.url(f"accounts/{acc2.id}/"))
        self.assertEqual(res.status_code, status.HTTP_204_NO_CONTENT)

    def test_update_account_name(self):
        res = self.client.patch(self.url(f"accounts/{self.acc.id}/"), {"name": "Updated"})
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data["name"], "Updated")


# ---------------------------------------------------------------------------
# API — Categories
# ---------------------------------------------------------------------------

class CategoryViewSetTest(BaseAPITest):
    def test_list_categories(self):
        res = self.client.get(self.url("categories/"))
        self.assertEqual(res.status_code, status.HTTP_200_OK)

    def test_create_top_level_category(self):
        res = self.client.post(self.url("categories/"), {"name": "Transport"})
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)

    def test_create_child_category(self):
        parent = make_category(self.ws, "Food")
        res = self.client.post(self.url("categories/"), {
            "name": "Dining Out",
            "parent": str(parent.id),
        })
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(str(res.data["parent"]), str(parent.id))

    def test_category_response_includes_children(self):
        parent = make_category(self.ws, "Food")
        make_category(self.ws, "Dining Out", parent=parent)
        res = self.client.get(self.url(f"categories/{parent.id}/"))
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res.data["children"]), 1)
        self.assertEqual(res.data["children"][0]["name"], "Dining Out")


# ---------------------------------------------------------------------------
# API — Transactions
# ---------------------------------------------------------------------------

class TransactionViewSetTest(BaseAPITest):
    def _payload(self, **overrides):
        p = {
            "date": "2025-06-10",
            "type": "EXPENSE",
            "amount": "1500.00",
            "account": str(self.acc.id),
            "category": str(self.cat.id),
            "note": "Test",
        }
        p.update(overrides)
        return p

    def test_create_expense(self):
        res = self.client.post(self.url("transactions/"), self._payload())
        print(res.data)
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data["amount"], "1500.00")
        self.assertEqual(res.data["type"], "EXPENSE")

    def test_create_income(self):
        res = self.client.post(self.url("transactions/"),
                               self._payload(type="INCOME", amount="50000.00"))
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data["type"], "INCOME")

    def test_list_transactions(self):
        make_transaction(self.ws, self.acc, self.cat)
        make_transaction(self.ws, self.acc, self.cat)
        res = self.client.get(self.url("transactions/"))
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(res.data), 2)

    def test_filter_by_month(self):
        make_transaction(self.ws, self.acc, self.cat, tx_date=date(2025, 6, 15))
        make_transaction(self.ws, self.acc, self.cat, tx_date=date(2025, 7, 1))
        res = self.client.get(self.url("transactions/") + "?month=2025-06")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        for tx in res.data:
            self.assertTrue(tx["date"].startswith("2025-06"))

    def test_update_transaction(self):
        tx = make_transaction(self.ws, self.acc, self.cat)
        res = self.client.patch(self.url(f"transactions/{tx.id}/"), {"amount": "999.00"})
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data["amount"], "999.00")

    def test_delete_transaction(self):
        tx = make_transaction(self.ws, self.acc, self.cat)
        res = self.client.delete(self.url(f"transactions/{tx.id}/"))
        self.assertEqual(res.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Transaction.objects.filter(id=tx.id).exists())

    def test_account_must_belong_to_workspace(self):
        ws2 = make_workspace(user_id=OTHER_USER_ID, name="Foreign WS")
        foreign_acc = make_account(ws2, "Foreign Account")
        res = self.client.post(self.url("transactions/"),
                            self._payload(account=str(foreign_acc.id)))
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_invalid_amount_rejected(self):
        res = self.client.post(self.url("transactions/"), self._payload(amount="abc"))
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)


# ---------------------------------------------------------------------------
# API — Budgets
# ---------------------------------------------------------------------------

class BudgetViewSetTest(BaseAPITest):
    def test_create_fixed_budget(self):
        res = self.client.post(self.url("budgets/"), {
            "month": "2025-06",
            "category": str(self.cat.id),
            "rule_type": "fixed",
            "value": "5000.00",
        })
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data["rule_type"], "fixed")

    def test_create_percent_budget(self):
        res = self.client.post(self.url("budgets/"), {
            "month": "2025-06",
            "category": str(self.cat.id),
            "rule_type": "percent",
            "value": "20.00",
        })
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data["rule_type"], "percent")

    def test_duplicate_budget_rejected(self):
        make_budget(self.ws, self.cat)
        res = self.client.post(self.url("budgets/"), {
            "month": "2025-06",
            "category": str(self.cat.id),
            "rule_type": "fixed",
            "value": "3000.00",
        })
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_copy_budgets_to_next_month(self):
        make_budget(self.ws, self.cat, month="2025-06", value="5000.00")
        res = self.client.post(self.url("budgets/copy-to-next-month/"), {"month": "2025-06"})
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data["nextMonth"], "2025-07")
        self.assertEqual(res.data["createdCount"], 1)
        self.assertTrue(Budget.objects.filter(
            workspace=self.ws, month="2025-07", category=self.cat
        ).exists())

    def test_copy_skips_existing(self):
        make_budget(self.ws, self.cat, month="2025-06", value="5000.00")
        make_budget(self.ws, self.cat, month="2025-07", value="4000.00")
        res = self.client.post(self.url("budgets/copy-to-next-month/"), {"month": "2025-06"})
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data["createdCount"], 0)
        self.assertEqual(res.data["skippedCount"], 1)
        # Existing value must NOT be overwritten
        b = Budget.objects.get(workspace=self.ws, month="2025-07", category=self.cat)
        self.assertEqual(b.value, Decimal("4000.00"))


# ---------------------------------------------------------------------------
# API — AccountMonthConfig
# ---------------------------------------------------------------------------

class AccountMonthConfigViewTest(BaseAPITest):
    def test_get_config(self):
        make_month_config(self.ws, self.acc)
        res = self.client.get(
            f"{BASE_URL}/workspaces/{self.ws.id}/config/",
            {"month": "2025-06", "accountId": str(self.acc.id)},
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data["income_base"], "50000.00")

    def test_get_config_returns_null_when_missing(self):
        res = self.client.get(
            f"{BASE_URL}/workspaces/{self.ws.id}/config/",
            {"month": "2099-01", "accountId": str(self.acc.id)},
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertIsNone(res.data)

    def test_upsert_config(self):
        res = self.client.put(
            f"{BASE_URL}/workspaces/{self.ws.id}/config/",
            {
                "month": "2025-06",
                "accountId": str(self.acc.id),
                "income_base": "60000.00",
                "opening_balance": "5000.00",
            },
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        cfg = AccountMonthConfig.objects.get(
            workspace=self.ws, account=self.acc, month="2025-06"
        )
        self.assertEqual(cfg.income_base, Decimal("60000.00"))

    def test_upsert_config_updates_existing(self):
        make_month_config(self.ws, self.acc, income_base="50000.00")
        res = self.client.put(
            f"{BASE_URL}/workspaces/{self.ws.id}/config/",
            {
                "month": "2025-06",
                "accountId": str(self.acc.id),
                "income_base": "75000.00",
                "opening_balance": "0.00",
            },
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        cfg = AccountMonthConfig.objects.get(
            workspace=self.ws, account=self.acc, month="2025-06"
        )
        self.assertEqual(cfg.income_base, Decimal("75000.00"))


# ---------------------------------------------------------------------------
# Report — Cashflow
# ---------------------------------------------------------------------------

class CashflowReportViewTest(BaseAPITest):
    def setUp(self):
        super().setUp()
        make_month_config(self.ws, self.acc, month="2025-06",
                          income_base="50000.00", opening_balance="10000.00")

    def _get(self, **params):
        params.setdefault("month", "2025-06")
        params.setdefault("accountId", str(self.acc.id))
        return self.client.get(
            f"{BASE_URL}/workspaces/{self.ws.id}/reports/cashflow/", params
        )

    def test_returns_200(self):
        self.assertEqual(self._get().status_code, status.HTTP_200_OK)

    def test_correct_structure(self):
        res = self._get()
        for key in ("month", "openingBalance", "closingBalance", "days"):
            self.assertIn(key, res.data)

    def test_opening_balance_from_config(self):
        self.assertEqual(self._get().data["openingBalance"], "10000.00")

    def test_closing_balance_with_no_transactions(self):
        self.assertEqual(self._get().data["closingBalance"], "10000.00")

    def test_closing_balance_with_income(self):
        make_transaction(self.ws, self.acc, self.cat, TxType.INCOME,
                         "20000.00", date(2025, 6, 1))
        self.assertEqual(self._get().data["closingBalance"], "30000.00")

    def test_closing_balance_with_expense(self):
        make_transaction(self.ws, self.acc, self.cat, TxType.EXPENSE,
                         "2000.00", date(2025, 6, 15))
        self.assertEqual(self._get().data["closingBalance"], "8000.00")

    def test_daily_rows_present_for_month(self):
        # The view only returns days that HAVE transactions — not all 30 days
        make_transaction(self.ws, self.acc, self.cat, TxType.INCOME,
                         "1000.00", date(2025, 6, 5))
        make_transaction(self.ws, self.acc, self.cat, TxType.EXPENSE,
                         "200.00", date(2025, 6, 10))
        res = self._get()
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res.data["days"]), 2)  # June = 30 days

    def test_daily_row_shape(self):
        make_transaction(self.ws, self.acc, self.cat, TxType.INCOME,
                         "1000.00", date(2025, 6, 5))
        day = self._get().data["days"][0]
        for key in ("date", "income", "expense", "net", "balance"):
            self.assertIn(key, day)

    def test_running_balance_is_cumulative(self):
        make_transaction(self.ws, self.acc, self.cat, TxType.INCOME,
                         "1000.00", date(2025, 6, 1))
        make_transaction(self.ws, self.acc, self.cat, TxType.EXPENSE,
                         "500.00", date(2025, 6, 2))
        days = {d["date"]: d for d in self._get().data["days"]}
        self.assertEqual(days["2025-06-01"]["balance"], "11000.00")
        self.assertEqual(days["2025-06-02"]["balance"], "10500.00")

    def test_month_required(self):
        res = self.client.get(
            f"{BASE_URL}/workspaces/{self.ws.id}/reports/cashflow/",
            {"accountId": str(self.acc.id)},
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)


# ---------------------------------------------------------------------------
# Report — Budget Monitor
# ---------------------------------------------------------------------------

class BudgetMonitorViewTest(BaseAPITest):
    def setUp(self):
        super().setUp()
        make_month_config(self.ws, self.acc, month="2025-06",
                          income_base="50000.00", opening_balance="0")
        self.budget = make_budget(self.ws, self.cat, month="2025-06",
                                  rule_type=BudgetRuleType.FIXED, value="5000.00")

    def _get(self, **params):
        params.setdefault("month", "2025-06")
        params.setdefault("accountId", str(self.acc.id))
        return self.client.get(
            f"{BASE_URL}/workspaces/{self.ws.id}/reports/budget-monitor/", params
        )

    def test_returns_200(self):
        self.assertEqual(self._get().status_code, status.HTTP_200_OK)

    def test_budget_row_present(self):
        res = self._get()
        cat_ids = [r["categoryId"] for r in res.data["rows"]]
        self.assertIn(str(self.cat.id), cat_ids)

    def test_budget_not_exceeded_when_under(self):
        make_transaction(self.ws, self.acc, self.cat, TxType.EXPENSE,
                         "2000.00", date(2025, 6, 10))
        res = self._get()
        row = next(r for r in res.data["rows"] if r["categoryId"] == str(self.cat.id))
        self.assertEqual(row["spent"], "2000.00")
        self.assertFalse(row["isExceeded"])

    def test_budget_exceeded_when_over(self):
        make_transaction(self.ws, self.acc, self.cat, TxType.EXPENSE,
                         "6000.00", date(2025, 6, 10))
        res = self._get()
        row = next(r for r in res.data["rows"] if r["categoryId"] == str(self.cat.id))
        self.assertTrue(row["isExceeded"])

    def test_percent_budget_resolves_correctly(self):
        cat2 = make_category(self.ws, "Transport")
        make_budget(self.ws, cat2, month="2025-06",
                    rule_type=BudgetRuleType.PERCENT, value="10.00")  # 10% of 50000 = 5000
        make_transaction(self.ws, self.acc, cat2, TxType.EXPENSE,
                         "4000.00", date(2025, 6, 5))
        res = self._get()
        row = next(r for r in res.data["rows"] if r["categoryId"] == str(cat2.id))
        self.assertEqual(row["budgetResolved"], "5000.00")
        self.assertFalse(row["isExceeded"])


# ---------------------------------------------------------------------------
# Report — Dashboard
# ---------------------------------------------------------------------------

class DashboardReportViewTest(BaseAPITest):
    def setUp(self):
        super().setUp()
        make_month_config(self.ws, self.acc, month="2025-06",
                          income_base="50000.00", opening_balance="10000.00")

    def _get(self, **params):
        params.setdefault("month", "2025-06")
        params.setdefault("accountId", str(self.acc.id))
        return self.client.get(
            f"{BASE_URL}/workspaces/{self.ws.id}/reports/dashboard/", params
        )

    def test_returns_200(self):
        self.assertEqual(self._get().status_code, status.HTTP_200_OK)

    def test_response_shape(self):
        res = self._get()
        for key in ("kpis", "budgets", "recentTransactions"):
            self.assertIn(key, res.data)

    def test_kpis_income_expense_net(self):
        make_transaction(self.ws, self.acc, self.cat, TxType.INCOME,
                         "50000.00", date(2025, 6, 1))
        make_transaction(self.ws, self.acc, self.cat, TxType.EXPENSE,
                         "5000.00", date(2025, 6, 10))
        kpis = self._get().data["kpis"]
        self.assertEqual(kpis["income"], "50000.00")
        self.assertEqual(kpis["expense"], "5000.00")
        self.assertEqual(kpis["net"], "45000.00")

    def test_recent_transactions_limit(self):
        for i in range(15):
            make_transaction(self.ws, self.acc, self.cat, tx_date=date(2025, 6, i + 1))
        self.assertLessEqual(len(self._get().data["recentTransactions"]), 10)