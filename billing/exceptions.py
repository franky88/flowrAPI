# billing/exceptions.py
import logging
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError
from rest_framework import status
from rest_framework.exceptions import APIException
from rest_framework.views import exception_handler
from rest_framework.response import Response

logger = logging.getLogger(__name__)


class PlanLimitExceeded(Exception):
    def __init__(self, message: str, limit_key: str):
        self.message = message
        self.limit_key = limit_key
        super().__init__(message)


def custom_exception_handler(exc, context):
    """
    Centralized error handler. Registered in settings.py:
        REST_FRAMEWORK = {
            "EXCEPTION_HANDLER": "billing.exceptions.custom_exception_handler",
        }

    Response shape is always:
        { "error": "<code>", "message": "<human string>", ...extra }
    """

    # ── 1. Plan limit (402) ───────────────────────────────────────────────────
    if isinstance(exc, PlanLimitExceeded):
        return Response(
            {
                "error": "plan_limit_exceeded",
                "message": exc.message,
                "limit_key": exc.limit_key,
            },
            status=status.HTTP_402_PAYMENT_REQUIRED,
        )

    # ── 2. DB uniqueness violations (400) ─────────────────────────────────────
    if isinstance(exc, IntegrityError):
        message = str(exc)
        # Map common constraint names to friendly messages
        constraint_map = {
            "uniq_account_workspace_name": "An account with this name already exists.",
            "uniq_category_workspace_parent_name": "A category with this name already exists.",
            "uniq_budget_workspace_month_category": "A budget for this category and month already exists.",
            "uniq_amc_workspace_month_account": "A month config for this account already exists.",
            "uniq_workspace_member": "This user is already a member of the workspace.",
        }
        friendly = next(
            (msg for key, msg in constraint_map.items() if key in message),
            "A duplicate entry already exists.",
        )
        return Response(
            {"error": "duplicate_entry", "message": friendly},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # ── 3. Django ValidationError (400) ───────────────────────────────────────
    if isinstance(exc, DjangoValidationError):
        return Response(
            {
                "error": "validation_error",
                "message": exc.message if hasattr(exc, "message") else str(exc),
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    # ── 4. Let DRF handle its own exceptions (ValidationError, NotFound, etc.)
    response = exception_handler(exc, context)

    if response is not None:
        # Normalize DRF error shape: always wrap in { error, message }
        original = response.data

        if isinstance(original, dict) and "error" in original:
            # Already normalized (e.g. re-raised from this handler)
            return response

        if isinstance(original, dict) and "detail" in original:
            # Standard DRF single-message errors: NotFound, PermissionDenied, etc.
            response.data = {
                "error": _status_to_code(response.status_code),
                "message": str(original["detail"]),
            }
        elif isinstance(original, dict):
            # Serializer field errors: { field: ["msg"] }
            response.data = {
                "error": "validation_error",
                "message": "Invalid input.",
                "fields": _flatten_errors(original),
            }
        elif isinstance(original, list):
            # Non-field errors: ["msg1", "msg2"]
            response.data = {
                "error": "validation_error",
                "message": " ".join(str(e) for e in original),
            }

        return response

    # ── 5. Unhandled exceptions → 500 ─────────────────────────────────────────
    view = context.get("view")
    logger.exception(
        "Unhandled exception in %s",
        view.__class__.__name__ if view else "unknown view",
        exc_info=exc,
    )
    return Response(
        {
            "error": "internal_error",
            "message": "An unexpected error occurred. Please try again.",
        },
        status=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _status_to_code(status_code: int) -> str:
    return {
        400: "bad_request",
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        405: "method_not_allowed",
        409: "conflict",
        429: "too_many_requests",
    }.get(status_code, "error")


def _flatten_errors(data: dict) -> dict:
    """Flatten DRF nested field errors into { field: "first error message" }."""
    flat = {}
    for field, errors in data.items():
        if isinstance(errors, list) and errors:
            flat[field] = str(errors[0])
        elif isinstance(errors, dict):
            for subfield, suberrors in errors.items():
                key = f"{field}.{subfield}"
                flat[key] = str(suberrors[0]) if isinstance(suberrors, list) else str(suberrors)
        else:
            flat[field] = str(errors)
    return flat


# ── Mixin (unchanged) ─────────────────────────────────────────────────────────

class PlanEnforcementMixin:
    _enforcer = None

    @property
    def enforcer(self):
        if self._enforcer is None:
            from billing.enforcement import PlanEnforcer
            self._enforcer = PlanEnforcer(self.request.user.id)
        return self._enforcer