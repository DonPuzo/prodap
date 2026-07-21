import csv

from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from django.core.exceptions import PermissionDenied, ValidationError

from .forms import (
    AdvertisementForm,
    AwardForm,
    BidForm,
    ClarificationAnswerForm,
    ClarificationQuestionForm,
    FundsConfirmationForm,
    FundsDeclineForm,
    LocalizedAuthenticationForm,
    MethodDeterminationForm,
    PackagingReviewForm,
    PlanLineForm,
    PrequalificationApplicantForm,
    PrequalificationReviewForm,
    ProcurementPlanForm,
    ProcurementRecordForm,
    RecordFromRequisitionForm,
    RejectWithReasonForm,
    RequisitionForm,
    SolicitationForm,
    StatusTransitionForm,
)
from .i18n import STRINGS, DEFAULT_LANG, get_strings
from .models import (
    Bid, Clarification, PlanLine, PrequalificationApplicant, ProcurementPlan, ProcurementRecord, RecordFlag,
    Requisition, Solicitation, User,
)
from .permissions import role_required
from .services import (
    answer_clarification,
    approve_plan,
    approve_plan_line,
    approve_solicitation,
    award_solicitation,
    confirm_requisition_funds,
    create_record_from_requisition,
    decline_requisition_funds,
    determine_requisition_method,
    find_similar_requisitions,
    get_current_solicitation,
    get_published_advertisement,
    prepare_solicitation,
    publish_advertisement,
    record_bid,
    record_prequalification_applicant,
    reject_plan,
    reject_plan_line,
    reject_solicitation,
    review_prequalification_applicant,
    review_requisition_packaging,
    submit_clarification_question,
    submit_plan,
    submit_requisition,
    transition_status,
)


class StaffLoginView(auth_views.LoginView):
    template_name = 'staff/login.html'
    authentication_form = LocalizedAuthenticationForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['lang'] = self.request.session.get('lang', DEFAULT_LANG)
        return kwargs


def set_lang(request, lang_code):
    if lang_code in STRINGS:
        request.session['lang'] = lang_code
    referer = request.META.get('HTTP_REFERER', '/')
    return redirect(referer)

# --- Public dashboard (no auth, ever — build prompt section 3 point 2) ---

ACTIVE_STATUSES = [
    ProcurementRecord.Status.ADVERTISED,
    ProcurementRecord.Status.TENDERING,
    ProcurementRecord.Status.AWARDED,
    ProcurementRecord.Status.IMPLEMENTATION,
]


def _headline_stats():
    """Shared by the landing page and the register — same numbers, just
    presented as a hook on one page and in context on the other."""
    all_records = ProcurementRecord.objects.all()
    active_count = all_records.filter(status__in=ACTIVE_STATUSES).count()
    total_value = sum((r.display_cost or 0) for r in all_records)
    total_count = all_records.count()
    return active_count, total_value, total_count


def public_dashboard(request):
    """The landing page — explains what ProDAP is before showing any data.
    The actual browsable register lives at public_register; this page's job
    is orientation and audience-based routing (public/oversight/staff), not
    search (see build prompt v2 section 7B / homepage research)."""
    active_count, total_value, total_count = _headline_stats()
    # Abandoned is a terminal exception, not a step in the normal sequence —
    # excluded from the linear teaser here (the About page's glossary still
    # covers every status, including Abandoned).
    progress_statuses = [
        (value, label) for value, label in ProcurementRecord.Status.choices
        if value != ProcurementRecord.Status.ABANDONED
    ]
    return render(request, 'public/dashboard.html', {
        'active_count': active_count,
        'total_value': total_value,
        'total_count': total_count,
        'progress_statuses': progress_statuses,
    })


