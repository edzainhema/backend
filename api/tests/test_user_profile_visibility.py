"""Tests for the M4 fix on /auth/user-profile/ (get_user_profile).

The profile grid must respect PER-POST visibility, not just the target
user's account-level privacy. Before this fix, the view returned every
post by `target_user` once `can_view_posts` was True -- including
posts on private pages the viewer couldn't access. That leaked the
post's media + caption (the grid renders thumbnails) even when tapping
through would 404.

These tests run as a third-party viewer hitting target's profile, with
target posting into a mix of private / public / public-override pages.
The expected behavior: only posts the viewer would also see in their
feed (per `post_visibility_q`) appear in the grid.
"""
from django.contrib.auth.models import User
from django.core.cache import cache
from rest_framework.test import APITestCase

from api.models import Follow, Page, PageFollow, Post


def _register(client, username: str) -> str:
    resp = client.post("/auth/register/", {
        "username": username,
        "password": "strong-pass-9999",
        "identifier_type": "email",
        "identifier": f"{username}@example.com",
    }, format="json")
    assert resp.status_code == 200, resp.content
    return resp.data["access"]


class GetUserProfileVisibilityTests(APITestCase):
    """M4: profile grid must filter posts via `post_visibility_q`."""

    def setUp(self):
        # B4 register throttle clear; same pattern as siblings.
        cache.clear()
        self.viewer_access = _register(self.client, "viewer")
        self.viewer = User.objects.get(username="viewer")
        self.target_access = _register(self.client, "target")
        self.target = User.objects.get(username="target")

        # Two pages target owns: one public, one private. Posts on
        # each plus a no-page (personal) post give us all the
        # visibility branches `post_visibility_q` cares about.
        self.public_page = Page.objects.create(
            owner=self.target, name="Open", is_private=False,
        )
        self.private_page = Page.objects.create(
            owner=self.target, name="Closed", is_private=True,
        )

        self.personal_post = Post.objects.create(
            user=self.target, description="no page",
        )
        self.public_page_post = Post.objects.create(
            user=self.target, page=self.public_page,
            description="on public page",
        )
        self.private_page_post = Post.objects.create(
            user=self.target, page=self.private_page,
            description="on private page",
        )

    def _auth(self, access: str):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

    def _get_profile(self, target_user_id=None):
        if target_user_id is None:
            target_user_id = self.target.id
        return self.client.get(
            f"/auth/user-profile/?user_id={target_user_id}",
        )

    def _post_ids(self, resp):
        return {p["id"] for p in resp.data.get("posts", [])}

    # ---------- per-post visibility ----------

    def test_personal_post_visible(self):
        """No-page personal post by a public-account target -- always
        visible to anyone allowed to see the profile."""
        self._auth(self.viewer_access)
        resp = self._get_profile()
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIn(self.personal_post.id, self._post_ids(resp))

    def test_public_page_post_visible(self):
        """Posts on a fully-public page are visible to anyone."""
        self._auth(self.viewer_access)
        resp = self._get_profile()
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIn(self.public_page_post.id, self._post_ids(resp))

    def test_private_page_post_hidden_when_viewer_does_not_follow_page(self):
        """THE M4 BUG: pre-fix, this post showed up in the grid even
        though the viewer can't access the page."""
        self._auth(self.viewer_access)
        resp = self._get_profile()
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertNotIn(self.private_page_post.id, self._post_ids(resp))

    def test_private_page_post_visible_when_viewer_follows_page(self):
        """Following the private page restores visibility -- the post
        appears in the grid AND would render correctly when tapped."""
        PageFollow.objects.create(user=self.viewer, page=self.private_page)
        self._auth(self.viewer_access)
        resp = self._get_profile()
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIn(self.private_page_post.id, self._post_ids(resp))

    def test_private_page_post_with_public_override_visible_to_author_follower(self):
        """`is_public_override=True` on a private-page post lets
        followers of the AUTHOR (not the page) see it. The viewer
        follows the target but NOT the private page."""
        leaked = Post.objects.create(
            user=self.target, page=self.private_page,
            description="opted-out post",
            is_public_override=True,
        )
        Follow.objects.create(follower=self.viewer, following=self.target)
        self._auth(self.viewer_access)
        resp = self._get_profile()
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIn(leaked.id, self._post_ids(resp))

    def test_viewer_who_owns_a_page_sees_targets_posts_on_that_page(self):
        """If the viewer owns a private page that the target somehow
        contributes to (e.g., anyone_can_post=True), the viewer should
        see those posts on target's profile -- mirroring search_posts'
        treatment of owner == follower."""
        viewer_page = Page.objects.create(
            owner=self.viewer, name="Viewer's Page",
            is_private=True, anyone_can_post=True,
        )
        crosspost = Post.objects.create(
            user=self.target, page=viewer_page,
            description="target posted to viewer's page",
        )
        self._auth(self.viewer_access)
        resp = self._get_profile()
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIn(crosspost.id, self._post_ids(resp))

    # ---------- own-profile no-op ----------

    def test_own_profile_shows_all_posts(self):
        """Viewing your own profile via /auth/user-profile/?user_id=<self>
        must show everything you've posted, regardless of page privacy.
        post_visibility_q's `own` branch handles this -- our filter is
        a no-op when viewer == target."""
        self._auth(self.target_access)
        resp = self._get_profile(self.target.id)
        self.assertEqual(resp.status_code, 200, resp.content)
        ids = self._post_ids(resp)
        for p in (
            self.personal_post,
            self.public_page_post,
            self.private_page_post,  # own private-page post visible to self
        ):
            self.assertIn(p.id, ids)

    # ---------- regression: access-level gates still fire ----------

    def test_private_account_hides_grid_entirely_from_non_follower(self):
        """If target's account is private and viewer doesn't follow,
        can_view_posts is False and the posts array is empty -- the
        M4 filter runs INSIDE that branch, so this path is unchanged."""
        prof = self.target.userprofile
        prof.is_private = True
        prof.save(update_fields=["is_private"])
        self._auth(self.viewer_access)
        resp = self._get_profile()
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data.get("posts"), [])

    def test_blocked_viewer_gets_404(self):
        """Block check fires before the visibility filter -- a blocked
        viewer gets the existence-hiding 404 regardless of post state."""
        from api.models import BlockedUser
        BlockedUser.objects.create(user=self.target, blocked_user=self.viewer)
        self._auth(self.viewer_access)
        resp = self._get_profile()
        self.assertEqual(resp.status_code, 404)
