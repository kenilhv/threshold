"""The console's sign-in round-trip, driven the way a browser drives it.

Nothing else in this suite does this. Every other test mints a token straight
from Keycloak's password grant and calls the API, so the one flow a person
actually uses -- open the console, sign in, come back -- had no test. It shipped
for four milestones sending a freshly signed-in user to
``http://0.0.0.0:3000/queue``: the address the Next server binds inside its
container, taken from ``request.url`` and used as the base for the redirect
home. Nobody noticed until somebody signed in.

This test is that somebody. Redirects are not followed automatically, so every
``Location`` header can be read, and the assertion that matters is on the last
one: the callback sends the browser back to the origin it came from. The
session cookie is asserted alongside it, because a redirect to the right place
after a login that quietly failed would pass the first check and mean nothing.

The credentials are the committed fixture accounts in
``infra/local/keycloak/threshold-realm.json``. Posting them to Keycloak's login
form from a test is what those accounts exist for.

Requires: make up
"""

from __future__ import annotations

import html
import re
import uuid
from collections.abc import Iterator
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
import pytest

pytestmark = pytest.mark.integration

CONSOLE = "http://localhost:3000"
KEYCLOAK = "http://localhost:8080"


class Browser:
    """A cookie jar with a browser's manners, not Python's.

    ``httpx`` delegates to ``http.cookiejar``, which refuses to send a
    ``Secure`` cookie over plain HTTP. Keycloak marks ``AUTH_SESSION_ID`` and
    ``KC_RESTART`` ``Secure``; browsers treat ``http://localhost`` as a secure
    context and send them anyway, which is the only reason signing in works at
    all in local development. A client that honours the letter of the rule loses
    Keycloak's session between fetching the login form and posting it, and
    Keycloak answers with a bare 400 and a re-rendered form -- indistinguishable
    from a wrong password.

    Rather than subclass a cookie policy and depend on which of its hooks httpx
    happens to call, this keeps the rule in one visible place: cookies are
    stored by name, scoped by path, and sent to localhost over http. That is
    what the browser in front of this console does.
    """

    def __init__(self, client: httpx.Client) -> None:
        self._client = client
        self._jar: dict[str, tuple[str, str]] = {}

    def _header(self, url: str) -> dict[str, str]:
        path = urlparse(url).path or "/"
        sending = [
            f"{name}={value}"
            for name, (value, scope) in self._jar.items()
            if path.startswith(scope)
        ]
        return {"Cookie": "; ".join(sending)} if sending else {}

    def _absorb(self, response: httpx.Response) -> None:
        for raw in response.headers.get_list("set-cookie"):
            pair, _, attrs = raw.partition(";")
            name, _, value = pair.strip().partition("=")
            if not name:
                continue
            scope = "/"
            for attr in attrs.split(";"):
                key, _, val = attr.strip().partition("=")
                if key.lower() == "path" and val:
                    scope = val
            # An expiry in the past is a deletion, which is how the console
            # clears the PKCE verifier once it has been used.
            if value:
                self._jar[name] = (value, scope)
            else:
                self._jar.pop(name, None)

    def get(self, url: str) -> httpx.Response:
        response = self._client.get(url, headers=self._header(url))
        self._absorb(response)
        return response

    def post(self, url: str, data: dict[str, str]) -> httpx.Response:
        response = self._client.post(url, data=data, headers=self._header(url))
        self._absorb(response)
        return response

    def holds(self, name: str) -> bool:
        return name in self._jar


#: What a Keycloak login page's form posts to. Tolerant of attribute order,
#: because the theme decides that and this test does not own the theme.
_FORM_ACTION = re.compile(r'<form[^>]+id="kc-form-login"[^>]+action="([^"]+)"')
_FORM_ACTION_FALLBACK = re.compile(r'action="([^"]+login-actions/authenticate[^"]*)"')


@pytest.fixture
def browser() -> Iterator[Browser]:
    """One cookie jar, no automatic redirects.

    A single client on purpose: cookies are scoped by host and not by port, so
    the console's cookies on ``localhost`` are sent to Keycloak on ``localhost``
    too -- which is exactly what a real browser does, and the flow has to work
    under that.
    """
    with httpx.Client(follow_redirects=False, timeout=30) as client:
        try:
            if client.get(f"{CONSOLE}/api/health").status_code != 200:
                pytest.skip("the console is not healthy; `make up`")
            if client.get(f"{KEYCLOAK}/realms/threshold").status_code != 200:
                pytest.skip("Keycloak is not serving the realm; `make up`")
        except httpx.HTTPError:
            pytest.skip("the stack is not running; `make up`")
        yield Browser(client)


def _location(response: httpx.Response) -> str:
    assert response.status_code in (302, 303, 307, 308), (
        f"expected a redirect, got {response.status_code} from {response.url}"
    )
    return urljoin(str(response.url), response.headers["location"])


def _follow_within(browser: Browser, url: str, host: str, *, hops: int = 5) -> httpx.Response:
    """Follow redirects only while they stay on ``host``. Stop at the first that leaves."""
    response = browser.get(url)
    for _ in range(hops):
        if response.status_code not in (302, 303, 307, 308):
            return response
        nxt = _location(response)
        if urlparse(nxt).netloc != host:
            return response
        response = browser.get(nxt)
    raise AssertionError(f"more than {hops} redirects on {host}")


