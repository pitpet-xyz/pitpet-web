from datetime import datetime
import random
import re
from django.db import transaction, connection, IntegrityError
from django.db.models.functions import Lower
from django.http import HttpResponse, Http404
from django.core.exceptions import PermissionDenied
from django.views.decorators.http import require_http_methods
from django.shortcuts import render, redirect
from django.urls import reverse
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.utils.timezone import make_aware
from django.utils.http import urlencode
from django.views.decorators.cache import cache_page
from django.core.cache import cache
from django.views.decorators.clickjacking import xframe_options_exempt
from django.apps import apps

config = apps.get_app_config("sic")

from sic.models import Tag, Taggregation, TaggregationHasTag
from sic.forms import (
    NewTagForm,
    EditTagForm,
    EditTaggregationForm,
    OrderByForm,
    EditTaggregationHasTagForm,
    DeleteTaggregationHasTagForm,
)
from sic.views.utils import (
    form_errors_as_string,
    Paginator,
    InvalidPage,
    check_next_url,
)
from sic.moderation import ModerationLogEntry


def browse_tags(request, page_num=1):
    if "order_by" in request.GET:
        request.session["tag_order_by"] = request.GET["order_by"]
    if "ordering" in request.GET:
        request.session["tag_ordering"] = request.GET["ordering"]
    order_by = request.session.get("tag_order_by", "created")
    ordering = request.session.get("tag_ordering", "desc")
    order_by_field = ("-" if ordering == "desc" else "") + order_by

    if page_num == 1 and request.get_full_path() != reverse("browse_tags"):
        return redirect(reverse("browse_tags"))
    if order_by == "name":
        tags = Tag.objects.order_by(
            Lower("name").asc() if ordering == "asc" else Lower("name").desc()
        )
    elif order_by == "created":
        tags = Tag.objects.order_by(order_by_field, "name")
    elif order_by == "active":
        tags = sorted(
            Tag.objects.all(),
            key=lambda t: t.latest.created
            if t.latest
            else make_aware(datetime.fromtimestamp(0)),
            reverse=ordering == "desc",
        )
    else:
        tags = sorted(
            Tag.objects.all(),
            key=lambda t: t.stories_count(),
            reverse=ordering == "desc",
        )
    paginator = Paginator(tags, 250)
    try:
        page = paginator.page(page_num)
    except InvalidPage:
        # page_num is bigger than the actual number of pages
        return redirect(
            reverse(
                "browse_tags_page",
                kwargs={"page_num": paginator.num_pages},
            )
        )
    order_by_form = OrderByForm(
        fields=browse_tags.ORDER_BY_FIELDS,
        initial={"order_by": order_by, "ordering": ordering},
    )
    return render(
        request,
        "tags/browse_tags.html",
        {
            "tags": page,
            "order_by_form": order_by_form,
            "pages": paginator.get_elided_page_range(number=page_num),
        },
    )


browse_tags.ORDER_BY_FIELDS = ["name", "created", "active", "number of posts"]


@login_required
@permission_required("sic.change_tag", raise_exception=True)
@transaction.atomic
def edit_tag(request, tag_pk, slug=None):
    try:
        tag = Tag.objects.get(pk=tag_pk)
    except Tag.DoesNotExist:
        raise Http404("Tag does not exist") from Tag.DoesNotExist
    if slug != tag.slugify:
        return redirect(tag.get_absolute_url())
    if not request.user.has_perm("sic.change_tag", tag):
        raise PermissionDenied("You don't have permissions to change this tag.")
    if request.method == "POST":
        form = EditTagForm(request.POST)
        if form.is_valid():
            name_before = tag.name
            parents_before = list(tag.parents.all())

            err = None
            try:
                with transaction.atomic():
                    tag.parents.set(form.cleaned_data["parents"])
            except IntegrityError as exc:
                err = exc
                with connection.cursor() as cursor:
                    path_strs = []
                    for p in form.cleaned_data["parents"]:
                        cursor.execute(
                            f"SELECT already_visited FROM cycle_check_view WHERE last_visited = {p.pk} AND already_visited LIKE '%{tag.pk}%';",
                            [],
                        )
                        path = cursor.fetchone()
                        if path:
                            path = path[0]
                            if isinstance(path, str):
                                path = [p.pk] + list(map(int, path.split(","))) + [p.pk]
                            else:
                                path = [p.pk, path, p.pk]
                            path_strs.append(
                                "‘"
                                + "’ → ‘".join(
                                    map(lambda pk: Tag.objects.get(pk=pk).name, path)
                                )
                                + "’"
                            )
                form.add_error("parents", f"{exc} {','.join(path_strs)}")

            if err is None:
                tag.name = form.cleaned_data["name"]
                tag.hex_color = form.cleaned_data["hex_color"]

                if name_before != tag.name:
                    ModerationLogEntry.edit_tag_name(
                        name_before, tag, request.user, form.cleaned_data["reason"]
                    )
                if parents_before != list(form.cleaned_data["parents"]):
                    ModerationLogEntry.edit_tag_parents(
                        parents_before, tag, request.user, form.cleaned_data["reason"]
                    )

                tag.save()
                if "next" in request.GET and check_next_url(request.GET["next"]):
                    return redirect(request.GET["next"])
                return redirect(reverse("browse_tags"))
        error = form_errors_as_string(form.errors)
        messages.add_message(request, messages.ERROR, f"Invalid form. Error: {error}")
    else:
        form = EditTagForm(
            initial={
                "pk": tag,
                "name": tag.name,
                "hex_color": tag.hex_color,
                "parents": tag.parents.all(),
            }
        )
    # colors = list(gen_html(mix=[198, 31, 31]))
    colors = list(gen_html())
    form.fields["parents"].queryset = Tag.objects.exclude(pk=tag_pk)
    return render(
        request,
        "tags/edit_tag.html",
        {
            "tag": tag,
            "form": form,
            "colors": colors,
            "parents": {p.name: p.hex_color for p in form.fields["parents"].queryset},
        },
    )