def public_register(request):
    records = ProcurementRecord.objects.select_related('law_profile').all()

    query = request.GET.get('q', '').strip()
    if query:
        records = records.filter(Q(title__icontains=query) | Q(vendor_name__icontains=query))

    status = request.GET.get('status', '')
    if status:
        records = records.filter(status=status)

    budget_source = request.GET.get('budget_source', '')
    if budget_source:
        records = records.filter(budget_source=budget_source)

    active_count, total_value, _ = _headline_stats()

    paginator = Paginator(records, 20)
    page = paginator.get_page(request.GET.get('page'))

    return render(request, 'public/register.html', {
        'page': page,
        'query': query,
        'status': status,
        'budget_source': budget_source,
        'status_choices': ProcurementRecord.Status.choices,
        'budget_source_choices': ProcurementRecord.BudgetSource.choices,
        'active_count': active_count,
        'total_value': total_value,
    })


def public_about(request):
    """Static explainer + status glossary — reuses ProcurementRecord.Status
    as the single source of truth rather than hardcoding the list again."""
    return render(request, 'public/about.html', {
        'status_choices': ProcurementRecord.Status.choices,
    })


def public_record_detail(request, pk):
    record = get_object_or_404(ProcurementRecord.objects.select_related('law_profile'), pk=pk)
    history = record.status_updates.select_related('updated_by').all()
    flagged_session = request.session.get('flagged_records', [])
    advertisement = get_published_advertisement(record)
    clarifications_answered = []
    clarifications_pending_count = 0
    can_ask_question = False
    reviewed_applicants = []
    if advertisement:
        all_clarifications = advertisement.solicitation.clarifications.all()
        clarifications_answered = [c for c in all_clarifications if c.answer]
        clarifications_pending_count = sum(1 for c in all_clarifications if not c.answer)
        can_ask_question = timezone.localdate() <= advertisement.closing_date
        # Pending applications aren't shown publicly, same "raw stays
        # private, resolved goes public" rule as clarifications — the
        # review note (which may contain sensitive evaluation commentary)
        # stays staff-only even once reviewed.
        reviewed_applicants = [
            a for a in advertisement.solicitation.prequalification_applicants.all()
            if a.outcome != PrequalificationApplicant.Outcome.PENDING
        ]
    # Bids and the award decision are only disclosed once the award has
    # actually been decided — not mid-evaluation, same "resolved becomes
    # public" timing as clarifications/prequalification, avoids leaking
    # competitive intelligence to other bidders while bids are still being
    # compiled.
    award = None
    bids = []
    if advertisement and hasattr(advertisement.solicitation, 'award'):
        award = advertisement.solicitation.award
        bids = advertisement.solicitation.bids.all()
    return render(request, 'public/detail.html', {
        'record': record,
        'history': history,
        'flag_count': record.flags.count(),
        'already_flagged': str(record.id) in flagged_session,
        'advertisement': advertisement,
        'clarifications_answered': clarifications_answered,
        'clarifications_pending_count': clarifications_pending_count,
        'can_ask_question': can_ask_question,
        'reviewed_applicants': reviewed_applicants,
        'award': award,
        'bids': bids,
    })


def flag_record(request, pk):
    """Public 'flag this project as concerning' — no login, no moderation
    queue, just a visible count. One flag per browser session per record,
    to keep the count meaningful without building real rate-limiting
    (build prompt v2 Phase 2 item 1 — deliberately minimal)."""
    record = get_object_or_404(ProcurementRecord, pk=pk)
    ui = get_strings(request.session.get('lang', DEFAULT_LANG))
    if request.method == 'POST':
        flagged_session = request.session.get('flagged_records', [])
        if str(record.id) not in flagged_session:
            RecordFlag.objects.create(record=record, note=request.POST.get('note', '').strip())
            flagged_session.append(str(record.id))
            request.session['flagged_records'] = flagged_session
            messages.success(request, ui['flag_success_message'])
        else:
            messages.info(request, ui['already_flagged'])
    return redirect('public_record_detail', pk=record.id)


def submit_clarification(request, pk):
    """Public, no login — ask a question about a published tender. Mirrors
    flag_record's shape: POST-only, errors surfaced via Django messages
    (not a form re-render), always redirects back to the detail page."""
    record = get_object_or_404(ProcurementRecord, pk=pk)
    ui = get_strings(request.session.get('lang', DEFAULT_LANG))
    if request.method == 'POST':
        form = ClarificationQuestionForm(request.POST)
        if form.is_valid():
            try:
                submit_clarification_question(record=record, question=form.cleaned_data['question'])
                messages.success(request, ui['ask_question_submitted_message'])
            except ValidationError as exc:
                messages.info(request, exc.message)
    return redirect('public_record_detail', pk=record.id)


