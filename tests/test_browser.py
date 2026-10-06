"""Headless browser checks on explicitly synthetic data. No GUI or screenshots."""
import copy
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
from threading import Thread
import unittest

from playwright.sync_api import sync_playwright, expect

from tests.support import render_fixture


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(headless=True)
        except Exception:
            cls.playwright.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "site"
        self.index, self.snapshots = render_fixture(self.output)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(self.output)))
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.url = f"http://127.0.0.1:{self.server.server_port}/"

    def close_server(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def open(self):
        self.page.goto(self.url)
        expect(self.page.locator("#dashboard")).to_be_visible()
        self.assertEqual(self.errors, [])

    def test_chart_and_table_reconcile_with_explicit_accounting_labels(self):
        self.open()
        expect(self.page.locator("#total")).to_have_text("$1,263.071B")
        expect(self.page.locator("#rows button")).to_have_count(17)
        expect(self.page.locator("#status")).to_contain_text("TEST FIXTURE")
        expect(self.page.locator("#rows")).to_contain_text("BNSF - PP&E")
        expect(self.page.locator("#rows")).to_contain_text("BHE - PP&E")
        self.assertTrue(self.page.locator("#chart").evaluate("el => getComputedStyle(el).backgroundImage.startsWith('conic-gradient')"))
        self.page.get_by_role("button", name="OXY", exact=True).click()
        expect(self.page.locator("#detail")).to_contain_text("equity-method carrying value")
        expect(self.page.locator("#detail")).to_contain_text("not 13F market value")
        expect(self.page.locator("#download")).to_have_attribute("href", "data/quarters/2026-Q2.json")

    def test_keyboard_selection_has_focus_and_pressed_state(self):
        self.open()
        button = self.page.get_by_role("button", name="GOOGL", exact=True)
        button.focus()
        self.page.keyboard.press("Enter")
        expect(button).to_have_attribute("aria-pressed", "true")
        expect(self.page.locator("#detail")).to_contain_text("Class A only")
        self.assertEqual(button.evaluate("el => getComputedStyle(el).outlineStyle"), "solid")
        self.assertGreaterEqual(float(button.evaluate("el => getComputedStyle(el).outlineWidth").rstrip("px")), 3)
        self.assertGreaterEqual(button.bounding_box()["height"], 44)

    def test_mobile_has_no_page_overflow(self):
        self.page.set_viewport_size({"width": 375, "height": 812})
        self.open()
        dimensions = self.page.evaluate("({scroll: document.documentElement.scrollWidth, width: document.documentElement.clientWidth})")
        self.assertLessEqual(dimensions["scroll"], dimensions["width"])
        self.assertEqual(len(self.page.locator(".allocation").evaluate("el => getComputedStyle(el).gridTemplateColumns").split()), 1)

    def test_pending_quarter_never_displays_an_old_chart(self):
        self.open()
        self.page.locator("#quarter").select_option("2026-Q3")
        expect(self.page.locator("#dashboard")).to_be_hidden()
        expect(self.page.locator("#status")).to_contain_text("pending 10-Q and 13F")
        expect(self.page.locator("#status")).to_contain_text("No previous-quarter holdings")
        self.page.locator("#quarter").select_option("2026-Q2")
        expect(self.page.locator("#dashboard")).to_be_visible()

    def test_corrupt_snapshot_is_not_normalized_to_100_percent(self):
        bad = copy.deepcopy(self.snapshots["2026-Q2"])
        bad["slices"][0]["amount"] += 1
        self.page.route("**/data/quarters/2026-Q2.json", lambda route: route.fulfill(json=bad))
        self.page.goto(self.url)
        expect(self.page.locator("#status")).to_contain_text("Allocation does not sum")
        expect(self.page.locator("#dashboard")).to_be_hidden()
        self.assertEqual(self.errors, [])

    def test_bad_index_path_cannot_fetch_external_data(self):
        bad = copy.deepcopy(self.index)
        bad["quarters"][2]["path"] = "https://external.invalid/quarter.json"
        self.page.route("**/data/quarters.json", lambda route: route.fulfill(json=bad))
        self.page.goto(self.url)
        expect(self.page.locator("#status")).to_contain_text("Invalid snapshot path")
        expect(self.page.locator("#dashboard")).to_be_hidden()

    def test_filing_period_mismatch_stops_display(self):
        bad = copy.deepcopy(self.snapshots["2026-Q2"])
        bad["financial_source"]["period"] = "2026-03-31"
        self.page.route("**/data/quarters/2026-Q2.json", lambda route: route.fulfill(json=bad))
        self.page.goto(self.url)
        expect(self.page.locator("#status")).to_contain_text("Source period mismatch")
        expect(self.page.locator("#dashboard")).to_be_hidden()

    def test_text_content_is_not_executed_as_html(self):
        data = copy.deepcopy(self.snapshots["2026-Q2"])
        data["slices"][0]["note"] = '<img src="bad" onerror="window.injected=true">'
        self.page.route("**/data/quarters/2026-Q2.json", lambda route: route.fulfill(json=data))
        self.open()
        expect(self.page.locator("#detail")).to_contain_text("<img")
        expect(self.page.locator("#detail img")).to_have_count(0)
        self.assertIsNone(self.page.evaluate("window.injected"))

    def test_stale_verification_is_visible(self):
        old = copy.deepcopy(self.index)
        old["generated_at"] = "2000-01-01T00:00:00+00:00"
        self.page.route("**/data/quarters.json", lambda route: route.fulfill(json=old))
        self.open()
        expect(self.page.locator("#status")).to_contain_text("more than 14 days old")
        expect(self.page.locator("#status")).to_have_class("status warning")

    def test_previous_adjacent_quarter_change(self):
        previous = copy.deepcopy(self.snapshots["2026-Q2"])
        previous.update({"quarter": "2026-Q1", "period": "2026-03-31", "total_assets": previous["total_assets"] - 1_000_000_000})
        previous["slices"][0]["amount"] -= 1_000_000_000
        previous["financial_source"]["period"] = previous["period"]
        for source in previous["holdings_sources"]:
            source["period"] = previous["period"]
        updated = copy.deepcopy(self.index)
        updated["quarters"][1] = {"quarter": "2026-Q1", "period": "2026-03-31", "status": "complete", "path": "data/quarters/2026-Q1.json"}
        self.page.route("**/data/quarters.json", lambda route: route.fulfill(json=updated))
        self.page.route("**/data/quarters/2026-Q1.json", lambda route: route.fulfill(json=previous))
        self.open()
        expect(self.page.locator("#change")).to_have_text("+0.08%")

    def test_slow_response_cannot_replace_a_new_pending_selection(self):
        held = []
        self.page.route("**/data/quarters/2026-Q2.json", lambda route: held.append(route))
        self.page.goto(self.url, wait_until="domcontentloaded")
        expect(self.page.locator("#quarter")).to_be_enabled()
        self.page.locator("#quarter").select_option("2026-Q3")
        self.assertEqual(len(held), 1)
        held[0].fulfill(json=self.snapshots["2026-Q2"])
        expect(self.page.locator("#dashboard")).to_be_hidden()
        expect(self.page.locator("#status")).to_contain_text("2026-Q3 is pending")


if __name__ == "__main__":
    unittest.main()
