from decimal import Decimal

from datetime import date, datetime

from django.db.models import Sum, Case, When, Value, DecimalField
from django.db.models.functions import Coalesce

from rest_framework import viewsets
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import ValidationError, PermissionDenied, NotFound

from billing.exceptions import PlanEnforcementMixin
from finance.models import (
    Account, AccountMonthConfig, Budget, BudgetRuleType, Category,
    Transaction, TxType, Workspace, WorkspaceMember, WorkspaceRole
)
from finance.serializers import (
    AccountMonthConfigSerializer, AccountSerializer, BudgetSerializer,
    CategorySerializer, TransactionSerializer
)
from finance.services import _get_opening_and_income_base, compute_budget_rows, compute_kpis_for_range, get_clerk_user
from finance.utils import month_range, pct_change, prev_month_yyyymm, q2
from rest_framework.decorators import action
from dateutil.relativedelta import relativedelta


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def resolve_workspace(user_id: str, workspace_id: str, require_editor: bool = False) -> Workspace:
    """
    Validates membership and returns the Workspace.
    Raises NotFound if workspace doesn't exist, PermissionDenied if not a member.
    """
    try:
        member = WorkspaceMember.objects.select_related("workspace").get(
            workspace_id=workspace_id,
            user_id=user_id,
        )
    except WorkspaceMember.DoesNotExist:
        raise NotFound("Workspace not found.")

    if require_editor and member.role == WorkspaceRole.VIEWER:
        raise PermissionDenied("Viewers cannot modify data.")

    return member.workspace


class WorkspaceMixin:
    """
    Mixin for all workspace-scoped views.
    Resolves workspace from URL kwarg and injects it into serializer context.
    Expects URL pattern: /v1/workspaces/<workspace_id>/...
    """

    def get_workspace(self, require_editor: bool = False) -> Workspace:
        workspace_id = self.kwargs.get("workspace_id")
        if not workspace_id:
            raise ValidationError("workspace_id is required in the URL.")
        return resolve_workspace(self.request.user.id, workspace_id, require_editor)

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["workspace"] = self.get_workspace()
        return context


# ---------------------------------------------------------------------------
# Workspace Management
# ---------------------------------------------------------------------------

class WhoAmIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        memberships = (
            WorkspaceMember.objects
            .filter(user_id=request.user.id)
            .select_related("workspace")
        )
        return Response({
            "userId": request.user.id,
            "isAuthenticated": True,
            "workspaces": [
                {
                    "id": str(m.workspace.id),
                    "name": m.workspace.name,
                    "role": m.role,
                }
                for m in memberships
            ],
        })