# --- Open data export (v2 section 5B) — same public-safe fields as the
# dashboard, no auth, no more data than what's already visible. ---

EXPORT_FIELDS = [
    'id', 'title', 'department', 'budget_source', 'estimated_cost', 'awarded_cost',
    'procurement_method', 'vendor_name', 'status', 'location', 'planned_start_date',
    'planned_end_date', 'actual_start_date', 'actual_end_date', 'created_at', 'updated_at',
]


def _record_to_dict(record):
    return {
        'id': str(record.id),
        'title': record.title,
        'department': record.department,
        'budget_source': record.budget_source,
        'estimated_cost': str(record.estimated_cost),
        'awarded_cost': str(record.awarded_cost) if record.awarded_cost is not None else None,
        'procurement_method': record.procurement_method,
        'vendor_name': record.vendor_name,
        'status': record.status,
        'location': record.location,
        'planned_start_date': record.planned_start_date.isoformat(),
        'planned_end_date': record.planned_end_date.isoformat(),
        'actual_start_date': record.actual_start_date.isoformat() if record.actual_start_date else None,
        'actual_end_date': record.actual_end_date.isoformat() if record.actual_end_date else None,
        'created_at': record.created_at.isoformat(),
        'updated_at': record.updated_at.isoformat(),
    }


def export_json(request):
    records = ProcurementRecord.objects.all()
    return JsonResponse({'records': [_record_to_dict(r) for r in records]})


def export_csv(request):
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="prodap_records.csv"'
    writer = csv.DictWriter(response, fieldnames=EXPORT_FIELDS)
    writer.writeheader()
    for record in ProcurementRecord.objects.all():
        row = _record_to_dict(record)
        writer.writerow({field: row[field] for field in EXPORT_FIELDS})
    return response


# --- Procurement office backend (login required) ---

@login_required
def staff_record_list(request):
    records = ProcurementRecord.objects.select_related('law_profile').annotate(flag_count=Count('flags'))
    return render(request, 'staff/record_list.html', {'records': records})


@login_required
def staff_record_edit(request, pk):
    record = get_object_or_404(ProcurementRecord, pk=pk)
    if request.method == 'POST':
        form = ProcurementRecordForm(request.POST, instance=record)
        if form.is_valid():
            form.save()
            return redirect('staff_record_list')
    else:
        form = ProcurementRecordForm(instance=record)
    return render(request, 'staff/record_form.html', {'form': form, 'is_new': False, 'record': record})


@login_required
def staff_status_transition(request, pk):
    record = get_object_or_404(ProcurementRecord, pk=pk)
    if request.method == 'POST':
        form = StatusTransitionForm(request.POST, current_status=record.status)
        if form.is_valid():
            transition_status(
                record=record,
                new_status=form.cleaned_data['new_status'],
                updated_by=request.user,
                note=form.cleaned_data['note'],
            )
            return redirect('staff_record_list')
    else:
        form = StatusTransitionForm(current_status=record.status)
    return render(request, 'staff/status_transition.html', {'form': form, 'record': record})


# --- Phase 1-Foundation: annual plans -> requisitions -> funds confirmation
# -> packaging review -> method determination -> record creation. See
# services.py for the gate-enforcing functions these views call; views
# themselves never write plan/requisition state directly. ---

@login_required
def staff_plan_list(request):
    plans = ProcurementPlan.objects.select_related('law_profile', 'financial_year', 'prepared_by')
    return render(request, 'staff/plan_list.html', {'plans': plans})


@role_required(User.Role.PROCUREMENT_UNIT)
def staff_plan_create(request):
    if request.method == 'POST':
        form = ProcurementPlanForm(request.POST)
        if form.is_valid():
            plan = form.save(commit=False)
            plan.prepared_by = request.user
            plan.save()
            return redirect('staff_plan_detail', pk=plan.pk)
    else:
        form = ProcurementPlanForm()
    return render(request, 'staff/plan_form.html', {'form': form})


