import datetime

from django.core.management.base import BaseCommand, CommandError

from procurement.models import LawProfile, ProcurementRecord, User
from procurement.services import transition_status

TODAY = datetime.date.today()


def d(days_offset):
    return TODAY + datetime.timedelta(days=days_offset)


class Command(BaseCommand):
    help = 'Seed 5-8 realistic sample procurement records spanning statuses (build prompt section 8 step 6).'

    def handle(self, *args, **options):
        try:
            law_profile = LawProfile.objects.get(slug='federal-ppa-2007')
        except LawProfile.DoesNotExist:
            raise CommandError('Run "manage.py seed_law_profiles" first.')

        actor = User.objects.filter(username='officer').first() or User.objects.filter(is_superuser=True).first()
        if not actor:
            raise CommandError('Run "manage.py seed_users" first.')

        if ProcurementRecord.objects.exists():
            self.stdout.write('Sample records already exist — nothing to do.')
            return

        methods = law_profile.procurement_methods

        records = [
            dict(
                title='Renovation of Faculty of Engineering Lecture Halls',
                description='Structural repairs, roofing, and furniture replacement for 6 lecture halls.',
                department='Faculty of Engineering',
                budget_source=ProcurementRecord.BudgetSource.TETFUND,
                estimated_cost=42_000_000,
                procurement_method=methods[0],
                location='Main Campus',
                planned_start_date=d(-60), planned_end_date=d(30),
                status_path=['Planning', 'Advertised', 'Tendering', 'Awarded', 'Implementation'],
                awarded_cost=39_500_000, vendor_name='Zenith Structures Ltd', vendor_registration_no='RC-882210',
            ),
            dict(
                title='Supply of Laboratory Equipment for Chemistry Department',
                description='Procurement of analytical balances, fume hoods, and glassware.',
                department='Faculty of Science',
                budget_source=ProcurementRecord.BudgetSource.GOVERNMENT_SUBVENTION,
                estimated_cost=18_500_000,
                procurement_method=methods[3],
                location='Science Complex',
                planned_start_date=d(-20), planned_end_date=d(40),
                status_path=['Planning', 'Advertised'],
            ),
            dict(
                title='Construction of 500-Capacity Student Hostel Block D',
                description='New hostel block including plumbing, electrical, and furnishing.',
                department='Physical Planning Unit',
                budget_source=ProcurementRecord.BudgetSource.DONOR_GRANT,
                estimated_cost=310_000_000,
                procurement_method=methods[0],
                location='North Campus Extension',
                planned_start_date=d(-200), planned_end_date=d(200),
                status_path=['Planning', 'Advertised', 'Tendering', 'Awarded', 'Implementation'],
                awarded_cost=298_000_000, vendor_name='Cornerstone Builders Nigeria Ltd', vendor_registration_no='RC-114400',
            ),
            dict(
                title='Campus-Wide CCTV Security System Upgrade',
                description='Installation of IP cameras and a central monitoring room.',
                department='Works and Security Unit',
                budget_source=ProcurementRecord.BudgetSource.IGR,
                estimated_cost=25_000_000,
                procurement_method=methods[1],
                location='All Campuses',
                planned_start_date=d(-90), planned_end_date=d(-10), actual_start_date=d(-85), actual_end_date=d(-15),
                status_path=['Planning', 'Advertised', 'Tendering', 'Awarded', 'Implementation', 'Completed'],
                awarded_cost=24_100_000, vendor_name='SecureView Technologies', vendor_registration_no='RC-556781',
            ),
            dict(
                title='Solar Power Backup Installation for Senate Building',
                description='Hybrid solar-inverter system to reduce diesel generator dependency.',
                department='Vice-Chancellor\'s Office',
                budget_source=ProcurementRecord.BudgetSource.ALUMNI,
                estimated_cost=15_800_000,
                procurement_method=methods[3],
                location='Administrative Block',
                planned_start_date=d(-150), planned_end_date=d(-60), actual_start_date=d(-140),
                status_path=['Planning', 'Advertised', 'Tendering', 'Awarded', 'Implementation', 'Abandoned'],
                awarded_cost=15_800_000, vendor_name='SunGrid Power Solutions', vendor_registration_no='RC-773310',
                abandon_note='Vendor ceased operations mid-project after failing to remit performance bond; contract terminated and relisted for FY2027 planning.',
            ),
            dict(
                title='Annual Supply of Office Stationery and Consumables',
                description='University-wide stationery supply contract for the academic year.',
                department='Bursary',
                budget_source=ProcurementRecord.BudgetSource.GOVERNMENT_SUBVENTION,
                estimated_cost=6_200_000,
                procurement_method=methods[3],
                location='All Campuses',
                planned_start_date=d(-30), planned_end_date=d(335),
                status_path=['Planning', 'Advertised', 'Tendering', 'Awarded'],
                awarded_cost=5_950_000, vendor_name='Bright Office Supplies Ltd', vendor_registration_no='RC-990021',
            ),
            dict(
                title='Emergency Repair of Central Water Borehole',
                description='Replacement of failed submersible pump serving student hostels.',
                department='Works and Security Unit',
                budget_source=ProcurementRecord.BudgetSource.IGR,
                estimated_cost=3_400_000,
                procurement_method=methods[5],
                location='Central Utilities Yard',
                planned_start_date=d(-10), planned_end_date=d(-2), actual_start_date=d(-9), actual_end_date=d(-3),
                status_path=['Planning', 'Advertised', 'Awarded', 'Implementation', 'Completed'],
                awarded_cost=3_350_000, vendor_name='AquaFix Nigeria', vendor_registration_no='RC-445120',
            ),
        ]

        for r in records:
            status_path = r.pop('status_path')
            abandon_note = r.pop('abandon_note', None)
            record = ProcurementRecord.objects.create(
                law_profile=law_profile,
                created_by=actor,
                status=status_path[0],
                **r,
            )
            prev = status_path[0]
            for step in status_path[1:]:
                note = 'Routine status update.'
                if step == 'Abandoned' and abandon_note:
                    note = abandon_note
                transition_status(record=record, new_status=step, updated_by=actor, note=note)
                prev = step
            self.stdout.write(f'Created "{record.title}" -> {record.status}')

        self.stdout.write(self.style.SUCCESS(f'Seeded {len(records)} sample procurement records.'))