class WorkspaceView(APIView):
    """
    POST /v1/workspaces/ — create a workspace (caller becomes owner)
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        name = request.data.get("name", "").strip()
        if not name:
            raise ValidationError("Workspace name is required.")

        workspace = Workspace.objects.create(name=name)
        WorkspaceMember.objects.create(
            workspace=workspace,
            user_id=request.user.id,
            role=WorkspaceRole.OWNER,
        )

        return Response({
            "id": str(workspace.id),
            "name": workspace.name,
            "role": WorkspaceRole.OWNER,
        }, status=201)


class WorkspaceMemberView(APIView):
    """
    POST   /v1/workspaces/<workspace_id>/members/  — invite a user
    DELETE /v1/workspaces/<workspace_id>/members/  — remove a user (owner only)
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, workspace_id):
        workspace = resolve_workspace(request.user.id, workspace_id)
        members = WorkspaceMember.objects.filter(workspace=workspace)  # no .values()

        result = []
        for m in members:
            clerk_user = get_clerk_user(m.user_id)  # m is now a model instance
            result.append({
                "userId": m.user_id,
                "role": m.role,
                "name": f"{clerk_user.get('first_name', '')} {clerk_user.get('last_name', '')}".strip() if clerk_user else m.user_id,
                "email": next(
                    (e["email_address"] for e in clerk_user.get("email_addresses", []) if e.get("id") == clerk_user.get("primary_email_address_id")),
                    None,
                ) if clerk_user else None,
                "imageUrl": clerk_user.get("image_url") if clerk_user else None,
            })

        return Response(result)

    def post(self, request, workspace_id):
        workspace = resolve_workspace(request.user.id, workspace_id)  # any member can invite? adjust if needed
        
        invitee_user_id = request.data.get("userId", "").strip()
        role = request.data.get("role", WorkspaceRole.EDITOR)

        if not invitee_user_id:
            raise ValidationError("userId is required.")
        if role not in WorkspaceRole.values:
            raise ValidationError(f"Invalid role. Choose from: {WorkspaceRole.values}")

        if WorkspaceMember.objects.filter(workspace=workspace, user_id=invitee_user_id).exists():
            raise ValidationError("User is already a member of this workspace.")

        member = WorkspaceMember.objects.create(
            workspace=workspace,
            user_id=invitee_user_id,
            role=role,
        )

        return Response({
            "workspaceId": str(workspace.id),
            "userId": member.user_id,
            "role": member.role,
        }, status=201)

    def delete(self, request, workspace_id):
        # Only owners can remove members
        workspace = resolve_workspace(request.user.id, workspace_id)

        caller = WorkspaceMember.objects.get(workspace=workspace, user_id=request.user.id)
        if caller.role != WorkspaceRole.OWNER:
            raise PermissionDenied("Only owners can remove members.")

        target_user_id = request.data.get("userId", "").strip()
        if not target_user_id:
            raise ValidationError("userId is required.")

        deleted, _ = WorkspaceMember.objects.filter(
            workspace=workspace,
            user_id=target_user_id,
        ).delete()

        if not deleted:
            raise NotFound("Member not found.")

        return Response(status=204)


# ---------------------------------------------------------------------------
# Core ViewSets
# ---------------------------------------------------------------------------

class AccountViewSet(PlanEnforcementMixin, WorkspaceMixin, viewsets.ModelViewSet):
    serializer_class = AccountSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        workspace = self.get_workspace()
        return Account.objects.filter(workspace=workspace).order_by("name")

    def perform_create(self, serializer):
        workspace = self.get_workspace(require_editor=True)
        self.enforcer.check_can_create_account()
        serializer.save(workspace=workspace)

    def perform_update(self, serializer):
        self.get_workspace(require_editor=True)
        serializer.save()

    def perform_destroy(self, instance):
        self.get_workspace(require_editor=True)
        instance.delete()


class CategoryViewSet(PlanEnforcementMixin, WorkspaceMixin, viewsets.ModelViewSet):
    serializer_class = CategorySerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        workspace = self.get_workspace()
        return (
            Category.objects
            .filter(workspace=workspace)
            .select_related("parent")
            .order_by("name")
        )

    def perform_create(self, serializer):
        workspace = self.get_workspace(require_editor=True)
        self.enforcer.check_can_create_category()
        serializer.save(workspace=workspace)

    def perform_update(self, serializer):
        self.get_workspace(require_editor=True)
        serializer.save()

    def perform_destroy(self, instance):
        self.get_workspace(require_editor=True)
        instance.delete()


class TransactionViewSet(PlanEnforcementMixin, WorkspaceMixin, viewsets.ModelViewSet):
    serializer_class = TransactionSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        workspace = self.get_workspace()
        qs = (
            Transaction.objects
            .filter(workspace=workspace)
            .select_related("account", "category")
            .order_by("-date")
        )

        month = self.request.query_params.get("month")
        if month:
            self.enforcer.check_can_access_month(month)  # check BEFORE filtering
            start, end = month_range(month)
            qs = qs.filter(date__gte=start, date__lt=end)

        account_id = self.request.query_params.get("accountId")
        if account_id:
            qs = qs.filter(account_id=account_id)

        category_id = self.request.query_params.get("categoryId")
        if category_id:
            qs = qs.filter(category_id=category_id)

        return qs

    def perform_create(self, serializer):
        workspace = self.get_workspace(require_editor=True)
        date = serializer.validated_data.get("date")
        if date:
            month = date.strftime("%Y-%m")
            self.enforcer.check_can_access_month(month)  # block writes outside history window
        serializer.save(workspace=workspace, created_by=self.request.user.id)

    def perform_update(self, serializer):
        self.get_workspace(require_editor=True)
        if "date" in serializer.validated_data:
            month = serializer.validated_data["date"].strftime("%Y-%m")
            self.enforcer.check_can_access_month(month)
        serializer.save()

    def perform_destroy(self, instance):
        self.get_workspace(require_editor=True)
        instance.delete()


