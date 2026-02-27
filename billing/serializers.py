from rest_framework import serializers
from billing.models import Subscription


class SubscriptionSerializer(serializers.ModelSerializer):
    effective_plan = serializers.CharField(read_only=True)

    class Meta:
        model = Subscription
        fields = [
            "plan",
            "effective_plan",
            "status",
            "stripe_customer_id",
            "stripe_subscription_id",
            "current_period_end",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]


class InternalSubscriptionUpdateSerializer(serializers.ModelSerializer):
    """Used by the internal Stripe webhook PATCH endpoint."""

    class Meta:
        model = Subscription
        fields = [
            "plan",
            "status",
            "stripe_customer_id",
            "stripe_subscription_id",
            "current_period_end",
        ]