"""Smoke tests for the auth flow: register -> login -> refresh -> protected access.

Covers the highest-risk entry path called out in the structure audit (R3).
"""
from django.contrib.auth.models import User
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


class ProtectedEndpointTests(APITestCase):
    def setUp(self):
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
