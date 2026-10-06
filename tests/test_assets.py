import copy
from decimal import Decimal
import unittest

from tests.support import FIXTURE, fact_rows, load_config, report
from assets import DataError, Facts, Observation, aggregate_holdings, allocate, extract, merge_reports, money, quarter


class AssetsTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config()
        self.rows = fact_rows()
        self.observations = extract(self.rows, FIXTURE["period"], self.config)
        self.holdings = copy.deepcopy(FIXTURE["synthetic_13f"])

    def allocation(self):
        return allocate(self.observations, self.holdings, self.config)

    def amounts(self):
        return {item["key"]: item["amount"] for item in self.allocation()["slices"]}

    def test_official_balance_sheet_totals_reconcile(self):
        data = self.allocation()
        self.assertEqual(data["total_assets"], 1_263_071_000_000)
        self.assertEqual(sum(item["amount"] for item in data["slices"]), data["total_assets"])
        self.assertTrue(all(delta == 0 for delta in data["reconciliation"].values()))
        self.assertEqual(len(data["slices"]), 17)

    def test_oxy_is_carrying_value_not_13f_market_value(self):
        amounts = self.amounts()
        self.assertEqual(amounts["OXY"], 10_727_000_000)
        self.assertEqual(amounts["equity_method"], 9_221_000_000)
        self.holdings[6]["Value"] *= 2
        self.assertEqual(self.amounts(), amounts)

    def test_khc_market_value_is_not_added(self):
        before = self.amounts()
        self.holdings[7]["Value"] *= 10
        self.assertEqual(self.amounts(), before)

    def test_13f_subdivides_equity_securities_only(self):
        amounts = self.amounts()
        selected = sum(amounts[item["ticker"]] for item in self.config["equities"])
        self.assertEqual(selected + amounts["other_equities"], 323_779_000_000)
        self.assertEqual(amounts["other_equities"], 143_779_000_000)

    def test_goog_is_not_merged_into_googl(self):
        before = self.amounts()
        self.holdings[-1]["Value"] *= 20
        self.assertEqual(self.amounts(), before)

    def test_missing_disclosed_stock_keeps_amount_in_other_equities(self):
        self.holdings = [row for row in self.holdings if row["Cusip"] != "02079K305"]
        amounts = self.amounts()
        self.assertEqual(amounts["GOOGL"], 0)
        self.assertEqual(amounts["other_equities"], 173_779_000_000)

    def test_ppe_cash_and_intangibles_do_not_overlap(self):
        amounts = self.amounts()
        self.assertEqual(amounts["bnsf"], 72_776_000_000)
        self.assertEqual(amounts["bhe"], 113_039_000_000)
        self.assertEqual(amounts["cash"], 40_609_000_000)
        self.assertEqual(amounts["intangibles"], 118_108_000_000)
        self.assertEqual(amounts["other"], 148_952_000_000)
        self.assertEqual(sum(item["amount"] for item in self.allocation()["other_breakdown"]), amounts["other"])

    def test_reported_rounding_is_explicit_not_forced_normalization(self):
        item = self.observations["total"]
        self.observations["total"] = Observation(item.amount + 1_000_000, item.uncertainty, item.provenance)
        data = self.allocation()
        self.assertEqual(data["reconciliation"]["consolidated_balance_sheet"], 1_000_000)
        self.assertEqual(data["other_breakdown"][-1]["amount"], 1_000_000)

    def test_corrupt_balance_sheet_stops_allocation(self):
        item = self.observations["total"]
        self.observations["total"] = Observation(item.amount + 20_000_000, item.uncertainty, item.provenance)
        with self.assertRaisesRegex(DataError, "Consolidated assets"):
            self.allocation()

    def test_ppe_subtotal_mismatch_stops_allocation(self):
        item = self.observations["bnsf"]
        self.observations["bnsf"] = Observation(item.amount + 20_000_000, item.uncertainty, item.provenance)
        with self.assertRaisesRegex(DataError, "PP&E"):
            self.allocation()

    def test_negative_other_equities_is_not_clipped(self):
        self.holdings[0]["Value"] = 400_000_000_000
        with self.assertRaisesRegex(DataError, "Negative allocation"):
            self.allocation()

    def test_oxy_cannot_exceed_parent(self):
        self.observations["oxy_carrying"] = Observation(30_000_000_000, Decimal(0), ())
        with self.assertRaisesRegex(DataError, "OXY exceeds"):
            self.allocation()

    def test_intangible_amortization_cannot_exceed_gross(self):
        self.observations["intangibles_rail_energy_amortization"] = Observation(3_000_000_000, Decimal(0), ())
        with self.assertRaisesRegex(DataError, "intangible assets"):
            self.allocation()

    def test_exact_period_and_dimensions_exclude_note_subtotals(self):
        extra = copy.deepcopy(self.rows[0])
        extra["period_instant"] = "2025-12-31"
        extra["numeric_value"] = 1
        self.rows.append(extra)
        extra = copy.deepcopy(self.rows[0])
        extra["dim_us-gaap_EquityMethodInvestmentNonconsolidatedInvesteeAxis"] = "brka:OccidentalPetroleumCorporationMember"
        extra["numeric_value"] = 80_000_000_000
        self.rows.append(extra)
        self.assertEqual(extract(self.rows, FIXTURE["period"], self.config)["total"].amount, 1_263_071_000_000)

    def test_duration_and_foreign_entity_facts_are_not_selected(self):
        for field, value in (("period_type", "duration"), ("entity_identifier", "1234")):
            with self.subTest(field=field):
                rows = copy.deepcopy(self.rows)
                rows[0][field] = value
                with self.assertRaisesRegex(DataError, "Missing Assets"):
                    extract(rows, FIXTURE["period"], self.config)

    def test_missing_fact_is_not_zero(self):
        with self.assertRaisesRegex(DataError, "Missing Assets"):
            extract(self.rows[1:], FIXTURE["period"], self.config)

    def test_different_extra_dimension_is_not_accepted(self):
        self.rows[0]["dim_srt_ConsolidationItemsAxis"] = "us-gaap:OperatingSegmentsMember"
        with self.assertRaisesRegex(DataError, "Missing Assets"):
            extract(self.rows, FIXTURE["period"], self.config)

    def test_conflicting_duplicate_facts_stop(self):
        duplicate = copy.deepcopy(self.rows[0])
        duplicate["numeric_value"] += 1
        self.rows.append(duplicate)
        with self.assertRaisesRegex(DataError, "Conflicting"):
            extract(self.rows, FIXTURE["period"], self.config)

    def test_identical_duplicate_facts_are_counted_once(self):
        self.rows.append(copy.deepcopy(self.rows[0]))
        self.assertEqual(extract(self.rows, FIXTURE["period"], self.config)["total"].amount, 1_263_071_000_000)

    def test_namespace_collision_stops(self):
        duplicate = copy.deepcopy(self.rows[0])
        duplicate["concept"] = "different:Assets"
        self.rows.append(duplicate)
        with self.assertRaisesRegex(DataError, "Ambiguous namespaces"):
            extract(self.rows, FIXTURE["period"], self.config)

    def test_usd_and_precision_are_required(self):
        for field, value in (("currency", "EUR"), ("decimals", "unknown"), ("numeric_value", None)):
            with self.subTest(field=field):
                rows = copy.deepcopy(self.rows)
                rows[0][field] = value
                with self.assertRaises(DataError):
                    extract(rows, FIXTURE["period"], self.config)

    def test_qname_underscore_encoding(self):
        for row in self.rows:
            row["concept"] = row["concept"].replace(":", "_")
        self.assertEqual(extract(self.rows, FIXTURE["period"], self.config)["total"].amount, 1_263_071_000_000)

    def test_selected_equity_method_classification_change_stops(self):
        extra = copy.deepcopy(self.rows[-1])
        axis = "dim_srt_ScheduleOfEquityMethodInvestmentEquityMethodInvesteeNameAxis"
        extra[axis] = "brka:AmericanExpressCompanyMember"
        self.rows.append(extra)
        with self.assertRaisesRegex(DataError, "AXP"):
            extract(self.rows, FIXTURE["period"], self.config)

    def test_bad_money_is_rejected(self):
        for amount in (None, True, -1, 1.25, "NaN", "Infinity", "not money"):
            with self.subTest(amount=amount), self.assertRaises(DataError):
                money(amount)
        self.assertEqual(money("1200000000000.0"), 1_200_000_000_000)

    def test_only_calendar_quarters(self):
        self.assertEqual(quarter("2025-12-31"), "2025-Q4")
        for invalid in ("2026-06-29", "2026-02-30", "bad", None):
            with self.subTest(invalid=invalid), self.assertRaises(DataError):
                quarter(invalid)

    def test_options_and_principal_are_not_ordinary_equity(self):
        base = self.holdings[0]
        rows = [base, *({**base, "PutCall": kind} for kind in ("Call", "Put", "CALL", "PUT")),
                {**base, "Type": "Principal"}]
        self.assertEqual(aggregate_holdings(rows)[base["Cusip"]], base["Value"])

    def test_duplicate_cusip_manager_rows_are_aggregated(self):
        row = self.holdings[0]
        self.assertEqual(aggregate_holdings([row, row])[row["Cusip"]], row["Value"] * 2)

    def test_invalid_13f_row_stops(self):
        for field, value in (("Cusip", "BAD"), ("Value", None), ("Type", "unknown"), ("PutCall", "unknown")):
            with self.subTest(field=field), self.assertRaises(DataError):
                aggregate_holdings([{**self.holdings[0], field: value}])
        with self.assertRaises(DataError):
            aggregate_holdings([{"Cusip": "037833100"}])


