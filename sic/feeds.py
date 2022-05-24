from datetime import date
from django.core.cache import cache
from django.contrib.sites.shortcuts import get_current_site
from django.contrib.syndication.views import Feed
import django.contrib.syndication.views as django_contrib_syndication_views
from django.http import Http404
from django.core.exceptions import PermissionDenied
from django.utils.encoding import iri_to_uri
from django.utils.feedgenerator import Atom1Feed, Rss201rev2Feed
from django.views.decorators.http import require_http_methods
from django.apps import apps
from .models import Story, User
from .auth import AuthToken

config = apps.get_app_config("sic")

# Edit site domain in /admin/sites/site/1/change


# https://github.com/django/django/blob/fbb1984046ae00bdf0b894a6b63294395da1cce8/django/contrib/syndication/views.py#L13
def add_domain(domain, url, secure=False):
    protocol = "https" if secure else "http"
    if url.startswith("//"):
        # Support network-path reference (see #16753) - RSS requires a protocol
        url = "%s:%s" % (protocol, url)
    elif not url.startswith(("mailto:",)) and not url.startswith(
        tuple(f"{scheme}://" for scheme in config.ACCEPTED_URI_SCHEMES)
    ):
        url = iri_to_uri("%s://%s%s" % (protocol, domain, url))
    return url


# Patch django's add_domain to allow gemini etc URI schemes in submitted links
django_contrib_syndication_views.add_domain = add_domain


class RssFeed(Rss201rev2Feed):
    def add_item(self, *args, **kwargs):
        if "_comments" in kwargs:
            kwargs["comments"] = kwargs["_comments"]
        return super().add_item(*args, **kwargs)


class AtomFeed(Atom1Feed):
    def add_item(self, *args, **kwargs):
        if "_comments" in kwargs:
            kwargs["comments"] = kwargs["_comments"]
        return super().add_item(*args, **kwargs)


class LatestStories(Feed):
    title = "sic latest stories"
    link = "/"
    description = ""

    def __init__(self, *args, **kwargs):
        self.request = None
        super().__init__(*args, **kwargs)

    def items(self):
        latest = cache.get("latest_stories_latest")
        try:
            actual_latest = (
                Story.objects.exclude(active=False).latest("created").created
            )
        except Story.DoesNotExist:
            actual_latest = date.fromtimestamp(0)
        items = cache.get("latest_stories")
        if items is None or (latest is not None and latest != actual_latest):
            items = Story.objects.exclude(active=False).order_by("-created")[:10]
            cache.set("latest_stories", items)
            cache.set("latest_stories_latest", actual_latest)
        return items

    def item_title(self, item):
        return item.title

    def item_description(self, item):
        return item.content_to_html

    def item_author_name(self, item):
        return str(item.user)

    def item_pubdate(self, item):
        return item.created

    def item_categories(self, item):
        return map(lambda t: str(t), item.tags.all())

    def item_link(self, item):
        return item.get_listing_url

    def get_context_data(self, **kwargs):
        # Bit of a hack, get_context_data() is called by the Feed view in
        # django/contrib/syndication/views.py so store the request object in
        # order to retrieve the domain from it for the comments url in
        # item_extra_kwargs()
        if "request" in kwargs:
            self.request = kwargs.get("request")
        return super().get_context_data(**kwargs)

    def item_extra_kwargs(self, item):
        link = self.item_link(item)
        url = item.get_absolute_url()
        if link != url:
            if self.request is not None:
                current_site = get_current_site(self.request)
                return {
                    "_comments": add_domain(
                        current_site.domain,
                        url,
                        self.request.is_secure(),
                    )
                }
            return {
                "_comments": url,
            }
        else:
            return {}


class LatestStoriesRss(LatestStories):
    feed_type = RssFeed
    subtitle = LatestStories.description


class LatestStoriesAtom(LatestStories):
    feed_type = AtomFeed
    subtitle = LatestStories.description


class UserLatestStoriesFeed(Feed):
    def __init__(self, user, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.latest_key = f"latest_stories_latest_{self.user.pk}"
        self.cache_key = f"latest_stories_{self.user.pk}"

    def items(self):
        latest = cache.get(self.latest_key)
        try:
            actual_latest = self.user.frontpage()["stories"].latest("created").created
        except Story.DoesNotExist:
            actual_latest = date.fromtimestamp(0)
        items = cache.get(self.cache_key)
        if items is None or (latest is not None and latest != actual_latest):
            items = Story.objects.exclude(active=False).order_by("-created")[:10]
            cache.set(self.cache_key, items)
            cache.set(self.latest_key, actual_latest)
        return items

    def __call__(self, request, *args, **kwargs):
        if "token" in request.GET:
            token = request.GET["token"]
            if AuthToken().check_token(self.user, token):
                return super().__call__(request, *args, **kwargs)
        raise PermissionDenied("Forbidden.")


class UserLatestStoriesRss(UserLatestStoriesFeed, LatestStoriesRss):
    pass


class UserLatestStoriesAtom(UserLatestStoriesFeed, LatestStoriesAtom):
    pass


@require_http_methods(["GET"])
def user_feeds_rss(request, username):
    try:
        user = User.objects.get(username=username)
    except User.DoesNotExist:
        try:
            user = User.objects.get(pk=int(username))
        except:
            raise Http404("User does not exist") from User.DoesNotExist
    return UserLatestStoriesRss(user)(request)


@require_http_methods(["GET"])
def user_feeds_atom(request, username):
    try:
        user = User.objects.get(username=username)
    except User.DoesNotExist:
        try:
            user = User.objects.get(pk=int(username))
        except:
            raise Http404("User does not exist") from User.DoesNotExist
    return UserLatestStoriesAtom(user)(request)
