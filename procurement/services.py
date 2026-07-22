import datetime

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import models as django_models
from django.db import transaction
from django.utils import timezone

from .models import (
    Abandonment,
    Advertisement,
    AuditEvent,
    Award,
    Bid,
    Clarification,
    Complaint,
    Contract,
    ContractCompletion,
    Invoice,
    Milestone,
    Payment,
    PerformanceGuarantee,
    PlanLine,
    PrequalificationApplicant,
    ProcessIdentifierSequence,
    ProcurementPlan,
    ProcurementRecord,
    Requisition,
    Solicitation,
    StatusUpdate,
    TendersBoardReview,
    ThresholdRule,
)


class SeparationOfDutiesError(ValidationError):
    """Raised when a user tries to approve/confirm their own request —
    enforces "one person must not request, evaluate, approve and authorise
    payment on the same transaction" independent of role assignment."""


def _require_no_pending_complaint(record):
    """Blueprint acceptance checklist: "Complaints and suspension
    instructions freeze the affected workflow." An unresolved complaint
    blocks every status transition on its record — called both here
    (the universal choke point every transition passes through, manual or
    evidence-gated) and, fail-fast, at the top of award_solicitation/
    sign_contract/complete_contract so the error surfaces before any work
    is done."""
    if record.complaints.filter(status=Complaint.Status.PENDING).exists():
        raise ValidationError(
            'This record has an unresolved complaint — the workflow is frozen until it is resolved.'
        )


def transition_status(*, record, new_status, updated_by, note=''):
    """The only sanctioned way to change a ProcurementRecord's status.

    Writes an audit row and updates the record in the same transaction —
    never allow a direct status field update that bypasses this (see
    PRODAP_AGENT_BUILD_PROMPT_V2.md section 3.6).
    """
    _require_no_pending_complaint(record)
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


# --- Prequalification / EOI (blueprint step 07, the other half): staff-
# recorded tracking of vendors who applied, and the outcome once reviewed.
# Not gated by procurement method — see PrequalificationApplicant's
# docstring for why hardcoding a method check here would be wrong. ---


def record_prequalification_applicant(*, solicitation, actor, vendor_name, vendor_registration_no=''):
    if not vendor_name.strip():
        raise ValidationError('A vendor name is required.')
    if solicitation.status != Solicitation.Status.APPROVED or not hasattr(solicitation, 'advertisement'):
        raise ValidationError('This solicitation has not been published — nothing to apply against yet.')
    with transaction.atomic():
        applicant = PrequalificationApplicant.objects.create(
            solicitation=solicitation, vendor_name=vendor_name.strip(),
            vendor_registration_no=vendor_registration_no.strip(), recorded_by=actor,
        )
        log_audit_event(target=applicant, action=AuditEvent.Action.PREQUALIFICATION_RECORDED, actor=actor)
    return applicant


def review_prequalification_applicant(*, applicant, actor, outcome, note):
    if outcome not in (PrequalificationApplicant.Outcome.QUALIFIED, PrequalificationApplicant.Outcome.NOT_QUALIFIED):
        raise ValidationError('Outcome must be Qualified or Not Qualified.')
    if not note.strip():
        raise ValidationError('A written review note is required.')
    if applicant.outcome != PrequalificationApplicant.Outcome.PENDING:
        raise ValidationError(f'This applicant is already {applicant.get_outcome_display().lower()}.')
    with transaction.atomic():
        applicant.outcome = outcome
        applicant.review_note = note
        applicant.reviewed_by = actor
        applicant.reviewed_at = timezone.now()
        applicant.save(update_fields=['outcome', 'review_note', 'reviewed_by', 'reviewed_at'])
        log_audit_event(
            target=applicant, action=AuditEvent.Action.PREQUALIFICATION_REVIEWED, actor=actor, reason=note,
            new_value={'outcome': outcome},
        )
    return applicant


# --- Bid recording & Award decision (blueprint Phase 3 — Approvals, first
# slice): staff-recorded administrative log of bids received, not a
# submission channel (see Bid's docstring) — followed by an Award decision
# that is now the only sanctioned way to reach Awarded status. ---