@login_required
@permission_required("sic.add_tag", raise_exception=True)
@transaction.atomic
def add_tag(request):
    if not request.user.has_perm("sic.add_tag"):
        raise PermissionDenied("You don't have permissions to add tags.")
    colors = list(gen_html())
    if request.method == "POST":
        form = NewTagForm(request.POST)
        if form.is_valid():
            new = Tag.objects.create(
                name=form.cleaned_data["name"],
                hex_color=form.cleaned_data["hex_color"],
            )
            new.parents.set(form.cleaned_data["parents"])
            new.save()
            ModerationLogEntry.create_tag(new, request.user)
            messages.add_message(
                request, messages.SUCCESS, f"You have created a tag: {new.name}."
            )
            if "next" in request.GET and check_next_url(request.GET["next"]):
                return redirect(request.GET["next"])
            return redirect(reverse("browse_tags"))
        error = form_errors_as_string(form.errors)
        messages.add_message(request, messages.ERROR, f"Invalid form. Error: {error}")
    else:
        form = NewTagForm(initial={"hex_color": colors[0]})
    return render(
        request,
        "tags/edit_tag.html",
        {
            "form": form,
            "colors": colors,
            "parents": {p.name: p.hex_color for p in form.fields["parents"].queryset},
        },
    )


# HSV values in [0..1[
# returns [r, g, b] values from 0 to 255
def hsv_to_rgb(h, s, v):
    h_i = int(h * 6)
    f = h * 6 - h_i
    p = v * (1 - s)
    q = v * (1 - f * s)
    t = v * (1 - (1 - f) * s)
    if h_i == 0:
        r, g, b = v, t, p
    if h_i == 1:
        r, g, b = q, v, p
    if h_i == 2:
        r, g, b = p, v, t
    if h_i == 3:
        r, g, b = p, q, v
    if h_i == 4:
        r, g, b = t, p, v
    if h_i == 5:
        r, g, b = v, p, q
    return [int(r * 256), int(g * 256), int(b * 256)]


def gen_html(mix=None):
    # use golden ratio
    golden_ratio_conjugate = 0.618033988749895
    for _ in range(0, 50):
        h = random.random()
        h += golden_ratio_conjugate
        h %= 1
        [r, g, b] = hsv_to_rgb(h, 0.5, 0.95)
        if mix:
            r = int((r + mix[0]) / 2)
            g = int((g + mix[1]) / 2)
            b = int((b + mix[2]) / 2)
        yield "#%02x%02x%02x" % (r, g, b)


def view_tag(request, tag_pk, slug=None, page_num=1):
    try:
        obj = Tag.objects.get(pk=tag_pk)
    except Tag.DoesNotExist:
        raise Http404("Tag does not exist") from Tag.DoesNotExist
    if "order_by" in request.GET:
        request.session["tag_order_by"] = request.GET["order_by"]
    if "ordering" in request.GET:
        request.session["tag_ordering"] = request.GET["ordering"]
    if page_num == 1 and request.get_full_path() != reverse(
        "view_tag", kwargs={"tag_pk": tag_pk, "slug": slug}
    ):
        return redirect(reverse("view_tag", kwargs={"tag_pk": tag_pk, "slug": slug}))
    if slug != obj.slugify:
        return redirect(
            reverse(
                "view_tag_page",
                kwargs={"tag_pk": tag_pk, "slug": obj.slugify, "page_num": page_num},
            )
        )
    order_by = request.session.get("tag_order_by", "created")
    ordering = request.session.get("tag_ordering", "desc")

    if order_by == "created":
        stories = list(obj.get_stories())
        stories = sorted(
            stories,
            key=lambda s: s.created,
            reverse=ordering == "desc",
        )
    elif order_by == "active":
        stories = list(obj.get_stories())
        stories = sorted(
            stories,
            key=lambda s: s.active_comments.latest("created").created
            if s.active_comments.exists()
            else make_aware(datetime.fromtimestamp(0)),
            reverse=ordering == "desc",
        )

    elif order_by == "number of comments":
        stories = list(obj.get_stories())
        stories = sorted(
            stories,
            key=lambda s: s.active_comments.count(),
            reverse=ordering == "desc",
        )
    else:
        stories = list(obj.get_stories())

    paginator = Paginator(stories, config.STORIES_PER_PAGE)
    try:
        page = paginator.page(page_num)
    except InvalidPage:
        # page_num is bigger than the actual number of pages
        return redirect(
            reverse(
                "view_tag_page",
                kwargs={
                    "tag_pk": tag_pk,
                    "slug": obj.slugify,
                    "page_num": paginator.num_pages,
                },
            )
        )
    order_by_form = OrderByForm(
        fields=view_tag.ORDER_BY_FIELDS,
        initial={"order_by": order_by, "ordering": ordering},
    )
    return render(
        request,
        "posts/all_stories.html",
        {
            "stories": page,
            "order_by_form": order_by_form,
            "tag": obj,
            "pages": paginator.get_elided_page_range(number=page_num),
        },
    )


view_tag.ORDER_BY_FIELDS = ["created", "active", "number of comments"]
