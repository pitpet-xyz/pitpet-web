import re
import secrets
import html
import itertools
import json
from datetime import datetime, timedelta
from django.http import Http404, HttpResponse, JsonResponse, HttpResponseBadRequest
from django.core.exceptions import PermissionDenied
from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login as auth_login
from django.db import transaction
from django.db.models import Value, BooleanField
from django.urls import reverse
from django.contrib import messages
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.sites.models import Site
from django.utils.timezone import make_aware
from django.utils.safestring import mark_safe
from django.views.decorators.http import require_http_methods, require_safe
from django.core.mail import EmailMessage
from django.apps import apps

config = apps.get_app_config("sic")
from wand.image import Image
from sic.auth import AuthToken, SSHAuthenticationForm
from sic.models import (
    User,
    Invitation,
    Story,
    StoryBookmark,
    CommentBookmark,
    Notification,
    Hat,
    Message,
    InvitationRequest,
    ExactTagFilter,
    DomainFilter,
)
from sic.forms import (
    GenerateInviteForm,
    EditProfileForm,
    EditAvatarForm,
    EditAccountSettings,
    EditSessionSettings,
    EditHatForm,
    UserCreationForm,
    ComposeMessageForm,
    InvitationRequestForm,
    AnnotationForm,
    EditExactTagFilter,
    EditDomainFilter,
    NotifyOnNewInvitationRequests,
    NewStory,
)

from sic.views.utils import (
    form_errors_as_string,
    HttpResponseNotImplemented,
    Paginator,
    InvalidPage,
    check_next_url,
)

# Convert image to data:image/... in order to save avatars as strings in database
def generate_image_thumbnail(blob):
    with Image(blob=blob) as i:
        with i.convert("webp") as page:
            page.alpha_channel = False
            width = page.width
            height = page.height
            ratio = 100.0 / (width * 1.0)
            new_height = int(ratio * height)
            page.thumbnail(width=100, height=new_height)
            return page.data_url()


def login(request):
    if request.method == "POST":
        username = request.POST["username"]
        password = request.POST["password"]
        user = authenticate(request, username=username, password=password)
        if user is not None:
            auth_login(request, user)
            return redirect(reverse("index"))
        messages.add_message(request, messages.ERROR, "Could not login.")
    return render(request, "account/login.html")


@transaction.atomic
@require_http_methods(["GET", "POST"])
def login_with_ssh_signature(request):
    if not config.ENABLE_SSH_OTP_LOGIN:
        messages.add_message(
            request, messages.ERROR, "SSH OTP login not allowed in this website."
        )
        return redirect(reverse("login"))

    token = None
    timeout_left = None
    generate_new = True
    if request.method == "POST":
        if "refresh" not in request.POST:
            username = request.POST["username"]
            signature = request.POST["password"]
            try:
                user = authenticate(request, username=username, ssh_signature=signature)
                if user is not None:
                    auth_login(request, user)
                    return redirect(reverse("index"))
                messages.add_message(
                    request,
                    messages.ERROR,
                    "Could not login. A possible reason is you have not configured a ssh_public_key in your account settings.",
                )
            except Exception as exc:
                messages.add_message(request, messages.ERROR, f"Could not login: {exc}")
        else:
            del request.session["ssh_challenge"]
    existing_token = request.session.get("ssh_challenge", None)
    if existing_token:
        timeout, prev_token = existing_token
        timeout = make_aware(datetime.fromtimestamp(timeout))
        now = make_aware(datetime.now())
        timeout_left = timeout + timedelta(minutes=6)
        if timeout < now and now - timeout < timedelta(minutes=6):
            generate_new = False
            token = prev_token

    if generate_new:
        # generate a challenge token
        token = secrets.token_hex(5)
        now = make_aware(datetime.now())
        request.session["ssh_challenge"] = (now.timestamp(), token)
        timeout_left = now + timedelta(minutes=6)
    return render(
        request,
        "account/login.html",
        {
            "ssh_challenge": token,
            "timeout_left": timeout_left,
            "form": SSHAuthenticationForm,
        },
    )


@login_required
def view_account(request):
    user = request.user
    generate_invite_form = GenerateInviteForm()
    hats = None
    can_add_hats = user.has_perm("sic.add_hat")
    if can_add_hats:
        hats = user.hats.all()
    return render(
        request,
        "account/account.html",
        {
            "user": request.user,
            "generate_invite_form": generate_invite_form,
            "can_add_hats": can_add_hats,
            "hats": hats,
            "new_story_form": NewStory(),
        },
    )


@login_required
@transaction.atomic
def edit_profile(request):
    user = request.user
    if request.method == "POST":
        form = EditProfileForm(request.POST)
        if form.is_valid():
            request.user.homepage = form.cleaned_data["homepage"]
            request.user.git_repository = form.cleaned_data["git_repository"]
            request.user.about = form.cleaned_data["about"]
            for i in range(1, 5):
                field = f"metadata_{i}"
                label = field + "_label"
                user._wrapped.__dict__[field] = form.cleaned_data[field]
                user._wrapped.__dict__[label] = form.cleaned_data[label]
            request.user.save()
            return redirect(reverse("account"))
        error = form_errors_as_string(form.errors)
        messages.add_message(request, messages.ERROR, f"Invalid form. Error: {error}")
    else:
        initial = {
            "homepage": user.homepage,
            "git_repository": user.git_repository,
            "about": user.about,
        }
        for i in range(1, 5):
            field = f"metadata_{i}"
            label = field + "_label"
            initial[field] = user._wrapped.__dict__[field]
            initial[label] = user._wrapped.__dict__[label]
        form = EditProfileForm(initial=initial)
    return render(
        request, "account/edit_profile.html", {"user": request.user, "form": form}
    )


