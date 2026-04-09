import os
from dotenv import load_dotenv

load_dotenv()

# Polymarket
POLY_API_KEY = os.getenv("POLY_API_KEY", "")
POLY_API_SECRET = os.getenv("POLY_API_SECRET", "")
POLY_API_PASSPHRASE = os.getenv("POLY_API_PASSPHRASE", "")
POLY_PRIVATE_KEY = os.getenv("POLY_PRIVATE_KEY", "")

# Trading
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "10"))
MAX_BET_USDC = float(os.getenv("MAX_BET_USDC", "50"))
MAX_BUY_PRICE = float(os.getenv("MAX_BUY_PRICE", "0.45"))
SELL_TARGET_PRICE = float(os.getenv("SELL_TARGET_PRICE", "0.50"))
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

# Detection
# If the underdog's price jumps by at least this much between two polls
# (the favorite's side crashing = walkover news just hit), treat it as a
# withdrawal signal — this fires BEFORE Polymarket closes the market,
# which is the whole point of running our own detector.
PRICE_SPIKE_THRESHOLD = float(os.getenv("PRICE_SPIKE_THRESHOLD", "0.05"))

# News confirmation (Sofascore / ESPN)
# Mode controls how the news layer affects trading:
#   'log'     — observe only; every spike/vanish signal is looked up and
#               the news status is logged, but trades are not affected.
#               Use this first to measure false-positive rates.
#   'gate'    — require a confirming walkover/retirement from a news source
#               before trading. Safer, but misses trades where news lags.
#   'trigger' — reserved for a future mode where news drives trades
#               directly (not yet wired in withdrawal_monitor).
NEWS_CHECK_ENABLED = os.getenv("NEWS_CHECK_ENABLED", "true").lower() == "true"
NEWS_CHECK_MODE = os.getenv("NEWS_CHECK_MODE", "log").lower()
NEWS_CACHE_TTL = float(os.getenv("NEWS_CACHE_TTL", "30"))

# Polymarket API
POLY_CLOB_URL = "https://clob.polymarket.com"
POLY_GAMMA_URL = "https://gamma-api.polymarket.com"
