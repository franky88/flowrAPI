from django.urls import path, include
from finance import views
from rest_framework.routers import DefaultRouter

router = DefaultRouter()

router.register(r"accounts", views.AccountViewSet, basename="accounts")
router.register(r"categories", views.CategoryViewSet, basename="categories")
router.register(r"transactions", views.TransactionViewSet, basename="transactions")
router.register(r"budgets", views.BudgetViewSet, basename="budgets")

urlpatterns = [
    path("whoami/", views.WhoAmIView.as_view(), name="whoami"),
    path("workspaces/", views.WorkspaceView.as_view()),
    path("workspaces/<uuid:workspace_id>/", include(router.urls)),
    path("workspaces/<uuid:workspace_id>/members/", views.WorkspaceMemberView.as_view()),
    path("workspaces/<uuid:workspace_id>/config/", views.AccountMonthConfigView.as_view()),
    path("workspaces/<uuid:workspace_id>/reports/cashflow/", views.CashflowReportView.as_view()),
    path("workspaces/<uuid:workspace_id>/reports/budget-monitor/", views.BudgetMonitorView.as_view()),
    path("workspaces/<uuid:workspace_id>/reports/dashboard/", views.DashboardReportView.as_view()),
    path("workspaces/<uuid:workspace_id>/reports/budget-period/", views.BudgetPeriodView.as_view()),
]
