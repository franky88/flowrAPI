from rest_framework.exceptions import APIException
from rest_framework.views import exception_handler
from rest_framework.response import Response


class PlanLimitExceeded(Exception):
    """
    Raised by PlanEnforcer when a user exceeds their plan's limits.
    Carries a human-readable message and a machine-readable limit_key.
    """

    def __init__(self, message: str, limit_key: str):
        self.message = message
        self.limit_key = limit_key
        super().__init__(message)


def custom_exception_handler(exc, context):
    """
    Register in settings.py:
        REST_FRAMEWORK = {
            "EXCEPTION_HANDLER": "billing.exceptions.custom_exception_handler",
        }
    """
    if isinstance(exc, PlanLimitExceeded):
        return Response(
            {
                "error": "plan_limit_exceeded",
                "message": exc.message,
                "limit_key": exc.limit_key,
            },
            status=402,
        )

    # Fall back to DRF's default handler for everything else
    return exception_handler(exc, context)


class PlanEnforcementMixin:
    """
    Add to any DRF view to get a lazy self.enforcer property.
    The PlanEnforcer (and its DB hit) is only created on first access.

    Usage:
        class MyView(PlanEnforcementMixin, generics.ListCreateAPIView):
            def perform_create(self, serializer):
                self.enforcer.check_can_create_account()
                serializer.save(...)
    """

    _enforcer = None

    @property
    def enforcer(self):
        if self._enforcer is None:
            from billing.enforcement import PlanEnforcer
            self._enforcer = PlanEnforcer(self.request.user.id)
        return self._enforcer