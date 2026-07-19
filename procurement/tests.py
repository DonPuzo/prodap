import datetime

from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.urls import reverse

from .forms import ProcurementRecordForm
from .models import (
    AuditEvent,
    FinancialYear,
    PlanLine,
    ProcurementPlan,
    ProcurementRecord,
    RecordFlag,
    Requisition,
    StatusUpdate,
    ThresholdRule,
    User,
)
from .models import LawProfile
from .services import (
    SeparationOfDutiesError,
    approve_plan,
    confirm_requisition_funds,
    create_record_from_requisition,
    determine_default_method,
    determine_requisition_method,
    get_approving_authority,
    reject_plan,
    review_requisition_packaging,
    submit_plan,
    submit_requisition,
    transition_status,
)


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


def make_financial_year(law_profile, label='FY2026'):
    return FinancialYear.objects.create(
        law_profile=law_profile, label=label,
        start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 12, 31), is_current=True,
    )


def make_threshold_rule(law_profile, actor, method='Open Competitive Bidding', min_value=0, max_value=5_000_000,
                         authority='Accounting Officer', is_default=True, effective_from=None, effective_to=None):
    return ThresholdRule.objects.create(
        law_profile=law_profile, procurement_method=method, min_value=min_value, max_value=max_value,
        approving_authority=authority, is_default_for_range=is_default,
        effective_from=effective_from or datetime.date(2020, 1, 1), effective_to=effective_to, created_by=actor,
    )