class BudgetViewSet(PlanEnforcementMixin, WorkspaceMixin, viewsets.ModelViewSet):
    serializer_class = BudgetSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        workspace = self.get_workspace()
        qs = (
            Budget.objects
            .filter(workspace=workspace)
            .select_related("category")
            .order_by("month", "category__name")
        )

        month = self.request.query_params.get("month")
        
        if month:
            if len(month) != 7 or month[4] != "-":
                raise ValidationError('Invalid month format. Use "YYYY-MM".')
            self.enforcer.check_can_access_month(month)
            qs = qs.filter(month=month)

        category_id = self.request.query_params.get("categoryId")
        if category_id:
            qs = qs.filter(category_id=category_id)

        return qs

    def perform_create(self, serializer):
        workspace = self.get_workspace(require_editor=True)
        serializer.save(workspace=workspace)

    def perform_update(self, serializer):
        self.get_workspace(require_editor=True)
        serializer.save()

    def perform_destroy(self, instance):
        self.get_workspace(require_editor=True)
        instance.delete()

    @action(detail=False, methods=["post"], url_path="copy-to-next-month")
    def copy_to_next_month(self, request, workspace_id):
        workspace = self.get_workspace(require_editor=True)

        month = request.data.get("month", "").strip()
        if not month or len(month) != 7 or month[4] != "-":
            raise ValidationError('Invalid month format. Use "YYYY-MM".')

        try:
            dt = datetime.strptime(month, "%Y-%m")
        except ValueError:
            raise ValidationError('Invalid month value.')

        next_month = (dt + relativedelta(months=1)).strftime("%Y-%m")

        source_budgets = Budget.objects.filter(workspace=workspace, month=month).select_related("category")
        if not source_budgets.exists():
            raise ValidationError(f"No budgets found for {month}.")

        created_objs = []
        skipped_count = 0

        for budget in source_budgets:
            obj, was_created = Budget.objects.get_or_create(
                workspace=workspace,
                month=next_month,
                category=budget.category,
                defaults={
                    "rule_type": budget.rule_type,
                    "value": budget.value,
                },
            )
            if was_created:
                created_objs.append(obj)
            else:
                skipped_count += 1

        return Response(
            {
                "nextMonth": next_month,
                "created": BudgetSerializer(created_objs, many=True).data,
                "createdCount": len(created_objs),
                "skippedCount": skipped_count,
            },
            status=201,
        )


# ---------------------------------------------------------------------------
# Config + Reports
# ---------------------------------------------------------------------------

class AccountMonthConfigView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, workspace_id):
        workspace = resolve_workspace(request.user.id, workspace_id)
        month = request.query_params.get("month")
        account_id = request.query_params.get("accountId")

        if not month or not account_id:
            raise ValidationError('"month" and "accountId" are required.')

        obj = AccountMonthConfig.objects.filter(
            workspace=workspace,
            month=month,
            account_id=account_id,
        ).first()

        return Response(AccountMonthConfigSerializer(obj).data if obj else None)

    def put(self, request, workspace_id):
        workspace = resolve_workspace(request.user.id, workspace_id, require_editor=True)
        month = request.query_params.get("month") or request.data.get("month")
        account_id = request.query_params.get("accountId") or request.data.get("accountId")

        if not month or not account_id:
            raise ValidationError('"month" and "accountId" are required.')

        if not Account.objects.filter(id=account_id, workspace=workspace).exists():
            raise ValidationError("Account not found.")

        try:
            opening_balance = Decimal(str(request.data.get("opening_balance", "0.00")))
        except Exception:
            raise ValidationError("opening_balance must be a valid decimal.")

        obj, _ = AccountMonthConfig.objects.update_or_create(
            workspace=workspace,
            month=month,
            account_id=account_id,
            defaults={"opening_balance": opening_balance},
        )

        return Response(AccountMonthConfigSerializer(obj).data)


