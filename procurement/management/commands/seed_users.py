import secrets

from django.core.management.base import BaseCommand

from procurement.models import User

DEMO_USERS = [
    ('admin', 'admin@example.edu.ng', User.Role.ADMIN, True),
    ('officer', 'officer@example.edu.ng', User.Role.PROCUREMENT_UNIT, False),
    ('requester', 'requester@example.edu.ng', User.Role.REQUESTING_UNIT, False),
    ('finance', 'finance@example.edu.ng', User.Role.FINANCE, False),
    ('accounting_officer', 'accounting.officer@example.edu.ng', User.Role.ACCOUNTING_OFFICER, False),
    ('tenders_board', 'tenders.board@example.edu.ng', User.Role.TENDERS_BOARD, False),
]


class Command(BaseCommand):
    help = 'Seed one demo user per Phase 1-Foundation role, each with a freshly generated password.'

    def handle(self, *args, **options):
        created_any = False

        for username, email, role, is_superuser in DEMO_USERS:
            if User.objects.filter(username=username).exists():
                continue
            password = secrets.token_urlsafe(12)
            if is_superuser:
                User.objects.create_superuser(username=username, email=email, password=password, role=role)
            else:
                User.objects.create_user(username=username, email=email, password=password, role=role)
            created_any = True
            self.stdout.write(self.style.WARNING(
                f'Created local {role} user — username: {username}  password: {password}\n'
                'This password is shown only once, here in this command output — save it now. '
                'It was randomly generated per-environment, so it is safe to seed on a real '
                'deployment, but you should still rotate it once you have real staff accounts.'
            ))

        if not created_any:
            self.stdout.write('Seed users already exist — nothing to do.')
