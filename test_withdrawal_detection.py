"""
Smoke test: simulate both withdrawal signals and confirm the bot detects
and acts on them.

Two scenarios, each with its own fresh Bot instance:

    Scenario A — PRICE SPIKE (primary detector):
        Take a real snapshot, then on the next poll return the same list
        but with one pre-match event's underdog price jacked up by
        >= PRICE_SPIKE_THRESHOLD. Assert the spike detector fires.

    Scenario B — VANISH (backup detector):
        Take a real snapshot, then on the next poll return the same list
        MINUS one pre-match event. Assert the vanish detector fires.

Both scenarios then feed the resulting Withdrawal through
Bot._handle_withdrawal() in DRY_RUN mode to confirm the full pipeline
(build MarketMatch, place buy) runs without error.
"""
import dataclasses
import logging
from datetime import datetime, timezone
from unittest.mock import patch

import config
from main import Bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

assert config.DRY_RUN, "Refusing to run simulation with DRY_RUN=False"


def pick_victim(known_matches):
    """Return a pre-match event from the snapshot, or None."""
    now = datetime.now(timezone.utc)
    for m in known_matches.values():
        if not m.match_start:
            continue
        try:
            s = m.match_start
            if "T" not in s:
                s = s + "T00:00:00+00:00"
            else:
                s = s.replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
        except ValueError:
            continue
        if dt > now:
            return m
    return None


def run_spike_scenario():
    print("\n" + "=" * 60)
    print("SCENARIO A: price-spike detector")
    print("=" * 60)
    bot = Bot()

    print("\n[A1] initial snapshot")
    bot.monitor.poll()
    print(f"  tracked matches: {len(bot.monitor._known_matches)}")

    victim = pick_victim(bot.monitor._known_matches)
    if victim is None:
        print("  no pre-match events available — skipping spike scenario")
        return

    print(f"\n[A2] simulating price spike on {victim.event_title!r}")
    print(f"  prev: {victim.player_a} @ {victim.price_a} | {victim.player_b} @ {victim.price_b}")

    # Bump the *cheaper* side up by > threshold to mimic walkover news.
    bump = config.PRICE_SPIKE_THRESHOLD + 0.02
    if victim.price_a <= victim.price_b:
        new_a = min(victim.price_a + bump, 0.50)
        new_b = max(victim.price_b - bump, 0.50)
    else:
        new_a = max(victim.price_a - bump, 0.50)
        new_b = min(victim.price_b + bump, 0.50)
    print(f"  new:  {victim.player_a} @ {new_a:.2f} | {victim.player_b} @ {new_b:.2f}")

    real_fetch = bot.poly.fetch_open_tennis_matches

    def fake_fetch():
        matches = real_fetch()
        out = []
        for m in matches:
            if m.condition_id == victim.condition_id:
                out.append(dataclasses.replace(m, price_a=new_a, price_b=new_b))
            else:
                out.append(m)
        return out

    with patch.object(bot.poly, "fetch_open_tennis_matches", side_effect=fake_fetch):
        withdrawals = bot.monitor.poll()

    print(f"\n[A3] poll returned {len(withdrawals)} withdrawal(s)")
    match = next(
        (w for w in withdrawals if w.match and w.match.condition_id == victim.condition_id),
        None,
    )
    if match is None:
        print(f"  FAIL: expected a spike withdrawal for {victim.event_title}")
        return
    print(f"  PASS: spike detector fired on {match.match.event_title}")

    print("\n[A4] feeding through Bot._handle_withdrawal (DRY_RUN)")
    bot._handle_withdrawal(match)


def run_vanish_scenario():
    print("\n" + "=" * 60)
    print("SCENARIO B: vanish detector")
    print("=" * 60)
    bot = Bot()

    print("\n[B1] initial snapshot")
    bot.monitor.poll()
    print(f"  tracked matches: {len(bot.monitor._known_matches)}")

    victim = pick_victim(bot.monitor._known_matches)
    if victim is None:
        print("  no pre-match events available — skipping vanish scenario")
        return

    print(f"\n[B2] simulating walkover (market disappears) on {victim.event_title!r}")

    real_fetch = bot.poly.fetch_open_tennis_matches

    def fake_fetch():
        matches = real_fetch()
        return [m for m in matches if m.condition_id != victim.condition_id]

    with patch.object(bot.poly, "fetch_open_tennis_matches", side_effect=fake_fetch):
        withdrawals = bot.monitor.poll()

    print(f"\n[B3] poll returned {len(withdrawals)} withdrawal(s)")
    match = next(
        (w for w in withdrawals if w.match and w.match.condition_id == victim.condition_id),
        None,
    )
    if match is None:
        print(f"  FAIL: expected a vanish withdrawal for {victim.event_title}")
        return
    print(f"  PASS: vanish detector fired on {match.match.event_title}")

    print("\n[B4] feeding through Bot._handle_withdrawal (DRY_RUN)")
    bot._handle_withdrawal(match)


def main():
    run_spike_scenario()
    run_vanish_scenario()


if __name__ == "__main__":
    main()
