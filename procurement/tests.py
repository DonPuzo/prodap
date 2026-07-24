import datetime
import json

import pyotp
from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from .forms import ProcurementRecordForm, RequisitionForm
from .models import (
    Abandonment,
    Advertisement,
    Award,
    AuditEvent,
    Bid,
    Clarification,
    Complaint,
    Contract,
    ContractCompletion,
    FinancialYear,
    Invoice,
    MFABackupCode,
    Milestone,
    Payment,
    PerformanceGuarantee,
    PlanLine,
    PrequalificationApplicant,
    ProcurementPlan,
    ProcurementRecord,
    RecordFlag,
    Requisition,
    Solicitation,
    StatusUpdate,
    TendersBoardReview,
    ThresholdRule,
    TOTPDevice,
    User,
)
from .models import LawProfile
from .services import (
    SeparationOfDutiesError,
    abandon_record,
    add_milestone,
    answer_clarification,
    approve_plan,
    approve_plan_line,
    approve_solicitation,
    award_solicitation,
    complete_contract,
    complete_milestone,
    confirm_mfa_enrollment,
    confirm_requisition_funds,
    create_record_from_requisition,
    determine_default_method,
    determine_requisition_method,
    disable_mfa,
    get_approving_authority,
    get_current_solicitation,
    get_risk_alerts,
    prepare_solicitation,
    publish_advertisement,
    record_bid,
    record_payment,
    record_performance_guarantee,
    record_prequalification_applicant,
    record_tenders_board_review,
    reject_plan,
    reject_plan_line,
    reject_solicitation,
    resolve_complaint,
    review_invoice,
    review_prequalification_applicant,
    review_requisition_packaging,
    sign_contract,
    start_mfa_enrollment,
    submit_clarification_question,
    submit_complaint,
    submit_invoice,
    submit_plan,
    submit_requisition,
    transition_status,
    verify_mfa_code,
)
from .services import MFA_MAX_FAILED_ATTEMPTS


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


