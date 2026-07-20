import datetime

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import models as django_models
from django.db import transaction
from django.utils import timezone

from .models import (
    Advertisement,
    AuditEvent,
    Clarification,
    PlanLine,
    ProcessIdentifierSequence,
    ProcurementPlan,
    ProcurementRecord,
    Requisition,
    Solicitation,
    StatusUpdate,
    ThresholdRule,
)


class SeparationOfDutiesError(ValidationError):
    """Raised when a user tries to approve/confirm their own request —
    enforces "one person must not request, evaluate, approve and authorise
    payment on the same transaction" independent of role assignment."""


def transition_status(*, record, new_status, updated_by, note=''):
    """The only sanctioned way to change a ProcurementRecord's status.

    Writes an audit row and updates the record in the same transaction —
    never allow a direct status field update that bypasses this (see
    PRODAP_AGENT_BUILD_PROMPT_V2.md section 3.6).
    """
    old_status = record.status
    with transaction.atomic():
        record.status = new_status
        record.save(update_fields=['status', 'updated_at'])
        StatusUpdate.objects.create(
            record=record,
            old_status=old_status,
            new_status=new_status,
            note=note,
            updated_by=updated_by,
        )
    return record


# --- Phase 1-Foundation: annual plans -> requisitions -> funds confirmation
# -> packaging review -> method/threshold determination -> record creation.
# Every gate-crossing write goes through a function here inside
# transaction.atomic() and logs one AuditEvent row, matching the discipline
# established by transition_status()/StatusUpdate above. ---


def log_audit_event(*, target, action, actor, reason='', old_value=None, new_value=None):
    return AuditEvent.objects.create(
        content_type=ContentType.objects.get_for_model(target),
        object_id=target.pk,
        action=action,
        actor=actor,
        role_at_time=actor.role,
        reason=reason,
        old_value=old_value,
        new_value=new_value,
    )


def submit_plan(*, plan, actor):
    with transaction.atomic():
        plan.status = ProcurementPlan.Status.SUBMITTED
        plan.submitted_at = timezone.now()
        plan.save(update_fields=['status', 'submitted_at'])
        log_audit_event(target=plan, action=AuditEvent.Action.PLAN_SUBMITTED, actor=actor)
    return plan


def approve_plan(*, plan, actor, note=''):
    if actor.pk == plan.prepared_by_id:
        raise SeparationOfDutiesError('You cannot approve a plan you prepared yourself.')
    with transaction.atomic():
        plan.status = ProcurementPlan.Status.APPROVED
        plan.approved_by = actor
        plan.approved_at = timezone.now()
        plan.save(update_fields=['status', 'approved_by', 'approved_at'])
        # Bulk-approve the plan's own (non-amendment) pending lines; a line
        # added later to an already-approved plan (is_amendment=True) stays
        # pending until individually approved via approve_plan_line().
        plan.lines.filter(status=PlanLine.Status.PENDING, is_amendment=False).update(
            status=PlanLine.Status.APPROVED
        )
        log_audit_event(target=plan, action=AuditEvent.Action.PLAN_APPROVED, actor=actor, reason=note)
    return plan


def reject_plan(*, plan, actor, reason):
    if not reason.strip():
        raise ValidationError('A reason is required to reject a plan.')
    with transaction.atomic():
        plan.status = ProcurementPlan.Status.REJECTED
        plan.rejected_reason = reason
        plan.save(update_fields=['status', 'rejected_reason'])
        log_audit_event(target=plan, action=AuditEvent.Action.PLAN_REJECTED, actor=actor, reason=reason)
    return plan


def _require_plan_line_pending(plan_line):
    if plan_line.status != PlanLine.Status.PENDING:
        raise ValidationError(
            f'This plan line is already {plan_line.get_status_display().lower()} — '
            'only a pending line can be approved or rejected.'
        )


