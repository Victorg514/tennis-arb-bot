"""
Tennis Withdrawal Arbitrage Bot — Main Orchestrator

Polls Polymarket's open tennis H2H markets, detects withdrawals when a
previously-open match disappears before its scheduled start, then buys
the opponent's shares cheap and sells after the 50/50 reset.
"""

import logging
import signal
import threading
import time

import config
from polymarket_client import PolymarketClient, MarketMatch
from withdrawal_monitor import WithdrawalMonitor, Withdrawal

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("bot")

# Suppress noisy HTTP debug logs
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


class Bot:
    def __init__(self):
        self.poly = PolymarketClient()
        self.monitor = WithdrawalMonitor(poly=self.poly)
        self._running = False
        self._sell_threads: list[threading.Thread] = []

    def run(self):
        self._running = True
        signal.signal(signal.SIGINT, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)

        logger.info("Bot started (DRY_RUN=%s, POLL_INTERVAL=%ds, MAX_BET=$%.2f)",
                     config.DRY_RUN, config.POLL_INTERVAL, config.MAX_BET_USDC)

        while self._running:
            try:
                self._tick()
            except Exception:
                logger.exception("Error in poll loop")
            time.sleep(config.POLL_INTERVAL)

        for t in self._sell_threads:
            t.join(timeout=10)
        logger.info("Bot stopped")

    def _tick(self):
        withdrawals = self.monitor.poll()
        for w in withdrawals:
            logger.info("=== VANISH: %s (%s) ===", w.match.event_title if w.match else "?", w.tournament_name)
            self._handle_withdrawal(w)

    def _handle_withdrawal(self, w: Withdrawal):
        if w.match is None:
            logger.warning("Withdrawal in %s has no associated Polymarket match — skipping", w.tournament_name)
            return

        market = MarketMatch.from_tennis_match(w.match)
        logger.info(
            "Trading market: '%s' | buy underdog %s @ %.2f (favorite: %s @ %.2f)",
            market.question,
            market.underdog_name, market.underdog_price,
            market.favorite_name, market.favorite_price,
        )

        result = self.poly.buy_opponent_shares(market)
        if not result:
            return

        logger.info("Buy placed — spawning sell monitor thread")
        t = threading.Thread(
            target=self._sell_after_reset,
            args=(market,),
            daemon=True,
        )
        t.start()
        self._sell_threads.append(t)

    def _sell_after_reset(self, market: MarketMatch):
        """Background thread: wait for 50/50 reset then sell."""
        reset = self.poly.monitor_for_reset(market, timeout=600)
        if reset:
            self.poly.sell_shares(market)
        else:
            logger.warning("No reset detected for '%s' — holding position", market.question)

    def _shutdown(self, signum, frame):
        logger.info("Shutting down...")
        self._running = False


if __name__ == "__main__":
    bot = Bot()
    bot.run()