@login_required
@transaction.atomic
def edit_avatar(request):
    if request.method == "POST":
        if "delete-image" in request.POST:
            request.user.avatar = None
            request.user.save()
            messages.add_message(request, messages.SUCCESS, "Avatar deleted.")
            return redirect(reverse("account"))
        form = EditAvatarForm(request.POST, request.FILES)
        if form.is_valid():
            img = form.cleaned_data["new_avatar"]
            avatar_title = form.cleaned_data["avatar_title"]
            if img:
                data_url = generate_image_thumbnail(img)
                request.user.avatar = data_url
            request.user.avatar_title = avatar_title if len(avatar_title) > 0 else None
            request.user.save()
            messages.add_message(request, messages.SUCCESS, "Avatar updated.")
            return redirect(reverse("account"))
        error = form_errors_as_string(form.errors)
        messages.add_message(request, messages.ERROR, f"Invalid form. Error: {error}")
    else:
        form = EditAvatarForm(initial={"avatar_title": request.user.avatar_title})
    return render(
        request, "account/edit_avatar.html", {"user": request.user, "form": form}
    )


def profile(request, name):
    try:
        user = User.get_by_display_name(name)
    except User.DoesNotExist:
        try:
            user = User.objects.get(pk=int(name))
        except:
            raise Http404("User does not exist") from User.DoesNotExist
    return render(request, "account/profile.html", {"user": user})


@login_required
def inbox(request):
    user = request.user
    inbox_messages = user.received_messages.all().order_by("-created")
    return render(request, "account/inbox.html", {"messages_": inbox_messages})


@login_required
def inbox_sent(request):
    user = request.user
    inbox_messages = user.sent_messages.all().order_by("-created")
    return render(
        request, "account/inbox.html", {"messages_": inbox_messages, "is_sent": True}
    )


RES_PREFIX_RE = re.compile(r"^[rR]e:[ ]{0,1}")


@login_required
@transaction.atomic
def inbox_compose(request, in_reply_to=None):
    user = request.user
    if not user.has_perm("sic.add_message"):
        raise PermissionDenied("You don't have permission to send messages.")
    if in_reply_to:
        try:
            in_reply_to = Message.objects.get(pk=in_reply_to)
        except Message.DoesNotExist:
            raise Http404("Message does not exist") from Message.DoesNotExist
        if not user.has_perm("sic.view_message", in_reply_to):
            raise PermissionDenied("You don't have permission to view this message.")
    if request.method == "POST":
        form = ComposeMessageForm(request.POST)
        if form.is_valid():
            recipient = form.cleaned_data["recipient"]
            msg = Message.objects.create(
                recipient=recipient,
                read_by_recipient=False,
                author=request.user,
                hat=None,
                subject=form.cleaned_data["subject"],
                body=form.cleaned_data["body"],
            )
            messages.add_message(
                request, messages.SUCCESS, f"Message sent to {recipient}"
            )
            return redirect(msg)
    else:
        if in_reply_to:
            form = ComposeMessageForm(
                initial={
                    "recipient": in_reply_to.author,
                    "subject": f"Re: {in_reply_to.subject}"
                    if not RES_PREFIX_RE.match(in_reply_to.subject)
                    else in_reply_to.subject,
                    "body": f"On {in_reply_to.created.strftime('%Y/%m/%d, a %A, at %I:%M %P')}, {in_reply_to.author} wrote:\n"
                    + "\n".join(map(lambda l: "> " + l, in_reply_to.body.split("\n")))
                    + "\n\n",
                }
            )
        else:
            form = ComposeMessageForm(initial=request.GET)
    return render(request, "account/inbox_compose.html", {"form": form})


QUOTED_RE = re.compile(
    r"^(?P<first_line>On \d{4,4}/\d{2,2}/\d{2,2}, a [A-Z][^,]{1,}, at \d{2,2}:\d{2,2}\s*[ap]m, (?P<user>.{1,}?) wrote:$).*?(?P<quoted_text>^&gt; .{1,}?$){1,}^$^$(?P<reply>.{0,})",
    flags=(re.MULTILINE | re.DOTALL),
)

QUOTED_PART_RE = re.compile(
    r"(?:^(?P<level>(:?&gt;[ ]*)+)(?P<quoted_text>.{0,}?$)){1,}",
    flags=re.MULTILINE | re.DOTALL,
)


