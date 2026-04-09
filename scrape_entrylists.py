"""
Scrape @EntryLists for withdrawals / cancelled matchups.

Pulls the recent timeline via a local Nitter instance, filters tweets
that mention a withdrawal / walkover / retirement, and writes the
results to data/EntryLists_withdrawals.json.
"""

import json
import pathlib
import re

from ntscraper import Nitter
import ntscraper.nitter as ntr

# --- monkey-patch avatar bug guard ----------------------------------------
_orig_get_user = ntr.Nitter._get_user
def _safe_get_user(self, tweet, is_encrypted):
    try:
        return _orig_get_user(self, tweet, is_encrypted)
    except IndexError:
        uname = tweet.find("a", class_="username")
        fname = tweet.find("a", class_="fullname")
        return {
            "id": None,
            "username": uname.text.lstrip("@") if uname else "unknown",
            "fullname": fname.text if fname else "unknown",
            "avatar_url": None,
        }
ntr.Nitter._get_user = _safe_get_user
# --------------------------------------------------------------------------

USER = "EntryLists"
NITTER_INSTANCE = "http://localhost:8080"

# Word-boundary matched against lowercased text. Short tokens like "wd"
# and "ret" need the boundaries so they don't match "forward"/"retreat".
WALKOVER_PATTERNS = [
    r"\bwalkover\b", r"\bw/o\b", r"\bwd\b",
    r"\bwithdraw(?:s|n|al|als)?\b", r"\bwithdrew\b",
    r"\bpull(?:s|ed)?\s+out\b", r"\bout\s+of\b", r"\bscratched\b",
]
RETIRED_PATTERNS = [r"\bretire(?:s|d|ment)?\b", r"\bret\b"]

WALKOVER_RE = re.compile("|".join(WALKOVER_PATTERNS), re.IGNORECASE)
RETIRED_RE = re.compile("|".join(RETIRED_PATTERNS), re.IGNORECASE)


def classify(text: str) -> str | None:
    if not text:
        return None
    if WALKOVER_RE.search(text):
        return "walkover"
    if RETIRED_RE.search(text):
        return "retired"
    return None


def main():
    print(f"Scraping timeline for @{USER}")
    scraper = Nitter(log_level=1, skip_instance_check=True,
                     instance=NITTER_INSTANCE)

    timeline = scraper.get_tweets(USER, "user", -1)["tweets"]
    print(f"  - {len(timeline)} tweets fetched")

    withdrawals = []
    for tw in timeline:
        text = tw.get("text", "")
        kind = classify(text)
        if kind is None:
            continue
        withdrawals.append({
            "kind": kind,
            "date": tw.get("date"),
            "link": tw.get("link"),
            "text": " ".join(text.split()),
        })

    print(f"  - {len(withdrawals)} withdrawal/retirement tweets matched")

    out_dir = pathlib.Path("data")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{USER}_withdrawals.json"
    out_path.write_text(
        json.dumps(withdrawals, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"  - written to {out_path}")


if __name__ == "__main__":
    main()