def _require_plan_line_still_approved(plan_line):
    """Every downstream gate re-checks this, not just submit_requisition —
    a line can be rejected (or superseded) after a requisition was created
    against it but before that requisition finished its own gates, and
    every later step must refuse to proceed on a line that is no longer
    approved (security review finding, Phase 1-Foundation)."""
    if plan_line.status != PlanLine.Status.APPROVED:
        raise ValidationError(
            f'The plan line behind this requisition is {plan_line.get_status_display().lower()}, '
            'not approved — this requisition cannot proceed.'
        )


def approve_plan_line(*, plan_line, actor, note=''):
    _require_plan_line_pending(plan_line)
    if actor.pk == plan_line.proposed_by_id:
        raise SeparationOfDutiesError('You cannot approve a plan line you proposed yourself.')
    with transaction.atomic():
        plan_line.status = PlanLine.Status.APPROVED
        plan_line.save(update_fields=['status'])
        log_audit_event(target=plan_line, action=AuditEvent.Action.PLAN_LINE_APPROVED, actor=actor, reason=note)
    return plan_line


def reject_plan_line(*, plan_line, actor, reason):
    if not reason.strip():
        raise ValidationError('A reason is required to reject a plan line.')
    _require_plan_line_pending(plan_line)
    with transaction.atomic():
        plan_line.status = PlanLine.Status.REJECTED
        plan_line.save(update_fields=['status'])
        log_audit_event(target=plan_line, action=AuditEvent.Action.PLAN_LINE_REJECTED, actor=actor, reason=reason)
    return plan_line


def submit_requisition(*, requisition, actor):
    if requisition.status != Requisition.Status.DRAFT:
        raise ValidationError(f'This requisition is already {requisition.get_status_display().lower()}.')
    _require_plan_line_still_approved(requisition.plan_line)
    with transaction.atomic():
        requisition.status = Requisition.Status.SUBMITTED
        requisition.submitted_at = timezone.now()
        requisition.save(update_fields=['status', 'submitted_at'])
        log_audit_event(target=requisition, action=AuditEvent.Action.REQUISITION_SUBMITTED, actor=actor)
    return requisition


def _next_process_identifier(*, law_profile, financial_year):
    """Race-safe under concurrent requests — select_for_update locks the
    counter row for the duration of the enclosing transaction."""
    seq, _ = ProcessIdentifierSequence.objects.select_for_update().get_or_create(
        law_profile=law_profile, financial_year=financial_year
    )
    seq.last_value += 1
    seq.save(update_fields=['last_value'])
    return f'{law_profile.slug.upper()}-{financial_year.label}-{seq.last_value:05d}'


def confirm_requisition_funds(*, requisition, actor, note=''):
    if requisition.status != Requisition.Status.SUBMITTED:
        raise ValidationError(
            f'This requisition is {requisition.get_status_display().lower()}, not submitted — '
            'it must be submitted before funds can be confirmed.'
        )
    _require_plan_line_still_approved(requisition.plan_line)
    if actor.pk == requisition.requested_by_id:
        raise SeparationOfDutiesError('You cannot confirm funds for a requisition you created yourself.')
    with transaction.atomic():
        requisition.process_identifier = _next_process_identifier(
            law_profile=requisition.plan_line.plan.law_profile,
            financial_year=requisition.plan_line.plan.financial_year,
        )
        requisition.status = Requisition.Status.FUNDS_CONFIRMED
        requisition.funds_confirmed_by = actor
        requisition.funds_confirmed_at = timezone.now()
        requisition.funds_confirmation_note = note
        requisition.save(update_fields=[
            'status', 'funds_confirmed_by', 'funds_confirmed_at',
            'funds_confirmation_note', 'process_identifier',
        ])
        log_audit_event(target=requisition, action=AuditEvent.Action.FUNDS_CONFIRMED, actor=actor, reason=note)
    return requisition


def decline_requisition_funds(*, requisition, actor, reason):
    if not reason.strip():
        raise ValidationError('A reason is required to decline funding.')
    if requisition.status != Requisition.Status.SUBMITTED:
        raise ValidationError(f'This requisition is {requisition.get_status_display().lower()}, not submitted.')
    with transaction.atomic():
        requisition.status = Requisition.Status.FUNDS_DECLINED
        requisition.funds_confirmation_note = reason
        requisition.save(update_fields=['status', 'funds_confirmation_note'])
        log_audit_event(target=requisition, action=AuditEvent.Action.FUNDS_DECLINED, actor=actor, reason=reason)
    return requisition