class CashflowReportView(PlanEnforcementMixin, APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, workspace_id):
        workspace = resolve_workspace(request.user.id, workspace_id)

        month = request.query_params.get("month")
        if not month:
            raise ValidationError('Query param "month" is required (YYYY-MM).')

        # (optional but recommended) validate month format
        if len(month) != 7 or month[4] != "-":
            raise ValidationError('Invalid month format. Use "YYYY-MM".')

        # ✅ enforce plan history window here
        self.enforcer.check_can_access_month(month)

        account_id = request.query_params.get("accountId")
        start, end = month_range(month)

        if account_id and not Account.objects.filter(id=account_id, workspace=workspace).exists():
            raise ValidationError("Account not found.")

        opening, _ = _get_opening_and_income_base(workspace, month, account_id)

        qs = Transaction.objects.filter(workspace=workspace, date__gte=start, date__lt=end)
        if account_id:
            qs = qs.filter(account_id=account_id)

        money = DecimalField(max_digits=12, decimal_places=2)

        rows = (
            qs.values("date")
            .annotate(
                income=Coalesce(
                    Sum(Case(When(type="INCOME", then="amount"), default=Value(0), output_field=money)),
                    Value(0), output_field=money,
                ),
                expense=Coalesce(
                    Sum(Case(When(type="EXPENSE", then="amount"), default=Value(0), output_field=money)),
                    Value(0), output_field=money,
                ),
            )
            .order_by("date")
        )

        days = []
        running = opening
        for r in rows:
            income = r["income"] or Decimal("0.00")
            expense = r["expense"] or Decimal("0.00")
            net = income - expense
            running += net
            days.append({
                "date": r["date"].isoformat(),
                "income": f"{income:.2f}",
                "expense": f"{expense:.2f}",
                "net": f"{net:.2f}",
                "balance": f"{running:.2f}",
            })

        return Response({
            "month": month,
            "accountId": account_id,
            "openingBalance": f"{opening:.2f}",
            "closingBalance": f"{running:.2f}",
            "days": days,
        })


class BudgetMonitorView(PlanEnforcementMixin, APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, workspace_id):
        workspace = resolve_workspace(request.user.id, workspace_id)
        month = request.query_params.get("month")
        if not month:
            raise ValidationError('Query param "month" is required (YYYY-MM).')
        
        if len(month) != 7 or month[4] != "-":
            raise ValidationError('Invalid month format. Use "YYYY-MM".')

        self.enforcer.check_can_access_month(month)

        account_id = request.query_params.get("accountId")
        mode = request.query_params.get("mode", "leaf")
        if mode not in ("leaf", "rollup"):
            raise ValidationError('Invalid "mode". Use "leaf" or "rollup".')

        start, end = month_range(month)

        if account_id and not Account.objects.filter(id=account_id, workspace=workspace).exists():
            raise ValidationError("Account not found.")

        _, income_base = _get_opening_and_income_base(workspace, month, account_id)

        expense_qs = Transaction.objects.filter(
            workspace=workspace,
            date__gte=start,
            date__lt=end,
            type=TxType.EXPENSE,
        )
        if account_id:
            expense_qs = expense_qs.filter(account_id=account_id)

        rows, total_budget, total_spent, percent_summary = compute_budget_rows(
            workspace=workspace,
            month=month,
            expense_qs=expense_qs,
            mode=mode,
            income_base=income_base,
        )

        return Response({
            "month": month,
            "accountId": account_id,
            "mode": mode,
            "percentSummary": percent_summary,
            "totals": {
                "budgetResolved": f"{total_budget:.2f}",
                "spent": f"{total_spent:.2f}",
                "remaining": f"{(total_budget - total_spent):.2f}",
                "isExceeded": (total_budget - total_spent) < Decimal("0.00"),
            },
            "rows": rows,
        })