@login_required
def inbox_message(request, message_pk):
    try:
        msg = Message.objects.get(pk=message_pk)
    except Message.DoesNotExist:
        raise Http404("Message does not exist") from Message.DoesNotExist
    if not request.user.has_perm("sic.view_message", msg):
        raise PermissionDenied("You don't have permission to view this message.")
    if msg.recipient == request.user:
        msg.read_by_recipient = True
        msg.save(update_fields=["read_by_recipient"])
    if config.FORMAT_QUOTED_MESSAGES:
        match = QUOTED_RE.match(
            html.escape(msg.body.replace("\r\n", "\n"), quote=False)
        )
    else:
        match = None
    if match:
        match = {
            "first_line": match.group("first_line"),
            "user": match.group("user"),
            "quoted_text": match.group("quoted_text").lstrip(),
            "reply": match.group("reply"),
        }

        def quotedrepl(matchobj):
            levels = list(
                filter(lambda l: len(l.strip()) != 0, matchobj["level"].split(";"))
            )
            quotes = "".join(
                list(
                    map(
                        lambda i: f"""<span class="quote-level-{i[0]%4}">{i[1]};</span>""",
                        enumerate(levels),
                    )
                )
            )
            return f"""{quotes} <i class="quoted-level-{len(levels)%4-1}">{matchobj["quoted_text"]}</i>"""

        match["quoted_text"] = mark_safe(
            QUOTED_PART_RE.sub(quotedrepl, match["quoted_text"])
        )
        match["reply"] = mark_safe(QUOTED_PART_RE.sub(quotedrepl, match["reply"]))
        try:
            user = User.get_by_display_name(match["user"])
            match["first_line"] = mark_safe(
                match["first_line"].replace(
                    match["user"], f"""<a href="{user.get_absolute_url()}">{user}</a>"""
                )
            )
        except User.DoesNotExist:
            pass
    return render(
        request, "account/inbox_message.html", {"msg": msg, "formatted_message": match}
    )


@login_required
def inbox_message_raw(request, message_pk):
    try:
        msg = Message.objects.get(pk=message_pk)
    except Message.DoesNotExist:
        raise Http404("Message does not exist") from Message.DoesNotExist
    if not request.user.has_perm("sic.view_message", msg):
        raise PermissionDenied("You don't have permission to view this message.")
    if msg.recipient == request.user:
        msg.read_by_recipient = True
        msg.save(update_fields=["read_by_recipient"])
    return HttpResponse(msg.body, content_type="text/plain; charset=utf-8")


@login_required
@transaction.atomic
def generate_invite(request, invite_pk=None):
    if not request.user.has_perm("sic.add_invitation"):
        raise PermissionDenied("You don't have permission to generate invitations.")
    if invite_pk:
        try:
            inv = Invitation.objects.get(pk=invite_pk)
        except Invitation.DoesNotExist:
            raise Http404("Invitation URL is not valid") from Invitation.DoesNotExist
        if inv.inviter != request.user:
            raise PermissionDenied("This is not your invite.")
        if not inv.is_valid():
            messages.add_message(request, messages.ERROR, "Invitation has expired.")
        else:
            inv.send(request)
    elif request.method == "POST":
        user = request.user
        form = GenerateInviteForm(request.POST)
        req_pk = None
        if "req-pk" in request.POST:
            req_pk = request.POST["req-pk"]
        if form.is_valid():
            address = form.cleaned_data["email"]
            req = None
            if req_pk:
                req = InvitationRequest.objects.filter(pk=req_pk).first()
            if not req:
                req = InvitationRequest.objects.filter(address=address).first()
            if req and req.requested_by_id:
                return vouch_for_user(request, req.requested_by_id)
            inv, created = user.invited.get_or_create(inviter=user, address=address)
            if created:
                messages.add_message(
                    request,
                    messages.SUCCESS,
                    f"Successfully generated invitation to {address}.",
                )
                inv.send(request)
                if req:
                    req.fulfilled_by = inv
                    req.save(update_fields=["fulfilled_by"])
        else:
            error = form_errors_as_string(form.errors)
            messages.add_message(
                request, messages.ERROR, f"Invalid form. Error: {error}"
            )
    if "next" in request.GET and check_next_url(request.GET["next"]):
        return redirect(request.GET["next"])
    return redirect(reverse("account"))


def profile_posts(request, name, page_num=1):
    print("profile_posts", name, page_num)
    if page_num == 1 and request.get_full_path() != reverse("profile", args=[name]):
        return redirect(reverse("profile", args=[name]))
    try:
        user = User.get_by_display_name(name)
    except User.DoesNotExist:
        raise Http404("User does not exist") from User.DoesNotExist
    story_obj = list(
        user.stories.filter(active=True)
        .annotate(is_story=Value("True", output_field=BooleanField()))
        .order_by("-created", "title")
    ) + list(
        user.comments.filter(deleted=False)
        .annotate(is_story=Value("False", output_field=BooleanField()))
        .order_by("-created")
    )
    story_obj = sorted(story_obj, key=lambda x: x.created, reverse=True)
    paginator = Paginator(story_obj, config.STORIES_PER_PAGE)
    try:
        page = paginator.page(page_num)
    except InvalidPage:
        # page_num BooleanFieldis bigger than the actual number of pages
        return redirect(
            reverse(
                "profile_page",
                args=[name],
                kwargs={"page_num": paginator.num_pages},
            )
        )
    return render(
        request,
        "account/profile_posts.html",
        {
            "posts": page,
            "user": user,
            "pages": paginator.get_elided_page_range(number=page_num),
        },
    )