@login_required
def staff_plan_detail(request, pk):
    plan = get_object_or_404(
        ProcurementPlan.objects.select_related('law_profile', 'financial_year', 'prepared_by', 'approved_by'), pk=pk
    )
    lines = plan.lines.select_related('proposed_by').all()
    return render(request, 'staff/plan_detail.html', {'plan': plan, 'lines': lines})


@role_required(User.Role.REQUESTING_UNIT, User.Role.PROCUREMENT_UNIT)
def staff_plan_line_create(request, pk):
    plan = get_object_or_404(ProcurementPlan, pk=pk)
    if request.method == 'POST':
        form = PlanLineForm(request.POST)
        if form.is_valid():
            line = form.save(commit=False)
            line.plan = plan
            line.proposed_by = request.user
            line.is_amendment = plan.status == ProcurementPlan.Status.APPROVED
            line.save()
            return redirect('staff_plan_detail', pk=plan.pk)
    else:
        form = PlanLineForm()
    return render(request, 'staff/plan_line_form.html', {'form': form, 'plan': plan})


@role_required(User.Role.PROCUREMENT_UNIT)
def staff_plan_submit(request, pk):
    plan = get_object_or_404(ProcurementPlan, pk=pk)
    if request.method == 'POST':
        submit_plan(plan=plan, actor=request.user)
    return redirect('staff_plan_detail', pk=plan.pk)


@role_required(User.Role.ACCOUNTING_OFFICER)
def staff_plan_approve(request, pk):
    plan = get_object_or_404(ProcurementPlan, pk=pk)
    error = None
    if request.method == 'POST':
        if 'reject' in request.POST:
            reject_form = RejectWithReasonForm(request.POST)
            if reject_form.is_valid():
                try:
                    reject_plan(plan=plan, actor=request.user, reason=reject_form.cleaned_data['reason'])
                    return redirect('staff_plan_detail', pk=plan.pk)
                except ValidationError as exc:
                    error = exc.message
        else:
            try:
                approve_plan(plan=plan, actor=request.user)
                return redirect('staff_plan_detail', pk=plan.pk)
            except ValidationError as exc:
                error = exc.message
    reject_form = RejectWithReasonForm()
    return render(request, 'staff/plan_approve.html', {'plan': plan, 'reject_form': reject_form, 'error': error})


@role_required(User.Role.ACCOUNTING_OFFICER)
def staff_plan_line_approve(request, pk):
    line = get_object_or_404(PlanLine, pk=pk)
    error = None
    if request.method == 'POST':
        if 'reject' in request.POST:
            reject_form = RejectWithReasonForm(request.POST)
            if reject_form.is_valid():
                try:
                    reject_plan_line(plan_line=line, actor=request.user, reason=reject_form.cleaned_data['reason'])
                    return redirect('staff_plan_detail', pk=line.plan_id)
                except ValidationError as exc:
                    error = exc.message
        else:
            try:
                approve_plan_line(plan_line=line, actor=request.user)
                return redirect('staff_plan_detail', pk=line.plan_id)
            except ValidationError as exc:
                error = exc.message
    reject_form = RejectWithReasonForm()
    return render(request, 'staff/plan_line_approve.html', {'line': line, 'reject_form': reject_form, 'error': error})


@login_required
def staff_requisition_list(request):
    requisitions = Requisition.objects.select_related('plan_line', 'requested_by')
    return render(request, 'staff/requisition_list.html', {'requisitions': requisitions})


@role_required(User.Role.REQUESTING_UNIT, User.Role.PROCUREMENT_UNIT)
def staff_requisition_create(request):
    if request.method == 'POST':
        form = RequisitionForm(request.POST)
        if form.is_valid():
            requisition = form.save(commit=False)
            requisition.requested_by = request.user
            requisition.save()
            return redirect('staff_requisition_detail', pk=requisition.pk)
    else:
        form = RequisitionForm()
    return render(request, 'staff/requisition_form.html', {'form': form})


