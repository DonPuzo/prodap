from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import (
    Advertisement,
    AuditEvent,
    Award,
    Bid,
    Clarification,
    Complaint,
    Contract,
    ContractCompletion,
    FinancialYear,
    Invoice,
    LawProfile,
    Milestone,
    Payment,
    PerformanceGuarantee,
    PlanLine,
    PrequalificationApplicant,
    ProcessIdentifierSequence,
    ProcurementPlan,
    ProcurementRecord,
    RecordFlag,
    Requisition,
    Solicitation,
    StatusUpdate,
    TendersBoardReview,
    ThresholdRule,
    User,
)


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    fieldsets = DjangoUserAdmin.fieldsets + (
        (None, {'fields': ('role',)}),
    )
    list_display = ('username', 'email', 'role', 'is_staff')


@admin.register(LawProfile)
class LawProfileAdmin(admin.ModelAdmin):
    list_display = ('slug', 'governing_law', 'jurisdiction_type', 'regulating_body')


class StatusUpdateInline(admin.TabularInline):
    model = StatusUpdate
    extra = 0
    readonly_fields = ('id', 'old_status', 'new_status', 'note', 'updated_by', 'updated_at')
    can_delete = False

    def has_add_permission(self, request, obj=None):
        # status_updates must only ever be created via services.transition_status,
        # never hand-entered — see PRODAP_AGENT_BUILD_PROMPT_V2.md section 3.6.
        return False


class RecordFlagInline(admin.TabularInline):
    model = RecordFlag
    extra = 0
    readonly_fields = ('id', 'note', 'created_at')
    can_delete = True

    def has_add_permission(self, request, obj=None):
        # flags come from the public flag_record view only.
        return False


@admin.register(ProcurementRecord)
class ProcurementRecordAdmin(admin.ModelAdmin):
    list_display = ('title', 'department', 'status', 'budget_source', 'display_cost', 'flag_count', 'cost_outlier')
    list_filter = ('status', 'budget_source', 'department')
    search_fields = ('title', 'vendor_name')
    readonly_fields = ('status', 'created_at', 'updated_at')
    inlines = [StatusUpdateInline, RecordFlagInline]

    @admin.display(description='Flags')
    def flag_count(self, obj):
        return obj.flags.count()

    @admin.display(description='Cost check', boolean=True)
    def cost_outlier(self, obj):
        return obj.is_cost_outlier()


@admin.register(FinancialYear)
class FinancialYearAdmin(admin.ModelAdmin):
    list_display = ('label', 'law_profile', 'start_date', 'end_date', 'is_current')
    list_filter = ('law_profile', 'is_current')


@admin.register(ThresholdRule)
class ThresholdRuleAdmin(admin.ModelAdmin):
    list_display = (
        'procurement_method', 'law_profile', 'min_value', 'max_value',
        'approving_authority', 'effective_from', 'effective_to', 'is_active',
    )
    list_filter = ('law_profile', 'procurement_method', 'is_active')
    readonly_fields = ('created_by', 'created_at')

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(ProcessIdentifierSequence)
class ProcessIdentifierSequenceAdmin(admin.ModelAdmin):
    list_display = ('law_profile', 'financial_year', 'last_value')
    readonly_fields = ('law_profile', 'financial_year', 'last_value')

    def has_add_permission(self, request):
        # created only by services._next_process_identifier()
        return False


class PlanLineInline(admin.TabularInline):
    model = PlanLine
    extra = 0
    readonly_fields = (
        'department', 'item_description', 'justification', 'quantity', 'estimated_cost',
        'budget_line', 'status', 'is_amendment', 'proposed_by', 'created_at',
    )
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(ProcurementPlan)
class ProcurementPlanAdmin(admin.ModelAdmin):
    list_display = ('financial_year', 'law_profile', 'status', 'prepared_by', 'approved_by')
    list_filter = ('status', 'law_profile')
    readonly_fields = ('status', 'submitted_at', 'approved_by', 'approved_at', 'created_at', 'updated_at')
    inlines = [PlanLineInline]


@admin.register(Requisition)
class RequisitionAdmin(admin.ModelAdmin):
    list_display = (
        'title', 'process_identifier', 'department', 'requested_value', 'status',
        'requested_by', 'determined_method',
    )
    list_filter = ('status', 'department')
    search_fields = ('title', 'process_identifier')
    readonly_fields = (
        'process_identifier', 'status', 'funds_confirmed_by', 'funds_confirmed_at',
        'packaging_reviewed', 'packaging_reviewed_by', 'packaging_reviewed_at',
        'threshold_rule', 'determined_method', 'determined_approving_authority',
        'bpp_prior_review_required', 'created_at', 'updated_at',
    )


@admin.register(Solicitation)
class SolicitationAdmin(admin.ModelAdmin):
    """Every field here is either service-written-once (prepare_solicitation/
    approve_solicitation/reject_solicitation) or must stay immutable after
    approval for the audit trail to mean anything — there is no legitimate
    post-creation admin edit path, so it's locked down fully from day one."""

    list_display = ('record', 'version', 'status', 'prepared_by', 'approved_by')
    list_filter = ('status',)
    readonly_fields = [f.name for f in Solicitation._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Advertisement)