class DashboardReportView(PlanEnforcementMixin, APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, workspace_id):
        workspace = resolve_workspace(request.user.id, workspace_id)
        month = request.query_params.get("month")
        if not month:
            raise ValidationError('Query param "month" is required (YYYY-MM).')
        
        if len(month) != 7 or month[4] != "-":
            raise ValidationError('Invalid month format. Use "YYYY-MM".')

        self.enforcer.check_can_access_month(month)

        account_id = request.query_params.get("accountId")
        mode = request.query_params.get("mode", "leaf")
        if mode not in ("leaf", "rollup"):
            raise ValidationError('Invalid "mode". Use "leaf" or "rollup".')

        start, end = month_range(month)

        if account_id and not Account.objects.filter(id=account_id, workspace=workspace).exists():
            raise ValidationError("Account not found.")

        opening, income_base = _get_opening_and_income_base(workspace, month, account_id)

        tx_qs = Transaction.objects.filter(workspace=workspace, date__gte=start, date__lt=end)
        if account_id:
            tx_qs = tx_qs.filter(account_id=account_id)

        money = DecimalField(max_digits=12, decimal_places=2)

        totals = tx_qs.aggregate(
            income=Coalesce(
                Sum(Case(When(type=TxType.INCOME, then="amount"), default=Value(0), output_field=money)),
                Value(0), output_field=money,
            ),
            expense=Coalesce(
                Sum(Case(When(type=TxType.EXPENSE, then="amount"), default=Value(0), output_field=money)),
                Value(0), output_field=money,
            ),
        )

        income = q2(totals["income"] or Decimal("0.00"))
        expense = q2(totals["expense"] or Decimal("0.00"))
        net = income - expense
        closing = opening + net

        prev_month = prev_month_yyyymm(month)
        prev_start, prev_end = month_range(prev_month)
        prev_income, prev_expense, prev_net = compute_kpis_for_range(
            workspace=workspace,
            start=prev_start,
            end=prev_end,
            account_id=account_id,
        )

        recent_rows = [
            {
                "id": str(t.id),
                "date": t.date.isoformat(),
                "type": "income" if t.type == TxType.INCOME else "expense",
                "amount": f"{t.amount:.2f}",
                "categoryName": t.category.name if t.category_id else "Uncategorized",
                "note": t.note,
                "createdBy": t.created_by,  # useful if showing who logged each tx
            }
            for t in tx_qs.select_related("category").order_by("-date", "-created_at")[:10]
        ]

        expense_qs = tx_qs.filter(type=TxType.EXPENSE)

        budget_rows, total_budget, total_spent, percent_summary = compute_budget_rows(
            workspace=workspace,
            month=month,
            expense_qs=expense_qs,
            mode=mode,
            income_base=income_base,
        )

        return Response({
            "month": month,
            "accountId": account_id,
            "mode": mode,
            "kpis": {
                "income": f"{income:.2f}",
                "expense": f"{expense:.2f}",
                "net": f"{net:.2f}",
                "openingBalance": f"{opening:.2f}",
                "closingBalance": f"{closing:.2f}",
                "incomeBase": f"{income_base:.2f}",
            },
            "kpisCompare": {
                "previousMonth": prev_month,
                "previous": {
                    "income": f"{prev_income:.2f}",
                    "expense": f"{prev_expense:.2f}",
                    "net": f"{prev_net:.2f}",
                },
                "delta": {
                    "income": f"{(income - prev_income):.2f}",
                    "expense": f"{(expense - prev_expense):.2f}",
                    "net": f"{(net - prev_net):.2f}",
                },
                "deltaPct": {
                    "income": f"{pct_change(income, prev_income):.2f}" if pct_change(income, prev_income) is not None else None,
                    "expense": f"{pct_change(expense, prev_expense):.2f}" if pct_change(expense, prev_expense) is not None else None,
                    "net": f"{pct_change(net, prev_net):.2f}" if pct_change(net, prev_net) is not None else None,
                },
            },
            "budgets": {
                "percentSummary": percent_summary,
                "totals": {
                    "budgetResolved": f"{total_budget:.2f}",
                    "spent": f"{total_spent:.2f}",
                    "remaining": f"{(total_budget - total_spent):.2f}",
                    "isExceeded": (total_budget - total_spent) < Decimal("0.00"),
                },
                "rows": budget_rows,
            },
            "recentTransactions": recent_rows,
        })
    
