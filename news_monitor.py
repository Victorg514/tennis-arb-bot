"""
News / tournament monitor — confirms withdrawals from external sources.

Current role: observe a price-spike signal and check whether any external
source (Sofascore, ESPN) independently reports a walkover or retirement
for the same match. Runs in one of three modes controlled by
`NEWS_CHECK_MODE` in config:

    log     — check every signal, log the result, do not change behavior.
              Use this first to measure the false-positive rate of the
              price-spike detector before giving the news layer veto power.
    gate    — require a confirming walkover/retirement from at least one
              source, otherwise skip the trade. Safer, but misses trades
              where news lags the order book.
    trigger — future mode: let news drive trades directly (not wired up
              yet; see WithdrawalMonitor for the current signal path).

The two sources run in order; the first one to find the match wins.
Both are fetched in bulk ("today's scheduled tennis matches") and cached
for NEWS_CACHE_TTL seconds so repeated lookups don't hammer upstream.

Match lookup is fuzzy by last-name on both players — Polymarket uses
"Stefanos Sakellaridis" while Sofascore uses "Sakellaridis S." — so both
shapes normalize to 'sakellaridis' and match.

NOTE: Sofascore/ESPN are unofficial upstreams. Their response shapes may
shift without warning. If the detail strings in logs start looking wrong,
inspect the raw JSON and adjust _classify_event / _extract_players.
"""

import logging
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

import httpx

import config

logger = logging.getLogger(__name__)


class MatchStatus(str, Enum):
    WALKOVER = "walkover"    # confirmed walkover — player did not take the court
    RETIRED = "retired"      # player retired mid-match
    NORMAL = "normal"        # match in progress / scheduled / finished normally
    NOT_FOUND = "not_found"  # we could not locate this match in any source
    ERROR = "error"          # all sources errored


@dataclass
class NewsCheckResult:
    status: MatchStatus
    source: str           # which source answered ("sofascore", "espn", "none")
    detail: str = ""      # human-readable info for logs

    @property
    def confirms_withdrawal(self) -> bool:
        return self.status in (MatchStatus.WALKOVER, MatchStatus.RETIRED)


# ----------------------------------------------------------------------
# Name normalization
# ----------------------------------------------------------------------