def record_bid(*, solicitation, actor, vendor_name, vendor_registration_no='', bid_amount, is_responsive=True, note=''):
    if solicitation.status != Solicitation.Status.APPROVED or not hasattr(solicitation, 'advertisement'):
        raise ValidationError('This solicitation has not been published — nothing to record bids against yet.')
    if timezone.localdate() <= solicitation.advertisement.closing_date:
        raise ValidationError('Bids can only be recorded after the bidding period has closed.')
    if not vendor_name.strip():
        raise ValidationError('A vendor name is required.')
    if not bid_amount or bid_amount <= 0:
        raise ValidationError('A positive bid amount is required.')
    with transaction.atomic():
        bid = Bid.objects.create(
            solicitation=solicitation, vendor_name=vendor_name.strip(),
            vendor_registration_no=vendor_registration_no.strip(), bid_amount=bid_amount,
            is_responsive=is_responsive, note=note.strip(), recorded_by=actor,
        )
        log_audit_event(target=bid, action=AuditEvent.Action.BID_RECORDED, actor=actor)
    return bid


def record_tenders_board_review(*, solicitation, actor, recommended_bid, evaluation_summary, quorum_present=True):
    """The evaluation/approval-routing stage (blueprint steps 11-13) —
    from this slice onward, award_solicitation() refuses to proceed
    without one. See TendersBoardReview's docstring for what's
    deliberately not modeled (per-bid numeric scoring, individual board
    member sign-offs)."""
    if hasattr(solicitation, 'tenders_board_review'):
        raise ValidationError('This solicitation already has a Tenders Board review on file.')
    if recommended_bid.solicitation_id != solicitation.id:
        raise ValidationError('The recommended bid must belong to this solicitation.')
    if not recommended_bid.is_responsive:
        raise ValidationError('A non-responsive bid cannot be recommended.')
    if not evaluation_summary.strip():
        raise ValidationError('A written evaluation summary is required.')
    if not quorum_present:
        raise ValidationError('The Tenders Board did not have quorum — no recommendation can be recorded.')
    with transaction.atomic():
        review = TendersBoardReview.objects.create(
            solicitation=solicitation, recommended_bid=recommended_bid,
            evaluation_summary=evaluation_summary.strip(), quorum_present=quorum_present, reviewed_by=actor,
        )
        log_audit_event(target=review, action=AuditEvent.Action.TENDERS_BOARD_REVIEWED, actor=actor)
    return review


def award_solicitation(
    *, solicitation, actor, winning_bid, decision_note,
    bpp_no_objection_reference='', bpp_no_objection_date=None,
):
    record = solicitation.record
    _require_no_pending_complaint(record)
    if record.status not in (ProcurementRecord.Status.ADVERTISED, ProcurementRecord.Status.TENDERING):
        raise ValidationError(f'This record is {record.status} — an award cannot be decided from this status.')
    if hasattr(solicitation, 'award'):
        raise ValidationError('This solicitation has already been awarded.')
    if not hasattr(solicitation, 'tenders_board_review'):
        raise ValidationError('A Tenders Board review is required before an award can be decided.')
    if winning_bid.solicitation_id != solicitation.id:
        raise ValidationError('The winning bid must belong to this solicitation.')
    if not winning_bid.is_responsive:
        raise ValidationError('A non-responsive bid cannot be selected as the winner.')
    if not decision_note.strip():
        raise ValidationError('A decision note is required — this becomes the public award justification.')
    if solicitation.bpp_prior_review_required and not (bpp_no_objection_reference.strip() and bpp_no_objection_date):
        raise ValidationError(
            'BPP Certificate of No Objection is required before award, per the determined procurement method.'
        )
    with transaction.atomic():
        award = Award.objects.create(
            solicitation=solicitation, winning_bid=winning_bid, decision_note=decision_note.strip(),
            bpp_no_objection_reference=bpp_no_objection_reference.strip(), bpp_no_objection_date=bpp_no_objection_date,
            awarded_by=actor,
        )
        record.vendor_name = winning_bid.vendor_name
        record.vendor_registration_no = winning_bid.vendor_registration_no
        record.awarded_cost = winning_bid.bid_amount
        record.save(update_fields=['vendor_name', 'vendor_registration_no', 'awarded_cost', 'updated_at'])
        log_audit_event(
            target=award, action=AuditEvent.Action.AWARD_DECIDED, actor=actor, reason=decision_note,
            new_value={'vendor_name': winning_bid.vendor_name, 'awarded_cost': str(winning_bid.bid_amount)},
        )
        transition_status(
            record=record, new_status=ProcurementRecord.Status.AWARDED, updated_by=actor,
            note=f'Awarded to {winning_bid.vendor_name} — ₦{winning_bid.bid_amount}.',
        )
    return award