# views.py — add this endpoint

class BudgetPeriodView(PlanEnforcementMixin, APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, workspace_id):
        workspace = resolve_workspace(request.user.id, workspace_id)

        date_from = request.query_params.get("dateFrom")
        date_to = request.query_params.get("dateTo")

        if not date_from or not date_to:
            raise ValidationError('"dateFrom" and "dateTo" are required (YYYY-MM-DD).')

        try:
            from_dt = date.fromisoformat(date_from)
            to_dt = date.fromisoformat(date_to)
        except ValueError:
            raise ValidationError("Invalid date format.")

        if from_dt > to_dt:
            raise ValidationError("dateFrom must be before dateTo.")

        # Derive month from dateFrom — budget resolution stays month-scoped
        month = from_dt.strftime("%Y-%m")

        self.enforcer.check_can_access_month(month)

        # Days in the month vs days in this pay period
        import calendar
        days_in_month = calendar.monthrange(from_dt.year, from_dt.month)[1]
        period_days = (to_dt - from_dt).days + 1
        period_ratio = Decimal(period_days) / Decimal(days_in_month)

        # Get income_base for the month
        account_id = request.query_params.get("accountId")
        _, income_base = _get_opening_and_income_base(workspace, month, account_id)

        # Spending within date range only
        expense_qs = Transaction.objects.filter(
            workspace=workspace,
            date__gte=from_dt,
            date__lte=to_dt,
            type=TxType.EXPENSE,
        )
        if account_id:
            expense_qs = expense_qs.filter(account_id=account_id)

        # Get monthly budgets and pro-rate them
        budgets = Budget.objects.filter(
            workspace=workspace,
            month=month,
        ).select_related("category")

        money = DecimalField(max_digits=12, decimal_places=2)
        spending = (
            expense_qs
            .values("category_id")
            .annotate(spent=Coalesce(Sum("amount"), Value(0), output_field=money))
        )
        spending_map = {str(r["category_id"]): r["spent"] for r in spending}

        rows = []
        for b in budgets:
            if b.rule_type == BudgetRuleType.FIXED:
                monthly_budget = b.value
            else:  # percent
                monthly_budget = (b.value / Decimal("100")) * income_base

            # Pro-rate: if pay period is 14/28 days, budget is 50% of monthly
            period_budget = (monthly_budget * period_ratio).quantize(Decimal("0.01"))
            spent = spending_map.get(str(b.category_id), Decimal("0.00"))

            rows.append({
                "categoryId": str(b.category_id),
                "categoryName": b.category.name,
                "ruleType": b.rule_type,
                "monthlyBudget": f"{monthly_budget:.2f}",
                "periodBudget": f"{period_budget:.2f}",
                "spent": f"{spent:.2f}",
                "remaining": f"{(period_budget - spent):.2f}",
                "isExceeded": spent > period_budget,
                "periodDays": period_days,
                "periodRatio": f"{period_ratio:.4f}",
            })

        return Response({
            "dateFrom": date_from,
            "dateTo": date_to,
            "month": month,
            "periodDays": period_days,
            "daysInMonth": days_in_month,
            "periodRatio": f"{period_ratio:.4f}",
            "rows": rows,
        })