"""Offline integration check against the installed, pinned EdgarTools release."""
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from assets import Facts
from update import primary_13f, validate_13f_xml
from edgar import ThirteenF
from edgar.xbrl import XBRL
from edgar.thirteenf.parsers.infotable_xml import parse_infotable_xml
from edgar.thirteenf.parsers.primary_xml import parse_primary_document_xml
import pandas as pd

xml = (ROOT / "tests/fixtures/13f.xml").read_text()
assert validate_13f_xml(xml) == [(10, 1)]
# Ticker lookup is unrelated to accounting. Keep this decoder check fully offline.
mapping = pd.DataFrame({"Ticker": ["AAPL"]}, index=["037833100"])
with patch("edgar.thirteenf.parsers.infotable_xml.cusip_ticker_mapping", return_value=mapping):
    table = parse_infotable_xml(xml)
assert table.iloc[0]["Value"] == 10
assert table.iloc[0]["SharesPrnAmount"] == 1
assert table.iloc[0]["Type"] == "Shares"
assert table.iloc[0]["PutCall"] == ""
assert table.iloc[0]["OtherManager"] == ""
for property_name in ("amendment_type", "amendment_number", "raw_infotable", "infotable_xml"):
    assert hasattr(ThirteenF, property_name), property_name
primary_xml = (ROOT / "tests/fixtures/13f-primary.xml").read_text()
primary = primary_13f(primary_xml, {"form": "13F-HR", "period": "2026-06-30"}, 1067983)
parsed = parse_primary_document_xml(primary_xml)
assert parsed.report_period.date().isoformat() == "2026-06-30"
assert parsed.summary_page.total_value == primary["raw_total"]
assert parsed.summary_page.total_holdings == primary["count"]
assert primary["unit_multiplier"] == 1
xbrl = XBRL.from_files(instance_file=ROOT / "tests/fixtures/xbrl.xml")
facts = Facts(xbrl.facts.get_facts(), "2026-06-30", 1067983)
assert facts.select("Assets", {}).amount == 1_263_071_000_000
assert facts.select("CashAndCashEquivalentsAtCarryingValue", {
    "srt:ProductOrServiceAxis": "brka:InsuranceAndOtherMember"}).amount == 35_096_000_000
print("Pinned EdgarTools XML decoding and dimensional fact contracts verified offline.")
