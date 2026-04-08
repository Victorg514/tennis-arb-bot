"""
Withdrawal detector — driven by the Polymarket watchlist.

Two detectors run in parallel against the same Polymarket snapshot. The
whole point of this bot is to react *before* Polymarket does — so the
primary signal is a sudden price move on an open market, not market
closure.

    1. Price-spike detector (primary, fast):
       When walkover news hits, the favorite's side crashes and the
       underdog's side rises. Between two polls, any match where the
       underdog's price jumped upward by >= PRICE_SPIKE_THRESHOLD is
       flagged as a withdrawal. This fires while the market is still
       open, well before Gamma closes it.

    2. Vanish detector (backup, slow):
       If we miss the spike (e.g., we weren't polling, or the whole
       move happened inside a single poll interval), we still catch it
       once Polymarket closes the market — the condition_id disappears
       from the next snapshot. This is the same signal the earlier
       version of the bot used.

Both detectors emit the same `Withdrawal` event, and both are deduped by
condition_id so we never act on the same market twice.
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import config
from polymarket_client import PolymarketClient, TennisMatch

logger = logging.getLogger(__name__)


@dataclass
class Withdrawal:
    """A detected vanishing-match signal.

    We don't actually know (or care) which player withdrew — the bet is
    always on the underdog. `tournament_name` and `tour` are for logging;
    `match` carries the data the trading path actually consumes.
    """
    tournament_name: str   # e.g. "Madrid"
    tour: str              # "atp" or "wta" — best-effort inferred from market
    match: TennisMatch | None = None  # the underlying Polymarket match, if available
    detected_at: float = field(default_factory=time.time)


class WithdrawalMonitor:
    """Polls Polymarket's open tennis matches and detects withdrawals by diff.

    The previous (tournament-scraping) design tried to be the authoritative
    source of "who's entered where." That's the wrong target — what we
    actually need is "which Polymarket matches could we trade." The two are
    not the same: Polymarket lists qualies and Challengers we'd otherwise
    miss, and skips tournaments we have no market on.
    """

    def __init__(self, poly: PolymarketClient | None = None):
        self.poly = poly or PolymarketClient()
        # condition_id -> TennisMatch snapshot from the previous poll
        self._known_matches: dict[str, TennisMatch] = {}
        self._initialized = False
        # condition_ids we've already emitted a withdrawal for, so the same
        # match doesn't get re-fired every poll (e.g. spike detector fires,
        # then the vanish detector would fire on the same cid seconds later)
        self._emitted: set[str] = set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def poll(self) -> list[Withdrawal]:
        """Re-fetch the Polymarket watchlist and return any detected withdrawals.

        On first call, just snapshots the current state and returns [].
        """
        current_matches = self.poly.fetch_open_tennis_matches()
        current_by_cid = {m.condition_id: m for m in current_matches if m.condition_id}

        if not self._initialized:
            self._known_matches = current_by_cid
            self._initialized = True
            logger.info("Initial Polymarket snapshot: %d open H2H matches", len(current_by_cid))
            return []

        withdrawals: list[Withdrawal] = []

        # --- Detector 1: price spike (primary, fast) --------------------
        # For any match present in BOTH the previous and current snapshot,
        # look for the underdog's price rising sharply. That's the shape of
        # walkover news hitting: the favorite's side collapses, pulling the
        # underdog up toward 0.50. We want to catch it here, not after the
        # market closes.
        for cid, curr in current_by_cid.items():
            if cid in self._emitted:
                continue
            prev = self._known_matches.get(cid)
            if prev is None:
                continue
            if not self._is_pre_match(curr):
                continue

            prev_underdog_price = min(prev.price_a, prev.price_b)
            # The "underdog" is identified from the *previous* snapshot —
            # we want to see whether that same token rose. Track by token id.
            prev_underdog_token = (
                prev.token_id_a if prev.price_a <= prev.price_b else prev.token_id_b
            )
            if curr.token_id_a == prev_underdog_token:
                curr_underdog_price = curr.price_a
            elif curr.token_id_b == prev_underdog_token:
                curr_underdog_price = curr.price_b
            else:
                continue  # token set changed — can't compare

            delta = curr_underdog_price - prev_underdog_price
            if delta >= config.PRICE_SPIKE_THRESHOLD:
                w = Withdrawal(
                    tournament_name=curr.tournament or "Unknown",
                    tour=self._infer_tour(curr),
                    match=curr,
                )
                withdrawals.append(w)
                self._emitted.add(cid)
                logger.warning(
                    "PRICE SPIKE: %s — underdog %.2f → %.2f (Δ %.2f) — will buy underdog",
                    curr.event_title, prev_underdog_price, curr_underdog_price, delta,
                )

        # --- Detector 2: vanish (backup, slow) --------------------------
        # If we missed the spike (or the whole move happened inside one
        # poll interval) we still catch the walkover once Polymarket closes
        # the market and the cid drops out of the next snapshot.
        vanished = set(self._known_matches.keys()) - set(current_by_cid.keys())
        for cid in vanished:
            if cid in self._emitted:
                continue
            prev = self._known_matches[cid]
            if not self._is_pre_match(prev):
                # Match already started (or we can't tell) — disappearance is
                # probably normal resolution, not a withdrawal. Skip.
                logger.debug("Ignoring vanished match (already started): %s", prev.event_title)
                continue

            w = Withdrawal(
                tournament_name=prev.tournament or "Unknown",
                tour=self._infer_tour(prev),
                match=prev,
            )
            withdrawals.append(w)
            self._emitted.add(cid)
            logger.warning(
                "VANISH DETECTED: %s — will buy underdog side",
                prev.event_title,
            )

        # Update snapshot for next poll
        self._known_matches = current_by_cid
        return withdrawals

    # ------------------------------------------------------------------
    # Heuristics
    # ------------------------------------------------------------------

    @staticmethod
    def _is_pre_match(m: TennisMatch) -> bool:
        """Was the match scheduled to start in the future at snapshot time?

        Uses the event's `startTime` (actual kick-off), not `startDate`
        (which is just when the market was listed). If the kick-off is still
        in the future, any disappearance is likely a withdrawal. If it's
        past, the disappearance is normal post-match resolution and gets
        ignored.
        """
        if not m.match_start:
            return True  # unknown — assume pre-match (conservative: emit)
        try:
            # Handle both "2026-04-09T09:00:00Z" and "2026-04-09" (eventDate fallback)
            s = m.match_start
            if "T" not in s:
                s = s + "T00:00:00+00:00"
            else:
                s = s.replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
        except ValueError:
            return True
        return dt > datetime.now(timezone.utc)

    @staticmethod
    def _infer_tour(m: TennisMatch) -> str:
        """Best-effort tour inference from event slug / title.

        Gamma event slugs look like 'atp-saka-samuel-2026-04-09' or
        'wta-siegemu-samsono-2026-01-18'. We just look for the prefix.
        """
        slug = (m.event_slug or "").lower()
        if slug.startswith("atp"):
            return "atp"
        if slug.startswith("wta"):
            return "wta"
        return "unknown"
