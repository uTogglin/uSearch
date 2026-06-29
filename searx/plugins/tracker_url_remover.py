# SPDX-License-Identifier: AGPL-3.0-or-later
# pylint: disable=missing-module-docstring, unused-argument

import logging
import typing as t
from urllib.parse import urlsplit, parse_qsl

from flask_babel import gettext  # pyright: ignore[reportUnknownVariableType]

from searx.data import TRACKER_PATTERNS

from . import Plugin, PluginInfo

if t.TYPE_CHECKING:
    import flask
    from searx.search import SearchWithPlugins
    from searx.extended_types import SXNG_Request
    from searx.result_types import Result, LegacyResult  # pyright: ignore[reportPrivateLocalImportUsage]
    from searx.plugins import PluginCfg


log = logging.getLogger("searx.plugins.tracker_url_remover")


# Known tracking query-parameters mapped to the company behind them and whether
# the parameter is an *advertising* click identifier (vs generic analytics).  The
# shield's "blocked on this link" breakdown uses this so several ``utm_*`` params
# collapse to a single "Google Analytics" entry rather than counting five times.
# Keys are matched as a prefix against the lowercased parameter name; the broad
# ClearURLs ruleset still does the actual stripping — this only *labels* what it
# removed, so anything not listed here is still counted as a generic tracker.
_PARAM_OWNERS: "list[tuple[str, str, bool]]" = [
    ("gclid", "Google Ads", True),
    ("gclsrc", "Google Ads", True),
    ("gbraid", "Google Ads", True),
    ("wbraid", "Google Ads", True),
    ("gad_source", "Google Ads", True),
    ("dclid", "Google Marketing", True),
    ("wt_mc", "Webtrekk", False),
    ("utm_", "Google Analytics", False),
    ("ga_", "Google Analytics", False),
    ("_ga", "Google Analytics", False),
    ("fbclid", "Meta / Facebook", True),
    ("fb_action", "Meta / Facebook", True),
    ("igshid", "Instagram", False),
    ("msclkid", "Microsoft Ads", True),
    ("ttclid", "TikTok", True),
    ("twclid", "Twitter / X Ads", True),
    ("yclid", "Yandex Ads", True),
    ("ysclid", "Yandex", False),
    ("li_fat_id", "LinkedIn Ads", True),
    ("epik", "Pinterest", True),
    ("mc_cid", "Mailchimp", False),
    ("mc_eid", "Mailchimp", False),
    ("_hsenc", "HubSpot", True),
    ("_hsmi", "HubSpot", True),
    ("hsa_", "HubSpot", True),
    ("mkt_tok", "Marketo", True),
    ("vero_", "Vero", False),
    ("oly_", "Omeda", False),
    ("_openstat", "OpenStat", False),
    ("s_kwcid", "Adobe", True),
    ("ef_id", "Adobe", True),
]


def _query_keys(url: str) -> "list[str]":
    try:
        return [k for k, _ in parse_qsl(urlsplit(url).query, keep_blank_values=True)]
    except ValueError:
        return []


def _owner(param: str) -> "tuple[str, bool]":
    p = param.lower()
    for prefix, owner, is_ad in _PARAM_OWNERS:
        if p.startswith(prefix):
            return owner, is_ad
    return "Tracking parameter", False


def _summarise_stripped(original: str, cleaned: str) -> "dict | None":
    """Diff the query params of a URL before/after cleaning and label what went.

    Returns ``{"names": [...], "ads": int}`` (companies deduped, ad-companies
    counted), or None when nothing was stripped.
    """
    before = _query_keys(original)
    if not before:
        return None
    after = set(_query_keys(cleaned))
    removed = [k for k in before if k not in after]
    if not removed:
        return None
    # company -> is_ad, preserving first-seen order so the list reads naturally;
    # this is the "filter out duplicates" step — five utm_* params collapse to a
    # single "Google Analytics" entry rather than counting five times.
    owners: "dict[str, bool]" = {}
    for key in removed:
        owner, is_ad = _owner(key)
        owners[owner] = owners.get(owner, False) or is_ad
    return {
        "names": list(owners.items()),  # [(company, is_ad), ...]
        "ads": sum(1 for v in owners.values() if v),
    }


def _stash(result: "t.Any", summary: dict) -> None:
    """Attach the stripped-tracker summary to a result for the SERP template.

    Result objects are dict-like (LegacyResult) or attribute-based (typed
    Result); set both ways defensively and never raise into the result pipeline.
    """
    # Encode each company as "Name" or "Name!" (trailing "!" = advertising) so the
    # client can colour ad chips without a second lookup.
    names = ",".join(n + ("!" if ad else "") for n, ad in summary["names"])[:300]
    try:
        result["trk_n"] = len(summary["names"])
        result["trk_ad"] = summary["ads"]
        result["trk_names"] = names
        return
    except (TypeError, KeyError):
        pass
    try:
        result.trk_n = len(summary["names"])  # type: ignore[attr-defined]
        result.trk_ad = summary["ads"]  # type: ignore[attr-defined]
        result.trk_names = names  # type: ignore[attr-defined]
    except (AttributeError, TypeError):
        pass


@t.final
class SXNGPlugin(Plugin):
    """Remove trackers arguments from the returned URL."""

    id = "tracker_url_remover"

    def __init__(self, plg_cfg: "PluginCfg") -> None:

        super().__init__(plg_cfg)
        self.info = PluginInfo(
            id=self.id,
            name=gettext("Tracker URL remover"),
            description=gettext("Remove trackers arguments from the returned URL"),
            preference_section="privacy",
        )

    def init(self, app: "flask.Flask") -> bool:
        TRACKER_PATTERNS.init()
        return True

    def on_result(self, request: "SXNG_Request", search: "SearchWithPlugins", result: "Result") -> bool:

        # Remember the result's main URL before cleaning so we can tell the user
        # which tracking params we stripped from this exact link (the shield's
        # "blocked on this link" breakdown).
        original = getattr(result, "url", None)
        if original is None:
            try:
                original = result["url"]  # LegacyResult is dict-like
            except (TypeError, KeyError):
                original = None

        result.filter_urls(self.filter_url_field)

        if original:
            cleaned = getattr(result, "url", None)
            if cleaned is None:
                try:
                    cleaned = result["url"]
                except (TypeError, KeyError):
                    cleaned = original
            summary = _summarise_stripped(original, cleaned)
            if summary:
                _stash(result, summary)
        return True

    @classmethod
    def filter_url_field(cls, result: "Result|LegacyResult", field_name: str, url_src: str) -> bool | str:
        """Returns bool ``True`` to use URL unchanged (``False`` to ignore URL).
        If URL should be modified, the returned string is the new URL to use."""

        if not url_src:
            log.debug("missing a URL in field %s", field_name)
            return True

        return TRACKER_PATTERNS.clean_url(url=url_src)