@login_required
def staff_requisition_detail(request, pk):
    requisition = get_object_or_404(
        Requisition.objects.select_related('plan_line__plan__law_profile', 'requested_by', 'funds_confirmed_by'),
        pk=pk,
    )
    record = getattr(requisition, 'record', None)
    return render(request, 'staff/requisition_detail.html', {'requisition': requisition, 'record': record})


@role_required(User.Role.REQUESTING_UNIT, User.Role.PROCUREMENT_UNIT)
def staff_requisition_submit(request, pk):
    requisition = get_object_or_404(Requisition, pk=pk)
    error = None
    if request.method == 'POST':
        try:
            submit_requisition(requisition=requisition, actor=request.user)
            return redirect('staff_requisition_detail', pk=requisition.pk)
        except ValidationError as exc:
            error = exc.message
    return render(request, 'staff/requisition_submit.html', {'requisition': requisition, 'error': error})


@role_required(User.Role.FINANCE)
def staff_requisition_confirm_funds(request, pk):
    requisition = get_object_or_404(Requisition, pk=pk)
    error = None
    if request.method == 'POST':
        if 'decline' in request.POST:
            decline_form = FundsDeclineForm(request.POST)
            if decline_form.is_valid():
                try:
                    decline_requisition_funds(
                        requisition=requisition, actor=request.user, reason=decline_form.cleaned_data['reason']
                    )
                    return redirect('staff_requisition_detail', pk=requisition.pk)
                except ValidationError as exc:
                    error = exc.message
        else:
            form = FundsConfirmationForm(request.POST)
            if form.is_valid():
                try:
                    confirm_requisition_funds(
                        requisition=requisition, actor=request.user, note=form.cleaned_data['note']
                    )
                    return redirect('staff_requisition_detail', pk=requisition.pk)
                except ValidationError as exc:
                    error = exc.message
    form = FundsConfirmationForm()
    decline_form = FundsDeclineForm()
    return render(request, 'staff/requisition_confirm_funds.html', {
        'requisition': requisition, 'form': form, 'decline_form': decline_form, 'error': error,
    })


@role_required(User.Role.PROCUREMENT_UNIT)
def staff_requisition_review_packaging(request, pk):
    requisition = get_object_or_404(Requisition, pk=pk)
    similar = find_similar_requisitions(requisition)
    error = None
    if request.method == 'POST':
        form = PackagingReviewForm(request.POST)
        if form.is_valid():
            try:
                review_requisition_packaging(
                    requisition=requisition, actor=request.user, note=form.cleaned_data['note']
                )
                return redirect('staff_requisition_detail', pk=requisition.pk)
            except ValidationError as exc:
                error = exc.message
    else:
        form = PackagingReviewForm()
    return render(request, 'staff/requisition_review_packaging.html', {
        'requisition': requisition, 'form': form, 'similar': similar, 'error': error,
    })


@role_required(User.Role.PROCUREMENT_UNIT)
def staff_requisition_determine_method(request, pk):
    requisition = get_object_or_404(Requisition, pk=pk)
    law_profile = requisition.plan_line.plan.law_profile
    error = None
    if request.method == 'POST':
        form = MethodDeterminationForm(request.POST, law_profile=law_profile)
        if form.is_valid():
            try:
                determine_requisition_method(
                    requisition=requisition, actor=request.user,
                    method_override=form.cleaned_data['method_override'],
                    override_justification=form.cleaned_data['override_justification'],
                )
                return redirect('staff_requisition_detail', pk=requisition.pk)
            except ValidationError as exc:
                error = exc.message
    else:
        form = MethodDeterminationForm(law_profile=law_profile)
    return render(request, 'staff/requisition_determine_method.html', {
        'requisition': requisition, 'form': form, 'error': error,
    })


@role_required(User.Role.PROCUREMENT_UNIT)
def staff_requisition_create_record(request, pk):
    requisition = get_object_or_404(Requisition, pk=pk)
    error = None
    if request.method == 'POST':
        form = RecordFromRequisitionForm(request.POST)
        if form.is_valid():
            try:
                create_record_from_requisition(
                    requisition=requisition, actor=request.user, record_fields=form.cleaned_data
                )
                return redirect('staff_record_list')
            except ValidationError as exc:
                error = exc.message
    else:
        form = RecordFromRequisitionForm()
    return render(request, 'staff/record_from_requisition_form.html', {
        'requisition': requisition, 'form': form, 'error': error,
    })


