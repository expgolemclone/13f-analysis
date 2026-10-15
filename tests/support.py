"""Explicitly synthetic SEC adapter fixtures. Never used by the production builder."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from update import build_data, load_config, write_site

FIXTURE = json.loads((ROOT / "tests/fixtures/2026-Q2.json").read_text(encoding="utf-8"))
NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


def fact_rows(period=FIXTURE["period"]):
    config = load_config()
    result = []
    for key, amount in FIXTURE["million_usd"].items():
        concept, scope = config["facts"][key]
        dimensions = ({config["scope_axis"]: config["scopes"][scope]}
                      if isinstance(scope, str) else scope or {})
        result.append({"concept": "fixture:" + concept, "period_type": "instant", "period_instant": period,
                       "entity_identifier": "0001067983", "currency": "USD", "decimals": "-6",
                       "numeric_value": amount * 1_000_000, "context_ref": "synthetic-" + key,
                       "is_dimensioned": bool(dimensions), **{"dim_" + axis.replace(":", "_"): member for axis, member in dimensions.items()}})
    return result


def descriptor(form="10-Q", period=FIXTURE["period"], accession="0001193125-26-341032"):
    return {"accession": accession, "form": form, "period": period, "filed": "2026-08-10",
            "accepted": "2026-08-10T12:00:00Z", "url": f"https://www.sec.gov/Archives/edgar/data/1067983/{accession.replace('-', '')}/synthetic-test-document.htm"}


def report(rows=None, form="13F-HR", period=FIXTURE["period"], kind=None, number=None, accession="0001193125-26-352200"):
    rows = FIXTURE["synthetic_13f"] if rows is None else rows
    meta = descriptor(form, period, accession)
    return {**meta, "source": meta, "holdings": rows, "amendment_type": kind, "amendment_number": number,
            "unit_ambiguous": False, "unit_multiplier": 1, "total_value": sum(item["Value"] for item in rows)}


class FixtureGateway:
    def __init__(self, period=FIXTURE["period"]):
        self.period = period
        self.rows = fact_rows(period)
        self.report = report(period=period)
        self.financial_source = descriptor("10-K" if period.endswith("12-31") else "10-Q", period)

    def list_filings(self):
        return [self.financial_source, self.report["source"]]

    def financial(self, descriptor):
        return self.rows

    def holdings(self, descriptor):
        return self.report


def render_fixture(output: Path):
    index, snapshots = build_data(FixtureGateway(), load_config(), NOW)
    index["test_fixture"] = True
    write_site(output, index, snapshots)
    return index, snapshots