@transaction.atomic
@require_http_methods(["GET", "POST"])
def signup(request, invite_pk=None):
    inv = None
    if invite_pk:
        try:
            inv = Invitation.objects.get(pk=invite_pk)
        except Invitation.DoesNotExist:
            raise Http404("Invitation URL is not valid") from Invitation.DoesNotExist
        if not inv.is_valid():
            messages.add_message(request, messages.ERROR, "Invitation has expired.")
            return redirect("index")

    if request.user.is_authenticated and not inv:
        messages.add_message(request, messages.ERROR, "You already have an account.")
        return redirect("index")
    elif request.user.is_authenticated and inv:
        is_valid = inv.is_valid()
        has_correct_address = inv.address == request.user.email
        if is_valid and has_correct_address:
            if not config.REQUIRE_VOUCH_FOR_PARTICIPATION:
                return HttpResponseBadRequest("Vouches are not required.")
            inv.accept(request.user)
            messages.add_message(request, messages.SUCCESS, "You have been vouched!")
        elif not is_valid:
            messages.add_message(request, messages.ERROR, "Invalid invitation.")
        elif not has_correct_address:
            messages.add_message(
                request,
                messages.ERROR,
                "The invitation address and your account email do not match.",
            )
        return redirect("index")

    if not inv and not config.ALLOW_REGISTRATIONS:
        return HttpResponseBadRequest("Registrations are closed.")

    if request.method == "GET":
        form = UserCreationForm(initial={"email": inv.address if inv else None})
    elif request.method == "POST":
        form = UserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            auth_login(request, user)
            if inv:
                inv.accept(user)
                Notification.objects.create(
                    user=inv.inviter,
                    name=f"{user} has accepted your invitation",
                    kind=Notification.Kind.OTHER,
                    body=f"You can view {user}'s profile at {user.get_absolute_url()}.",
                    caused_by=user,
                    url=user.get_absolute_url(),
                )
            messages.add_message(
                request,
                messages.INFO,
                "You must validate your email address before being able to do anything on the website.",
            )
            user.send_validation_email(request)
            return redirect(reverse("welcome"))
    else:
        return redirect(reverse("index"))
    return render(request, "account/signup.html", {"form": form, "inv": inv})


@transaction.atomic
@require_http_methods(["GET", "POST"])
def accept_invite(request, invite_pk):
    return signup(request, invite_pk=invite_pk)


@login_required
@transaction.atomic
@require_http_methods(["POST"])
def send_validation_email(request):
    request.user.send_validation_email(request)
    return redirect("account")


@login_required
@transaction.atomic
@require_http_methods(["GET", "POST"])
def validate_email(request, token):
    user = request.user
    if user.email_validated:
        messages.add_message(
            request, messages.WARNING, "Your email address has already been validated!"
        )
        return redirect("index")
    from sic.auth import EmailValidationToken

    gen = EmailValidationToken()
    if gen.check_token(user, token):
        user.email_validated = True
        user.save(update_fields=["email_validated"])
        messages.add_message(
            request, messages.SUCCESS, "Your email address has been validated!"
        )
        return redirect("index")
    else:
        messages.add_message(
            request, messages.ERROR, "Link invalid or expired, try sending a new one."
        )
        return redirect("account")


@login_required
@transaction.atomic
@require_http_methods(["POST"])
def bookmark_story(request):
    user = request.user
    if "story_pk" not in request.POST:
        return HttpResponseBadRequest("Requested bookmark without a story primary key.")
    story_pk = request.POST["story_pk"]
    try:
        story_obj = Story.objects.get(pk=story_pk)
    except Story.DoesNotExist:
        raise Http404("Story does not exist") from Story.DoesNotExist
    if user.saved_stories.filter(pk=story_obj.pk).exists():
        user.saved_stories.remove(story_obj)
    else:
        user.saved_stories.add(story_obj, through_defaults=None)
    if "next" in request.GET and check_next_url(request.GET["next"]):
        return redirect(request.GET["next"])
    return redirect(reverse("index"))


@login_required
def bookmarks_json(request):
    user = request.user
    ret = []
    domain = Site.objects.get_current().domain
    tls = "" if settings.DEBUG else "s"
    for b in user.saved_stories.through.objects.filter(story__active=True).order_by(
        "-created", "story__title"
    ):
        story = {
            "id": b.story.pk,
            "user": str(b.story.user),
            "title": b.story.title,
            "content": b.story.content_to_plain_text,
            "sic_url": f"http{tls}://{domain}{b.story.get_absolute_url()}",
            "created": b.story.created,
            "publish_date": b.story.publish_date,
            "tags": list(map(lambda t: str(t), b.story.tags.all())),
            "kind": list(map(lambda k: str(k), b.story.kind.all())),
        }
        ret.append(
            {
                "type": "story",
                "story": story,
                "created": b.created,
                "annotation": b.annotation,
            }
        )
    for b in user.saved_comments.through.objects.filter(
        comment__deleted=False
    ).order_by("-created"):
        comment = {
            "id": b.comment.pk,
            "user": str(b.comment.user),
            "story_id": b.comment.story.pk,
            "story_title": b.comment.story.title,
            "text": b.comment.text_to_plain_text,
            "sic_url": "http://" + domain + b.comment.get_absolute_url(),
            "parent": ("http://" + domain + b.comment.parent.get_absolute_url())
            if b.comment.parent
            else None,
            "created": b.comment.created,
        }
        ret.append(
            {
                "type": "comment",
                "comment": comment,
                "created": b.created,
                "annotation": b.annotation,
            }
        )
    return JsonResponse(ret, safe=False)


@login_required
def bookmarks(request, page_num=1):
    if page_num == 1 and request.get_full_path() != reverse("bookmarks"):
        return redirect(reverse("bookmarks"))
    user = request.user
    story_obj = list(
        user.saved_stories.through.objects.filter(story__active=True)
        .annotate(is_story=Value("True", output_field=BooleanField()))
        .select_related("story")
        .order_by("-created", "story__title")
    ) + list(
        user.saved_comments.through.objects.filter(comment__deleted=False)
        .annotate(is_story=Value("False", output_field=BooleanField()))
        .order_by("-created")
        .select_related("comment")
    )
    story_obj = sorted(story_obj, key=lambda x: x.created, reverse=True)
    paginator = Paginator(story_obj, config.STORIES_PER_PAGE)
    try:
        page = paginator.page(page_num)
    except InvalidPage:
        # page_num BooleanFieldis bigger than the actual number of pages
        return redirect(
            reverse(
                "bookmarks_page",
                kwargs={"page_num": paginator.num_pages},
            )
        )
    return render(
        request,
        "account/bookmarks.html",
        {
            "bookmarks": page,
            "user": user,
            "pages": paginator.get_elided_page_range(number=page_num),
        },
    )


