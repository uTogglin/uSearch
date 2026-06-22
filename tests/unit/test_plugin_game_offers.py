# SPDX-License-Identifier: AGPL-3.0-or-later
# pylint: disable=missing-module-docstring,missing-class-docstring,invalid-name,protected-access

import os
import pathlib
import tempfile
import time
import unittest

from searx.plugins import game_offers as go


class GameOffersTokens(unittest.TestCase):

    def test_accent_folding(self):
        self.assertEqual(go._tokens("Pokémon"), {"pokemon"})
        self.assertEqual(go._tokens("Brütal Legend"), {"brutal", "legend"})

    def test_punctuation_and_numbers(self):
        self.assertEqual(go._tokens("Half-Life 2"), {"half", "life", "2"})
        self.assertEqual(go._tokens("S.T.A.L.K.E.R. 2"), {"s", "t", "a", "l", "k", "e", "r", "2"})


class GameOffersNormaliseQuery(unittest.TestCase):

    def test_strips_trailing_platform(self):
        self.assertEqual(go._normalise_query("hades pc"), "hades")
        self.assertEqual(go._normalise_query("elden ring on steam"), "elden ring")

    def test_strips_stacked_qualifiers(self):
        self.assertEqual(go._normalise_query("hades steam key buy cheap pc"), "hades")
        self.assertEqual(go._normalise_query("cyberpunk 2077 cd key price"), "cyberpunk 2077")

    def test_falls_back_when_empty(self):
        # An all-qualifier query must not normalise away to nothing.
        self.assertTrue(go._normalise_query("buy cheap pc game key"))


class GameOffersConfidence(unittest.TestCase):

    def test_full_match_is_confident(self):
        self.assertEqual(go._match_confidence("Hades", "hades"), 1.0)

    def test_shopping_noise_ignored(self):
        # "buy", "pc", "cheapest" are stopwords, so "Hades" fully covers it.
        self.assertEqual(go._match_confidence("Hades", "buy hades pc cheapest"), 1.0)

    def test_wrong_title_is_low_confidence(self):
        self.assertLess(go._match_confidence("Hades II", "stardew valley"), go._MIN_CONFIDENCE)


class GameOffersBestMatch(unittest.TestCase):

    def test_exact_title_wins(self):
        cands = [{"t": "Hades II"}, {"t": "Hades"}, {"t": "Hades' Star"}]
        picked = go._best_match("hades", cands, lambda c: c["t"])
        self.assertEqual(picked["t"], "Hades")

    def test_idf_prefers_distinctive_token(self):
        cands = [{"t": "The Witcher 3: Wild Hunt"}, {"t": "Wild West Online"}]
        picked = go._best_match("the witcher 3 wild hunt", cands, lambda c: c["t"])
        self.assertEqual(picked["t"], "The Witcher 3: Wild Hunt")

    def test_empty(self):
        self.assertIsNone(go._best_match("anything", [], lambda c: c))


class GameOffersCurrency(unittest.TestCase):

    def setUp(self):
        # Prime the FX cache so _convert never hits the network in tests.
        go._fx["rates"] = dict(go._FX_FALLBACK)
        go._fx["ts"] = time.time()

    def test_same_currency_is_identity(self):
        self.assertEqual(go._convert(19.99, "GBP", "GBP"), 19.99)

    def test_usd_to_gbp(self):
        # 10 USD * 0.79 = 7.9 GBP with the fallback table.
        self.assertAlmostEqual(go._convert(10.0, "USD", "GBP"), 7.9, places=2)

    def test_unknown_currency_returns_none(self):
        self.assertIsNone(go._convert(10.0, "USD", "XYZ"))

    def test_price_text_symbols(self):
        self.assertEqual(go._price_text(9.99, "GBP"), "£9.99")
        self.assertEqual(go._price_text(9.99, "USD"), "$9.99")
        self.assertEqual(go._price_text(9.99, "PLN"), "zł 9.99")
        self.assertEqual(go._price_text(None, "GBP"), "")


