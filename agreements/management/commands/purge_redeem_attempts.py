from django.core.management.base import BaseCommand

from agreements.services import purge_old_attempts


class Command(BaseCommand):
    help = "Deletes failed agreement-code redeem attempts older than 24 hours (also done as new ones come in)."

    def handle(self, **options):
        self.stdout.write(f"Intentos borrados: {purge_old_attempts()}")
