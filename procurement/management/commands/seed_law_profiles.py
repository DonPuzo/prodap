import datetime

from django.core.management.base import BaseCommand

from procurement.models import FinancialYear, LawProfile, ThresholdRule, User

FEDERAL_PPA_2007 = {
    'slug': 'federal-ppa-2007',
    'jurisdiction_type': 'federal',
    'governing_law': 'Public Procurement Act No. 14, 2007',
    'regulating_body': 'Bureau of Public Procurement (BPP)',
    'procurement_methods': [
        'Open Competitive Bidding',
        'Restricted Tendering',
        'Two-Stage Tendering',
        'Request for Quotations',
        'Direct Procurement',
        'Emergency Procurement',
    ],
    'approval_thresholds': [
        {'min': 0, 'max': 5000000, 'approving_authority': 'Accounting Officer'},
        {'min': 5000001, 'max': 50000000, 'approving_authority': 'Ministerial Tenders Board'},
        {'min': 50000001, 'max': None, 'approving_authority': 'Federal Executive Council'},
    ],
}


class Command(BaseCommand):
    help = (
        'Seed the federal PPA 2007 law profile plus a current FinancialYear and the '
        'versioned ThresholdRule rows equivalent to its 3-band approval_thresholds '
        '(Phase 1-Foundation — run seed_users first, this needs a user for created_by).'
    )

    def handle(self, *args, **options):
        profile, created = LawProfile.objects.update_or_create(
            slug=FEDERAL_PPA_2007['slug'],
            defaults={k: v for k, v in FEDERAL_PPA_2007.items() if k != 'slug'},
        )
        verb = 'Created' if created else 'Updated'
        self.stdout.write(self.style.SUCCESS(f'{verb} law profile: {profile.governing_law}'))

        actor = User.objects.filter(is_superuser=True).first()
        if not actor:
            self.stdout.write(self.style.WARNING(
                'No superuser found — skipping FinancialYear/ThresholdRule seeding. '
                'Run seed_users first, then re-run this command.'
            ))
            return

        today = datetime.date.today()
        year_label = f'FY{today.year}'
        financial_year, fy_created = FinancialYear.objects.update_or_create(
            law_profile=profile, label=year_label,
            defaults={
                'start_date': datetime.date(today.year, 1, 1),
                'end_date': datetime.date(today.year, 12, 31),
                'is_current': True,
            },
        )
        FinancialYear.objects.filter(law_profile=profile).exclude(pk=financial_year.pk).update(is_current=False)
        self.stdout.write(self.style.SUCCESS(f'{"Created" if fy_created else "Updated"} financial year: {year_label}'))

        anchor = datetime.date(2020, 1, 1)
        for band in FEDERAL_PPA_2007['approval_thresholds']:
            ThresholdRule.objects.update_or_create(
                law_profile=profile,
                procurement_method='Open Competitive Bidding',
                min_value=band['min'],
                effective_from=anchor,
                defaults={
                    'max_value': band['max'],
                    'approving_authority': band['approving_authority'],
                    'is_default_for_range': True,
                    'is_active': True,
                    'note': 'Seeded from LawProfile.approval_thresholds (federal PPA 2007 bands).',
                    'created_by': actor,
                },
            )
        self.stdout.write(self.style.SUCCESS(
            f'Seeded {len(FEDERAL_PPA_2007["approval_thresholds"])} threshold rules for {profile.governing_law}.'
        ))