def sign_in(browser: Browser, *, user: str = "buyer", start: str = "/") -> httpx.Response:
    """Drive the whole flow and return the console's *callback* response, unfollowed.

    ``start`` is the page the person first opened. Whatever it was, the callback
    is supposed to send them back to it.
    """
    first = browser.get(f"{CONSOLE}{start}")
    login_url = _location(first)
    assert urlparse(login_url).path == "/api/auth/login", login_url

    to_keycloak = browser.get(login_url)
    auth_url = _location(to_keycloak)
    assert urlparse(auth_url).netloc == urlparse(KEYCLOAK).netloc, auth_url

    page = _follow_within(browser, auth_url, urlparse(KEYCLOAK).netloc)
    assert page.status_code == 200, f"Keycloak did not serve a login page: {page.status_code}"
    match = _FORM_ACTION.search(page.text) or _FORM_ACTION_FALLBACK.search(page.text)
    assert match, "no login form on the Keycloak page"
    action = html.unescape(match.group(1))

    posted = browser.post(
        action,
        data={"username": user, "password": user, "credentialId": ""},
    )
    callback_url = _location(posted)
    assert urlparse(callback_url).path == "/api/auth/callback", callback_url
    assert "code=" in callback_url, "Keycloak did not issue an authorization code"

    return browser.get(callback_url)


class TestSigningInBringsYouBackToTheConsole:
    def test_the_callback_redirects_to_the_console_origin_and_not_the_bind_address(
        self, browser: Browser
    ) -> None:
        """The bug, as a sentence. The server binds 0.0.0.0; the browser must
        never be told to go there."""
        callback = sign_in(browser)
        location = _location(callback)
        assert urlparse(location).netloc == urlparse(CONSOLE).netloc, location
        assert "0.0.0.0" not in location, location
        assert urlparse(location).path == "/queue", location

    def test_the_session_cookie_travels_with_that_redirect(self, browser: Browser) -> None:
        """Without this, a login that silently failed and redirected somewhere
        valid would pass the test above and mean nothing."""
        callback = sign_in(browser)
        assert callback.status_code in (302, 303, 307, 308)
        assert browser.holds("threshold_session"), "no session was minted"

    def test_following_it_renders_a_page_and_not_the_login_form(self, browser: Browser) -> None:
        """The whole point, from the person's side of the screen."""
        callback = sign_in(browser)
        landed = browser.get(_location(callback))
        assert landed.status_code == 200, landed.status_code
        assert "Sign in to your account" not in landed.text

    def test_a_deep_link_survives_the_round_trip(self, browser: Browser) -> None:
        """Somebody opens a link to one invoice, signs in, and must land on that
        invoice -- not on the queue. This is the path a shared link takes, and
        it is the one that broke first."""
        target = f"/invoice/{uuid.uuid4()}"
        callback = sign_in(browser, start=target)
        location = _location(callback)
        assert urlparse(location).netloc == urlparse(CONSOLE).netloc, location
        assert urlparse(location).path == target, location

    def test_the_inbound_and_outbound_origins_agree(self, browser: Browser) -> None:
        """The vacuity guard for this file.

        The redirect_uri sent *to* Keycloak was always built from the configured
        origin, and it is what made Keycloak accept the callback at all. The
        redirect sent back *from* the callback has to name the same host. If the
        two ever diverge, this test is comparing the wrong thing and says so.
        """
        first = browser.get(f"{CONSOLE}/")
        to_keycloak = browser.get(_location(first))
        auth_url = _location(to_keycloak)
        inbound = parse_qs(urlparse(auth_url).query)["redirect_uri"][0]

        callback = sign_in(browser)
        outbound = _location(callback)

        assert urlparse(inbound).netloc == urlparse(outbound).netloc == urlparse(CONSOLE).netloc


class TestTheCallbackDefendsItself:
    def test_a_forged_state_is_refused_and_still_sent_home(self, browser: Browser) -> None:
        """Two claims in one.

        The ``state`` check is what makes the callback unforgeable: without it
        an attacker hands a victim's browser their own authorization code and
        the session belongs to the attacker. And the refusal's redirect had the
        same 0.0.0.0 bug as the success path, so it is asserted to land on the
        console origin as well -- an error page a person cannot reach is an
        error nobody reports.
        """
        first = browser.get(f"{CONSOLE}/")
        to_keycloak = browser.get(_location(first))
        auth_url = _location(to_keycloak)
        page = _follow_within(browser, auth_url, urlparse(KEYCLOAK).netloc)
        match = _FORM_ACTION.search(page.text) or _FORM_ACTION_FALLBACK.search(page.text)
        assert match
        posted = browser.post(
            html.unescape(match.group(1)),
            data={"username": "buyer", "password": "buyer", "credentialId": ""},
        )
        callback_url = _location(posted)

        forged = re.sub(r"state=[^&]+", "state=not-the-state-that-was-issued", callback_url)
        refused = browser.get(forged)
        location = _location(refused)

        assert urlparse(location).netloc == urlparse(CONSOLE).netloc, location
        assert "error=state" in location, location
        assert not browser.holds("threshold_session"), "a forged callback minted a session"
