"""Headless browser checks on explicitly synthetic data. No GUI or screenshots."""
import copy
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import math
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

    def chart_point(self, angle, radius=0.76):
        chart = self.page.locator('#chart')
        chart.scroll_into_view_if_needed()
        box = chart.bounding_box()
        distance = box['width'] / 2 * radius
        radians = math.radians(angle)
        return (box['x'] + box['width'] / 2 + math.sin(radians) * distance,
                box['y'] + box['height'] / 2 - math.cos(radians) * distance)

    def hover_chart(self, angle, radius=0.76):
        self.page.mouse.move(*self.chart_point(angle, radius))

    def slice_angles(self, data=None):
        data = self.snapshots['2026-Q2'] if data is None else data
        allocated = 0
        for item in data['slices']:
            start = allocated / data['total_assets'] * 360
            allocated += item['amount']
            end = allocated / data['total_assets'] * 360
            if item['amount']:
                yield item['key'], start, end

    def expect_preview(self, key):
        row = self.page.locator(f'#rows tr:has(button[data-key="{key}"])')
        label = row.locator('button').inner_text()
        amount = row.locator('td').nth(1).inner_text()
        percent = row.locator('td').nth(2).inner_text()
        expect(self.page.locator('#preview strong')).to_have_text(label)
        expect(self.page.locator('#preview span')).to_have_text(f'${amount}B / {percent} of total')
        expect(self.page.locator('#rows .is-preview')).to_have_count(1)
        expect(row).to_have_class('is-preview')
        self.assertEqual(self.errors, [])

    def expect_no_preview(self):
        expect(self.page.locator('#preview')).to_have_text('Preview: hover a slice or focus a table item.')
        expect(self.page.locator('#rows .is-preview')).to_have_count(0)

    def install_earlier_snapshot(self, quarter='2026-Q1'):
        period = next(entry['period'] for entry in self.index['quarters'] if entry['quarter'] == quarter)
        previous = copy.deepcopy(self.snapshots['2026-Q2'])
        previous.update({'quarter': quarter, 'period': period, 'total_assets': previous['total_assets'] - 1_000_000_000})
        previous['slices'][0]['amount'] -= 1_000_000_000
        previous['financial_source']['period'] = previous['period']
        for source in previous['holdings_sources']:
            source['period'] = previous['period']
        updated = copy.deepcopy(self.index)
        position = next(i for i, entry in enumerate(updated['quarters']) if entry['quarter'] == quarter)
        updated['quarters'][position] = {'quarter': quarter, 'period': period, 'status': 'complete', 'path': f'data/quarters/{quarter}.json'}
        self.page.route('**/data/quarters.json', lambda route: route.fulfill(json=updated))
        self.page.route(f'**/data/quarters/{quarter}.json', lambda route: route.fulfill(json=previous))
        return previous

    def test_chart_hover_previews_every_slice_without_selecting_or_layout_shift(self):
        self.open()
        self.hover_chart(5)
        detail = self.page.locator('#detail').text_content()
        position = self.page.locator('#detail').bounding_box()
        for key, start, end in self.slice_angles():
            with self.subTest(key=key):
                self.hover_chart((start + end) / 2)
                self.expect_preview(key)
                expect(self.page.locator('#rows button[aria-pressed="true"]')).to_have_attribute('data-key', 'cash')
                expect(self.page.locator('#detail')).to_have_text(detail)
                self.assertEqual(position, self.page.locator('#detail').bounding_box())
        expect(self.page.locator('.hole strong')).to_have_text('100%')
        self.assertIsNone(self.page.locator('#preview').get_attribute('aria-live'))

    def test_chart_wrap_boundaries_and_zero_slices(self):
        data = copy.deepcopy(self.snapshots['2026-Q2'])
        data['slices'][0]['amount'] += data['slices'][2]['amount']
        data['slices'][2]['amount'] = 0
        self.page.route('**/data/quarters/2026-Q2.json', lambda route: route.fulfill(json=data))
        self.open()
        self.hover_chart(0)
        self.expect_preview('cash')
        self.hover_chart(359.5)
        self.expect_preview('other')
        angles = list(self.slice_angles(data))
        for (left_key, _, boundary), (right_key, _, _) in zip(angles, angles[1:]):
            with self.subTest(boundary=boundary):
                self.hover_chart(boundary - 0.5)
                self.expect_preview(left_key)
                self.hover_chart(boundary + 0.5)
                self.expect_preview(right_key)
        # Exact interval edges are tested with subpixel coordinates, independent of mouse pixel rounding.
        x, y = self.chart_point(0)
        self.assertEqual(self.page.evaluate('point => chartKey({clientX:point[0],clientY:point[1]})', [x, y]), 'cash')
        self.assertNotIn('AAPL', self.page.evaluate('sectors.map(item=>item.key)'))
        self.page.locator('#rows button[data-key="AAPL"]').hover()
        self.expect_preview('AAPL')
        expect(self.page.locator('#preview span')).to_have_text('$0.000B / 0.00% of total')

    def test_small_slice_and_zero_final_slice_preserve_chart_geometry(self):
        data = copy.deepcopy(self.snapshots['2026-Q2'])
        # 0.7 degrees is small but still a visible, hittable sector in the rendered ring.
        small = round(data['total_assets'] * 0.7 / 360)
        data['slices'][0]['amount'] += data['slices'][2]['amount'] - small + data['slices'][-1]['amount']
        data['slices'][2]['amount'] = small
        data['slices'][-1]['amount'] = 0
        for item in data['other_breakdown']:
            item['amount'] = 0
        self.page.route('**/data/quarters/2026-Q2.json', lambda route: route.fulfill(json=data))
        self.open()
        for key, start, end in self.slice_angles(data):
            self.hover_chart((start + end) / 2)
            self.expect_preview(key)
        self.hover_chart(359.5)
        self.expect_preview('intangibles')
        self.assertEqual(self.page.evaluate('sectors.at(-1).end'), 360)

    def test_hole_outer_edge_corners_and_pointerleave_clear_preview(self):
        self.open()
        for radius in [0, 0.5, 1.05]:
            self.hover_chart(5)
            self.expect_preview('cash')
            self.hover_chart(0, radius)
            self.expect_no_preview()
        self.hover_chart(5)
        box = self.page.locator('#chart').bounding_box()
        self.page.mouse.move(box['x'] + 2, box['y'] + 2)
        self.expect_no_preview()
        for radius in [0.52, 1]:
            x, y = self.chart_point(0, radius)
            self.assertIsNone(self.page.evaluate('point => chartKey({clientX:point[0],clientY:point[1]})', [x, y]))
        self.hover_chart(5)
        self.page.locator('#chart-heading').hover()
        self.expect_no_preview()

    def test_escape_dismisses_until_target_changes_or_reentry(self):
        self.open()
        self.hover_chart(5)
        self.expect_preview('cash')
        self.page.keyboard.press('Escape')
        self.expect_no_preview()
        self.hover_chart(6)
        self.expect_no_preview()
        self.hover_chart(30)
        self.expect_preview('treasury_bills')
        self.page.keyboard.press('Escape')
        self.hover_chart(0, 0)
        self.expect_no_preview()
        self.hover_chart(30)
        self.expect_preview('treasury_bills')

    def test_table_hover_and_keyboard_focus_share_preview_not_selection(self):
        self.open()
        button = self.page.locator('#rows button[data-key="GOOGL"]')
        button.focus()
        self.expect_preview('GOOGL')
        self.hover_chart(5)
        self.expect_preview('GOOGL')
        self.page.keyboard.press('Escape')
        self.expect_no_preview()
        self.page.keyboard.press('Tab')
        self.expect_preview('KO')
        self.page.keyboard.press('Space')
        expect(self.page.locator('#rows button[data-key="KO"]')).to_have_attribute('aria-pressed', 'true')
        self.page.locator('#rows button[data-key="KO"]').evaluate('el=>el.blur()')
        self.expect_preview('cash')
        button.hover()
        self.expect_preview('GOOGL')
        expect(self.page.locator('#rows button[data-key="KO"]')).to_have_attribute('aria-pressed', 'true')
        self.page.locator('#chart-heading').hover()
        self.expect_no_preview()

    def test_switching_mouse_and_keyboard_on_same_button_updates_preview_priority(self):
        self.open()
        button = self.page.locator('#rows button[data-key="GOOGL"]')
        button.focus()
        self.expect_preview('GOOGL')
        button.click()
        self.hover_chart(5)
        self.expect_preview('cash')
        expect(button).to_be_focused()
        self.page.keyboard.press('Enter')
        self.expect_preview('GOOGL')
        self.page.keyboard.press('Escape')
        self.expect_no_preview()
        self.page.keyboard.press('Enter')
        self.expect_no_preview()

    def test_preview_resets_on_quarter_change_and_uses_new_amounts(self):
        previous = self.install_earlier_snapshot()
        self.open()
        self.hover_chart(5)
        self.expect_preview('cash')
        old_value = self.page.locator('#preview span').inner_text()
        for quarter in ['2026-Q3', '2026-Q1', '2026-Q2', '2026-Q1']:
            self.page.locator('#quarter').select_option(quarter)
            self.expect_no_preview()
            if quarter != '2026-Q3':
                expect(self.page.locator('#dashboard')).to_be_visible()
                data = previous if quarter == '2026-Q1' else self.snapshots['2026-Q2']
                _, start, end = next(self.slice_angles(data))
                self.hover_chart((start + end) / 2)
                self.expect_preview('cash')
                if quarter == '2026-Q1':
                    self.assertNotEqual(old_value, self.page.locator('#preview span').inner_text())

    def test_loading_error_and_slow_response_do_not_restore_a_stale_preview(self):
        previous = self.install_earlier_snapshot('2025-Q4')
        held = []
        # This quarter is not prefetched as the immediately preceding quarter of Q2.
        self.page.route('**/data/quarters/2025-Q4.json', lambda route: held.append(route))
        self.open()
        self.hover_chart(5)
        self.page.locator('#quarter').select_option('2025-Q4')
        expect(self.page.locator('#dashboard')).to_be_hidden()
        self.expect_no_preview()
        self.assertEqual(len(held), 1)
        self.page.locator('#quarter').select_option('2026-Q3')
        held.pop().fulfill(json=previous)
        expect(self.page.locator('#status')).to_contain_text('2026-Q3 is pending')
        self.expect_no_preview()
        self.page.locator('#quarter').select_option('2026-Q2')
        expect(self.page.locator('#dashboard')).to_be_visible()
        # A fresh page loads the changed, invalid quarter instead of the validated cached snapshot.
        self.page.route('**/data/quarters/2025-Q4.json', lambda route: route.fulfill(json={}))
        self.page.reload()
        expect(self.page.locator('#dashboard')).to_be_visible()
        self.hover_chart(5)
        self.page.locator('#quarter').select_option('2025-Q4')
        self.expect_no_preview()
        expect(self.page.locator('#status')).to_contain_text('Cannot display verified data')
        expect(self.page.locator('#dashboard')).to_be_hidden()
        self.expect_no_preview()
        self.assertEqual(self.errors, [])

    def test_preview_remains_safe_text(self):
        data = copy.deepcopy(self.snapshots['2026-Q2'])
        data['slices'][0]['label'] = '<img src="bad" onerror="window.injected=true">'
        self.page.route('**/data/quarters/2026-Q2.json', lambda route: route.fulfill(json=data))
        self.open()
        self.hover_chart(5)
        expect(self.page.locator('#preview strong')).to_have_text(data['slices'][0]['label'])
        expect(self.page.locator('#preview img')).to_have_count(0)
        self.assertIsNone(self.page.evaluate('window.injected'))

    def test_preview_after_resizing_and_scrolling_has_no_overflow(self):
        self.open()
        for width in [375, 320, 1200]:
            with self.subTest(width=width):
                self.page.set_viewport_size({'width': width, 'height': 812})
                for key, start, end in self.slice_angles():
                    self.hover_chart((start + end) / 2)
                    self.expect_preview(key)
                dimensions = self.page.evaluate('({scroll:document.documentElement.scrollWidth,width:document.documentElement.clientWidth})')
                self.assertLessEqual(dimensions['scroll'], dimensions['width'])
                preview_box = self.page.locator('#preview').bounding_box()
                self.assertGreaterEqual(preview_box['x'], 0)
                self.assertLessEqual(preview_box['x'] + preview_box['width'], width)
                self.assertLessEqual(self.page.locator('#preview').evaluate('el=>el.scrollHeight'), preview_box['height'])

    def test_touch_keeps_existing_table_selection_without_hover_preview(self):
        context = self.browser.new_context(has_touch=True, is_mobile=True, viewport={'width': 375, 'height': 812})
        self.addCleanup(context.close)
        self.page = context.new_page()
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.open()
        button = self.page.locator('#rows button[data-key="OXY"]')
        button.tap()
        expect(button).to_have_attribute('aria-pressed', 'true')
        expect(self.page.locator('#detail')).to_contain_text('equity-method carrying value')
        self.expect_no_preview()
        chart = self.page.locator('#chart')
        chart.scroll_into_view_if_needed()
        self.page.touchscreen.tap(*self.chart_point(5))
        self.expect_no_preview()
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
        expect(self.page.get_by_role("link", name="expgolemclone/13f-analysis", exact=True)).to_have_attribute(
            "href", "https://github.com/expgolemclone/13f-analysis")

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
        self.install_earlier_snapshot()
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
