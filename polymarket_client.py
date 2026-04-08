"""
Polymarket CLOB client for searching tennis markets and placing trades.

Flow:
1. Fetch open tennis match events from Polymarket Gamma (`fetch_open_tennis_matches`)
2. When a player withdraws, locate the relevant market (`find_tennis_market`)
3. Buy opponent shares at current (low) price
4. After Polymarket resets to 50/50, sell for profit
"""

import json
import logging
import time
from dataclasses import dataclass

import httpx
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

import config

logger = logging.getLogger(__name__)


@dataclass
class TennisMatch:
    """An open Polymarket head-to-head tennis match we could trade on.

    This is the raw "watchlist" entry — populated from Gamma events before we
    know whether any player has withdrawn. Converted into a `MarketMatch` once
    a specific player is flagged as withdrawn.
    """
    event_id: str
    event_slug: str
    event_title: str              # e.g. "Madrid: Stefanos Sakellaridis vs Toby Samuel"
    tournament: str               # parsed prefix, e.g. "Madrid"
    condition_id: str             # the H2H market's condition_id
    market_question: str
    player_a: str                 # full name
    player_b: str                 # full name
    token_id_a: str
    token_id_b: str
    price_a: float
    price_b: float
    match_start: str              # ISO8601 — actual match kick-off (Gamma `startTime`)
    end_date: str                 # ISO8601 — event window end (Gamma `endDate`)


@dataclass
class MarketMatch:
    """A Polymarket H2H market structured for the underdog-buy trade.

    By convention:
        underdog_* — the lower-priced side we BUY (target of the arb)
        favorite_* — the higher-priced side (we do not trade it)

    Regardless of who actually withdrew, the trade is always: buy the
    underdog. Polymarket resets the market to ~50/50 after a walkover, so
    the cheaper side always has upside to ~0.50.
    """
    condition_id: str
    question: str
    market_slug: str
    underdog_token_id: str
    underdog_name: str
    underdog_price: float
    favorite_token_id: str
    favorite_name: str
    favorite_price: float

    @classmethod
    def from_tennis_match(cls, tm: "TennisMatch") -> "MarketMatch":
        """Build a MarketMatch by labeling the cheaper side as underdog."""
        if tm.price_a <= tm.price_b:
            underdog = (tm.player_a, tm.token_id_a, tm.price_a)
            favorite = (tm.player_b, tm.token_id_b, tm.price_b)
        else:
            underdog = (tm.player_b, tm.token_id_b, tm.price_b)
            favorite = (tm.player_a, tm.token_id_a, tm.price_a)
        return cls(
            condition_id=tm.condition_id,
            question=tm.market_question,
            market_slug=tm.event_slug,
            underdog_name=underdog[0],
            underdog_token_id=underdog[1],
            underdog_price=underdog[2],
            favorite_name=favorite[0],
            favorite_token_id=favorite[1],
            favorite_price=favorite[2],
        )


