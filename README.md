# Tennis Withdrawal Arbitrage Bot

Exploits a Polymarket pricing inefficiency: when a tennis player withdraws from a match, Polymarket closes the old market and resets the replacement one to ~50/50 instead of voiding. Between the withdrawal and the reset there's a short window where the favorite's shares haven't dropped yet — the bot buys the underdog cheap and sells once the market rebalances.

## How the arbitrage works

1. Player A (favorite, 62c) vs Player B (underdog, 38c) — market open on Polymarket
2. Player A withdraws
3. Polymarket hasn't reflected it yet — Player B shares still at 38c
4. Bot buys Player B shares at 38c
5. Polymarket closes / resets the market to ~50/50
6. Bot sells Player B shares at ~50c → **~12c profit per share**

The arb only works when **the favorite withdraws**. If the underdog withdraws, the favorite was already priced near 1.0 and there's nothing to capture. The bot doesn't try to know who withdrew — it always buys the cheaper (underdog) side of the vanishing market, and the `MAX_BUY_PRICE` guard refuses when prices have already moved past the profitable window.

**Speed is the whole game.** Polymarket's own reset takes minutes to complete, but the pricing edge starts closing the moment the first trader hears the news. The bot's primary signal is therefore a *price spike* on an open market — not market closure. When the favorite's side starts crashing, the underdog's price rises; we detect that rise on the next poll and buy before Polymarket itself has closed the market. A slower vanish detector runs alongside as a backup for the cases where the whole price move fits inside a single poll interval.

## Architecture

```
main.py                  Orchestrator — runs the poll loop, dispatches buy/sell threads
withdrawal_monitor.py    Snapshots Polymarket open matches, detects disappearances
news_monitor.py          Cross-checks detected signals against Sofascore + ESPN
polymarket_client.py     Fetches Gamma tennis events, places CLOB orders
config.py                All settings loaded from .env
test_withdrawal_detection.py  Smoke test that simulates a walkover end-to-end
```

### main.py

- `Bot` class wires `WithdrawalMonitor`, `NewsMonitor`, and `PolymarketClient` together
- `NewsMonitor` is constructed only if `NEWS_CHECK_ENABLED` is true, then passed into `WithdrawalMonitor` as the optional confirmation layer
- Each tick: `monitor.poll()` → for every withdrawal, build a `MarketMatch` directly from the attached `TennisMatch` → `poly.buy_opponent_shares()`
- Spawns a daemon thread per buy that runs `poly.monitor_for_reset()` then `poly.sell_shares()`
- Handles SIGINT/SIGTERM for graceful shutdown and waits up to 10s for pending sell threads

### polymarket_client.py

- `PolymarketClient.fetch_open_tennis_matches()` queries Gamma `/events?tag_slug=tennis&active=true&closed=false` and returns a list of `TennisMatch` objects — one per open head-to-head market
- Each `TennisMatch` carries: event id/slug/title, parsed `tournament` prefix, condition id, both player full names, both token ids, both current prices, and the actual match `startTime`
- Non-H2H sub-markets (O/U, handicap, Set 1 Winner) are filtered — we only keep the market whose `question` matches the event title
- Tournament-winner ("outright") events are skipped — no "X vs Y" structure
- `MarketMatch.from_tennis_match(tm)` labels the cheaper side as `underdog_*` and the pricier side as `favorite_*` — no notion of "who withdrew" because the trade is always on the underdog
- `.buy_opponent_shares()` places a GTC buy order via `py-clob-client` on the underdog token; `.sell_shares()` places the sell at the reset target; `.monitor_for_reset()` polls the order book waiting for the ~50c reset
- Tracks `_traded_markets` by condition id to avoid duplicate trades
- Respects `DRY_RUN` flag — logs but doesn't execute when true

### withdrawal_monitor.py

`WithdrawalMonitor` is **Polymarket-driven**, not tournament-scraping. On each poll it re-fetches `PolymarketClient.fetch_open_tennis_matches()` and runs two detectors against the diff against the previous snapshot. Both emit the same `Withdrawal` dataclass (`tournament_name`, `tour`, and a reference to the `TennisMatch`) and both are deduped by condition id via `_emitted`, so a given market is only acted on once.

**Detector 1 — price spike (primary, fast):** for any condition id present in both snapshots, track the underdog token's price. If it rose by at least `PRICE_SPIKE_THRESHOLD` (default `0.05`) between polls, emit a withdrawal. This is the signal that walkover news has just hit — the favorite's side is crashing, pulling the underdog up toward 0.50 — and it fires while the market is still open, before Gamma closes it.

**Detector 2 — vanish (backup, slow):** any condition id that was in the previous snapshot and is no longer open is a candidate withdrawal, filtered to pre-match events only (using `event.startTime` — the actual match kick-off, not `startDate` which is just when the market was listed). This catches walkovers whose entire price move happened inside a single poll interval, or that happened while the bot was offline.

The first call snapshots the state and returns `[]`. Neither detector needs to know *which* player withdrew — the bet is always on the underdog side of the market.

