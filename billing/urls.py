from django.urls import path
from billing.cancel import CancelSubscriptionView
from billing.checkout import CreateCheckoutSessionView
from billing.views import SubscriptionView, InternalSubscriptionUpdateView
from billing.webhook import stripe_webhook

urlpatterns = [
    path("subscription/", SubscriptionView.as_view(), name="subscription"),
    path("subscription/checkout/", CreateCheckoutSessionView.as_view()),
    path("subscription/cancel/", CancelSubscriptionView.as_view()),
    path("internal/subscription/<str:user_id>/", InternalSubscriptionUpdateView.as_view(), name="internal-subscription-update",),
    path("webhooks/stripe/", stripe_webhook),
]