class StatusTransitionTests(TestCase):
    """The audit trail is the platform's core integrity control (build
    prompt v2 section 3.6) — verify it can't be bypassed or left partial."""

    def setUp(self):
        self.law_profile = make_law_profile()
        self.actor = User.objects.create_user(username='officer', password='x', role=User.Role.PROCUREMENT_UNIT)
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
        self.actor = User.objects.create_user(username='officer', password='x', role=User.Role.PROCUREMENT_UNIT)

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
        dropdown empty in production. See forms.py ProcurementRecordForm.__init__."""
        form = ProcurementRecordForm()
        self.assertTrue(form.instance._state.adding)
        self.assertGreater(len(form.fields['procurement_method'].choices), 0)


class CostOutlierTests(TestCase):
    """Rule-based (not ML) cost-outlier flag (build prompt v2 Phase 2 item 2)."""

    def setUp(self):
        self.law_profile = make_law_profile()
        self.actor = User.objects.create_user(username='officer', password='x', role=User.Role.PROCUREMENT_UNIT)

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
        self.actor = User.objects.create_user(username='officer', password='x', role=User.Role.PROCUREMENT_UNIT)
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
        self.actor = User.objects.create_user(username='officer', password='x', role=User.Role.PROCUREMENT_UNIT)
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


# --- Phase 1-Foundation: annual plans -> requisitions -> funds confirmation
# -> packaging review -> method determination -> record creation. ---

class ThresholdRuleTests(TestCase):
    def setUp(self):
        self.law_profile = make_law_profile()
        self.actor = User.objects.create_user(username='finance1', password='x', role=User.Role.FINANCE)

    def test_value_in_range_resolves_correct_rule(self):
        make_threshold_rule(self.law_profile, self.actor, max_value=5_000_000, authority='Accounting Officer')
        make_threshold_rule(
            self.law_profile, self.actor, min_value=5_000_001, max_value=None,
            authority='Tenders Board', is_default=False,
        )
        rule = get_approving_authority(
            law_profile=self.law_profile, method='Open Competitive Bidding', value=2_000_000
        )
        self.assertEqual(rule.approving_authority, 'Accounting Officer')

    def test_out_of_range_raises(self):
        make_threshold_rule(self.law_profile, self.actor, min_value=0, max_value=1_000_000)
        with self.assertRaises(ValidationError):
            get_approving_authority(
                law_profile=self.law_profile, method='Open Competitive Bidding', value=5_000_000
            )

    def test_effective_date_boundary_picks_the_currently_active_rule(self):
        old = make_threshold_rule(
            self.law_profile, self.actor, authority='Old Authority',
            effective_from=datetime.date(2020, 1, 1), effective_to=datetime.date(2025, 12, 31),
        )
        make_threshold_rule(
            self.law_profile, self.actor, authority='New Authority', effective_from=datetime.date(2026, 1, 1),
        )
        rule = get_approving_authority(
            law_profile=self.law_profile, method='Open Competitive Bidding', value=1_000_000,
            as_of=datetime.date(2026, 6, 1),
        )
        self.assertEqual(rule.approving_authority, 'New Authority')
        # And the superseded rule is still queryable historically, never deleted.
        self.assertTrue(ThresholdRule.objects.filter(pk=old.pk).exists())

    def test_determine_default_method_picks_default_rule(self):
        make_threshold_rule(self.law_profile, self.actor, is_default=True)
        rule = determine_default_method(law_profile=self.law_profile, value=1_000_000)
        self.assertTrue(rule.is_default_for_range)


class ProcurementPlanWorkflowTests(TestCase):
    def setUp(self):
        self.law_profile = make_law_profile()
        self.fy = make_financial_year(self.law_profile)
        self.preparer = User.objects.create_user(username='pu_prep', password='x', role=User.Role.PROCUREMENT_UNIT)
        self.approver = User.objects.create_user(username='ao_appr', password='x', role=User.Role.ACCOUNTING_OFFICER)
        self.requester = User.objects.create_user(username='ru_req', password='x', role=User.Role.REQUESTING_UNIT)

    def make_plan(self):
        return ProcurementPlan.objects.create(
            law_profile=self.law_profile, financial_year=self.fy, prepared_by=self.preparer
        )

    def test_submit_then_approve_bulk_approves_pending_lines(self):
        plan = self.make_plan()
        line = PlanLine.objects.create(
            plan=plan, department='Bursary', item_description='Chairs', justification='Need chairs',
            quantity=10, estimated_cost=500_000, budget_line='B1', proposed_quarter='Q1',
            proposed_by=self.requester,
        )
        submit_plan(plan=plan, actor=self.preparer)
        plan.refresh_from_db()
        self.assertEqual(plan.status, ProcurementPlan.Status.SUBMITTED)

        approve_plan(plan=plan, actor=self.approver)
        plan.refresh_from_db()
        line.refresh_from_db()
        self.assertEqual(plan.status, ProcurementPlan.Status.APPROVED)
        self.assertEqual(line.status, PlanLine.Status.APPROVED)
        self.assertTrue(
            AuditEvent.objects.filter(action=AuditEvent.Action.PLAN_APPROVED, object_id=plan.id).exists()
        )

    def test_reject_requires_reason(self):
        plan = self.make_plan()
        with self.assertRaises(ValidationError):
            reject_plan(plan=plan, actor=self.approver, reason='')

    def test_approve_plan_separation_of_duties(self):
        plan = self.make_plan()
        with self.assertRaises(SeparationOfDutiesError):
            approve_plan(plan=plan, actor=self.preparer)


class PlanLineAmendmentTests(TestCase):
    def setUp(self):
        self.law_profile = make_law_profile()
        self.fy = make_financial_year(self.law_profile)
        self.preparer = User.objects.create_user(username='pu_amend', password='x', role=User.Role.PROCUREMENT_UNIT)
        self.approver = User.objects.create_user(username='ao_amend', password='x', role=User.Role.ACCOUNTING_OFFICER)
        self.requester = User.objects.create_user(username='ru_amend', password='x', role=User.Role.REQUESTING_UNIT)
        self.plan = ProcurementPlan.objects.create(
            law_profile=self.law_profile, financial_year=self.fy, prepared_by=self.preparer
        )

    def test_line_added_to_approved_plan_stays_pending_as_amendment(self):
        approve_plan(plan=self.plan, actor=self.approver)
        line = PlanLine.objects.create(
            plan=self.plan, department='Bursary', item_description='New need', justification='x',
            estimated_cost=100_000, budget_line='B1', proposed_quarter='Q2', proposed_by=self.requester,
            is_amendment=True,
        )
        self.assertEqual(line.status, PlanLine.Status.PENDING)

    def test_requisition_cannot_be_submitted_against_pending_line(self):
        line = PlanLine.objects.create(
            plan=self.plan, department='Bursary', item_description='New need', justification='x',
            estimated_cost=100_000, budget_line='B1', proposed_quarter='Q2', proposed_by=self.requester,
        )
        requisition = Requisition.objects.create(
            plan_line=line, title='Req', department='Bursary', requested_value=100_000,
            budget_source=ProcurementRecord.BudgetSource.IGR, requested_by=self.requester,
        )
        with self.assertRaises(ValidationError):
            submit_requisition(requisition=requisition, actor=self.requester)


class RequisitionGateTests(TestCase):
    def setUp(self):
        self.law_profile = make_law_profile()
        self.fy = make_financial_year(self.law_profile)
        self.preparer = User.objects.create_user(username='pu_req', password='x', role=User.Role.PROCUREMENT_UNIT)
        self.approver = User.objects.create_user(username='ao_req', password='x', role=User.Role.ACCOUNTING_OFFICER)
        self.requester = User.objects.create_user(username='ru_req2', password='x', role=User.Role.REQUESTING_UNIT)
        self.finance = User.objects.create_user(username='fin_req', password='x', role=User.Role.FINANCE)
        make_threshold_rule(self.law_profile, self.preparer, max_value=5_000_000, authority='Accounting Officer')

        self.plan = ProcurementPlan.objects.create(
            law_profile=self.law_profile, financial_year=self.fy, prepared_by=self.preparer
        )
        self.line = PlanLine.objects.create(
            plan=self.plan, department='Bursary', item_description='Chairs', justification='need',
            estimated_cost=500_000, budget_line='B1', proposed_quarter='Q1', proposed_by=self.requester,
        )
        approve_plan(plan=self.plan, actor=self.approver)
        self.line.refresh_from_db()

    def make_requisition(self):
        return Requisition.objects.create(
            plan_line=self.line, title='Chairs requisition', department='Bursary',
            requested_value=500_000, budget_source=ProcurementRecord.BudgetSource.IGR,
            requested_by=self.requester,
        )

    def test_confirm_funds_separation_of_duties(self):
        req = self.make_requisition()
        submit_requisition(requisition=req, actor=self.requester)
        with self.assertRaises(SeparationOfDutiesError):
            confirm_requisition_funds(requisition=req, actor=self.requester)

    def test_process_identifier_set_only_after_funds_confirmed_and_unique(self):
        req = self.make_requisition()
        self.assertFalse(req.process_identifier)
        submit_requisition(requisition=req, actor=self.requester)
        confirm_requisition_funds(requisition=req, actor=self.finance)
        req.refresh_from_db()
        self.assertTrue(req.process_identifier)

        line2 = PlanLine.objects.create(
            plan=self.plan, department='Bursary', item_description='Tables', justification='need',
            estimated_cost=300_000, budget_line='B1', proposed_quarter='Q1', proposed_by=self.requester,
            status=PlanLine.Status.APPROVED,
        )
        req2 = Requisition.objects.create(
            plan_line=line2, title='Tables requisition', department='Bursary', requested_value=300_000,
            budget_source=ProcurementRecord.BudgetSource.IGR, requested_by=self.requester,
        )
        submit_requisition(requisition=req2, actor=self.requester)
        confirm_requisition_funds(requisition=req2, actor=self.finance)
        req2.refresh_from_db()
        self.assertNotEqual(req.process_identifier, req2.process_identifier)

    def test_determine_method_refuses_before_packaging_reviewed(self):
        req = self.make_requisition()
        submit_requisition(requisition=req, actor=self.requester)
        confirm_requisition_funds(requisition=req, actor=self.finance)
        with self.assertRaises(ValidationError):
            determine_requisition_method(requisition=req, actor=self.preparer)

    def test_create_record_refuses_until_all_gates_passed(self):
        req = self.make_requisition()
        with self.assertRaises(ValidationError):
            create_record_from_requisition(requisition=req, actor=self.preparer, record_fields={
                'title': 'X', 'location': 'Main', 'planned_start_date': datetime.date.today(),
                'planned_end_date': datetime.date.today() + datetime.timedelta(days=30),
            })

    def test_full_happy_path_plan_to_record(self):
        req = self.make_requisition()
        submit_requisition(requisition=req, actor=self.requester)
        confirm_requisition_funds(requisition=req, actor=self.finance, note='Funds available')
        review_requisition_packaging(
            requisition=req, actor=self.preparer, note='Checked — no similar requisitions found.'
        )
        determine_requisition_method(requisition=req, actor=self.preparer)
        req.refresh_from_db()
        self.assertEqual(req.determined_method, 'Open Competitive Bidding')
        self.assertEqual(req.determined_approving_authority, 'Accounting Officer')

        record = create_record_from_requisition(requisition=req, actor=self.preparer, record_fields={
            'title': 'Chairs for Bursary', 'location': 'Main Campus',
            'planned_start_date': datetime.date.today(),
            'planned_end_date': datetime.date.today() + datetime.timedelta(days=30),
        })
        self.assertEqual(record.requisition, req)
        self.assertEqual(record.department, 'Bursary')
        self.assertEqual(record.procurement_method, 'Open Competitive Bidding')
        self.assertEqual(record.law_profile, self.law_profile)

        req.refresh_from_db()
        self.assertEqual(req.status, Requisition.Status.RECORD_CREATED)

        events = set(AuditEvent.objects.filter(
            content_type__model='requisition', object_id=req.id
        ).values_list('action', flat=True))
        self.assertIn(AuditEvent.Action.REQUISITION_SUBMITTED, events)
        self.assertIn(AuditEvent.Action.FUNDS_CONFIRMED, events)
        self.assertIn(AuditEvent.Action.PACKAGING_REVIEWED, events)
        self.assertIn(AuditEvent.Action.METHOD_DETERMINED, events)
        self.assertIn(AuditEvent.Action.RECORD_CREATED_FROM_REQUISITION, events)


class RoleGateViewTests(TestCase):
    def setUp(self):
        self.requester = User.objects.create_user(username='ru_view', password='x', role=User.Role.REQUESTING_UNIT)
        self.client = Client()

    def test_plan_create_rejects_wrong_role(self):
        self.client.force_login(self.requester)
        response = self.client.get(reverse('staff_plan_create'))
        self.assertEqual(response.status_code, 403)

    def test_plan_create_allows_procurement_unit(self):
        pu = User.objects.create_user(username='pu_view', password='x', role=User.Role.PROCUREMENT_UNIT)
        self.client.force_login(pu)
        response = self.client.get(reverse('staff_plan_create'))
        self.assertEqual(response.status_code, 200)

    def test_superuser_bypasses_role_check(self):
        admin = User.objects.create_superuser(username='admin_view', password='x', role=User.Role.ADMIN)
        self.client.force_login(admin)
        response = self.client.get(reverse('staff_plan_create'))
        self.assertEqual(response.status_code, 200)

    def test_anonymous_redirects_to_login(self):
        response = self.client.get(reverse('staff_plan_create'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/staff/login/', response.url)


class LegacyDataCompatibilityTests(TestCase):
    """Explicit regression guard: pre-Foundation records (requisition=None)
    still work through the untouched edit/status-transition views."""

    def setUp(self):
        self.law_profile = make_law_profile()
        self.actor = User.objects.create_user(username='legacy_officer', password='x', role=User.Role.PROCUREMENT_UNIT)
        self.record = make_record(self.law_profile, self.actor)
        self.client = Client()

    def test_legacy_record_has_no_requisition(self):
        self.assertIsNone(self.record.requisition)

    def test_legacy_record_full_clean_still_passes(self):
        self.record.full_clean()

    def test_legacy_record_edit_view_still_works(self):
        self.client.force_login(self.actor)
        response = self.client.post(reverse('staff_record_edit', args=[self.record.id]), {
            'title': 'Updated Title', 'department': self.record.department,
            'budget_source': self.record.budget_source, 'estimated_cost': self.record.estimated_cost,
            'procurement_method': self.record.procurement_method, 'location': self.record.location,
            'planned_start_date': self.record.planned_start_date, 'planned_end_date': self.record.planned_end_date,
            'law_profile': self.law_profile.pk,
        })
        self.assertEqual(response.status_code, 302)
        self.record.refresh_from_db()
        self.assertEqual(self.record.title, 'Updated Title')

    def test_legacy_record_status_transition_still_works(self):
        self.client.force_login(self.actor)
        response = self.client.post(reverse('staff_status_transition', args=[self.record.id]), {
            'new_status': 'Advertised', 'note': 'Moving forward',
        })
        self.assertEqual(response.status_code, 302)
        self.record.refresh_from_db()
        self.assertEqual(self.record.status, 'Advertised')


class AuditEventTests(TestCase):
    def setUp(self):
        self.law_profile = make_law_profile()
        self.fy = make_financial_year(self.law_profile)
        self.preparer = User.objects.create_user(username='pu_audit', password='x', role=User.Role.PROCUREMENT_UNIT)

    def test_plan_submission_writes_exactly_one_audit_event(self):
        plan = ProcurementPlan.objects.create(
            law_profile=self.law_profile, financial_year=self.fy, prepared_by=self.preparer
        )
        submit_plan(plan=plan, actor=self.preparer)
        events = AuditEvent.objects.filter(content_type__model='procurementplan', object_id=plan.id)
        self.assertEqual(events.count(), 1)
        event = events.first()
        self.assertEqual(event.action, AuditEvent.Action.PLAN_SUBMITTED)
        self.assertEqual(event.actor, self.preparer)
        self.assertEqual(event.role_at_time, User.Role.PROCUREMENT_UNIT)
