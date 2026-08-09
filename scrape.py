#!/usr/bin/env python3
"""
scrape.py -- pull this week's flyer items for one store from Flipp's flyerkit API.

Reads config.json (produced by discover.py), finds the publication that is
valid *today*, fetches its products, normalizes and ranks them, and writes:

    data/items.json       -> input for generate.py
    data/raw_sample.json  -> first two raw products + field names, for debugging
    data/last_run.json    -> status breadcrumb

Exit codes:
    0  success
    1  config / network / parse error
    2  no publication valid today (between flyers) -> workflow serves fallback

The field names Flipp uses vary slightly between merchants, so every lookup
tries a list of candidate keys. If parsing misses, send data/raw_sample.json
back to Claude and the key lists below get a one-line fix.
"""
import datetime
import json
import pathlib
import re
import sys

import requests

ROOT = pathlib.Path(__file__).resolve().parent
DATA = ROOT / "data"
CONFIG_PATH = ROOT / "config.json"

NAME_KEYS = ["name", "title"]
IMAGE_KEYS = ["cutout_image_url", "clean_image_url", "clipping_image_url",
              "large_image_url", "image_url", "x_large_image_url"]
PRICE_TEXT_KEYS = ["price_text", "sale_price_text"]
# price_text last: Guardian's hosted API carries the number only as text
# ("12.00"), so scoring falls back to parsing it; non-numeric text scores 0.
PRICE_NUM_KEYS = ["current_price", "price", "sale_price", "price_text"]
VALID_FROM_KEYS = ["valid_from", "available_from", "start_date"]
VALID_TO_KEYS = ["valid_to", "available_to", "end_date"]

# "40% OFF*" deals ship with an empty price field; the story IS the price.
PERCENT_OFF_RE = re.compile(r"(\d{1,2})\s*%\s*off", re.I)
DISCLAIMER_RE = re.compile(r"\*+\s*(our regular retail price)?", re.I)


def die(msg, code=1):
    print(f"ERROR: {msg}", file=sys.stderr)
    _write_status("error", msg)
    sys.exit(code)


