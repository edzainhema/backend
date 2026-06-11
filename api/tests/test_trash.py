"""Focused tests for the page/post trash (soft delete) — Phases 1 & 2.

Run just these while iterating:
    python manage.py test api.tests.test_trash

Covers: page soft delete + post cascade + contributor notification
(delete_page / teardown_page), individual post soft delete (delete_post), the
trashed-post listing, restore_page, and the purge (permanent-delete) endpoints —
plus that the default managers hide trashed content while all_objects sees it,
and that a purged page leaves its (trashed) posts behind via SET_NULL.
"""
import io
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APITestCase

from api.models import Notification, Page, Post


def _register(client, username):
    resp = client.post("/auth/register/", {
        "username": username,
        "password": "strong-pass-9999",
        "identifier_type": "email",
        "identifier": f"{username}@example.com",
    }, format="json")
    assert resp.status_code == 200, resp.content
    return resp.data["access"]


class TrashTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.admin_access = _register(self.client, "admin")
        self.admin = User.objects.get(username="admin")
        self.contrib_access = _register(self.client, "contrib")
        self.contrib = User.objects.get(username="contrib")

        self.page = Page.objects.create(owner=self.admin, name="My Page")
        self.admin_post = Post.objects.create(user=self.admin, page=self.page)
        self.contrib_post = Post.objects.create(user=self.contrib, page=self.page)

    def _as(self, access):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

    # ── delete_page: soft delete + cascade + notify (a, b, c) ──────────────
    def test_delete_page_soft_deletes_cascades_and_notifies(self):
        self._as(self.admin_access)
        resp = self.client.post("/pages/delete/", {"page_id": self.page.id}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)

        # Page row survives, trashed, hidden from the default manager.
        page = Page.all_objects.get(id=self.page.id)
        self.assertIsNotNone(page.deleted_at)
        self.assertFalse(Page.objects.filter(id=self.page.id).exists())

        # Both posts moved to trash (reason page_deleted), hidden from objects.
        for p in (self.admin_post, self.contrib_post):
            row = Post.all_objects.get(id=p.id)
            self.assertIsNotNone(row.trashed_at)
            self.assertEqual(row.trashed_reason, "page_deleted")
        self.assertFalse(
            Post.objects.filter(id__in=[self.admin_post.id, self.contrib_post.id]).exists()
        )

        # Contributor is notified; the admin (the actor) is NOT.
        self.assertTrue(Notification.objects.filter(
            recipient=self.contrib, notification_type="page_deleted", page_id=self.page.id,
        ).exists())
        self.assertFalse(Notification.objects.filter(
            recipient=self.admin, notification_type="page_deleted",
        ).exists())

    def test_delete_page_requires_owner(self):
        self._as(self.contrib_access)
        resp = self.client.post("/pages/delete/", {"page_id": self.page.id}, format="json")
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(Page.objects.filter(id=self.page.id).exists())

    # ── delete_post: soft delete (reason self) ─────────────────────────────
    def test_delete_post_soft_deletes(self):
        self._as(self.contrib_access)
        resp = self.client.post("/posts/delete/", {"post_id": self.contrib_post.id}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        row = Post.all_objects.get(id=self.contrib_post.id)
        self.assertIsNotNone(row.trashed_at)
        self.assertEqual(row.trashed_reason, "self")
        self.assertFalse(Post.objects.filter(id=self.contrib_post.id).exists())
        # already trashed → hidden from objects → second delete 404s
        resp2 = self.client.post("/posts/delete/", {"post_id": self.contrib_post.id}, format="json")
        self.assertEqual(resp2.status_code, 404)

    def test_cannot_delete_others_post(self):
        self._as(self.contrib_access)
        resp = self.client.post("/posts/delete/", {"post_id": self.admin_post.id}, format="json")
        self.assertEqual(resp.status_code, 404)

    # ── delete_post: bulk path (Profile multi-select) ──────────────────────
    def test_bulk_delete_posts(self):
        self._as(self.contrib_access)
        # Two more of the contributor's own posts to bulk-trash alongside the
        # first; the admin's post is included to prove ownership scoping.
        extra1 = Post.objects.create(user=self.contrib, page=self.page)
        extra2 = Post.objects.create(user=self.contrib, page=self.page)
        resp = self.client.post(
            "/posts/delete/",
            {"post_ids": [self.contrib_post.id, extra1.id, extra2.id, self.admin_post.id]},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        # Only the caller's three live posts are trashed in one call; the admin's
        # post isn't the caller's, so it's skipped.
        self.assertEqual(resp.data["count"], 3)
        for p in (self.contrib_post, extra1, extra2):
            row = Post.all_objects.get(id=p.id)
            self.assertIsNotNone(row.trashed_at)
            self.assertEqual(row.trashed_reason, "self")
        self.assertFalse(
            Post.objects.filter(id__in=[self.contrib_post.id, extra1.id, extra2.id]).exists()
        )
        self.assertTrue(Post.objects.filter(id=self.admin_post.id).exists())
        # Idempotent: re-trashing already-trashed ids trashes nothing new.
        again = self.client.post(
            "/posts/delete/",
            {"post_ids": [self.contrib_post.id, extra1.id]},
            format="json",
        )
        self.assertEqual(again.status_code, 200, again.content)
        self.assertEqual(again.data["count"], 0)

    # ── list_trashed_posts ─────────────────────────────────────────────────
    def test_list_trashed_posts(self):
        self._as(self.contrib_access)
        self.client.post("/posts/delete/", {"post_id": self.contrib_post.id}, format="json")
        resp = self.client.get("/posts/trash/")
        self.assertEqual(resp.status_code, 200, resp.content)
        ids = [p["id"] for p in resp.data["results"]]
        self.assertEqual(ids, [self.contrib_post.id])

    # ── purge_post: trash-only + owner-only ────────────────────────────────
    def test_purge_post_requires_trashed_then_deletes(self):
        self._as(self.contrib_access)
        live = self.client.post("/posts/purge/", {"post_id": self.contrib_post.id}, format="json")
        self.assertEqual(live.status_code, 400)
        self.client.post("/posts/delete/", {"post_id": self.contrib_post.id}, format="json")
        gone = self.client.post("/posts/purge/", {"post_id": self.contrib_post.id}, format="json")
        self.assertEqual(gone.status_code, 200, gone.content)
        self.assertFalse(Post.all_objects.filter(id=self.contrib_post.id).exists())

    def test_purge_post_owner_only(self):
        self._as(self.contrib_access)
        self.client.post("/posts/delete/", {"post_id": self.contrib_post.id}, format="json")
        self._as(self.admin_access)
        resp = self.client.post("/posts/purge/", {"post_id": self.contrib_post.id}, format="json")
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(Post.all_objects.filter(id=self.contrib_post.id).exists())

    # ── restore_page (does NOT auto-restore posts yet — that's a later phase) ─
    def test_restore_page(self):
        self._as(self.admin_access)
        self.client.post("/pages/delete/", {"page_id": self.page.id}, format="json")
        self.assertFalse(Page.objects.filter(id=self.page.id).exists())
        resp = self.client.post("/pages/restore/", {"page_id": self.page.id}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(Page.objects.filter(id=self.page.id).exists())
        self.assertIsNone(Page.all_objects.get(id=self.page.id).deleted_at)

    # ── purge_page: trash-only; survivors detach via SET_NULL (d, e) ────────
    def test_purge_page_requires_trash_and_detaches_posts(self):
        self._as(self.admin_access)
        live = self.client.post("/pages/purge/", {"page_id": self.page.id}, format="json")
        self.assertEqual(live.status_code, 400)

        self.client.post("/pages/delete/", {"page_id": self.page.id}, format="json")
        gone = self.client.post("/pages/purge/", {"page_id": self.page.id}, format="json")
        self.assertEqual(gone.status_code, 200, gone.content)
        self.assertFalse(Page.all_objects.filter(id=self.page.id).exists())

        # The contributor's post survives the page purge: detached (page_id null)
        # but still in trash for them.
        row = Post.all_objects.get(id=self.contrib_post.id)
        self.assertIsNone(row.page_id)
        self.assertIsNotNone(row.trashed_at)

    # ── restore_post: into a page (Phase 3) ────────────────────────────────
    def test_restore_post_into_page(self):
        # Trash the contributor's post, then restore it into a page they can
        # post in (the page is anyone_can_post by default).
        self._as(self.contrib_access)
        self.client.post("/posts/delete/", {"post_id": self.contrib_post.id}, format="json")
        resp = self.client.post(
            "/posts/restore/",
            {"post_id": self.contrib_post.id, "page_id": self.page.id},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        row = Post.all_objects.get(id=self.contrib_post.id)
        self.assertIsNone(row.trashed_at)
        self.assertEqual(row.trashed_reason, "")
        self.assertEqual(row.page_id, self.page.id)
        # Live again → visible from the default manager.
        self.assertTrue(Post.objects.filter(id=self.contrib_post.id).exists())

    def test_restore_post_requires_trashed(self):
        self._as(self.contrib_access)
        resp = self.client.post(
            "/posts/restore/",
            {"post_id": self.contrib_post.id, "page_id": self.page.id},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_restore_post_rejects_page_you_cant_post_in(self):
        # A locked-down page the contributor isn't an allowed poster on.
        locked = Page.objects.create(
            owner=self.admin, name="Locked", anyone_can_post=False
        )
        self._as(self.contrib_access)
        self.client.post("/posts/delete/", {"post_id": self.contrib_post.id}, format="json")
        resp = self.client.post(
            "/posts/restore/",
            {"post_id": self.contrib_post.id, "page_id": locked.id},
            format="json",
        )
        self.assertEqual(resp.status_code, 403)
        # Still trashed (restore was rejected).
        self.assertIsNotNone(Post.all_objects.get(id=self.contrib_post.id).trashed_at)

    # ── Phase 4: admin's own-media flags on purge / restore ────────────────
    def _trash_page(self):
        self._as(self.admin_access)
        self.client.post("/pages/delete/", {"page_id": self.page.id}, format="json")

    def test_purge_page_with_own_media_deletes_admin_posts_only(self):
        self._trash_page()
        resp = self.client.post(
            "/pages/purge/",
            {"page_id": self.page.id, "purge_own_media": True},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        # Admin's post permanently gone.
        self.assertFalse(Post.all_objects.filter(id=self.admin_post.id).exists())
        # Contributor's post survives (detached, still in their trash).
        row = Post.all_objects.get(id=self.contrib_post.id)
        self.assertIsNone(row.page_id)
        self.assertIsNotNone(row.trashed_at)

    def test_purge_page_keeps_own_media_by_default(self):
        self._trash_page()
        resp = self.client.post("/pages/purge/", {"page_id": self.page.id}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        admin_row = Post.all_objects.get(id=self.admin_post.id)
        self.assertIsNone(admin_row.page_id)
        self.assertIsNotNone(admin_row.trashed_at)  # still in admin's trash

    def test_restore_page_with_own_media_restores_admin_posts_only(self):
        self._trash_page()
        resp = self.client.post(
            "/pages/restore/",
            {"page_id": self.page.id, "restore_own_media": True},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        # Admin's post restored back into the page.
        admin_row = Post.all_objects.get(id=self.admin_post.id)
        self.assertIsNone(admin_row.trashed_at)
        self.assertEqual(admin_row.page_id, self.page.id)
        self.assertTrue(Post.objects.filter(id=self.admin_post.id).exists())
        # Contributor's post stays trashed (interactive prompt is Phase 5).
        self.assertIsNotNone(Post.all_objects.get(id=self.contrib_post.id).trashed_at)

    def test_restore_page_keeps_own_media_by_default(self):
        self._trash_page()
        resp = self.client.post("/pages/restore/", {"page_id": self.page.id}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIsNotNone(Post.all_objects.get(id=self.admin_post.id).trashed_at)

    # ── Phase 5: interactive contributor restore ───────────────────────────
    def test_restore_page_notifies_contributors(self):
        self._trash_page()  # trashes both posts + (Phase 2) notifies contrib
        # Restore the page → contributors with page_deleted posts get a
        # page_restored notification; the admin (actor) does not.
        self.client.post("/pages/restore/", {"page_id": self.page.id}, format="json")
        self.assertTrue(Notification.objects.filter(
            recipient=self.contrib, notification_type="page_restored", page_id=self.page.id,
        ).exists())
        self.assertFalse(Notification.objects.filter(
            recipient=self.admin, notification_type="page_restored",
        ).exists())

    def test_restore_my_page_media(self):
        self._trash_page()
        self.client.post("/pages/restore/", {"page_id": self.page.id}, format="json")
        # Contributor accepts: restore their posts back into the now-live page.
        self._as(self.contrib_access)
        resp = self.client.post(
            "/pages/restore-my-media/", {"page_id": self.page.id}, format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data.get("count"), 1)
        row = Post.all_objects.get(id=self.contrib_post.id)
        self.assertIsNone(row.trashed_at)
        self.assertEqual(row.page_id, self.page.id)
        self.assertTrue(Post.objects.filter(id=self.contrib_post.id).exists())
        # The prompt is resolved: their page_restored notification is read.
        self.assertTrue(Notification.objects.filter(
            recipient=self.contrib, notification_type="page_restored",
            page_id=self.page.id, is_read=True,
        ).exists())

    def test_restore_my_page_media_requires_live_page(self):
        # Page never restored (still trashed) → not reachable via Page.objects.
        self._trash_page()
        self._as(self.contrib_access)
        resp = self.client.post(
            "/pages/restore-my-media/", {"page_id": self.page.id}, format="json",
        )
        self.assertEqual(resp.status_code, 404)

    # ── Phase 6: retention sweep + account-deletion teardown ───────────────
    def test_retention_purges_only_expired_trash(self):
        self._as(self.contrib_access)
        self.client.post("/posts/delete/", {"post_id": self.contrib_post.id}, format="json")
        # Backdate the contributor's trashed post past the window.
        Post.all_objects.filter(id=self.contrib_post.id).update(
            trashed_at=timezone.now() - timedelta(days=100),
        )
        # The admin's post is freshly trashed (inside the window).
        self._as(self.admin_access)
        self.client.post("/posts/delete/", {"post_id": self.admin_post.id}, format="json")

        call_command("purge_expired_trash", stdout=io.StringIO())

        self.assertFalse(Post.all_objects.filter(id=self.contrib_post.id).exists())
        self.assertTrue(Post.all_objects.filter(id=self.admin_post.id).exists())

    def test_retention_purges_expired_trashed_page(self):
        self._as(self.admin_access)
        self.client.post("/pages/delete/", {"page_id": self.page.id}, format="json")
        Page.all_objects.filter(id=self.page.id).update(
            deleted_at=timezone.now() - timedelta(days=100),
        )
        call_command("purge_expired_trash", stdout=io.StringIO())
        self.assertFalse(Page.all_objects.filter(id=self.page.id).exists())

    def test_account_deletion_trashes_contributors_live_posts(self):
        # The admin owns a LIVE page holding a contributor's LIVE post. Deleting
        # the admin's account must trash the contributor's post (page_deleted),
        # NOT orphan it into a page-less live post via SET_NULL.
        self.admin.delete()
        row = Post.all_objects.get(id=self.contrib_post.id)
        self.assertIsNotNone(row.trashed_at)
        self.assertEqual(row.trashed_reason, "page_deleted")
        # The admin's own post went with the account.
        self.assertFalse(Post.all_objects.filter(id=self.admin_post.id).exists())
