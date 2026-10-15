"""Build a verified static site from SEC filings, or inspect filing XBRL selectors."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import sys
import xml.etree.ElementTree as ET

from assets import DataError, allocate, extract, merge_reports, money, quarter

ROOT = Path(__file__).resolve().parents[1]
FORMS = {"10-Q", "10-Q/A", "10-K", "10-K/A", "13F-HR", "13F-HR/A"}


def validate_identity(identity: str) -> str:
    emails = re.findall(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", identity)
    if len(emails) != 1 or not identity.isascii() or "\n" in identity or "\r" in identity:
        raise DataError("SEC_USER_AGENT must contain a descriptive ASCII application name and one real contact email")
    email = emails[0]
    domain = email.split("@", 1)[1].lower()
    labels = domain.split(".")
    if any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels):
        raise DataError("Invalid SEC contact email domain")
    if (domain == "localhost.dev" or any(domain == reserved or domain.endswith("." + reserved)
            for reserved in ("example.com", "example.org", "example.net", "invalid", "example", "test", "localhost"))
            or "noreply" in email.lower() or "no-reply" in email.lower()):
        raise DataError("SEC_USER_AGENT cannot use a placeholder or no-reply contact")
    if not identity.replace(email, "").strip(" ()<>/"):
        raise DataError("SEC_USER_AGENT requires an application name, not just an email")
    return identity


def load_config() -> dict:
    config = json.loads((ROOT / "config/berkshire.json").read_text(encoding="utf-8"))
    quarter(config["since"])
    if config["since"] < "2023-03-31":
        raise DataError("Accounting policy supports 2023-Q1 onward; earlier history needs separate review")
    cusips = [item["cusip"] for item in config["equities"]]
    if len(set(cusips)) != len(cusips):
        raise DataError("Duplicate selected CUSIP")
    return config


def periods(since: str, today: date) -> list[str]:
    start = date.fromisoformat(since)
    quarter(since)
    result = []
    for year in range(start.year, today.year + 1):
        for month, day in ((3, 31), (6, 30), (9, 30), (12, 31)):
            end = date(year, month, day)
            if start <= end <= today:
                result.append(end.isoformat())
    return result


def select_financial(filings: list[dict], period: str) -> dict | None:
    base = "10-K" if period.endswith("12-31") else "10-Q"
    candidates = [item for item in filings if item["period"] == period and item["form"] in {base, base + "/A"}]
    wrong = [item for item in filings if item["period"] == period and item["form"].startswith("10-") and item not in candidates]
    if wrong:
        raise DataError(f"Unexpected quarterly/annual form at {period}")
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item["filed"], item["accepted"], item["accession"]))


def source(filing, period: str) -> dict:
    return {
        "accession": filing.accession_no, "form": filing.form, "period": period,
        "filed": str(filing.filing_date), "accepted": str(filing.acceptance_datetime),
        "url": f"https://www.sec.gov/Archives/edgar/data/{filing.cik}/{filing.accession_no.replace('-', '')}/{filing.primary_document}",
    }


def strict_xml(xml: str):
    if not isinstance(xml, str) or not xml or "<!DOCTYPE" in xml.upper() or "<!ENTITY" in xml.upper():
        raise DataError("13F requires XML without DTDs or entities")
    try:
        return ET.fromstring(xml)
    except ET.ParseError as error:
        raise DataError("Invalid 13F XML") from error


def primary_13f(xml: str, descriptor: dict, cik: int) -> dict:
    """Declared SEC schema units, never inferred prices or library overrides."""
    root = strict_xml(xml)
    if root.tag != "{http://www.sec.gov/edgar/thirteenffiler}edgarSubmission":
        raise DataError("Expected SEC 13F primary XML")
    def text(path, required=True):
        nodes = root.findall(path)
        if not nodes and not required:
            return None
        if len(nodes) != 1 or not nodes[0].text or not nodes[0].text.strip():
            raise DataError(f"Missing or ambiguous 13F primary field: {path}")
        return nodes[0].text.strip()
    schema = text("{*}schemaVersion")
    units = {"X0201": 1000, "X0202": 1}
    if schema not in units:
        raise DataError(f"Unsupported or missing 13F unit schema: {schema}")
    if text("{*}headerData/{*}submissionType") != descriptor["form"]:
        raise DataError("13F primary form differs from submissions metadata")
    identifier = text("{*}headerData/{*}filerInfo/{*}filer/{*}credentials/{*}cik")
    if not identifier.isdigit() or int(identifier) != cik:
        raise DataError("13F primary filer CIK differs from requested company")
    for path in ("{*}headerData/{*}filerInfo/{*}periodOfReport", "{*}formData/{*}coverPage/{*}reportCalendarOrQuarter"):
        try:
            period = datetime.strptime(text(path), "%m-%d-%Y").date().isoformat()
        except ValueError as error:
            raise DataError("Invalid 13F cover-page reporting period") from error
        if period != descriptor["period"]:
            raise DataError("13F cover-page period differs from SEC submissions metadata")
    flag = text("{*}formData/{*}coverPage/{*}isAmendment")
    if flag not in {"true", "false"} or (flag == "true") != descriptor["form"].endswith("/A"):
        raise DataError("13F amendment flag differs from its form")
    number = text("{*}formData/{*}coverPage/{*}amendmentNo", required=False)
    kind = text("{*}formData/{*}coverPage/{*}amendmentInfo/{*}amendmentType", required=False)
    if flag == "true":
        if not number or not number.isdigit() or int(number) <= 0 or kind not in {"RESTATEMENT", "NEW HOLDINGS"}:
            raise DataError("Invalid 13F amendment metadata")
    elif number is not None or kind is not None:
        raise DataError("Original 13F has amendment metadata")
    totals = {}
    for key, field in (("count", "tableEntryTotal"), ("raw_total", "tableValueTotal")):
        value = text("{*}formData/{*}summaryPage/{*}" + field)
        if not re.fullmatch(r"[0-9]+", value):
            raise DataError(f"Invalid 13F summary number: {field}")
        totals[key] = int(value)
    return {**totals, "schema_version": schema, "unit_multiplier": units[schema],
            "amendment_type": kind, "amendment_number": int(number) if number else None}


def validate_13f_xml(xml: str) -> list[tuple[int, int]]:
    """Reject malformed XML and missing numbers before SDK parser recovery/defaults."""
    root = strict_xml(xml)
    rows = []
    for entry in root.findall(".//{*}infoTable"):
        for path in ("{*}cusip", "{*}titleOfClass", "{*}shrsOrPrnAmt/{*}sshPrnamtType"):
            element = entry.find(path)
            if element is None or not element.text or not element.text.strip():
                raise DataError(f"Missing 13F field: {path}")
        amounts = []
        for path in ("{*}value", "{*}shrsOrPrnAmt/{*}sshPrnamt"):
            element = entry.find(path)
            text = element.text.strip() if element is not None and element.text else ""
            if not re.fullmatch(r"[0-9]+", text):
                raise DataError(f"Invalid or missing 13F number: {path}")
            amounts.append(int(text))
        rows.append(tuple(amounts))
    if not rows:
        raise DataError("No 13F information-table rows")
    return rows


class EdgarGateway:
    """Single SEC transport/parser. SDK imports are not needed by offline tests."""
    def __init__(self, config: dict):
        identity = validate_identity(os.environ.get("SEC_USER_AGENT", ""))
        rate = os.environ.get("EDGAR_RATE_LIMIT_PER_SEC", "")
        if rate not in {"1", "2", "3"}:
            raise DataError("Set EDGAR_RATE_LIMIT_PER_SEC to 1, 2 or 3 before importing EdgarTools")
        from edgar import Company, set_identity
        set_identity(identity)
        self.company = Company(config["cik"])
        self.since = config["since"]
        self.filings = {}

    def list_filings(self) -> list[dict]:
        filings = self.company.get_filings(form=sorted(FORMS), filing_date=self.since + ":", trigger_full_load=True)
        result = []
        for filing in filings:
            period = str(filing.report_date)
            quarter(period)
            if period < self.since:
                continue
            if not filing.acceptance_datetime:
                raise DataError(f"Missing acceptance timestamp: {filing.accession_no}")
            self.filings[filing.accession_no] = filing
            result.append(source(filing, period))
        return result

    def financial(self, descriptor: dict) -> list[dict]:
        filing = self.filings[descriptor["accession"]]
        xbrl = filing.xbrl()
        if xbrl is None:
            raise DataError(f"No XBRL in {filing.accession_no}; amendments are not silently bypassed")
        return xbrl.facts.get_facts()

    def holdings(self, descriptor: dict) -> dict:
        from edgar import ThirteenF
        filing = self.filings[descriptor["accession"]]
        primary = primary_13f(filing.xml(), descriptor, filing.cik)
        report = ThirteenF(filing, use_latest_period_of_report=False)
        raw_numbers = validate_13f_xml(report.infotable_xml)
        info = report.primary_form_information
        if primary["count"] != len(raw_numbers) or info.summary_page.total_holdings != primary["count"]:
            raise DataError("13F row count differs from cover-page entry count")
        if money(info.summary_page.total_value) != primary["raw_total"]:
            raise DataError("SDK 13F cover value differs from primary XML")
        if info.report_period.date().isoformat() != descriptor["period"]:
            raise DataError("13F cover-page period differs from SEC submissions metadata")
        if (report.amendment_type != primary["amendment_type"]
                or report.amendment_number != primary["amendment_number"]):
            raise DataError("SDK 13F amendment metadata differs from primary XML")
        table = report.raw_infotable
        if table is None or table.empty:
            raise DataError(f"Empty 13F table: {filing.accession_no}")
        fields = ("Cusip", "Class", "Type", "PutCall", "Value", "SharesPrnAmount", "OtherManager")
        missing = set(fields) - set(table.columns)
        if missing:
            raise DataError(f"Missing EdgarTools 13F columns: {sorted(missing)}")
        records = table.to_dict(orient="records")
        if len(records) != len(raw_numbers):
            raise DataError("SDK omitted or added 13F information-table rows")
        rows = []
        for row, (raw_value, raw_shares) in zip(records, raw_numbers):
            normalized = {key: row[key] for key in fields}
            normalized["Value"] = money(normalized["Value"])
            normalized["SharesPrnAmount"] = money(normalized["SharesPrnAmount"])
            if normalized["Value"] != raw_value or normalized["SharesPrnAmount"] != raw_shares:
                raise DataError("SDK monetary/share normalization differs from the source XML")
            normalized["Value"] *= primary["unit_multiplier"]
            rows.append(normalized)
        unit_source = {"schema_version": primary["schema_version"], "multiplier": primary["unit_multiplier"],
                       "basis": "SEC 13F XML schema", "instructions": "https://www.sec.gov/files/form13f.pdf"}
        return {
            **descriptor, "holdings": rows, "source": {**descriptor, "value_units": unit_source},
            "amendment_type": primary["amendment_type"], "amendment_number": primary["amendment_number"],
            "unit_ambiguous": False, "unit_multiplier": primary["unit_multiplier"],
            "total_value": primary["raw_total"] * primary["unit_multiplier"],
        }


def build_data(gateway, config: dict, now: datetime) -> tuple[dict, dict[str, dict]]:
    filings = gateway.list_filings()
    accessions = [item["accession"] for item in filings]
    if len(accessions) != len(set(accessions)):
        raise DataError("Duplicate SEC filing metadata")
    for filing in filings:
        if filing["form"] not in FORMS:
            raise DataError(f"Unexpected filing form: {filing['form']}")
        quarter(filing["period"])
    index, snapshots = [], {}
    for period in periods(config["since"], now.date()):
        financial = select_financial(filings, period)
        reports = [item for item in filings if item["period"] == period and item["form"].startswith("13F-")]
        key = quarter(period)
        if financial is None or not reports:
            missing = []
            if financial is None:
                missing.append("10-K" if period.endswith("12-31") else "10-Q")
            if not reports:
                missing.append("13F")
            index.append({"quarter": key, "period": period, "status": "pending", "missing": missing})
            continue
        observations = extract(gateway.financial(financial), period, config)
        holdings, holdings_sources = merge_reports([gateway.holdings(item) for item in reports])
        snapshot = allocate(observations, holdings, config)
        snapshot.update({"schema_version": 1, "quarter": key, "period": period,
                         "financial_source": financial, "holdings_sources": holdings_sources})
        snapshots[key] = snapshot
        index.append({"quarter": key, "period": period, "status": "complete", "path": f"data/quarters/{key}.json"})
    if not snapshots:
        raise DataError("No quarter has both verified financial statements and 13F holdings")
    return ({"schema_version": 1, "cik": config["cik"], "generated_at": now.isoformat(),
             "latest_complete": max(snapshots), "quarters": index}, snapshots)


def write_site(output: Path, index: dict, snapshots: dict[str, dict]):
    if output.exists():
        raise DataError("Output must be a new directory; existing publications are never overwritten in place")
    output.mkdir(parents=True)
    data = output / "data/quarters"
    data.mkdir(parents=True)
    for key, snapshot in snapshots.items():
        (data / f"{key}.json").write_text(json.dumps(snapshot, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    (output / "data/quarters.json").write_text(json.dumps(index, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    shutil.copyfile(ROOT / "templates/index.html", output / "index.html")
    # Written last. Deployment tests require this marker and the exact snapshot inventory.
    (output / "build.json").write_text(json.dumps({"quarters": sorted(snapshots), "schema_version": 1}) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "inspect"))
    parser.add_argument("--output", type=Path, required=True, help="Fresh output directory, outside the repository")
    args = parser.parse_args()
    try:
        if args.output.exists():
            raise DataError("Output already exists; use a fresh output directory")
        output = args.output.resolve()
        if output == ROOT or ROOT in output.parents:
            raise DataError("Generated output must stay outside the repository")
        config = load_config()
        gateway = EdgarGateway(config)
        if args.command == "build":
            index, snapshots = build_data(gateway, config, datetime.now(timezone.utc))
            write_site(output, index, snapshots)
            print(f"Verified {len(snapshots)} quarter(s): {index['latest_complete']}. Output: {output}")
        else:
            filings = gateway.list_filings()
            financial = [item for item in filings if item["form"].startswith("10-")]
            if not financial:
                raise DataError("No financial statements found")
            newest_period = max(item["period"] for item in financial)
            descriptor = select_financial(financial, newest_period)
            rows = gateway.financial(descriptor)
            output.mkdir(parents=True)
            (output / "facts.json").write_text(json.dumps({"source": descriptor, "facts": rows}, indent=2, default=str) + "\n", encoding="utf-8")
            print(f"XBRL inspection saved outside the repository: {output}")
        return 0
    except (DataError, ImportError) as error:
        print(f"Build stopped: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