# --- Complaints handling (blueprint Phase 3 — Approvals): public, no-login
# intake at any project stage, resolved by the Accounting Officer
# (independent of the Procurement Unit whose conduct may be the subject of
# the complaint). submit_complaint() is anonymous with respect to audit
# events — no actor, matching flag_record's/submit_clarification_question's
# precedent for public-submitted content — but DOES require contact info
# (unlike Clarification) since a complaint needs real follow-up. ---


def submit_complaint(*, record, complainant_name, complainant_contact, description):
    if not complainant_name.strip():
        raise ValidationError('Your name is required.')
    if not complainant_contact.strip():
        raise ValidationError('Contact information (email or phone) is required so we can follow up.')
    if not description.strip():
        raise ValidationError('A description of the complaint is required.')
    return Complaint.objects.create(
        record=record, complainant_name=complainant_name.strip(),
        complainant_contact=complainant_contact.strip(), description=description.strip(),
    )


def resolve_complaint(*, complaint, actor, status, resolution_note):
    if status not in (Complaint.Status.UPHELD, Complaint.Status.DISMISSED):
        raise ValidationError('Outcome must be Upheld or Dismissed.')
    if not resolution_note.strip():
        raise ValidationError('A resolution note is required — this becomes the public response.')
    if complaint.status != Complaint.Status.PENDING:
        raise ValidationError(f'This complaint is already {complaint.get_status_display().lower()}.')
    with transaction.atomic():
        complaint.status = status
        complaint.resolution_note = resolution_note.strip()
        complaint.resolved_by = actor
        complaint.resolved_at = timezone.now()
        complaint.save(update_fields=['status', 'resolution_note', 'resolved_by', 'resolved_at'])
        log_audit_event(
            target=complaint, action=AuditEvent.Action.COMPLAINT_RESOLVED, actor=actor, reason=resolution_note,
            new_value={'status': status},
        )
    return complaint


# --- Contract lifecycle (blueprint Phase 4), first slice: signing an
# Award into a Contract, plus milestone tracking. sign_contract() is now
# the only sanctioned way to reach Implementation status, same pattern as
# publish_advertisement()/award_solicitation(). Invoices/payments/
# variations and evidence-gating Implementation -> Completed remain
# explicitly out of scope for this slice. ---


def sign_contract(*, award, actor, contract_reference, start_date, end_date, vendor_signatory_name, signed_date):
    record = award.solicitation.record
    _require_no_pending_complaint(record)
    if record.status != ProcurementRecord.Status.AWARDED:
        raise ValidationError(f'This record is {record.status} — a contract cannot be signed from this status.')
    if hasattr(award, 'contract'):
        raise ValidationError('This award already has a signed contract.')
    if not contract_reference.strip():
        raise ValidationError('A contract reference is required.')
    if not vendor_signatory_name.strip():
        raise ValidationError('The vendor signatory name is required.')
    if end_date < start_date:
        raise ValidationError({'end_date': 'End date must be on or after the start date.'})
    with transaction.atomic():
        contract = Contract.objects.create(
            award=award, contract_reference=contract_reference.strip(), start_date=start_date, end_date=end_date,
            vendor_signatory_name=vendor_signatory_name.strip(), signed_date=signed_date, signed_by=actor,
        )
        log_audit_event(target=contract, action=AuditEvent.Action.CONTRACT_SIGNED, actor=actor)
        transition_status(
            record=record, new_status=ProcurementRecord.Status.IMPLEMENTATION, updated_by=actor,
            note=f'Contract {contract_reference} signed with {vendor_signatory_name}.',
        )
    return contract