@login_required
@transaction.atomic
def edit_story_bookmark(request, bookmark_pk):
    user = request.user
    try:
        bookmark = StoryBookmark.objects.get(pk=bookmark_pk)
    except StoryBookmark.DoesNotExist:
        raise Http404("Story bookmark does not exist") from StoryBookmark.DoesNotExist
    if not user.has_perm("sic.change_storybookmark", bookmark):
        raise Http404("Story bookmark does not exist")

    if request.method == "POST":
        form = AnnotationForm(request.POST)
        if form.is_valid():
            bookmark.annotation = form.cleaned_data["annotation"]
            bookmark.save(update_fields=["annotation"])
            messages.add_message(
                request, messages.SUCCESS, "Bookmark annotation saved."
            )
            return redirect(reverse("bookmarks"))
        error = form_errors_as_string(form.errors)
        messages.add_message(request, messages.ERROR, f"Invalid form. Error: {error}")
    else:
        form = AnnotationForm(initial={"annotation": bookmark.annotation})
    return render(
        request,
        "account/edit_bookmark.html",
        {"bookmark": bookmark, "form": form},
    )


@login_required
@transaction.atomic
def edit_comment_bookmark(request, bookmark_pk):
    user = request.user
    try:
        bookmark = CommentBookmark.objects.get(pk=bookmark_pk)
    except CommentBookmark.DoesNotExist:
        raise Http404(
            "Comment bookmark does not exist"
        ) from CommentBookmark.DoesNotExist
    if not user.has_perm("sic.change_commentbookmark", bookmark):
        raise Http404("Comment bookmark does not exist")

    if request.method == "POST":
        form = AnnotationForm(request.POST)
        if form.is_valid():
            bookmark.annotation = form.cleaned_data["annotation"]
            bookmark.save(update_fields=["annotation"])
            messages.add_message(
                request, messages.SUCCESS, "Bookmark annotation saved."
            )
            return redirect(reverse("bookmarks"))
        error = form_errors_as_string(form.errors)
        messages.add_message(request, messages.ERROR, f"Invalid form. Error: {error}")
    else:
        form = AnnotationForm(initial={"annotation": bookmark.annotation})
    return render(
        request,
        "account/edit_bookmark.html",
        {"bookmark": bookmark, "form": form},
    )


@login_required
@transaction.atomic
def edit_settings(request):
    user = request.user
    session_settings = {
        "vivid_colors": request.session.get("vivid_colors", True),
        "font_size": request.session.get("font_size", 100),
    }
    form = None
    session_form = None
    if request.method == "POST":
        if "session-settings" in request.POST:
            session_form = EditSessionSettings(request.POST)
            if session_form.is_valid():
                request.session["vivid_colors"] = session_form.cleaned_data[
                    "vivid_colors"
                ]
                request.session["font_size"] = session_form.cleaned_data["font_size"]
                messages.add_message(
                    request, messages.SUCCESS, "Session settings updated successfully."
                )
                return redirect(reverse("account"))
            error = form_errors_as_string(session_form.errors)
        else:
            form = EditAccountSettings(request.POST)
            if form.is_valid():
                if form.cleaned_data["username"] != user.username:
                    user.username = form.cleaned_data["username"]
                if form.cleaned_data["email"] != user.email:
                    user.email = form.cleaned_data["email"]
                user.email_notifications = form.cleaned_data["email_notifications"]
                user.email_replies = form.cleaned_data["email_replies"]
                user.email_messages = form.cleaned_data["email_messages"]
                user.email_mentions = form.cleaned_data["email_mentions"]
                user.enable_mailing_list = form.cleaned_data["enable_mailing_list"]
                user.enable_mailing_list_comments = form.cleaned_data[
                    "enable_mailing_list_comments"
                ]
                user.enable_mailing_list_replies = form.cleaned_data[
                    "enable_mailing_list_replies"
                ]
                user.enable_mailing_list_replying = form.cleaned_data[
                    "enable_mailing_list_replying"
                ]
                user.show_avatars = form.cleaned_data["show_avatars"]
                user.show_colors = form.cleaned_data["show_colors"]
                user.ssh_public_key = form.cleaned_data["ssh_public_key"]
                user.save()
                messages.add_message(
                    request, messages.SUCCESS, "Account settings updated successfully."
                )
                return redirect(reverse("account"))
            error = form_errors_as_string(form.errors)
        messages.add_message(request, messages.ERROR, f"Invalid form. Error: {error}")
    if form is None:
        initial = user._wrapped.__dict__
        initial["user"] = user
        form = EditAccountSettings(initial=initial)
    if session_form is None:
        session_form = EditSessionSettings(initial=session_settings)
    return render(
        request,
        "account/edit_settings.html",
        {
            "user": user,
            "form": form,
            "session_form": session_form,
        },
    )


@login_required
def notifications(request):
    user = request.user
    actives = list(user.notifications.filter(read__isnull=True).order_by("-created"))
    rest = list(user.notifications.filter(read__isnull=False).order_by("-created"))
    user.notifications.filter(read__isnull=True).update(read=make_aware(datetime.now()))
    return render(
        request,
        "account/notifications.html",
        {"user": user, "active_notifications": actives, "rest_notifications": rest},
    )


