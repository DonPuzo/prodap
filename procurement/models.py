import uuid

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import models


class User(AbstractUser):
    class Role(models.TextChoices):
        REQUESTING_UNIT = 'requesting_unit', 'Requesting Unit'
        PROCUREMENT_UNIT = 'procurement_unit', 'Procurement Unit'
        FINANCE = 'finance', 'Finance/Budget'
        ACCOUNTING_OFFICER = 'accounting_officer', 'Accounting Officer'
        TENDERS_BOARD = 'tenders_board', 'Tenders Board'
        ADMIN = 'admin', 'Admin'
        # Reserved for later phases — not built yet: evaluation_committee
        # (folded into Tenders Board's evaluation_summary for this slice —
        # see TendersBoardReview's docstring), bpp_reviewer, contract_manager,
        # bidder, observer.

    role = models.CharField(max_length=32, choices=Role.choices, default=Role.PROCUREMENT_UNIT)

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
    default_minimum_bidding_days = models.PositiveIntegerField(
        default=14,
        help_text=(
            'Institutional policy default: minimum number of days required between '
            'advertisement publication and bid closing. This is a configurable placeholder, '
            'NOT a verified statutory figure for any specific procurement method — the source '
            'framework gives illustrative examples (e.g. "at least 30 days for consultancy '
            'proposals") but no complete table the way it does for approval thresholds. '
            'Adjust per institution/legal review; a future increment could branch this per '
            'procurement_method once an authoritative table is available.'
        ),
    )

    def __str__(self):
        return self.governing_law