class GameOffersPersistedFX(unittest.TestCase):
    """The persisted fx_rates.json seed feeds local-currency conversion across
    restarts and suspend/resume cycles."""

    def setUp(self):
        self._orig_file = go._FX_FILE
        self._tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._tmp.close()
        go._FX_FILE = pathlib.Path(self._tmp.name)

    def tearDown(self):
        go._FX_FILE = self._orig_file
        os.unlink(self._tmp.name)

    def test_roundtrip(self):
        rates = {"USD": 1.0, "GBP": 0.5, "EUR": 0.8}
        go._persist_fx(rates)
        loaded = go._load_persisted_fx()
        self.assertEqual(loaded["GBP"], 0.5)
        self.assertEqual(loaded["EUR"], 0.8)

    def test_persisted_seed_overrides_static_fallback(self):
        # A fresher persisted GBP rate must win over the static fallback when the
        # live feed is unreachable.
        go._persist_fx({"USD": 1.0, "GBP": 0.5})
        seed = {**go._FX_FALLBACK, **go._load_persisted_fx()}
        self.assertEqual(seed["GBP"], 0.5)
        self.assertEqual(seed["JPY"], go._FX_FALLBACK["JPY"])  # not in file -> fallback kept

    def test_corrupt_file_degrades_to_empty(self):
        go._FX_FILE.write_text("{ not json", encoding="utf-8")
        self.assertEqual(go._load_persisted_fx(), {})

    def test_bad_usd_base_rejected(self):
        # USD must be the 1.0 base; a table that disagrees is untrustworthy.
        go._FX_FILE.write_text('{"rates": {"USD": 1.1, "GBP": 0.5}}', encoding="utf-8")
        self.assertEqual(go._load_persisted_fx(), {})

    def test_missing_file_degrades_to_empty(self):
        os.unlink(self._tmp.name)
        self.assertEqual(go._load_persisted_fx(), {})
        # recreate so tearDown's unlink succeeds
        go._FX_FILE.write_text("{}", encoding="utf-8")


class GameOffersDedupe(unittest.TestCase):

    def _o(self, store, price):
        return {"store": store, "price": price, "currency": "GBP", "official": True}

    def test_keeps_cheapest_per_store_and_sorts(self):
        rows = go._dedupe_cheapest([
            self._o("Steam", 19.99),
            self._o("steam", 14.99),   # same store, cheaper -> wins
            self._o("GOG", 9.99),
            self._o("Fanatical", 24.99),
        ])
        self.assertEqual([(r["store"], r["price"]) for r in rows],
                         [("GOG", 9.99), ("steam", 14.99), ("Fanatical", 24.99)])

    def test_drops_priceless_rows(self):
        rows = go._dedupe_cheapest([self._o("Steam", None), self._o("GOG", 5.0)])
        self.assertEqual([r["store"] for r in rows], ["GOG"])


class GameOffersAssemble(unittest.TestCase):

    def setUp(self):
        go._fx["rates"] = dict(go._FX_FALLBACK)
        go._fx["ts"] = time.time()

    def _official(self, store, price, source):
        return {"store": store, "price": price, "currency": "GBP", "cut": 0,
                "regular": None, "url": "", "official": True, "source": source}

    def test_merges_sources_cheapest_first(self):
        cheapshark = {"title": "Hades", "steamAppID": "1145360",
                      "official": [self._official("Steam", 19.99, "cheapshark")], "low": 9.99}
        itad = {"title": "Hades",
                "official": [self._official("GOG", 14.99, "itad")], "low": 8.49}

        out = go._assemble("hades", [cheapshark, itad], "GBP", 8)
        self.assertEqual(out["type"], "game")
        self.assertEqual(out["title"], "Hades")
        # Official sorted cheapest-first across sources.
        self.assertEqual([o["store"] for o in out["official"]], ["GOG", "Steam"])
        # Historical low is the cheapest reported across sources.
        self.assertEqual(out["historicalLow"]["price"], 8.49)
        self.assertEqual(out["steamAppID"], "1145360")
        # priceText is filled in for rendering.
        self.assertEqual(out["official"][0]["priceText"], "£14.99")

    def test_no_keyshops_in_payload(self):
        # AllKeyShop removed — the payload is official-stores-only.
        cheapshark = {"title": "Hades", "official": [self._official("Steam", 19.99, "cheapshark")]}
        out = go._assemble("hades", [cheapshark], "GBP", 8)
        self.assertNotIn("keyshops", out)
        self.assertNotIn("keyshopAggregate", out)

    def test_gated_when_no_confident_title(self):
        bad = {"title": "Completely Different Game", "official": [self._official("Steam", 5, "cheapshark")]}
        out = go._assemble("hades", [bad], "GBP", 8)
        self.assertIsNone(out["type"])

    def test_gated_when_no_offers(self):
        out = go._assemble("hades", [{"title": "Hades", "official": [], "keyshops": []}], "GBP", 8)
        self.assertIsNone(out["type"])

    def test_empty_sources(self):
        out = go._assemble("hades", [], "GBP", 8)
        self.assertIsNone(out["type"])


class GameOffersDisplayCurrency(unittest.TestCase):

    def test_known_country(self):
        self.assertEqual(go._display_currency("GB"), "GBP")
        self.assertEqual(go._display_currency("DE"), "EUR")
        self.assertEqual(go._display_currency("US"), "USD")

    def test_unknown_country_falls_back(self):
        # No setting context in a bare unit test -> built-in default GBP.
        self.assertEqual(go._display_currency("ZZ"), "GBP")


if __name__ == "__main__":
    unittest.main()
