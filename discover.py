#!/usr/bin/env python3
"""
discover.py -- turn a browser devtools capture into config.json

Accepts either:
  1. A HAR file exported from the Network tab while the flyer page loads, or
  2. A plain text file with one URL per line (copied from the Network tab).

Usage:
    python discover.py capture.har
    python discover.py urls.txt

It looks for Flipp "flyerkit" API calls (the API the embedded flyer viewer
uses), pulls out the four values the scraper needs, and writes config.json.
The access token is not a secret -- it ships to every visitor's browser in
the public flyer page -- so it is safe to commit.
"""
import json
import pathlib
import re
import sys
from urllib.parse import urlparse, parse_qs

ROOT = pathlib.Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"

# Flipp serves the same endpoint shapes under two path prefixes:
#   .../flyerkit/v4.0/publications/<merchant>   (classic flyerkit)
#   .../hosted/publications/<merchant>          (hosted viewer, seen live 2026-07)
FLYERKIT_RE = re.compile(
    r"(https://[^/]+/(?:flyerkit/v[\d.]+|hosted))/publications?/([^/?#]+)", re.I)

DEFAULTS = {
    "mode": "flyerkit",
    "api_base": "",
    "merchant": "",
    "access_token": "",
    "store_code": "7063802",
    "locale": "en-ca",
    "store_name": "Thousand Islands Pharmacy",
    "brand_sub": "This Week's Specials",
    "flyer_public_url": "https://www.guardian-ida-remedysrx.ca/en/flyer?storeCode=7063802&retailerId=guardian",
    "max_items": 12,
    "require_image": True,
    "require_price": True,
    # Road-sign guardrails: legitimate flyer deals that don't belong four
    # feet tall on King St. Keywords match name+description; categories
    # match Flipp's merchant tags and Google taxonomy labels.
    "exclude_keywords": [
        "diarrhea", "laxative", "hemorrhoid", "constipation",
        "lice", "wart", "condom", "pregnancy test", "feminine", "yeast",
        "enema", "stool", "suppositor", "tampon",
        "nicotine", "zonnic", "smoking",
    ],
    "exclude_categories": [
        "feminine", "sexual", "fertility", "tobacco", "smoking cessation",
    ],
    # Merchandising tiers: the week's deal in each slot always makes the
    # sign; the rest alternates house brand with one pick per variety group.
    "priority_slots": [
        {"label": "incontinence", "max": 2, "keywords": [
            "incontinence", "tena", "poise", "depend", "always discreet",
            "bladder"]},
        {"label": "vitamins", "max": 2, "keywords": [
            "vitamin", "multivitamin", "jamieson", "webber", "ddrops",
            "omega", "calcium", "magnesium", "b12", "probiotic",
            "supplement"]},
        {"label": "meal replacement", "max": 2, "keywords": [
            "boost", "ensure", "glucerna", "meal replacement",
            "nutritional shake"]},
    ],
    "brand_fill_keywords": ["option+"],
    "variety_groups": [
        ["dental", "oral care", "toothpaste", "toothbrush", "mouthwash",
         "floss", "denture"],
        ["hair care", "shampoo", "conditioner", "hair colour", "styling"],
        ["personal care", "deodorant", "antiperspirant", "body wash",
         "soap", "bath", "shave", "razor"],
        ["skin care", "lotion", "moisturizer", "sunscreen", "lip care"],
        ["cosmetics", "nail", "mascara", "lipstick"],
    ],
    "preset_slides": [
        {"title": "Free Local Delivery", "sub": "Ask us for details",
         "enabled": True},
        {"title": "Seniors Day Every Thursday", "sub": "Ask us in store",
         "enabled": False},
        {"title": "Feeling better starts here®",
         "sub": "Your local pharmacy®", "enabled": True},
    ],
    "fallback_lines": [
        "Weekly specials in store now",
        "Ask us about this week's flyer deals",
    ],
    "download_images": True,
    "observed_urls": [],
}


def load_urls(path: pathlib.Path):
    """Return every request URL found in the input file (HAR or plain list)."""
    text = path.read_text(encoding="utf-8", errors="replace")
    urls = []
    try:
        har = json.loads(text)
        for entry in har.get("log", {}).get("entries", []):
            url = entry.get("request", {}).get("url")
            if url:
                urls.append(url)
    except json.JSONDecodeError:
        for line in text.splitlines():
            line = line.strip().strip('"').strip("'")
            if line.startswith("http"):
                urls.append(line)
    return urls


def is_flippish(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(k in host for k in ("flipp", "wishabi"))


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    src = pathlib.Path(sys.argv[1])
    if not src.exists():
        sys.exit(f"File not found: {src}")

    urls = load_urls(src)
    if not urls:
        sys.exit("No URLs found in that file. Export a HAR from the Network tab, "
                 "or paste request URLs one per line into a text file.")

    flipp_urls = [u for u in urls if is_flippish(u)]
    cfg = dict(DEFAULTS)
    # Keep a de-duplicated sample of observed Flipp traffic for debugging.
    seen = []
    for u in flipp_urls:
        if u not in seen:
            seen.append(u)
    cfg["observed_urls"] = seen[:15]

    # Prefer the plural /publications/<merchant> listing over the singular
    # /publication/<id>/products call -- the latter's second segment is a
    # publication id, not the merchant slug.
    hits = []
    for u in flipp_urls:
        m = FLYERKIT_RE.search(u)
        if m:
            hits.append((m.group(1), m.group(2), u))
    hit = next((h for h in hits if "/publications/" in h[2]), hits[0] if hits else None)

    token = store = locale = None
    for u in flipp_urls:
        q = parse_qs(urlparse(u).query)
        token = token or (q.get("access_token") or [None])[0]
        store = store or (q.get("store_code") or q.get("store") or [None])[0]
        locale = locale or (q.get("locale") or [None])[0]

    if hit:
        cfg["api_base"], cfg["merchant"], _ = hit
        if token:
            cfg["access_token"] = token
        if store:
            cfg["store_code"] = store
        if locale:
            cfg["locale"] = locale
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        print("Found the flyerkit API. Wrote config.json:")
        print(f"  api_base:     {cfg['api_base']}")
        print(f"  merchant:     {cfg['merchant']}")
        print(f"  access_token: {'yes' if cfg['access_token'] else 'MISSING - see below'}")
        print(f"  store_code:   {cfg['store_code']}")
        print(f"  locale:       {cfg['locale']}")
        if not cfg["access_token"]:
            print("\nNo access_token in the captured URLs. Reload the flyer page with the "
                  "Network tab open, filter for 'flyerkit', and re-export -- the token "
                  "rides on those requests as a query parameter.")
        else:
            print("\nNext: python scrape.py && python generate.py, then open docs/index.html")
    else:
        cfg["mode"] = "unknown"
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        print("No flyerkit-style URLs in this capture, but I did see Flipp traffic."
              if flipp_urls else
              "No Flipp traffic in this capture at all.")
        print("Wrote config.json with mode='unknown' and the observed URLs.")
        print("Paste the observed_urls list from config.json back into the chat and "
              "the scraper can be adapted to whatever API the viewer is using.")


if __name__ == "__main__":
    main()