# --- Phase 2 (non-cryptographic slice): solicitation preparation ->
# advertisement/publication. ---

@login_required
def staff_record_detail(request, pk):
    record = get_object_or_404(
        ProcurementRecord.objects.select_related('law_profile', 'requisition'), pk=pk
    )
    solicitation = get_current_solicitation(record)
    versions = record.solicitations.all()
    advertisement = getattr(solicitation, 'advertisement', None) if solicitation else None
    return render(request, 'staff/record_detail.html', {
        'record': record, 'solicitation': solicitation, 'versions': versions, 'advertisement': advertisement,
    })


@role_required(User.Role.PROCUREMENT_UNIT)
def staff_solicitation_create(request, pk):
    record = get_object_or_404(ProcurementRecord, pk=pk)
    error = None
    if request.method == 'POST':
        form = SolicitationForm(request.POST)
        if form.is_valid():
            try:
                solicitation = prepare_solicitation(
                    record=record, actor=request.user, fields=form.solicitation_fields()
                )
                return redirect('staff_solicitation_detail', pk=solicitation.pk)
            except ValidationError as exc:
                error = exc.message
    else:
        form = SolicitationForm()
    return render(request, 'staff/solicitation_form.html', {'record': record, 'form': form, 'error': error})


@login_required
def staff_solicitation_detail(request, pk):
    solicitation = get_object_or_404(
        Solicitation.objects.select_related('record', 'prepared_by', 'approved_by'), pk=pk
    )
    advertisement = getattr(solicitation, 'advertisement', None)
    clarifications = solicitation.clarifications.select_related('answered_by').all()
    applicants = solicitation.prequalification_applicants.select_related('recorded_by', 'reviewed_by').all()
    bids = solicitation.bids.select_related('recorded_by').all()
    award = getattr(solicitation, 'award', None)
    return render(request, 'staff/solicitation_detail.html', {
        'solicitation': solicitation, 'record': solicitation.record, 'advertisement': advertisement,
        'clarifications': clarifications, 'answer_form': ClarificationAnswerForm(),
        'applicants': applicants, 'applicant_form': PrequalificationApplicantForm(),
        'review_form': PrequalificationReviewForm(),
        'bids': bids, 'award': award, 'bid_form': BidForm(),
        'award_form': AwardForm(solicitation=solicitation) if not award else None,
    })


@role_required(User.Role.ACCOUNTING_OFFICER)
def staff_solicitation_approve(request, pk):
    solicitation = get_object_or_404(Solicitation, pk=pk)
    error = None
    if request.method == 'POST':
        if 'reject' in request.POST:
            reject_form = RejectWithReasonForm(request.POST)
            if reject_form.is_valid():
                try:
                    reject_solicitation(
                        solicitation=solicitation, actor=request.user, reason=reject_form.cleaned_data['reason']
                    )
                    return redirect('staff_solicitation_detail', pk=solicitation.pk)
                except ValidationError as exc:
                    error = exc.message
        else:
            try:
                approve_solicitation(solicitation=solicitation, actor=request.user)
                return redirect('staff_solicitation_detail', pk=solicitation.pk)
            except ValidationError as exc:
                error = exc.message
    reject_form = RejectWithReasonForm()
    return render(request, 'staff/solicitation_approve.html', {
        'solicitation': solicitation, 'reject_form': reject_form, 'error': error,
    })


@role_required(User.Role.PROCUREMENT_UNIT)
def staff_advertisement_publish(request, pk):
    solicitation = get_object_or_404(Solicitation, pk=pk)
    error = None
    if request.method == 'POST':
        form = AdvertisementForm(request.POST)
        if form.is_valid():
            try:
                publish_advertisement(
                    solicitation=solicitation, actor=request.user,
                    channels=form.cleaned_data['channels'],
                    publication_proof=form.cleaned_data['publication_proof'],
                    closing_date=form.cleaned_data['closing_date'],
                )
                return redirect('staff_record_detail', pk=solicitation.record_id)
            except ValidationError as exc:
                error = exc.message if hasattr(exc, 'message') else exc.messages
    else:
        form = AdvertisementForm()
    return render(request, 'staff/advertisement_publish_form.html', {
        'solicitation': solicitation, 'form': form, 'error': error,
    })


