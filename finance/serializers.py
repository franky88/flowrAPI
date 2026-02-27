from decimal import Decimal
from rest_framework import serializers
from django.db.models import Sum
from django.db.models.functions import Coalesce
from finance.models import Account, AccountMonthConfig, Budget, BudgetRuleType, Category, Transaction, TxType
from finance.utils import month_range


class AccountSerializer(serializers.ModelSerializer):
    class Meta:
        model = Account
        fields = ["id", "name", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]


class CategorySerializer(serializers.ModelSerializer):
    children = serializers.SerializerMethodField()

    class Meta:
        model = Category
        fields = [
            "id",
            "name",
            "parent",
            "children",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "children", "created_at", "updated_at"]

    def get_children(self, obj):
        qs = obj.children.order_by("name")
        return CategorySerializer(qs, many=True).data


class TransactionSerializer(serializers.ModelSerializer):
    created_by = serializers.CharField(read_only=True)  # set in view, not by client

    class Meta:
        model = Transaction
        fields = [
            "id",
            "date",
            "type",
            "amount",
            "account",
            "category",
            "note",
            "created_by",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_by", "created_at", "updated_at"]

    def validate_account(self, account):
        """Ensure the account belongs to the same workspace."""
        workspace = self.context.get("workspace")
        if workspace and account.workspace_id != workspace.id:
            raise serializers.ValidationError("Account does not belong to this workspace.")
        return account

    def validate_category(self, category):
        """Ensure the category belongs to the same workspace."""
        workspace = self.context.get("workspace")
        if workspace and category.workspace_id != workspace.id:
            raise serializers.ValidationError("Category does not belong to this workspace.")
        return category


class AccountMonthConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model = AccountMonthConfig
        fields = [
            "id",
            "month",
            "account",
            "income_base",
            "opening_balance",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_account(self, account):
        workspace = self.context.get("workspace")
        if workspace and account.workspace_id != workspace.id:
            raise serializers.ValidationError("Account does not belong to this workspace.")
        return account


class BudgetSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source="category.name", read_only=True)
    resolved_amount = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = Budget
        fields = [
            "id",
            "month",
            "category",
            "category_name",
            "rule_type",
            "value",
            "resolved_amount",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "category_name", "resolved_amount", "created_at", "updated_at"]

    def validate(self, attrs):
        rule_type = attrs.get("rule_type", getattr(self.instance, "rule_type", None))
        value = attrs.get("value", getattr(self.instance, "value", None))

        if rule_type is None or value is None:
            return attrs

        if rule_type == BudgetRuleType.FIXED:
            if value <= 0:
                raise serializers.ValidationError({"value": "Fixed budget value must be greater than 0."})

        if rule_type == BudgetRuleType.PERCENT:
            if value < 0 or value > 100:
                raise serializers.ValidationError({"value": "Percent budget value must be between 0 and 100."})

        return attrs

    def validate_category(self, category):
        workspace = self.context.get("workspace")
        if workspace and category.workspace_id != workspace.id:
            raise serializers.ValidationError("Category does not belong to this workspace.")
        return category

    def get_resolved_amount(self, obj: Budget):
        workspace = self.context.get("workspace")
        if not workspace:
            return None

        if obj.rule_type == BudgetRuleType.FIXED:
            return obj.value

        request = self.context.get("request")
        account_id = (
            request.query_params.get("accountId")
            if request and hasattr(request, "query_params")
            else None
        )

        start, end = month_range(obj.month)

        tx_qs = Transaction.objects.filter(
            workspace=workspace,
            date__gte=start,
            date__lt=end,
            type=TxType.INCOME,
        )
        if account_id:
            tx_qs = tx_qs.filter(account_id=account_id)

        base = tx_qs.aggregate(
            total=Coalesce(Sum("amount"), Decimal("0.00"))
        )["total"] or Decimal("0.00")

        return (Decimal(str(base)) * (obj.value / Decimal("100"))).quantize(Decimal("0.01"))