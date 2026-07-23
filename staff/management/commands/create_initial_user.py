from django.core.management.base import BaseCommand
from django.contrib.auth.models import User


class Command(BaseCommand):
    help = 'Creates the initial staff user (tanit/tanit)'

    def handle(self, *args, **options):
        username = 'tanit'
        password = 'tanit'
        email = 'tanit@tanitech.de'

        if User.objects.filter(username=username).exists():
            self.stdout.write(self.style.WARNING(f'User "{username}" already exists.'))
        else:
            user = User.objects.create_user(
                username=username,
                email=email,
                password=password,
                is_staff=True,
                is_superuser=True
            )
            self.stdout.write(self.style.SUCCESS(f'Successfully created user "{username}" with staff/superuser access.'))

