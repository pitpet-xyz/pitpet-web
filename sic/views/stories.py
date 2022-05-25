from html.parser import HTMLParser
from datetime import datetime
import hashlib
import urllib.request
from django.db import transaction
from django.shortcuts import render, redirect
from django.urls import reverse
from django.contrib import messages
from django.views.decorators.http import require_safe
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import PermissionDenied
from django.utils.timezone import make_aware
from django.http import Http404, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.apps import apps

config = apps.get_app_config("sic")
from sic.models import Story, StoryKind, Comment, Notification, Tag
from sic.forms import (
    SubmitCommentForm,
    SubmitStoryForm,
    EditStoryForm,
    OrderByForm,
)
from sic.markdown import comment_to_html
from sic.views.utils import (
    form_errors_as_string,
    Paginator,
    InvalidPage,
    check_safe_url,
    check_next_url,
)
from sic.moderation import ModerationLogEntry


def story(request, story_pk, slug=None):
    try:
        story_obj = Story.objects.get(pk=story_pk)
    except Story.DoesNotExist:
        raise Http404("Story does not exist") from Story.DoesNotExist
    media = None
    if story_obj.media_sha256 is not None:
        from sic.s3 import BucketObject

        obj = BucketObject.from_sha256(story_obj.media_sha256)
        if obj:
            media = obj.url()

    ongoing_reply_pk = None
    try:
        comment_pk = next(iter(request.session["comment_preview"].keys()))
        comment_pk = comment_pk if comment_pk == "null" else int(comment_pk)
        ongoing_reply_pk = comment_pk
    except (StopIteration, KeyError, ValueError):
        pass
    if request.method == "POST":
        form = SubmitCommentForm(request.POST)
        if not request.user.is_authenticated:
            messages.add_message(
                request, messages.ERROR, "You must be logged in to comment."
            )
        elif not request.user.has_perm("sic.add_comment"):
            if request.user.banned_by_user is not None:
                messages.add_message(
                    request,
                    messages.ERROR,
                    "You are banned and not allowed to comment.",
                )
            else:
                messages.add_message(
                    request, messages.ERROR, "You are not allowed to comment."
                )
        else:
            if form.is_valid():
                comment = Comment.objects.create(
                    user=request.user,
                    story=story_obj,
                    parent=None,
                    text=form.cleaned_data["text"],
                )
                request.session["comment_preview"] = {}
                return redirect(comment)
            error = form_errors_as_string(form.errors)
            messages.add_message(
                request, messages.ERROR, f"Invalid comment form. Error: {error}"
            )
    else:
        if slug != story_obj.slugify:
            return redirect(story_obj.get_absolute_url())
        form = SubmitCommentForm()
    comments = story_obj.active_comments.prefetch_related("user", "votes")
    return render(
        request,
        "posts/story.html",
        {
            "story": story_obj,
            "media": media,
            "comment_form": form,
            "comments": comments,
            "ongoing_reply_pk": ongoing_reply_pk,
        },
    )


def all_stories_tmpl(request, view_name, json_response, page_num=1):
    if "order_by" in request.GET:
        request.session["all_stories_order_by"] = request.GET["order_by"]
    if "ordering" in request.GET:
        request.session["all_stories_ordering"] = request.GET["ordering"]

    if page_num == 1 and request.get_full_path() != reverse(view_name):
        return redirect(reverse(view_name))

    order_by = request.session.get("all_stories_order_by", "hotness")
    ordering = request.session.get("all_stories_ordering", "desc")
    order_by_field = ("-" if ordering == "desc" else "") + order_by

    story_obj = Story.objects.filter(active=True).prefetch_related(
        "tags", "user", "comments"
    )
    if order_by == "hotness":
        stories = sorted(
            story_obj.order_by("created", "title"),
            key=lambda s: s.hotness,
            reverse=ordering == "desc",
        )
    elif order_by == "last commented":
        stories = sorted(
            story_obj.order_by("created", "title"),
            key=lambda s: s.active_comments.latest("created").created
            if s.active_comments.exists()
            else s.created,
            reverse=ordering == "desc",
        )
    else:
        stories = list(story_obj.order_by(order_by_field, "title"))
    now = make_aware(datetime.now())
    unix_epoch = make_aware(datetime.fromtimestamp(0))
    pinned = list(
        filter(
            lambda s: s.pinned and (s.pinned >= now or s.pinned == unix_epoch), stories
        )
    )
    if pinned:
        for p in pinned:
            stories.remove(p)
        pinned.reverse()
        for p in pinned:
            p.pinned_status = True
            stories.insert(0, p)

    paginator = Paginator(stories, config.STORIES_PER_PAGE)
    try:
        page = paginator.page(page_num)
    except InvalidPage:
        # page_num is bigger than the actual number of pages
        return redirect(
            reverse(f"{view_name}_page", kwargs={"page_num": paginator.num_pages})
        )
    order_by_form = OrderByForm(
        fields=all_stories.ORDER_BY_FIELDS,
        initial={"order_by": order_by, "ordering": ordering},
    )
    if json_response:
        return JsonResponse(
            {
                "stories": [s.to_json_dict() for s in page],
                "page_num": page_num,
                "pages": paginator.num_pages,
                "next_page": None
                if page_num == paginator.num_pages
                else reverse(f"{view_name}_page", kwargs={"page_num": page_num + 1}),
            }
        )

    return render(
        request,
        "posts/all_stories.html",
        {
            "stories": page,
            "order_by_form": order_by_form,
            "pages": paginator.get_elided_page_range(number=page_num),
        },
    )


