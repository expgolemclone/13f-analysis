import copy
from datetime import date, datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests.support import FIXTURE, NOW, ROOT, FixtureGateway, descriptor, load_config, render_fixture
from assets import DataError
from update import EdgarGateway, build_data, periods, select_financial, validate_13f_xml, validate_identity, write_site


class UpdateTests(unittest.TestCase):
    def test_calendar_periods_include_pending_latest_quarter(self):
        self.assertEqual(periods("2025-12-31", date(2026, 10, 6)),
                         ["2025-12-31", "2026-03-31", "2026-06-30", "2026-09-30"])
        self.assertEqual(periods("2025-12-31", date(2025, 12, 30)), [])

    def test_q4_requires_10k(self):
        self.assertIsNone(select_financial([], "2025-12-31"))
        filing = descriptor("10-K", "2025-12-31")
        self.assertEqual(select_financial([filing], "2025-12-31"), filing)
        with self.assertRaises(DataError):
            select_financial([descriptor("10-Q", "2025-12-31")], "2025-12-31")

    def test_latest_financial_amendment_selected(self):
        base = descriptor()
        amended = {**base, "form": "10-Q/A", "filed": "2026-08-11", "accession": "0001193125-26-341033"}
        self.assertEqual(select_financial([base, amended], FIXTURE["period"]), amended)

    def test_pending_quarters_never_use_previous_holdings(self):
        index, data = build_data(FixtureGateway(), load_config(), NOW)
        self.assertEqual(index["latest_complete"], "2026-Q2")
        self.assertEqual(list(data), ["2026-Q2"])
        self.assertEqual(index["quarters"][-1], {"quarter": "2026-Q3", "period": "2026-09-30", "status": "pending", "missing": ["10-Q", "13F"]})

    def test_missing_13f_is_pending_even_with_balance_sheet(self):
        gateway = FixtureGateway()
        gateway.list_filings = lambda: [gateway.financial_source]
        with self.assertRaisesRegex(DataError, "No quarter"):
            build_data(gateway, load_config(), NOW)

    def test_rebuilding_same_sources_is_deterministic(self):
        first = build_data(FixtureGateway(), load_config(), NOW)
        second = build_data(FixtureGateway(), load_config(), NOW)
        self.assertEqual(first, second)

    def test_failure_does_not_create_publication(self):
        gateway = FixtureGateway()
        gateway.rows[0]["numeric_value"] = 2_000_000_000_000
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "site"
            with self.assertRaises(DataError):
                index, snapshots = build_data(gateway, load_config(), NOW)
                write_site(output, index, snapshots)
            self.assertFalse(output.exists())

    def test_existing_publication_not_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "site"
            render_fixture(output)
            before = (output / "index.html").read_bytes()
            with self.assertRaises(DataError):
                render_fixture(output)
            self.assertEqual((output / "index.html").read_bytes(), before)

    def test_site_has_single_index_and_exact_snapshot_inventory(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "site"
            index, snapshots = render_fixture(output)
            marker = json.loads((output / "build.json").read_text())
            self.assertEqual(marker["quarters"], sorted(snapshots))
            actual = [path.stem for path in (output / "data/quarters").glob("*.json")]
            self.assertEqual(actual, sorted(snapshots))
            self.assertEqual(json.loads((output / "data/quarters.json").read_text()), index)
            self.assertFalse((output / "data/latest.json").exists())
            self.assertIn("BNSF and BHE", (output / "index.html").read_text())

    def test_duplicate_metadata_stops(self):
        gateway = FixtureGateway()
        gateway.list_filings = lambda: [gateway.financial_source, gateway.financial_source]
        with self.assertRaisesRegex(DataError, "Duplicate SEC"):
            build_data(gateway, load_config(), NOW)

    def test_identity_rejects_empty_placeholder_noreply_and_header_injection(self):
        for identity in ("", "App person@example.com", "App person@users.noreply.github.com", "person@real-domain.test",
                         "App person@real-domain.test\nX-Foo: bad", "App one@real.test two@real.test", "App person@localhost.dev",
                         "App person@reserved.test", "App person@mail.example.com", "App person@-bad.org"):
            with self.subTest(identity=identity), self.assertRaises(DataError):
                validate_identity(identity)
        # Validation is not proof of mailbox ownership. This address is never used for network requests.
        self.assertEqual(validate_identity("Test-App contact@fixture-mail.org"), "Test-App contact@fixture-mail.org")

    def test_missing_identity_stops_before_sdk_import(self):
        with patch.dict(os.environ, {"SEC_USER_AGENT": ""}):
            with self.assertRaisesRegex(DataError, "SEC_USER_AGENT"):
                EdgarGateway(load_config())

    def test_rate_limit_is_required_not_overridden(self):
        with patch.dict(os.environ, {"SEC_USER_AGENT": "Test-App contact@fixture-mail.org", "EDGAR_RATE_LIMIT_PER_SEC": "9"}):
            with self.assertRaisesRegex(DataError, "RATE_LIMIT"):
                EdgarGateway(load_config())

    def test_cli_missing_identity_stops_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "site"
            env = {**os.environ, "SEC_USER_AGENT": ""}
            result = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/update.py"), "build", "--output", str(output)],
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn("SEC_USER_AGENT", result.stderr)
            self.assertFalse(output.exists())

    def test_cli_generated_data_cannot_be_written_into_repository(self):
        output = ROOT / "forbidden-generated-test-output"
        self.assertFalse(output.exists())
        result = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/update.py"), "build", "--output", str(output)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("outside the repository", result.stderr)
        self.assertFalse(output.exists())

    def test_gateway_uses_exact_filings_and_sdk_public_fields(self):
        filing = SimpleNamespace(cik=1067983, accession_no="0001193125-26-341032", form="10-Q",
                                 report_date="2026-06-30", filing_date=date(2026, 8, 10),
                                 acceptance_datetime="2026-08-10T12:00:00Z", primary_document="brka-20260630.htm")
        filing.xbrl = lambda: SimpleNamespace(facts=SimpleNamespace(get_facts=lambda: ["SDK facts sentinel"]))
        calls = []
        company = SimpleNamespace(get_filings=lambda **kwargs: calls.append(kwargs) or [filing])
        sdk = SimpleNamespace(Company=lambda cik: company, set_identity=lambda value: None)
        with patch.dict(sys.modules, {"edgar": sdk}), patch.dict(os.environ, {
                "SEC_USER_AGENT": "Test-App contact@fixture-mail.org", "EDGAR_RATE_LIMIT_PER_SEC": "3"}):
            gateway = EdgarGateway(load_config())
            filings = gateway.list_filings()
            self.assertEqual(filings[0]["period"], "2026-06-30")
            self.assertEqual(gateway.financial(filings[0]), ["SDK facts sentinel"])
            self.assertTrue(calls[0]["trigger_full_load"])
            self.assertEqual(calls[0]["filing_date"], "2025-12-31:")
            filing.xbrl = lambda: None
            with self.assertRaisesRegex(DataError, "No XBRL"):
                gateway.financial(filings[0])


class GatewayHoldingsTests(unittest.TestCase):
    def report(self, multiplier=1):
        source = descriptor("13F-HR")
        source["period"] = "2023-03-31" if multiplier == 1000 else "2026-06-30"
        record = {"Cusip": "037833100", "Class": "COM", "Type": "Shares", "PutCall": "",
                  "Value": 10 * multiplier, "SharesPrnAmount": 1, "OtherManager": ""}
        frame = SimpleNamespace(columns=list(record), empty=False, to_dict=lambda **kwargs: [copy.deepcopy(record)])
        report = SimpleNamespace(infotable_xml=(ROOT / "tests/fixtures/13f.xml").read_text(), total_holdings=1,
                                 value_unit_resolution=SimpleNamespace(multiplier=multiplier, ambiguous=False),
                                 primary_form_information=SimpleNamespace(report_period=datetime.fromisoformat(source["period"])),
                                 infotable=frame, amendment_type=None, amendment_number=None, total_value=10 * multiplier)
        gateway = object.__new__(EdgarGateway)
        gateway.filings = {source["accession"]: SimpleNamespace(accession_no=source["accession"])}
        def constructor(filing, *, use_latest_period_of_report):
            self.assertFalse(use_latest_period_of_report)
            return report
        sdk = SimpleNamespace(ThirteenF=constructor)
        return gateway, source, report, record, sdk

    def test_sdk_dollars_are_not_multiplied_twice(self):
        for multiplier in (1, 1000):
            gateway, source, report, row, sdk = self.report(multiplier)
            with self.subTest(multiplier=multiplier), patch.dict(sys.modules, {"edgar": sdk}):
                result = gateway.holdings(source)
                self.assertEqual(result["holdings"], [row])
                self.assertEqual(result["total_value"], 10 * multiplier)

    def test_sdk_normalization_mismatch_stops(self):
        gateway, source, report, row, sdk = self.report()
        row["Value"] = 0
        with patch.dict(sys.modules, {"edgar": sdk}), self.assertRaisesRegex(DataError, "normalization"):
            gateway.holdings(source)

    def test_sdk_must_preserve_every_xml_row(self):
        gateway, source, report, row, sdk = self.report()
        report.infotable.to_dict = lambda **kwargs: [row, row]
        with patch.dict(sys.modules, {"edgar": sdk}), self.assertRaisesRegex(DataError, "omitted or added"):
            gateway.holdings(source)

    def test_cover_row_count_must_match_xml(self):
        gateway, source, report, row, sdk = self.report()
        report.total_holdings = 2
        with patch.dict(sys.modules, {"edgar": sdk}), self.assertRaisesRegex(DataError, "entry count"):
            gateway.holdings(source)

    def test_cover_period_must_match_submissions(self):
        gateway, source, report, row, sdk = self.report()
        report.primary_form_information.report_period = datetime(2026, 3, 31)
        with patch.dict(sys.modules, {"edgar": sdk}), self.assertRaisesRegex(DataError, "cover-page period"):
            gateway.holdings(source)

    def test_ambiguous_units_stop(self):
        gateway, source, report, row, sdk = self.report()
        report.value_unit_resolution.ambiguous = True
        with patch.dict(sys.modules, {"edgar": sdk}), self.assertRaisesRegex(DataError, "Ambiguous"):
            gateway.holdings(source)

    def test_unknown_unit_multiplier_stops(self):
        gateway, source, report, row, sdk = self.report()
        report.value_unit_resolution.multiplier = 10
        with patch.dict(sys.modules, {"edgar": sdk}), self.assertRaisesRegex(DataError, "Unsupported"):
            gateway.holdings(source)

    def test_missing_instrument_columns_stop(self):
        gateway, source, report, row, sdk = self.report()
        report.infotable.columns.remove("OtherManager")
        with patch.dict(sys.modules, {"edgar": sdk}), self.assertRaisesRegex(DataError, "columns"):
            gateway.holdings(source)


class XmlTests(unittest.TestCase):
    def setUp(self):
        self.xml = (ROOT / "tests/fixtures/13f.xml").read_text()

    def test_valid_xml(self):
        self.assertEqual(validate_13f_xml(self.xml), [(10, 1)])

    def test_sdk_parser_cannot_silently_default_invalid_numbers(self):
        for value in ("", "NaN", "bad", "10.1", "-10"):
            with self.subTest(value=value), self.assertRaises(DataError):
                validate_13f_xml(self.xml.replace("<value>10</value>", f"<value>{value}</value>"))

    def test_missing_fields_malformed_xml_and_dtd_stop(self):
        for xml in (self.xml.replace("<cusip>037833100</cusip>", ""), self.xml[:-20],
                    '<!DOCTYPE informationTable [<!ENTITY test "bad">]>' + self.xml,
                    "<informationTable/>"):
            with self.subTest(xml=xml[:30]), self.assertRaises(DataError):
                validate_13f_xml(xml)


if __name__ == "__main__":
    unittest.main()
