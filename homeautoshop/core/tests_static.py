"""
Tests that /static/ is actually served (SPEC §5.1).

This is not a theoretical concern. Gunicorn serves no static files and the
Caddy site block only reverse-proxies, so for a while `collectstatic` wrote a
stylesheet that nothing on the deployed stack would ever hand to a browser.
The symptom is quiet: the page renders, unstyled, and the console reports a
MIME type error rather than a missing file, because Django answers the request
with a 404 *page* whose content type is text/html.
"""

from __future__ import annotations

from django.test import TestCase

from homeautoshop.accounts.models import User


class StaticFileServingTests(TestCase):
    def test_stylesheet_is_served_as_css(self):
        response = self.client.get("/static/app.css")
        self.assertEqual(response.status_code, 200)
        # The content type is the whole point: a 404 HTML page also has a body.
        self.assertTrue(response.headers["Content-Type"].startswith("text/css"))

    def test_manifest_is_served_as_a_manifest(self):
        response = self.client.get("/static/manifest.webmanifest")
        self.assertEqual(response.status_code, 200)
        self.assertIn("manifest+json", response.headers["Content-Type"])

    def test_a_missing_static_file_is_still_a_miss(self):
        """WhiteNoise must not answer for files that are not there."""
        self.assertEqual(self.client.get("/static/nope.css").status_code, 404)

    def test_the_hidden_attribute_still_hides(self):
        """A rule this page's scripts depend on, and that is easy to delete.

        `[hidden] { display: none }` comes from the browser and loses to any
        author rule that sets `display` on the same element. Two of ours do, so
        without an explicit override the top bar's sync indicator — rendered
        `hidden` and toggled by `offline.js` — sat there saying "Up to date"
        regardless. Asserted against the served file rather than the source,
        because a stylesheet nothing serves is the other failure this module is
        about.
        """
        response = self.client.get("/static/app.css")
        # WhiteNoise streams, so there is no `.content` to read.
        css = b"".join(response.streaming_content).decode()
        self.assertIn("[hidden] { display: none !important; }", css)

    def test_every_page_reference_resolves(self):
        """A rendered page must not point at a stylesheet that 404s."""
        user = User.objects.create_user(username="static-check", password="x" * 16)
        self.client.force_login(user)
        page = self.client.get("/").content.decode()

        for attribute in ('href="/static/', 'src="/static/'):
            start = 0
            while (found := page.find(attribute, start)) != -1:
                url = page[found + len(attribute) - len("/static/") :]
                url = url[: url.index('"')]
                self.assertEqual(
                    self.client.get(url).status_code, 200, f"{url} does not resolve"
                )
                start = found + 1
