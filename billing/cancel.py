import stripe
import os
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from billing.models import Subscription

stripe.api_key = os.environ["STRIPE_SECRET_KEY"]

class CancelSubscriptionView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user_id = request.user.id

        try:
            sub = Subscription.objects.get(user_id=user_id)
        except Subscription.DoesNotExist:
            return Response({"error": "No subscription found."}, status=404)

        if not sub.stripe_subscription_id:
            return Response({"error": "No active Stripe subscription."}, status=400)

        stripe.Subscription.modify(
            sub.stripe_subscription_id,
            cancel_at_period_end=True,
        )

        sub.status = "cancelled"
        sub.save()

        return Response({"message": "Subscription will cancel at end of billing period."})