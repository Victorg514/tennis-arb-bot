"""
News / tournament monitor — confirms withdrawals from external sources.

Current role: observe a price-spike signal and check whether any external
source (X/@EntryLists, Sofascore, ESPN) independently reports a walkover
or retirement for the same match. Runs in one of three modes controlled
by `NEWS_CHECK_MODE` in config:

    log     — check every signal, log the result, do not change behavior.
              Use this first to measure the false-positive rate of the
              price-spike detector before giving the news layer veto power.
    gate    — require a confirming walkover/retirement from at least one
              source, otherwise skip the trade. Safer, but misses trades
              where news lags the order book.
    trigger — future mode: let news drive trades directly (not wired up
              yet; see WithdrawalMonitor for the current signal path).

Sources run in priority order; the first one to find the match wins.
Order: X (@EntryLists via Nitter) → Sofascore → ESPN. X is checked first
because EntryLists posts withdrawals well before structured APIs reflect
them. Sofascore/ESPN are fetched in bulk ("today's scheduled tennis
matches") and cached for NEWS_CACHE_TTL seconds. X is fetched as a recent
tweet timeline and cached the same way.

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

# Keyword sets for parsing free-text tweets. Word-boundary matched, so
# "WD" won't match "forward" and " ret " won't match "retreat". Order
# matters only for the detail string — both are checked.
_WALKOVER_KEYWORDS = (
    "walkover", "w/o", "wd", "withdraw", "withdraws", "withdrew",
    "withdrawn", "withdrawal", "pulls out", "pulled out", "out of",
)
_RETIRED_KEYWORDS = ("retired", "retires", "retirement", "ret")


def _tweet_mentions_keyword(text_norm: str, keywords: tuple[str, ...]) -> str | None:
    """Return the matched keyword if any appears as a whole word/phrase in text_norm.

    text_norm is expected to be lowercased & padded with spaces on both
    sides so simple substring checks act as word-boundary checks for
    short tokens like 'wd' and 'ret'.
    """
    for kw in keywords:
        if " " in kw:
            if kw in text_norm:
                return kw
        else:
            if f" {kw} " in text_norm:
                return kw
    return None


class XSource:
    """X / Twitter via a local Nitter instance — top-priority news source.

    Scrapes the @EntryLists timeline (configurable) and parses each tweet
    for withdrawal / retirement keywords near a target player's last
    name. EntryLists tweets typically announce a single player pulling
    out of a tournament, so we hit on EITHER target player's last name
    appearing alongside a keyword (we do not require both names).
    """

    name = "x"

    def __init__(self):
        self.account = config.X_ACCOUNT
        self.instance = config.X_NITTER_INSTANCE
        self.fetch_count = config.X_FETCH_COUNT
        self._scraper = None  # lazy — avoids ntscraper import at module load

    def _get_scraper(self):
        if self._scraper is not None:
            return self._scraper
        try:
            from ntscraper import Nitter
            import ntscraper.nitter as ntr
        except ImportError:
            logger.warning("ntscraper not installed — X source disabled")
            return None

        # Same avatar-bug guard as scrape_entrylists.py: ntscraper crashes
        # with IndexError when a tweet's user block is missing fields.
        if not getattr(ntr.Nitter._get_user, "_patched", False):
            _orig = ntr.Nitter._get_user

            def _safe(self, tweet, is_encrypted):
                try:
                    return _orig(self, tweet, is_encrypted)
                except IndexError:
                    uname = tweet.find("a", class_="username")
                    fname = tweet.find("a", class_="fullname")
                    return {
                        "id": None,
                        "username": uname.text.lstrip("@") if uname else "unknown",
                        "fullname": fname.text if fname else "unknown",
                        "avatar_url": None,
                    }

            _safe._patched = True
            ntr.Nitter._get_user = _safe

        self._scraper = Nitter(
            log_level=1,
            skip_instance_check=True,
            instance=self.instance,
        )
        return self._scraper

    def fetch_recent(self) -> list[dict]:
        scraper = self._get_scraper()
        if scraper is None:
            return []
        try:
            result = scraper.get_tweets(self.account, "user", self.fetch_count)
            return result.get("tweets", []) or []
        except Exception:
            logger.exception("X/Nitter fetch failed for @%s", self.account)
            return []

    @staticmethod
    def classify_tweet(text: str, player_a: str, player_b: str) -> tuple[MatchStatus, str]:
        """Inspect one tweet for a withdrawal/retirement of either player.

        Returns (status, matched_player_name). status is NORMAL when the
        tweet doesn't apply.
        """
        if not text:
            return MatchStatus.NORMAL, ""
        text_norm = " " + _normalize(text) + " "

        la = _last_name(player_a)
        lb = _last_name(player_b)
        matched = ""
        if la and f" {la} " in text_norm:
            matched = player_a
        elif lb and f" {lb} " in text_norm:
            matched = player_b
        if not matched:
            return MatchStatus.NORMAL, ""

        if _tweet_mentions_keyword(text_norm, _WALKOVER_KEYWORDS):
            return MatchStatus.WALKOVER, matched
        if _tweet_mentions_keyword(text_norm, _RETIRED_KEYWORDS):
            return MatchStatus.RETIRED, matched
        return MatchStatus.NORMAL, ""


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
        self.x = XSource() if config.X_SOURCE_ENABLED else None
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

    def _get_x_tweets(self) -> list[dict]:
        """Cached fetch of recent @EntryLists tweets via Nitter."""
        if self.x is None:
            return []
        cached = self._cache.get(self.x.name)
        if cached and (time.time() - cached[0]) < self._cache_ttl:
            return cached[1]
        tweets = self.x.fetch_recent()
        self._cache[self.x.name] = (time.time(), tweets)
        return tweets

    def check_match(self, player_a: str, player_b: str) -> NewsCheckResult:
        """Look up a specific match across sources and return the first hit.

        Sources are tried in order of priority. X (@EntryLists) is the
        top-priority source because tweet announcements typically lead
        the structured APIs by minutes. Sofascore and ESPN follow as
        bulk-event fallbacks.
        """
        sources_tried = 0

        # Source 1: X / @EntryLists (top priority)
        if self.x is not None:
            try:
                tweets = self._get_x_tweets()
                if tweets:
                    sources_tried += 1
                for tweet in tweets:
                    text = tweet.get("text", "")
                    status, matched = XSource.classify_tweet(text, player_a, player_b)
                    if status in (MatchStatus.WALKOVER, MatchStatus.RETIRED):
                        snippet = " ".join(text.split())[:140]
                        return NewsCheckResult(
                            status=status,
                            source="x",
                            detail=f"@{self.x.account}: {matched} — {snippet}",
                        )
            except Exception:
                logger.exception("X/Nitter lookup failed")

        # Source 2: Sofascore
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

        # Source 3: ESPN (fallback)
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
