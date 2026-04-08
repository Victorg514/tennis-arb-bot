import os
from dotenv import load_dotenv

load_dotenv()

# Polymarket
POLY_API_KEY = os.getenv("POLY_API_KEY", "")
POLY_API_SECRET = os.getenv("POLY_API_SECRET", "")
POLY_API_PASSPHRASE = os.getenv("POLY_API_PASSPHRASE", "")
POLY_PRIVATE_KEY = os.getenv("POLY_PRIVATE_KEY", "")

# Trading
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "30"))
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

# Polymarket API
POLY_CLOB_URL = "https://clob.polymarket.com"
POLY_GAMMA_URL = "https://gamma-api.polymarket.com"
