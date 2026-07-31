#!/usr/bin/env python3
"""
generate.py -- build the two things ViPlex consumes, from data/items.json:

    docs/sign-feed.xml   -> RSS 2.0 ticker (ViPlex RSS widget)
    docs/index.html      -> 336x144 rotating deal slides (ViPlex web page widget)
    docs/img/*           -> product cutout images, self-hosted so the sign
                            never depends on Flipp's CDN staying hotlinkable

Modes:
    python generate.py               normal build from data/items.json
    python generate.py --fallback    evergreen branding only -- used when the
                                     scrape fails so the sign NEVER shows
                                     stale prices

Test helpers:
    --items PATH      use a fixture instead of data/items.json
    --out PATH        write somewhere other than docs/
    --no-download     keep remote image URLs (offline testing)
"""
import argparse
import datetime
import json
import pathlib
import shutil
import sys
from xml.sax.saxutils import escape

ROOT = pathlib.Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"

# --- LED sign design constants -------------------------------------------
# The canvas is a 336x144 physical LED matrix read from a moving car, in an
# old Android webview. That dictates everything: pure black background
# (unlit LEDs), heavy system sans only (no webfonts in the player), two
# sizes of information per slide (product name, price), and one signature
# element -- the price, set huge in retail amber, which reads at distance
# and is the one thing a driver needs.
W, H = 336, 144
# Palette per the Guardian branding summary (GDN_Branding_Summary v3.0).
# Deal slides stay on black (unlit LEDs, product photos pop); brand and
# preset slides flip to white-on-Guardian-Green, the brand's signature look.
# Guardian Green is too dark to carry small TEXT on black, hence backgrounds.
BG = "#000000"
FG = "#FFFFFF"
GREEN = "#00643C"    # Guardian Green, PMS 3425 -- brand/preset backgrounds
RED = "#EE3124"      # Guardian/I.D.A. Red, PMS 3556 -- prices, as in the flyer
LIGHT = "#F7F6F5"    # Light Grey -- secondary text
SLIDE_SECONDS = 7
RELOAD_SECONDS = 21600  # webview re-pulls the page every 6 h


def load_config():
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return {}


