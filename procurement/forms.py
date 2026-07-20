from django import forms
from django.contrib.auth.forms import AuthenticationForm

from .i18n import DEFAULT_LANG, get_strings
from .models import Advertisement, LawProfile, PlanLine, ProcurementPlan, ProcurementRecord, Requisition, Solicitation


class LocalizedAuthenticationForm(AuthenticationForm):
    """Same login form Django ships, with labels and the invalid-login
    error pulled from our EN/Pidgin dict — the login page is reachable
    straight from the public toolbar, so it shouldn't drop out of
    whichever language the visitor picked (build prompt v2 section 7B)."""

    def __init__(self, *args, lang=DEFAULT_LANG, **kwargs):
        super().__init__(*args, **kwargs)
        strings = get_strings(lang)
        self.fields['username'].label = strings['username_label']
        self.fields['password'].label = strings['password_label']
        if lang == 'pcm':
            self.error_messages['invalid_login'] = (
                'Di username or password no correct. Both fields fit dey case-sensitive.'
            )


class ProcurementRecordForm(forms.ModelForm):
    class Meta:
        model = ProcurementRecord
        fields = [
            'title', 'description', 'department', 'budget_source', 'estimated_cost',
            'awarded_cost', 'procurement_method', 'vendor_name', 'vendor_registration_no',
            'location', 'planned_start_date', 'planned_end_date', 'actual_start_date',
            'actual_end_date', 'law_profile',
        ]
        widgets = {
            'description': forms.Textarea(attrs={'rows': 4}),
            'planned_start_date': forms.DateInput(attrs={'type': 'date'}),
            'planned_end_date': forms.DateInput(attrs={'type': 'date'}),
            'actual_start_date': forms.DateInput(attrs={'type': 'date'}),
            'actual_end_date': forms.DateInput(attrs={'type': 'date'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # procurement_method choices are populated from the selected law
        # profile's data, never a hardcoded dropdown (build prompt section 7).
        #
        # NOTE: ProcurementRecord.id uses default=uuid.uuid4, so a fresh
        # unsaved instance already has a non-None pk the moment it's
        # constructed — `not self.instance.pk` is NOT a valid "is this a
        # new record" check here (unlike Django's default auto-increment
        # PKs). Use instance._state.adding instead, which Django sets
        # specifically for this purpose regardless of PK strategy.
        is_new = self.instance._state.adding
        law_profile = None
        if self.data.get('law_profile'):
            law_profile = LawProfile.objects.filter(pk=self.data.get('law_profile')).first()
        elif not is_new and self.instance.law_profile_id:
            law_profile = self.instance.law_profile
        elif is_new:
            law_profile = LawProfile.objects.first()

        method_choices = [(m, m) for m in law_profile.procurement_methods] if law_profile else []
        self.fields['procurement_method'] = forms.ChoiceField(choices=method_choices)
        if law_profile and is_new and not self.initial.get('law_profile'):
            self.fields['law_profile'].initial = law_profile.pk

    def clean(self):
        cleaned = super().clean()
        law_profile = cleaned.get('law_profile')
        method = cleaned.get('procurement_method')
        if law_profile and method and method not in law_profile.procurement_methods:
            self.add_error(
                'procurement_method',
                f'"{method}" is not a valid method under {law_profile.governing_law}.',
            )
        return cleaned


class StatusTransitionForm(forms.Form):
    new_status = forms.ChoiceField(choices=[])
    note = forms.CharField(
        widget=forms.Textarea(attrs={'rows': 3}),
        required=True,
        help_text='Required: explain this status change (this is written to the permanent audit trail).',
    )

    def __init__(self, *args, current_status=None, **kwargs):
        super().__init__(*args, **kwargs)
        excluded = {current_status}
        if current_status == ProcurementRecord.Status.PLANNING:
            # Planning -> Advertised is now evidence-derived (see
            # services.publish_advertisement) — not a free manual pick, for
            # any record, legacy or Foundation-phase (Phase 2 slice).
            excluded.add(ProcurementRecord.Status.ADVERTISED)
        choices = [c for c in ProcurementRecord.Status.choices if c[0] not in excluded]
        self.fields['new_status'].choices = choices


# --- Phase 1-Foundation: annual plans -> requisitions ---

class ProcurementPlanForm(forms.ModelForm):
    class Meta:
        model = ProcurementPlan
        fields = ['law_profile', 'financial_year']


class PlanLineForm(forms.ModelForm):
    class Meta:
        model = PlanLine
        fields = [
            'department', 'item_description', 'justification', 'quantity',
            'unit_of_measure', 'estimated_cost', 'budget_line', 'proposed_method',
            'proposed_quarter',
        ]
        widgets = {'justification': forms.Textarea(attrs={'rows': 3})}


class RequisitionForm(forms.ModelForm):
    # department is deliberately NOT user-entered — it's always derived
    # from the chosen plan_line (see clean() below) so a requisition can
    # never be misattributed to a different department than the one whose
    # budget line was actually approved (security review finding,
    # Phase 1-Foundation: forms.py used to let this be freely typed).
    class Meta:
        model = Requisition
        fields = ['plan_line', 'title', 'requested_value', 'budget_source']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Only approved plan lines may initiate a requisition — the form
        # itself narrows the choice so this can't be bypassed by picking an
        # unapproved line from a stale page (build prompt v2 discipline:
        # enforce gates at the data layer, not just the UI).
        self.fields['plan_line'].queryset = PlanLine.objects.filter(status=PlanLine.Status.APPROVED)

    def clean(self):
        cleaned = super().clean()
        plan_line = cleaned.get('plan_line')
        if plan_line:
            # Set here (not left to construct_instance) so Requisition.clean()
            # — run next, during _post_clean() — validates against the real
            # value, and so requested_value > plan_line.estimated_cost surfaces
            # as a normal form error instead of an uncaught exception.
            self.instance.department = plan_line.department
        return cleaned


class FundsConfirmationForm(forms.Form):
    note = forms.CharField(widget=forms.Textarea(attrs={'rows': 3}), required=False)


class FundsDeclineForm(forms.Form):
    reason = forms.CharField(widget=forms.Textarea(attrs={'rows': 3}), required=True)


class PackagingReviewForm(forms.Form):
    note = forms.CharField(
        widget=forms.Textarea(attrs={'rows': 4}),
        required=True,
        help_text='Required: document that this requisition was checked against recent similar '
                   'requisitions for suspected splitting, per the anti-splitting review gate.',
    )


class MethodDeterminationForm(forms.Form):
    method_override = forms.ChoiceField(choices=[], required=False, label='Override method (optional)')
    override_justification = forms.CharField(
        widget=forms.Textarea(attrs={'rows': 3}), required=False,
        help_text='Required only if overriding the default method.',
    )

    def __init__(self, *args, law_profile=None, **kwargs):
        super().__init__(*args, **kwargs)
        methods = law_profile.procurement_methods if law_profile else []
        self.fields['method_override'].choices = [('', 'Use default method for this value')] + [
            (m, m) for m in methods
        ]

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('method_override') and not cleaned.get('override_justification', '').strip():
            self.add_error('override_justification', 'A justification is required when overriding the method.')
        return cleaned


class RecordFromRequisitionForm(forms.ModelForm):
    """Deliberately narrow — only the fields a requisition can't already
    supply. department/budget_source/estimated_cost/procurement_method/
    law_profile are taken from the requisition server-side, not re-entered
    (see services.create_record_from_requisition)."""

    class Meta:
        model = ProcurementRecord
        fields = [
            'title', 'description', 'location', 'planned_start_date', 'planned_end_date',
            'vendor_name', 'vendor_registration_no',
        ]
        widgets = {
            'description': forms.Textarea(attrs={'rows': 4}),
            'planned_start_date': forms.DateInput(attrs={'type': 'date'}),
            'planned_end_date': forms.DateInput(attrs={'type': 'date'}),
        }


class RejectWithReasonForm(forms.Form):
    reason = forms.CharField(widget=forms.Textarea(attrs={'rows': 3}), required=True)


# --- Phase 2 (non-cryptographic slice): solicitation -> advertisement ---

class SolicitationForm(forms.ModelForm):
    technical_weight_pct = forms.IntegerField(
        required=False, min_value=0, max_value=100, label='Technical weight (%)'
    )
    financial_weight_pct = forms.IntegerField(
        required=False, min_value=0, max_value=100, label='Financial weight (%)'
    )

    class Meta:
        model = Solicitation
        fields = [
            'eligibility_criteria', 'scope_and_specifications', 'evaluation_criteria',
            'bid_security_required', 'bid_security_type', 'bid_security_amount',
        ]
        widgets = {
            'eligibility_criteria': forms.Textarea(attrs={'rows': 4}),
            'scope_and_specifications': forms.Textarea(attrs={'rows': 6}),
            'evaluation_criteria': forms.Textarea(attrs={'rows': 4}),
        }

    def clean(self):
        cleaned = super().clean()
        tech, fin = cleaned.get('technical_weight_pct'), cleaned.get('financial_weight_pct')
        if tech is not None and fin is not None and tech + fin != 100:
            self.add_error('financial_weight_pct', 'Technical and financial weights must sum to 100.')
        cleaned['evaluation_weights'] = (
            {'technical': tech, 'financial': fin} if (tech is not None or fin is not None) else {}
        )
        if cleaned.get('bid_security_required') and not cleaned.get('bid_security_amount'):
            self.add_error('bid_security_amount', 'Required when bid security is required.')
        return cleaned

    def solicitation_fields(self):
        """The subset of cleaned_data that maps onto Solicitation model
        fields — strips the two helper weight inputs, which are folded into
        evaluation_weights above (see services.prepare_solicitation)."""
        return {
            k: v for k, v in self.cleaned_data.items()
            if k not in ('technical_weight_pct', 'financial_weight_pct')
        }


class AdvertisementForm(forms.Form):
    channels = forms.MultipleChoiceField(choices=Advertisement.CHANNEL_CHOICES, widget=forms.CheckboxSelectMultiple)
    publication_proof = forms.CharField(widget=forms.Textarea(attrs={'rows': 4}))
    closing_date = forms.DateField(widget=forms.DateInput(attrs={'type': 'date'}))