class AbandonmentTests(TestCase):
    """The only sanctioned way a ProcurementRecord reaches Abandoned status
    (see services.abandon_record) — closes the last unconditional
    manual-dropdown gap, mirroring StatusTransitionTests' own theme."""

    def setUp(self):
        self.law_profile = make_law_profile()
        self.creator = User.objects.create_user(username='pu_abandon', password='x', role=User.Role.PROCUREMENT_UNIT)
        self.ao = User.objects.create_user(username='ao_abandon', password='x', role=User.Role.ACCOUNTING_OFFICER)
        self.record = make_record(self.law_profile, self.creator)

    def test_requires_valid_reason(self):
        with self.assertRaises(ValidationError):
            abandon_record(record=self.record, actor=self.ao, reason='not_a_real_reason', justification='x')

    def test_requires_justification(self):
        with self.assertRaises(ValidationError):
            abandon_record(
                record=self.record, actor=self.ao, reason=Abandonment.Reason.NEED_ELIMINATED, justification='',
            )

    def test_cannot_abandon_twice(self):
        abandon_record(
            record=self.record, actor=self.ao, reason=Abandonment.Reason.NEED_ELIMINATED,
            justification='No longer needed.',
        )
        with self.assertRaises(ValidationError):
            abandon_record(record=self.record, actor=self.ao, reason=Abandonment.Reason.OTHER, justification='Again.')

    def test_cannot_abandon_completed_record(self):
        transition_status(record=self.record, new_status=ProcurementRecord.Status.COMPLETED, updated_by=self.ao)
        with self.assertRaises(ValidationError):
            abandon_record(record=self.record, actor=self.ao, reason=Abandonment.Reason.OTHER, justification='Too late.')

    def test_blocked_by_pending_complaint(self):
        submit_complaint(
            record=self.record, complainant_name='Jane Doe', complainant_contact='jane@example.com',
            description='Concern.',
        )
        with self.assertRaises(ValidationError):
            abandon_record(record=self.record, actor=self.ao, reason=Abandonment.Reason.OTHER, justification='x')

    def test_full_happy_path_sets_status_and_public_disclosure(self):
        abandonment = abandon_record(
            record=self.record, actor=self.ao, reason=Abandonment.Reason.BUDGET_EXCEEDED,
            justification='All bids substantially exceeded the approved budget envelope.',
        )
        self.record.refresh_from_db()
        self.assertEqual(self.record.status, ProcurementRecord.Status.ABANDONED)
        self.assertEqual(abandonment.previous_status, ProcurementRecord.Status.PLANNING)
        self.assertTrue(
            AuditEvent.objects.filter(
                content_type__model='abandonment', action=AuditEvent.Action.RECORD_ABANDONED,
            ).exists()
        )
        updates = list(self.record.status_updates.all())
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].new_status, ProcurementRecord.Status.ABANDONED)
        response = self.client.get(reverse('public_record_detail', args=[self.record.id]))
        self.assertContains(response, 'All bids substantially exceeded the approved budget envelope.')

    def test_abandon_view_rejects_wrong_role(self):
        self.client.force_login(self.creator)
        response = self.client.post(reverse('staff_record_abandon', args=[self.record.id]), {
            'reason': Abandonment.Reason.OTHER, 'justification': 'x',
        })
        self.assertEqual(response.status_code, 403)

    def test_abandon_view_allows_accounting_officer(self):
        self.client.force_login(self.ao)
        response = self.client.post(reverse('staff_record_abandon', args=[self.record.id]), {
            'reason': Abandonment.Reason.OTHER, 'justification': 'Duplicate requisition, cancelling.',
        })
        self.assertEqual(response.status_code, 302)
        self.record.refresh_from_db()
        self.assertEqual(self.record.status, ProcurementRecord.Status.ABANDONED)


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

    def test_export_ocds_accessible_without_login(self):
        response = self.client.get(reverse('export_ocds'))
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
        untouched by that change (Phase 2 non-cryptographic slice). Uses
        Tendering here since Abandoned is also evidence-gated now (see
        services.abandon_record) — not a free manual pick either."""
        self.client.force_login(self.actor)
        response = self.client.post(reverse('staff_status_transition', args=[self.record.id]), {
            'new_status': 'Tendering', 'note': 'Moving forward',
        })
        self.assertEqual(response.status_code, 302)
        self.record.refresh_from_db()
        self.assertEqual(self.record.status, 'Tendering')

    def test_planning_to_advertised_no_longer_manually_selectable(self):
        self.client.force_login(self.actor)
        response = self.client.post(reverse('staff_status_transition', args=[self.record.id]), {
            'new_status': 'Advertised', 'note': 'Attempted manual bypass',
        })
        self.assertEqual(response.status_code, 200)  # form re-rendered with error, not redirected
        self.record.refresh_from_db()
        self.assertEqual(self.record.status, 'Planning')  # unchanged

    def test_abandoned_no_longer_manually_selectable(self):
        self.client.force_login(self.actor)
        response = self.client.post(reverse('staff_status_transition', args=[self.record.id]), {
            'new_status': 'Abandoned', 'note': 'Attempted manual bypass',
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


class TendersBoardReviewTests(TestCase):
    """The evaluation/approval-routing stage (blueprint steps 11-13) — from
    this slice onward, award_solicitation() refuses to proceed without a
    TendersBoardReview on file. This class covers record_tenders_board_review()'s
    own validation; AwardTests covers the resulting gate on award_solicitation()."""

    def setUp(self):
        self.law_profile = make_law_profile()
        self.fy = make_financial_year(self.law_profile)
        self.preparer = User.objects.create_user(username='pu_tbr', password='x', role=User.Role.PROCUREMENT_UNIT)
        self.approver = User.objects.create_user(username='ao_tbr', password='x', role=User.Role.ACCOUNTING_OFFICER)
        self.requester = User.objects.create_user(username='ru_tbr', password='x', role=User.Role.REQUESTING_UNIT)
        self.finance = User.objects.create_user(username='fin_tbr', password='x', role=User.Role.FINANCE)
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

        self.solicitation = prepare_solicitation(record=self.record, actor=self.preparer, fields=SOLICITATION_FIELDS)
        approve_solicitation(solicitation=self.solicitation, actor=self.approver)
        publish_advertisement(
            solicitation=self.solicitation, actor=self.preparer, channels=['institution_website'],
            publication_proof='Posted.', closing_date=datetime.date.today() + datetime.timedelta(days=30),
        )
        self.solicitation.advertisement.closing_date = datetime.date.today() - datetime.timedelta(days=1)
        self.solicitation.advertisement.save(update_fields=['closing_date'])
        self.bid = record_bid(
            solicitation=self.solicitation, actor=self.preparer, vendor_name='Acme Furniture Ltd', bid_amount=480_000
        )

    def test_review_requires_bid_from_same_solicitation(self):
        other_record = make_record(self.law_profile, self.preparer)
        other_solicitation = Solicitation.objects.create(
            record=other_record, prepared_by=self.preparer, **SOLICITATION_FIELDS
        )
        foreign_bid = Bid.objects.create(
            solicitation=other_solicitation, vendor_name='Other Vendor', bid_amount=100_000, recorded_by=self.preparer,
        )
        with self.assertRaises(ValidationError):
            record_tenders_board_review(
                solicitation=self.solicitation, actor=self.preparer, recommended_bid=foreign_bid,
                evaluation_summary='Lowest bid.',
            )

    def test_review_rejects_non_responsive_bid(self):
        non_responsive_bid = record_bid(
            solicitation=self.solicitation, actor=self.preparer, vendor_name='Beta Supplies',
            bid_amount=490_000, is_responsive=False, note='Missing bid security.',
        )
        with self.assertRaises(ValidationError):
            record_tenders_board_review(
                solicitation=self.solicitation, actor=self.preparer, recommended_bid=non_responsive_bid,
                evaluation_summary='Should not be recommendable.',
            )

    def test_review_requires_evaluation_summary(self):
        with self.assertRaises(ValidationError):
            record_tenders_board_review(
                solicitation=self.solicitation, actor=self.preparer, recommended_bid=self.bid, evaluation_summary='',
            )

    def test_review_requires_quorum(self):
        with self.assertRaises(ValidationError):
            record_tenders_board_review(
                solicitation=self.solicitation, actor=self.preparer, recommended_bid=self.bid,
                evaluation_summary='Lowest bid.', quorum_present=False,
            )

    def test_review_twice_raises(self):
        record_tenders_board_review(
            solicitation=self.solicitation, actor=self.preparer, recommended_bid=self.bid,
            evaluation_summary='Lowest bid.',
        )
        with self.assertRaises(ValidationError):
            record_tenders_board_review(
                solicitation=self.solicitation, actor=self.preparer, recommended_bid=self.bid,
                evaluation_summary='Again.',
            )

    def test_award_blocked_without_tenders_board_review(self):
        with self.assertRaises(ValidationError):
            award_solicitation(
                solicitation=self.solicitation, actor=self.approver, winning_bid=self.bid,
                decision_note='Lowest bid.',
            )

    def test_full_happy_path_review_then_award_and_public_disclosure(self):
        record_tenders_board_review(
            solicitation=self.solicitation, actor=self.preparer, recommended_bid=self.bid,
            evaluation_summary='Acme is the lowest evaluated responsive bid.',
        )
        self.assertTrue(
            AuditEvent.objects.filter(
                content_type__model='tendersboardreview', action=AuditEvent.Action.TENDERS_BOARD_REVIEWED,
            ).exists()
        )
        award_solicitation(
            solicitation=self.solicitation, actor=self.approver, winning_bid=self.bid, decision_note='Lowest bid.',
        )
        response = self.client.get(reverse('public_record_detail', args=[self.record.id]))
        self.assertContains(response, 'Acme is the lowest evaluated responsive bid.')


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
        self.tenders_board = User.objects.create_user(username='tb_award', password='x', role=User.Role.TENDERS_BOARD)
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
        own_bid = record_bid(solicitation=solicitation, actor=self.preparer, vendor_name='Acme Furniture Ltd', bid_amount=480_000)
        record_tenders_board_review(
            solicitation=solicitation, actor=self.tenders_board, recommended_bid=own_bid,
            evaluation_summary='Lowest evaluated bid.',
        )
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
        responsive_bid = record_bid(
            solicitation=solicitation, actor=self.preparer, vendor_name='Beta Supplies', bid_amount=495_000,
        )
        non_responsive_bid = record_bid(
            solicitation=solicitation, actor=self.preparer, vendor_name='Acme Furniture Ltd',
            bid_amount=480_000, is_responsive=False, note='Missing bid security.',
        )
        record_tenders_board_review(
            solicitation=solicitation, actor=self.tenders_board, recommended_bid=responsive_bid,
            evaluation_summary='Beta Supplies is the lowest responsive bid.',
        )
        with self.assertRaises(ValidationError):
            award_solicitation(
                solicitation=solicitation, actor=self.approver, winning_bid=non_responsive_bid, decision_note='Lowest bid.',
            )

    def test_award_requires_decision_note(self):
        solicitation = self.publish_and_close()
        bid = record_bid(solicitation=solicitation, actor=self.preparer, vendor_name='Acme Furniture Ltd', bid_amount=480_000)
        record_tenders_board_review(
            solicitation=solicitation, actor=self.tenders_board, recommended_bid=bid, evaluation_summary='Lowest bid.',
        )
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
        record_tenders_board_review(
            solicitation=solicitation, actor=self.tenders_board, recommended_bid=bid, evaluation_summary='Lowest bid.',
        )
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
        record_tenders_board_review(
            solicitation=solicitation, actor=self.tenders_board, recommended_bid=bid, evaluation_summary='Lowest bid.',
        )
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
        record_tenders_board_review(
            solicitation=solicitation, actor=self.tenders_board, recommended_bid=winning_bid,
            evaluation_summary='Acme Furniture Ltd is the lowest evaluated responsive bid.',
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
        record_tenders_board_review(
            solicitation=solicitation, actor=self.tenders_board, recommended_bid=bid, evaluation_summary='Lowest bid.',
        )
        self.client.force_login(self.preparer)  # procurement_unit, not accounting_officer
        response = self.client.post(
            reverse('staff_award_decide', args=[solicitation.id]),
            {'winning_bid': str(bid.id), 'decision_note': 'Lowest bid.'},
        )
        self.assertEqual(response.status_code, 403)

    def test_award_decide_view_allows_accounting_officer(self):
        solicitation = self.publish_and_close()
        bid = record_bid(solicitation=solicitation, actor=self.preparer, vendor_name='Acme Furniture Ltd', bid_amount=480_000)
        record_tenders_board_review(
            solicitation=solicitation, actor=self.tenders_board, recommended_bid=bid, evaluation_summary='Lowest bid.',
        )
        self.client.force_login(self.approver)
        response = self.client.post(
            reverse('staff_award_decide', args=[solicitation.id]),
            {'winning_bid': str(bid.id), 'decision_note': 'Lowest bid.'},
        )
        self.assertEqual(response.status_code, 302)
        self.record.refresh_from_db()
        self.assertEqual(self.record.status, 'Awarded')

    def test_tenders_board_review_view_rejects_wrong_role(self):
        solicitation = self.publish_and_close()
        bid = record_bid(solicitation=solicitation, actor=self.preparer, vendor_name='Acme Furniture Ltd', bid_amount=480_000)
        self.client.force_login(self.preparer)  # procurement_unit, not tenders_board
        response = self.client.post(
            reverse('staff_tenders_board_review', args=[solicitation.id]),
            {'recommended_bid': str(bid.id), 'evaluation_summary': 'Lowest bid.', 'quorum_present': 'on'},
        )
        self.assertEqual(response.status_code, 403)

    def test_tenders_board_review_view_allows_tenders_board(self):
        solicitation = self.publish_and_close()
        bid = record_bid(solicitation=solicitation, actor=self.preparer, vendor_name='Acme Furniture Ltd', bid_amount=480_000)
        self.client.force_login(self.tenders_board)
        response = self.client.post(
            reverse('staff_tenders_board_review', args=[solicitation.id]),
            {'recommended_bid': str(bid.id), 'evaluation_summary': 'Lowest bid.', 'quorum_present': 'on'},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(TendersBoardReview.objects.filter(solicitation=solicitation).exists())


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

    def test_is_overdue_false_within_response_window(self):
        complaint = submit_complaint(
            record=self.record, complainant_name='Jane Doe', complainant_contact='jane@example.com',
            description='Filed today.',
        )
        self.assertFalse(complaint.is_overdue)

    def test_is_overdue_true_past_response_window(self):
        complaint = submit_complaint(
            record=self.record, complainant_name='Jane Doe', complainant_contact='jane@example.com',
            description='Filed long ago.',
        )
        days = self.law_profile.default_complaint_response_days
        stale = timezone.now() - datetime.timedelta(days=days + 1)
        Complaint.objects.filter(pk=complaint.pk).update(submitted_at=stale)
        complaint.refresh_from_db()
        self.assertTrue(complaint.is_overdue)

    def test_is_overdue_false_once_resolved_even_if_past_window(self):
        complaint = submit_complaint(
            record=self.record, complainant_name='Jane Doe', complainant_contact='jane@example.com',
            description='Filed long ago.',
        )
        days = self.law_profile.default_complaint_response_days
        stale = timezone.now() - datetime.timedelta(days=days + 1)
        Complaint.objects.filter(pk=complaint.pk).update(submitted_at=stale)
        resolve_complaint(
            complaint=complaint, actor=self.approver, status=Complaint.Status.DISMISSED,
            resolution_note='Reviewed — no irregularity found.',
        )
        complaint.refresh_from_db()
        self.assertFalse(complaint.is_overdue)

    def test_public_page_shows_overdue_count(self):
        complaint = submit_complaint(
            record=self.record, complainant_name='Jane Doe', complainant_contact='jane@example.com',
            description='Filed long ago.',
        )
        days = self.law_profile.default_complaint_response_days
        stale = timezone.now() - datetime.timedelta(days=days + 1)
        Complaint.objects.filter(pk=complaint.pk).update(submitted_at=stale)
        response = self.client.get(reverse('public_record_detail', args=[self.record.id]))
        self.assertContains(response, '1 complaint(s) past the institution')


class ComplaintHoldTests(TestCase):
    """Acceptance checklist: "Complaints and suspension instructions freeze
    the affected workflow" (E-Procurement Platform Integration Framework,
    p.11/18). An unresolved complaint must block every status transition —
    manual or evidence-gated — on its record, until resolved."""

    def setUp(self):
        self.law_profile = make_law_profile()
        self.fy = make_financial_year(self.law_profile)
        self.preparer = User.objects.create_user(username='pu_hold', password='x', role=User.Role.PROCUREMENT_UNIT)
        self.approver = User.objects.create_user(username='ao_hold', password='x', role=User.Role.ACCOUNTING_OFFICER)
        self.requester = User.objects.create_user(username='ru_hold', password='x', role=User.Role.REQUESTING_UNIT)
        self.finance = User.objects.create_user(username='fin_hold', password='x', role=User.Role.FINANCE)
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

        self.solicitation = prepare_solicitation(record=self.record, actor=self.preparer, fields=SOLICITATION_FIELDS)
        approve_solicitation(solicitation=self.solicitation, actor=self.approver)
        publish_advertisement(
            solicitation=self.solicitation, actor=self.preparer, channels=['institution_website'],
            publication_proof='Posted.', closing_date=datetime.date.today() + datetime.timedelta(days=30),
        )
        self.solicitation.advertisement.closing_date = datetime.date.today() - datetime.timedelta(days=1)
        self.solicitation.advertisement.save(update_fields=['closing_date'])
        self.bid = record_bid(
            solicitation=self.solicitation, actor=self.preparer, vendor_name='Acme Furniture Ltd', bid_amount=480_000
        )
        record_tenders_board_review(
            solicitation=self.solicitation, actor=self.preparer, recommended_bid=self.bid,
            evaluation_summary='Lowest evaluated bid.',
        )

    def file_complaint(self):
        return submit_complaint(
            record=self.record, complainant_name='Jane Doe', complainant_contact='jane@example.com',
            description='Concerned the process was rushed.',
        )

    def test_award_blocked_while_complaint_pending(self):
        self.file_complaint()
        with self.assertRaises(ValidationError):
            award_solicitation(
                solicitation=self.solicitation, actor=self.approver, winning_bid=self.bid,
                decision_note='Lowest evaluated bid.',
            )

    def test_manual_transition_blocked_while_complaint_pending(self):
        self.file_complaint()
        with self.assertRaises(ValidationError):
            transition_status(record=self.record, new_status='Tendering', updated_by=self.preparer)

    def test_sign_contract_blocked_while_complaint_pending(self):
        award = award_solicitation(
            solicitation=self.solicitation, actor=self.approver, winning_bid=self.bid,
            decision_note='Lowest evaluated bid.',
        )
        self.file_complaint()
        with self.assertRaises(ValidationError):
            sign_contract(
                award=award, actor=self.preparer, contract_reference='CT-HOLD-1',
                vendor_signatory_name='Acme MD', signed_date=datetime.date.today(),
                start_date=datetime.date.today(), end_date=datetime.date.today() + datetime.timedelta(days=90),
            )

    def test_complete_contract_blocked_while_complaint_pending(self):
        award = award_solicitation(
            solicitation=self.solicitation, actor=self.approver, winning_bid=self.bid,
            decision_note='Lowest evaluated bid.',
        )
        contract = sign_contract(
            award=award, actor=self.preparer, contract_reference='CT-HOLD-2',
            vendor_signatory_name='Acme MD', signed_date=datetime.date.today(),
            start_date=datetime.date.today(), end_date=datetime.date.today() + datetime.timedelta(days=90),
        )
        self.file_complaint()
        with self.assertRaises(ValidationError):
            complete_contract(contract=contract, actor=self.approver, completion_date=datetime.date.today(), inspection_note='Done.')

    def test_workflow_resumes_once_complaint_resolved(self):
        complaint = self.file_complaint()
        with self.assertRaises(ValidationError):
            award_solicitation(
                solicitation=self.solicitation, actor=self.approver, winning_bid=self.bid,
                decision_note='Lowest evaluated bid.',
            )
        resolve_complaint(
            complaint=complaint, actor=self.approver, status=Complaint.Status.DISMISSED,
            resolution_note='Reviewed — timeline complied with the minimum bidding period.',
        )
        award = award_solicitation(
            solicitation=self.solicitation, actor=self.approver, winning_bid=self.bid,
            decision_note='Lowest evaluated bid.',
        )
        self.assertIsNotNone(award.pk)

    def test_staff_status_transition_view_shows_error_not_crash(self):
        self.file_complaint()
        self.client.force_login(self.preparer)
        response = self.client.post(reverse('staff_status_transition', args=[self.record.id]), {
            'new_status': 'Tendering', 'note': 'Attempted during complaint hold.',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'unresolved complaint')
        self.record.refresh_from_db()
        self.assertEqual(self.record.status, 'Advertised')


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
        record_tenders_board_review(
            solicitation=solicitation, actor=self.preparer, recommended_bid=bid,
            evaluation_summary='Lowest evaluated bid.',
        )
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


class ContractCompletionTests(TestCase):
    """Final acceptance sign-off — the last remaining manual gap in the
    record status lifecycle. Setup walks all the way to a signed Contract,
    same chain as ContractMilestoneTests."""

    def setUp(self):
        self.law_profile = make_law_profile()
        self.fy = make_financial_year(self.law_profile)
        self.preparer = User.objects.create_user(username='pu_complete', password='x', role=User.Role.PROCUREMENT_UNIT)
        self.approver = User.objects.create_user(username='ao_complete', password='x', role=User.Role.ACCOUNTING_OFFICER)
        self.requester = User.objects.create_user(username='ru_complete', password='x', role=User.Role.REQUESTING_UNIT)
        self.finance = User.objects.create_user(username='fin_complete', password='x', role=User.Role.FINANCE)
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
        record_tenders_board_review(
            solicitation=solicitation, actor=self.preparer, recommended_bid=bid,
            evaluation_summary='Lowest evaluated bid.',
        )
        award = award_solicitation(
            solicitation=solicitation, actor=self.approver, winning_bid=bid, decision_note='Lowest evaluated bid.',
        )
        self.contract = sign_contract(
            award=award, actor=self.preparer, contract_reference='CT-2026-100',
            vendor_signatory_name='Acme Managing Director', signed_date=datetime.date.today(),
            start_date=datetime.date.today(), end_date=datetime.date.today() + datetime.timedelta(days=90),
        )

    def test_complete_requires_implementation_status(self):
        transition_status(record=self.record, new_status='Abandoned', updated_by=self.preparer)
        with self.assertRaises(ValidationError):
            complete_contract(
                contract=self.contract, actor=self.approver,
                completion_date=datetime.date.today(), inspection_note='Done.',
            )

    def test_complete_requires_all_milestones_completed(self):
        add_milestone(
            contract=self.contract, actor=self.preparer, description='Delivery',
            due_date=datetime.date.today() + datetime.timedelta(days=10),
        )
        with self.assertRaises(ValidationError):
            complete_contract(
                contract=self.contract, actor=self.approver,
                completion_date=datetime.date.today(), inspection_note='Done.',
            )

    def test_complete_with_zero_milestones_is_allowed(self):
        completion = complete_contract(
            contract=self.contract, actor=self.approver,
            completion_date=datetime.date.today(), inspection_note='No milestones tracked; delivery confirmed.',
        )
        self.assertIsNotNone(completion.pk)

    def test_complete_requires_note(self):
        with self.assertRaises(ValidationError):
            complete_contract(
                contract=self.contract, actor=self.approver,
                completion_date=datetime.date.today(), inspection_note='',
            )

    def test_complete_twice_raises(self):
        complete_contract(
            contract=self.contract, actor=self.approver,
            completion_date=datetime.date.today(), inspection_note='Confirmed complete.',
        )
        with self.assertRaises(ValidationError):
            complete_contract(
                contract=self.contract, actor=self.approver,
                completion_date=datetime.date.today(), inspection_note='Again.',
            )

    def test_full_happy_path_completion_visible_publicly(self):
        milestone = add_milestone(
            contract=self.contract, actor=self.preparer, description='Delivery and installation',
            due_date=datetime.date.today() + datetime.timedelta(days=10),
        )
        complete_milestone(milestone=milestone, actor=self.preparer, completion_note='Inspected, verified complete.')
        complete_contract(
            contract=self.contract, actor=self.approver,
            completion_date=datetime.date.today(), inspection_note='Final walkthrough confirms full delivery.',
        )

        self.record.refresh_from_db()
        self.assertEqual(self.record.status, ProcurementRecord.Status.COMPLETED)

        status_updates = StatusUpdate.objects.filter(record=self.record)
        self.assertEqual(status_updates.last().old_status, 'Implementation')
        self.assertEqual(status_updates.last().new_status, 'Completed')

        self.assertTrue(
            AuditEvent.objects.filter(
                content_type__model='contractcompletion', action=AuditEvent.Action.CONTRACT_COMPLETED
            ).exists()
        )

        response = self.client.get(reverse('public_record_detail', args=[self.record.id]))
        self.assertContains(response, 'Final walkthrough confirms full delivery.')

    def test_completed_no_longer_manually_selectable_from_implementation(self):
        self.client.force_login(self.preparer)
        response = self.client.post(reverse('staff_status_transition', args=[self.record.id]), {
            'new_status': 'Completed', 'note': 'Attempted manual bypass',
        })
        self.assertEqual(response.status_code, 200)
        self.record.refresh_from_db()
        self.assertEqual(self.record.status, 'Implementation')

    def test_contract_complete_view_rejects_wrong_role(self):
        self.client.force_login(self.preparer)  # procurement_unit, not accounting_officer
        response = self.client.post(reverse('staff_contract_complete', args=[self.contract.id]), {
            'completion_date': datetime.date.today(), 'inspection_note': 'Done.',
        })
        self.assertEqual(response.status_code, 403)

    def test_contract_complete_view_allows_accounting_officer(self):
        self.client.force_login(self.approver)
        response = self.client.post(reverse('staff_contract_complete', args=[self.contract.id]), {
            'completion_date': datetime.date.today(), 'inspection_note': 'Confirmed complete.',
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(ContractCompletion.objects.filter(contract=self.contract).exists())


class PerformanceGuaranteeTests(TestCase):
    """Post-award performance security (blueprint step 17 — "verified
    conditional securities"). Conditionally required: complete_contract()
    only demands one when the underlying Solicitation had
    bid_security_required=True — see PerformanceGuarantee's docstring."""

    def setUp(self):
        self.law_profile = make_law_profile()
        self.fy = make_financial_year(self.law_profile)
        self.preparer = User.objects.create_user(username='pu_guar', password='x', role=User.Role.PROCUREMENT_UNIT)
        self.approver = User.objects.create_user(username='ao_guar', password='x', role=User.Role.ACCOUNTING_OFFICER)
        self.requester = User.objects.create_user(username='ru_guar', password='x', role=User.Role.REQUESTING_UNIT)
        self.finance = User.objects.create_user(username='fin_guar', password='x', role=User.Role.FINANCE)
        make_threshold_rule(self.law_profile, self.preparer, max_value=5_000_000, authority='Accounting Officer')

        self.plan = ProcurementPlan.objects.create(
            law_profile=self.law_profile, financial_year=self.fy, prepared_by=self.preparer
        )

    def _walk_to_signed_contract(self, bid_security_required, suffix):
        line = PlanLine.objects.create(
            plan=self.plan, department='Bursary', item_description=f'Chairs {suffix}', justification='need',
            estimated_cost=500_000, budget_line='B1', proposed_quarter='Q1', proposed_by=self.requester,
            is_amendment=self.plan.status == ProcurementPlan.Status.APPROVED,
        )
        if self.plan.status != ProcurementPlan.Status.APPROVED:
            approve_plan(plan=self.plan, actor=self.approver)
        else:
            approve_plan_line(plan_line=line, actor=self.approver)
        line.refresh_from_db()

        req = Requisition.objects.create(
            plan_line=line, title=f'Chairs requisition {suffix}', department='Bursary',
            requested_value=500_000, budget_source=ProcurementRecord.BudgetSource.IGR,
            requested_by=self.requester,
        )
        submit_requisition(requisition=req, actor=self.requester)
        confirm_requisition_funds(requisition=req, actor=self.finance)
        review_requisition_packaging(requisition=req, actor=self.preparer, note='Checked, no splitting.')
        determine_requisition_method(requisition=req, actor=self.preparer)
        record = create_record_from_requisition(requisition=req, actor=self.preparer, record_fields={
            'title': f'Chairs for Bursary {suffix}', 'location': 'Main Campus',
            'planned_start_date': datetime.date.today(),
            'planned_end_date': datetime.date.today() + datetime.timedelta(days=30),
        })

        fields = dict(SOLICITATION_FIELDS)
        fields['bid_security_required'] = bid_security_required
        if bid_security_required:
            fields['bid_security_type'] = 'Bank Guarantee'
            fields['bid_security_amount'] = 50_000
        solicitation = prepare_solicitation(record=record, actor=self.preparer, fields=fields)
        approve_solicitation(solicitation=solicitation, actor=self.approver)
        publish_advertisement(
            solicitation=solicitation, actor=self.preparer, channels=['institution_website'],
            publication_proof='Posted.', closing_date=datetime.date.today() + datetime.timedelta(days=30),
        )
        solicitation.advertisement.closing_date = datetime.date.today() - datetime.timedelta(days=1)
        solicitation.advertisement.save(update_fields=['closing_date'])
        bid = record_bid(solicitation=solicitation, actor=self.preparer, vendor_name='Acme Furniture Ltd', bid_amount=480_000)
        record_tenders_board_review(
            solicitation=solicitation, actor=self.preparer, recommended_bid=bid,
            evaluation_summary='Lowest evaluated bid.',
        )
        award = award_solicitation(
            solicitation=solicitation, actor=self.approver, winning_bid=bid, decision_note='Lowest evaluated bid.',
        )
        return sign_contract(
            award=award, actor=self.preparer, contract_reference=f'CT-GUAR-{suffix}',
            vendor_signatory_name='Acme Managing Director', signed_date=datetime.date.today(),
            start_date=datetime.date.today(), end_date=datetime.date.today() + datetime.timedelta(days=90),
        )

    def test_record_guarantee_requires_all_fields(self):
        contract = self._walk_to_signed_contract(bid_security_required=True, suffix='A')
        with self.assertRaises(ValidationError):
            record_performance_guarantee(
                contract=contract, actor=self.preparer, guarantee_type='', issuing_institution='First Bank',
                reference_number='REF1', amount=50_000, expiry_date=datetime.date.today() + datetime.timedelta(days=365),
            )

    def test_record_guarantee_requires_positive_amount(self):
        contract = self._walk_to_signed_contract(bid_security_required=True, suffix='B')
        with self.assertRaises(ValidationError):
            record_performance_guarantee(
                contract=contract, actor=self.preparer, guarantee_type='Bank Guarantee',
                issuing_institution='First Bank', reference_number='REF1', amount=0,
                expiry_date=datetime.date.today() + datetime.timedelta(days=365),
            )

    def test_record_guarantee_twice_raises(self):
        contract = self._walk_to_signed_contract(bid_security_required=True, suffix='C')
        record_performance_guarantee(
            contract=contract, actor=self.preparer, guarantee_type='Bank Guarantee',
            issuing_institution='First Bank', reference_number='REF1', amount=50_000,
            expiry_date=datetime.date.today() + datetime.timedelta(days=365),
        )
        with self.assertRaises(ValidationError):
            record_performance_guarantee(
                contract=contract, actor=self.preparer, guarantee_type='Insurance Bond',
                issuing_institution='Second Insurer', reference_number='REF2', amount=60_000,
                expiry_date=datetime.date.today() + datetime.timedelta(days=365),
            )

    def test_completion_blocked_without_guarantee_when_bid_security_was_required(self):
        contract = self._walk_to_signed_contract(bid_security_required=True, suffix='D')
        with self.assertRaises(ValidationError):
            complete_contract(
                contract=contract, actor=self.approver, completion_date=datetime.date.today(),
                inspection_note='Done.',
            )

    def test_completion_allowed_without_guarantee_when_bid_security_not_required(self):
        contract = self._walk_to_signed_contract(bid_security_required=False, suffix='E')
        completion = complete_contract(
            contract=contract, actor=self.approver, completion_date=datetime.date.today(),
            inspection_note='No bid security was required for this tender; delivery confirmed.',
        )
        self.assertIsNotNone(completion.pk)

    def test_full_happy_path_completion_succeeds_once_guarantee_recorded(self):
        contract = self._walk_to_signed_contract(bid_security_required=True, suffix='F')
        record_performance_guarantee(
            contract=contract, actor=self.preparer, guarantee_type='Bank Guarantee',
            issuing_institution='First Bank of Nigeria', reference_number='PG-2026-001', amount=50_000,
            expiry_date=datetime.date.today() + datetime.timedelta(days=365),
        )
        completion = complete_contract(
            contract=contract, actor=self.approver, completion_date=datetime.date.today(),
            inspection_note='Guarantee on file; delivery confirmed.',
        )
        self.assertIsNotNone(completion.pk)

        self.assertTrue(
            AuditEvent.objects.filter(
                content_type__model='performanceguarantee',
                action=AuditEvent.Action.PERFORMANCE_GUARANTEE_RECORDED,
            ).exists()
        )

        response = self.client.get(reverse('public_record_detail', args=[contract.award.solicitation.record_id]))
        self.assertContains(response, 'Bank Guarantee')
        self.assertContains(response, '50,000')
        # Issuing institution and reference number stay staff-only.
        self.assertNotContains(response, 'First Bank of Nigeria')
        self.assertNotContains(response, 'PG-2026-001')

    def test_guarantee_add_view_rejects_wrong_role(self):
        contract = self._walk_to_signed_contract(bid_security_required=True, suffix='G')
        self.client.force_login(self.approver)  # accounting_officer, not procurement_unit
        response = self.client.post(reverse('staff_performance_guarantee_add', args=[contract.id]), {
            'guarantee_type': 'Bank Guarantee', 'issuing_institution': 'First Bank',
            'reference_number': 'REF1', 'amount': '50000', 'expiry_date': datetime.date.today() + datetime.timedelta(days=365),
        })
        self.assertEqual(response.status_code, 403)

    def test_guarantee_add_view_allows_procurement_unit(self):
        contract = self._walk_to_signed_contract(bid_security_required=True, suffix='H')
        self.client.force_login(self.preparer)
        response = self.client.post(reverse('staff_performance_guarantee_add', args=[contract.id]), {
            'guarantee_type': 'Bank Guarantee', 'issuing_institution': 'First Bank',
            'reference_number': 'REF1', 'amount': '50000', 'expiry_date': datetime.date.today() + datetime.timedelta(days=365),
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(PerformanceGuarantee.objects.filter(contract=contract).exists())


class InvoicePaymentTests(TestCase):
    """Invoices & payments (blueprint Phase 4). Finance's own blueprint
    role ("confirm appropriation, reserve funds, validate invoices and
    process authorised payment") maps directly onto this — no new role
    needed. Deliberately independent of the Completion gate — see
    Invoice's docstring."""

    def setUp(self):
        self.law_profile = make_law_profile()
        self.fy = make_financial_year(self.law_profile)
        self.preparer = User.objects.create_user(username='pu_inv', password='x', role=User.Role.PROCUREMENT_UNIT)
        self.approver = User.objects.create_user(username='ao_inv', password='x', role=User.Role.ACCOUNTING_OFFICER)
        self.requester = User.objects.create_user(username='ru_inv', password='x', role=User.Role.REQUESTING_UNIT)
        self.finance = User.objects.create_user(username='fin_inv', password='x', role=User.Role.FINANCE)
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
        record_tenders_board_review(
            solicitation=solicitation, actor=self.preparer, recommended_bid=bid,
            evaluation_summary='Lowest evaluated bid.',
        )
        award = award_solicitation(
            solicitation=solicitation, actor=self.approver, winning_bid=bid, decision_note='Lowest evaluated bid.',
        )
        self.contract = sign_contract(
            award=award, actor=self.preparer, contract_reference='CT-INV-1',
            vendor_signatory_name='Acme Managing Director', signed_date=datetime.date.today(),
            start_date=datetime.date.today(), end_date=datetime.date.today() + datetime.timedelta(days=90),
        )

    def submit(self, **overrides):
        fields = dict(
            contract=self.contract, actor=self.preparer, invoice_number='INV-001',
            amount=100_000, submitted_date=datetime.date.today(),
        )
        fields.update(overrides)
        return submit_invoice(**fields)

    def test_submit_requires_invoice_number(self):
        with self.assertRaises(ValidationError):
            self.submit(invoice_number='')

    def test_submit_requires_positive_amount(self):
        with self.assertRaises(ValidationError):
            self.submit(amount=0)

    def test_submit_with_milestone_requires_it_belongs_and_is_completed(self):
        milestone = add_milestone(
            contract=self.contract, actor=self.preparer, description='Delivery',
            due_date=datetime.date.today() + datetime.timedelta(days=10),
        )
        with self.assertRaises(ValidationError):
            self.submit(milestone=milestone)  # not completed yet
        complete_milestone(milestone=milestone, actor=self.preparer, completion_note='Inspected, verified.')
        invoice = self.submit(milestone=milestone)
        self.assertEqual(invoice.milestone, milestone)

    def test_review_requires_valid_outcome_and_note(self):
        invoice = self.submit()
        with self.assertRaises(ValidationError):
            review_invoice(invoice=invoice, actor=self.finance, status='pending', review_note='x')
        with self.assertRaises(ValidationError):
            review_invoice(invoice=invoice, actor=self.finance, status=Invoice.Status.APPROVED, review_note='')

    def test_review_separation_of_duties(self):
        invoice = self.submit()
        with self.assertRaises(SeparationOfDutiesError):
            review_invoice(
                invoice=invoice, actor=self.preparer, status=Invoice.Status.APPROVED, review_note='Approving my own.'
            )

    def test_review_twice_raises(self):
        invoice = self.submit()
        review_invoice(invoice=invoice, actor=self.finance, status=Invoice.Status.APPROVED, review_note='Checked.')
        with self.assertRaises(ValidationError):
            review_invoice(invoice=invoice, actor=self.finance, status=Invoice.Status.REJECTED, review_note='Again.')

    def test_payment_requires_approved_invoice(self):
        invoice = self.submit()
        with self.assertRaises(ValidationError):
            record_payment(
                invoice=invoice, actor=self.finance, amount=100_000,
                payment_date=datetime.date.today(), payment_reference='PMT-1',
            )

    def test_payment_twice_raises(self):
        invoice = self.submit()
        review_invoice(invoice=invoice, actor=self.finance, status=Invoice.Status.APPROVED, review_note='Checked.')
        record_payment(
            invoice=invoice, actor=self.finance, amount=100_000,
            payment_date=datetime.date.today(), payment_reference='PMT-1',
        )
        with self.assertRaises(ValidationError):
            record_payment(
                invoice=invoice, actor=self.finance, amount=100_000,
                payment_date=datetime.date.today(), payment_reference='PMT-2',
            )

    def test_full_happy_path_payment_visible_publicly(self):
        invoice = self.submit()
        review_invoice(invoice=invoice, actor=self.finance, status=Invoice.Status.APPROVED, review_note='Checked, matches contract.')
        record_payment(
            invoice=invoice, actor=self.finance, amount=100_000,
            payment_date=datetime.date.today(), payment_reference='PMT-VERIFY-1',
        )
        self.assertTrue(
            AuditEvent.objects.filter(content_type__model='invoice', action=AuditEvent.Action.INVOICE_REVIEWED).exists()
        )
        self.assertTrue(
            AuditEvent.objects.filter(content_type__model='payment', action=AuditEvent.Action.PAYMENT_RECORDED).exists()
        )
        response = self.client.get(reverse('public_record_detail', args=[self.record.id]))
        self.assertContains(response, '100,000')
        # The bank payment reference stays staff-only.
        self.assertNotContains(response, 'PMT-VERIFY-1')

    def test_invoice_review_view_rejects_wrong_role(self):
        invoice = self.submit()
        self.client.force_login(self.preparer)  # procurement_unit, not finance
        response = self.client.post(reverse('staff_invoice_review', args=[invoice.id]), {
            'status': 'approved', 'review_note': 'Checked.',
        })
        self.assertEqual(response.status_code, 403)

    def test_invoice_review_view_allows_finance(self):
        invoice = self.submit()
        self.client.force_login(self.finance)
        response = self.client.post(reverse('staff_invoice_review', args=[invoice.id]), {
            'status': 'approved', 'review_note': 'Checked.',
        })
        self.assertEqual(response.status_code, 302)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, Invoice.Status.APPROVED)

    def test_payment_record_view_allows_finance(self):
        invoice = self.submit()
        review_invoice(invoice=invoice, actor=self.finance, status=Invoice.Status.APPROVED, review_note='Checked.')
        self.client.force_login(self.finance)
        response = self.client.post(reverse('staff_payment_record', args=[invoice.id]), {
            'amount': '100000', 'payment_date': datetime.date.today(), 'payment_reference': 'PMT-VIEW-1',
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Payment.objects.filter(invoice=invoice).exists())

    def test_invoice_submit_view_allows_procurement_unit(self):
        self.client.force_login(self.preparer)
        response = self.client.post(reverse('staff_invoice_submit', args=[self.contract.id]), {
            'invoice_number': 'INV-VIEW-1', 'amount': '100000', 'submitted_date': datetime.date.today(),
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Invoice.objects.filter(contract=self.contract, invoice_number='INV-VIEW-1').exists())


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

    def test_contract_completion_admin_has_no_add_permission(self):
        from .admin import ContractCompletionAdmin
        from django.contrib.admin.sites import site
        admin_instance = ContractCompletionAdmin(ContractCompletion, site)
        self.assertFalse(admin_instance.has_add_permission(request=None))
        self.assertFalse(admin_instance.has_delete_permission(request=None))
        all_fields = {f.name for f in ContractCompletion._meta.fields}
        self.assertEqual(set(admin_instance.readonly_fields), all_fields)

    def test_performance_guarantee_admin_has_no_add_permission(self):
        from .admin import PerformanceGuaranteeAdmin
        from django.contrib.admin.sites import site
        admin_instance = PerformanceGuaranteeAdmin(PerformanceGuarantee, site)
        self.assertFalse(admin_instance.has_add_permission(request=None))
        self.assertFalse(admin_instance.has_delete_permission(request=None))
        all_fields = {f.name for f in PerformanceGuarantee._meta.fields}
        self.assertEqual(set(admin_instance.readonly_fields), all_fields)

    def test_invoice_admin_has_no_add_permission(self):
        from .admin import InvoiceAdmin
        from django.contrib.admin.sites import site
        admin_instance = InvoiceAdmin(Invoice, site)
        self.assertFalse(admin_instance.has_add_permission(request=None))
        self.assertFalse(admin_instance.has_delete_permission(request=None))
        all_fields = {f.name for f in Invoice._meta.fields}
        self.assertEqual(set(admin_instance.readonly_fields), all_fields)

    def test_payment_admin_has_no_add_permission(self):
        from .admin import PaymentAdmin
        from django.contrib.admin.sites import site
        admin_instance = PaymentAdmin(Payment, site)
        self.assertFalse(admin_instance.has_add_permission(request=None))
        self.assertFalse(admin_instance.has_delete_permission(request=None))
        all_fields = {f.name for f in Payment._meta.fields}
        self.assertEqual(set(admin_instance.readonly_fields), all_fields)

    def test_tenders_board_review_admin_has_no_add_permission(self):
        from .admin import TendersBoardReviewAdmin
        from django.contrib.admin.sites import site
        admin_instance = TendersBoardReviewAdmin(TendersBoardReview, site)
        self.assertFalse(admin_instance.has_add_permission(request=None))
        self.assertFalse(admin_instance.has_delete_permission(request=None))
        all_fields = {f.name for f in TendersBoardReview._meta.fields}
        self.assertEqual(set(admin_instance.readonly_fields), all_fields)

    def test_abandonment_admin_has_no_add_permission(self):
        from .admin import AbandonmentAdmin
        from django.contrib.admin.sites import site
        admin_instance = AbandonmentAdmin(Abandonment, site)
        self.assertFalse(admin_instance.has_add_permission(request=None))
        self.assertFalse(admin_instance.has_delete_permission(request=None))
        all_fields = {f.name for f in Abandonment._meta.fields}
        self.assertEqual(set(admin_instance.readonly_fields), all_fields)


class AnalyticsTests(TestCase):
    """Reports & Risk Analytics (blueprint Phase 5) — pure read/aggregation,
    visible to any authenticated staff member, same as staff_record_list/
    staff_requisition_list."""

    def setUp(self):
        self.law_profile = make_law_profile()
        self.staff = User.objects.create_user(username='any_staff', password='x', role=User.Role.PROCUREMENT_UNIT)

    def test_requires_login(self):
        response = self.client.get(reverse('staff_analytics'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/staff/login/', response.url)

    def test_accessible_to_any_authenticated_staff(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse('staff_analytics'))
        self.assertEqual(response.status_code, 200)

    def test_status_and_department_aggregation(self):
        make_record(self.law_profile, self.staff, department='Bursary', estimated_cost=100_000)
        make_record(self.law_profile, self.staff, department='Bursary', estimated_cost=200_000)
        make_record(self.law_profile, self.staff, department='Faculty of Science', estimated_cost=50_000)
        self.client.force_login(self.staff)
        response = self.client.get(reverse('staff_analytics'))
        self.assertContains(response, 'Bursary')
        self.assertContains(response, 'Faculty of Science')
        self.assertContains(response, '300,000')  # Bursary total (100,000 + 200,000)

    def test_complaint_counts_reflected(self):
        record = make_record(self.law_profile, self.staff)
        submit_complaint(
            record=record, complainant_name='Jane Doe', complainant_contact='jane@example.com',
            description='Concerned about the process.',
        )
        self.client.force_login(self.staff)
        response = self.client.get(reverse('staff_analytics'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<div class="val">1</div>')  # one pending complaint

    def test_full_chain_reflects_payment_and_cycle_time(self):
        fy = make_financial_year(self.law_profile)
        approver = User.objects.create_user(username='ao_analytics', password='x', role=User.Role.ACCOUNTING_OFFICER)
        requester = User.objects.create_user(username='ru_analytics', password='x', role=User.Role.REQUESTING_UNIT)
        finance = User.objects.create_user(username='fin_analytics', password='x', role=User.Role.FINANCE)
        make_threshold_rule(self.law_profile, self.staff, max_value=5_000_000, authority='Accounting Officer')

        plan = ProcurementPlan.objects.create(law_profile=self.law_profile, financial_year=fy, prepared_by=self.staff)
        line = PlanLine.objects.create(
            plan=plan, department='Bursary', item_description='Chairs', justification='need',
            estimated_cost=500_000, budget_line='B1', proposed_quarter='Q1', proposed_by=requester,
        )
        approve_plan(plan=plan, actor=approver)
        line.refresh_from_db()

        req = Requisition.objects.create(
            plan_line=line, title='Chairs requisition', department='Bursary',
            requested_value=500_000, budget_source=ProcurementRecord.BudgetSource.IGR, requested_by=requester,
        )
        submit_requisition(requisition=req, actor=requester)
        confirm_requisition_funds(requisition=req, actor=finance)
        review_requisition_packaging(requisition=req, actor=self.staff, note='Checked, no splitting.')
        determine_requisition_method(requisition=req, actor=self.staff)
        record = create_record_from_requisition(requisition=req, actor=self.staff, record_fields={
            'title': 'Chairs for Bursary', 'location': 'Main Campus',
            'planned_start_date': datetime.date.today(),
            'planned_end_date': datetime.date.today() + datetime.timedelta(days=30),
        })
        solicitation = prepare_solicitation(record=record, actor=self.staff, fields=SOLICITATION_FIELDS)
        approve_solicitation(solicitation=solicitation, actor=approver)
        publish_advertisement(
            solicitation=solicitation, actor=self.staff, channels=['institution_website'],
            publication_proof='Posted.', closing_date=datetime.date.today() + datetime.timedelta(days=30),
        )
        solicitation.advertisement.closing_date = datetime.date.today() - datetime.timedelta(days=1)
        solicitation.advertisement.save(update_fields=['closing_date'])
        bid = record_bid(solicitation=solicitation, actor=self.staff, vendor_name='Acme Furniture Ltd', bid_amount=480_000)
        record_tenders_board_review(
            solicitation=solicitation, actor=self.staff, recommended_bid=bid,
            evaluation_summary='Lowest evaluated bid.',
        )
        award = award_solicitation(
            solicitation=solicitation, actor=approver, winning_bid=bid, decision_note='Lowest evaluated bid.',
        )
        contract = sign_contract(
            award=award, actor=self.staff, contract_reference='CT-ANALYTICS-1',
            vendor_signatory_name='Acme MD', signed_date=datetime.date.today(),
            start_date=datetime.date.today(), end_date=datetime.date.today() + datetime.timedelta(days=90),
        )
        complete_contract(
            contract=contract, actor=approver, completion_date=datetime.date.today(),
            inspection_note='Delivery confirmed.',
        )
        invoice = submit_invoice(
            contract=contract, actor=self.staff, invoice_number='INV-ANALYTICS-1',
            amount=480_000, submitted_date=datetime.date.today(),
        )
        review_invoice(invoice=invoice, actor=finance, status=Invoice.Status.APPROVED, review_note='Checked.')
        record_payment(
            invoice=invoice, actor=finance, amount=480_000,
            payment_date=datetime.date.today(), payment_reference='PMT-ANALYTICS-1',
        )

        self.client.force_login(self.staff)
        response = self.client.get(reverse('staff_analytics'))
        self.assertContains(response, '480,000')  # total paid
        # Regression guard: created_at and completed_at both land "today"
        # in this test, so avg_cycle_days == 0 — a naive `{% if
        # avg_cycle_days %}` template check would treat 0 as falsy and
        # wrongly render the "no data" dash instead of "0".
        self.assertContains(response, '<div class="value">0</div>')
        self.assertNotContains(response, '<div class="value">—</div>')


class RiskAlertTests(TestCase):
    """Automated risk alerts (blueprint Phase 5) — surfaces existing
    rule-based signals (ProcurementRecord.is_cost_outlier, a new
    cycle-time equivalent, and Complaint.is_overdue) as concrete lists
    rather than just the aggregate counts staff_analytics already showed.
    Staff-only — see get_risk_alerts' docstring for why this isn't public."""

    def setUp(self):
        self.law_profile = make_law_profile()
        self.fy = make_financial_year(self.law_profile)
        self.staff = User.objects.create_user(username='pu_risk', password='x', role=User.Role.PROCUREMENT_UNIT)
        self.approver = User.objects.create_user(username='ao_risk', password='x', role=User.Role.ACCOUNTING_OFFICER)
        self.requester = User.objects.create_user(username='ru_risk', password='x', role=User.Role.REQUESTING_UNIT)
        self.finance = User.objects.create_user(username='fin_risk', password='x', role=User.Role.FINANCE)
        make_threshold_rule(self.law_profile, self.staff, max_value=50_000_000, authority='Accounting Officer')
        # One shared, pre-approved plan reused across every record this test
        # builds — ProcurementPlan is unique_together on (law_profile,
        # financial_year), so each test can only create one. New lines
        # added after approval are approved individually via
        # approve_plan_line (see approve_plan's own "amendment" handling).
        self.plan = ProcurementPlan.objects.create(
            law_profile=self.law_profile, financial_year=self.fy, prepared_by=self.staff,
        )
        approve_plan(plan=self.plan, actor=self.approver)

    def _build_awarded_record(self, *, awarded_cost, suffix, department='Bursary',
                               method='Open Competitive Bidding', vendor_name='Acme Furniture Ltd'):
        line = PlanLine.objects.create(
            plan=self.plan, department=department, item_description=f'Item {suffix}', justification='need',
            estimated_cost=awarded_cost, budget_line=f'B-{suffix}', proposed_quarter='Q1',
            proposed_by=self.requester, is_amendment=True,
        )
        approve_plan_line(plan_line=line, actor=self.approver)
        req = Requisition.objects.create(
            plan_line=line, title=f'Requisition {suffix}', department=department,
            requested_value=awarded_cost, budget_source=ProcurementRecord.BudgetSource.IGR,
            requested_by=self.requester,
        )
        submit_requisition(requisition=req, actor=self.requester)
        confirm_requisition_funds(requisition=req, actor=self.finance)
        review_requisition_packaging(requisition=req, actor=self.staff, note='Checked, no splitting.')
        determine_requisition_method(requisition=req, actor=self.staff)
        record = create_record_from_requisition(requisition=req, actor=self.staff, record_fields={
            'title': f'Record {suffix}', 'location': 'Main Campus',
            'planned_start_date': datetime.date.today(),
            'planned_end_date': datetime.date.today() + datetime.timedelta(days=30),
        })
        record.procurement_method = method
        record.save(update_fields=['procurement_method'])
        solicitation = prepare_solicitation(record=record, actor=self.staff, fields=SOLICITATION_FIELDS)
        approve_solicitation(solicitation=solicitation, actor=self.approver)
        publish_advertisement(
            solicitation=solicitation, actor=self.staff, channels=['institution_website'],
            publication_proof='Posted.', closing_date=datetime.date.today() + datetime.timedelta(days=30),
        )
        solicitation.advertisement.closing_date = datetime.date.today() - datetime.timedelta(days=1)
        solicitation.advertisement.save(update_fields=['closing_date'])
        bid = record_bid(solicitation=solicitation, actor=self.staff, vendor_name=vendor_name, bid_amount=awarded_cost)
        record_tenders_board_review(
            solicitation=solicitation, actor=self.staff, recommended_bid=bid, evaluation_summary='Lowest bid.',
        )
        award_solicitation(solicitation=solicitation, actor=self.approver, winning_bid=bid, decision_note='Lowest bid.')
        record.refresh_from_db()
        return record

    def _build_completed_record(self, *, awarded_cost, cycle_days, suffix, department='Bursary',
                                 method='Open Competitive Bidding', vendor_name='Acme Furniture Ltd'):
        record = self._build_awarded_record(
            awarded_cost=awarded_cost, suffix=suffix, department=department, method=method,
            vendor_name=vendor_name,
        )
        contract = sign_contract(
            award=record.solicitations.get().award, actor=self.staff, contract_reference=f'CT-RISK-{suffix}',
            vendor_signatory_name='Vendor MD', signed_date=datetime.date.today(),
            start_date=datetime.date.today(), end_date=datetime.date.today() + datetime.timedelta(days=90),
        )
        completion = complete_contract(
            contract=contract, actor=self.approver, completion_date=datetime.date.today(),
            inspection_note='Delivery confirmed.',
        )
        start = timezone.now() - datetime.timedelta(days=cycle_days)
        ProcurementRecord.objects.filter(pk=record.pk).update(created_at=start)
        record.refresh_from_db()
        return record

    def test_cost_outlier_surfaced_in_risk_alerts(self):
        for i in range(3):
            self._build_awarded_record(awarded_cost=100_000, suffix=f'norm{i}')
        outlier = self._build_awarded_record(awarded_cost=250_000, suffix='outlier')
        alerts = get_risk_alerts()
        flagged_ids = [o['record'].id for o in alerts['cost_outliers']]
        self.assertIn(outlier.id, flagged_ids)

    def test_cost_outlier_not_flagged_when_within_normal_range(self):
        for i in range(4):
            self._build_awarded_record(awarded_cost=100_000, suffix=f'norm{i}')
        alerts = get_risk_alerts()
        self.assertEqual(alerts['cost_outliers'], [])

    def test_cycle_time_outlier_surfaced_in_risk_alerts(self):
        for i in range(3):
            self._build_completed_record(awarded_cost=100_000, cycle_days=10, suffix=f'fast{i}')
        slow = self._build_completed_record(awarded_cost=100_000, cycle_days=60, suffix='slow')
        alerts = get_risk_alerts()
        flagged_ids = [o['record'].id for o in alerts['cycle_outliers']]
        self.assertIn(slow.id, flagged_ids)

    def test_vendor_repeat_complaints_surfaced(self):
        record1 = self._build_awarded_record(awarded_cost=100_000, suffix='v1', vendor_name='Repeat Vendor Ltd')
        record2 = self._build_awarded_record(awarded_cost=100_000, suffix='v2', vendor_name='Repeat Vendor Ltd')
        submit_complaint(
            record=record1, complainant_name='Jane Doe', complainant_contact='jane@example.com',
            description='Concern one.',
        )
        submit_complaint(
            record=record2, complainant_name='John Doe', complainant_contact='john@example.com',
            description='Concern two.',
        )
        alerts = get_risk_alerts()
        vendors = {v['vendor_name']: v['count'] for v in alerts['vendor_repeat_complaints']}
        self.assertEqual(vendors.get('Repeat Vendor Ltd'), 2)

    def test_single_complaint_vendor_not_flagged_as_repeat(self):
        record = self._build_awarded_record(awarded_cost=100_000, suffix='single', vendor_name='One-Off Vendor Ltd')
        submit_complaint(
            record=record, complainant_name='Jane Doe', complainant_contact='jane@example.com',
            description='Concern.',
        )
        alerts = get_risk_alerts()
        vendors = {v['vendor_name'] for v in alerts['vendor_repeat_complaints']}
        self.assertNotIn('One-Off Vendor Ltd', vendors)

    def test_overdue_complaint_surfaced(self):
        record = make_record(self.law_profile, self.staff)
        complaint = submit_complaint(
            record=record, complainant_name='Jane Doe', complainant_contact='jane@example.com',
            description='Filed long ago.',
        )
        days = self.law_profile.default_complaint_response_days
        stale = timezone.now() - datetime.timedelta(days=days + 1)
        Complaint.objects.filter(pk=complaint.pk).update(submitted_at=stale)
        alerts = get_risk_alerts()
        flagged_ids = [c.id for c in alerts['overdue_complaints']]
        self.assertIn(complaint.id, flagged_ids)

    def test_risk_alerts_section_renders_on_analytics_page(self):
        for i in range(3):
            self._build_awarded_record(awarded_cost=100_000, suffix=f'norm{i}')
        outlier = self._build_awarded_record(awarded_cost=250_000, suffix='outlier2')
        self.client.force_login(self.staff)
        response = self.client.get(reverse('staff_analytics'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Cost Outliers')
        self.assertContains(response, outlier.title)


class OCDSExportTests(TestCase):
    """Open Contracting Data Standard export (blueprint Phase 5
    interoperability) — one compiled release per record, public, no
    login. See export_ocds's own docstring in views.py for the honesty
    caveats on ocid/license/procurementMethod."""

    def setUp(self):
        self.law_profile = make_law_profile()
        self.fy = make_financial_year(self.law_profile)
        self.staff = User.objects.create_user(username='pu_ocds', password='x', role=User.Role.PROCUREMENT_UNIT)
        self.approver = User.objects.create_user(username='ao_ocds', password='x', role=User.Role.ACCOUNTING_OFFICER)
        self.requester = User.objects.create_user(username='ru_ocds', password='x', role=User.Role.REQUESTING_UNIT)
        self.finance = User.objects.create_user(username='fin_ocds', password='x', role=User.Role.FINANCE)
        make_threshold_rule(self.law_profile, self.staff, max_value=5_000_000, authority='Accounting Officer')

    def _get_release(self, record):
        response = self.client.get(reverse('export_ocds'))
        payload = json.loads(response.content)
        matches = [r for r in payload['releases'] if r['tender']['id'] == str(record.id)]
        self.assertEqual(len(matches), 1)
        return payload, matches[0]

    def test_planning_record_maps_to_planning_status_and_ocid_shape(self):
        record = make_record(self.law_profile, self.staff)
        _, release = self._get_release(record)
        self.assertEqual(release['tender']['status'], 'planning')
        self.assertTrue(release['ocid'].startswith('ocds-prodap-'))
        self.assertEqual(release['tender']['procurementMethodDetails'], record.procurement_method)

    def test_open_competitive_bidding_maps_to_open_ocds_method(self):
        record = make_record(self.law_profile, self.staff, procurement_method='Open Competitive Bidding')
        _, release = self._get_release(record)
        self.assertEqual(release['tender'].get('procurementMethod'), 'open')

    def test_publisher_name_matches_tenant_name(self):
        from django.conf import settings
        record = make_record(self.law_profile, self.staff)
        payload, _ = self._get_release(record)
        self.assertEqual(payload['publisher']['name'], settings.TENANT_NAME)

    def _build_awarded_record(self):
        plan = ProcurementPlan.objects.create(law_profile=self.law_profile, financial_year=self.fy, prepared_by=self.staff)
        line = PlanLine.objects.create(
            plan=plan, department='Bursary', item_description='Chairs', justification='need',
            estimated_cost=480_000, budget_line='B1', proposed_quarter='Q1', proposed_by=self.requester,
        )
        approve_plan(plan=plan, actor=self.approver)
        line.refresh_from_db()
        req = Requisition.objects.create(
            plan_line=line, title='Chairs requisition', department='Bursary',
            requested_value=480_000, budget_source=ProcurementRecord.BudgetSource.IGR, requested_by=self.requester,
        )
        submit_requisition(requisition=req, actor=self.requester)
        confirm_requisition_funds(requisition=req, actor=self.finance)
        review_requisition_packaging(requisition=req, actor=self.staff, note='Checked, no splitting.')
        determine_requisition_method(requisition=req, actor=self.staff)
        record = create_record_from_requisition(requisition=req, actor=self.staff, record_fields={
            'title': 'OCDS Chairs Record', 'location': 'Main Campus',
            'planned_start_date': datetime.date.today(),
            'planned_end_date': datetime.date.today() + datetime.timedelta(days=30),
        })
        solicitation = prepare_solicitation(record=record, actor=self.staff, fields=SOLICITATION_FIELDS)
        approve_solicitation(solicitation=solicitation, actor=self.approver)
        publish_advertisement(
            solicitation=solicitation, actor=self.staff, channels=['institution_website'],
            publication_proof='Posted.', closing_date=datetime.date.today() + datetime.timedelta(days=30),
        )
        solicitation.advertisement.closing_date = datetime.date.today() - datetime.timedelta(days=1)
        solicitation.advertisement.save(update_fields=['closing_date'])
        bid = record_bid(solicitation=solicitation, actor=self.staff, vendor_name='Acme Furniture Ltd', bid_amount=480_000)
        record_tenders_board_review(
            solicitation=solicitation, actor=self.staff, recommended_bid=bid, evaluation_summary='Lowest bid.',
        )
        award = award_solicitation(
            solicitation=solicitation, actor=self.approver, winning_bid=bid, decision_note='Lowest bid.',
        )
        record.refresh_from_db()
        return record, award

    def test_awarded_record_includes_award_and_supplier_party(self):
        record, award = self._build_awarded_record()
        _, release = self._get_release(record)
        self.assertIn('awards', release)
        self.assertEqual(release['awards'][0]['id'], str(award.id))
        self.assertEqual(release['awards'][0]['value']['amount'], 480_000.0)
        supplier_names = [p['name'] for p in release['parties'] if 'supplier' in p['roles']]
        self.assertIn('Acme Furniture Ltd', supplier_names)
        self.assertEqual(release['tender']['status'], 'complete')

    def test_signed_contract_included(self):
        record, award = self._build_awarded_record()
        sign_contract(
            award=award, actor=self.staff, contract_reference='CT-OCDS-1',
            vendor_signatory_name='Acme MD', signed_date=datetime.date.today(),
            start_date=datetime.date.today(), end_date=datetime.date.today() + datetime.timedelta(days=90),
        )
        _, release = self._get_release(record)
        self.assertIn('contracts', release)
        self.assertEqual(release['contracts'][0]['awardID'], str(award.id))
        self.assertEqual(release['contracts'][0]['status'], 'active')


class MFAServiceTests(TestCase):
    """Authenticator-app (TOTP) second factor for staff logins — closes
    the blueprint gap "no MFA on staff logins." Service-layer validation
    logic; MFALoginFlowTests below covers the actual login/setup views."""

    def setUp(self):
        self.user = User.objects.create_user(username='mfa_user', password='x', role=User.Role.PROCUREMENT_UNIT)

    def _enable(self):
        secret = pyotp.random_base32()
        backup_codes = confirm_mfa_enrollment(user=self.user, secret=secret, code=pyotp.TOTP(secret).now())
        return secret, backup_codes

    def test_start_enrollment_does_not_raise_when_not_yet_enrolled(self):
        start_mfa_enrollment(user=self.user)  # should not raise

    def test_start_enrollment_raises_if_already_confirmed(self):
        self._enable()
        with self.assertRaises(ValidationError):
            start_mfa_enrollment(user=self.user)

    def test_start_enrollment_does_not_create_a_device_row(self):
        # A bare enrollment check must stay side-effect-free — no row
        # until confirm_mfa_enrollment() actually runs.
        start_mfa_enrollment(user=self.user)
        self.assertFalse(TOTPDevice.objects.filter(user=self.user).exists())

    def test_confirm_rejects_wrong_code(self):
        secret = pyotp.random_base32()
        with self.assertRaises(ValidationError):
            confirm_mfa_enrollment(user=self.user, secret=secret, code='000000')
        self.assertFalse(TOTPDevice.objects.filter(user=self.user).exists())

    def test_confirm_with_correct_code_enables_and_issues_backup_codes(self):
        secret, backup_codes = self._enable()
        device = TOTPDevice.objects.get(user=self.user)
        self.assertTrue(device.confirmed)
        self.assertIsNotNone(device.confirmed_at)
        self.assertEqual(device.secret, secret)
        self.assertEqual(len(backup_codes), 8)
        self.assertEqual(MFABackupCode.objects.filter(user=self.user).count(), 8)
        self.assertTrue(
            AuditEvent.objects.filter(
                content_type__model='user', object_id=self.user.pk, action=AuditEvent.Action.MFA_ENABLED,
            ).exists()
        )

    def test_confirm_raises_if_already_confirmed(self):
        self._enable()
        with self.assertRaises(ValidationError):
            confirm_mfa_enrollment(user=self.user, secret=pyotp.random_base32(), code='000000')

    def test_verify_accepts_valid_totp_code(self):
        secret, _ = self._enable()
        self.assertTrue(verify_mfa_code(user=self.user, code=pyotp.TOTP(secret).now()))

    def test_verify_accepts_backup_code_once_only(self):
        _, backup_codes = self._enable()
        self.assertTrue(verify_mfa_code(user=self.user, code=backup_codes[0]))
        with self.assertRaises(ValidationError):
            verify_mfa_code(user=self.user, code=backup_codes[0])

    def test_verify_locks_out_after_max_failed_attempts(self):
        secret, _ = self._enable()
        for _ in range(MFA_MAX_FAILED_ATTEMPTS):
            with self.assertRaises(ValidationError):
                verify_mfa_code(user=self.user, code='000000')
        with self.assertRaises(ValidationError) as ctx:
            verify_mfa_code(user=self.user, code=pyotp.TOTP(secret).now())
        self.assertIn('Too many failed attempts', str(ctx.exception))

    def test_verify_rejects_the_same_code_used_twice(self):
        # Replay protection: capturing a valid code and resubmitting it
        # must not succeed a second time, even within its validity window.
        secret, _ = self._enable()
        code = pyotp.TOTP(secret).now()
        self.assertTrue(verify_mfa_code(user=self.user, code=code))
        with self.assertRaises(ValidationError) as ctx:
            verify_mfa_code(user=self.user, code=code)
        self.assertIn('already been used', str(ctx.exception))

    def test_disable_removes_device_and_backup_codes(self):
        self._enable()
        disable_mfa(user=self.user)
        self.assertFalse(TOTPDevice.objects.filter(user=self.user).exists())
        self.assertFalse(MFABackupCode.objects.filter(user=self.user).exists())
        self.assertTrue(
            AuditEvent.objects.filter(
                content_type__model='user', object_id=self.user.pk, action=AuditEvent.Action.MFA_DISABLED,
            ).exists()
        )

    def test_disable_raises_if_not_enabled(self):
        with self.assertRaises(ValidationError):
            disable_mfa(user=self.user)


class MFALoginFlowTests(TestCase):
    """The actual login/setup views, including the two-step
    password-then-code flow via the real HTTP client (not force_login,
    since the whole point is verifying request.user stays anonymous
    between the two steps)."""

    def setUp(self):
        self.user = User.objects.create_user(username='mfa_login_user', password='correcthorse', role=User.Role.PROCUREMENT_UNIT)

    def _enable_mfa(self):
        secret = pyotp.random_base32()
        confirm_mfa_enrollment(user=self.user, secret=secret, code=pyotp.TOTP(secret).now())
        return TOTPDevice.objects.get(user=self.user)

    def test_login_without_mfa_enabled_logs_in_directly(self):
        response = self.client.post(reverse('staff_login'), {'username': 'mfa_login_user', 'password': 'correcthorse'})
        self.assertEqual(response.status_code, 302)
        self.assertNotEqual(response.url, reverse('staff_mfa_verify'))
        response2 = self.client.get(reverse('staff_record_list'))
        self.assertEqual(response2.status_code, 200)

    def test_login_with_mfa_enabled_does_not_log_in_yet(self):
        self._enable_mfa()
        response = self.client.post(reverse('staff_login'), {'username': 'mfa_login_user', 'password': 'correcthorse'})
        self.assertRedirects(response, reverse('staff_mfa_verify'))
        # Still not authenticated — the password step alone must not grant access.
        response2 = self.client.get(reverse('staff_record_list'))
        self.assertEqual(response2.status_code, 302)
        self.assertIn('/staff/login/', response2.url)

    def test_mfa_verify_with_correct_code_completes_login(self):
        device = self._enable_mfa()
        self.client.post(reverse('staff_login'), {'username': 'mfa_login_user', 'password': 'correcthorse'})
        response = self.client.post(reverse('staff_mfa_verify'), {'code': pyotp.TOTP(device.secret).now()}, follow=True)
        self.assertEqual(response.status_code, 200)
        response2 = self.client.get(reverse('staff_record_list'))
        self.assertEqual(response2.status_code, 200)

    def test_mfa_verify_with_wrong_code_stays_logged_out(self):
        self._enable_mfa()
        self.client.post(reverse('staff_login'), {'username': 'mfa_login_user', 'password': 'correcthorse'})
        response = self.client.post(reverse('staff_mfa_verify'), {'code': '000000'})
        self.assertEqual(response.status_code, 200)
        response2 = self.client.get(reverse('staff_record_list'))
        self.assertEqual(response2.status_code, 302)

    def test_relogging_in_resets_lockout(self):
        # A locked-out device must not be a permanent dead end — a fresh
        # correct password submission re-opens the door (the actual rate
        # limit is coupling code-guessing to a correct password guess).
        device = self._enable_mfa()
        self.client.post(reverse('staff_login'), {'username': 'mfa_login_user', 'password': 'correcthorse'})
        for _ in range(MFA_MAX_FAILED_ATTEMPTS):
            self.client.post(reverse('staff_mfa_verify'), {'code': '000000'})
        device.refresh_from_db()
        self.assertGreaterEqual(device.failed_attempts, MFA_MAX_FAILED_ATTEMPTS)

        self.client.post(reverse('staff_login'), {'username': 'mfa_login_user', 'password': 'correcthorse'})
        device.refresh_from_db()
        self.assertEqual(device.failed_attempts, 0)
        response = self.client.post(reverse('staff_mfa_verify'), {'code': pyotp.TOTP(device.secret).now()}, follow=True)
        response2 = self.client.get(reverse('staff_record_list'))
        self.assertEqual(response2.status_code, 200)

    def test_mfa_verify_without_pending_session_redirects_to_login(self):
        response = self.client.get(reverse('staff_mfa_verify'))
        self.assertRedirects(response, reverse('staff_login'))

    def test_mfa_setup_requires_login(self):
        response = self.client.get(reverse('staff_mfa_setup'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/staff/login/', response.url)

    def test_mfa_setup_shows_qr_and_secret_when_not_enabled(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('staff_mfa_setup'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<svg')
        # No device row yet — the secret is staged in the session only,
        # not written to the database by a bare GET (see
        # services.start_mfa_enrollment's docstring).
        self.assertFalse(TOTPDevice.objects.filter(user=self.user).exists())
        secret = self.client.session['mfa_pending_secret']
        self.assertContains(response, secret)

    def test_mfa_setup_get_does_not_write_a_device_row(self):
        self.client.force_login(self.user)
        self.client.get(reverse('staff_mfa_setup'))
        self.client.get(reverse('staff_mfa_setup'))
        self.client.get(reverse('staff_mfa_setup'))
        self.assertFalse(TOTPDevice.objects.filter(user=self.user).exists())

    def test_mfa_setup_post_correct_code_enables_and_shows_backup_codes(self):
        self.client.force_login(self.user)
        self.client.get(reverse('staff_mfa_setup'))
        secret = self.client.session['mfa_pending_secret']
        response = self.client.post(reverse('staff_mfa_setup'), {'code': pyotp.TOTP(secret).now()})
        self.assertEqual(response.status_code, 200)
        device = TOTPDevice.objects.get(user=self.user)
        self.assertTrue(device.confirmed)
        self.assertEqual(device.secret, secret)
        self.assertEqual(MFABackupCode.objects.filter(user=self.user).count(), 8)

    def test_mfa_setup_already_enabled_shows_disable_form(self):
        self.client.force_login(self.user)
        self._enable_mfa()
        response = self.client.get(reverse('staff_mfa_setup'))
        self.assertContains(response, 'enabled')
        self.assertContains(response, 'Disable MFA')

    def test_mfa_disable_with_correct_code_disables(self):
        self.client.force_login(self.user)
        device = self._enable_mfa()
        response = self.client.post(reverse('staff_mfa_disable'), {'code': pyotp.TOTP(device.secret).now()}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(TOTPDevice.objects.filter(user=self.user).exists())

    def test_mfa_disable_with_wrong_code_does_not_disable(self):
        self.client.force_login(self.user)
        self._enable_mfa()
        self.client.post(reverse('staff_mfa_disable'), {'code': '000000'})
        self.assertTrue(TOTPDevice.objects.filter(user=self.user, confirmed=True).exists())