def rss(channel_title, lines, link):
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    items = "\n".join(
        f"    <item><title>{escape(line)}</title></item>" for line in lines
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>{escape(channel_title)}</title>
    <link>{escape(link)}</link>
    <description>{escape(channel_title)}</description>
    <lastBuildDate>{now}</lastBuildDate>
{items}
  </channel>
</rss>
"""


def download_images(items, out_dir):
    """Self-host cutouts under docs/img/. On any failure keep the remote URL."""
    import requests  # imported here so --no-download works without network
    img_dir = out_dir / "img"
    if img_dir.exists():
        shutil.rmtree(img_dir)
    img_dir.mkdir(parents=True)
    (img_dir / ".gitkeep").write_text("", encoding="utf-8")
    for index, item in enumerate(items):
        url = item.get("image")
        if not url:
            continue
        suffix = ".png" if ".png" in url.lower() else ".jpg"
        local = img_dir / f"{index:02d}{suffix}"
        try:
            resp = requests.get(url, timeout=15,
                                headers={"User-Agent": "Mozilla/5.0 (pharmacy sign feed)"})
            resp.raise_for_status()
            local.write_bytes(resp.content)
            item["image"] = f"img/{local.name}"
        except Exception as exc:  # noqa: BLE001 -- any failure => hotlink instead
            print(f"  image {index:02d} kept remote ({exc})", file=sys.stderr)


def slide_html(item):
    name = escape(item["name"])
    price = escape(item.get("price", ""))
    qual = escape(item.get("qualifier", ""))
    story = escape(item.get("story", ""))
    img = escape(item.get("image", ""), {'"': "&quot;"})
    picture = (f'<div class="pic"><img src="{img}" alt=""></div>' if img
               else '<div class="pic"></div>')
    qual_html = f'<div class="qual">{qual}</div>' if qual else ""
    story_html = f'<div class="story">{story}</div>' if story else ""
    # 7 chars of 44px bold is all the text column fits ("$10.00" is 6).
    price_class = "price" if len(item.get("price", "")) <= 7 else "price long"
    return f"""    <div class="slide">
      {picture}
      <div class="txt">
        <div class="name">{name}</div>
        {qual_html}
        <div class="{price_class}">{price}</div>
        {story_html}
      </div>
    </div>"""


def preset_html(preset):
    title = escape(str(preset.get("title", "")))
    sub = escape(str(preset.get("sub", "")))
    sub_html = f'<div class="psub">{sub}</div>' if sub else ""
    return f"""    <div class="slide flat">
      <div class="mid"><div class="cell">
        <div class="ptitle">{title}</div>
        {sub_html}
      </div></div>
    </div>"""


def weave(deals, presets):
    """Spread preset slides evenly through the deal rotation."""
    if not presets:
        return deals
    if not deals:
        return presets
    step = max(1, round(len(deals) / (len(presets) + 1)))
    out, index = [], 0
    for position, slide in enumerate(deals, start=1):
        out.append(slide)
        if index < len(presets) and position == step * (index + 1):
            out.append(presets[index])
            index += 1
    out.extend(presets[index:])
    return out


def page(cfg, items, dates_line, presets):
    store = escape(cfg.get("store_name", "Weekly Specials"))
    sub = escape(cfg.get("brand_sub", "This Week's Specials"))
    brand = f"""    <div class="slide brand">
      <div class="store">{store}</div>
      <div class="sub">{sub}</div>
      <div class="dates">{escape(dates_line)}</div>
    </div>"""
    slides = "\n".join([brand] + weave([slide_html(item) for item in items],
                                       [preset_html(p) for p in presets]))
    # ES5 only, no external assets: the ViPlex player webview is old and may
    # be firewalled to this one page.
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="{RELOAD_SECONDS}">
<title>{store}</title>
<style>
  html, body {{ margin:0; padding:0; background:{BG}; overflow:hidden; }}
  body {{ width:{W}px; height:{H}px; position:relative;
         font-family:'PT Sans', Arial, Helvetica, sans-serif; color:{FG}; }}
  .slide {{ position:absolute; top:0; left:0; width:{W}px; height:{H}px;
            display:none; }}
  .slide.on {{ display:block; }}
  .pic {{ position:absolute; top:6px; left:6px; width:130px; height:132px; }}
  .pic img {{ width:100%; height:100%; object-fit:contain; }}
  .txt {{ position:absolute; top:0; left:144px; width:{W - 150}px; height:{H}px; }}
  .name {{ margin-top:10px; font-size:17px; line-height:20px; font-weight:bold;
           max-height:40px; overflow:hidden; }}
  .qual {{ margin-top:4px; font-size:14px; line-height:16px; font-weight:bold;
           color:{FG}; }}
  .price {{ margin-top:4px; font-size:44px; line-height:46px; font-weight:800;
            color:{RED}; white-space:nowrap; }}
  .price.long {{ font-size:30px; line-height:34px; }}
  .story {{ margin-top:2px; font-size:13px; color:{LIGHT}; white-space:nowrap;
            overflow:hidden; }}
  .brand {{ text-align:center; background:{GREEN}; }}
  .store {{ margin-top:14px; font-size:22px; line-height:25px; font-weight:800; }}
  .sub {{ margin-top:6px; font-size:18px; color:{FG}; font-weight:bold; }}
  .dates {{ margin-top:8px; font-size:12px; color:{LIGHT}; }}
  .flat {{ background:{GREEN}; }}
  .mid {{ display:table; width:{W}px; height:{H}px; }}
  .cell {{ display:table-cell; vertical-align:middle; text-align:center; }}
  .ptitle {{ font-size:24px; line-height:28px; font-weight:800;
             padding:0 12px; }}
  .psub {{ margin-top:6px; font-size:15px; color:{LIGHT}; }}
</style>
</head>
<body>
{slides}
<script>
  var slides = document.getElementsByClassName('slide');
  var index = 0;
  if (slides.length) slides[0].className += ' on';
  if (slides.length > 1) {{
    setInterval(function () {{
      slides[index].className = slides[index].className.replace(' on', '');
      index = (index + 1) % slides.length;
      slides[index].className += ' on';
    }}, {SLIDE_SECONDS * 1000});
  }}
</script>
</body>
</html>
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fallback", action="store_true")
    parser.add_argument("--items", default=str(ROOT / "data" / "items.json"))
    parser.add_argument("--out", default=str(ROOT / "docs"))
    parser.add_argument("--no-download", action="store_true")
    args = parser.parse_args()

    cfg = load_config()
    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")

    store = cfg.get("store_name", "Weekly Specials")
    link = cfg.get("flyer_public_url", "https://example.com")
    channel = store if "specials" in store.lower() else f"{store} Weekly Specials"

    items = []
    dates_line = ""
    if not args.fallback:
        items_path = pathlib.Path(args.items)
        if items_path.exists():
            payload = json.loads(items_path.read_text(encoding="utf-8"))
            items = payload.get("items", [])
            pub = payload.get("publication", {})
            if pub.get("valid_from") and pub.get("valid_to"):
                dates_line = f"{pub['valid_from']} to {pub['valid_to']}"

    presets = [p for p in cfg.get("preset_slides", []) if p.get("enabled", True)]
    preset_lines = [" \u2014 ".join(part for part in
                               (str(p.get("title", "")).strip(),
                                str(p.get("sub", "")).strip()) if part)
                    for p in presets]

    if args.fallback or not items:
        if not args.fallback:
            print("No items available -- building fallback so the sign "
                  "never shows stale prices.", file=sys.stderr)
        lines = cfg.get("fallback_lines", ["Weekly specials in store now"]) + preset_lines
        (out_dir / "sign-feed.xml").write_text(rss(channel, lines, link), encoding="utf-8")
        (out_dir / "index.html").write_text(
            page(cfg, [], "See flyer in store", presets), encoding="utf-8")
        print(f"Fallback build written to {out_dir}/")
        return

    if cfg.get("download_images", True) and not args.no_download:
        download_images(items, out_dir)

    def ticker(item):
        deal = " ".join(p for p in (item.get("qualifier", ""),
                                    item.get("price", "")) if p)
        return f"{item['name']} \u2014 {deal}" if deal else item["name"]

    lines = [ticker(item) for item in items] + preset_lines
    (out_dir / "sign-feed.xml").write_text(rss(channel, lines, link), encoding="utf-8")
    (out_dir / "index.html").write_text(page(cfg, items, dates_line, presets),
                                        encoding="utf-8")
    print(f"Wrote {len(items)} deal slides + {len(presets)} preset slides "
          f"+ RSS to {out_dir}/")


if __name__ == "__main__":
    main()
