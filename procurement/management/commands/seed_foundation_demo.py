import datetime

from django.core.management.base import BaseCommand, CommandError

from procurement.models import (
    FinancialYear,
    LawProfile,
    PlanLine,
    ProcurementPlan,
    ProcurementRecord,
    Requisition,
    User,
)
from procurement.services import (
    approve_plan,
    confirm_requisition_funds,
    create_record_from_requisition,
    determine_requisition_method,
    review_requisition_packaging,
    submit_plan,
    submit_requisition,
)


class Command(BaseCommand):
    help = (
        'Walk one demo ProcurementPlan through the real Phase 1-Foundation service '
        'functions end-to-end (plan -> line -> approve -> requisition -> funds -> '
        'packaging -> method -> record). Idempotent; also doubles as a live smoke '
        'test of the service layer on every deploy. Run after seed_users and '
        'seed_law_profiles.'
    )

    def handle(self, *args, **options):
        try:
            law_profile = LawProfile.objects.get(slug='federal-ppa-2007')
            financial_year = FinancialYear.objects.get(law_profile=law_profile, is_current=True)
        except (LawProfile.DoesNotExist, FinancialYear.DoesNotExist):
            raise CommandError('Run "manage.py seed_law_profiles" first.')

        try:
            preparer = User.objects.get(username='officer')
            approver = User.objects.get(username='accounting_officer')
            requester = User.objects.get(username='requester')
            finance = User.objects.get(username='finance')
        except User.DoesNotExist:
            raise CommandError('Run "manage.py seed_users" first.')

        if ProcurementPlan.objects.filter(law_profile=law_profile, financial_year=financial_year).exists():
            self.stdout.write('Foundation demo plan already exists — nothing to do.')
            return

        plan = ProcurementPlan.objects.create(
            law_profile=law_profile, financial_year=financial_year, prepared_by=preparer,
        )
        line = PlanLine.objects.create(
            plan=plan,
            department='Faculty of Engineering',
            item_description='Structural Survey Equipment',
            justification='Replacement of aging survey equipment for undergraduate fieldwork.',
            quantity=1,
            unit_of_measure='set',
            estimated_cost=4_500_000,
            budget_line='ENG-2026-EQUIP-01',
            proposed_method='Request for Quotations',
            proposed_quarter='Q1',
            proposed_by=requester,
        )
        submit_plan(plan=plan, actor=preparer)
        approve_plan(plan=plan, actor=approver, note='Approved for FY — routine equipment replacement.')
        line.refresh_from_db()

        requisition = Requisition.objects.create(
            plan_line=line,
            title='Structural Survey Equipment — Faculty of Engineering',
            department=line.department,
            requested_value=line.estimated_cost,
            budget_source=ProcurementRecord.BudgetSource.TETFUND,
            requested_by=requester,
        )
        submit_requisition(requisition=requisition, actor=requester)
        confirm_requisition_funds(
            requisition=requisition, actor=finance, note='Funds available under FY TETFund allocation.',
        )
        review_requisition_packaging(
            requisition=requisition, actor=preparer,
            note='Checked against requisitions from the last 90 days — no similar recent purchases found.',
        )
        determine_requisition_method(requisition=requisition, actor=preparer)

        record = create_record_from_requisition(
            requisition=requisition, actor=preparer, record_fields={
                'title': 'Structural Survey Equipment — Faculty of Engineering',
                'description': 'Total station and GPS survey equipment for undergraduate fieldwork.',
                'location': 'Main Campus',
                'planned_start_date': datetime.date.today() + datetime.timedelta(days=14),
                'planned_end_date': datetime.date.today() + datetime.timedelta(days=60),
            },
        )

        self.stdout.write(self.style.SUCCESS(
            f'Seeded Foundation demo: plan {financial_year.label} -> requisition '
            f'{requisition.process_identifier} -> record "{record.title}".'
        ))
