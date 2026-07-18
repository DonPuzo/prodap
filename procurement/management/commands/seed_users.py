from django.core.management.base import BaseCommand

from procurement.models import User


class Command(BaseCommand):
    help = 'Seed one admin and one procurement_officer user for local testing only.'

    def handle(self, *args, **options):
        created_any = False

        if not User.objects.filter(username='admin').exists():
            User.objects.create_superuser(
                username='admin',
                email='admin@example.edu.ng',
                password='ChangeMe-Local-Dev-Only-123',
                role=User.Role.ADMIN,
            )
            created_any = True
            self.stdout.write(self.style.WARNING(
                'Created local admin user — username: admin  password: ChangeMe-Local-Dev-Only-123\n'
                'This is an obviously fake password for local testing ONLY. '
                'It MUST be changed before any real deployment.'
            ))

        if not User.objects.filter(username='officer').exists():
            User.objects.create_user(
                username='officer',
                email='officer@example.edu.ng',
                password='ChangeMe-Local-Dev-Only-123',
                role=User.Role.PROCUREMENT_OFFICER,
            )
            created_any = True
            self.stdout.write(self.style.WARNING(
                'Created local procurement_officer user — username: officer  password: ChangeMe-Local-Dev-Only-123\n'
                'This is an obviously fake password for local testing ONLY. '
                'It MUST be changed before any real deployment.'
            ))

        if not created_any:
            self.stdout.write('Seed users already exist — nothing to do.')
