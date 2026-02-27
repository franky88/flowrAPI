import os
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied, NotFound

from billing.models import Subscription
from billing.enforcement import PlanEnforcer
from billing.serializers import InternalSubscriptionUpdateSerializer


class SubscriptionView(APIView):
    """
    GET /api/subscription/
    Returns the current user's plan, status, and live usage counts.
    Call on app load to gate UI features.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        enforcer = PlanEnforcer(request.user.id)
        sub = enforcer.subscription
        usage = enforcer.get_usage_summary()

        return Response({
            "plan": sub.effective_plan,
            "status": sub.status,
            **usage,
        })


class InternalSubscriptionUpdateView(APIView):
    """
    PATCH /api/internal/subscription/<user_id>/
    Called by the Stripe webhook handler to sync subscription state.
    Protected by a shared secret header, NOT Clerk JWT.

    TODO: Wire up real Stripe webhook handler when ready.
    """
    permission_classes = []  # No Clerk auth — uses shared secret instead

    def patch(self, request, user_id):
        secret = request.headers.get("X-Internal-Secret", "")
        expected = os.environ.get("INTERNAL_WEBHOOK_SECRET", "")

        if not expected or secret != expected:
            raise PermissionDenied("Invalid or missing internal secret.")

        try:
            sub = Subscription.objects.get(user_id=user_id)
        except Subscription.DoesNotExist:
            raise NotFound(f"No subscription found for user {user_id}.")

        serializer = InternalSubscriptionUpdateSerializer(sub, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        return Response({
            "user_id": user_id,
            "plan": sub.effective_plan,
            "status": sub.status,
        })