def all_stories(request, page_num=1):
    return all_stories_tmpl(request, "all_stories", False, page_num)


all_stories.ORDER_BY_FIELDS = ["hotness", "created", "last commented"]


def all_stories_json(request, page_num=1):
    return all_stories_tmpl(request, "all_stories_json", True, page_num)


all_stories_json.ORDER_BY_FIELDS = all_stories.ORDER_BY_FIELDS


@login_required
@permission_required("sic.add_story", raise_exception=True)
@transaction.atomic
def submit_story(request):
    user = request.user
    preview = None
    if request.method == "POST":
        if "preview" in request.POST:
            form = SubmitStoryForm(request.POST, request.FILES)
            form.is_valid()
            preview = {
                "content": comment_to_html(request.POST["content"]),
                "title": form.cleaned_data["title"],
                "publish_date": form.cleaned_data["publish_date"],
                "tags": form.cleaned_data["tags"],
            }
        else:
            form = SubmitStoryForm(request.POST, request.FILES)
            form.fields["title"].required = True
            if form.is_valid():
                if "media" in request.FILES and request.FILES["media"]:
                    f = request.FILES["media"]
                    from sic.s3 import upload_media

                    media_obj = upload_media(f)
                    media_sha256 = media_obj.hexdigest
                else:
                    media_sha256 = None
                title = form.cleaned_data["title"]
                content = form.cleaned_data["content"]
                publish_date = form.cleaned_data["publish_date"]
                user_is_author = form.cleaned_data["user_is_author"]
                user_is_author = True

                new_story = Story.objects.create(
                    media_sha256=media_sha256,
                    title=title,
                    publish_date=publish_date,
                    content=content,
                    user=user,
                    user_is_author=user_is_author,
                    content_warning=form.cleaned_data["content_warning"],
                )
                new_story.tags.set(form.cleaned_data["tags"])
                new_story.kind.set(form.cleaned_data["kind"])
                new_story.save()
                return redirect(new_story.get_absolute_url())
            form.fields["title"].required = False
            error = form_errors_as_string(form.errors)
            messages.add_message(
                request, messages.ERROR, f"Invalid form. Error: {error}"
            )
    else:
        form = SubmitStoryForm(initial={"kind": StoryKind.default_value()})
    return render(
        request,
        "posts/submit.html",
        {
            "form": form,
            "preview": preview,
            "tags": {t.name: t.hex_color for t in form.fields["tags"].queryset},
            "kinds": {k.name: k.hex_color for k in form.fields["kind"].queryset},
        },
    )


@login_required
@transaction.atomic
def upvote_story(request, story_pk):
    if request.method == "POST":
        if not config.ENABLE_KARMA:
            return HttpResponseBadRequest("Karma is disabled.")
        user = request.user
        if not request.user.email_validated:
            messages.add_message(
                request,
                messages.ERROR,
                "You must validate your email address before being able to use the website.",
            )
        else:
            try:
                story_obj = Story.objects.get(pk=story_pk)
            except Story.DoesNotExist:
                raise Http404("Story does not exist") from Story.DoesNotExist
            if story_obj.user.pk == user.pk:
                messages.add_message(
                    request, messages.ERROR, "You cannot vote on your own posts."
                )
            else:
                vote, created = user.votes.get_or_create(
                    story=story_obj, comment=None, user=user
                )
                if not created:
                    vote.delete()
    if "next" in request.GET and check_next_url(request.GET["next"]):
        return redirect(request.GET["next"])
    return redirect(reverse("index"))


