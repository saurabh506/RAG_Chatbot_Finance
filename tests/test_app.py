import re
import unittest
from datetime import date

from streamlit.testing.v1 import AppTest

import config

config.VALIDATE_LIVE = False  # UI tests never touch the network

APP = str(__import__("pathlib").Path(__file__).resolve().parents[1] / "app.py")


LINK = r"\]\(https?://"  # markdown link target, so [url](url) counts once


def run_app(question=None, click=None):
    at = AppTest.from_file(APP, default_timeout=30).run()
    if click is not None:
        at.button[click].click().run()
    elif question is not None:
        at.text_input[0].set_value(question).run()
    return at


def page_text(at):
    return " ".join(m.value for m in at.markdown) + " " + " ".join(c.value for c in at.caption)


class AppTests(unittest.TestCase):
    def test_initial_page(self):
        at = run_app()
        self.assertFalse(at.exception)
        self.assertEqual(len(at.button), 3)
        self.assertIn("Facts-only. No investment advice.", [c.value for c in at.caption])

    def test_refusal_has_one_link_and_no_error_box(self):
        at = run_app("Should I buy HDFC Mid Cap?")
        self.assertFalse(at.exception)
        self.assertEqual(len(at.error), 0)
        links = re.findall(LINK, " ".join(m.value for m in at.markdown))
        self.assertEqual(len(links), 1)

    def test_pii_not_echoed(self):
        at = run_app("My PAN is ABCDE1234F, exit load?")
        self.assertNotIn("ABCDE1234F", page_text(at))

    def test_every_chip_produces_an_answer_with_one_link(self):
        for i in range(3):
            at = run_app(click=i)
            self.assertFalse(at.exception, i)
            links = re.findall(LINK, " ".join(m.value for m in at.markdown))
            self.assertEqual(len(links), 1, i)

    def test_staleness(self):
        import app

        today = date(2026, 12, 15)
        self.assertTrue(app.is_stale("2026-11-01", today))
        self.assertFalse(app.is_stale("2026-11-20", today))
        self.assertFalse(app.is_stale(None, today))

    def test_display_text_drops_inline_source(self):
        import app
        from rag.retrieve import Answer

        a = Answer("X has an expense ratio of 1%. Source: https://a.b — Last updated from sources: 2026-11-01.",
                   "https://a.b", "2026-11-01", "I1", None, "facts")
        self.assertEqual(app.display_text(a), "X has an expense ratio of 1%.")