def _write_status(status, detail=""):
    DATA.mkdir(exist_ok=True)
    (DATA / "last_run.json").write_text(json.dumps({
        "status": status,
        "detail": detail,
        "at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    }, indent=2) + "\n", encoding="utf-8")


def load_config():
    if not CONFIG_PATH.exists():
        die("config.json missing. Run: python discover.py <your capture file>")
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if cfg.get("mode") != "flyerkit":
        die("config.json mode is not 'flyerkit'. Send the observed_urls list from "
            "config.json back to Claude so the scraper can be adapted.")
    for key in ("api_base", "merchant", "access_token"):
        if not cfg.get(key):
            die(f"config.json is missing '{key}'. Re-run discover.py on a fresh capture.")
    return cfg


def get_json(url, params):
    resp = requests.get(url, params=params, timeout=30,
                        headers={"User-Agent": "Mozilla/5.0 (pharmacy sign feed)"})
    resp.raise_for_status()
    return resp.json()


def pick(record, keys):
    for key in keys:
        value = record.get(key)
        if value not in (None, "", 0, "0"):
            return value
    return None


def parse_date(value):
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def current_publication(publications, today):
    """Publication valid today; if several overlap, the most recently started."""
    live = []
    for pub in publications:
        start = parse_date(pick(pub, VALID_FROM_KEYS))
        end = parse_date(pick(pub, VALID_TO_KEYS))
        if start and end and start <= today <= end:
            live.append((start, pub))
    if not live:
        return None
    live.sort(key=lambda pair: pair[0], reverse=True)
    return live[0][1]


def price_parts(item):
    """Big price core plus a small qualifier line ('2 cans for').

    The pre text changes the meaning of the number, so it is kept (rendered
    small above the price). The post text ('ea.', 'selected types and
    sizes') is unit/legalese noise at sign scale and is dropped.
    """
    text = pick(item, PRICE_TEXT_KEYS)
    if text:
        core = str(text).strip()
    else:
        number = pick(item, PRICE_NUM_KEYS)
        if number is None:
            return None, ""
        try:
            core = f"${float(number):.2f}"
        except (TypeError, ValueError):
            core = str(number).strip()
    if core and core[0].isdigit() and "/" not in core:
        core = "$" + core
    pre = str(item.get("pre_price_text") or "").strip()
    return core, pre


def discount_score(item):
    try:
        current = float(pick(item, PRICE_NUM_KEYS) or 0)
        original = float(item.get("original_price") or 0)
        if original > current > 0:
            return (original - current) / original
    except (TypeError, ValueError):
        pass
    percent = PERCENT_OFF_RE.search(str(item.get("sale_story") or ""))
    if percent:
        return int(percent.group(1)) / 100.0
    return 0.0


def category_names(item):
    """Every category label Flipp attaches: merchant tags + Google taxonomy."""
    names = [str(c) for c in (item.get("categories") or [])]
    levels = item.get("item_categories") or {}
    if isinstance(levels, dict):
        for level in levels.values():
            if isinstance(level, dict) and level.get("category_name"):
                names.append(str(level["category_name"]))
    return names


def keep(item, cfg):
    name = pick(item, NAME_KEYS)
    if not name:
        return None
    display = str(name).strip()
    # PDF-derived flyers split brand and product across `name` and
    # `description` ("Option+" / "Diarrhea Relief Caplets") -- join them so
    # the slide says what the product actually is. An em dash, not a space:
    # multi-brand combos ("3M, BENADRYL or MAGIC BAG" + "NEXCARE bandages,
    # ...") would otherwise fuse into one all-caps run that generate.py
    # reads as a single brand line.
    desc = str(item.get("description") or "").strip()
    if desc and desc.lower() not in display.lower():
        display = f"{display} — {desc}"
    display = " ".join(display.split())  # descriptions carry literal newlines
    # Combo deals whose third option is just the brand leave a connector
    # dangling before the qualifier ("Itch Relief or Selected Types and
    # Sizes") -- "or Selected..." is never a real alternative, drop it.
    display = re.sub(r"\s(?:or|and|&)(?=\s(?:on\s)?selected\s)", "",
                     display, flags=re.I)
    story = " ".join(str(item.get("sale_story") or "").split())
    # Some descriptions repeat the sale story ("... Caplets Save up to $8");
    # the story gets its own line on the slide, so trim the duplicate.
    if story and display.lower().endswith(story.lower()):
        display = display[:-len(story)].rstrip(" -–—,")
    lowered = display.lower()
    # An exclude entry may be a plain substring, or a LIST of substrings
    # that must ALL match (["cerave", "hair"] kills CeraVe hair-care combo
    # deals while CeraVe facial products stay advertisable).
    for bad in cfg.get("exclude_keywords", []):
        if isinstance(bad, list):
            if bad and all(str(b).lower() in lowered for b in bad):
                return None
        elif str(bad).lower() in lowered:
            return None
    cats = " | ".join(category_names(item)).lower()
    if any(bad.lower() in cats for bad in cfg.get("exclude_categories", [])):
        return None
    price, qualifier = price_parts(item)
    if not price:
        percent = PERCENT_OFF_RE.search(story)
        if percent:
            price = f"{percent.group(1)}% OFF"
            story = PERCENT_OFF_RE.sub("", story, count=1)
            story = DISCLAIMER_RE.sub("", story).strip(" ,-–—")
    image = pick(item, IMAGE_KEYS)
    if cfg.get("require_price", True) and not price:
        return None
    if cfg.get("require_image", True) and not image:
        return None
    return {
        "name": display,
        "price": price or "",
        "qualifier": qualifier,
        "image": image or "",
        "story": story,
        "_score": discount_score(item),
        "_match": f"{lowered} | {cats}",
    }


def matches(entry, terms):
    return any(str(t).lower() in entry["_match"] for t in terms)


def select_items(normalized, cfg):
    """Merchandising tiers, all lists score-sorted before we get here.

    1. priority_slots -- the week's incontinence / vitamin / meal-replacement
       deal always makes the sign (a slot with no match this week just skips).
    2. Remainder alternates house-brand items (brand_fill_keywords) with one
       pick per variety_groups bucket in rotation, so no category floods the
       sign. Backfill by discount if the pools run dry.
    """
    max_items = int(cfg.get("max_items", 12))
    chosen, used = [], set()

    def add(entry):
        if id(entry) not in used and len(chosen) < max_items:
            chosen.append(entry)
            used.add(id(entry))

    for slot in cfg.get("priority_slots", []):
        keywords = slot.get("keywords", [])
        picks = [n for n in normalized if matches(n, keywords)]
        # Keyword order is preference order (national brand first), then
        # discount depth -- "40% off Jamieson" beats the house-brand vitamin.
        def rank(entry):
            return next(i for i, kw in enumerate(keywords)
                        if str(kw).lower() in entry["_match"])
        picks.sort(key=lambda n: (rank(n), -n["_score"]))
        for entry in picks[:int(slot.get("max", 2))]:
            add(entry)
        if picks:
            print(f"  slot {slot.get('label', '?')}: "
                  + "; ".join(p["name"][:50] for p in picks[:int(slot.get('max', 2))]))
        else:
            print(f"  slot {slot.get('label', '?')}: nothing on this week")

    fill_kw = cfg.get("brand_fill_keywords", [])
    brand = [n for n in normalized if id(n) not in used and matches(n, fill_kw)]
    # House-brand items ride ONLY the brand track: their deep house discounts
    # would otherwise outscore the national brands inside every variety
    # bucket and the sign fills up with one label. brand_fill_max caps the
    # track so growing max_items grows national-brand variety, not house.
    groups = [[n for n in normalized if id(n) not in used
               and not matches(n, fill_kw) and matches(n, g)]
              for g in cfg.get("variety_groups", [])]
    max_brand = int(cfg.get("brand_fill_max", max_items))
    brand_picks = 0
    group_index, take_brand = 0, True
    while len(chosen) < max_items and (brand or any(groups)):
        pool = None
        if take_brand and brand and brand_picks < max_brand:
            pool = brand
        elif groups:
            for _ in range(len(groups)):
                candidate = groups[group_index % len(groups)]
                group_index += 1
                if candidate:
                    pool = candidate
                    break
        if pool is None:
            pool = (brand if brand_picks < max_brand else None) \
                or next((g for g in groups if g), None)
        if pool is None:
            break
        if pool is brand:
            brand_picks += 1
        entry = pool.pop(0)
        add(entry)
        brand = [n for n in brand if id(n) not in used]
        groups = [[n for n in g if id(n) not in used] for g in groups]
        take_brand = not take_brand

    for entry in normalized:  # backfill by discount depth
        add(entry)
    return chosen


def main():
    cfg = load_config()
    today = datetime.date.today()
    base = cfg["api_base"].rstrip("/")
    auth = {"access_token": cfg["access_token"], "locale": cfg.get("locale", "en-ca")}

    try:
        publications = get_json(
            f"{base}/publications/{cfg['merchant']}",
            {**auth, "store_code": cfg.get("store_code", "")},
        )
    except requests.RequestException as exc:
        die(f"Could not fetch publications list: {exc}")

    if isinstance(publications, dict):  # some responses wrap the list
        publications = (publications.get("publications")
                        or publications.get("data") or [])
    if not isinstance(publications, list) or not publications:
        die("Publications response was empty or an unexpected shape. "
            "Send data/raw_sample.json to Claude.", 1)

    pub = current_publication(publications, today)
    if pub is None:
        _write_status("no_current_publication",
                      f"{len(publications)} publications, none valid on {today}")
        print(f"No publication valid today ({today}); sign should fall back.")
        sys.exit(2)

    pub_id = pub.get("id") or pub.get("publication_id")
    try:
        products = get_json(
            f"{base}/publication/{pub_id}/products",
            {**auth, "display_type": "all"},
        )
    except requests.RequestException as exc:
        die(f"Could not fetch products for publication {pub_id}: {exc}")

    if isinstance(products, dict):
        products = products.get("products") or products.get("data") or []

    DATA.mkdir(exist_ok=True)
    (DATA / "raw_sample.json").write_text(json.dumps({
        "publication_keys": sorted(pub.keys()),
        "product_keys": sorted(products[0].keys()) if products else [],
        "first_two_products": products[:2],
    }, indent=2, default=str) + "\n", encoding="utf-8")

    normalized = [n for n in (keep(p, cfg) for p in products) if n]
    if not normalized:
        die("Publication found but zero items survived filtering. "
            "Send data/raw_sample.json to Claude -- likely a field-name mismatch.", 1)

    normalized.sort(key=lambda item: item["_score"], reverse=True)
    items = [{k: v for k, v in item.items() if not k.startswith("_")}
             for item in select_items(normalized, cfg)]

    payload = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "publication": {
            "id": pub_id,
            "name": pub.get("name") or pub.get("title") or "",
            "valid_from": str(pick(pub, VALID_FROM_KEYS) or "")[:10],
            "valid_to": str(pick(pub, VALID_TO_KEYS) or "")[:10],
        },
        "items": items,
    }
    (DATA / "items.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    _write_status("ok", f"{len(items)} items from publication {pub_id}")
    print(f"Wrote {len(items)} items (of {len(products)} raw) "
          f"from flyer valid {payload['publication']['valid_from']} "
          f"to {payload['publication']['valid_to']}.")


if __name__ == "__main__":
    main()