class FinancialYear(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    law_profile = models.ForeignKey(LawProfile, on_delete=models.PROTECT, related_name='financial_years')
    label = models.CharField(max_length=20)
    start_date = models.DateField()
    end_date = models.DateField()
    is_current = models.BooleanField(default=False)

    class Meta:
        unique_together = [('law_profile', 'label')]
        ordering = ['-start_date']

    def __str__(self):
        return self.label


class ThresholdRule(models.Model):
    """Versioned, effective-dated replacement for treating LawProfile's flat
    `approval_thresholds` JSON as canonical. A new rule supersedes an old one
    by closing its effective_to date and adding a new row — never mutating
    or deleting history (e-procurement integration framework: "Non-Negotiable
    Technical and Integrity Controls" / versioned threshold tables)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    law_profile = models.ForeignKey(LawProfile, on_delete=models.PROTECT, related_name='threshold_rules')
    procurement_method = models.CharField(max_length=100)
    min_value = models.DecimalField(max_digits=16, decimal_places=2)
    max_value = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    approving_authority = models.CharField(max_length=255)
    bpp_prior_review_required = models.BooleanField(default=False)
    is_default_for_range = models.BooleanField(
        default=False,
        help_text='Exactly one active rule per overlapping value range/date should be the default (open competitive bidding).',
    )
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    note = models.TextField(blank=True, help_text='Legal citation / rationale for this rule.')
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='threshold_rules_created'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-effective_from']

    def clean(self):
        if self.law_profile_id and self.procurement_method:
            if self.procurement_method not in self.law_profile.procurement_methods:
                raise ValidationError({
                    'procurement_method': (
                        f'"{self.procurement_method}" is not recognized by the '
                        f'{self.law_profile.governing_law} profile.'
                    )
                })

    def __str__(self):
        upper = self.max_value if self.max_value is not None else 'unbounded'
        return f'{self.procurement_method}: {self.min_value}-{upper} (from {self.effective_from})'


class ProcessIdentifierSequence(models.Model):
    """Race-safe counter for generating a unique procurement process
    identifier at funds-confirmation time (select_for_update, not a racy
    count()+1)."""

    law_profile = models.ForeignKey(LawProfile, on_delete=models.PROTECT, related_name='process_id_sequences')
    financial_year = models.ForeignKey(FinancialYear, on_delete=models.PROTECT, related_name='process_id_sequences')
    last_value = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = [('law_profile', 'financial_year')]

    def __str__(self):
        return f'{self.law_profile_id}/{self.financial_year.label}: {self.last_value}'


class ProcurementPlan(models.Model):
    """The consolidated annual procurement plan for one financial year.
    Only an approved plan line may initiate a procurement requisition —
    the single most important stage gate in Phase 1 (see PlanLine)."""

    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        SUBMITTED = 'submitted', 'Submitted'
        APPROVED = 'approved', 'Approved'
        REJECTED = 'rejected', 'Rejected'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    law_profile = models.ForeignKey(LawProfile, on_delete=models.PROTECT, related_name='procurement_plans')
    financial_year = models.ForeignKey(FinancialYear, on_delete=models.PROTECT, related_name='procurement_plans')
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    prepared_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='plans_prepared'
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name='plans_approved'
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    rejected_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('law_profile', 'financial_year')]
        ordering = ['-created_at']

    def __str__(self):
        return f'Procurement Plan {self.financial_year.label}'


class PlanLine(models.Model):
    """One need/item proposed by a requesting unit within an annual plan.
    Approving the parent plan bulk-approves its non-amendment pending
    lines; a line added later to an already-approved plan (is_amendment)
    stays pending until individually approved."""

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        APPROVED = 'approved', 'Approved'
        REJECTED = 'rejected', 'Rejected'

    class Quarter(models.TextChoices):
        Q1 = 'Q1', 'Q1'
        Q2 = 'Q2', 'Q2'
        Q3 = 'Q3', 'Q3'
        Q4 = 'Q4', 'Q4'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    plan = models.ForeignKey(ProcurementPlan, on_delete=models.CASCADE, related_name='lines')
    department = models.CharField(max_length=255)
    item_description = models.CharField(max_length=255)
    justification = models.TextField()
    quantity = models.PositiveIntegerField(default=1)
    unit_of_measure = models.CharField(max_length=50, blank=True)
    estimated_cost = models.DecimalField(max_digits=16, decimal_places=2)
    budget_line = models.CharField(max_length=255)
    proposed_method = models.CharField(
        max_length=100, blank=True,
        help_text='Non-binding — the requisition\'s binding method comes from ThresholdRule.',
    )
    proposed_quarter = models.CharField(max_length=2, choices=Quarter.choices)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    is_amendment = models.BooleanField(default=False)
    amendment_note = models.TextField(blank=True)
    proposed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='plan_lines_proposed'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f'{self.item_description} ({self.department})'


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
    # Nullable: records seeded before Phase 1-Foundation (or any future
    # record deliberately created outside the requisition flow) simply have
    # requisition=None — that is honest, unambiguous "pre-Foundation legacy
    # data" rather than a fabricated approval trail. String reference avoids
    # a forward-declaration problem since Requisition is defined below and
    # itself references ProcurementRecord.BudgetSource.
    requisition = models.OneToOneField(
        'Requisition', on_delete=models.PROTECT, null=True, blank=True, related_name='record'
    )
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

    # 25% above the department+method median — deliberately simple and
    # explainable, not a tuned/statistical threshold (see cost_outlier_ratio).
    COST_OUTLIER_THRESHOLD = 1.25

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

    def is_cost_outlier(self):
        ratio = self.cost_outlier_ratio()
        return ratio is not None and ratio >= self.COST_OUTLIER_THRESHOLD

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


class Requisition(models.Model):
    """Created from an approved PlanLine, gated through funds confirmation,
    packaging/anti-splitting review, and method/threshold determination
    before it can produce a ProcurementRecord (see services.py for the
    functions that enforce each gate — never write these fields directly
    from a view)."""

    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        SUBMITTED = 'submitted', 'Submitted'
        FUNDS_CONFIRMED = 'funds_confirmed', 'Funds Confirmed'
        FUNDS_DECLINED = 'funds_declined', 'Funds Declined'
        RECORD_CREATED = 'record_created', 'Record Created'
        CANCELLED = 'cancelled', 'Cancelled'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    process_identifier = models.CharField(max_length=50, unique=True, blank=True, null=True)
    plan_line = models.ForeignKey(PlanLine, on_delete=models.PROTECT, related_name='requisitions')
    title = models.CharField(max_length=255)
    department = models.CharField(max_length=255)
    requested_value = models.DecimalField(max_digits=16, decimal_places=2)
    budget_source = models.CharField(max_length=32, choices=ProcurementRecord.BudgetSource.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='requisitions_created'
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    funds_confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name='requisitions_funds_confirmed',
    )
    funds_confirmed_at = models.DateTimeField(null=True, blank=True)
    funds_confirmation_note = models.TextField(blank=True)
    packaging_reviewed = models.BooleanField(default=False)
    packaging_review_note = models.TextField(blank=True)
    packaging_reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name='requisitions_packaging_reviewed',
    )
    packaging_reviewed_at = models.DateTimeField(null=True, blank=True)
    threshold_rule = models.ForeignKey(
        ThresholdRule, on_delete=models.PROTECT, null=True, blank=True, related_name='requisitions'
    )
    determined_method = models.CharField(max_length=100, blank=True)
    determined_approving_authority = models.CharField(max_length=255, blank=True)
    bpp_prior_review_required = models.BooleanField(default=False)
    method_override = models.CharField(max_length=100, blank=True)
    method_override_justification = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def clean(self):
        # A requisition must stay inside what the accounting_officer
        # actually approved on the plan line — without this, the entire
        # point of the plan-approval gate is defeated (security review
        # finding, Phase 1-Foundation). department is form-derived from
        # plan_line (see forms.RequisitionForm) but validated here too as
        # defense-in-depth against any other code path creating one.
        if self.plan_line_id and self.requested_value is not None:
            if self.requested_value > self.plan_line.estimated_cost:
                raise ValidationError({
                    'requested_value': (
                        f'Requested value (₦{self.requested_value}) exceeds the approved plan '
                        f'line estimate (₦{self.plan_line.estimated_cost}). Requesting more than '
                        'what was approved needs a formally approved plan amendment first.'
                    )
                })
        if self.plan_line_id and self.department and self.department != self.plan_line.department:
            raise ValidationError({
                'department': (
                    f'Department ("{self.department}") does not match the approved plan '
                    f'line\'s department ("{self.plan_line.department}").'
                )
            })

    def __str__(self):
        return self.title


class AuditEvent(models.Model):
    """Append-only log for every Phase 1-Foundation gate crossing (plan
    approval, funds confirmation, packaging review, method determination,
    ...). Deliberately a NEW, separate model from StatusUpdate — StatusUpdate
    already does its one job (record status history) correctly and 19+
    existing tests depend on its exact shape; a future unified audit view
    can UNION both rather than forcing a breaking generalization now.

    object_id is a plain UUIDField (not the usual loose CharField) because
    every target model in this app uses UUID primary keys."""

    class Action(models.TextChoices):
        PLAN_SUBMITTED = 'plan_submitted', 'Plan Submitted'
        PLAN_APPROVED = 'plan_approved', 'Plan Approved'
        PLAN_REJECTED = 'plan_rejected', 'Plan Rejected'
        PLAN_LINE_APPROVED = 'plan_line_approved', 'Plan Line Approved'
        PLAN_LINE_REJECTED = 'plan_line_rejected', 'Plan Line Rejected'
        REQUISITION_SUBMITTED = 'requisition_submitted', 'Requisition Submitted'
        FUNDS_CONFIRMED = 'funds_confirmed', 'Funds Confirmed'
        FUNDS_DECLINED = 'funds_declined', 'Funds Declined'
        PACKAGING_REVIEWED = 'packaging_reviewed', 'Packaging Reviewed'
        METHOD_DETERMINED = 'method_determined', 'Method Determined'
        METHOD_OVERRIDDEN = 'method_overridden', 'Method Overridden'
        RECORD_CREATED_FROM_REQUISITION = 'record_created_from_requisition', 'Record Created From Requisition'
        SOLICITATION_PREPARED = 'solicitation_prepared', 'Solicitation Prepared'
        SOLICITATION_APPROVED = 'solicitation_approved', 'Solicitation Approved'
        SOLICITATION_REJECTED = 'solicitation_rejected', 'Solicitation Rejected'
        ADVERTISEMENT_PUBLISHED = 'advertisement_published', 'Advertisement Published'
        CLARIFICATION_ANSWERED = 'clarification_answered', 'Clarification Answered'
        PREQUALIFICATION_RECORDED = 'prequalification_recorded', 'Prequalification Applicant Recorded'
        PREQUALIFICATION_REVIEWED = 'prequalification_reviewed', 'Prequalification Applicant Reviewed'
        BID_RECORDED = 'bid_recorded', 'Bid Recorded'
        AWARD_DECIDED = 'award_decided', 'Award Decided'
        COMPLAINT_RESOLVED = 'complaint_resolved', 'Complaint Resolved'
        CONTRACT_SIGNED = 'contract_signed', 'Contract Signed'
        MILESTONE_ADDED = 'milestone_added', 'Milestone Added'
        MILESTONE_COMPLETED = 'milestone_completed', 'Milestone Completed'
        CONTRACT_COMPLETED = 'contract_completed', 'Contract Completed'
        PERFORMANCE_GUARANTEE_RECORDED = 'performance_guarantee_recorded', 'Performance Guarantee Recorded'
        INVOICE_SUBMITTED = 'invoice_submitted', 'Invoice Submitted'
        INVOICE_REVIEWED = 'invoice_reviewed', 'Invoice Reviewed'
        PAYMENT_RECORDED = 'payment_recorded', 'Payment Recorded'
        TENDERS_BOARD_REVIEWED = 'tenders_board_reviewed', 'Tenders Board Reviewed'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    content_type = models.ForeignKey(ContentType, on_delete=models.PROTECT)
    object_id = models.UUIDField()
    target = GenericForeignKey('content_type', 'object_id')
    action = models.CharField(max_length=40, choices=Action.choices)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='audit_events')
    role_at_time = models.CharField(max_length=32)
    reason = models.TextField(blank=True)
    old_value = models.JSONField(null=True, blank=True)
    new_value = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']
        indexes = [models.Index(fields=['content_type', 'object_id'])]

    def __str__(self):
        return f'{self.action} by {self.actor_id} at {self.created_at:%Y-%m-%d %H:%M}'


# --- Phase 2 (non-cryptographic slice): solicitation preparation -> advertisement/publication. ---
# Prequalification/EOI and clarifications/addenda are NOT built here — the natural
# extension point for both is an FK to Solicitation, added later without restructuring
# this. The encrypted bid submission/opening system is entirely separate future work.


class Solicitation(models.Model):
    """The SBD/RFP document for a ProcurementRecord (integration framework
    step 06). FK, not OneToOne — a rejected solicitation is superseded by a
    new version rather than mutated in place (append-only history, matching
    ThresholdRule's "never mutate, add a new row" discipline). Hangs off
    ProcurementRecord directly (not Requisition) so it also works for
    pre-Foundation legacy records that have requisition=None.

    Deliberately does not duplicate procurement_method or
    bpp_prior_review_required — those are read live off record.procurement_method
    (an immutable snapshot taken at record-creation time) and the
    bpp_prior_review_required property below, since re-copying them here would
    only add a staleness risk for no benefit."""

    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        APPROVED = 'approved', 'Approved'
        REJECTED = 'rejected', 'Rejected'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    record = models.ForeignKey(ProcurementRecord, on_delete=models.PROTECT, related_name='solicitations')
    version = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)

    eligibility_criteria = models.TextField(
        help_text='Who may bid: registration, category, experience requirements.'
    )
    scope_and_specifications = models.TextField(
        help_text='Technical specifications / scope of work / terms of reference.'
    )
    evaluation_criteria = models.TextField(help_text='Narrative evaluation methodology.')
    evaluation_weights = models.JSONField(
        default=dict, blank=True,
        help_text=(
            'Optional structured weights, e.g. {"technical": 70, "financial": 30}. '
            'Informational only — automated scoring is a future evaluation phase, not built yet.'
        ),
    )
    bid_security_required = models.BooleanField(default=False)
    bid_security_type = models.CharField(max_length=100, blank=True, help_text='e.g. Bank guarantee, insurance bond.')
    bid_security_amount = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)

    prepared_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='solicitations_prepared'
    )
    prepared_at = models.DateTimeField(auto_now_add=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name='solicitations_approved',
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    rejected_reason = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-version']
        unique_together = [('record', 'version')]

    def clean(self):
        if self.bid_security_required and self.bid_security_amount is None:
            raise ValidationError({'bid_security_amount': 'Required when bid security is required.'})

    @property
    def bpp_prior_review_required(self):
        """None means "not tracked" — e.g. for a legacy record with no requisition."""
        requisition = self.record.requisition
        return requisition.bpp_prior_review_required if requisition else None

    def __str__(self):
        return f'{self.record.title} — Solicitation v{self.version} ({self.status})'


class Advertisement(models.Model):
    """The publication record for an approved Solicitation (integration
    framework step 07, publication half only — prequalification/EOI is
    explicitly future work). OneToOne on Solicitation: one publish event per
    approved solicitation version; re-advertisement (a solicitation that
    fails to attract bidders) would need a new Solicitation version plus a
    fresh Advertisement — no dedicated re-advertise flow exists yet."""

    CHANNEL_CHOICES = [
        ('newspaper', 'National Newspaper'),
        ('institution_website', 'Institution Website'),
        ('institution_notice_board', 'Institution Notice Board'),
        ('bpp_portal', 'BPP / National Procurement Portal'),
        ('other', 'Other (see publication proof)'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    solicitation = models.OneToOneField(Solicitation, on_delete=models.PROTECT, related_name='advertisement')
    channels = models.JSONField(default=list, help_text='List of channel codes — see Advertisement.CHANNEL_CHOICES.')
    publication_proof = models.TextField(
        help_text='Reference/description of proof of publication (newspaper name+date, URL, notice-board photo reference, etc).'
    )
    closing_date = models.DateField(help_text='Bid/proposal submission deadline.')
    minimum_bidding_days_applied = models.PositiveIntegerField(
        help_text=(
            'Snapshot of the institutional minimum-bidding-period policy in effect at publish '
            'time (see LawProfile.default_minimum_bidding_days) — kept even if the policy '
            'default later changes, so historical records stay explainable.'
        ),
    )
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='advertisements_published'
    )
    published_at = models.DateTimeField(auto_now_add=True)

    def clean(self):
        if not self.channels:
            raise ValidationError({'channels': 'At least one publication channel is required.'})
        valid_codes = dict(self.CHANNEL_CHOICES)
        bad = [c for c in self.channels if c not in valid_codes]
        if bad:
            raise ValidationError({'channels': f'Unrecognized channel(s): {", ".join(bad)}.'})

    def __str__(self):
        return f'Advertisement for {self.solicitation.record.title} (closes {self.closing_date})'


class Clarification(models.Model):
    """Public Q&A on a published Solicitation (integration framework step
    08). Deliberately anonymous — no asked_by field — matching both
    RecordFlag's existing anonymous-by-design precedent and the blueprint's
    own rule that responses be "distributed equally, without identifying
    the questioner." An empty `answer` means unanswered; see
    services.answer_clarification() for the only sanctioned way to answer.

    Public display is deliberately conservative: answered Q&A pairs are
    shown in full, but unanswered questions are not shown by raw text (only
    a pending count) — mirroring how RecordFlag's note text is staff-only
    while only the flag count is public, and how a real tender addendum
    publishes a finished Q&A pair rather than raw incoming questions."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    solicitation = models.ForeignKey(Solicitation, on_delete=models.PROTECT, related_name='clarifications')
    question = models.TextField()
    asked_at = models.DateTimeField(auto_now_add=True)
    answer = models.TextField(blank=True)
    answered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name='clarifications_answered',
    )
    answered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['asked_at']

    def __str__(self):
        status = 'answered' if self.answer else 'pending'
        return f'Clarification on {self.solicitation.record.title} ({status})'


class PrequalificationApplicant(models.Model):
    """Expression-of-interest / prequalification tracking (integration
    framework step 07's other half). Staff-recorded on behalf of a vendor —
    no vendor accounts/self-service exist yet (still explicitly deferred
    elsewhere in the roadmap), so a procurement_unit member enters that a
    vendor applied and later records the outcome.

    Deliberately NOT gated by procurement_method: method names are
    law-profile-configured data (LawProfile.procurement_methods), not a
    fixed code enum, so hardcoding e.g. "Restricted Tendering" here would
    silently break for any other law profile using different terminology.
    Available on any published solicitation; staff simply use it when the
    method calls for prequalification.

    Recorded against the solicitation actually advertised — this is a
    simplification versus real two-stage tendering, where EOI/prequalification
    is often its own earlier advertisement stage with its own document. A
    future increment could model that as a separate stage if needed; for now
    this tracks applicants against the one Solicitation/Advertisement this
    system already models."""

    class Outcome(models.TextChoices):
        PENDING = 'pending', 'Pending'
        QUALIFIED = 'qualified', 'Qualified'
        NOT_QUALIFIED = 'not_qualified', 'Not Qualified'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    solicitation = models.ForeignKey(
        Solicitation, on_delete=models.PROTECT, related_name='prequalification_applicants'
    )
    vendor_name = models.CharField(max_length=255)
    vendor_registration_no = models.CharField(max_length=100, blank=True)
    outcome = models.CharField(max_length=16, choices=Outcome.choices, default=Outcome.PENDING)
    review_note = models.TextField(blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='prequalifications_recorded'
    )
    recorded_at = models.DateTimeField(auto_now_add=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name='prequalifications_reviewed',
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['recorded_at']

    def __str__(self):
        return f'{self.vendor_name} — {self.solicitation.record.title} ({self.get_outcome_display()})'


class Bid(models.Model):
    """Staff-recorded administrative log of bids received against a
    published Solicitation — NOT a submission channel. The encrypted bid
    vault (digital signatures, authorized opening, tamper-evident logs)
    remains entirely separate future work; this only records, after the
    bidding window has closed, what a Procurement Unit member received by
    other means (physical/email), so the Award decision below has an
    honest evidentiary trail to point at instead of a free-typed vendor
    name and amount."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    solicitation = models.ForeignKey(Solicitation, on_delete=models.PROTECT, related_name='bids')
    vendor_name = models.CharField(max_length=255)
    vendor_registration_no = models.CharField(max_length=100, blank=True)
    bid_amount = models.DecimalField(max_digits=16, decimal_places=2)
    is_responsive = models.BooleanField(
        default=True,
        help_text='Whether this bid meets the solicitation\'s eligibility/technical requirements. '
                   'A non-responsive bid is still recorded, not omitted — an honest record of what '
                   'was actually received — but cannot be selected as the winning bid.',
    )
    note = models.TextField(blank=True, help_text='e.g. reason for non-responsiveness.')
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='bids_recorded'
    )
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['recorded_at']

    def __str__(self):
        return f'{self.vendor_name}: ₦{self.bid_amount} ({self.solicitation.record.title})'


class TendersBoardReview(models.Model):
    """The Tenders Board's recommendation on a Solicitation's bids
    (blueprint steps 11-13 — evaluation and approval routing), the missing
    stage between "bids received" and "award decided." From this slice
    onward, services.award_solicitation() refuses to proceed without one —
    same principle as every other evidence-before-status-change gate in
    this app.

    Deliberately does not model per-bid numeric scoring as a separate
    table — evaluation_summary (the board's written rationale) serves as
    a lightweight stand-in for a full Bid Evaluation Report. There are no
    individual "board member" user accounts (same simplification already
    used for Bid/PrequalificationApplicant — staff-recorded, not a
    multi-user workflow), so this is recorded by one Tenders Board user on
    the board's behalf, with quorum_present as an explicit written
    attestation rather than derived from individual sign-offs.

    The Accounting Officer retains final discretion at Award — this gate
    only requires that independent evaluation happened and is on record,
    not that the eventual award must match the board's recommendation."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    solicitation = models.OneToOneField(Solicitation, on_delete=models.PROTECT, related_name='tenders_board_review')
    recommended_bid = models.ForeignKey(Bid, on_delete=models.PROTECT, related_name='+')
    evaluation_summary = models.TextField(help_text='The board\'s written rationale — public once an award exists.')
    quorum_present = models.BooleanField(default=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='tenders_board_reviews'
    )
    reviewed_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'Tenders Board review: {self.solicitation.record.title}'


class Award(models.Model):
    """The award decision for a Solicitation — the only sanctioned
    *service-layer* way a ProcurementRecord's vendor_name/
    vendor_registration_no/awarded_cost fields get set with a real
    evidentiary trail from Phase 3 onward (see services.award_solicitation).
    OneToOne: one award per solicitation, DB-enforced.

    Note: the pre-existing generic staff_status_transition view (Phase 1
    MVP, @login_required only, not role-gated) can still manually flip a
    record's status field to 'Awarded' from most other statuses without
    going through this model at all — a known, cross-cutting limitation
    predating this feature (same gap already applies to every other status
    value), not something this slice introduces or can fully close on its
    own. StatusTransitionForm's new exclusion (Advertised/Tendering ->
    Awarded) narrows this the same way the Phase 2 slice narrowed it for
    Advertised, but does not close it for every current-status starting
    point. A future hardening pass should either role-gate that view per
    transition or enforce the Award-must-exist invariant inside
    transition_status()/ProcurementRecord itself.

    Deliberately does not duplicate the award amount — record.awarded_cost
    is copied from winning_bid.bid_amount at award time, read live from
    the bid rather than re-entered (same principle as
    Solicitation.bpp_prior_review_required).

    decision_note is intentionally PUBLIC once an Award exists — unlike
    Clarification's raw question text or PrequalificationApplicant's
    review_note (both staff-only until/unless resolved), this is the
    award justification itself, which the integration framework's own
    disclosure rules require to be published for oversight."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    solicitation = models.OneToOneField(Solicitation, on_delete=models.PROTECT, related_name='award')
    winning_bid = models.ForeignKey(Bid, on_delete=models.PROTECT, related_name='+')
    decision_note = models.TextField(help_text='Award justification — public once decided.')
    bpp_no_objection_reference = models.CharField(max_length=100, blank=True)
    bpp_no_objection_date = models.DateField(null=True, blank=True)
    awarded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='awards_decided'
    )
    awarded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'Award: {self.solicitation.record.title} -> {self.winning_bid.vendor_name}'


class Complaint(models.Model):
    """Public complaint intake and resolution (blueprint Phase 3 —
    Approvals, complaints handling). Anchored on ProcurementRecord
    directly, not Solicitation — unlike Clarification/Bid, a complaint can
    be filed at any stage of a project (before advertisement, during
    tendering, or after award/implementation), not just while a tender is
    open.

    Visibility is deliberately NOT the same pattern as Clarification's
    "both sides become public once resolved": here, `description` (the
    complainant's own words) stays staff-only forever, even after
    resolution. A complaint is inherently accusatory — publishing raw,
    unverified allegations verbatim carries real reputational and
    retaliation risk regardless of outcome. Only `resolution_note` (the
    institution's own accountable output, same principle as Award's
    decision_note) and the resolved outcome (upheld/dismissed) are
    disclosed publicly, plus a pending count while unresolved.
    `complainant_name`/`complainant_contact` are never public — kept only
    so staff can follow up."""

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        UPHELD = 'upheld', 'Upheld'
        DISMISSED = 'dismissed', 'Dismissed'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    record = models.ForeignKey(ProcurementRecord, on_delete=models.PROTECT, related_name='complaints')
    complainant_name = models.CharField(max_length=255)
    complainant_contact = models.CharField(max_length=255, help_text='Email or phone — never shown publicly.')
    description = models.TextField(help_text='Staff-only, even once resolved — see class docstring.')
    submitted_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    resolution_note = models.TextField(blank=True, help_text='Public once resolved.')
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name='complaints_resolved',
    )
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['submitted_at']

    def __str__(self):
        return f'Complaint on {self.record.title} ({self.get_status_display()})'


class Contract(models.Model):
    """Formalizes an Award into a signed contract (blueprint Phase 4 —
    Contract lifecycle). The only sanctioned way a ProcurementRecord
    reaches Implementation status from Phase 4 onward (see
    services.sign_contract). OneToOne on Award: one contract per award,
    DB-enforced.

    Deliberately does not store its own contract value — read live from
    award.winning_bid.bid_amount (same "don't duplicate, read live"
    principle already used by Solicitation.bpp_prior_review_required and
    Award's own amount handling). A future increment could add a distinct
    negotiated final value if institutions need one; not built now to
    avoid a second, potentially-divergent source of truth without a
    concrete need for it yet."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    award = models.OneToOneField(Award, on_delete=models.PROTECT, related_name='contract')
    contract_reference = models.CharField(max_length=100, unique=True)
    start_date = models.DateField()
    end_date = models.DateField()
    vendor_signatory_name = models.CharField(max_length=255)
    signed_date = models.DateField()
    signed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='contracts_signed'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def clean(self):
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValidationError({'end_date': 'End date must be on or after the start date.'})

    def __str__(self):
        return f'Contract {self.contract_reference} ({self.award.solicitation.record.title})'


class Milestone(models.Model):
    """A delivery milestone against a signed Contract. Unlike Bid/
    Clarification, milestones are public from creation, not hidden until
    resolved — a delivery timeline isn't competitively sensitive the way
    bid amounts or evaluation are, and visibility into whether a
    contractor is on schedule is itself a transparency goal."""

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        COMPLETED = 'completed', 'Completed'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    contract = models.ForeignKey(Contract, on_delete=models.PROTECT, related_name='milestones')
    description = models.CharField(max_length=255)
    due_date = models.DateField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    completion_note = models.TextField(
        blank=True, help_text='Required when marking complete — evidence of inspection/verification.'
    )
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name='milestones_completed',
    )
    completed_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='milestones_created'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['due_date']

    def __str__(self):
        return f'{self.description} ({self.get_status_display()})'


class PerformanceGuarantee(models.Model):
    """Post-award performance security (blueprint Phase 4 — "guarantees").
    Distinct from Solicitation.bid_security_* (submitted WITH a bid, to
    guarantee bid seriousness): this is the security the winning vendor
    posts after contract signing, to guarantee contract performance.

    Conditionally required, not unconditionally: services.complete_contract()
    refuses to proceed without one when the underlying Solicitation had
    bid_security_required=True — the same "if bid security mattered here,
    performance security matters too" logic real procurement practice
    follows, since (unlike BPP No-Objection) there is no ThresholdRule-style
    statutory table this specific gate could consult instead."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    contract = models.OneToOneField(Contract, on_delete=models.PROTECT, related_name='performance_guarantee')
    guarantee_type = models.CharField(max_length=100, help_text='e.g. Bank guarantee, insurance bond, cash deposit.')
    issuing_institution = models.CharField(max_length=255, help_text='Staff-only — see public disclosure notes.')
    reference_number = models.CharField(max_length=100, help_text='Staff-only — see public disclosure notes.')
    amount = models.DecimalField(max_digits=16, decimal_places=2)
    expiry_date = models.DateField()
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='performance_guarantees_verified'
    )
    verified_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.guarantee_type} for {self.contract.contract_reference}'


class ContractCompletion(models.Model):
    """Final acceptance sign-off on a Contract — the only sanctioned way a
    ProcurementRecord reaches Completed status (see services.
    complete_contract). OneToOne on Contract: one completion record per
    contract, DB-enforced. Decided by the Accounting Officer (final
    institutional sign-off, same authority level as Award and Complaint
    resolution) — Procurement Unit continues to own day-to-day execution
    (bids, contract signing, milestones).

    inspection_note is intentionally PUBLIC, same principle as Award's
    decision_note — it's the institution's own accountable acceptance
    record, not a third party's raw submission."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    contract = models.OneToOneField(Contract, on_delete=models.PROTECT, related_name='completion')
    completion_date = models.DateField()
    inspection_note = models.TextField(help_text='Final acceptance summary — public once recorded.')
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='contract_completions_decided'
    )
    completed_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'Completion: {self.contract.contract_reference}'


class Invoice(models.Model):
    """A vendor invoice against a signed Contract (blueprint Phase 4 —
    "invoices, payments"). Submitted by Procurement Unit (day-to-day
    administration, same tier as milestones/contract signing), reviewed by
    Finance — Finance's own blueprint role is literally "confirm
    appropriation, reserve funds, validate invoices and process authorised
    payment" (Roles and Permissions table), so this maps directly onto an
    existing role rather than inventing a new one.

    `milestone` is optional, not required: a mobilisation/advance invoice
    may have no completed milestone behind it yet; a subsequent interim
    invoice normally does — see clean() for the one thing that IS enforced
    (a linked milestone must actually belong to this contract and be
    completed, matching "post-mobilisation payment requires the applicable
    interim performance certificate")."""

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        APPROVED = 'approved', 'Approved'
        REJECTED = 'rejected', 'Rejected'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    contract = models.ForeignKey(Contract, on_delete=models.PROTECT, related_name='invoices')
    milestone = models.ForeignKey(
        Milestone, on_delete=models.PROTECT, null=True, blank=True, related_name='invoices',
        help_text='Optional — the completed milestone this invoice corresponds to, if any.',
    )
    invoice_number = models.CharField(max_length=100)
    amount = models.DecimalField(max_digits=16, decimal_places=2)
    submitted_date = models.DateField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='invoices_submitted'
    )
    submitted_at = models.DateTimeField(auto_now_add=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name='invoices_reviewed'
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True)

    class Meta:
        ordering = ['submitted_at']

    def clean(self):
        if self.milestone_id and self.contract_id and self.milestone.contract_id != self.contract_id:
            raise ValidationError({'milestone': 'This milestone does not belong to the selected contract.'})
        if self.milestone_id and self.milestone.status != Milestone.Status.COMPLETED:
            raise ValidationError({'milestone': 'The linked milestone must be completed first.'})

    def __str__(self):
        return f'Invoice {self.invoice_number} ({self.get_status_display()})'


class Payment(models.Model):
    """A disbursement against an approved Invoice — one payment per
    invoice (partial/split payments against a single invoice aren't
    modeled; a vendor would submit a separate invoice for each tranche).
    Recorded by Finance, matching Invoice review.

    Public from creation, same as Milestone/Invoice — per the blueprint's
    own Public Disclosure table, "Implementation" stage publishes
    "milestones, progress, payments, variations..."; payment data isn't
    competitively sensitive the way bid/evaluation data is."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    invoice = models.OneToOneField(Invoice, on_delete=models.PROTECT, related_name='payment')
    amount = models.DecimalField(max_digits=16, decimal_places=2)
    payment_date = models.DateField()
    payment_reference = models.CharField(max_length=100)
    paid_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='payments_recorded')
    paid_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'Payment {self.payment_reference} for {self.invoice.invoice_number}'
