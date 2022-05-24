import subprocess
import types
from datetime import datetime, timedelta
from pathlib import Path
import urllib.request
import enum
import logging
import shlex
from django.db import models
from django.utils.timezone import make_aware
from django.utils.module_loading import import_string
from sic.models import Story
from sic.search import index_story

BASE_DIR = Path(__file__).resolve().parent.parent


class JobKind(models.Model):
    id = models.AutoField(primary_key=True)
    dotted_path = models.TextField(null=False, blank=False, unique=True)
    created = models.DateTimeField(auto_now_add=True, null=False, blank=False)
    last_modified = models.DateTimeField(auto_now_add=True, null=False, blank=False)

    def __str__(self):
        return self.dotted_path

    @staticmethod
    def from_func(func):
        if isinstance(func, types.FunctionType):
            dotted_path = f"{func.__module__}.{func.__name__}"
            ret, _ = JobKind.objects.get_or_create(dotted_path=dotted_path)
            return ret
        else:
            raise TypeError

    def run(self, job):
        logging.info("jobkind run")
        try:
            func = import_string(self.dotted_path)
            return func(job)
        except ImportError:
            logging.error(f"Could not resolve job dotted_path: {self.dotted_path}")
            raise ImportError


class Job(models.Model):
    id = models.AutoField(primary_key=True)
    kind = models.ForeignKey(JobKind, null=True, on_delete=models.SET_NULL)
    created = models.DateTimeField(auto_now_add=True)
    active = models.BooleanField(default=True, null=False, blank=False)
    periodic = models.BooleanField(default=False, null=False, blank=False)
    failed = models.BooleanField(default=False, null=False, blank=False)
    last_run = models.DateTimeField(default=None, null=True, blank=True)
    logs = models.TextField(null=True, blank=True)
    data = models.JSONField(null=True, blank=True)

    def __str__(self):
        return f"{self.kind} {self.data}"

    def run(self):
        if not self.kind_id:
            return
        self.last_run = make_aware(datetime.now())
        try:
            res = self.kind.run(self)
            if res and not self.periodic:
                self.active = False
            if isinstance(res, str):
                if self.logs is None:
                    self.logs = ""
                self.logs += res
            self.failed = False
            self.save(update_fields=["last_run", "failed", "active", "logs"])
        except Exception as exc:
            if self.logs is None:
                self.logs = ""
            self.logs += str(exc)
            self.failed = True
            self.save(update_fields=["last_run", "failed", "logs"])
        return
