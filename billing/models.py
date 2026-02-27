from django.db import models
from django.utils import timezone


class PlanChoice(models.TextChoices):
    FREE = "free", "Free"
    PRO = "pro", "Pro"
    ENTERPRISE = "enterprise", "Enterprise"


class StatusChoice(models.TextChoices):
    ACTIVE = "active", "Active"
    CANCELLED = "cancelled", "Cancelled"
    PAST_DUE = "past_due", "Past Due"
    TRIALING = "trialing", "Trialing"


class Subscription(models.Model):
    user_id = models.CharField(max_length=255, unique=True, db_index=True)
    plan = models.CharField(max_length=20, choices=PlanChoice.choices, default=PlanChoice.FREE)
    status = models.CharField(max_length=20, choices=StatusChoice.choices, default=StatusChoice.ACTIVE)

    # Populated after Stripe checkout — null until then
    stripe_customer_id = models.CharField(max_length=255, blank=True, null=True)
    stripe_subscription_id = models.CharField(max_length=255, blank=True, null=True)
    current_period_end = models.DateTimeField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["user_id"], name="idx_subscription_user"),
        ]

    def __str__(self) -> str:
        return f"{self.user_id} — {self.plan} ({self.status})"

    @property
    def effective_plan(self) -> str:
        """
        Always use this instead of reading .plan directly.
        Downgrades to free if the subscription is not in a paid-active state.
        past_due gets a grace period until current_period_end.
        """
        if self.status == StatusChoice.ACTIVE or self.status == StatusChoice.TRIALING:
            return self.plan

        if self.status == StatusChoice.PAST_DUE:
            if self.current_period_end and timezone.now() < self.current_period_end:
                return self.plan  # still within grace period
            return PlanChoice.FREE

        # cancelled or anything else
        return PlanChoice.FREE