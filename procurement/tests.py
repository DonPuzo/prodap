import datetime

from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.urls import reverse

from .forms import ProcurementRecordForm, RequisitionForm
from .models import (
    Advertisement,
    Award,
    AuditEvent,
    Bid,
    Clarification,
    Complaint,
    Contract,
    FinancialYear,
    Milestone,
    PlanLine,
    PrequalificationApplicant,
    ProcurementPlan,
    ProcurementRecord,
    RecordFlag,
    Requisition,
    Solicitation,
    StatusUpdate,
    ThresholdRule,
    User,
)
from .models import LawProfile
from .services import (
    SeparationOfDutiesError,
    add_milestone,
    answer_clarification,
    approve_plan,
    approve_plan_line,
    approve_solicitation,
    award_solicitation,
    complete_milestone,
    confirm_requisition_funds,
    create_record_from_requisition,
    determine_default_method,
    determine_requisition_method,
    get_approving_authority,
    get_current_solicitation,
    prepare_solicitation,
    publish_advertisement,
    record_bid,
    record_prequalification_applicant,
    reject_plan,
    reject_plan_line,
    reject_solicitation,
    resolve_complaint,
    review_prequalification_applicant,
    review_requisition_packaging,
    sign_contract,
    submit_clarification_question,
    submit_complaint,
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

    def test_landing_page_accessible_without_login(self):
        response = self.client.get(reverse('public_dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Browse the register')  # audience card CTA, landing-page-specific
        self.assertNotContains(response, self.record.title)  # no raw table on the landing page

    def test_register_page_accessible_without_login_and_lists_records(self):
        response = self.client.get(reverse('public_register'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.record.title)

    def test_register_page_search_still_works(self):
        response = self.client.get(reverse('public_register'), {'q': self.record.title})
        self.assertContains(response, self.record.title)

    def test_about_page_accessible_without_login(self):
        response = self.client.get(reverse('public_about'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Planning')

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
        """Planning -> Advertised specifically now requires
        publish_advertisement (see StatusTransitionForm) — every OTHER
        transition, including for legacy no-requisition records, is
        untouched by that change (Phase 2 non-cryptographic slice)."""
        self.client.force_login(self.actor)
        response = self.client.post(reverse('staff_status_transition', args=[self.record.id]), {
            'new_status': 'Abandoned', 'note': 'Moving forward',
        })
        self.assertEqual(response.status_code, 302)
        self.record.refresh_from_db()
        self.assertEqual(self.record.status, 'Abandoned')

    def test_planning_to_advertised_no_longer_manually_selectable(self):
        self.client.force_login(self.actor)
        response = self.client.post(reverse('staff_status_transition', args=[self.record.id]), {
            'new_status': 'Advertised', 'note': 'Attempted manual bypass',
        })
        self.assertEqual(response.status_code, 200)  # form re-rendered with error, not redirected
        self.record.refresh_from_db()
        self.assertEqual(self.record.status, 'Planning')  # unchanged


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


class GateOrderEnforcementTests(TestCase):
    """Security review regression tests: every downstream gate re-verifies
    plan_line/requisition status, and approve/reject refuse to act twice.
    Without these checks, a requisition could be walked through funds
    confirmation, packaging review, and record creation even after its
    plan line was rejected out from under it, or before it was ever
    submitted — see services.py _require_plan_line_still_approved."""

    def setUp(self):
        self.law_profile = make_law_profile()
        self.fy = make_financial_year(self.law_profile)
        self.preparer = User.objects.create_user(username='pu_gate', password='x', role=User.Role.PROCUREMENT_UNIT)
        self.approver = User.objects.create_user(username='ao_gate', password='x', role=User.Role.ACCOUNTING_OFFICER)
        self.requester = User.objects.create_user(username='ru_gate', password='x', role=User.Role.REQUESTING_UNIT)
        self.finance = User.objects.create_user(username='fin_gate', password='x', role=User.Role.FINANCE)
        make_threshold_rule(self.law_profile, self.preparer, max_value=5_000_000)
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
            plan_line=self.line, title='Chairs', department='Bursary', requested_value=500_000,
            budget_source=ProcurementRecord.BudgetSource.IGR, requested_by=self.requester,
        )

    def test_reject_plan_line_refuses_on_already_approved_line(self):
        with self.assertRaises(ValidationError):
            reject_plan_line(plan_line=self.line, actor=self.approver, reason='changed mind')

    def test_approve_plan_line_refuses_on_already_approved_line(self):
        with self.assertRaises(ValidationError):
            approve_plan_line(plan_line=self.line, actor=self.approver)

    def test_confirm_funds_refuses_unsubmitted_requisition(self):
        requisition = self.make_requisition()
        with self.assertRaises(ValidationError):
            confirm_requisition_funds(requisition=requisition, actor=self.finance)

    def test_packaging_review_refuses_before_funds_confirmed(self):
        requisition = self.make_requisition()
        submit_requisition(requisition=requisition, actor=self.requester)
        with self.assertRaises(ValidationError):
            review_requisition_packaging(requisition=requisition, actor=self.preparer, note='checked')

    def test_downstream_gate_refuses_when_plan_line_no_longer_approved(self):
        requisition = self.make_requisition()
        submit_requisition(requisition=requisition, actor=self.requester)
        confirm_requisition_funds(requisition=requisition, actor=self.finance)

        # Simulate the plan line becoming non-approved by some other means
        # after the requisition already passed funds confirmation - the
        # exact scenario the security review flagged.
        self.line.status = PlanLine.Status.REJECTED
        self.line.save(update_fields=['status'])

        with self.assertRaises(ValidationError):
            review_requisition_packaging(requisition=requisition, actor=self.preparer, note='checked')


class RequisitionValueValidationTests(TestCase):
    """Security review regression: a requisition's value/department must
    match what was actually approved on its plan line — otherwise the
    entire point of the plan-approval gate is defeated."""

    def setUp(self):
        self.law_profile = make_law_profile()
        self.fy = make_financial_year(self.law_profile)
        self.preparer = User.objects.create_user(username='pu_val', password='x', role=User.Role.PROCUREMENT_UNIT)
        self.requester = User.objects.create_user(username='ru_val', password='x', role=User.Role.REQUESTING_UNIT)
        self.approver = User.objects.create_user(username='ao_val', password='x', role=User.Role.ACCOUNTING_OFFICER)
        self.plan = ProcurementPlan.objects.create(
            law_profile=self.law_profile, financial_year=self.fy, prepared_by=self.preparer
        )
        self.line = PlanLine.objects.create(
            plan=self.plan, department='Bursary', item_description='Stationery', justification='need',
            estimated_cost=10_000, budget_line='B1', proposed_quarter='Q1', proposed_by=self.requester,
        )
        approve_plan(plan=self.plan, actor=self.approver)
        self.line.refresh_from_db()

    def test_model_clean_rejects_value_exceeding_approved_estimate(self):
        req = Requisition(
            plan_line=self.line, title='Inflated', department='Bursary', requested_value=50_000_000,
            budget_source=ProcurementRecord.BudgetSource.IGR, requested_by=self.requester,
        )
        with self.assertRaises(ValidationError):
            req.full_clean()

    def test_model_clean_accepts_value_within_approved_estimate(self):
        req = Requisition(
            plan_line=self.line, title='Reasonable', department='Bursary', requested_value=9_500,
            budget_source=ProcurementRecord.BudgetSource.IGR, requested_by=self.requester,
        )
        req.full_clean()  # should not raise

    def test_model_clean_rejects_mismatched_department(self):
        req = Requisition(
            plan_line=self.line, title='Wrong dept', department='Faculty of Science', requested_value=9_500,
            budget_source=ProcurementRecord.BudgetSource.IGR, requested_by=self.requester,
        )
        with self.assertRaises(ValidationError):
            req.full_clean()

    def test_form_rejects_value_exceeding_approved_estimate(self):
        form = RequisitionForm(data={
            'plan_line': self.line.pk, 'title': 'Inflated Requisition', 'requested_value': '50000000',
            'budget_source': ProcurementRecord.BudgetSource.IGR,
        })
        self.assertFalse(form.is_valid())
        self.assertIn('requested_value', form.errors)

    def test_form_derives_department_from_plan_line_not_user_input(self):
        form = RequisitionForm(data={
            'plan_line': self.line.pk, 'title': 'Stationery Requisition', 'requested_value': '9500',
            'budget_source': ProcurementRecord.BudgetSource.IGR,
        })
        self.assertTrue(form.is_valid(), form.errors)
        requisition = form.save(commit=False)
        self.assertEqual(requisition.department, 'Bursary')


# --- Phase 2 (non-cryptographic slice): solicitation preparation ->
# advertisement/publication. ---

SOLICITATION_FIELDS = dict(
    eligibility_criteria='Must be registered with BPP.',
    scope_and_specifications='Supply and install 50 office chairs.',
    evaluation_criteria='Lowest evaluated responsive bid.',
    evaluation_weights={},
    bid_security_required=False,
    bid_security_type='',
    bid_security_amount=None,
)


class SolicitationAdvertisementTests(TestCase):
    def setUp(self):
        self.law_profile = make_law_profile()
        self.fy = make_financial_year(self.law_profile)
        self.preparer = User.objects.create_user(username='pu_sol', password='x', role=User.Role.PROCUREMENT_UNIT)
        self.approver = User.objects.create_user(username='ao_sol', password='x', role=User.Role.ACCOUNTING_OFFICER)
        self.requester = User.objects.create_user(username='ru_sol', password='x', role=User.Role.REQUESTING_UNIT)
        self.finance = User.objects.create_user(username='fin_sol', password='x', role=User.Role.FINANCE)
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

        req = Requisition.objects.create(
            plan_line=self.line, title='Chairs requisition', department='Bursary',
            requested_value=500_000, budget_source=ProcurementRecord.BudgetSource.IGR,
            requested_by=self.requester,
        )
        submit_requisition(requisition=req, actor=self.requester)
        confirm_requisition_funds(requisition=req, actor=self.finance)
        review_requisition_packaging(requisition=req, actor=self.preparer, note='Checked, no splitting.')
        determine_requisition_method(requisition=req, actor=self.preparer)
        self.record = create_record_from_requisition(requisition=req, actor=self.preparer, record_fields={
            'title': 'Chairs for Bursary', 'location': 'Main Campus',
            'planned_start_date': datetime.date.today(),
            'planned_end_date': datetime.date.today() + datetime.timedelta(days=30),
        })

    def test_prepare_solicitation_requires_planning_status(self):
        transition_status(record=self.record, new_status='Abandoned', updated_by=self.preparer)
        with self.assertRaises(ValidationError):
            prepare_solicitation(record=self.record, actor=self.preparer, fields=SOLICITATION_FIELDS)

    def test_approve_solicitation_separation_of_duties(self):
        solicitation = prepare_solicitation(record=self.record, actor=self.preparer, fields=SOLICITATION_FIELDS)
        with self.assertRaises(SeparationOfDutiesError):
            approve_solicitation(solicitation=solicitation, actor=self.preparer)

    def test_reject_then_reprepare_creates_new_version(self):
        v1 = prepare_solicitation(record=self.record, actor=self.preparer, fields=SOLICITATION_FIELDS)
        reject_solicitation(solicitation=v1, actor=self.approver, reason='Needs clearer specifications.')
        v2 = prepare_solicitation(record=self.record, actor=self.preparer, fields=SOLICITATION_FIELDS)
        self.assertEqual(v2.version, 2)
        self.assertEqual(self.record.solicitations.count(), 2)
        self.assertEqual(get_current_solicitation(self.record), v2)

    def test_publish_advertisement_enforces_minimum_bidding_days(self):
        solicitation = prepare_solicitation(record=self.record, actor=self.preparer, fields=SOLICITATION_FIELDS)
        approve_solicitation(solicitation=solicitation, actor=self.approver)
        too_soon = datetime.date.today() + datetime.timedelta(days=1)
        with self.assertRaises(ValidationError):
            publish_advertisement(
                solicitation=solicitation, actor=self.preparer, channels=['institution_website'],
                publication_proof='Posted on website.', closing_date=too_soon,
            )

    def test_publish_advertisement_rejects_unapproved_solicitation(self):
        solicitation = prepare_solicitation(record=self.record, actor=self.preparer, fields=SOLICITATION_FIELDS)
        closing = datetime.date.today() + datetime.timedelta(days=30)
        with self.assertRaises(ValidationError):
            publish_advertisement(
                solicitation=solicitation, actor=self.preparer, channels=['institution_website'],
                publication_proof='Posted.', closing_date=closing,
            )

    def test_publish_advertisement_is_idempotent_guard(self):
        solicitation = prepare_solicitation(record=self.record, actor=self.preparer, fields=SOLICITATION_FIELDS)
        approve_solicitation(solicitation=solicitation, actor=self.approver)
        closing = datetime.date.today() + datetime.timedelta(days=30)
        publish_advertisement(
            solicitation=solicitation, actor=self.preparer, channels=['institution_website'],
            publication_proof='Posted.', closing_date=closing,
        )
        with self.assertRaises(ValidationError):
            publish_advertisement(
                solicitation=solicitation, actor=self.preparer, channels=['newspaper'],
                publication_proof='Posted again.', closing_date=closing,
            )

    def test_full_happy_path_solicitation_to_advertised(self):
        solicitation = prepare_solicitation(record=self.record, actor=self.preparer, fields=SOLICITATION_FIELDS)
        approve_solicitation(solicitation=solicitation, actor=self.approver)
        closing = datetime.date.today() + datetime.timedelta(days=30)
        publish_advertisement(
            solicitation=solicitation, actor=self.preparer, channels=['institution_website', 'newspaper'],
            publication_proof='Posted on website and Daily Times, 2026-07-20.', closing_date=closing,
        )

        self.record.refresh_from_db()
        self.assertEqual(self.record.status, ProcurementRecord.Status.ADVERTISED)

        status_updates = StatusUpdate.objects.filter(record=self.record)
        self.assertEqual(status_updates.count(), 1)
        self.assertEqual(status_updates.first().old_status, 'Planning')
        self.assertEqual(status_updates.first().new_status, 'Advertised')

        sol_events = set(AuditEvent.objects.filter(
            content_type__model='solicitation', object_id=solicitation.id
        ).values_list('action', flat=True))
        self.assertIn(AuditEvent.Action.SOLICITATION_PREPARED, sol_events)
        self.assertIn(AuditEvent.Action.SOLICITATION_APPROVED, sol_events)
        self.assertTrue(
            AuditEvent.objects.filter(
                content_type__model='advertisement', action=AuditEvent.Action.ADVERTISEMENT_PUBLISHED
            ).exists()
        )

        response = self.client.get(reverse('public_record_detail', args=[self.record.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, SOLICITATION_FIELDS['eligibility_criteria'])
        self.assertContains(response, str(closing))


class ClarificationTests(TestCase):
    """Public Q&A on a published Solicitation (blueprint step 08).
    Deliberately anonymous, and deliberately conservative about what's
    shown publicly before an answer exists — see Clarification's docstring
    in models.py."""

    def setUp(self):
        self.law_profile = make_law_profile()
        self.fy = make_financial_year(self.law_profile)
        self.preparer = User.objects.create_user(username='pu_clar', password='x', role=User.Role.PROCUREMENT_UNIT)
        self.approver = User.objects.create_user(username='ao_clar', password='x', role=User.Role.ACCOUNTING_OFFICER)
        self.requester = User.objects.create_user(username='ru_clar', password='x', role=User.Role.REQUESTING_UNIT)
        self.finance = User.objects.create_user(username='fin_clar', password='x', role=User.Role.FINANCE)
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

        req = Requisition.objects.create(
            plan_line=self.line, title='Chairs requisition', department='Bursary',
            requested_value=500_000, budget_source=ProcurementRecord.BudgetSource.IGR,
            requested_by=self.requester,
        )
        submit_requisition(requisition=req, actor=self.requester)
        confirm_requisition_funds(requisition=req, actor=self.finance)
        review_requisition_packaging(requisition=req, actor=self.preparer, note='Checked, no splitting.')
        determine_requisition_method(requisition=req, actor=self.preparer)
        self.record = create_record_from_requisition(requisition=req, actor=self.preparer, record_fields={
            'title': 'Chairs for Bursary', 'location': 'Main Campus',
            'planned_start_date': datetime.date.today(),
            'planned_end_date': datetime.date.today() + datetime.timedelta(days=30),
        })

    def publish(self, closing_days=30):
        solicitation = prepare_solicitation(record=self.record, actor=self.preparer, fields=SOLICITATION_FIELDS)
        approve_solicitation(solicitation=solicitation, actor=self.approver)
        closing = datetime.date.today() + datetime.timedelta(days=closing_days)
        publish_advertisement(
            solicitation=solicitation, actor=self.preparer, channels=['institution_website'],
            publication_proof='Posted.', closing_date=closing,
        )
        return solicitation

    def test_submit_before_publication_is_rejected(self):
        with self.assertRaises(ValidationError):
            submit_clarification_question(record=self.record, question='Is a warranty required?')

    def test_submit_after_closing_date_is_rejected(self):
        solicitation = self.publish()
        solicitation.advertisement.closing_date = datetime.date.today() - datetime.timedelta(days=1)
        solicitation.advertisement.save(update_fields=['closing_date'])
        with self.assertRaises(ValidationError):
            submit_clarification_question(record=self.record, question='Too late?')

    def test_submitted_question_not_publicly_visible_until_answered(self):
        self.publish()
        submit_clarification_question(record=self.record, question='Is a warranty required on the chairs?')
        response = self.client.get(reverse('public_record_detail', args=[self.record.id]))
        self.assertNotContains(response, 'Is a warranty required on the chairs?')
        self.assertContains(response, '1 question(s) awaiting a response.')

    def test_answer_twice_raises(self):
        self.publish()
        clarification = submit_clarification_question(record=self.record, question='Q?')
        answer_clarification(clarification=clarification, actor=self.preparer, answer='A.')
        with self.assertRaises(ValidationError):
            answer_clarification(clarification=clarification, actor=self.preparer, answer='A again.')

    def test_full_happy_path_submit_answer_visible_publicly(self):
        self.publish()
        clarification = submit_clarification_question(
            record=self.record, question='Is a warranty required on the chairs?'
        )
        answer_clarification(
            clarification=clarification, actor=self.preparer,
            answer='Yes, a minimum 12-month warranty is required.',
        )
        self.assertTrue(
            AuditEvent.objects.filter(
                content_type__model='clarification', action=AuditEvent.Action.CLARIFICATION_ANSWERED,
            ).exists()
        )
        response = self.client.get(reverse('public_record_detail', args=[self.record.id]))
        self.assertContains(response, 'Is a warranty required on the chairs?')
        self.assertContains(response, 'Yes, a minimum 12-month warranty is required.')

    def test_staff_answer_view_rejects_wrong_role(self):
        self.publish()
        clarification = submit_clarification_question(record=self.record, question='Q?')
        self.client.force_login(self.requester)
        response = self.client.post(
            reverse('staff_clarification_answer', args=[clarification.id]), {'answer': 'A.'}
        )
        self.assertEqual(response.status_code, 403)
        clarification.refresh_from_db()
        self.assertEqual(clarification.answer, '')

    def test_staff_answer_view_allows_procurement_unit(self):
        self.publish()
        clarification = submit_clarification_question(record=self.record, question='Q?')
        self.client.force_login(self.preparer)
        response = self.client.post(
            reverse('staff_clarification_answer', args=[clarification.id]), {'answer': 'A.'}
        )
        self.assertEqual(response.status_code, 302)
        clarification.refresh_from_db()
        self.assertEqual(clarification.answer, 'A.')


class PrequalificationTests(TestCase):
    """Staff-recorded EOI/prequalification tracking (blueprint step 07's
    other half). Not gated by procurement method — see
    PrequalificationApplicant's docstring in models.py."""

    def setUp(self):
        self.law_profile = make_law_profile()
        self.fy = make_financial_year(self.law_profile)
        self.preparer = User.objects.create_user(username='pu_preq', password='x', role=User.Role.PROCUREMENT_UNIT)
        self.approver = User.objects.create_user(username='ao_preq', password='x', role=User.Role.ACCOUNTING_OFFICER)
        self.requester = User.objects.create_user(username='ru_preq', password='x', role=User.Role.REQUESTING_UNIT)
        self.finance = User.objects.create_user(username='fin_preq', password='x', role=User.Role.FINANCE)
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

        req = Requisition.objects.create(
            plan_line=self.line, title='Chairs requisition', department='Bursary',
            requested_value=500_000, budget_source=ProcurementRecord.BudgetSource.IGR,
            requested_by=self.requester,
        )
        submit_requisition(requisition=req, actor=self.requester)
        confirm_requisition_funds(requisition=req, actor=self.finance)
        review_requisition_packaging(requisition=req, actor=self.preparer, note='Checked, no splitting.')
        determine_requisition_method(requisition=req, actor=self.preparer)
        self.record = create_record_from_requisition(requisition=req, actor=self.preparer, record_fields={
            'title': 'Chairs for Bursary', 'location': 'Main Campus',
            'planned_start_date': datetime.date.today(),
            'planned_end_date': datetime.date.today() + datetime.timedelta(days=30),
        })

    def publish(self):
        solicitation = prepare_solicitation(record=self.record, actor=self.preparer, fields=SOLICITATION_FIELDS)
        approve_solicitation(solicitation=solicitation, actor=self.approver)
        closing = datetime.date.today() + datetime.timedelta(days=30)
        publish_advertisement(
            solicitation=solicitation, actor=self.preparer, channels=['institution_website'],
            publication_proof='Posted.', closing_date=closing,
        )
        return solicitation

    def test_record_applicant_requires_published_solicitation(self):
        solicitation = prepare_solicitation(record=self.record, actor=self.preparer, fields=SOLICITATION_FIELDS)
        with self.assertRaises(ValidationError):
            record_prequalification_applicant(
                solicitation=solicitation, actor=self.preparer, vendor_name='Acme Furniture Ltd',
            )

    def test_record_applicant_requires_vendor_name(self):
        solicitation = self.publish()
        with self.assertRaises(ValidationError):
            record_prequalification_applicant(solicitation=solicitation, actor=self.preparer, vendor_name='   ')

    def test_review_requires_note(self):
        solicitation = self.publish()
        applicant = record_prequalification_applicant(
            solicitation=solicitation, actor=self.preparer, vendor_name='Acme Furniture Ltd',
        )
        with self.assertRaises(ValidationError):
            review_prequalification_applicant(
                applicant=applicant, actor=self.preparer, outcome=PrequalificationApplicant.Outcome.QUALIFIED, note=''
            )

    def test_review_twice_raises(self):
        solicitation = self.publish()
        applicant = record_prequalification_applicant(
            solicitation=solicitation, actor=self.preparer, vendor_name='Acme Furniture Ltd',
        )
        review_prequalification_applicant(
            applicant=applicant, actor=self.preparer,
            outcome=PrequalificationApplicant.Outcome.QUALIFIED, note='Meets registration requirements.',
        )
        with self.assertRaises(ValidationError):
            review_prequalification_applicant(
                applicant=applicant, actor=self.preparer,
                outcome=PrequalificationApplicant.Outcome.NOT_QUALIFIED, note='Changed my mind.',
            )

    def test_pending_applicant_not_publicly_visible(self):
        solicitation = self.publish()
        record_prequalification_applicant(
            solicitation=solicitation, actor=self.preparer, vendor_name='Acme Furniture Ltd',
        )
        response = self.client.get(reverse('public_record_detail', args=[self.record.id]))
        self.assertNotContains(response, 'Acme Furniture Ltd')

    def test_full_happy_path_reviewed_outcome_visible_publicly(self):
        solicitation = self.publish()
        applicant = record_prequalification_applicant(
            solicitation=solicitation, actor=self.preparer, vendor_name='Acme Furniture Ltd',
        )
        review_prequalification_applicant(
            applicant=applicant, actor=self.preparer,
            outcome=PrequalificationApplicant.Outcome.QUALIFIED, note='Meets registration requirements.',
        )
        self.assertTrue(
            AuditEvent.objects.filter(
                content_type__model='prequalificationapplicant',
                action=AuditEvent.Action.PREQUALIFICATION_REVIEWED,
            ).exists()
        )
        response = self.client.get(reverse('public_record_detail', args=[self.record.id]))
        self.assertContains(response, 'Acme Furniture Ltd')
        self.assertNotContains(response, 'Meets registration requirements.')  # review note stays staff-only

    def test_add_applicant_view_rejects_wrong_role(self):
        solicitation = self.publish()
        self.client.force_login(self.requester)
        response = self.client.post(
            reverse('staff_prequalification_add', args=[solicitation.id]), {'vendor_name': 'Acme Furniture Ltd'}
        )
        self.assertEqual(response.status_code, 403)

    def test_add_applicant_view_allows_procurement_unit(self):
        solicitation = self.publish()
        self.client.force_login(self.preparer)
        response = self.client.post(
            reverse('staff_prequalification_add', args=[solicitation.id]), {'vendor_name': 'Acme Furniture Ltd'}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(solicitation.prequalification_applicants.count(), 1)


class AwardTests(TestCase):
    """Bid recording & Award decision (blueprint Phase 3 — Approvals, first
    slice). Bids are a staff-recorded administrative log, not a submission
    channel — see Bid's docstring in models.py."""

    def setUp(self):
        self.law_profile = make_law_profile()
        self.fy = make_financial_year(self.law_profile)
        self.preparer = User.objects.create_user(username='pu_award', password='x', role=User.Role.PROCUREMENT_UNIT)
        self.approver = User.objects.create_user(username='ao_award', password='x', role=User.Role.ACCOUNTING_OFFICER)
        self.requester = User.objects.create_user(username='ru_award', password='x', role=User.Role.REQUESTING_UNIT)
        self.finance = User.objects.create_user(username='fin_award', password='x', role=User.Role.FINANCE)
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

        req = Requisition.objects.create(
            plan_line=self.line, title='Chairs requisition', department='Bursary',
            requested_value=500_000, budget_source=ProcurementRecord.BudgetSource.IGR,
            requested_by=self.requester,
        )
        submit_requisition(requisition=req, actor=self.requester)
        confirm_requisition_funds(requisition=req, actor=self.finance)
        review_requisition_packaging(requisition=req, actor=self.preparer, note='Checked, no splitting.')
        determine_requisition_method(requisition=req, actor=self.preparer)
        self.record = create_record_from_requisition(requisition=req, actor=self.preparer, record_fields={
            'title': 'Chairs for Bursary', 'location': 'Main Campus',
            'planned_start_date': datetime.date.today(),
            'planned_end_date': datetime.date.today() + datetime.timedelta(days=30),
        })

    def publish_and_close(self):
        solicitation = prepare_solicitation(record=self.record, actor=self.preparer, fields=SOLICITATION_FIELDS)
        approve_solicitation(solicitation=solicitation, actor=self.approver)
        closing = datetime.date.today() + datetime.timedelta(days=30)
        publish_advertisement(
            solicitation=solicitation, actor=self.preparer, channels=['institution_website'],
            publication_proof='Posted.', closing_date=closing,
        )
        solicitation.advertisement.closing_date = datetime.date.today() - datetime.timedelta(days=1)
        solicitation.advertisement.save(update_fields=['closing_date'])
        return solicitation

    def test_record_bid_before_closing_is_rejected(self):
        solicitation = prepare_solicitation(record=self.record, actor=self.preparer, fields=SOLICITATION_FIELDS)
        approve_solicitation(solicitation=solicitation, actor=self.approver)
        publish_advertisement(
            solicitation=solicitation, actor=self.preparer, channels=['institution_website'],
            publication_proof='Posted.', closing_date=datetime.date.today() + datetime.timedelta(days=30),
        )
        with self.assertRaises(ValidationError):
            record_bid(solicitation=solicitation, actor=self.preparer, vendor_name='Acme Furniture Ltd', bid_amount=480_000)

    def test_record_bid_requires_vendor_name_and_positive_amount(self):
        solicitation = self.publish_and_close()
        with self.assertRaises(ValidationError):
            record_bid(solicitation=solicitation, actor=self.preparer, vendor_name='', bid_amount=480_000)
        with self.assertRaises(ValidationError):
            record_bid(solicitation=solicitation, actor=self.preparer, vendor_name='Acme Furniture Ltd', bid_amount=0)

    def test_award_requires_bid_from_same_solicitation(self):
        solicitation = self.publish_and_close()
        other_record = make_record(self.law_profile, self.preparer)
        other_solicitation = Solicitation.objects.create(
            record=other_record, prepared_by=self.preparer, **SOLICITATION_FIELDS
        )
        foreign_bid = Bid.objects.create(
            solicitation=other_solicitation, vendor_name='Other Vendor', bid_amount=100_000, recorded_by=self.preparer,
        )
        with self.assertRaises(ValidationError):
            award_solicitation(
                solicitation=solicitation, actor=self.approver, winning_bid=foreign_bid,
                decision_note='Lowest evaluated bid.',
            )

    def test_award_rejects_non_responsive_bid(self):
        solicitation = self.publish_and_close()
        bid = record_bid(
            solicitation=solicitation, actor=self.preparer, vendor_name='Acme Furniture Ltd',
            bid_amount=480_000, is_responsive=False, note='Missing bid security.',
        )
        with self.assertRaises(ValidationError):
            award_solicitation(
                solicitation=solicitation, actor=self.approver, winning_bid=bid, decision_note='Lowest bid.',
            )

    def test_award_requires_decision_note(self):
        solicitation = self.publish_and_close()
        bid = record_bid(solicitation=solicitation, actor=self.preparer, vendor_name='Acme Furniture Ltd', bid_amount=480_000)
        with self.assertRaises(ValidationError):
            award_solicitation(solicitation=solicitation, actor=self.approver, winning_bid=bid, decision_note='')

    def test_award_requires_bpp_no_objection_when_flagged(self):
        # The requisition's determined method already snapshotted
        # bpp_prior_review_required=False at determination time (setUp) —
        # flip it directly to simulate a method/threshold that required BPP
        # prior review, without re-testing the determination engine itself
        # (already covered by ThresholdRuleTests).
        self.record.requisition.bpp_prior_review_required = True
        self.record.requisition.save(update_fields=['bpp_prior_review_required'])

        solicitation = self.publish_and_close()
        bid = record_bid(solicitation=solicitation, actor=self.preparer, vendor_name='Acme Furniture Ltd', bid_amount=480_000)
        self.assertTrue(solicitation.bpp_prior_review_required)
        with self.assertRaises(ValidationError):
            award_solicitation(
                solicitation=solicitation, actor=self.approver, winning_bid=bid, decision_note='Lowest bid.',
            )
        # Providing both BPP fields succeeds.
        award_solicitation(
            solicitation=solicitation, actor=self.approver, winning_bid=bid, decision_note='Lowest bid.',
            bpp_no_objection_reference='BPP/NOC/2026/001', bpp_no_objection_date=datetime.date.today(),
        )

    def test_award_is_idempotent_guard(self):
        solicitation = self.publish_and_close()
        bid = record_bid(solicitation=solicitation, actor=self.preparer, vendor_name='Acme Furniture Ltd', bid_amount=480_000)
        award_solicitation(solicitation=solicitation, actor=self.approver, winning_bid=bid, decision_note='Lowest bid.')
        with self.assertRaises(ValidationError):
            award_solicitation(solicitation=solicitation, actor=self.approver, winning_bid=bid, decision_note='Again.')

    def test_full_happy_path_award_sets_record_fields_and_status(self):
        solicitation = self.publish_and_close()
        losing_bid = record_bid(
            solicitation=solicitation, actor=self.preparer, vendor_name='Beta Supplies', bid_amount=495_000,
        )
        winning_bid = record_bid(
            solicitation=solicitation, actor=self.preparer, vendor_name='Acme Furniture Ltd',
            vendor_registration_no='RC-123456', bid_amount=480_000,
        )
        award_solicitation(
            solicitation=solicitation, actor=self.approver, winning_bid=winning_bid,
            decision_note='Lowest evaluated responsive bid.',
        )

        self.record.refresh_from_db()
        self.assertEqual(self.record.status, ProcurementRecord.Status.AWARDED)
        self.assertEqual(self.record.vendor_name, 'Acme Furniture Ltd')
        self.assertEqual(self.record.vendor_registration_no, 'RC-123456')
        self.assertEqual(self.record.awarded_cost, 480_000)

        # Two StatusUpdate rows total: Planning->Advertised (from publish)
        # and Advertised->Awarded (from this award) — StatusUpdate is
        # ordered chronologically (Meta.ordering = ['updated_at']).
        status_updates = StatusUpdate.objects.filter(record=self.record)
        self.assertEqual(status_updates.count(), 2)
        self.assertEqual(status_updates.last().old_status, 'Advertised')
        self.assertEqual(status_updates.last().new_status, 'Awarded')

        self.assertTrue(
            AuditEvent.objects.filter(
                content_type__model='bid', action=AuditEvent.Action.BID_RECORDED
            ).count() == 2
        )
        self.assertTrue(
            AuditEvent.objects.filter(
                content_type__model='award', action=AuditEvent.Action.AWARD_DECIDED
            ).exists()
        )

        response = self.client.get(reverse('public_record_detail', args=[self.record.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Acme Furniture Ltd')
        self.assertContains(response, 'Beta Supplies')
        self.assertContains(response, 'Lowest evaluated responsive bid.')

    def test_awarded_no_longer_manually_selectable_from_advertised(self):
        solicitation = self.publish_and_close()
        self.client.force_login(self.preparer)
        response = self.client.post(reverse('staff_status_transition', args=[self.record.id]), {
            'new_status': 'Awarded', 'note': 'Attempted manual bypass',
        })
        self.assertEqual(response.status_code, 200)
        self.record.refresh_from_db()
        self.assertEqual(self.record.status, 'Advertised')

    def test_bid_add_view_rejects_wrong_role(self):
        solicitation = self.publish_and_close()
        self.client.force_login(self.requester)
        response = self.client.post(
            reverse('staff_bid_add', args=[solicitation.id]), {'vendor_name': 'Acme Furniture Ltd', 'bid_amount': '480000'}
        )
        self.assertEqual(response.status_code, 403)

    def test_bid_add_view_allows_procurement_unit(self):
        solicitation = self.publish_and_close()
        self.client.force_login(self.preparer)
        response = self.client.post(
            reverse('staff_bid_add', args=[solicitation.id]), {'vendor_name': 'Acme Furniture Ltd', 'bid_amount': '480000'}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(solicitation.bids.count(), 1)

    def test_award_decide_view_rejects_wrong_role(self):
        solicitation = self.publish_and_close()
        bid = record_bid(solicitation=solicitation, actor=self.preparer, vendor_name='Acme Furniture Ltd', bid_amount=480_000)
        self.client.force_login(self.preparer)  # procurement_unit, not accounting_officer
        response = self.client.post(
            reverse('staff_award_decide', args=[solicitation.id]),
            {'winning_bid': str(bid.id), 'decision_note': 'Lowest bid.'},
        )
        self.assertEqual(response.status_code, 403)

    def test_award_decide_view_allows_accounting_officer(self):
        solicitation = self.publish_and_close()
        bid = record_bid(solicitation=solicitation, actor=self.preparer, vendor_name='Acme Furniture Ltd', bid_amount=480_000)
        self.client.force_login(self.approver)
        response = self.client.post(
            reverse('staff_award_decide', args=[solicitation.id]),
            {'winning_bid': str(bid.id), 'decision_note': 'Lowest bid.'},
        )
        self.assertEqual(response.status_code, 302)
        self.record.refresh_from_db()
        self.assertEqual(self.record.status, 'Awarded')


class ComplaintTests(TestCase):
    """Public complaint intake and resolution (blueprint Phase 3 —
    Approvals). Anchored on ProcurementRecord directly, so setup is
    lighter than Bid/Clarification/Prequalification — no solicitation
    chain needed, a complaint can be filed at any stage."""

    def setUp(self):
        self.law_profile = make_law_profile()
        self.creator = User.objects.create_user(username='pu_complaint', password='x', role=User.Role.PROCUREMENT_UNIT)
        self.approver = User.objects.create_user(username='ao_complaint', password='x', role=User.Role.ACCOUNTING_OFFICER)
        self.other_staff = User.objects.create_user(username='ru_complaint', password='x', role=User.Role.REQUESTING_UNIT)
        self.record = make_record(self.law_profile, self.creator)

    def test_submit_requires_name_contact_and_description(self):
        with self.assertRaises(ValidationError):
            submit_complaint(record=self.record, complainant_name='', complainant_contact='a@b.com', description='x')
        with self.assertRaises(ValidationError):
            submit_complaint(record=self.record, complainant_name='Jane Doe', complainant_contact='', description='x')
        with self.assertRaises(ValidationError):
            submit_complaint(record=self.record, complainant_name='Jane Doe', complainant_contact='a@b.com', description='')

    def test_submit_works_regardless_of_record_status(self):
        # Unlike Clarification/Bid, no gating by advertisement/closing date —
        # a complaint can be filed at any stage.
        complaint = submit_complaint(
            record=self.record, complainant_name='Jane Doe', complainant_contact='jane@example.com',
            description='The process seemed rushed.',
        )
        self.assertEqual(complaint.status, Complaint.Status.PENDING)

    def test_resolve_requires_valid_outcome_and_note(self):
        complaint = submit_complaint(
            record=self.record, complainant_name='Jane Doe', complainant_contact='jane@example.com',
            description='Concerned about vendor selection.',
        )
        with self.assertRaises(ValidationError):
            resolve_complaint(complaint=complaint, actor=self.approver, status='pending', resolution_note='x')
        with self.assertRaises(ValidationError):
            resolve_complaint(complaint=complaint, actor=self.approver, status=Complaint.Status.DISMISSED, resolution_note='')

    def test_resolve_twice_raises(self):
        complaint = submit_complaint(
            record=self.record, complainant_name='Jane Doe', complainant_contact='jane@example.com',
            description='Concerned about vendor selection.',
        )
        resolve_complaint(
            complaint=complaint, actor=self.approver, status=Complaint.Status.DISMISSED,
            resolution_note='Reviewed — no irregularity found.',
        )
        with self.assertRaises(ValidationError):
            resolve_complaint(
                complaint=complaint, actor=self.approver, status=Complaint.Status.UPHELD, resolution_note='Again.',
            )

    def test_pending_complaint_description_not_publicly_visible(self):
        submit_complaint(
            record=self.record, complainant_name='Jane Doe', complainant_contact='jane@example.com',
            description='Concerned about vendor selection secretly favoring a bidder.',
        )
        response = self.client.get(reverse('public_record_detail', args=[self.record.id]))
        self.assertNotContains(response, 'Concerned about vendor selection secretly favoring a bidder.')
        self.assertContains(response, '1 complaint(s) under review.')

    def test_full_happy_path_resolution_visible_but_description_stays_private(self):
        complaint = submit_complaint(
            record=self.record, complainant_name='Jane Doe', complainant_contact='jane@example.com',
            description='Concerned about vendor selection secretly favoring a bidder.',
        )
        resolve_complaint(
            complaint=complaint, actor=self.approver, status=Complaint.Status.DISMISSED,
            resolution_note='Reviewed the evaluation records — no irregularity found.',
        )
        self.assertTrue(
            AuditEvent.objects.filter(
                content_type__model='complaint', action=AuditEvent.Action.COMPLAINT_RESOLVED,
            ).exists()
        )
        response = self.client.get(reverse('public_record_detail', args=[self.record.id]))
        self.assertContains(response, 'Reviewed the evaluation records — no irregularity found.')
        # The raw complaint text and the complainant's contact info stay
        # private forever, even once resolved — see Complaint's docstring.
        self.assertNotContains(response, 'Concerned about vendor selection secretly favoring a bidder.')
        self.assertNotContains(response, 'jane@example.com')

    def test_resolve_view_rejects_wrong_role(self):
        complaint = submit_complaint(
            record=self.record, complainant_name='Jane Doe', complainant_contact='jane@example.com', description='x',
        )
        self.client.force_login(self.other_staff)
        response = self.client.post(
            reverse('staff_complaint_resolve', args=[complaint.id]),
            {'status': 'dismissed', 'resolution_note': 'No issue found.'},
        )
        self.assertEqual(response.status_code, 403)

    def test_resolve_view_allows_accounting_officer(self):
        complaint = submit_complaint(
            record=self.record, complainant_name='Jane Doe', complainant_contact='jane@example.com', description='x',
        )
        self.client.force_login(self.approver)
        response = self.client.post(
            reverse('staff_complaint_resolve', args=[complaint.id]),
            {'status': 'dismissed', 'resolution_note': 'No issue found.'},
        )
        self.assertEqual(response.status_code, 302)
        complaint.refresh_from_db()
        self.assertEqual(complaint.status, Complaint.Status.DISMISSED)

    def test_file_complaint_view_public_no_login(self):
        response = self.client.post(reverse('file_complaint', args=[self.record.id]), {
            'complainant_name': 'Jane Doe', 'complainant_contact': 'jane@example.com',
            'description': 'The advertised deadline was too short.',
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.record.complaints.count(), 1)


class ContractMilestoneTests(TestCase):
    """Contract signing & milestone tracking (blueprint Phase 4, first
    slice). Setup walks all the way to a real Award, same chain as
    AwardTests, since Contract requires one."""

    def setUp(self):
        self.law_profile = make_law_profile()
        self.fy = make_financial_year(self.law_profile)
        self.preparer = User.objects.create_user(username='pu_contract', password='x', role=User.Role.PROCUREMENT_UNIT)
        self.approver = User.objects.create_user(username='ao_contract', password='x', role=User.Role.ACCOUNTING_OFFICER)
        self.requester = User.objects.create_user(username='ru_contract', password='x', role=User.Role.REQUESTING_UNIT)
        self.finance = User.objects.create_user(username='fin_contract', password='x', role=User.Role.FINANCE)
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

        req = Requisition.objects.create(
            plan_line=self.line, title='Chairs requisition', department='Bursary',
            requested_value=500_000, budget_source=ProcurementRecord.BudgetSource.IGR,
            requested_by=self.requester,
        )
        submit_requisition(requisition=req, actor=self.requester)
        confirm_requisition_funds(requisition=req, actor=self.finance)
        review_requisition_packaging(requisition=req, actor=self.preparer, note='Checked, no splitting.')
        determine_requisition_method(requisition=req, actor=self.preparer)
        self.record = create_record_from_requisition(requisition=req, actor=self.preparer, record_fields={
            'title': 'Chairs for Bursary', 'location': 'Main Campus',
            'planned_start_date': datetime.date.today(),
            'planned_end_date': datetime.date.today() + datetime.timedelta(days=30),
        })

        solicitation = prepare_solicitation(record=self.record, actor=self.preparer, fields=SOLICITATION_FIELDS)
        approve_solicitation(solicitation=solicitation, actor=self.approver)
        publish_advertisement(
            solicitation=solicitation, actor=self.preparer, channels=['institution_website'],
            publication_proof='Posted.', closing_date=datetime.date.today() + datetime.timedelta(days=30),
        )
        solicitation.advertisement.closing_date = datetime.date.today() - datetime.timedelta(days=1)
        solicitation.advertisement.save(update_fields=['closing_date'])
        bid = record_bid(solicitation=solicitation, actor=self.preparer, vendor_name='Acme Furniture Ltd', bid_amount=480_000)
        self.award = award_solicitation(
            solicitation=solicitation, actor=self.approver, winning_bid=bid, decision_note='Lowest evaluated bid.',
        )

    def sign(self):
        return sign_contract(
            award=self.award, actor=self.preparer, contract_reference='CT-2026-001',
            vendor_signatory_name='Acme Managing Director', signed_date=datetime.date.today(),
            start_date=datetime.date.today(), end_date=datetime.date.today() + datetime.timedelta(days=90),
        )

    def test_sign_requires_awarded_status(self):
        self.sign()  # moves record to Implementation
        with self.assertRaises(ValidationError):
            sign_contract(
                award=self.award, actor=self.preparer, contract_reference='CT-2026-002',
                vendor_signatory_name='Someone', signed_date=datetime.date.today(),
                start_date=datetime.date.today(), end_date=datetime.date.today() + datetime.timedelta(days=30),
            )

    def test_sign_rejects_end_before_start(self):
        with self.assertRaises(ValidationError):
            sign_contract(
                award=self.award, actor=self.preparer, contract_reference='CT-2026-003',
                vendor_signatory_name='Someone', signed_date=datetime.date.today(),
                start_date=datetime.date.today(), end_date=datetime.date.today() - datetime.timedelta(days=1),
            )

    def test_complete_milestone_requires_note(self):
        contract = self.sign()
        milestone = add_milestone(
            contract=contract, actor=self.preparer, description='Delivery',
            due_date=datetime.date.today() + datetime.timedelta(days=10),
        )
        with self.assertRaises(ValidationError):
            complete_milestone(milestone=milestone, actor=self.preparer, completion_note='')

    def test_complete_milestone_twice_raises(self):
        contract = self.sign()
        milestone = add_milestone(
            contract=contract, actor=self.preparer, description='Delivery',
            due_date=datetime.date.today() + datetime.timedelta(days=10),
        )
        complete_milestone(milestone=milestone, actor=self.preparer, completion_note='Inspected, verified complete.')
        with self.assertRaises(ValidationError):
            complete_milestone(milestone=milestone, actor=self.preparer, completion_note='Again.')

    def test_full_happy_path_contract_and_milestones_visible_publicly(self):
        contract = self.sign()
        self.record.refresh_from_db()
        self.assertEqual(self.record.status, ProcurementRecord.Status.IMPLEMENTATION)

        milestone = add_milestone(
            contract=contract, actor=self.preparer, description='Delivery and installation',
            due_date=datetime.date.today() + datetime.timedelta(days=10),
        )
        complete_milestone(milestone=milestone, actor=self.preparer, completion_note='Inspected on-site, verified complete.')

        status_updates = StatusUpdate.objects.filter(record=self.record)
        self.assertEqual(status_updates.last().old_status, 'Awarded')
        self.assertEqual(status_updates.last().new_status, 'Implementation')

        self.assertTrue(
            AuditEvent.objects.filter(content_type__model='contract', action=AuditEvent.Action.CONTRACT_SIGNED).exists()
        )
        self.assertTrue(
            AuditEvent.objects.filter(
                content_type__model='milestone', action=AuditEvent.Action.MILESTONE_COMPLETED
            ).exists()
        )

        response = self.client.get(reverse('public_record_detail', args=[self.record.id]))
        self.assertContains(response, 'CT-2026-001')
        self.assertContains(response, 'Delivery and installation')
        self.assertContains(response, 'Inspected on-site, verified complete.')

    def test_implementation_no_longer_manually_selectable_from_awarded(self):
        self.client.force_login(self.preparer)
        response = self.client.post(reverse('staff_status_transition', args=[self.record.id]), {
            'new_status': 'Implementation', 'note': 'Attempted manual bypass',
        })
        self.assertEqual(response.status_code, 200)
        self.record.refresh_from_db()
        self.assertEqual(self.record.status, 'Awarded')

    def test_contract_sign_view_rejects_wrong_role(self):
        self.client.force_login(self.approver)  # accounting_officer, not procurement_unit
        response = self.client.post(reverse('staff_contract_sign', args=[self.award.id]), {
            'contract_reference': 'CT-X', 'vendor_signatory_name': 'X',
            'signed_date': datetime.date.today(), 'start_date': datetime.date.today(),
            'end_date': datetime.date.today() + datetime.timedelta(days=30),
        })
        self.assertEqual(response.status_code, 403)

    def test_contract_sign_view_allows_procurement_unit(self):
        self.client.force_login(self.preparer)
        response = self.client.post(reverse('staff_contract_sign', args=[self.award.id]), {
            'contract_reference': 'CT-X', 'vendor_signatory_name': 'X',
            'signed_date': datetime.date.today(), 'start_date': datetime.date.today(),
            'end_date': datetime.date.today() + datetime.timedelta(days=30),
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Contract.objects.filter(award=self.award).exists())

    def test_milestone_add_and_complete_views_allow_procurement_unit(self):
        contract = self.sign()
        self.client.force_login(self.preparer)
        response = self.client.post(reverse('staff_milestone_add', args=[contract.id]), {
            'description': 'Site handover', 'due_date': datetime.date.today() + datetime.timedelta(days=5),
        })
        self.assertEqual(response.status_code, 302)
        milestone = contract.milestones.first()
        self.assertIsNotNone(milestone)
        response = self.client.post(reverse('staff_milestone_complete', args=[milestone.id]), {
            'completion_note': 'Confirmed handover complete.',
        })
        self.assertEqual(response.status_code, 302)
        milestone.refresh_from_db()
        self.assertEqual(milestone.status, Milestone.Status.COMPLETED)


class SolicitationViewRoleGateTests(TestCase):
    def setUp(self):
        self.law_profile = make_law_profile()
        self.actor = User.objects.create_user(username='pu_solview', password='x', role=User.Role.PROCUREMENT_UNIT)
        self.wrong_role = User.objects.create_user(username='ru_solview', password='x', role=User.Role.REQUESTING_UNIT)
        self.record = make_record(self.law_profile, self.actor)
        self.client = Client()

    def test_solicitation_create_rejects_wrong_role(self):
        self.client.force_login(self.wrong_role)
        response = self.client.get(reverse('staff_solicitation_create', args=[self.record.id]))
        self.assertEqual(response.status_code, 403)

    def test_solicitation_create_allows_procurement_unit(self):
        self.client.force_login(self.actor)
        response = self.client.get(reverse('staff_solicitation_create', args=[self.record.id]))
        self.assertEqual(response.status_code, 200)

    def test_solicitation_approve_rejects_wrong_role(self):
        solicitation = Solicitation.objects.create(prepared_by=self.actor, record=self.record, **SOLICITATION_FIELDS)
        self.client.force_login(self.wrong_role)
        response = self.client.get(reverse('staff_solicitation_approve', args=[solicitation.id]))
        self.assertEqual(response.status_code, 403)

    def test_superuser_bypasses_role_check(self):
        admin = User.objects.create_superuser(username='admin_solview', password='x', role=User.Role.ADMIN)
        self.client.force_login(admin)
        response = self.client.get(reverse('staff_solicitation_create', args=[self.record.id]))
        self.assertEqual(response.status_code, 200)


class SolicitationAdminLockdownTests(TestCase):
    """Every field on Solicitation/Advertisement is either service-written-
    once or must stay immutable post-approval/publish — no legitimate
    post-creation admin edit path exists for either (see admin.py)."""

    def test_solicitation_admin_has_no_add_permission(self):
        from .admin import SolicitationAdmin
        from django.contrib.admin.sites import site
        admin_instance = SolicitationAdmin(Solicitation, site)
        self.assertFalse(admin_instance.has_add_permission(request=None))
        self.assertFalse(admin_instance.has_delete_permission(request=None))
        all_fields = {f.name for f in Solicitation._meta.fields}
        self.assertEqual(set(admin_instance.readonly_fields), all_fields)

    def test_advertisement_admin_has_no_add_permission(self):
        from .admin import AdvertisementAdmin
        from django.contrib.admin.sites import site
        admin_instance = AdvertisementAdmin(Advertisement, site)
        self.assertFalse(admin_instance.has_add_permission(request=None))
        self.assertFalse(admin_instance.has_delete_permission(request=None))
        all_fields = {f.name for f in Advertisement._meta.fields}
        self.assertEqual(set(admin_instance.readonly_fields), all_fields)

    def test_clarification_admin_has_no_add_permission(self):
        from .admin import ClarificationAdmin
        from django.contrib.admin.sites import site
        admin_instance = ClarificationAdmin(Clarification, site)
        self.assertFalse(admin_instance.has_add_permission(request=None))
        self.assertFalse(admin_instance.has_delete_permission(request=None))
        all_fields = {f.name for f in Clarification._meta.fields}
        self.assertEqual(set(admin_instance.readonly_fields), all_fields)

    def test_prequalification_admin_has_no_add_permission(self):
        from .admin import PrequalificationApplicantAdmin
        from django.contrib.admin.sites import site
        admin_instance = PrequalificationApplicantAdmin(PrequalificationApplicant, site)
        self.assertFalse(admin_instance.has_add_permission(request=None))
        self.assertFalse(admin_instance.has_delete_permission(request=None))
        all_fields = {f.name for f in PrequalificationApplicant._meta.fields}
        self.assertEqual(set(admin_instance.readonly_fields), all_fields)

    def test_bid_admin_has_no_add_permission(self):
        from .admin import BidAdmin
        from django.contrib.admin.sites import site
        admin_instance = BidAdmin(Bid, site)
        self.assertFalse(admin_instance.has_add_permission(request=None))
        self.assertFalse(admin_instance.has_delete_permission(request=None))
        all_fields = {f.name for f in Bid._meta.fields}
        self.assertEqual(set(admin_instance.readonly_fields), all_fields)

    def test_award_admin_has_no_add_permission(self):
        from .admin import AwardAdmin
        from django.contrib.admin.sites import site
        admin_instance = AwardAdmin(Award, site)
        self.assertFalse(admin_instance.has_add_permission(request=None))
        self.assertFalse(admin_instance.has_delete_permission(request=None))
        all_fields = {f.name for f in Award._meta.fields}
        self.assertEqual(set(admin_instance.readonly_fields), all_fields)

    def test_complaint_admin_has_no_add_permission(self):
        from .admin import ComplaintAdmin
        from django.contrib.admin.sites import site
        admin_instance = ComplaintAdmin(Complaint, site)
        self.assertFalse(admin_instance.has_add_permission(request=None))
        self.assertFalse(admin_instance.has_delete_permission(request=None))
        all_fields = {f.name for f in Complaint._meta.fields}
        self.assertEqual(set(admin_instance.readonly_fields), all_fields)

    def test_contract_admin_has_no_add_permission(self):
        from .admin import ContractAdmin
        from django.contrib.admin.sites import site
        admin_instance = ContractAdmin(Contract, site)
        self.assertFalse(admin_instance.has_add_permission(request=None))
        self.assertFalse(admin_instance.has_delete_permission(request=None))
        all_fields = {f.name for f in Contract._meta.fields}
        self.assertEqual(set(admin_instance.readonly_fields), all_fields)

    def test_milestone_admin_has_no_add_permission(self):
        from .admin import MilestoneAdmin
        from django.contrib.admin.sites import site
        admin_instance = MilestoneAdmin(Milestone, site)
        self.assertFalse(admin_instance.has_add_permission(request=None))
        self.assertFalse(admin_instance.has_delete_permission(request=None))
        all_fields = {f.name for f in Milestone._meta.fields}
        self.assertEqual(set(admin_instance.readonly_fields), all_fields)