def add_milestone(*, contract, actor, description, due_date):
    if not description.strip():
        raise ValidationError('A milestone description is required.')
    with transaction.atomic():
        milestone = Milestone.objects.create(
            contract=contract, description=description.strip(), due_date=due_date, created_by=actor,
        )
        log_audit_event(target=milestone, action=AuditEvent.Action.MILESTONE_ADDED, actor=actor)
    return milestone


def complete_milestone(*, milestone, actor, completion_note):
    if milestone.status != Milestone.Status.PENDING:
        raise ValidationError('This milestone is already completed.')
    if not completion_note.strip():
        raise ValidationError('A completion/inspection note is required.')
    with transaction.atomic():
        milestone.status = Milestone.Status.COMPLETED
        milestone.completion_note = completion_note.strip()
        milestone.completed_by = actor
        milestone.completed_at = timezone.now()
        milestone.save(update_fields=['status', 'completion_note', 'completed_by', 'completed_at'])
        log_audit_event(target=milestone, action=AuditEvent.Action.MILESTONE_COMPLETED, actor=actor, reason=completion_note)
    return milestone


def record_performance_guarantee(
    *, contract, actor, guarantee_type, issuing_institution, reference_number, amount, expiry_date,
):
    if hasattr(contract, 'performance_guarantee'):
        raise ValidationError('This contract already has a performance guarantee on file.')
    if not guarantee_type.strip() or not issuing_institution.strip() or not reference_number.strip():
        raise ValidationError('Guarantee type, issuing institution, and reference number are all required.')
    if not amount or amount <= 0:
        raise ValidationError('A positive guarantee amount is required.')
    with transaction.atomic():
        guarantee = PerformanceGuarantee.objects.create(
            contract=contract, guarantee_type=guarantee_type.strip(),
            issuing_institution=issuing_institution.strip(), reference_number=reference_number.strip(),
            amount=amount, expiry_date=expiry_date, verified_by=actor,
        )
        log_audit_event(target=guarantee, action=AuditEvent.Action.PERFORMANCE_GUARANTEE_RECORDED, actor=actor)
    return guarantee


def complete_contract(*, contract, actor, completion_date, inspection_note):
    """Final acceptance sign-off — the only sanctioned way a
    ProcurementRecord reaches Completed status. Closes the loop opened by
    sign_contract()/add_milestone(): every milestone (if any exist) must
    already be completed before the contract itself can be marked
    complete. Also requires a PerformanceGuarantee on file whenever the
    underlying solicitation required bid security — see
    PerformanceGuarantee's docstring for why that's the trigger used."""
    record = contract.award.solicitation.record
    _require_no_pending_complaint(record)
    if record.status != ProcurementRecord.Status.IMPLEMENTATION:
        raise ValidationError(f'This record is {record.status} — completion cannot be recorded from this status.')
    if hasattr(contract, 'completion'):
        raise ValidationError('This contract has already been marked complete.')
    if contract.milestones.exclude(status=Milestone.Status.COMPLETED).exists():
        raise ValidationError('All milestones must be completed before the contract can be marked complete.')
    if contract.award.solicitation.bid_security_required and not hasattr(contract, 'performance_guarantee'):
        raise ValidationError(
            'A performance guarantee is required before completion — bid security was required for this tender.'
        )
    if not inspection_note.strip():
        raise ValidationError('A final inspection/acceptance note is required.')
    with transaction.atomic():
        completion = ContractCompletion.objects.create(
            contract=contract, completion_date=completion_date, inspection_note=inspection_note.strip(),
            completed_by=actor,
        )
        log_audit_event(target=completion, action=AuditEvent.Action.CONTRACT_COMPLETED, actor=actor, reason=inspection_note)
        transition_status(
            record=record, new_status=ProcurementRecord.Status.COMPLETED, updated_by=actor,
            note=f'Contract {contract.contract_reference} completed and accepted.',
        )
    return completion


# --- Invoices & payments (blueprint Phase 4 — "invoices, payments").
# Deliberately independent of the Completion gate above: final payments
# often trail acceptance in practice (retention, etc.), so this pass does
# not tie complete_contract() to invoice/payment state — see Invoice's
# docstring. ---