class PolymarketClient:
    """Handles Polymarket market search and order execution."""

    def __init__(self):
        self.gamma = httpx.Client(
            base_url=config.POLY_GAMMA_URL,
            timeout=15,
        )

        # CLOB client for placing orders
        self.clob = ClobClient(
            config.POLY_CLOB_URL,
            key=config.POLY_API_KEY,
            chain_id=137,  # Polygon
            funder=config.POLY_PRIVATE_KEY,
        )

        if config.POLY_API_KEY and config.POLY_API_SECRET:
            self.clob.set_api_creds(self.clob.create_or_derive_api_creds())
            logger.info("Polymarket CLOB client initialized with API credentials")
        else:
            logger.warning("No Polymarket API credentials - running in search-only mode")

        self._traded_markets: set[str] = set()  # condition_ids we've already acted on

    # ------------------------------------------------------------------
    # Watchlist — fetch every open tennis H2H match
    # ------------------------------------------------------------------

    def fetch_open_tennis_matches(self) -> list[TennisMatch]:
        """Return all currently-open H2H tennis match markets on Polymarket.

        Uses Gamma `/events?tag_slug=tennis`. Each event contains multiple
        sub-markets (H2H, O/U, handicaps, Set 1 Winner, etc.). We keep only
        the H2H — identified as the sub-market whose `question` matches the
        parent event title.

        Tournament-winner ("outright") events are skipped — they don't have
        the "X vs Y" structure we can arb.
        """
        try:
            resp = self.gamma.get("/events", params={
                "tag_slug": "tennis",
                "closed": "false",
                "active": "true",
                "limit": 500,
            })
            resp.raise_for_status()
            events = resp.json()
        except Exception:
            logger.exception("Failed to fetch tennis events from Gamma")
            return []

        matches: list[TennisMatch] = []
        for event in events:
            tm = self._event_to_tennis_match(event)
            if tm is not None:
                matches.append(tm)

        logger.info("Fetched %d open tennis H2H matches from Polymarket", len(matches))
        return matches

    def _event_to_tennis_match(self, event: dict) -> TennisMatch | None:
        """Extract the H2H market from a Gamma event, or None if not an H2H."""
        title = event.get("title", "")
        if " vs " not in title.lower():
            # Tournament-winner / outright event — not a head-to-head
            return None

        # Find the H2H sub-market: question should match (or start with) the event title
        h2h = None
        for m in event.get("markets", []):
            if m.get("closed") or m.get("archived") or not m.get("active"):
                continue
            q = m.get("question", "")
            if q == title or q.startswith(title):
                h2h = m
                break

        if h2h is None:
            return None

        # Parse the JSON-encoded fields
        try:
            outcomes = json.loads(h2h.get("outcomes", "[]"))
            prices = json.loads(h2h.get("outcomePrices", "[]"))
            token_ids = json.loads(h2h.get("clobTokenIds", "[]"))
        except (json.JSONDecodeError, TypeError):
            logger.debug("Could not parse market JSON fields for event %s", title)
            return None

        if len(outcomes) != 2 or len(prices) != 2 or len(token_ids) != 2:
            return None

        # The two outcomes must look like player names, not Over/Under etc.
        if any(o.lower() in {"over", "under", "yes", "no"} for o in outcomes):
            return None

        # Tournament prefix: "Madrid: Stefanos Sakellaridis vs Toby Samuel" -> "Madrid"
        tournament = title.split(":", 1)[0].strip() if ":" in title else ""

        # `startTime` is the real match kick-off; `startDate` is when the
        # market was listed (sometimes days earlier). We want the former.
        match_start = event.get("startTime") or event.get("eventDate") or ""

        return TennisMatch(
            event_id=str(event.get("id", "")),
            event_slug=event.get("slug", ""),
            event_title=title,
            tournament=tournament,
            condition_id=h2h.get("conditionId", ""),
            market_question=h2h.get("question", ""),
            player_a=outcomes[0],
            player_b=outcomes[1],
            token_id_a=token_ids[0],
            token_id_b=token_ids[1],
            price_a=float(prices[0]),
            price_b=float(prices[1]),
            match_start=match_start,
            end_date=event.get("endDate", ""),
        )

    # ------------------------------------------------------------------
    # Trading
    # ------------------------------------------------------------------

    def get_current_price(self, token_id: str) -> float:
        """Get current best ask price for a token."""
        try:
            book = self.clob.get_order_book(token_id)
            if book and book.asks:
                return float(book.asks[0].price)
        except Exception:
            logger.exception("Failed to get price for token %s", token_id)
        return 0.0

    def buy_opponent_shares(self, market: MarketMatch) -> dict | None:
        """Buy the underdog side of the market.

        "Opponent" here means the cheaper (underdog) side — the target of the
        underdog-buy strategy, regardless of who actually withdrew. Kept the
        historical method name so existing callers don't churn.
        """
        if market.condition_id in self._traded_markets:
            logger.warning("Already traded %s, skipping", market.condition_id)
            return None

        # Fresh price from the CLOB — Gamma's cached price can be slightly
        # stale during the brief walkover window.
        current_price = self.get_current_price(market.underdog_token_id)
        if current_price <= 0:
            current_price = market.underdog_price

        if current_price <= 0:
            logger.error("Cannot determine price for %s", market.underdog_name)
            return None

        if current_price > config.MAX_BUY_PRICE:
            logger.info(
                "Price %.2f > max %.2f for %s, skipping",
                current_price, config.MAX_BUY_PRICE, market.underdog_name,
            )
            return None

        shares = config.MAX_BET_USDC / current_price
        size = round(shares, 2)

        logger.info(
            "BUY ORDER: %s shares of '%s' @ %.4f (total ~$%.2f) | Market: %s",
            size, market.underdog_name, current_price,
            size * current_price, market.question,
        )

        if config.DRY_RUN:
            logger.info("[DRY RUN] Would place buy order - not executing")
            self._traded_markets.add(market.condition_id)
            return {"dry_run": True, "side": "buy", "price": current_price, "size": size}

        try:
            order_args = OrderArgs(
                price=current_price,
                size=size,
                side=BUY,
                token_id=market.underdog_token_id,
            )
            signed_order = self.clob.create_order(order_args)
            result = self.clob.post_order(signed_order, OrderType.GTC)

            self._traded_markets.add(market.condition_id)
            logger.info("Buy order placed: %s", result)
            return result
        except Exception:
            logger.exception("Failed to place buy order for %s", market.underdog_name)
            return None

    def sell_shares(self, market: MarketMatch, price: float | None = None) -> dict | None:
        """Sell the underdog shares after the market resets toward 50/50."""
        sell_price = price or config.SELL_TARGET_PRICE

        current_price = self.get_current_price(market.underdog_token_id)
        if current_price < sell_price * 0.95:
            logger.info(
                "Price %.4f hasn't reached sell target %.4f yet for %s",
                current_price, sell_price, market.underdog_name,
            )
            return None

        size = round(config.MAX_BET_USDC / config.MAX_BUY_PRICE, 2)

        logger.info(
            "SELL ORDER: %s shares of '%s' @ %.4f | Market: %s",
            size, market.underdog_name, sell_price, market.question,
        )

        if config.DRY_RUN:
            logger.info("[DRY RUN] Would place sell order - not executing")
            return {"dry_run": True, "side": "sell", "price": sell_price, "size": size}

        try:
            order_args = OrderArgs(
                price=sell_price,
                size=size,
                side=SELL,
                token_id=market.underdog_token_id,
            )
            signed_order = self.clob.create_order(order_args)
            result = self.clob.post_order(signed_order, OrderType.GTC)

            logger.info("Sell order placed: %s", result)
            return result
        except Exception:
            logger.exception("Failed to place sell order for %s", market.underdog_name)
            return None

    def monitor_for_reset(self, market: MarketMatch, timeout: int = 600) -> bool:
        """Poll until the underdog price reaches the ~50/50 reset target."""
        start = time.time()
        check_interval = 5  # seconds

        logger.info("Monitoring %s for 50/50 reset (timeout: %ds)...", market.question, timeout)

        while time.time() - start < timeout:
            price = self.get_current_price(market.underdog_token_id)
            if price >= config.SELL_TARGET_PRICE * 0.95:
                logger.info("Reset detected! %s price now at %.4f", market.underdog_name, price)
                return True

            logger.debug("Price check: %s @ %.4f (waiting for %.4f)",
                         market.underdog_name, price, config.SELL_TARGET_PRICE)
            time.sleep(check_interval)

        logger.warning("Timeout waiting for reset on %s", market.question)
        return False