@login_required
@transaction.atomic
def edit_hat(request, hat_pk=None):
    if hat_pk:
        try:
            hat = Hat.objects.get(pk=hat_pk)
        except Hat.DoesNotExist:
            raise Http404("Hat does not exist") from Hat.DoesNotExist
    else:
        hat = None
    user = request.user

    if hat and not user.has_perm("sic.change_hat", hat):
        raise PermissionDenied("This is not your hat.")
    if request.method == "POST":
        form = EditHatForm(request.POST)
        if form.is_valid():
            new_name = form.cleaned_data["name"]
            new_color = form.cleaned_data["hex_color"]
            if hat:
                hat.name = new_name
                hat.hex_color = new_color
            else:
                hat = Hat.objects.create(name=new_name, hex_color=new_color, user=user)
            hat.save()
            messages.add_message(request, messages.SUCCESS, "Hat edited successfully.")
            return redirect(reverse("account"))
        error = form_errors_as_string(form.errors)
        messages.add_message(request, messages.ERROR, f"Invalid form. Error: {error}")
    else:
        form = EditHatForm(
            initial={"name": hat.name, "hex_color": hat.hex_color} if hat else {}
        )
    return render(
        request, "account/edit_hat.html", {"user": user, "form": form, "hat": hat}
    )


@login_required
@require_http_methods(["GET"])
@transaction.atomic
def issue_token(request):
    user = request.user
    user.auth_token = AuthToken().make_token(user)
    user.save(update_fields=["auth_token"])
    messages.add_message(request, messages.SUCCESS, "New auth token generated.")
    if "next" in request.GET and check_next_url(request.GET["next"]):
        return redirect(request.GET["next"])
    return redirect(reverse("account"))


@login_required
@require_http_methods(["GET", "POST"])
@transaction.atomic
def invitation_requests(request):
    user = request.user
    notify_form = NotifyOnNewInvitationRequests(
        initial={"notify_me": user.notify_on_new_invitation_request}
    )
    if request.method == "POST":
        try:
            if "vote-pk" in request.POST and "submit" in request.POST:
                vote_pk = request.POST["vote-pk"]
                choice = request.POST[f"choice-{vote_pk}"]
                choice = True if choice == "yes" else False if choice == "no" else None
                note = request.POST[f"note-{vote_pk}"]
                InvitationRequest.objects.get(pk=int(vote_pk)).votes.create(
                    user=user, in_favor=choice, note=note
                )
            elif "vote-pk" in request.POST and "delete-vote" in request.POST:
                vote_pk = request.POST["vote-pk"]
                InvitationRequest.objects.get(pk=int(vote_pk)).votes.filter(
                    user__pk=user.pk
                ).delete()
            elif "set-notify" in request.POST:
                notify_form = NotifyOnNewInvitationRequests(request.POST)
                if notify_form.is_valid():
                    user.notify_on_new_invitation_request = notify_form.cleaned_data[
                        "notify_me"
                    ]
                    user.save(update_fields=["notify_on_new_invitation_request"])
                    messages.add_message(
                        request,
                        messages.SUCCESS,
                        "Your notification settings have been updated.",
                    )
        except Exception as exc:
            messages.add_message(request, messages.ERROR, f"Exception: {exc}")
    requests = list(InvitationRequest.objects.filter(fulfilled_by__isnull=True))
    for req in requests:
        req.have_voted = False
        if req.votes.filter(user__pk=user.pk).exists():
            req.have_voted = True
            continue
    return render(
        request,
        "account/invitation_requests.html",
        {"requests": requests, "notify_form": notify_form},
    )


@require_http_methods(["GET", "POST"])
@transaction.atomic
def new_invitation_request(request):
    if request.user.is_authenticated and request.user.can_participate:
        messages.add_message(request, messages.ERROR, "You already have an account.")
        return redirect("account")
    if config.ALLOW_REGISTRATIONS and not request.user.is_authenticated:
        return redirect("signup")
    if request.method == "POST":
        if not config.ALLOW_INVITATION_REQUESTS:
            messages.add_message(
                request, messages.ERROR, "Invitation requests are disabled."
            )
            return redirect("index")
        form = InvitationRequestForm(request.POST)
        if form.is_valid():
            messages.add_message(request, messages.SUCCESS, "Request submitted.")
            new_req = InvitationRequest(
                name=form.cleaned_data["name"],
                address=form.cleaned_data["address"],
                about=form.cleaned_data["about"],
                requested_by_id=request.user.id
                if request.user.is_authenticated
                else None,
            )
            new_req.save()
        return redirect("index")
    if request.user.is_authenticated:
        from django import forms

        form = InvitationRequestForm(
            initial={"name": str(request.user), "address": request.user.email}
        )
        form.fields["name"].widget = forms.HiddenInput()
        form.fields["address"].widget = forms.HiddenInput()
    else:
        form = InvitationRequestForm()
    return render(
        request,
        "account/new_invitation_request.html",
        {
            "form": form,
        },
    )


@login_required
@transaction.atomic
def welcome(request):
    form = NewStory()
    return render(
        request,
        "account/after_signup.html",
        {
            "user": request.user,
            "form": form,
        },
    )


