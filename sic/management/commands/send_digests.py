from django.core.management.base import BaseCommand
from django.apps import apps

config = apps.get_app_config("sic")


class Command(BaseCommand):
    help = "Send digest mails"

    def handle(self, *args, **kwargs):
        pass
