from django.urls import path
from billing.views import SubscriptionView, InternalSubscriptionUpdateView

urlpatterns = [
    path("subscription/", SubscriptionView.as_view(), name="subscription"),
    path("internal/subscription/<str:user_id>/", InternalSubscriptionUpdateView.as_view(), name="internal-subscription-update",),
]