@login_required
@require_safe
def my_activity(request, page_num=1):
    if page_num == 1 and request.get_full_path() != reverse("account_activity"):
        return redirect(reverse("account_activity"))
    user = request.user
    user_stories = user.stories.prefetch_related("tags", "user", "comments").order_by(
        "-created"
    )
    user_comments = user.comments.prefetch_related("parent", "replies").order_by(
        "-created"
    )
    activities = []
    for story in user_stories:
        activities.append(
            {
                "type": "story",
                "obj": story,
                "date": story.created,
            }
        )
        comments = story.comments.exclude(user_id=user.pk).filter(
            parent_id=None, deleted=False
        )
        if comments.exists():
            date = max(comment.created for comment in comments)
            activities.append(
                {
                    "type": "story_reply",
                    "obj": story,
                    "date": date,
                    "count": len(comments),
                    "items": comments,
                }
            )
    for comment in user_comments:
        activities.append(
            {
                "type": "comment",
                "obj": comment,
                "date": comment.created,
            }
        )
        replies = comment.replies.exclude(user_id=user.pk).filter(deleted=False)
        if replies.exists():
            date = comment.created
            for reply in replies:
                date = max(date, reply.created)
            activities.append(
                {
                    "type": "comment_reply",
                    "obj": comment,
                    "date": date,
                    "count": len(replies),
                    "items": replies,
                }
            )
    activities.sort(key=lambda a: a["date"], reverse=True)
    paginator = Paginator(activities, config.STORIES_PER_PAGE)
    try:
        page = paginator.page(page_num)
    except InvalidPage:
        # page_num BooleanFieldis bigger than the actual number of pages
        return redirect(
            reverse(
                "account_activity_page",
                kwargs={"page_num": paginator.num_pages},
            )
        )
    groups = []
    for key, group in itertools.groupby(page, key=lambda a: a["type"]):
        group = list(group)
        date_min = min(v["date"] for v in group)
        date_max = max(v["date"] for v in group)
        if date_max.date() == date_min.date():
            date_max = None
        groups.append(
            {
                "type": key,
                "count": len(group),
                "items": group,
                "date_min": date_min,
                "date_max": date_max,
            }
        )
    return render(
        request,
        "account/activity.html",
        {
            "user": user,
            "activities": groups,
            "page": page,
            "pages": paginator.get_elided_page_range(number=page_num),
        },
    )


@login_required
@transaction.atomic
@require_safe
def edit_filters(request):
    user = request.user
    return render(
        request,
        "account/edit_filters.html",
        {
            "user": user,
            "exact_tag_filters": ExactTagFilter.objects.filter(excluded_in_user=user),
            "domain_filters": DomainFilter.objects.filter(excluded_in_user=user),
        },
    )


@login_required
@transaction.atomic
@require_http_methods(["GET", "POST"])
def add_tag_filter(request):
    user = request.user
    if request.method == "POST":
        form = EditExactTagFilter(request.POST)
        if form.is_valid():
            f, _created = ExactTagFilter.objects.get_or_create(
                name=form.cleaned_data["name"], tag=form.cleaned_data["tag"]
            )
            f.excluded_in_user.set([user])
            messages.add_message(request, messages.SUCCESS, "Filter saved.")
            return redirect(reverse("edit_filters"))
        error = form_errors_as_string(form.errors)
        messages.add_message(request, messages.ERROR, f"Invalid form. Error: {error}")
    else:
        form = EditExactTagFilter()
    return render(
        request,
        "account/add_filter.html",
        {
            "user": user,
            "form": form,
            "title": "Add tag filter",
        },
    )


@login_required
@transaction.atomic
@require_http_methods(["GET", "POST"])
def add_domain_filter(request):
    user = request.user
    if request.method == "POST":
        form = EditDomainFilter(request.POST)
        if form.is_valid():
            f, _created = DomainFilter.objects.get_or_create(
                name=form.cleaned_data["name"],
                match_string=form.cleaned_data["match_string"],
                is_regexp=form.cleaned_data["is_regexp"],
            )
            f.excluded_in_user.set([user])
            messages.add_message(request, messages.SUCCESS, "Filter saved.")
            return redirect(reverse("edit_filters"))
        error = form_errors_as_string(form.errors)
        messages.add_message(request, messages.ERROR, f"Invalid form. Error: {error}")
    else:
        form = EditDomainFilter()
    return render(
        request,
        "account/add_filter.html",
        {
            "user": user,
            "form": form,
            "title": "Add domain filter",
        },
    )


@login_required
@transaction.atomic
@require_http_methods(["GET", "POST"])
def edit_tag_filter(request, pk):
    try:
        f = ExactTagFilter.objects.get(pk=pk)
    except ExactTagFilter.DoesNotExist:
        raise Http404("Filter does not exist") from ExactTagFilter.DoesNotExist
    if not request.user.has_perm("sic.change_exacttagfilter", f):
        raise Http404("Filter does not exist") from PermissionDenied(
            "You don't have permission to view this filter."
        )
    user = request.user

    if request.method == "POST":
        form = EditExactTagFilter(request.POST)
        if form.is_valid():
            f.name = form.cleaned_data["name"]
            f.tag = form.cleaned_data["tag"]
            f.save()
            messages.add_message(request, messages.SUCCESS, "Filter saved.")
            return redirect(reverse("edit_filters"))
        error = form_errors_as_string(form.errors)
        messages.add_message(request, messages.ERROR, f"Invalid form. Error: {error}")
    else:
        form = EditExactTagFilter(
            initial={
                "name": f.name,
                "tag": f.tag,
            }
        )
    return render(
        request,
        "account/add_filter.html",
        {
            "user": user,
            "form": form,
            "title": "Edit tag filter",
        },
    )


