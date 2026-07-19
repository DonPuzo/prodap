from django import forms
from django.contrib.auth.forms import AuthenticationForm

from .i18n import DEFAULT_LANG, get_strings
from .models import LawProfile, ProcurementRecord


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
        choices = [c for c in ProcurementRecord.Status.choices if c[0] != current_status]
        self.fields['new_status'].choices = choices
