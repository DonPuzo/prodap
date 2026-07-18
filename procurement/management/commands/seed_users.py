import secrets

from django.core.management.base import BaseCommand

from procurement.models import User


class Command(BaseCommand):
    help = 'Seed one admin and one procurement_officer user with a freshly generated password.'

    def handle(self, *args, **options):
        created_any = False

        if not User.objects.filter(username='admin').exists():
            password = secrets.token_urlsafe(12)
            User.objects.create_superuser(
                username='admin',
                email='admin@example.edu.ng',
                password=password,
                role=User.Role.ADMIN,
            )
            created_any = True
            self.stdout.write(self.style.WARNING(
                f'Created local admin user — username: admin  password: {password}\n'
                'This password is shown only once, here in this command output — save it now. '
                'It was randomly generated per-environment, so it is safe to seed on a real '
                'deployment, but you should still rotate it once you have real staff accounts.'
            ))

        if not User.objects.filter(username='officer').exists():
            password = secrets.token_urlsafe(12)
            User.objects.create_user(
                username='officer',
                email='officer@example.edu.ng',
                password=password,
                role=User.Role.PROCUREMENT_OFFICER,
            )
            created_any = True
            self.stdout.write(self.style.WARNING(
                f'Created local procurement_officer user — username: officer  password: {password}\n'
                'This password is shown only once, here in this command output — save it now. '
                'It was randomly generated per-environment, so it is safe to seed on a real '
                'deployment, but you should still rotate it once you have real staff accounts.'
            ))

        if not created_any:
            self.stdout.write('Seed users already exist — nothing to do.')
