# billing/checkout.py
import stripe
import os
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

stripe.api_key = os.environ["STRIPE_SECRET_KEY"]

PRICE_ID = "price_1T944CRggErS8UWFY9PoeoIK"  # your actual price ID

class CreateCheckoutSessionView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user_id = request.user.id

        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": PRICE_ID, "quantity": 1}],
            success_url=f"{os.environ['FRONTEND_URL']}/dashboard/billing?upgrade=success",
            cancel_url=f"{os.environ['FRONTEND_URL']}/dashboard/billing?upgrade=cancelled",
            metadata={"user_id": user_id},
        )

        return Response({"url": session.url})  # ← must return this