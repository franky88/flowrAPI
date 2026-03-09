import stripe
import os
import datetime
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from billing.models import Subscription

stripe.api_key = os.environ["STRIPE_SECRET_KEY"]

@csrf_exempt
def stripe_webhook(request):
    payload = request.body
    sig_header = request.META.get("HTTP_STRIPE_SIGNATURE")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, os.environ["STRIPE_WEBHOOK_SECRET"]
        )
    except Exception:
        return HttpResponse(status=400)

    data = event["data"]["object"]

    print(f"Received Stripe event: {event['type']}")
    print(f"Data keys: {list(data.keys())}")

    if event["type"] == "checkout.session.completed":
        user_id = data["metadata"]["user_id"]

        sub, _ = Subscription.objects.get_or_create(user_id=user_id)
        sub.plan = "pro"
        sub.status = "active"
        sub.stripe_customer_id = data["customer"]
        sub.stripe_subscription_id = data["subscription"]
        # Don't set current_period_end here — invoice.payment_succeeded handles it
        sub.save()

    elif event["type"] == "invoice.payment_succeeded":
        stripe_sub_id = (
            data.get("subscription")
            or (data.get("parent") or {}).get("subscription_details", {}).get("subscription")
        )
        print("invoice.payment_succeeded stripe_sub_id:", stripe_sub_id)

        if stripe_sub_id:
            # Get period end from the invoice line item — no extra API call needed
            lines = data["lines"]["data"] if "lines" in data else []
            period_end = lines[0]["period"]["end"] if lines else None

            print("period_end raw:", period_end, type(period_end))

            if period_end:
                period_end_dt = datetime.datetime.fromtimestamp(period_end, tz=datetime.timezone.utc)

                updated = Subscription.objects.filter(
                    stripe_subscription_id=stripe_sub_id
                ).update(status="active", current_period_end=period_end_dt)

                if not updated:
                    customer_id = data.get("customer")
                    if customer_id:
                        Subscription.objects.filter(
                            stripe_customer_id=customer_id
                        ).update(
                            status="active",
                            stripe_subscription_id=stripe_sub_id,
                            current_period_end=period_end_dt,
                        )

    elif event["type"] == "invoice.payment_failed":
        stripe_sub_id = (
            data.get("subscription")
            or (data.get("parent") or {}).get("subscription_details", {}).get("subscription")
        )
        if stripe_sub_id:
            Subscription.objects.filter(stripe_subscription_id=stripe_sub_id).update(
                status="past_due"
            )

    elif event["type"] == "customer.subscription.deleted":
        stripe_sub_id = data["id"]
        Subscription.objects.filter(stripe_subscription_id=stripe_sub_id).update(
            plan="free",
            status="cancelled",
        )

    return HttpResponse(status=200)