@login_required
@permission_required("sic.change_story", raise_exception=True)
@transaction.atomic
def edit_story(request, story_pk, slug=None):
    user = request.user
    preview = None
    try:
        story_obj = Story.objects.get(pk=story_pk)
    except Story.DoesNotExist:
        raise Http404("Story does not exist") from Story.DoesNotExist
    if not request.user.has_perm("sic.change_story", story_obj):
        raise PermissionDenied("Only the author of the story can edit it.")
    if request.method == "POST":
        if "preview" in request.POST:
            form = EditStoryForm(request.POST)
            form.is_valid()
            preview = {
                "content": comment_to_html(request.POST["content"]),
                "title": form.cleaned_data["title"],
                "publish_date": form.cleaned_data["publish_date"],
                "tags": form.cleaned_data["tags"],
            }
        else:
            form = EditStoryForm(request.POST)
            if form.is_valid():
                title_before = story_obj.title
                cont_before = story_obj.content
                cw_before = story_obj.content_warning
                pubdate_before = story_obj.publish_date
                tags_before = list(story_obj.tags.all())
                kinds_before = list(story_obj.kind.all())

                story_obj.title = form.cleaned_data["title"]
                story_obj.content = form.cleaned_data["content"]
                story_obj.user_is_author = form.cleaned_data["user_is_author"]
                story_obj.tags.set(form.cleaned_data["tags"])
                story_obj.kind.set(form.cleaned_data["kind"])
                story_obj.publish_date = form.cleaned_data["publish_date"]
                story_obj.content_warning = form.cleaned_data["content_warning"]

                if title_before != form.cleaned_data["title"]:
                    ModerationLogEntry.edit_story_title(
                        title_before, story_obj, user, form.cleaned_data["reason"]
                    )
                if cont_before != form.cleaned_data["content"]:
                    ModerationLogEntry.edit_story_desc(
                        cont_before, story_obj, user, form.cleaned_data["reason"]
                    )
                if cw_before != form.cleaned_data["content_warning"]:
                    ModerationLogEntry.edit_story_cw(
                        cw_before, story_obj, user, form.cleaned_data["reason"]
                    )
                if pubdate_before != form.cleaned_data["publish_date"]:
                    ModerationLogEntry.edit_story_pubdate(
                        pubdate_before, story_obj, user, form.cleaned_data["reason"]
                    )
                if tags_before != list(form.cleaned_data["tags"]):
                    ModerationLogEntry.edit_story_tags(
                        tags_before, story_obj, user, form.cleaned_data["reason"]
                    )
                if kinds_before != list(form.cleaned_data["kind"]):
                    ModerationLogEntry.edit_story_kind(
                        kinds_before, story_obj, user, form.cleaned_data["reason"]
                    )

                story_obj.save()
                return redirect(story_obj.get_absolute_url())
            error = form_errors_as_string(form.errors)
            messages.add_message(
                request, messages.ERROR, f"Invalid form. Error: {error}"
            )
    else:
        form = EditStoryForm(
            initial={
                "title": story_obj.title,
                "content": story_obj.content,
                "publish_date": story_obj.publish_date,
                "user_is_author": story_obj.user_is_author,
                "tags": story_obj.tags.all(),
                "kind": story_obj.kind.all(),
                "content_warning": story_obj.content_warning,
            }
        )
    return render(
        request,
        "posts/submit.html",
        {
            "form": form,
            "preview": preview,
            "story": story_obj,
            "tags": {t.name: t.hex_color for t in form.fields["tags"].queryset},
            "kinds": {k.name: k.hex_color for k in form.fields["kind"].queryset},
        },
    )


class TitleHTMLExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = ""
        self.ogtitle = None
        self.publish_date = None
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self.in_title = True
        elif tag == "meta":
            attrs = {a[0]: a[1] for a in attrs}
            if (
                "content" in attrs
                and "property" in attrs
                and attrs["property"] == "article:published_time"
            ):
                try:
                    if attrs["content"].endswith("Z"):
                        attrs["content"] = attrs["content"][:-1]
                    self.publish_date = datetime.fromisoformat(attrs["content"]).date()
                except:
                    pass
            if (
                "content" in attrs
                and "property" in attrs
                and attrs["property"] == "og:title"
            ):
                self.ogtitle = attrs["content"]

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.title += data
