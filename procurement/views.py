import csv

from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render

from .forms import LocalizedAuthenticationForm, ProcurementRecordForm, StatusTransitionForm
from .i18n import STRINGS, DEFAULT_LANG, get_strings
from .models import ProcurementRecord, RecordFlag
from .services import transition_status


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


def public_dashboard(request):
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

    all_records = ProcurementRecord.objects.all()
    active_count = all_records.filter(status__in=ACTIVE_STATUSES).count()
    total_value = sum((r.display_cost or 0) for r in all_records)

    paginator = Paginator(records, 20)
    page = paginator.get_page(request.GET.get('page'))

    return render(request, 'public/dashboard.html', {
        'page': page,
        'query': query,
        'status': status,
        'budget_source': budget_source,
        'status_choices': ProcurementRecord.Status.choices,
        'budget_source_choices': ProcurementRecord.BudgetSource.choices,
        'active_count': active_count,
        'total_value': total_value,
    })


def public_record_detail(request, pk):
    record = get_object_or_404(ProcurementRecord.objects.select_related('law_profile'), pk=pk)
    history = record.status_updates.select_related('updated_by').all()
    flagged_session = request.session.get('flagged_records', [])
    return render(request, 'public/detail.html', {
        'record': record,
        'history': history,
        'flag_count': record.flags.count(),
        'already_flagged': str(record.id) in flagged_session,
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
def staff_record_create(request):
    if request.method == 'POST':
        form = ProcurementRecordForm(request.POST)
        if form.is_valid():
            record = form.save(commit=False)
            record.created_by = request.user
            record.save()
            return redirect('staff_record_list')
    else:
        form = ProcurementRecordForm()
    return render(request, 'staff/record_form.html', {'form': form, 'is_new': True})


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
