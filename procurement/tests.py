import datetime

from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.urls import reverse

from .forms import ProcurementRecordForm
from .models import LawProfile, ProcurementRecord, RecordFlag, StatusUpdate, User
from .services import transition_status


def make_law_profile():
    return LawProfile.objects.create(
        slug='federal-ppa-2007',
        jurisdiction_type='federal',
        governing_law='Public Procurement Act No. 14, 2007',
        regulating_body='Bureau of Public Procurement (BPP)',
        procurement_methods=['Open Competitive Bidding', 'Request for Quotations'],
        approval_thresholds=[{'min': 0, 'max': 5000000, 'approving_authority': 'Accounting Officer'}],
    )


def make_record(law_profile, actor, **overrides):
    defaults = dict(
        title='Test Project',
        department='Faculty of Science',
        budget_source=ProcurementRecord.BudgetSource.IGR,
        estimated_cost=1_000_000,
        procurement_method='Open Competitive Bidding',
        location='Main Campus',
        planned_start_date=datetime.date.today(),
        planned_end_date=datetime.date.today() + datetime.timedelta(days=30),
        law_profile=law_profile,
        created_by=actor,
    )
    defaults.update(overrides)
    return ProcurementRecord.objects.create(**defaults)


class StatusTransitionTests(TestCase):
    """The audit trail is the platform's core integrity control (build
    prompt v2 section 3.6) — verify it can't be bypassed or left partial."""

    def setUp(self):
        self.law_profile = make_law_profile()
        self.actor = User.objects.create_user(username='officer', password='x', role=User.Role.PROCUREMENT_OFFICER)
        self.record = make_record(self.law_profile, self.actor)

    def test_transition_updates_status_and_writes_audit_row(self):
        transition_status(record=self.record, new_status='Advertised', updated_by=self.actor, note='Published tender.')
        self.record.refresh_from_db()
        self.assertEqual(self.record.status, 'Advertised')

        updates = list(StatusUpdate.objects.filter(record=self.record))
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].old_status, 'Planning')
        self.assertEqual(updates[0].new_status, 'Advertised')
        self.assertEqual(updates[0].note, 'Published tender.')
        self.assertEqual(updates[0].updated_by, self.actor)

    def test_multiple_transitions_build_full_history_in_order(self):
        for status in ['Advertised', 'Tendering', 'Awarded']:
            transition_status(record=self.record, new_status=status, updated_by=self.actor, note=f'-> {status}')
        history = list(self.record.status_updates.all())
        self.assertEqual([u.new_status for u in history], ['Advertised', 'Tendering', 'Awarded'])
        self.assertEqual([u.old_status for u in history], ['Planning', 'Advertised', 'Tendering'])