def review_requisition_packaging(*, requisition, actor, note):
    """The Foundation-phase anti-splitting control: a required written
    decision, not automated pattern detection (that's Phase 5)."""
    if not note.strip():
        raise ValidationError('A written packaging/anti-splitting review note is required.')
    if requisition.status != Requisition.Status.FUNDS_CONFIRMED:
        raise ValidationError('Funds must be confirmed before the packaging/anti-splitting review.')
    _require_plan_line_still_approved(requisition.plan_line)
    with transaction.atomic():
        requisition.packaging_reviewed = True
        requisition.packaging_review_note = note
        requisition.packaging_reviewed_by = actor
        requisition.packaging_reviewed_at = timezone.now()
        requisition.save(update_fields=[
            'packaging_reviewed', 'packaging_review_note',
            'packaging_reviewed_by', 'packaging_reviewed_at',
        ])
        log_audit_event(target=requisition, action=AuditEvent.Action.PACKAGING_REVIEWED, actor=actor, reason=note)
    return requisition


def find_similar_requisitions(requisition, lookback_days=90):
    """Read-only aid for the packaging reviewer — same department within
    the lookback window. Informational only, never blocks automatically."""
    cutoff = timezone.now() - datetime.timedelta(days=lookback_days)
    return Requisition.objects.filter(
        department=requisition.department, created_at__gte=cutoff,
    ).exclude(pk=requisition.pk).select_related('plan_line').order_by('-created_at')[:10]


def get_threshold_rules(*, law_profile, value, as_of=None, method=None):
    as_of = as_of or timezone.localdate()
    qs = ThresholdRule.objects.filter(
        law_profile=law_profile,
        is_active=True,
        effective_from__lte=as_of,
        min_value__lte=value,
    ).filter(
        django_models.Q(max_value__isnull=True) | django_models.Q(max_value__gte=value)
    ).filter(
        django_models.Q(effective_to__isnull=True) | django_models.Q(effective_to__gte=as_of)
    )
    if method:
        qs = qs.filter(procurement_method=method)
    return qs.order_by('-effective_from')


def determine_default_method(*, law_profile, value, as_of=None):
    rule = get_threshold_rules(law_profile=law_profile, value=value, as_of=as_of).filter(
        is_default_for_range=True
    ).first()
    if not rule:
        raise ValidationError(
            'No default threshold rule covers this value — configure ThresholdRule '
            'data for this law profile before proceeding.'
        )
    return rule


def get_approving_authority(*, law_profile, method, value, as_of=None):
    rule = get_threshold_rules(law_profile=law_profile, value=value, as_of=as_of, method=method).first()
    if not rule:
        raise ValidationError(f'No active threshold rule covers "{method}" at this value.')
    return rule


def determine_requisition_method(*, requisition, actor, method_override='', override_justification=''):
    if requisition.status != Requisition.Status.FUNDS_CONFIRMED:
        raise ValidationError('Funds must be confirmed before method determination.')
    if not requisition.packaging_reviewed:
        raise ValidationError('Packaging/anti-splitting review must be completed before method determination.')
    _require_plan_line_still_approved(requisition.plan_line)
    law_profile = requisition.plan_line.plan.law_profile
    if method_override:
        if not override_justification.strip():
            raise ValidationError('An overridden procurement method requires a written justification.')
        rule = get_approving_authority(
            law_profile=law_profile, method=method_override, value=requisition.requested_value
        )
        action = AuditEvent.Action.METHOD_OVERRIDDEN
    else:
        rule = determine_default_method(law_profile=law_profile, value=requisition.requested_value)
        override_justification = ''
        action = AuditEvent.Action.METHOD_DETERMINED
    with transaction.atomic():
        requisition.threshold_rule = rule
        requisition.determined_method = rule.procurement_method
        requisition.determined_approving_authority = rule.approving_authority
        requisition.bpp_prior_review_required = rule.bpp_prior_review_required
        requisition.method_override = method_override
        requisition.method_override_justification = override_justification
        requisition.save(update_fields=[
            'threshold_rule', 'determined_method', 'determined_approving_authority',
            'bpp_prior_review_required', 'method_override', 'method_override_justification',
        ])
        log_audit_event(
            target=requisition, action=action, actor=actor, reason=override_justification,
            new_value={'method': rule.procurement_method, 'approving_authority': rule.approving_authority},
        )
    return requisition


