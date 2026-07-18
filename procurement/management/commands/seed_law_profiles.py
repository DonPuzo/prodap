from django.core.management.base import BaseCommand

from procurement.models import LawProfile

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
    help = 'Seed the federal PPA 2007 law profile (MVP scope — see build prompt section 5).'

    def handle(self, *args, **options):
        profile, created = LawProfile.objects.update_or_create(
            slug=FEDERAL_PPA_2007['slug'],
            defaults={k: v for k, v in FEDERAL_PPA_2007.items() if k != 'slug'},
        )
        verb = 'Created' if created else 'Updated'
        self.stdout.write(self.style.SUCCESS(f'{verb} law profile: {profile.governing_law}'))
