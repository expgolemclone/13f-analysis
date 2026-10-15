"""Non-overlapping Berkshire asset allocation. No network or inferred amounts."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
import re


class DataError(ValueError):
    """A filing cannot support a verified allocation."""


def money(value) -> int:
    if isinstance(value, bool) or value is None:
        raise DataError(f"Invalid monetary value: {value!r}")
    try:
        amount = Decimal(str(value))
    except InvalidOperation as error:
        raise DataError(f"Invalid monetary value: {value!r}") from error
    if not amount.is_finite() or amount < 0 or amount != amount.to_integral_value():
        raise DataError(f"Expected nonnegative whole USD, received {value!r}")
    return int(amount)


def quarter(period: str) -> str:
    try:
        day = date.fromisoformat(period)
    except (TypeError, ValueError) as error:
        raise DataError(f"Invalid reporting date: {period!r}") from error
    if (day.month, day.day) not in {(3, 31), (6, 30), (9, 30), (12, 31)}:
        raise DataError(f"Not a calendar quarter end: {period}")
    return f"{day.year}-Q{day.month // 3}"


def qname(value: str) -> str:
    # EdgarTools represents qualified names with either a colon or underscore.
    return value.replace("_", ":", 1) if ":" not in value else value


@dataclass(frozen=True)
class Observation:
    amount: int
    uncertainty: Decimal
    provenance: tuple[dict, ...]


def check_equal(label: str, left: Observation, right: list[Observation]) -> int:
    delta = left.amount - sum(item.amount for item in right)
    tolerance = left.uncertainty + sum((item.uncertainty for item in right), Decimal(0))
    if abs(delta) > tolerance:
        raise DataError(f"{label}: difference {delta:,} USD exceeds filing rounding {tolerance}")
    return delta


class Facts:
    def __init__(self, rows: list[dict], period: str, cik: int):
        quarter(period)
        self.period = period
        self.rows = []
        for row in rows:
            if row.get("period_type") != "instant" or str(row.get("period_instant")) != period:
                continue
            identifier = str(row.get("entity_identifier", ""))
            if not identifier.isdigit() or int(identifier) != cik:
                continue
            self.rows.append(row)

    def matching(self, concept: str, dimensions: dict) -> list[dict]:
        wanted = {qname(key): qname(value) for key, value in dimensions.items()}
        matches = []
        for row in self.rows:
            actual = {qname(key[4:]): qname(value) for key, value in row.items() if key.startswith("dim_")}
            name = qname(row["concept"])
            if name.split(":", 1)[-1] == concept and actual == wanted:
                if row.get("is_dimensioned") and not actual:
                    raise DataError(f"Dimensions missing from EdgarTools fact: {name}")
                matches.append(row)
        return matches

    def select(self, concept: str, dimensions: dict) -> Observation:
        rows = self.matching(concept, dimensions)
        if not rows:
            raise DataError(f"Missing {concept} at {self.period}, dimensions={dimensions}")
        names = {qname(row["concept"]) for row in rows}
        if len(names) != 1:
            raise DataError(f"Ambiguous namespaces for {concept}: {sorted(names)}")
        if any(row.get("currency") != "USD" for row in rows):
            raise DataError(f"Expected USD for {concept}")
        values = {money(row["numeric_value"]) for row in rows}
        if len(values) != 1:
            raise DataError(f"Conflicting facts for {concept}: {sorted(values)}")
        uncertainties = []
        for row in rows:
            decimals = str(row["decimals"])
            if decimals == "INF":
                uncertainties.append(Decimal(0))
            elif re.fullmatch(r"-?\d+", decimals):
                uncertainties.append(Decimal(10) ** -int(decimals) / 2)
            else:
                raise DataError(f"Invalid decimals for {concept}: {decimals}")
        provenance = tuple({
            "concept": qname(row["concept"]), "context": row["context_ref"],
            "dimensions": dimensions, "decimals": str(row["decimals"]), "unit": "USD",
        } for row in sorted(rows, key=lambda item: item["context_ref"]))
        return Observation(values.pop(), min(uncertainties), provenance)


def extract(rows: list[dict], period: str, config: dict) -> dict[str, Observation]:
    facts = Facts(rows, period, config["cik"])
    result = {}
    for key, (concept, scope) in config["facts"].items():
        dimensions = ({config["scope_axis"]: config["scopes"][scope]}
                      if isinstance(scope, str) else scope or {})
        result[key] = facts.select(concept, dimensions)
    # A selected ordinary stock changing to equity-method accounting requires review.
    for security in config["equities"]:
        dimensions = {"srt:ScheduleOfEquityMethodInvestmentEquityMethodInvesteeNameAxis": security["member"]}
        if facts.matching("EquityMethodInvestments", dimensions):
            raise DataError(f"Accounting classification changed for {security['ticker']}")
    return result


def aggregate_holdings(rows: list[dict]) -> dict[str, int]:
    values = {}
    for row in rows:
        required = {"Cusip", "Class", "Type", "PutCall", "Value", "SharesPrnAmount"}
        if not required <= row.keys():
            raise DataError(f"Missing 13F fields: {sorted(required - row.keys())}")
        cusip = row["Cusip"]
        if not isinstance(cusip, str) or not re.fullmatch(r"[A-Z0-9]{9}", cusip):
            raise DataError(f"Invalid CUSIP: {cusip!r}")
        amount = money(row["Value"])
        money(row["SharesPrnAmount"])
        if (not isinstance(row["PutCall"], str) or row["PutCall"].upper() not in {"", "PUT", "CALL"}
                or row["Type"] not in {"Shares", "Principal"}):
            raise DataError("Unrecognized 13F instrument type")
        # Options and principal-amount instruments must not become ordinary stock slices.
        if row["PutCall"] == "" and row["Type"] == "Shares":
            values[cusip] = values.get(cusip, 0) + amount
    return values


def merge_reports(reports: list[dict]) -> tuple[list[dict], list[dict]]:
    """Restatements replace; new-holdings amendments append, never silently replace."""
    ordered = sorted(reports, key=lambda item: (item["filed"], item["accepted"], item["accession"]))
    if not ordered:
        raise DataError("No 13F reports")
    periods = {item["period"] for item in ordered}
    if len(periods) != 1:
        raise DataError("13F reports have different periods")
    holdings, sources, previous_number = None, [], 0
    accessions = set()
    for report in ordered:
        if report["accession"] in accessions:
            raise DataError("Duplicate 13F accession")
        accessions.add(report["accession"])
        rows = report["holdings"]
        if not rows:
            raise DataError("Empty 13F information table")
        if report["unit_ambiguous"]:
            raise DataError("Ambiguous 13F monetary units")
        raw_sum = sum(money(row["Value"]) for row in rows)
        if report["unit_multiplier"] not in {1, 1000}:
            raise DataError("Unsupported 13F monetary unit multiplier")
        tolerance = Decimal(report["unit_multiplier"]) * len(rows) / 2
        if abs(raw_sum - money(report["total_value"])) > tolerance:
            raise DataError("13F information table does not reconcile to cover-page total")
        kind = report["amendment_type"]
        if report["form"] == "13F-HR":
            if holdings is not None or kind is not None:
                raise DataError("Multiple original 13F reports or invalid amendment metadata")
            holdings, sources = list(rows), [report["source"]]
        elif report["form"] == "13F-HR/A":
            number = report["amendment_number"]
            if not isinstance(number, int) or isinstance(number, bool) or number <= previous_number:
                raise DataError("Missing or non-increasing 13F amendment number")
            previous_number = number
            if kind == "RESTATEMENT":
                holdings, sources = list(rows), [report["source"]]
            elif kind == "NEW HOLDINGS" and holdings is not None:
                # Repeated rows may indicate a replayed amendment rather than additional holdings.
                signatures = {tuple(sorted(row.items())) for row in holdings}
                if any(tuple(sorted(row.items())) in signatures for row in rows):
                    raise DataError("Overlapping 13F new-holdings amendment needs review")
                holdings.extend(rows)
                sources.append(report["source"])
            else:
                raise DataError(f"Unsupported or orphaned 13F amendment: {kind}")
        else:
            raise DataError(f"Unexpected 13F form: {report['form']}")
    aggregate_holdings(holdings)
    return holdings, sources


INSURANCE_KEYS = (
    "cash_insurance", "treasury_bills", "fixed_income", "equities", "equity_method", "loans",
    "receivables_insurance", "inventories", "ppe_insurance", "lease_equipment",
    "goodwill_insurance", "intangibles_insurance", "deferred_reinsurance", "other_insurance",
)
RAIL_ENERGY_KEYS = (
    "cash_rail_energy", "receivables_rail_energy", "ppe_rail_energy", "goodwill_rail_energy",
    "regulatory_assets", "other_rail_energy",
)


def allocate(observations: dict[str, Observation], holdings: list[dict], config: dict) -> dict:
    o = observations
    checks = {
        "insurance_balance_sheet": check_equal("Insurance and Other", o["insurance_total"], [o[key] for key in INSURANCE_KEYS]),
        "rail_energy_balance_sheet": check_equal("Railroad, Utilities and Energy", o["rail_energy_total"], [o[key] for key in RAIL_ENERGY_KEYS]),
        "consolidated_balance_sheet": check_equal("Consolidated assets", o["total"], [o["insurance_total"], o["rail_energy_total"]]),
        "rail_energy_ppe": check_equal("BNSF + BHE PP&E", o["ppe_rail_energy"], [o["bnsf"], o["bhe"]]),
    }
    total = o["total"].amount
    if total <= 0:
        raise DataError("Total assets must be positive")
    intangibles_rail = o["intangibles_rail_energy_gross"].amount - o["intangibles_rail_energy_amortization"].amount
    if not 0 <= intangibles_rail <= o["other_rail_energy"].amount:
        raise DataError("Rail/energy intangible assets exceed their balance-sheet parent")
    if o["oxy_carrying"].amount > o["equity_method"].amount:
        raise DataError("OXY exceeds total equity-method investments")
    stock_values = aggregate_holdings(holdings)
    slices = []

    def add(key, label, amount, measurement, note):
        if amount < 0:
            raise DataError(f"Negative allocation: {label}")
        slices.append({"key": key, "label": label, "amount": amount,
                       "measurement": measurement, "note": note})

    add("cash", "Cash & equivalents", o["cash_insurance"].amount + o["cash_rail_energy"].amount,
        "carrying value", "Includes Treasury bills classified as cash equivalents. Restricted cash remains in Other.")
    add("treasury_bills", "T-Bills", o["treasury_bills"].amount, "carrying value",
        "Short-term investments outside cash equivalents. Includes unsettled purchases; liabilities are not netted.")
    named_equities = 0
    for security in config["equities"]:
        # Absence from a verified full 13F means no disclosed position, not zero assets in Berkshire.
        amount = stock_values.get(security["cusip"], 0)
        named_equities += amount
        note = "13F ordinary shares at quarter-end market value; confidential positions can remain in Other equities."
        if security["ticker"] == "GOOGL":
            note += " Alphabet Class A only. GOOG Class C, if held, remains in Other equities."
        add(security["ticker"], security["ticker"], amount, "fair value", note)
    add("OXY", "OXY", o["oxy_carrying"].amount, "equity-method carrying value",
        "Common stock only, not 13F market value. Preferred stock and warrants remain in Other equities.")
    add("other_equities", "Other equities", o["equities"].amount - named_equities, "fair value",
        "Balance-sheet equity securities less the displayed fair-value stocks. Includes non-13F investments.")
    add("bnsf", "BNSF - PP&E", o["bnsf"].amount, "net carrying value",
        "Property, plant and equipment only. Not BNSF total assets, enterprise value or shareholders' equity.")
    add("bhe", "BHE - PP&E", o["bhe"].amount, "net carrying value",
        "Property, plant and equipment only. Not BHE total assets, enterprise value or shareholders' equity.")
    add("fixed_income", "Fixed income", o["fixed_income"].amount, "reported carrying value",
        "Investments in fixed maturity securities, separate from short-term Treasury bills.")
    add("equity_method", "Equity-method investments", o["equity_method"].amount - o["oxy_carrying"].amount,
        "equity-method carrying value", "Remaining equity-method investments after removing OXY common stock.")
    add("receivables", "Loans & receivables", sum(o[key].amount for key in ("loans", "receivables_insurance", "receivables_rail_energy")),
        "net carrying value", "Includes insurance premiums, reinsurance and trade receivables, net of allowances.")
    add("intangibles", "Goodwill & intangibles",
        sum(o[key].amount for key in ("goodwill_insurance", "goodwill_rail_energy", "intangibles_insurance")) + intangibles_rail,
        "net carrying value", "Includes rail/energy intangible assets originally reported inside Other assets.")
    other_parts = [
        {"label": label, "amount": o[key].amount}
        for key, label in (
            ("inventories", "Inventories"), ("ppe_insurance", "Insurance/other PP&E"),
            ("lease_equipment", "Equipment held for lease"), ("deferred_reinsurance", "Deferred reinsurance charges"),
            ("regulatory_assets", "Regulatory assets"), ("other_insurance", "Insurance/other miscellaneous assets"),
        )
    ]
    other_parts.append({"label": "Rail/energy other assets excluding intangibles", "amount": o["other_rail_energy"].amount - intangibles_rail})
    rounding = sum(checks.values())
    other_parts.append({"label": "Disclosed-precision reconciliation", "amount": rounding})
    other = total - sum(item["amount"] for item in slices)
    if other != sum(item["amount"] for item in other_parts):
        raise DataError("Other allocation does not reconcile to its disclosed components")
    add("other", "Other", other, "reported carrying value", "See the disclosed component breakdown, including rounding reconciliation.")
    return {
        "total_assets": total, "slices": slices, "other_breakdown": other_parts,
        "reconciliation": checks,
        "components": {key: {"amount": item.amount, "sources": list(item.provenance)} for key, item in o.items()},
    }
