from functools import lru_cache
import datetime
import typing
import threading
from email.utils import make_msgid
from django.utils.functional import cached_property
from django.apps import AppConfig
from django.conf import settings
from django.urls import reverse_lazy
from django.utils.safestring import mark_safe


class SicAppConfig(AppConfig):
    name = "sic"  # python path
    label = "sic"  # python identifier
    verbose_name = "PitPet"  # full human readable name

    S3_BUCKET = "pitpet-object-bucket"
    API_ENDPOINT = (
        "https://awolro67m3kvcwjrbax67toaj40cllpj.lambda-url.eu-central-1.on.aws/"
    )

    subtitle = "is a community about pets and their lifetimes."

    THEME_COLOR_HEX = "#1e82be"
    DARK_THEME_COLOR_HEX = "#15557b"

    BANNED_USERNAMES = [
        "admin",
        "administrator",
        "contact",
        "fraud",
        "guest",
        "help",
        "hostmaster",
        "sic",
        "Cher",
        "mailer-daemon",
        "moderator",
        "moderators",
        "nobody",
        "postmaster",
        "root",
        "security",
        "support",
        "sysop",
        "webmaster",
        "enable",
        "new",
        "signup",
    ]

    # days old accounts are considered new for
    NEW_USER_DAYS = 70

    # minimum karma required to be able to add/change Tag objects
    MIN_KARMA_TO_EDIT_TAGS = 5

    # minimum karma required to be able to offer title/tag suggestions
    MIN_KARMA_TO_SUGGEST = 10

    # minimum karma required to be able to flag comments
    MIN_KARMA_TO_FLAG = 50

    # minimum karma required to be able to submit new stories
    MIN_KARMA_TO_SUBMIT_STORIES = -4

    # minimum karma required to process invitation requests
    MIN_KARMA_FOR_INVITATION_REQUESTS = MIN_KARMA_TO_FLAG

    # proportion of posts authored by user to consider as heavy self promoter
    HEAVY_SELF_PROMOTER_PROPORTION = 0.51

    # minimum number of submitted stories before checking self promotion
    MIN_STORIES_CHECK_SELF_PROMOTION = 2

    WEB_PROTOCOL = "http"  # Used when generating URLs, replace with "https" if needed

    DEFAULT_FROM_EMAIL = settings.DEFAULT_FROM_EMAIL
    DIGEST_SUBJECT = "[pitpet] digest for"
    INVITATION_SUBJECT = "Your invitation to pitpet"
    INVITATION_BODY = "Visit the following url to complete your registration:"
    INVITATION_FROM = DEFAULT_FROM_EMAIL
    NOTIFICATION_FROM = DEFAULT_FROM_EMAIL

    MAILING_LIST_ID = verbose_name
    MAILING_LIST_ADDRESS = None  # If None, will be MAILING_LIST_ID@config.get_domain()
    MAILING_LIST_FROM = (
        None  # If None, poster's username@domain.tld will be used as From address
    )

    STORIES_PER_PAGE = 20

    FTS_DATABASE_NAME = "fts"
    FTS_DATABASE_FILENAME = "fts.db"
    FTS_COMMENTS_TABLE_NAME = "fts5_comments"
    FTS_STORIES_TABLE_NAME = "fts5_stories"

    MENTION_TOKENIZER_NAME = "mention_tokenizer"

    SEND_WEBMENTIONS = False

    FORMAT_QUOTED_MESSAGES = True
    DETECT_USERNAME_MENTIONS_IN_COMMENTS = False
    MAILING_LIST = False

    SHOW_GIT_REPOSITORY_IN_ABOUT_PAGE = False
    SHOW_GIT_COMMIT_IN_FOOTER = False

    ALLOW_INVITATION_REQUESTS = False

    ALLOW_REGISTRATIONS = True

    REQUIRE_VOUCH_FOR_PARTICIPATION = False

    ENABLE_SSH_OTP_LOGIN = False

    DISALLOW_REPOSTS_PERIOD: typing.Optional[datetime.timedelta] = datetime.timedelta(
        weeks=1
    )

    ENABLE_KARMA = True

    VISIBLE_KARMA = False

    ENABLE_FETCHING_REMOTE_CONTENT = False

    ENABLE_URL_POSTING = True

    ACCEPTED_URI_SCHEMES: typing.List[str] = [
        "dat",
        "finger",
        "gemini",
        "gopher",
        "irc",
        "ircs",
        "jabber",
        "magnet",
        "matrix",
        "news",
        "nntp",
        "snews",
        "telnet",
        "xmpp",
        "ftp",
        "ftps",
        "http",
        "https",
    ]

    MODEL_VERBOSE_NAMES: typing.Dict[str, str] = {
        #        "story": ("thread", "threads"),
        #        "comment": ("reply", "replies"),
        #       "taggregation": ("topic", "topics"),
    }

    @lru_cache(maxsize=None)
    def model_verbose_names(self, model_name: str, plural: bool) -> str:
        if model_name in self.MODEL_VERBOSE_NAMES:
            return self.MODEL_VERBOSE_NAMES[model_name][1 if plural else 0]
        model = self.models[model_name]
        return model._meta.verbose_name_plural if plural else model._meta.verbose_name

    @cached_property
    def post_ranking(self) -> "sic.voting.PostRanking":
        # from sic.voting import TemporalRanking
        # return TemporalRanking()
        from sic.voting import KarmaRanking

        return KarmaRanking()

    @property
    def html_label(self):
        """Override this to change HTML label used in static html"""
        return mark_safe("<strong><code>PitPet</code></strong>")

    @property
    def html_subtitle(self):
        """Override this to change HTML subtitle used in static html"""
        return mark_safe("is a community about pets and stories")

    @property
    def html_signup_request_info(self):
        ret = f"""You will need an invitation from an existing user to join. You can ask someone you know, or on <a href="{reverse_lazy('about')}">IRC</a>"""
        if SicAppConfig.ALLOW_INVITATION_REQUESTS:
            ret += " or submit an invitation request here. Members of the community can review your request and send you an invite."
        else:
            ret += "."
        return mark_safe(ret)

    def ready(self):
        import sic.notifications
        import sic.mail
        import sic.jobs
        import sic.flatpages
        import sic.s3
        from sic.s3 import Session

        def sched_jobs():
            from sic.jobs import Job
            import sched
            import time

            def exec_fn():
                for job in Job.objects.filter(active=True, failed=False):
                    job.run()

            s = sched.scheduler(time.time, time.sleep)
            while True:
                s.enter(15 * 60, 1, exec_fn)
                s.run(blocking=True)

        self.scheduling_thread = threading.Thread(target=sched_jobs, daemon=True)
        self.scheduling_thread.name = "scheduling_thread"
        self.scheduling_thread.start()
        self.aws_session = Session()
        print(f"aws_session = {self.aws_session}")

    @staticmethod
    @lru_cache(maxsize=None)
    def get_domain():
        from .models import Site

        return Site.objects.get_current().domain

    @staticmethod
    def make_msgid():
        domain = SicAppConfig.get_domain()
        return make_msgid(domain=domain)

    @staticmethod
    @lru_cache(maxsize=None)
    def mailing_list_address() -> str:
        if SicAppConfig.MAILING_LIST_ADDRESS:
            return SicAppConfig.MAILING_LIST_ADDRESS
        return f"{SicAppConfig.MAILING_LIST_ID}@{SicAppConfig.get_domain()}"
