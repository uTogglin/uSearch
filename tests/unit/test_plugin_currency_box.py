# SPDX-License-Identifier: AGPL-3.0-or-later
# pylint: disable=missing-module-docstring,missing-class-docstring,invalid-name,protected-access

import unittest

from searx.plugins import currency_box as cb


class CurrencyResolve(unittest.TestCase):

    def test_iso_codes(self):
        self.assertEqual(cb._resolve_currency("usd"), "USD")
        self.assertEqual(cb._resolve_currency("GBP"), "GBP")
        self.assertEqual(cb._resolve_currency(" eur "), "EUR")

    def test_symbols(self):
        self.assertEqual(cb._resolve_currency("$"), "USD")
        self.assertEqual(cb._resolve_currency("£"), "GBP")
        self.assertEqual(cb._resolve_currency("€"), "EUR")
        self.assertEqual(cb._resolve_currency("¥"), "JPY")

    def test_multi_char_symbol_prefix(self):
        self.assertEqual(cb._resolve_currency("A$"), "AUD")
        self.assertEqual(cb._resolve_currency("C$"), "CAD")
        self.assertEqual(cb._resolve_currency("R$"), "BRL")

    def test_colloquial_words(self):
        self.assertEqual(cb._resolve_currency("dollars"), "USD")
        self.assertEqual(cb._resolve_currency("pounds"), "GBP")
        self.assertEqual(cb._resolve_currency("euros"), "EUR")
        self.assertEqual(cb._resolve_currency("yen"), "JPY")

    def test_colloquial_last_word(self):
        # A multi-word colloquial name resolves on its final word.
        self.assertEqual(cb._resolve_currency("us dollars"), "USD")
        self.assertEqual(cb._resolve_currency("japanese yen"), "JPY")

    def test_unknown_is_none(self):
        self.assertIsNone(cb._resolve_currency("school"))
        self.assertIsNone(cb._resolve_currency(""))


class CurrencyParse(unittest.TestCase):

    def test_amount_and_pair(self):
        self.assertEqual(cb._parse_query("25 gbp to usd"), (25.0, "GBP", "USD"))
        self.assertEqual(cb._parse_query("convert 100 usd to eur"), (100.0, "USD", "EUR"))

    def test_default_amount_is_one(self):
        self.assertEqual(cb._parse_query("gbp to usd"), (1.0, "GBP", "USD"))
        self.assertEqual(cb._parse_query("euro to yen"), (1.0, "EUR", "JPY"))

    def test_separators(self):
        self.assertEqual(cb._parse_query("50 usd in inr"), (50.0, "USD", "INR"))
        self.assertEqual(cb._parse_query("10 eur into gbp"), (10.0, "EUR", "GBP"))
        self.assertEqual(cb._parse_query("usd = eur"), (1.0, "USD", "EUR"))

    def test_symbol_amount(self):
        self.assertEqual(cb._parse_query("$25 to eur"), (25.0, "USD", "EUR"))

    def test_thousands_separator(self):
        self.assertEqual(cb._parse_query("1,500 usd to gbp"), (1500.0, "USD", "GBP"))

    def test_rejects_non_currency(self):
        self.assertIsNone(cb._parse_query("back to school"))
        self.assertIsNone(cb._parse_query("how to learn python"))
        self.assertIsNone(cb._parse_query("just a normal query"))


class CurrencyCrossRate(unittest.TestCase):

    def test_identity(self):
        self.assertEqual(cb._cross_rate("USD", "USD"), 1.0)
        self.assertEqual(cb._cross_rate("EUR", "EUR"), 1.0)

    def test_cross_rate_matches_table(self):
        rates = cb._load_fx()
        self.assertIn("GBP", rates)
        self.assertIn("EUR", rates)
        # 1 GBP in EUR == (EUR per USD) / (GBP per USD).
        expected = rates["EUR"] / rates["GBP"]
        self.assertAlmostEqual(cb._cross_rate("GBP", "EUR"), expected, places=6)

    def test_unknown_pair_is_none(self):
        self.assertIsNone(cb._cross_rate("USD", "ZZZ"))


def _set_matrix(rates):
    """Swap a fake EUR-base matrix into the in-memory store, returning the old
    one so a test can restore it."""
    old = dict(cb._ts)
    cb._ts["rates"] = cb._normalise_matrix(rates)
    cb._ts["dates"] = sorted(cb._ts["rates"])
    cb._ts["fetched_at"] = 1.0
    return old


def _restore_matrix(old):
    cb._ts.clear()
    cb._ts.update(old)