class AdvertisementAdmin(admin.ModelAdmin):
    list_display = ('solicitation', 'closing_date', 'published_by', 'published_at')
    readonly_fields = [f.name for f in Advertisement._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Clarification)
class ClarificationAdmin(admin.ModelAdmin):
    """Every field is either public-submitted (question) or service-written-
    once via answer_clarification() — no legitimate admin edit path, same
    posture as SolicitationAdmin/AdvertisementAdmin."""

    list_display = ('solicitation', 'asked_at', 'answered_by', 'answered_at')
    list_filter = ('solicitation',)
    readonly_fields = [f.name for f in Clarification._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PrequalificationApplicant)
class PrequalificationApplicantAdmin(admin.ModelAdmin):
    """Every field is either staff-entered-once (record_prequalification_applicant)
    or service-written-once via review_prequalification_applicant() — same
    lockdown posture as SolicitationAdmin/AdvertisementAdmin/ClarificationAdmin."""

    list_display = ('vendor_name', 'solicitation', 'outcome', 'recorded_by', 'reviewed_by')
    list_filter = ('outcome',)
    readonly_fields = [f.name for f in PrequalificationApplicant._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Bid)
class BidAdmin(admin.ModelAdmin):
    """Every field is staff-entered-once via services.record_bid() — no
    legitimate admin edit path, same lockdown posture as every sibling
    admin added this session."""

    list_display = ('vendor_name', 'solicitation', 'bid_amount', 'is_responsive', 'recorded_by')
    list_filter = ('is_responsive',)
    readonly_fields = [f.name for f in Bid._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(TendersBoardReview)
class TendersBoardReviewAdmin(admin.ModelAdmin):
    """Every field is service-written-once via
    services.record_tenders_board_review() — no legitimate admin edit path."""

    list_display = ('solicitation', 'recommended_bid', 'quorum_present', 'reviewed_by', 'reviewed_at')
    readonly_fields = [f.name for f in TendersBoardReview._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Award)
class AwardAdmin(admin.ModelAdmin):
    """Every field is service-written-once via services.award_solicitation()
    — no legitimate admin edit path."""

    list_display = ('solicitation', 'winning_bid', 'awarded_by', 'awarded_at')
    readonly_fields = [f.name for f in Award._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Complaint)
class ComplaintAdmin(admin.ModelAdmin):
    """Every field is either public-submitted (complainant_name/contact/
    description) or service-written-once via resolve_complaint() — no
    legitimate admin edit path. Admin can still see complainant contact
    details for legitimate follow-up (superuser/staff already have
    inherent trust in this single-institution deployment)."""

    list_display = ('record', 'complainant_name', 'status', 'submitted_at', 'resolved_by')
    list_filter = ('status',)
    readonly_fields = [f.name for f in Complaint._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Contract)
class ContractAdmin(admin.ModelAdmin):
    """Every field is service-written-once via services.sign_contract() —
    no legitimate admin edit path."""

    list_display = ('contract_reference', 'award', 'signed_date', 'signed_by')
    readonly_fields = [f.name for f in Contract._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Milestone)
class MilestoneAdmin(admin.ModelAdmin):
    """Every field is service-written via services.add_milestone()/
    complete_milestone() — no legitimate admin edit path."""

    list_display = ('description', 'contract', 'due_date', 'status', 'completed_by')
    list_filter = ('status',)
    readonly_fields = [f.name for f in Milestone._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PerformanceGuarantee)
class PerformanceGuaranteeAdmin(admin.ModelAdmin):
    """Every field is service-written-once via
    services.record_performance_guarantee() — no legitimate admin edit path."""

    list_display = ('contract', 'guarantee_type', 'amount', 'expiry_date', 'verified_by')
    readonly_fields = [f.name for f in PerformanceGuarantee._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    """Every field is either service-written-once (submit_invoice) or
    service-mutated-once (review_invoice) — no legitimate admin edit path."""

    list_display = ('invoice_number', 'contract', 'amount', 'status', 'submitted_by', 'reviewed_by')
    list_filter = ('status',)
    readonly_fields = [f.name for f in Invoice._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    """Every field is service-written-once via services.record_payment()
    — no legitimate admin edit path."""

    list_display = ('payment_reference', 'invoice', 'amount', 'payment_date', 'paid_by')
    readonly_fields = [f.name for f in Payment._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ContractCompletion)
class ContractCompletionAdmin(admin.ModelAdmin):
    """Every field is service-written-once via services.complete_contract()
    — no legitimate admin edit path."""

    list_display = ('contract', 'completion_date', 'completed_by', 'completed_at')
    readonly_fields = [f.name for f in ContractCompletion._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ('action', 'content_type', 'object_id', 'actor', 'role_at_time', 'created_at')
    list_filter = ('action', 'content_type')
    readonly_fields = [f.name for f in AuditEvent._meta.fields]

    def has_add_permission(self, request):
        # every AuditEvent is written by services.log_audit_event() only.
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