class AmendmentTests(unittest.TestCase):
    def setUp(self):
        self.base = report()
        self.new = {**FIXTURE["synthetic_13f"][0], "Cusip": "012345678", "Value": 20_000, "SharesPrnAmount": 100}

    def amendment(self, kind, rows, number=1):
        amended = report(rows, "13F-HR/A", kind=kind, number=number, accession=f"0001193125-26-{352200 + number:06}")
        amended["filed"] = "2026-08-11"
        amended["accepted"] = f"2026-08-11T12:0{number}:00Z"
        return amended

    def test_restatement_replaces_original(self):
        amend = self.amendment("RESTATEMENT", [self.new])
        rows, sources = merge_reports([amend, self.base])
        self.assertEqual(rows, [self.new])
        self.assertEqual(sources, [amend["source"]])

    def test_new_holdings_append_to_original(self):
        amend = self.amendment("NEW HOLDINGS", [self.new])
        rows, sources = merge_reports([amend, self.base])
        self.assertEqual(len(rows), len(self.base["holdings"]) + 1)
        self.assertEqual(len(sources), 2)

    def test_restatement_then_new_holdings(self):
        rows, _ = merge_reports([self.base, self.amendment("RESTATEMENT", [self.new]),
                                  self.amendment("NEW HOLDINGS", [self.base["holdings"][0]], 2)])
        self.assertEqual(len(rows), 2)

    def test_new_holdings_before_restatement_are_superseded(self):
        rows, sources = merge_reports([self.base, self.amendment("NEW HOLDINGS", [self.new]),
                                        self.amendment("RESTATEMENT", [self.base["holdings"][0]], 2)])
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(sources), 1)

    def test_duplicate_or_ambiguous_amendments_stop(self):
        for kind in (None, "UNKNOWN"):
            with self.subTest(kind=kind), self.assertRaises(DataError):
                merge_reports([self.base, self.amendment(kind, [self.new])])
        with self.assertRaises(DataError):
            merge_reports([self.base, self.base])
        with self.assertRaises(DataError):
            merge_reports([self.amendment("NEW HOLDINGS", [self.new])])
        with self.assertRaises(DataError):
            merge_reports([self.base, self.amendment("NEW HOLDINGS", self.base["holdings"])])

    def test_non_increasing_amendment_numbers_stop(self):
        for number in (0, None, True):
            with self.subTest(number=number), self.assertRaises(DataError):
                amended = self.amendment("RESTATEMENT", [self.new])
                amended["amendment_number"] = number
                merge_reports([self.base, amended])

    def test_unit_ambiguity_and_cover_mismatch_stop(self):
        bad = copy.deepcopy(self.base)
        bad["unit_ambiguous"] = True
        with self.assertRaisesRegex(DataError, "Ambiguous"):
            merge_reports([bad])
        bad["unit_ambiguous"] = False
        bad["total_value"] += 1000
        with self.assertRaisesRegex(DataError, "cover-page"):
            merge_reports([bad])

    def test_historical_thousands_precision_is_not_multiplied_again(self):
        old = report([{**self.new, "Value": 1_000_000}])
        old["unit_multiplier"] = 1000
        old["total_value"] = 1_000_400
        rows, _ = merge_reports([old])
        self.assertEqual(rows[0]["Value"], 1_000_000)

    def test_mixed_periods_stop(self):
        amended = self.amendment("NEW HOLDINGS", [self.new])
        amended["period"] = "2026-03-31"
        with self.assertRaisesRegex(DataError, "different periods"):
            merge_reports([self.base, amended])

    def test_empty_report_is_not_zero_holdings(self):
        with self.assertRaisesRegex(DataError, "Empty"):
            merge_reports([report([])])


if __name__ == "__main__":
    unittest.main()