def create_record_from_requisition(*, requisition, actor, record_fields):
    """The only sanctioned way a ProcurementRecord may be created from
    Phase 1-Foundation onward — replaces free-form ad hoc creation.
    record_fields supplies the human-entered fields only (title,
    description, location, dates, vendor); everything traceable to the
    requisition (department, budget_source, estimated_cost, procurement_method,
    law_profile) is taken from the requisition itself, not re-entered."""
    if requisition.status != Requisition.Status.FUNDS_CONFIRMED:
        raise ValidationError('Funds must be confirmed before a procurement record can be created.')
    if not requisition.packaging_reviewed:
        raise ValidationError('Packaging/anti-splitting review must be completed first.')
    if not requisition.determined_method:
        raise ValidationError('Procurement method must be determined first.')
    _require_plan_line_still_approved(requisition.plan_line)
    with transaction.atomic():
        record = ProcurementRecord.objects.create(
            requisition=requisition,
            department=requisition.department,
            budget_source=requisition.budget_source,
            estimated_cost=requisition.requested_value,
            procurement_method=requisition.determined_method,
            law_profile=requisition.plan_line.plan.law_profile,
            created_by=actor,
            **record_fields,
        )
        requisition.status = Requisition.Status.RECORD_CREATED
        requisition.save(update_fields=['status'])
        log_audit_event(
            target=requisition, action=AuditEvent.Action.RECORD_CREATED_FROM_REQUISITION, actor=actor,
            new_value={'record_id': str(record.id)},
        )
    return record


# --- Phase 2 (non-cryptographic slice): solicitation preparation ->
# advertisement/publication. Every gate function re-validates the full
# precondition chain (record status, solicitation status), not just the
# immediately-prior step, matching the defense-in-depth discipline added to
# the Requisition gates above. ---


def get_current_solicitation(record):
    """Latest non-rejected version for a record, or None. Read-only helper
    shared by staff and public views."""
    return record.solicitations.exclude(status=Solicitation.Status.REJECTED).order_by('-version').first()


def get_published_advertisement(record):
    return Advertisement.objects.filter(solicitation__record=record).select_related(
        'solicitation'
    ).order_by('-solicitation__version').first()


def prepare_solicitation(*, record, actor, fields):
    if record.status != ProcurementRecord.Status.PLANNING:
        raise ValidationError(f'This record is {record.status}, not Planning — a solicitation cannot be prepared.')
    current = get_current_solicitation(record)
    if current is not None:
        raise ValidationError(
            f'This record already has a solicitation in {current.get_status_display()} status — '
            'reject it before preparing a new version.'
        )
    with transaction.atomic():
        next_version = (
            record.solicitations.aggregate(django_models.Max('version'))['version__max'] or 0
        ) + 1
        solicitation = Solicitation.objects.create(
            record=record, version=next_version, prepared_by=actor, **fields
        )
        log_audit_event(target=solicitation, action=AuditEvent.Action.SOLICITATION_PREPARED, actor=actor)
    return solicitation


def approve_solicitation(*, solicitation, actor, note=''):
    if solicitation.status != Solicitation.Status.DRAFT:
        raise ValidationError(f'This solicitation is already {solicitation.get_status_display().lower()}.')
    if actor.pk == solicitation.prepared_by_id:
        raise SeparationOfDutiesError('You cannot approve a solicitation you prepared yourself.')
    if solicitation.record.status != ProcurementRecord.Status.PLANNING:
        raise ValidationError('The record behind this solicitation is no longer in Planning.')
    with transaction.atomic():
        solicitation.status = Solicitation.Status.APPROVED
        solicitation.approved_by = actor
        solicitation.approved_at = timezone.now()
        solicitation.save(update_fields=['status', 'approved_by', 'approved_at'])
        log_audit_event(target=solicitation, action=AuditEvent.Action.SOLICITATION_APPROVED, actor=actor, reason=note)
    return solicitation


