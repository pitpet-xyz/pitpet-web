from datetime import datetime, timedelta
import logging
import email
from email.headerregistry import Address as AddressHeader
import re
import typing
from email.policy import default as email_policy
from django.db import models
from django.db.models import F, Q
from django.template import Context, Template
from django.core import mail
from django.utils.timezone import make_aware
from django.core.mail import EmailMessage
from django.urls import reverse
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.apps import apps

config = apps.get_app_config("sic")
from sic.models import Story, User, Comment, Tag, ExactTagFilter, DomainFilter
from sic.markdown import Textractor

logger = logging.getLogger("sic")


def test_bit(int_, offset):
    mask = 1 << offset
    return int_ & mask > 0


def set_bit(int_, offset):
    mask = 1 << offset
    return int_ | mask


def clear_bit(int_, offset):
    mask = ~(1 << offset)
    return int_ & mask