class LawProfileValidationTests(TestCase):
    """procurement_method must always come from the record's law profile
    data, never a hardcoded global enum (build prompt section 5)."""

    def setUp(self):
        self.law_profile = make_law_profile()
        self.actor = User.objects.create_user(username='officer', password='x', role=User.Role.PROCUREMENT_OFFICER)

    def test_model_clean_rejects_method_not_in_law_profile(self):
        record = make_record(self.law_profile, self.actor, procurement_method='Emergency Procurement')
        with self.assertRaises(ValidationError):
            record.clean()

    def test_model_clean_accepts_method_in_law_profile(self):
        record = make_record(self.law_profile, self.actor, procurement_method='Request for Quotations')
        record.clean()  # should not raise

    def test_form_rejects_method_not_in_law_profile(self):
        form = ProcurementRecordForm(data={
            'title': 'Bad Method Test',
            'department': 'Bursary',
            'budget_source': ProcurementRecord.BudgetSource.IGR,
            'estimated_cost': '500000',
            'procurement_method': 'Not A Real Method',
            'location': 'Main Campus',
            'planned_start_date': datetime.date.today(),
            'planned_end_date': datetime.date.today() + datetime.timedelta(days=10),
            'law_profile': self.law_profile.pk,
        })
        self.assertFalse(form.is_valid())
        self.assertIn('procurement_method', form.errors)

    def test_new_unsaved_record_form_populates_method_choices(self):
        """Regression test: ProcurementRecord.id uses default=uuid.uuid4, so
        a fresh unsaved instance's .pk is never None/falsy — a naive
        `not self.instance.pk` check to detect "new record" silently fails
        for this model and left the create form's procurement_method
        dropdown empty in production (nobody could create a record via the
        UI at all). See forms.py ProcurementRecordForm.__init__."""
        form = ProcurementRecordForm()
        self.assertTrue(form.instance._state.adding)
        self.assertGreater(len(form.fields['procurement_method'].choices), 0)

    def test_staff_can_create_record_end_to_end(self):
        """Drives the real view, not just the form in isolation — this is
        the exact path that was broken in production."""
        self.client.force_login(self.actor)
        response = self.client.post(reverse('staff_record_create'), {
            'title': 'End-to-End Create Test',
            'department': 'Bursary',
            'budget_source': ProcurementRecord.BudgetSource.IGR,
            'estimated_cost': '750000',
            'procurement_method': self.law_profile.procurement_methods[0],
            'location': 'Main Campus',
            'planned_start_date': datetime.date.today(),
            'planned_end_date': datetime.date.today() + datetime.timedelta(days=10),
            'law_profile': self.law_profile.pk,
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(ProcurementRecord.objects.filter(title='End-to-End Create Test').exists())


class CostOutlierTests(TestCase):
    """Rule-based (not ML) cost-outlier flag (build prompt v2 Phase 2 item 2)."""

    def setUp(self):
        self.law_profile = make_law_profile()
        self.actor = User.objects.create_user(username='officer', password='x', role=User.Role.PROCUREMENT_OFFICER)

    def test_no_flag_without_enough_comparables(self):
        record = make_record(self.law_profile, self.actor, awarded_cost=9_000_000)
        self.assertIsNone(record.cost_outlier_ratio())
        self.assertFalse(record.is_cost_outlier())

    def test_flags_when_significantly_above_peer_median(self):
        for cost in [2_100_000, 2_300_000, 2_600_000]:
            make_record(self.law_profile, self.actor, awarded_cost=cost)
        outlier = make_record(self.law_profile, self.actor, awarded_cost=7_800_000)
        self.assertGreaterEqual(outlier.cost_outlier_ratio(), ProcurementRecord.COST_OUTLIER_THRESHOLD)
        self.assertTrue(outlier.is_cost_outlier())

    def test_does_not_flag_normally_priced_record_among_peers(self):
        for cost in [2_100_000, 2_300_000, 2_600_000]:
            make_record(self.law_profile, self.actor, awarded_cost=cost)
        normal = make_record(self.law_profile, self.actor, awarded_cost=2_400_000)
        self.assertFalse(normal.is_cost_outlier())


class RecordFlagTests(TestCase):
    """Public citizen flagging (build prompt v2 Phase 2 item 1) — no login,
    one flag per browser session per record, no moderation workflow."""

    def setUp(self):
        self.law_profile = make_law_profile()
        self.actor = User.objects.create_user(username='officer', password='x', role=User.Role.PROCUREMENT_OFFICER)
        self.record = make_record(self.law_profile, self.actor)
        self.client = Client()

    def test_flagging_requires_no_authentication(self):
        response = self.client.post(
            reverse('flag_record', args=[self.record.id]), {'note': 'Looks off'}, follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(RecordFlag.objects.filter(record=self.record).count(), 1)

    def test_second_flag_from_same_session_is_blocked(self):
        url = reverse('flag_record', args=[self.record.id])
        self.client.post(url, {'note': 'First'})
        self.client.post(url, {'note': 'Second attempt'})
        self.assertEqual(RecordFlag.objects.filter(record=self.record).count(), 1)

    def test_flag_count_visible_on_public_detail_page(self):
        RecordFlag.objects.create(record=self.record, note='Concerning')
        response = self.client.get(reverse('public_record_detail', args=[self.record.id]))
        self.assertContains(response, '1')


class PublicAccessTests(TestCase):
    """Public dashboard must work with zero authentication, always
    (build prompt section 3 point 2 — a permanent product decision)."""

    def setUp(self):
        self.law_profile = make_law_profile()
        self.actor = User.objects.create_user(username='officer', password='x', role=User.Role.PROCUREMENT_OFFICER)
        self.record = make_record(self.law_profile, self.actor)
        self.client = Client()

    def test_dashboard_accessible_without_login(self):
        response = self.client.get(reverse('public_dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.record.title)

    def test_detail_page_accessible_without_login(self):
        response = self.client.get(reverse('public_record_detail', args=[self.record.id]))
        self.assertEqual(response.status_code, 200)

    def test_export_json_accessible_without_login(self):
        response = self.client.get(reverse('export_json'))
        self.assertEqual(response.status_code, 200)

    def test_staff_list_redirects_when_not_logged_in(self):
        response = self.client.get(reverse('staff_record_list'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/staff/login/', response.url)

    def test_staff_list_accessible_once_logged_in(self):
        self.client.force_login(self.actor)
        response = self.client.get(reverse('staff_record_list'))
        self.assertEqual(response.status_code, 200)

    def test_status_change_without_login_is_rejected(self):
        response = self.client.post(
            reverse('staff_status_transition', args=[self.record.id]),
            {'new_status': 'Advertised', 'note': 'Should not work'},
        )
        self.assertEqual(response.status_code, 302)
        self.record.refresh_from_db()
        self.assertEqual(self.record.status, 'Planning')