def reject_solicitation(*, solicitation, actor, reason):
    if not reason.strip():
        raise ValidationError('A reason is required to reject a solicitation.')
    if solicitation.status != Solicitation.Status.DRAFT:
        raise ValidationError(f'This solicitation is already {solicitation.get_status_display().lower()}.')
    with transaction.atomic():
        solicitation.status = Solicitation.Status.REJECTED
        solicitation.rejected_reason = reason
        solicitation.save(update_fields=['status', 'rejected_reason'])
        log_audit_event(target=solicitation, action=AuditEvent.Action.SOLICITATION_REJECTED, actor=actor, reason=reason)
    return solicitation


def publish_advertisement(*, solicitation, actor, channels, publication_proof, closing_date):
    if solicitation.status != Solicitation.Status.APPROVED:
        raise ValidationError('The solicitation must be approved before it can be advertised.')
    record = solicitation.record
    if record.status != ProcurementRecord.Status.PLANNING:
        raise ValidationError(f'This record is {record.status}, not Planning — it cannot be advertised.')
    if hasattr(solicitation, 'advertisement'):
        raise ValidationError('This solicitation has already been advertised.')

    minimum_days = record.law_profile.default_minimum_bidding_days
    published_at = timezone.now()
    earliest_closing = published_at.date() + datetime.timedelta(days=minimum_days)
    if closing_date < earliest_closing:
        raise ValidationError({
            'closing_date': (
                f'Closing date must be at least {minimum_days} days after publication '
                f'({earliest_closing}), per institutional policy.'
            )
        })

    with transaction.atomic():
        advertisement = Advertisement.objects.create(
            solicitation=solicitation, channels=channels, publication_proof=publication_proof,
            closing_date=closing_date, minimum_bidding_days_applied=minimum_days, published_by=actor,
        )
        log_audit_event(
            target=advertisement, action=AuditEvent.Action.ADVERTISEMENT_PUBLISHED, actor=actor,
            new_value={'closing_date': closing_date.isoformat(), 'channels': channels},
        )
        # Same transaction — StatusUpdate stays the single source of truth
        # for ProcurementRecord.status history; the AuditEvent above covers
        # the advertisement-specific gate crossing separately.
        transition_status(
            record=record, new_status=ProcurementRecord.Status.ADVERTISED, updated_by=actor,
            note=f'Advertised via {", ".join(channels)}; closes {closing_date}.',
        )
    return advertisement


# --- Clarifications & Addenda (blueprint step 08): public Q&A on a
# published Solicitation. submit_clarification_question() is anonymous —
# no actor, no audit event — matching flag_record's existing precedent for
# public-submitted content. answer_clarification() is the one sanctioned
# staff action and IS audited, same as every other gate crossing above. ---


def submit_clarification_question(*, record, question):
    if not question.strip():
        raise ValidationError('A question is required.')
    solicitation = get_current_solicitation(record)
    if solicitation is None or solicitation.status != Solicitation.Status.APPROVED or not hasattr(solicitation, 'advertisement'):
        raise ValidationError('This record does not have a published solicitation to ask about.')
    if timezone.localdate() > solicitation.advertisement.closing_date:
        raise ValidationError('The bidding period for this solicitation has closed.')
    return Clarification.objects.create(solicitation=solicitation, question=question.strip())


def answer_clarification(*, clarification, actor, answer):
    if not answer.strip():
        raise ValidationError('An answer is required.')
    if clarification.answer:
        raise ValidationError('This clarification has already been answered.')
    with transaction.atomic():
        clarification.answer = answer
        clarification.answered_by = actor
        clarification.answered_at = timezone.now()
        clarification.save(update_fields=['answer', 'answered_by', 'answered_at'])
        log_audit_event(target=clarification, action=AuditEvent.Action.CLARIFICATION_ANSWERED, actor=actor)
    return clarification
