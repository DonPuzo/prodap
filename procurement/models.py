import uuid

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models


class User(AbstractUser):
    class Role(models.TextChoices):
        PROCUREMENT_OFFICER = 'procurement_officer', 'Procurement Officer'
        ADMIN = 'admin', 'Admin'

    role = models.CharField(max_length=32, choices=Role.choices, default=Role.PROCUREMENT_OFFICER)

    def __str__(self):
        return f'{self.get_full_name() or self.username} ({self.role})'


class LawProfile(models.Model):
    """A jurisdiction's procurement rules, stored as data so a new state law
    can be added without a code change (build prompt section 5)."""

    JURISDICTION_CHOICES = [('federal', 'Federal'), ('state', 'State')]

    slug = models.SlugField(primary_key=True)
    jurisdiction_type = models.CharField(max_length=32, choices=JURISDICTION_CHOICES)
    governing_law = models.CharField(max_length=255)
    regulating_body = models.CharField(max_length=255)
    procurement_methods = models.JSONField(default=list)
    approval_thresholds = models.JSONField(default=list)

    def __str__(self):
        return self.governing_law


class ProcurementRecord(models.Model):
    class BudgetSource(models.TextChoices):
        TETFUND = 'TETFund', 'TETFund'
        IGR = 'IGR', 'IGR'
        GOVERNMENT_SUBVENTION = 'Government Subvention', 'Government Subvention'
        DONOR_GRANT = 'Donor/Grant', 'Donor/Grant'
        ALUMNI = 'Alumni', 'Alumni'
        OTHER = 'Other', 'Other'

    class Status(models.TextChoices):
        PLANNING = 'Planning', 'Planning'
        ADVERTISED = 'Advertised', 'Advertised'
        TENDERING = 'Tendering', 'Tendering'
        AWARDED = 'Awarded', 'Awarded'
        IMPLEMENTATION = 'Implementation', 'Implementation'
        COMPLETED = 'Completed', 'Completed'
        ABANDONED = 'Abandoned', 'Abandoned'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    department = models.CharField(max_length=255)
    budget_source = models.CharField(max_length=32, choices=BudgetSource.choices)
    estimated_cost = models.DecimalField(max_digits=16, decimal_places=2)
    awarded_cost = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    procurement_method = models.CharField(max_length=100)
    vendor_name = models.CharField(max_length=255, blank=True, null=True)
    vendor_registration_no = models.CharField(max_length=100, blank=True, null=True)
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.PLANNING)
    location = models.CharField(max_length=255)
    planned_start_date = models.DateField()
    planned_end_date = models.DateField()
    actual_start_date = models.DateField(null=True, blank=True)
    actual_end_date = models.DateField(null=True, blank=True)
    law_profile = models.ForeignKey(LawProfile, on_delete=models.PROTECT, related_name='records')
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='created_records'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def clean(self):
        # procurement_method must come from the attached law profile, never
        # a hardcoded global enum (build prompt section 5).
        if self.law_profile_id and self.procurement_method:
            valid_methods = self.law_profile.procurement_methods
            if self.procurement_method not in valid_methods:
                raise ValidationError({
                    'procurement_method': (
                        f'"{self.procurement_method}" is not a procurement method '
                        f'recognized by the {self.law_profile.governing_law} profile.'
                    )
                })

    @property
    def display_cost(self):
        return self.awarded_cost if self.awarded_cost is not None else self.estimated_cost

    def cost_outlier_ratio(self):
        """Rule-based cost-outlier signal (build prompt v2 Phase 2 item 2) —
        median awarded cost for same method+department, not ML. Returns None
        if there isn't enough comparable data yet."""
        comparables = ProcurementRecord.objects.filter(
            procurement_method=self.procurement_method,
            department=self.department,
            awarded_cost__isnull=False,
        ).exclude(pk=self.pk).values_list('awarded_cost', flat=True)
        comparables = sorted(comparables)
        if len(comparables) < 3 or self.display_cost is None:
            return None
        mid = len(comparables) // 2
        median = comparables[mid] if len(comparables) % 2 else (comparables[mid - 1] + comparables[mid]) / 2
        if not median:
            return None
        return float(self.display_cost) / float(median)

    def __str__(self):
        return self.title


class RecordFlag(models.Model):
    """Public 'flag this project as concerning' signal — Phase 2 item 1,
    the highest-evidenced anti-corruption feature in this category (see
    PRODAP_AGENT_BUILD_PROMPT_V2.md section 0, item 4 / Ukraine's Dozorro).

    Deliberately minimal: no moderation workflow, no status, no assignment.
    Just a public count plus optional notes, visible to both the public and
    staff, so scrutiny is visible rather than routed through a queue.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    record = models.ForeignKey(ProcurementRecord, on_delete=models.CASCADE, related_name='flags')
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'Flag on {self.record.title} ({self.created_at:%Y-%m-%d})'


class StatusUpdate(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    record = models.ForeignKey(ProcurementRecord, on_delete=models.CASCADE, related_name='status_updates')
    old_status = models.CharField(max_length=32, choices=ProcurementRecord.Status.choices, blank=True, null=True)
    new_status = models.CharField(max_length=32, choices=ProcurementRecord.Status.choices)
    note = models.TextField(blank=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='status_updates'
    )
    updated_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['updated_at']

    def __str__(self):
        return f'{self.record.title}: {self.old_status} -> {self.new_status}'