class CurrencySeries(unittest.TestCase):

    def test_cross_division_from_matrix(self):
        # EUR→USD and EUR→GBP per day; GBP→USD = USD/GBP.
        old = _set_matrix({
            "2026-06-20": {"USD": 1.10, "GBP": 0.85},
            "2026-06-21": {"USD": 1.12, "GBP": 0.84},
        })
        try:
            s = cb._pair_series("GBP", "USD")
        finally:
            _restore_matrix(old)
        self.assertEqual([p["d"] for p in s], ["2026-06-20", "2026-06-21"])
        self.assertAlmostEqual(s[0]["v"], 1.10 / 0.85, places=6)
        self.assertAlmostEqual(s[1]["v"], 1.12 / 0.84, places=6)

    def test_eur_leg_uses_implicit_one(self):
        old = _set_matrix({"2026-06-21": {"USD": 1.12}})
        try:
            s = cb._pair_series("EUR", "USD")  # EUR is implicitly 1.0
        finally:
            _restore_matrix(old)
        self.assertAlmostEqual(s[-1]["v"], 1.12, places=6)

    def test_uncovered_pair_is_empty(self):
        old = _set_matrix({"2026-06-21": {"USD": 1.12}})
        try:
            self.assertEqual(cb._pair_series("USD", "XAU"), [])
        finally:
            _restore_matrix(old)

    def test_refresh_swaps_in_memory(self):
        fetched = {"2026-06-21": {"USD": 1.12, "GBP": 0.84}}
        old = dict(cb._ts)
        orig = cb._fetch_matrix
        cb._fetch_matrix = lambda days: cb._normalise_matrix(fetched)
        # Don't touch the real data file during the test.
        orig_persist = cb._persist_ts
        cb._persist_ts = lambda rates: None
        try:
            self.assertTrue(cb._refresh_timeseries())
            self.assertIn("2026-06-21", cb._ts["rates"])
            self.assertEqual(cb._ts["rates"]["2026-06-21"]["EUR"], 1.0)
        finally:
            cb._fetch_matrix = orig
            cb._persist_ts = orig_persist
            _restore_matrix(old)


class CurrencyBuild(unittest.TestCase):

    def test_series_drives_rate_and_result(self):
        old = _set_matrix({
            "2026-06-20": {"USD": 1.10, "GBP": 0.85},
            "2026-06-21": {"USD": 1.12, "GBP": 0.84},
        })
        try:
            d = cb._build(25.0, "GBP", "USD")
        finally:
            _restore_matrix(old)
        self.assertEqual(d["type"], "currency")
        self.assertEqual(d["from"], "GBP")
        self.assertEqual(d["to"], "USD")
        # Spot rate is the chart's latest point, so the number lines up with it.
        last = round(1.12 / 0.84, 6)
        self.assertEqual(d["rate"], last)
        self.assertEqual(d["result"], round(25.0 * last, 4))
        self.assertEqual(len(d["series"]), 2)

    def test_falls_back_to_cross_rate_without_series(self):
        old = _set_matrix({})  # empty matrix → no chart
        try:
            d = cb._build(1.0, "GBP", "EUR")
        finally:
            _restore_matrix(old)
        self.assertEqual(d["series"], [])
        self.assertAlmostEqual(d["rate"], cb._cross_rate("GBP", "EUR"), places=6)

    def test_dropdown_includes_active_pair(self):
        codes = {c[0] for c in cb._dropdown("GBP", "USD")}
        self.assertIn("GBP", codes)
        self.assertIn("USD", codes)


class CurrencyAnswer(unittest.TestCase):

    def test_answer_text_from_matrix(self):
        old = _set_matrix({"2026-06-21": {"USD": 1.12, "GBP": 0.84}})
        try:
            ans = cb.answer_for("25 gbp to usd")
        finally:
            _restore_matrix(old)
        self.assertIsNotNone(ans)
        text, _url = ans
        rate = 1.12 / 0.84
        self.assertEqual(text, f"25 GBP = {cb._fmt_amount(round(25 * rate, 4))} USD")

    def test_answer_none_for_non_currency(self):
        self.assertIsNone(cb.answer_for("back to school"))
        self.assertIsNone(cb.answer_for("python tutorial"))

    def test_fmt_amount_trims_zeros(self):
        self.assertEqual(cb._fmt_amount(25.0), "25")
        self.assertEqual(cb._fmt_amount(33.11), "33.11")
        self.assertEqual(cb._fmt_amount(1500), "1,500")


if __name__ == "__main__":
    unittest.main()
