"""The sign-in, sign-out and password pages of the admin's own users."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hinoki import core
from hinoki.admin.csrf import FIELD, cookie_header
from hinoki.admin.site import SECURITY_HEADERS
from hinoki.admin.templates import Raw, escape
from hinoki.admin.users.auth import end_cookie, session_cookie
from hinoki.admin.users.service import AdminUserError
from hinoki.api import API
from hinoki.auth import Caller
from hinoki.http.request import Request
from hinoki.http.response import Response

if TYPE_CHECKING:
    from hinoki.admin.core import Admin

MAX_NAME = 200
MAX_SECRET = 1100  # bytes of a password field read from a form
EXPIRED = "The form expired or is invalid. Reload and try again."
WRONG = "Wrong username or password."
SIGN_IN = "!sign-in"  # the CSRF subject before sign-in; no username can be this


def field(admin: Admin, name: str, label: str, kind: str, autocomplete: str) -> Raw:
    control = Raw(
        f'<input id="f-{name}" name="{name}" type="{kind}" required '
        f'autocomplete="{autocomplete}" maxlength="{MAX_NAME}">'
    )
    return admin.templates.render(
        "field", name=name, label=label, control=control, help=Raw("")
    )


def page(  # noqa: PLR0913 - one value per part of the page
    admin: Admin,
    request: Request,
    subject: str,
    *,
    title: str,
    action: str,
    fields: list[Raw],
    submit: str,
    problem: str = "",
    links: str = "",
    status: int = 200,
) -> Response:
    """A small card page; its CSRF token is for ``subject``."""
    token, fresh = admin.csrf.issue(request, subject)
    t = admin.templates
    html = t.render(
        "auth",
        theme=admin.theme.name,
        title=title,
        site=admin.site,
        stylesheet=f"{admin.base}/static/admin.css?v={admin.digests['admin.css']}",
        sprite=t.sprite,
        notice=admin.parts.notice("error", problem) if problem else Raw(""),
        action=action,
        csrf=token,
        fields=Raw("".join(fields)),
        links=Raw(links),
        submit=submit,
    )
    headers = list(SECURITY_HEADERS)
    if fresh:
        headers.append(
            cookie_header(
                request,
                token,
                admin.base,
                secure=admin.secure_cookies,
                name=admin.csrf.cookie,
            )
        )
    return Response(status, html, headers, "text/html; charset=utf-8")


def redirect(location: str, *headers: tuple[str, str]) -> Response:
    return Response(303, headers=[*SECURITY_HEADERS, ("location", location), *headers])


def secure(admin: Admin, request: Request) -> bool:
    return (
        request.scheme == "https"
        if admin.secure_cookies is None
        else admin.secure_cookies
    )


def session_of(admin: Admin, request: Request) -> str:
    return request.COOKIES.get(admin.session_cookie, "")


def signin_page(
    admin: Admin, request: Request, problem: str = "", status: int = 200
) -> Response:
    return page(
        admin,
        request,
        SIGN_IN,
        title="Sign in",
        action=f"{admin.base}/login",
        fields=[
            field(admin, "username", "Username", "text", "username"),
            field(admin, "password", "Password", "password", "current-password"),
        ],
        submit="Sign in",
        problem=problem,
        status=status,
    )


async def login_form(self: API, request: Request) -> Response:
    admin: Admin = self.admin  # type: ignore[attr-defined]
    if await admin.accounts.user_for(session_of(admin, request)) is not None:
        return redirect(admin.base or "/")
    return signin_page(admin, request)


async def login_submit(self: API, request: Request) -> Response:
    admin: Admin = self.admin  # type: ignore[attr-defined]
    if not admin.csrf.valid(request, SIGN_IN):
        return signin_page(admin, request, EXPIRED, 403)
    accounts = admin.accounts
    username = request.POST.get("username", "")[:MAX_NAME]
    password = request.POST.get("password", "")[:MAX_SECRET]
    user = await accounts.verify(username, password)
    if user is None:
        return signin_page(admin, request, WRONG, 401)
    previous = session_of(admin, request)
    if previous:
        await accounts.close_session(previous)  # a new sign-in never reuses a token
    token = await accounts.open_session(user, request.headers.get("user-agent", ""))
    cookie = session_cookie(
        token,
        admin.base,
        hours=accounts.settings.session_hours,
        secure=secure(admin, request),
        name=admin.session_cookie,
    )
    return redirect(admin.base or "/", cookie)


async def logout(self: API, request: Request, principal: Caller[Any]) -> Response:
    admin: Admin = self.admin  # type: ignore[attr-defined]
    if not admin.csrf.valid(request, principal.id):
        return page(
            admin,
            request,
            principal.id,
            title="Sign out",
            action=f"{admin.base}/logout",
            fields=[],
            submit="Sign out",
            problem=EXPIRED,
            status=403,
        )
    await admin.accounts.close_session(session_of(admin, request))
    return redirect(
        f"{admin.base}/login",
        end_cookie(
            admin.base, secure=secure(admin, request), name=admin.session_cookie
        ),
    )


def password_page(
    admin: Admin,
    request: Request,
    principal: Caller[Any],
    problem: str = "",
    status: int = 200,
) -> Response:
    least = admin.accounts.settings.min_password_length
    return page(
        admin,
        request,
        principal.id,
        title=f"Change password for {principal.id}",
        action=f"{admin.base}/password",
        fields=[
            field(admin, "current", "Current password", "password", "current-password"),
            field(
                admin,
                "new",
                f"New password (at least {least} characters)",
                "password",
                "new-password",
            ),
            field(admin, "again", "New password again", "password", "new-password"),
        ],
        submit="Change password",
        problem=problem,
        links=f'<a class="hn-btn" href="{escape(admin.base or "/")}">Cancel</a>',
        status=status,
    )


async def password_form(
    self: API, request: Request, principal: Caller[Any]
) -> Response:
    return password_page(self.admin, request, principal)  # type: ignore[attr-defined]


async def password_submit(
    self: API, request: Request, principal: Caller[Any]
) -> Response:
    admin: Admin = self.admin  # type: ignore[attr-defined]
    if not admin.csrf.valid(request, principal.id):
        return password_page(admin, request, principal, EXPIRED, 403)
    problem = await change_password(admin, request, principal)
    if problem:
        return password_page(admin, request, principal, problem, 422)
    token = await admin.accounts.open_session(
        principal.user, request.headers.get("user-agent", "")
    )
    cookie = session_cookie(
        token,
        admin.base,
        hours=admin.accounts.settings.session_hours,
        secure=secure(admin, request),
        name=admin.session_cookie,
    )
    return redirect(f"{admin.base}?done=password", cookie)


async def change_password(
    admin: Admin, request: Request, principal: Caller[Any]
) -> str:
    """Change the signed-in user's password; the message to show, or ``""``."""
    accounts = admin.accounts
    given = request.POST
    current = given.get("current", "")[:MAX_SECRET]
    new = given.get("new", "")[:MAX_SECRET]
    if await accounts.verify(principal.id, current) is None:
        return "The current password is wrong."
    if new != given.get("again", ""):
        return "The two new passwords differ."
    try:
        await accounts.set_password(principal.user, new)  # ends every session
    except AdminUserError as error:
        return str(error)
    return ""


AUTH_VIEW_FUNCTIONS: dict[str, Any] = {
    "login_form": login_form,
    "login_submit": login_submit,
    "logout": logout,
    "password_form": password_form,
    "password_submit": password_submit,
}

__all__ = ["AUTH_VIEW_FUNCTIONS", "FIELD", "core"]
