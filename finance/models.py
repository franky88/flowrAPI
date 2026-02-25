from __future__ import annotations

from django.db import models
from django.core.validators import RegexValidator
from django.utils import timezone
import uuid


MONTH_VALIDATOR = RegexValidator(
    regex=r"^\d{4}-\d{2}$",
    message='Month must be in format "YYYY-MM" (e.g. "2026-02").',
)


class TxType(models.TextChoices):
    INCOME = "INCOME", "Income"
    EXPENSE = "EXPENSE", "Expense"


class BudgetRuleType(models.TextChoices):
    FIXED = "fixed", "Fixed"
    PERCENT = "percent", "Percent"


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

class WorkspaceRole(models.TextChoices):
    OWNER = "owner", "Owner"
    EDITOR = "editor", "Editor"
    VIEWER = "viewer", "Viewer"


class Workspace(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=120)

    def __str__(self) -> str:
        return self.name


class WorkspaceMember(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        Workspace,
        on_delete=models.CASCADE,
        related_name="members",
    )
    user_id = models.CharField(max_length=255, db_index=True)  # Clerk userId
    role = models.CharField(max_length=10, choices=WorkspaceRole.choices, default=WorkspaceRole.EDITOR)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "user_id"],
                name="uniq_workspace_member",
            ),
        ]
        indexes = [
            models.Index(fields=["user_id"], name="idx_workspace_member_user"),
        ]

    def __str__(self) -> str:
        return f"{self.user_id} → {self.workspace} ({self.role})"


class Account(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="accounts")
    name = models.CharField(max_length=120)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["workspace", "name"], name="uniq_account_workspace_name"),
        ]
        indexes = [
            models.Index(fields=["workspace"], name="idx_account_workspace"),
        ]

    def __str__(self) -> str:
        return f"{self.name}"


class Category(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="categories")
    name = models.CharField(max_length=120)
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="children")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["workspace", "parent", "name"], name="uniq_category_workspace_parent_name"),
        ]
        verbose_name_plural = "Categories"
        indexes = [
            models.Index(fields=["workspace"], name="idx_category_workspace"),
        ]

    def __str__(self) -> str:
        return self.name


class Budget(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="budgets")
    month = models.CharField(max_length=7, validators=[MONTH_VALIDATOR])
    category = models.ForeignKey(Category, on_delete=models.CASCADE, related_name="budgets")
    rule_type = models.CharField(max_length=10, choices=BudgetRuleType.choices)
    value = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["workspace", "month", "category"], name="uniq_budget_workspace_month_category"),
        ]
        indexes = [
            models.Index(fields=["workspace", "month"], name="idx_budget_workspace_month"),
        ]

    def __str__(self) -> str:
        return f"{self.month} - {self.category_id} ({self.rule_type})"


class Transaction(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="transactions")
    created_by = models.CharField(max_length=255)
    date = models.DateField(db_index=True)
    type = models.CharField(max_length=10, choices=TxType.choices)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="transactions")
    category = models.ForeignKey(Category, on_delete=models.CASCADE, related_name="transactions")
    note = models.TextField(blank=True, null=True)

    class Meta:
        indexes = [
            models.Index(fields=["workspace", "date"], name="idx_tx_workspace_date"),
            models.Index(fields=["workspace", "category"], name="idx_tx_workspace_category"),
            models.Index(fields=["workspace", "account"], name="idx_tx_workspace_account"),
        ]

    def __str__(self) -> str:
        return f"{self.date} {self.type} {self.amount}"

class AccountMonthConfig(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="account_month_configs")
    month = models.CharField(max_length=7, validators=[MONTH_VALIDATOR])
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="month_configs")
    income_base = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    opening_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["workspace", "month", "account"], name="uniq_amc_workspace_month_account"),
        ]
        indexes = [
            models.Index(fields=["workspace", "month"], name="idx_amc_workspace_month"),
        ]

