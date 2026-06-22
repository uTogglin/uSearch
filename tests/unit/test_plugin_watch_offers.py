# SPDX-License-Identifier: AGPL-3.0-or-later
# pylint: disable=missing-module-docstring,missing-class-docstring,invalid-name,protected-access

import unittest

from searx.plugins import watch_offers as wo


def _edge(title, *, offers=True):
    """Build a popularTitles edge the way JustWatch's GraphQL returns it."""
    return {"node": {"content": {"title": title}, "offers": [{"x": 1}] if offers else []}}


class WatchOffersTokens(unittest.TestCase):

    def test_accent_folding(self):
        # "Pokémon" must fold to {pokemon} so it matches an un-accented query;
        # without folding the regex splits the é into {pok, mon}.
        self.assertEqual(wo._tokens("Pokémon"), {"pokemon"})
        self.assertIn("pokemon", wo._tokens("Pokémon"))
        self.assertEqual(wo._tokens("Amélie"), {"amelie"})

    def test_punctuation_and_case(self):
        self.assertEqual(wo._tokens("Black & White"), {"black", "white"})
        self.assertEqual(wo._tokens("Spider-Man: Homecoming"), {"spider", "man", "homecoming"})


class WatchOffersNormalise(unittest.TestCase):

    def test_strips_season_marker(self):
        self.assertEqual(wo._normalise_query("raising dion season 2"), "raising dion")
        self.assertEqual(wo._normalise_query("the bear s3e01"), "the bear")

    def test_strips_trailing_media_qualifier(self):
        self.assertEqual(wo._normalise_query("pokemon black and white series"), "pokemon black and white")
        self.assertEqual(wo._normalise_query("the bear tv series"), "the bear")
        self.assertEqual(wo._normalise_query("attack on titan anime"), "attack on titan")

    def test_keeps_qualifier_word_mid_title(self):
        # "series" only strips at the end — generic phrases keep their meaning.
        self.assertEqual(wo._normalise_query("time series analysis"), "time series analysis")

    def test_never_strips_bare_show_or_movie(self):
        # Real titles end in these words; they must survive.
        self.assertEqual(wo._normalise_query("the truman show"), "the truman show")
        self.assertEqual(wo._normalise_query("the lego movie"), "the lego movie")

    def test_falls_back_when_empty(self):
        self.assertEqual(wo._normalise_query("series"), "series")


class WatchOffersConfidence(unittest.TestCase):

    def test_full_match_is_confident(self):
        self.assertEqual(wo._match_confidence("The Bear", "the bear"), 1.0)

    def test_platform_noise_ignored(self):
        # "apple tv" are qualifiers, so "Severance" fully covers the content.
        self.assertEqual(wo._match_confidence("Severance", "severance apple tv"), 1.0)

    def test_parent_title_is_low_confidence(self):
        # "Pokémon" covers only "pokemon" of {pokemon, black, white}.
        self.assertLess(wo._match_confidence("Pokémon", "pokemon black and white"), wo._MIN_CONFIDENCE)

    def test_all_qualifier_query_not_gated(self):
        self.assertEqual(wo._match_confidence("Anything", "the show"), 1.0)


class WatchOffersPickTitle(unittest.TestCase):

    def test_idf_prefers_distinctive_token(self):
        # The real failure: a 2002 film literally titled "Black and White" used
        # to outrank the "Pokémon" show on raw token-count coverage. IDF weights
        # the rare "pokemon" above the generic black/and/white.
        edges = [
            _edge("Colin in Black and White"),
            _edge("Pokémon"),
            _edge("Black and White"),
            _edge("Black & White & Sex"),
        ]
        picked = wo._pick_title(edges, "pokemon black and white series")
        self.assertEqual(picked["content"]["title"], "Pokémon")

    def test_exact_match_wins(self):
        edges = [_edge("Thunderbirds"), _edge("Thunderbirds Are Go")]
        picked = wo._pick_title(edges, "thunderbirds are go")
        self.assertEqual(picked["content"]["title"], "Thunderbirds Are Go")

    def test_shorter_title_wins_for_bare_query(self):
        edges = [_edge("Thunderbirds Are Go"), _edge("Thunderbirds")]
        picked = wo._pick_title(edges, "thunderbirds")
        self.assertEqual(picked["content"]["title"], "Thunderbirds")

    def test_empty_edges(self):
        self.assertIsNone(wo._pick_title([], "anything"))