def _normalize(s: str) -> str:
    """Lowercase, strip accents, drop punctuation. Keeps spaces."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return "".join(c for c in s.lower() if c.isalnum() or c.isspace()).strip()


def _last_name(full_name: str) -> str:
    """Best-effort last-name extraction across common formats.

        'Stefanos Sakellaridis' -> 'sakellaridis'   (Polymarket: first last)
        'Sakellaridis S.'       -> 'sakellaridis'   (Sofascore:  last first-initial)
        'Juan Pablo Varillas'   -> 'varillas'       (three-part: last is last)
        'de Minaur A.'          -> 'minaur'         (nobility particle dropped)
    """
    norm = _normalize(full_name)
    if not norm:
        return ""
    parts = norm.split()
    # Short trailing token is an initial -> last name is the leading token(s)
    if len(parts) >= 2 and len(parts[-1]) <= 2:
        return parts[0] if len(parts[0]) > 2 else parts[-2]
    return parts[-1]


def _match_players(a_target: str, b_target: str, a_candidate: str, b_candidate: str) -> bool:
    """Do the two candidate names match the two targets (either order)?"""
    ta, tb = _last_name(a_target), _last_name(b_target)
    ca, cb = _last_name(a_candidate), _last_name(b_candidate)
    if not (ta and tb and ca and cb):
        return False
    return (ta == ca and tb == cb) or (ta == cb and tb == ca)


# ----------------------------------------------------------------------
# Sources
# ----------------------------------------------------------------------

class SofascoreSource:
    """Sofascore unofficial JSON API — fast, broad coverage (ATP/WTA/CH/ITF)."""

    name = "sofascore"
    BASE_URL = "https://api.sofascore.com/api/v1/sport/tennis/scheduled-events"

    def __init__(self):
        self._client = httpx.Client(
            timeout=10,
            headers={
                # Default python-httpx UA gets 403'd; any modern browser UA works.
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json",
            },
        )

    def fetch_today(self) -> list[dict]:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            resp = self._client.get(f"{self.BASE_URL}/{date}")
            resp.raise_for_status()
            return resp.json().get("events", [])
        except Exception:
            logger.exception("Sofascore fetch failed")
            return []

    @staticmethod
    def extract_players(event: dict) -> tuple[str, str]:
        home = (event.get("homeTeam") or {}).get("name", "")
        away = (event.get("awayTeam") or {}).get("name", "")
        return home, away

    @staticmethod
    def classify(event: dict) -> MatchStatus:
        status = event.get("status") or {}
        desc = (status.get("description") or "").lower()
        type_ = (status.get("type") or "").lower()
        if "walkover" in desc or "walkover" in type_:
            return MatchStatus.WALKOVER
        if "retired" in desc or "retired" in type_:
            return MatchStatus.RETIRED
        return MatchStatus.NORMAL


class ESPNSource:
    """ESPN tennis scoreboard — stable, slightly narrower coverage."""

    name = "espn"
    URLS = [
        "https://site.api.espn.com/apis/site/v2/sports/tennis/atp/scoreboard",
        "https://site.api.espn.com/apis/site/v2/sports/tennis/wta/scoreboard",
    ]

    def __init__(self):
        self._client = httpx.Client(timeout=10)

    def fetch_today(self) -> list[dict]:
        events: list[dict] = []
        for url in self.URLS:
            try:
                resp = self._client.get(url)
                resp.raise_for_status()
                events.extend(resp.json().get("events", []))
            except Exception:
                logger.exception("ESPN fetch failed for %s", url)
        return events

    @staticmethod
    def extract_players(event: dict) -> tuple[str, str]:
        try:
            competitors = event["competitions"][0]["competitors"]
            names = [c.get("athlete", {}).get("displayName", "") for c in competitors[:2]]
            if len(names) == 2:
                return names[0], names[1]
        except (KeyError, IndexError, TypeError):
            pass
        return "", ""

    @staticmethod
    def classify(event: dict) -> MatchStatus:
        try:
            stype = event["competitions"][0]["status"]["type"]
            name = (stype.get("name") or "").upper()
            desc = (stype.get("description") or "").lower()
        except (KeyError, IndexError, TypeError):
            return MatchStatus.NORMAL
        if "WALKOVER" in name or "walkover" in desc or "forfeit" in desc:
            return MatchStatus.WALKOVER
        if "RETIRED" in name or "retired" in desc:
            return MatchStatus.RETIRED
        return MatchStatus.NORMAL


# ----------------------------------------------------------------------
# Aggregator
# ----------------------------------------------------------------------

class NewsMonitor:
    """Cached wrapper over multiple tennis news sources.

    Caches each source's full "today's events" response for NEWS_CACHE_TTL
    seconds. A single check_match() call is therefore O(1) HTTP on a warm
    cache (important — this runs inside the bot's 10s poll loop).
    """

    def __init__(self, cache_ttl: float | None = None):
        self.sofascore = SofascoreSource()
        self.espn = ESPNSource()
        self._cache_ttl = cache_ttl if cache_ttl is not None else config.NEWS_CACHE_TTL
        # source name -> (fetched_at, events list)
        self._cache: dict[str, tuple[float, list[dict]]] = {}

    def _get_events(self, source) -> list[dict]:
        cached = self._cache.get(source.name)
        if cached and (time.time() - cached[0]) < self._cache_ttl:
            return cached[1]
        events = source.fetch_today()
        self._cache[source.name] = (time.time(), events)
        return events

    def check_match(self, player_a: str, player_b: str) -> NewsCheckResult:
        """Look up a specific match across sources and return the first hit.

        Sources are tried in order of priority. Within each source we scan
        all of today's events and match on last-name pairs.
        """
        sources_tried = 0

        # Source 1: Sofascore (primary)
        try:
            events = self._get_events(self.sofascore)
            sources_tried += 1
            for event in events:
                home, away = SofascoreSource.extract_players(event)
                if _match_players(player_a, player_b, home, away):
                    status = SofascoreSource.classify(event)
                    desc = (event.get("status") or {}).get("description", "?")
                    return NewsCheckResult(
                        status=status,
                        source="sofascore",
                        detail=f"{home} vs {away} [{desc}]",
                    )
        except Exception:
            logger.exception("Sofascore lookup failed")

        # Source 2: ESPN (fallback)
        try:
            events = self._get_events(self.espn)
            sources_tried += 1
            for event in events:
                home, away = ESPNSource.extract_players(event)
                if _match_players(player_a, player_b, home, away):
                    status = ESPNSource.classify(event)
                    return NewsCheckResult(
                        status=status,
                        source="espn",
                        detail=f"{home} vs {away}",
                    )
        except Exception:
            logger.exception("ESPN lookup failed")

        if sources_tried == 0:
            return NewsCheckResult(status=MatchStatus.ERROR, source="none")
        return NewsCheckResult(status=MatchStatus.NOT_FOUND, source="none")