@role_required(User.Role.PROCUREMENT_UNIT)
def staff_clarification_answer(request, pk):
    """POST-only inline action from staff/solicitation_detail.html — no
    separate GET form page, same combined-actions-on-one-page convention
    used throughout the Foundation/Phase 2 staff screens."""
    clarification = get_object_or_404(Clarification, pk=pk)
    if request.method == 'POST':
        form = ClarificationAnswerForm(request.POST)
        if form.is_valid():
            try:
                answer_clarification(clarification=clarification, actor=request.user, answer=form.cleaned_data['answer'])
            except ValidationError as exc:
                messages.error(request, exc.message)
    return redirect('staff_solicitation_detail', pk=clarification.solicitation_id)


@role_required(User.Role.PROCUREMENT_UNIT)
def staff_prequalification_add(request, pk):
    """POST-only inline action from staff/solicitation_detail.html,
    matching staff_clarification_answer's shape."""
    solicitation = get_object_or_404(Solicitation, pk=pk)
    if request.method == 'POST':
        form = PrequalificationApplicantForm(request.POST)
        if form.is_valid():
            try:
                record_prequalification_applicant(
                    solicitation=solicitation, actor=request.user,
                    vendor_name=form.cleaned_data['vendor_name'],
                    vendor_registration_no=form.cleaned_data['vendor_registration_no'],
                )
            except ValidationError as exc:
                messages.error(request, exc.message)
    return redirect('staff_solicitation_detail', pk=solicitation.pk)


@role_required(User.Role.PROCUREMENT_UNIT)
def staff_prequalification_review(request, pk):
    applicant = get_object_or_404(PrequalificationApplicant, pk=pk)
    if request.method == 'POST':
        form = PrequalificationReviewForm(request.POST)
        if form.is_valid():
            try:
                review_prequalification_applicant(
                    applicant=applicant, actor=request.user,
                    outcome=form.cleaned_data['outcome'], note=form.cleaned_data['note'],
                )
            except ValidationError as exc:
                messages.error(request, exc.message)
    return redirect('staff_solicitation_detail', pk=applicant.solicitation_id)


@role_required(User.Role.PROCUREMENT_UNIT)
def staff_bid_add(request, pk):
    solicitation = get_object_or_404(Solicitation, pk=pk)
    if request.method == 'POST':
        form = BidForm(request.POST)
        if form.is_valid():
            try:
                record_bid(
                    solicitation=solicitation, actor=request.user,
                    vendor_name=form.cleaned_data['vendor_name'],
                    vendor_registration_no=form.cleaned_data['vendor_registration_no'],
                    bid_amount=form.cleaned_data['bid_amount'],
                    is_responsive=form.cleaned_data['is_responsive'],
                    note=form.cleaned_data['note'],
                )
            except ValidationError as exc:
                messages.error(request, exc.message)
    return redirect('staff_solicitation_detail', pk=solicitation.pk)


@role_required(User.Role.ACCOUNTING_OFFICER)
def staff_award_decide(request, pk):
    solicitation = get_object_or_404(Solicitation, pk=pk)
    if request.method == 'POST':
        form = AwardForm(request.POST, solicitation=solicitation)
        if form.is_valid():
            try:
                award_solicitation(
                    solicitation=solicitation, actor=request.user,
                    winning_bid=form.cleaned_data['winning_bid'],
                    decision_note=form.cleaned_data['decision_note'],
                    bpp_no_objection_reference=form.cleaned_data['bpp_no_objection_reference'],
                    bpp_no_objection_date=form.cleaned_data['bpp_no_objection_date'],
                )
            except ValidationError as exc:
                messages.error(request, exc.message if hasattr(exc, 'message') else exc.messages)
        else:
            messages.error(request, 'Invalid award submission — check the winning bid and required fields.')
    return redirect('staff_solicitation_detail', pk=solicitation.pk)