def submit_invoice(*, contract, actor, invoice_number, amount, submitted_date, milestone=None):
    if not invoice_number.strip():
        raise ValidationError('An invoice number is required.')
    if not amount or amount <= 0:
        raise ValidationError('A positive invoice amount is required.')
    if milestone is not None:
        if milestone.contract_id != contract.id:
            raise ValidationError('The selected milestone does not belong to this contract.')
        if milestone.status != Milestone.Status.COMPLETED:
            raise ValidationError('The linked milestone must be completed before invoicing against it.')
    with transaction.atomic():
        invoice = Invoice.objects.create(
            contract=contract, milestone=milestone, invoice_number=invoice_number.strip(),
            amount=amount, submitted_date=submitted_date, submitted_by=actor,
        )
        log_audit_event(target=invoice, action=AuditEvent.Action.INVOICE_SUBMITTED, actor=actor)
    return invoice


def review_invoice(*, invoice, actor, status, review_note):
    if status not in (Invoice.Status.APPROVED, Invoice.Status.REJECTED):
        raise ValidationError('Outcome must be Approved or Rejected.')
    if not review_note.strip():
        raise ValidationError('A written review note is required.')
    if invoice.status != Invoice.Status.PENDING:
        raise ValidationError(f'This invoice is already {invoice.get_status_display().lower()}.')
    if actor.pk == invoice.submitted_by_id:
        raise SeparationOfDutiesError('You cannot review an invoice you submitted yourself.')
    with transaction.atomic():
        invoice.status = status
        invoice.review_note = review_note.strip()
        invoice.reviewed_by = actor
        invoice.reviewed_at = timezone.now()
        invoice.save(update_fields=['status', 'review_note', 'reviewed_by', 'reviewed_at'])
        log_audit_event(
            target=invoice, action=AuditEvent.Action.INVOICE_REVIEWED, actor=actor, reason=review_note,
            new_value={'status': status},
        )
    return invoice


def record_payment(*, invoice, actor, amount, payment_date, payment_reference):
    if invoice.status != Invoice.Status.APPROVED:
        raise ValidationError('Only an approved invoice can be paid.')
    if hasattr(invoice, 'payment'):
        raise ValidationError('This invoice has already been paid.')
    if not amount or amount <= 0:
        raise ValidationError('A positive payment amount is required.')
    if not payment_reference.strip():
        raise ValidationError('A payment reference is required.')
    with transaction.atomic():
        payment = Payment.objects.create(
            invoice=invoice, amount=amount, payment_date=payment_date,
            payment_reference=payment_reference.strip(), paid_by=actor,
        )
        log_audit_event(target=payment, action=AuditEvent.Action.PAYMENT_RECORDED, actor=actor)
    return payment


# --- Abandonment: the only sanctioned way a ProcurementRecord reaches
# Abandoned status (see Abandonment's docstring) — closes the last
# remaining unconditional manual-dropdown gap in the status machine. ---

def abandon_record(*, record, actor, reason, justification):
    _require_no_pending_complaint(record)
    if record.status in (ProcurementRecord.Status.COMPLETED, ProcurementRecord.Status.ABANDONED):
        raise ValidationError(f'This record is {record.status} — it cannot be abandoned from this status.')
    if hasattr(record, 'abandonment'):
        raise ValidationError('This record has already been abandoned.')
    if reason not in Abandonment.Reason.values:
        raise ValidationError('A valid abandonment reason is required.')
    if not justification.strip():
        raise ValidationError('A justification is required — this becomes the public explanation.')
    previous_status = record.status
    with transaction.atomic():
        abandonment = Abandonment.objects.create(
            record=record, previous_status=previous_status, reason=reason,
            justification=justification.strip(), abandoned_by=actor,
        )
        log_audit_event(
            target=abandonment, action=AuditEvent.Action.RECORD_ABANDONED, actor=actor, reason=justification,
            old_value={'status': previous_status}, new_value={'reason': reason},
        )
        transition_status(
            record=record, new_status=ProcurementRecord.Status.ABANDONED, updated_by=actor,
            note=f'Abandoned ({abandonment.get_reason_display()}): {justification.strip()}',
        )
    return abandonment
