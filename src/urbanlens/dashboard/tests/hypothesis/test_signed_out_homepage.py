"""The signed-out landing page must actually be styled.

Reported as "missing a container div (or some other similar problem causing it
to be unstyled)". The container was there - every page has one. What was
missing is the *body class*: `_homepage.scss` scopes all of its rules under
`body.page-home`, `base.html` sets that class from `page_name`, and the
anonymous index view never put `page_name` in its context. So the page
rendered its markup with none of its own CSS applying.

This is a whole class of bug - a stylesheet keyed on a body class a view
forgets to set fails silently and looks like broken markup - so the test
asserts the contract rather than the symptom.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase


class SignedOutHomepageTests(TestCase):
    def test_the_body_carries_the_class_the_stylesheet_needs(self) -> None:
        response = self.client.get(reverse("index"))

        self.assertContains(response, "page-home", msg_prefix="without body.page-home, none of _homepage.scss applies")

    def test_the_landing_markup_is_rendered(self) -> None:
        response = self.client.get(reverse("index"))

        self.assertContains(response, "home-hero")
        self.assertContains(response, "home-features")

    def test_a_signed_in_user_is_sent_to_their_dashboard(self) -> None:
        """The landing page is for signed-out visitors only."""
        baker.make(User)
        user = baker.make(User)
        self.client.force_login(user)

        response = self.client.get(reverse("index"))

        self.assertEqual(response.status_code, 302)

    def test_signed_out_visitors_get_no_file_upload_markup(self) -> None:
        """The comment-photo dialog is unusable while signed out, so it shouldn't render either."""
        response = self.client.get(reverse("index"))

        self.assertNotContains(response, 'id="comment-image-composer"')
        self.assertNotContains(response, 'id="cip-file-input"')


class SignedInHomepageTests(TestCase):
    def test_signed_in_visitors_still_get_the_photo_dialog(self) -> None:
        """The gate in `themes/base.html` must not take the feature away from who can use it."""
        user = baker.make(User)
        self.client.force_login(user)

        response = self.client.get(reverse("home.view"))

        self.assertContains(response, 'id="comment-image-composer"')
        self.assertContains(response, 'id="cip-file-input"')