After either detector fires, `WithdrawalMonitor._news_allows_trade()` consults the optional `NewsMonitor` (see below). In `log` mode (the default) the result is written to the log but trades proceed unchanged — this is the "measure the false-positive rate" phase. In `gate` mode the trade is blocked unless a news source confirms a walkover or retirement. Dedup (`_emitted`) runs *before* the gate, so a blocked cid is not re-checked on every subsequent poll.

### news_monitor.py

`NewsMonitor` provides a cross-source confirmation layer for the signals that `WithdrawalMonitor` produces. It is independent of Polymarket and queries public tennis data directly, so it can act as:

- a **false-positive filter** for the price-spike detector (current role, `NEWS_CHECK_MODE=log`), or
- a **hard gate** that blocks any trade without confirmation (`NEWS_CHECK_MODE=gate`), or
- eventually, a **primary trigger** where news drives trades before any price move on Polymarket (`trigger` mode, not yet wired).

Two sources are tried in order. Both are fetched in bulk once per cache window ("today's scheduled tennis matches") and cached for `NEWS_CACHE_TTL` seconds so `check_match()` is O(1) HTTP on a warm cache:

1. **Sofascore** (`api.sofascore.com/api/v1/sport/tennis/scheduled-events/{date}`) — unofficial JSON, fast, broad coverage (ATP/WTA/Challenger/ITF). Status is read from `event.status.description` / `event.status.type`; values matching `walkover` or `retired` are treated as confirmation.
2. **ESPN** (`site.api.espn.com/.../tennis/atp/scoreboard` + `wta/scoreboard`) — stable fallback. Status read from `competitions[0].status.type.name`/`description`.

Player-name matching is fuzzy on last names only. Polymarket uses `"Stefanos Sakellaridis"`; Sofascore uses `"Sakellaridis S."`; ESPN uses either. `_last_name()` normalizes both formats (plus accented characters, multi-part surnames, and `"de Minaur A."`-style nobility particles) so both shapes collapse to `"sakellaridis"`. A match requires *both* players' last names to appear on the same event — one-sided matches are rejected.

`NewsCheckResult.status` is one of:

| Status | Meaning |
|---|---|
| `walkover` | Source confirms a walkover — the surviving player advances without playing |
| `retired` | Source confirms a mid-match retirement |
| `normal` | Match exists on the source but is scheduled/in-progress/finished normally (= no withdrawal signal) |
| `not_found` | None of the sources listed this match today — common for Challengers and qualies |
| `error` | All sources errored (network, rate limit, schema drift) |

Only `walkover` and `retired` count as confirmation (`result.confirms_withdrawal == True`).

> **Unofficial sources, unstable schemas.** Sofascore and ESPN expose JSON for web clients but do not publish it as a public API. Field names can shift without warning. The `log` mode is specifically designed to make schema drift visible — if every check starts returning `not_found` or `error`, inspect the raw JSON and update `SofascoreSource.classify` / `ESPNSource.classify`. Rate limiting is also a risk; raise `NEWS_CACHE_TTL` if you see HTTP 403 responses in the logs.

> **News latency is not zero.** Sofascore typically reflects walkovers within ~1 minute of the official call; ESPN can be 2-3 minutes. Both are *slower* than the price-spike signal (which fires within one poll interval = ~10s). In `log` mode that just shows up as occasional `not_found` results on freshly-broken walkovers — useful signal in itself. In `gate` mode it means some real walkovers will be skipped because the news source is still catching up. If this becomes the dominant failure mode, a natural next step is to retry the news check during the existing 10-minute sell-monitor window and retroactively log the confirmation.

> **Why Polymarket-driven instead of scraping ATP/WTA entry lists?** The earlier version of the bot scraped ATP/WTA/tennisexplorer tournament pages to snapshot entries per tournament. That approach had three fatal gaps:
> 1. **It missed upcoming tournaments.** ATP Madrid was invisible until the first match was played, even though Polymarket had been listing its markets for days.
> 2. **It missed qualifying rounds.** Main-draw entry pages don't list qualifiers, but Polymarket lists quali matches.
> 3. **It missed Challengers.** The filter dropped them, but Polymarket has Challenger markets.
>
> Driving the snapshot off Polymarket itself guarantees perfect coverage: we only watch matches we can actually trade.

### config.py

All config from environment variables via `.env`:

| Variable | Default | Purpose |
|---|---|---|
| `POLY_API_KEY` | — | Polymarket CLOB API key |
| `POLY_API_SECRET` | — | CLOB API secret |
| `POLY_API_PASSPHRASE` | — | CLOB API passphrase |
| `POLY_PRIVATE_KEY` | — | Ethereum private key (Polygon) for signing orders |
| `POLL_INTERVAL` | 10 | Seconds between Polymarket snapshots |
| `MAX_BET_USDC` | 50 | Max USDC to spend per trade |
| `MAX_BUY_PRICE` | 0.45 | Won't buy if underdog price exceeds this |
| `SELL_TARGET_PRICE` | 0.50 | Target sell price (the 50/50 reset price) |
| `PRICE_SPIKE_THRESHOLD` | 0.05 | Underdog-side price rise between polls that triggers the primary detector |
| `DRY_RUN` | true | Set to `false` to execute real trades |
| `NEWS_CHECK_ENABLED` | true | Master switch for the Sofascore/ESPN confirmation layer |
| `NEWS_CHECK_MODE` | `log` | `log` = observe only, `gate` = require confirmation, `trigger` = reserved |
| `NEWS_CACHE_TTL` | 30 | Seconds to cache each news source's "today's events" response |

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# Fill in your Polymarket API credentials + private key in .env
python main.py
```

### Getting Polymarket credentials

You need a Polymarket CLOB API key. This requires:
1. An Ethereum wallet with USDC on Polygon
2. Register at polymarket.com and enable trading
3. Generate API keys from the CLOB API (see py-clob-client docs)

## Testing

Before running live, verify detection works end-to-end with the simulation:

```bash
python test_withdrawal_detection.py
```

Takes a real snapshot from Polymarket and runs two scenarios against fresh `Bot` instances:
1. **Price-spike**: picks a pre-match event, bumps its underdog price by `PRICE_SPIKE_THRESHOLD + 0.02`, polls again, asserts the spike detector fired and the withdrawal routed through `Bot._handle_withdrawal`
2. **Vanish**: picks a pre-match event, drops it from the next snapshot, polls again, asserts the vanish detector fired

Both scenarios exercise the full trading pipeline in `DRY_RUN` (asserted at startup — the test refuses to run otherwise).

## What still needs to be built

### Promote `news_monitor` from confirmation to primary trigger

`NewsMonitor` currently runs in `log` mode as a passive observer — every signal from the price-spike or vanish detector is cross-checked against Sofascore + ESPN and the result is logged, but the trade decision is unchanged. The natural progression is:

1. **Phase 1 (now):** `NEWS_CHECK_MODE=log`. Collect a week of logs. Measure how often each fired signal is confirmed by a news source vs. returns `not_found` vs. `normal`. That gives us the real false-positive rate of the spike detector.
2. **Phase 2:** `NEWS_CHECK_MODE=gate`. Block unconfirmed trades. Safer, but loses trades where news lags the order book.
3. **Phase 3:** add a `trigger` mode where `NewsMonitor` is polled on its own cadence and emits withdrawals directly — no price spike required. The spike+vanish detectors stay on as backups / ground-truth reconciliation. This is the "be faster than the market" mode the bot was originally scoped for.
4. **Phase 4:** replace/augment Sofascore and ESPN with a true real-time feed — X/Twitter firehose filtered for known tennis journalists, or a paid live-data API — so the bot fires before public websites have even caught up.

Sources currently used by `NewsMonitor`:
- **Sofascore** — ~1 min latency, broad coverage, unofficial endpoint (rate-limit sensitive)
- **ESPN** — ~1-2 min latency, stable schema, narrower coverage

Sources worth adding later:
- **X/Twitter stream** for known tennis journalists (@josemorgado, @BenRothenberg, @TennisChannel, @ATPTour, @WTA)
- **ATP/WTA official news feeds** — press releases sometimes beat entry-list updates
- **Flashscore / Sofascore live stream** for retirement events during matches already in progress

### Position tracking

`polymarket_client.py` currently estimates position size from config rather than tracking actual fills. Should:
- Record actual fill size and price from buy order response
- Use real position size when selling
- Track P&L per trade

### Spurious-withdrawal filtering

A market can disappear from Gamma for reasons other than a walkover — manual takedown, temporary de-listing, tag changes. Worth adding:
- Confirm the event is actually `closed`/`archived` via a direct `/events/{id}` check before acting
- Require the vanish to persist across 2 consecutive polls

## Dependencies

- `httpx` — HTTP client for Polymarket Gamma API calls and Sofascore/ESPN lookups
- `py-clob-client` — Official Polymarket CLOB order client
- `python-dotenv` — .env file loading
- `websockets` — (reserved for future real-time feeds)

`curl_cffi` and `beautifulsoup4` are still in `requirements.txt` from the previous tennisexplorer-scraping approach. They're currently unused but kept in case Phase 2 brings back browser-impersonating HTTP.

## Risk notes

- `DRY_RUN=true` by default — bot will only log, not trade
- `MAX_BUY_PRICE=0.45` prevents buying if the market has already moved past the profitable window
- `MAX_BET_USDC=50` caps exposure per trade
- The bot tracks traded markets (`_traded_markets` + `_emitted`) to avoid double-buying the same event
- The bot never guesses which player withdrew — it always buys the cheaper side. If the underdog actually withdrew (no arb), the favorite is already near 1.0 and the price guard refuses any trade on it. So the always-underdog rule fails safe in the inverted case

## Running the bot continuously

See the "Running the bot 24/7" section below.
