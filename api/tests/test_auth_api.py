"""Smoke tests for the auth flow: register -> login -> refresh -> protected access.

Covers the highest-risk entry path called out in the structure audit (R3),
plus the B2 logout / refresh-revocation fix, the B3 password-reset flow,
and the B4 rate-limit throttles.
"""
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test.utils import override_settings
from rest_framework.test import APITestCase

from api.models import UserProfile


class RegisterTests(APITestCase):
    def test_register_creates_user_and_returns_tokens(self):
        resp = self.client.post("/auth/register/", {
            "username": "alice",
            "password": "s3cret-pass",
            "identifier_type": "email",
            "identifier": "alice@example.com",
        }, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIn("access", resp.data)
        self.assertIn("refresh", resp.data)
        self.assertTrue(User.objects.filter(username="alice").exists())
        # A UserProfile is created alongside every account.
        user = User.objects.get(username="alice")
        self.assertTrue(UserProfile.objects.filter(user=user).exists())

    def test_duplicate_username_rejected(self):
        User.objects.create(username="bob", password="x")
        resp = self.client.post("/auth/register/", {
            "username": "bob",
            "password": "whatever",
        }, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_missing_fields_rejected(self):
        resp = self.client.post("/auth/register/", {"username": "noPass"}, format="json")
        self.assertEqual(resp.status_code, 400)


class LoginTests(APITestCase):
    def setUp(self):
        self.password = "correct-horse"
        resp = self.client.post("/auth/register/", {
            "username": "carol",
            "password": self.password,
            "identifier_type": "email",
            "identifier": "carol@example.com",
        }, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_login_with_username_succeeds(self):
        resp = self.client.post("/auth/login/", {
            "identifier": "carol",
            "password": self.password,
        }, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIn("access", resp.data)
        self.assertIn("refresh", resp.data)

    def test_login_wrong_password_rejected(self):
        resp = self.client.post("/auth/login/", {
            "identifier": "carol",
            "password": "wrong",
        }, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_token_refresh_issues_new_access(self):
        login = self.client.post("/auth/login/", {
            "identifier": "carol",
            "password": self.password,
        }, format="json")
        refresh = login.data["refresh"]
        resp = self.client.post("/auth/token/refresh/", {"refresh": refresh}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIn("access", resp.data)


class LogoutTests(APITestCase):
    """Covers the B2 fix: /auth/logout/ revokes refresh tokens server-side,
    and BLACKLIST_AFTER_ROTATION=True invalidates rotated-away tokens.

    Without these, a refresh token stayed good for its full 60-day lifetime
    regardless of "sign out" or password change -- a stolen refresh kept
    minting access tokens indefinitely.
    """

    def setUp(self):
        # Clear DRF throttle counters so the B4 register cap (5/min) isn't
        # tripped by accumulated /auth/register/ calls from sibling tests.
        cache.clear()
        self.password = "logout-pass-123"
        reg = self.client.post("/auth/register/", {
            "username": "logout_user",
            "password": self.password,
            "identifier_type": "email",
            "identifier": "logout@example.com",
        }, format="json")
        self.assertEqual(reg.status_code, 200, reg.content)
        self.refresh = reg.data["refresh"]
        self.access = reg.data["access"]

    def test_logout_blacklists_refresh_token(self):
        # The fresh refresh token works once...
        ok = self.client.post(
            "/auth/token/refresh/", {"refresh": self.refresh}, format="json",
        )
        self.assertEqual(ok.status_code, 200, ok.content)
        rotated_refresh = ok.data.get("refresh", self.refresh)

        # ...but after /auth/logout/, the rotated-to refresh can't mint access.
        out = self.client.post(
            "/auth/logout/", {"refresh": rotated_refresh}, format="json",
        )
        self.assertEqual(out.status_code, 200, out.content)

        denied = self.client.post(
            "/auth/token/refresh/", {"refresh": rotated_refresh}, format="json",
        )
        self.assertEqual(denied.status_code, 401, denied.content)

    def test_rotation_invalidates_old_refresh_token(self):
        # Rotation issues a new refresh and blacklists the old one
        # (BLACKLIST_AFTER_ROTATION=True). Without that flag this would 200.
        first = self.client.post(
            "/auth/token/refresh/", {"refresh": self.refresh}, format="json",
        )
        self.assertEqual(first.status_code, 200, first.content)
        self.assertIn("refresh", first.data)
        new_refresh = first.data["refresh"]
        self.assertNotEqual(new_refresh, self.refresh)

        # Try to reuse the original -- must be rejected.
        replay = self.client.post(
            "/auth/token/refresh/", {"refresh": self.refresh}, format="json",
        )
        self.assertEqual(replay.status_code, 401, replay.content)

        # The new one still works.
        ok2 = self.client.post(
            "/auth/token/refresh/", {"refresh": new_refresh}, format="json",
        )
        self.assertEqual(ok2.status_code, 200, ok2.content)

    def test_logout_idempotent_on_missing_token(self):
        # Empty body: there's nothing to revoke, so report success rather
        # than 400 -- the desired end state is already true.
        resp = self.client.post("/auth/logout/", {}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data.get("status"), "ok")

    def test_logout_idempotent_on_garbage_token(self):
        # Malformed string: same reasoning -- caller wants to be logged out,
        # and no real token corresponds to this string.
        resp = self.client.post(
            "/auth/logout/", {"refresh": "not-a-jwt"}, format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_logout_idempotent_on_replay(self):
        # First logout revokes the token; a second logout with the same
        # token must still report success so the client's retry-on-flaky-
        # network doesn't surface as an error.
        first = self.client.post(
            "/auth/logout/", {"refresh": self.refresh}, format="json",
        )
        self.assertEqual(first.status_code, 200, first.content)
        second = self.client.post(
            "/auth/logout/", {"refresh": self.refresh}, format="json",
        )
        self.assertEqual(second.status_code, 200, second.content)

    def test_logout_works_without_access_token(self):
        # The refresh token's own signature authorizes its revocation,
        # so a user whose access token has already expired must still be
        # able to log out. No Authorization header is sent here.
        resp = self.client.post(
            "/auth/logout/", {"refresh": self.refresh}, format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)

        # And the token is now actually revoked.
        denied = self.client.post(
            "/auth/token/refresh/", {"refresh": self.refresh}, format="json",
        )
        self.assertEqual(denied.status_code, 401, denied.content)


class ProtectedEndpointTests(APITestCase):
    def setUp(self):
        # Clear DRF throttle counters so the B4 register cap (5/min) isn't
        # tripped by accumulated /auth/register/ calls from sibling tests.
        cache.clear()
        self.password = "hunter2-pass"
        reg = self.client.post("/auth/register/", {
            "username": "dave",
            "password": self.password,
            "identifier_type": "email",
            "identifier": "dave@example.com",
        }, format="json")
        self.access = reg.data["access"]

    def test_profile_requires_auth(self):
        resp = self.client.get("/auth/profile/")
        self.assertEqual(resp.status_code, 401)

    def test_profile_returns_data_when_authenticated(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.access}")
        resp = self.client.get("/auth/profile/")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIn("posts", resp.data)
        self.assertIn("has_more", resp.data)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class PasswordResetTests(APITestCase):
    """Covers the B3 fix: /auth/password-reset-{request,confirm}/ deliver a
    JSON version of Django's built-in PasswordResetView pair, and the
    confirm step also blacklists outstanding refresh tokens so a reset
    triggered because a token leaked actually closes that hole.
    """

    def setUp(self):
        # Clear DRF throttle counters so the B4 register cap (5/min) isn't
        # tripped by accumulated /auth/register/ calls from sibling tests.
        cache.clear()
        from django.core import mail
        self.password = "old-pass-9876"
        reg = self.client.post("/auth/register/", {
            "username": "reset_user",
            "password": self.password,
            "identifier_type": "email",
            "identifier": "reset@example.com",
        }, format="json")
        self.assertEqual(reg.status_code, 200, reg.content)
        self.refresh = reg.data["refresh"]
        self.access = reg.data["access"]
        mail.outbox = []  # clear anything registration emitted

    # ---------------- /auth/password-reset-request/ ----------------

    def test_request_sends_email_for_known_email(self):
        from django.core import mail
        resp = self.client.post(
            "/auth/password-reset-request/",
            {"identifier": "reset@example.com"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(len(mail.outbox), 1)
        body = mail.outbox[0].body
        self.assertIn("Code:", body)
        self.assertIn("uid=", body)
        self.assertIn("token=", body)

    def test_request_sends_email_when_identifier_is_username(self):
        # Identifier resolver accepts username/email/phone; the request
        # endpoint just hands the raw input through.
        from django.core import mail
        resp = self.client.post(
            "/auth/password-reset-request/",
            {"identifier": "reset_user"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(len(mail.outbox), 1)

    def test_request_returns_200_for_unknown_identifier(self):
        # The endpoint must NOT branch its response on existence -- a
        # different status code or message would let an attacker probe
        # which emails belong to real users.
        from django.core import mail
        resp = self.client.post(
            "/auth/password-reset-request/",
            {"identifier": "ghost@example.com"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(resp.data.get("status"), "ok")

    def test_request_returns_200_for_blank_input(self):
        # Blank identifier is treated as a no-op success (same shape so
        # the response timing/body doesn't visibly branch).
        from django.core import mail
        resp = self.client.post(
            "/auth/password-reset-request/", {}, format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(len(mail.outbox), 0)

    # ---------------- /auth/password-reset-confirm/ ----------------

    def _request_and_extract(self):
        """Trigger a reset and pull (uid, token) out of the email body."""
        from django.core import mail
        self.client.post(
            "/auth/password-reset-request/",
            {"identifier": "reset@example.com"},
            format="json",
        )
        body = mail.outbox[-1].body
        # Email contains both a deep link and a "Code: <uid>.<token>" line.
        # Parse the code form -- it's a stable, machine-friendly source.
        code_line = next(line for line in body.splitlines() if "Code:" in line)
        uid_token = code_line.split("Code:")[1].strip()
        uid, _, token = uid_token.partition(".")
        return uid, token

    def test_confirm_updates_password(self):
        uid, token = self._request_and_extract()
        new_password = "new-pass-abcdef1"
        resp = self.client.post(
            "/auth/password-reset-confirm/",
            {"uid": uid, "token": token, "new_password": new_password},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)

        # Old password no longer logs in.
        bad = self.client.post(
            "/auth/login/",
            {"identifier": "reset_user", "password": self.password},
            format="json",
        )
        self.assertEqual(bad.status_code, 400)
        # New password does.
        ok = self.client.post(
            "/auth/login/",
            {"identifier": "reset_user", "password": new_password},
            format="json",
        )
        self.assertEqual(ok.status_code, 200, ok.content)

    def test_confirm_accepts_combined_code_field(self):
        # Frontend deep-link routing may not be available on every
        # device; the email also prints a "Code: <uid>.<token>" the user
        # can paste as a single field. Confirm accepts that form too.
        uid, token = self._request_and_extract()
        resp = self.client.post(
            "/auth/password-reset-confirm/",
            {"code": f"{uid}.{token}", "new_password": "new-pass-zyxwv2"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_confirm_blacklists_outstanding_refresh_tokens(self):
        # The security-critical step: a reset triggered because a token
        # leaked must invalidate ALL existing refresh tokens for the user.
        uid, token = self._request_and_extract()
        resp = self.client.post(
            "/auth/password-reset-confirm/",
            {"uid": uid, "token": token, "new_password": "post-leak-pass-99"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)

        # The refresh token that was valid before the reset must no
        # longer mint access tokens.
        denied = self.client.post(
            "/auth/token/refresh/", {"refresh": self.refresh}, format="json",
        )
        self.assertEqual(denied.status_code, 401, denied.content)

    def test_confirm_rejects_invalid_token(self):
        uid, _ = self._request_and_extract()
        resp = self.client.post(
            "/auth/password-reset-confirm/",
            {"uid": uid, "token": "bogus", "new_password": "good-pass-77"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        # Error message must NOT distinguish bad-uid from bad-token --
        # either would otherwise narrow an enumeration attempt.
        self.assertIn("Invalid or expired", resp.data.get("error", ""))

    def test_confirm_rejects_invalid_uid(self):
        # Symmetric to the bad-token case; same generic message.
        resp = self.client.post(
            "/auth/password-reset-confirm/",
            {"uid": "not-a-uid", "token": "abc", "new_password": "good-pass-77"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("Invalid or expired", resp.data.get("error", ""))

    def test_confirm_rejects_weak_password(self):
        # AUTH_PASSWORD_VALIDATORS includes MinimumLengthValidator (8
        # chars by default) and CommonPasswordValidator -- confirm runs
        # both via validate_password(). Registration silently bypasses
        # them today (audit H2); this endpoint is the canonical place
        # to start enforcing them.
        uid, token = self._request_and_extract()
        resp = self.client.post(
            "/auth/password-reset-confirm/",
            {"uid": uid, "token": token, "new_password": "abc"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        # Should mention the validation failure, not the token.
        self.assertNotIn("Invalid or expired", resp.data.get("error", ""))

    def test_confirm_token_cannot_be_replayed(self):
        # Token generator hashes the user's password + last_login into
        # the token, so a successful reset auto-invalidates the link.
        uid, token = self._request_and_extract()
        first = self.client.post(
            "/auth/password-reset-confirm/",
            {"uid": uid, "token": token, "new_password": "fresh-pass-aa11"},
            format="json",
        )
        self.assertEqual(first.status_code, 200, first.content)

        # Try the same link again -- must fail with the generic message.
        replay = self.client.post(
            "/auth/password-reset-confirm/",
            {"uid": uid, "token": token, "new_password": "second-pass-bb22"},
            format="json",
        )
        self.assertEqual(replay.status_code, 400, replay.content)
        self.assertIn("Invalid or expired", replay.data.get("error", ""))

    def test_confirm_rejects_missing_fields(self):
        resp = self.client.post(
            "/auth/password-reset-confirm/", {}, format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.content)


# ---------------------------------------------------------------------------
# B4: rate-limit / throttle tests
# ---------------------------------------------------------------------------
#
# Each test class clears the throttle counters in setUp() (DRF stores them
# in `caches['default']`, the same cache the rest of the app uses) so a
# previous test can't pre-fill the bucket and skew this one. We override
# the rates to very low values so triggering the throttle takes 2-3
# requests instead of the production limit -- avoids time.sleep-style
# tests that would make the suite slow.
#
# The throttle classes themselves live in api/services/throttles.py and
# are wired into views via `@throttle_classes([...])`. The scopes match
# the keys in REST_FRAMEWORK['DEFAULT_THROTTLE_RATES'] (settings.py), so
# the override below also changes which scopes the throttle classes look
# up at request time -- no monkey-patching needed.
#
# Note on coverage: these tests exercise the AUTH endpoints because they
# are the cleanest to test (no fixtures, no DB setup). The non-auth
# throttles (FollowRateThrottle, SendMessageRateThrottle, etc.) use the
# same DRF UserRateThrottle plumbing keyed by `request.user.pk`; the
# proof of mechanism transfers. If you want explicit coverage for those
# too, copy the pattern below with a tiny rate override and one POST per
# endpoint.


def _throttle_test_rates():
    """Aggressive rates that trip after just 2 successful calls.

    Keep `password_reset_request` at exactly 2 so a third call within
    the same minute returns 429. The pattern transfers to any scope.
    """
    return {
        # Globals -- set high so they don't fire before the scoped one.
        'anon': '1000/min',
        'user': '1000/min',
        # Tight per-scope caps the tests below depend on.
        'auth_login':              '2/min',
        'auth_register':           '2/min',
        'auth_social':             '2/min',
        'auth_logout':             '2/min',
        'password_reset_request':  '2/min',
        'password_reset_confirm':  '2/min',
        'follow':                  '2/min',
        'send_message':            '2/min',
        'post_create':             '2/min',
        'comment_create':          '2/min',
        'post_engagement':         '2/min',
        'report':                  '2/min',
        'page_invite':             '2/min',
    }


@override_settings(REST_FRAMEWORK={
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ),
    'DEFAULT_THROTTLE_CLASSES': (
        'rest_framework.throttling.AnonRateThrottle',
        'rest_framework.throttling.UserRateThrottle',
    ),
    'DEFAULT_THROTTLE_RATES': _throttle_test_rates(),
})
class RateLimitTests(APITestCase):
    """Covers the B4 fix: sensitive endpoints reject excess requests with
    HTTP 429 instead of accepting unbounded floods.

    Each test posts up to the cap with payloads we expect to be REJECTED
    by the view (bad credentials, missing fields) so we're testing the
    throttle, not creating a side effect like a real account. Throttle
    decisions happen BEFORE the view runs, so a 400-from-the-view counts
    as "the throttle let me through" -- exactly what we want.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # DRF gotcha: `SimpleRateThrottle.THROTTLE_RATES` is bound as a
        # CLASS attribute to `api_settings.DEFAULT_THROTTLE_RATES` at the
        # moment the throttling module is imported. After that, the class
        # attribute holds a reference to the ORIGINAL settings dict.
        # `@override_settings(REST_FRAMEWORK={...})` does fire DRF's
        # `reload_api_settings` handler, which clears `api_settings`'
        # cached attrs -- but the throttle class's own `THROTTLE_RATES`
        # attribute is never rebound, so per-request throttle
        # construction keeps reading the production rates.
        #
        # Fix: swap the throttle class attribute directly for the
        # lifetime of the test class. AnonRateThrottle and
        # UserRateThrottle both inherit `THROTTLE_RATES` from
        # SimpleRateThrottle, so patching the base class propagates to
        # every subclass via MRO without us having to enumerate them.
        # tearDownClass restores the original so this can't leak into
        # later test classes if the runner reuses the process.
        from rest_framework.throttling import SimpleRateThrottle
        cls._original_throttle_rates = SimpleRateThrottle.THROTTLE_RATES
        SimpleRateThrottle.THROTTLE_RATES = _throttle_test_rates()

    @classmethod
    def tearDownClass(cls):
        from rest_framework.throttling import SimpleRateThrottle
        SimpleRateThrottle.THROTTLE_RATES = cls._original_throttle_rates
        super().tearDownClass()

    def setUp(self):
        # DRF throttles store counters in caches['default']. Clearing in
        # setUp keeps tests independent: a previous test's 429 doesn't
        # carry over to this one.
        cache.clear()

    def _post(self, url, body):
        return self.client.post(url, body, format="json")

    # ---------------- Anonymous / auth-entry endpoints ----------------

    def test_login_throttle_kicks_in(self):
        url = "/auth/login/"
        body = {"identifier": "nobody", "password": "wrong"}
        # Cap is 2/min; first two calls return the view's normal 400
        # (invalid credentials), third returns 429.
        self.assertEqual(self._post(url, body).status_code, 400)
        self.assertEqual(self._post(url, body).status_code, 400)
        self.assertEqual(self._post(url, body).status_code, 429)

    def test_register_throttle_kicks_in(self):
        url = "/auth/register/"
        body = {"username": ""}  # rejected by the view; doesn't create a row
        self.assertEqual(self._post(url, body).status_code, 400)
        self.assertEqual(self._post(url, body).status_code, 400)
        self.assertEqual(self._post(url, body).status_code, 429)

    def test_social_auth_throttle_kicks_in(self):
        url = "/auth/social/"
        body = {"provider": "unsupported"}
        self.assertEqual(self._post(url, body).status_code, 400)
        self.assertEqual(self._post(url, body).status_code, 400)
        self.assertEqual(self._post(url, body).status_code, 429)

    def test_password_reset_request_throttle_kicks_in(self):
        # This one is special: the view ALWAYS returns 200 (enumeration
        # protection from B3), so before the cap the assertion is 200 and
        # the third call is the 429.
        url = "/auth/password-reset-request/"
        body = {"identifier": "ghost@example.com"}
        self.assertEqual(self._post(url, body).status_code, 200)
        self.assertEqual(self._post(url, body).status_code, 200)
        self.assertEqual(self._post(url, body).status_code, 429)

    def test_password_reset_confirm_throttle_kicks_in(self):
        url = "/auth/password-reset-confirm/"
        body = {"uid": "x", "token": "y", "new_password": "irrelevant"}
        # First two: rejected by the view (invalid token) with 400.
        self.assertEqual(self._post(url, body).status_code, 400)
        self.assertEqual(self._post(url, body).status_code, 400)
        self.assertEqual(self._post(url, body).status_code, 429)

    def test_logout_throttle_kicks_in(self):
        # Logout is idempotent (always 200), so this verifies the cap
        # kicks in on the third call regardless of token validity.
        url = "/auth/logout/"
        body = {"refresh": "garbage"}
        self.assertEqual(self._post(url, body).status_code, 200)
        self.assertEqual(self._post(url, body).status_code, 200)
        self.assertEqual(self._post(url, body).status_code, 429)

    # ---------------- Throttle bucket isolation ----------------

    def test_throttle_is_per_scope_not_global(self):
        # Hitting login twice (filling THAT bucket) must NOT also fill
        # register's bucket -- they're separately scoped.
        login_url = "/auth/login/"
        register_url = "/auth/register/"
        self._post(login_url, {"identifier": "x", "password": "y"})
        self._post(login_url, {"identifier": "x", "password": "y"})
        # Login is now at the cap.
        self.assertEqual(
            self._post(login_url, {"identifier": "x", "password": "y"}).status_code,
            429,
        )
        # Register still has its full budget.
        self.assertEqual(
            self._post(register_url, {"username": ""}).status_code,
            400,
        )

    # ---------------- Authenticated write endpoints ----------------

    def test_follow_throttle_kicks_in_per_user(self):
        # UserRateThrottle subclasses key on user.pk -- the cap is
        # per-account, not per-IP, so a botnet rotating IPs from one
        # account still hits the wall. We test that here by posting
        # follow toggles as one user and watching the third get 429.
        from rest_framework_simplejwt.tokens import RefreshToken
        actor = User.objects.create_user(
            username="follow_actor", password="x", email="actor@example.com",
        )
        UserProfile.objects.create(user=actor)
        target = User.objects.create_user(
            username="follow_target", password="x", email="target@example.com",
        )
        UserProfile.objects.create(user=target)
        token = str(RefreshToken.for_user(actor).access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        url = "/auth/follow/"
        body = {"user_id": target.id}
        # First two calls succeed (toggle on, toggle off -- both 2xx);
        # third call is the throttle.
        self.assertIn(self._post(url, body).status_code, (200, 201))
        self.assertIn(self._post(url, body).status_code, (200, 201))
        self.assertEqual(self._post(url, body).status_code, 429)

    def test_page_chat_send_throttle_kicks_in_per_user(self):
        """B4-1: /pages/chat/send/ has the same SendMessageRateThrottle
        as /auth/send-message/. The two endpoints have the same spam +
        push-fanout attack profile, but the page-chat one was missed
        in the initial B4 pass."""
        from rest_framework_simplejwt.tokens import RefreshToken
        from api.models import Page
        sender = User.objects.create_user(
            username="pc_throttle_sender", password="x",
        )
        UserProfile.objects.create(user=sender)
        # Owner-of-page bypasses the membership / chat_enabled checks,
        # so a single user can drive the throttle in isolation.
        page = Page.objects.create(
            owner=sender, name="Throttle Test", chat_enabled=True,
        )
        token = str(RefreshToken.for_user(sender).access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        url = "/pages/chat/send/"
        body = {"page_id": page.id, "text": "hi"}
        # Cap is 2/min (test override); third call must 429.
        self.assertEqual(self._post(url, body).status_code, 201)
        self.assertEqual(self._post(url, body).status_code, 201)
        self.assertEqual(self._post(url, body).status_code, 429)


# ---------------------------------------------------------------------------
# H2: registration password-strength enforcement
# ---------------------------------------------------------------------------
#
# /auth/register/ now runs the new password through Django's
# AUTH_PASSWORD_VALIDATORS via validate_password() before creating the user.
# Previously make_password() was called directly, so a 1-char password
# would happily produce a real account. The validator config in settings.py
# raises the MinimumLengthValidator floor to 10 chars (Django default 8).
#
# setUp() clears `caches['default']` so the B4 register throttle (5/min)
# can't carry over from an earlier test and trip the 6th call here.


class RegistrationPasswordValidationTests(APITestCase):
    """Covers the H2 fix: registration runs AUTH_PASSWORD_VALIDATORS."""

    def setUp(self):
        # B4 register throttle is 5/min keyed in caches['default'];
        # without this, a previous test's calls plus this class's 4
        # registration attempts could trip the throttle and return 429
        # instead of the validator's 400. See RateLimitTests.setUp.
        cache.clear()

    def _register(self, password, username="newuser", email="new@example.com"):
        return self.client.post("/auth/register/", {
            "username": username,
            "password": password,
            "identifier_type": "email",
            "identifier": email,
        }, format="json")

    def test_short_password_rejected(self):
        # 7 chars: below the configured min_length of 10.
        resp = self._register("abc1234")
        self.assertEqual(resp.status_code, 400, resp.content)
        error = (resp.data.get("error") or "").lower()
        self.assertIn("short", error + " ")  # Django: "too short"
        # And no user row was created.
        self.assertFalse(User.objects.filter(username="newuser").exists())

    def test_common_password_rejected(self):
        # "password123" ships in Django's bundled common-password list.
        resp = self._register("password123")
        self.assertEqual(resp.status_code, 400, resp.content)
        error = (resp.data.get("error") or "").lower()
        self.assertIn("common", error)
        self.assertFalse(User.objects.filter(username="newuser").exists())

    def test_numeric_only_password_rejected(self):
        # 10 chars (passes length) but entirely numeric -> NumericPasswordValidator.
        resp = self._register("1234567890")
        self.assertEqual(resp.status_code, 400, resp.content)
        error = (resp.data.get("error") or "").lower()
        # Django's message: "This password is entirely numeric."
        self.assertIn("numeric", error)
        self.assertFalse(User.objects.filter(username="newuser").exists())

    def test_strong_password_accepted(self):
        # 15 chars, mixed alphanumerics with separators -- passes all 4
        # validators, so registration succeeds and tokens come back.
        resp = self._register("solid-pass-9xyz")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIn("access", resp.data)
        self.assertIn("refresh", resp.data)
        self.assertTrue(User.objects.filter(username="newuser").exists())


# ---------------------------------------------------------------------------
# H3: registration TOCTOU race handling
# ---------------------------------------------------------------------------
#
# Two simultaneous register calls with the same username (or, post-migration
# 0099, the same email/phone) can both pass the .exists() pre-check and
# both try to insert. The losing insert raises IntegrityError, which the
# view now catches and turns into a 400 (previously it became a 500).
#
# The tests below exercise BOTH layers:
#   1. The migration's DB-level constraints actually fire on a duplicate
#      insert (direct ORM-level tests, no view).
#   2. The view turns IntegrityError into 400 with the correct
#      identifier-specific message (mocked-create tests).


class RegistrationConstraintTests(APITestCase):
    """Direct DB tests for the migration 0099 partial unique indexes
    on auth_user.email and api_userprofile.phone_number."""

    def test_duplicate_email_blocked_at_db_level(self):
        from django.db import IntegrityError
        User.objects.create_user(
            username="u1", email="dup@example.com", password="x",
        )
        # H3: second insert with the same non-empty email must raise.
        with self.assertRaises(IntegrityError):
            User.objects.create_user(
                username="u2", email="dup@example.com", password="x",
            )

    def test_blank_emails_allowed_multiple(self):
        # Partial index condition is `email <> ''`, so blanks aren't
        # constrained -- two users registering via phone (no email)
        # must coexist.
        User.objects.create_user(username="u1", email="", password="x")
        User.objects.create_user(username="u2", email="", password="x")
        self.assertEqual(User.objects.filter(email="").count(), 2)

    def test_duplicate_phone_blocked_at_db_level(self):
        from django.db import IntegrityError
        u1 = User.objects.create_user(username="u1", password="x")
        UserProfile.objects.create(user=u1, phone_number="+15551234567")
        u2 = User.objects.create_user(username="u2", password="x")
        with self.assertRaises(IntegrityError):
            UserProfile.objects.create(user=u2, phone_number="+15551234567")

    def test_blank_phones_allowed_multiple(self):
        u1 = User.objects.create_user(username="u1", password="x")
        UserProfile.objects.create(user=u1, phone_number="")
        u2 = User.objects.create_user(username="u2", password="x")
        UserProfile.objects.create(user=u2, phone_number="")
        self.assertEqual(
            UserProfile.objects.filter(phone_number="").count(), 2,
        )


class RegistrationRaceHandlingTests(APITestCase):
    """The view-level half of the H3 backstop: an IntegrityError from
    .create() must surface as 400, not 500. We simulate the TOCTOU race
    by mocking User.objects.create to raise (which is exactly what
    the DB does when the migration 0099 constraints fire on the
    losing insert in a real race)."""

    def setUp(self):
        # B4 register throttle is 5/min; each test in this class fires
        # one register call, but cumulative across the suite they trip
        # the bucket without a clear.
        cache.clear()

    def test_username_race_returns_400_not_500(self):
        from unittest.mock import patch
        from django.db import IntegrityError

        # Pre-create the colliding row so the re-check in the except
        # handler can identify the conflict and return the
        # username-specific message.
        User.objects.create_user(username="race_name", password="x")

        # Force the create to raise IntegrityError despite the
        # .exists() pre-check having returned True (which would have
        # short-circuited with a 400 anyway). To get past the pre-check
        # AND hit the create, mock the pre-check too: the simplest
        # equivalent is to mock create directly with a NEW username
        # that the pre-check sees as free.
        with patch("api.views.auth.User.objects.create") as mock_create:
            mock_create.side_effect = IntegrityError(
                "UNIQUE constraint failed: auth_user.username"
            )
            resp = self.client.post("/auth/register/", {
                "username": "race_name",  # matches pre-existing for re-check
                "password": "strong-pass-9999",
            }, format="json")

        # The pre-check would normally return 400 here without ever
        # reaching create. But ANY IntegrityError must NOT 500 the API,
        # so this test guards both the pre-check AND the backstop.
        self.assertNotEqual(resp.status_code, 500, resp.content)
        self.assertEqual(resp.status_code, 400)

    def test_email_race_returns_400_not_500(self):
        from unittest.mock import patch
        from django.db import IntegrityError

        User.objects.create_user(
            username="other", email="race@example.com", password="x",
        )
        with patch("api.views.auth.User.objects.create") as mock_create:
            mock_create.side_effect = IntegrityError(
                "UNIQUE constraint failed: auth_user.email"
            )
            resp = self.client.post("/auth/register/", {
                "username": "fresh_name",
                "password": "strong-pass-9999",
                "identifier_type": "email",
                "identifier": "race@example.com",
            }, format="json")

        self.assertNotEqual(resp.status_code, 500, resp.content)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data.get("error"), "Email already in use")

    def test_phone_race_returns_400_not_500(self):
        from unittest.mock import patch
        from django.db import IntegrityError

        existing = User.objects.create_user(username="other2", password="x")
        UserProfile.objects.create(
            user=existing, phone_number="+15551234567",
        )
        # User row creates fine; UserProfile.create is what races.
        with patch(
            "api.views.auth.UserProfile.objects.create"
        ) as mock_create:
            mock_create.side_effect = IntegrityError(
                "UNIQUE constraint failed: api_userprofile.phone_number"
            )
            resp = self.client.post("/auth/register/", {
                "username": "fresh_name_2",
                "password": "strong-pass-9999",
                "identifier_type": "phone",
                "identifier": "+15551234567",
            }, format="json")

        self.assertNotEqual(resp.status_code, 500, resp.content)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(
            resp.data.get("error"), "Phone number already in use",
        )

    def test_unidentified_integrityerror_returns_409_not_500(self):
        """If the race winner row got deleted between our race and our
        re-check (rare), the re-check finds nothing and falls back to a
        generic 409. Still not 500."""
        from unittest.mock import patch
        from django.db import IntegrityError

        # No pre-existing row -- the re-check will find nothing.
        with patch("api.views.auth.User.objects.create") as mock_create:
            mock_create.side_effect = IntegrityError(
                "UNIQUE constraint failed"
            )
            resp = self.client.post("/auth/register/", {
                "username": "no_match",
                "password": "strong-pass-9999",
            }, format="json")

        self.assertNotEqual(resp.status_code, 500, resp.content)
        self.assertEqual(resp.status_code, 409)