@login_required
@transaction.atomic
@require_http_methods(["GET", "POST"])
def edit_domain_filter(request, pk):
    try:
        f = DomainFilter.objects.get(pk=pk)
    except DomainFilter.DoesNotExist:
        raise Http404("Filter does not exist") from DomainFilter.DoesNotExist
    if not request.user.has_perm("sic.change_domainfilter", f):
        raise Http404("Filter does not exist") from PermissionDenied(
            "You don't have permission to view this filter."
        )
    user = request.user
    form = EditDomainFilter(
        initial={
            "name": f.name,
            "match_string": f.match_string,
            "is_regexp": f.is_regexp,
        }
    )
    return render(
        request,
        "account/add_filter.html",
        {
            "user": user,
            "form": form,
            "title": "Edit domain filter",
        },
    )


@login_required
@transaction.atomic
@require_http_methods(["POST"])
def delete_tag_filter(request, pk):
    try:
        f = ExactTagFilter.objects.get(pk=pk)
    except ExactTagFilter.DoesNotExist:
        raise Http404("Filter does not exist") from ExactTagFilter.DoesNotExist
    if not request.user.has_perm("sic.delete_exacttagfilter", f):
        raise Http404("Filter does not exist") from PermissionDenied(
            "You don't have permission to view this filter."
        )
    f.delete()
    messages.add_message(request, messages.SUCCESS, "Filter deleted.")
    return redirect(reverse("edit_filters"))


@login_required
@transaction.atomic
@require_http_methods(["POST"])
def delete_domain_filter(request, pk):
    try:
        f = DomainFilter.objects.get(pk=pk)
    except DomainFilter.DoesNotExist:
        raise Http404("Filter does not exist") from DomainFilter.DoesNotExist
    if not request.user.has_perm("sic.delete_domainfilter", f):
        raise Http404("Filter does not exist") from PermissionDenied(
            "You don't have permission to view this filter."
        )
    f.delete()
    messages.add_message(request, messages.SUCCESS, "Filter deleted.")
    return redirect(reverse("edit_filters"))


@login_required
@transaction.atomic
@require_http_methods(["POST"])
def vouch_for_user(request, pk):
    if not config.REQUIRE_VOUCH_FOR_PARTICIPATION:
        return HttpResponseBadRequest("Vouches are not required.")
    if not request.user.has_perm("sic.add_invitation"):
        raise PermissionDenied("You don't have permission to vouch for other users.")
    try:
        receiver = User.objects.get(pk=pk)
    except User.DoesNotExist:
        raise Http404("User does not exist") from User.DoesNotExist
    user_has_invite = False
    try:
        user_has_invite = receiver.invited_by is not None
    except Invitation.DoesNotExist:
        pass
    if user_has_invite:
        return HttpResponseBadRequest("User is vouched for.")

    inv, created = request.user.invited.get_or_create(
        inviter=request.user,
        receiver=receiver,
        address=receiver.email,
        accepted=make_aware(datetime.now()),
    )
    if created:
        root_url = f"{config.WEB_PROTOCOL}://{config.get_domain()}"
        Notification.objects.create(
            user=receiver,
            name=f"{request.user} has vouched for your account",
            kind=Notification.Kind.OTHER,
            body=f"Hello {receiver},\n\nuser {request.user} has vouched for your account. You can now post comments and stories.\n\n{root_url}",
            caused_by=request.user,
            url=None,
        )
    req = InvitationRequest.objects.filter(requested_by_id=pk).first()
    if req:
        req.fulfilled_by = inv
        req.save(update_fields=["fulfilled_by"])
    messages.add_message(request, messages.SUCCESS, f"You have vouched for {receiver}.")
    if "next" in request.GET and check_next_url(request.GET["next"]):
        return redirect(request.GET["next"])
    return redirect(receiver)


@require_http_methods(["POST"])
def toggle_signup_box_view(request):
    if "show_signup_box" in request.session:
        del request.session["show_signup_box"]
    else:
        request.session["show_signup_box"] = True
    if "next" in request.GET and check_next_url(request.GET["next"]):
        return redirect(request.GET["next"])
    return redirect(reverse("index"))


@login_required
@transaction.atomic
@require_http_methods(["POST"])
def restart_account(request):
    from sic import blockchain
    from sic.s3 import upload_media

    form = NewStory(request.POST, request.FILES)

    user = request.user
    if form.is_valid():
        password = form.cleaned_data["password"]

        try:
            media_obj = upload_media(request.FILES["picture"])
            media_sha256 = media_obj.hexdigest
            blockchain_data = {
                "type": "spawn",
                "username": str(user),
                "picture_sha256": media_sha256,
                "previous_story_hash": user.birth_hash if user.birth_hash else None,
                "date": make_aware(datetime.now()).isoformat(),
            }

            birth_hash = blockchain.spawn_story(password, json.dumps(blockchain_data))
            data_url = generate_image_thumbnail(form.cleaned_data["picture"])
            user.avatar = data_url
            user.birth_hash = birth_hash
            user.save()
            messages.add_message(
                request,
                messages.SUCCESS,
                f"You have started a new Pet Story with birth hash {birth_hash}",
            )
        except Exception as exc:
            messages.add_message(
                request, messages.ERROR, f"Could not start new birth hash: {exc}"
            )
    else:
        error = form_errors_as_string(form.errors)
        messages.add_message(request, messages.ERROR, f"Invalid form. Error: {error}")
    user.reset_ttl_cache()
    return redirect(reverse("account"))
