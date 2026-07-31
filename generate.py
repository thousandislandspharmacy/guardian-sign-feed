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
# Design system: Guardian Green field on every slide, tonal pharmacy-cross
# motif, and ONE red device (logo bar / promo chip / skewed price ticket)
# reused everywhere so the family reads as a set. Palette per the Guardian
# branding summary (GDN_Branding_Summary v3.0); layout per the LED design
# spec worked out with Eric 2026-08-01.
FG = "#FFFFFF"
GREEN = "#00643C"    # Guardian Green, PMS 3425 -- field on every slide
MOTIF = "#12724A"    # tonal green -- cross motif only
RED = "#EE3124"      # Guardian/I.D.A. Red, PMS 3556 -- the red device only
LIGHT = "#F7F6F5"    # Light Grey -- savings/dates lines only
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


def clean_cutout(data):
    """Deterministic packshot cleanup for a black LED canvas.

    Lifts the near-white studio background (flood fill from the edges only,
    so white areas INSIDE the packaging are untouched), trims the margins,
    and downscales to ~3x display size. No AI, no redrawing -- product
    pixels are never modified. Returns PNG bytes, or None to keep the
    original file.
    """
    import io
    from collections import deque

    from PIL import Image

    img = Image.open(io.BytesIO(data)).convert("RGBA")
    if max(img.size) > 1600:  # phone-camera originals: shrink before the fill
        img.thumbnail((1600, 1600), Image.LANCZOS)
    w, h = img.size
    px = img.load()
    threshold = 235  # JPEG-artifact tolerant "white"

    def is_bg(p):
        return p[3] == 0 or (p[0] >= threshold and p[1] >= threshold
                             and p[2] >= threshold)

    seen = bytearray(w * h)
    queue = deque()
    queue.extend((x, y) for x in range(w) for y in (0, h - 1))
    queue.extend((x, y) for y in range(h) for x in (0, w - 1))
    while queue:
        x, y = queue.popleft()
        if not (0 <= x < w and 0 <= y < h) or seen[y * w + x]:
            continue
        seen[y * w + x] = 1
        if not is_bg(px[x, y]):
            continue
        px[x, y] = (0, 0, 0, 0)
        queue.extend(((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)))

    bbox = img.getbbox()
    if not bbox:
        return None  # image was entirely background -- keep the original
    img = img.crop(bbox)
    if max(img.size) > 400:
        img.thumbnail((400, 400), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()


def find_override(name):
    """Hand-made hero shot from overrides/, matched by filename keyword.

    overrides/tena.png matches any item whose name contains "tena";
    dashes/underscores in the filename become spaces ("always-discreet.png").
    """
    folder = ROOT / "overrides"
    if not folder.is_dir():
        return None
    lowered = str(name).lower()
    for path in sorted(folder.iterdir()):
        if path.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
            continue
        keyword = path.stem.lower().replace("-", " ").replace("_", " ").strip()
        if keyword and keyword in lowered:
            return path
    return None


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
        override = find_override(item.get("name", ""))
        if not url and not override:
            continue
        try:
            if override:
                data = override.read_bytes()
                suffix = override.suffix.lower()
                print(f"  image {index:02d}: override {override.name}")
            else:
                resp = requests.get(url, timeout=15,
                                    headers={"User-Agent": "Mozilla/5.0 (pharmacy sign feed)"})
                resp.raise_for_status()
                data = resp.content
                suffix = ".png" if ".png" in url.lower() else ".jpg"
            try:
                cleaned = clean_cutout(data)
            except Exception as exc:  # noqa: BLE001 -- cleanup is best-effort
                print(f"  image {index:02d} kept as-is ({exc})", file=sys.stderr)
                cleaned = None
            if cleaned:
                data, suffix = cleaned, ".png"
            local = img_dir / f"{index:02d}{suffix}"
            local.write_bytes(data)
            item["image"] = f"img/{local.name}"
        except Exception as exc:  # noqa: BLE001 -- any failure => hotlink instead
            print(f"  image {index:02d} kept remote ({exc})", file=sys.stderr)


def cross(style):
    return (f'<div class="cross" style="{style}">'
            '<div class="cross-v"></div><div class="cross-h"></div></div>')


def slide_html(item):
    name = escape(item["name"])
    price = escape(item.get("price", ""))
    qual = escape(item.get("qualifier", ""))
    story = escape(item.get("story", ""))
    img = escape(item.get("image", ""), {'"': "&quot;", "'": "&#39;"})
    pic = (f'<div class="pic" style="background-image:url(\'{img}\')"></div>'
           if img else "")
    # 44px core fits up to 6 chars ("$13.99") beside the product zone; longer
    # cores, and any plate carrying a qualifier line, drop to 34px so the
    # plate's bottom edge clears the savings line's fixed 122px anchor.
    plate_cls = ("plate" if not qual and len(item.get("price", "")) <= 6
                 else "plate p-mid")
    qual_html = (f'<span class="plate-inner plate-qual">{qual}</span>'
                 if qual else "")
    story_html = f'<div class="story">{story}</div>' if story else ""
    return f"""    <div class="slide">
      {cross("width:110px;height:110px;right:-28px;top:-22px;")}
      {pic}
      <div class="name">{name}</div>
      <div class="{plate_cls}">{qual_html}<span class="plate-inner price">{price}</span></div>
      {story_html}
    </div>"""


def page(cfg, items, dates_line):
    """Deal slides only. Eric supplies brand/promo slides in ViPlex weekly;
    the lone exception is the scrape-failure fallback (items empty), which
    shows a single evergreen brand card rather than a blank sign."""
    store = escape(cfg.get("store_name", "Weekly Specials"))
    sub = escape(cfg.get("brand_sub", "This Week's Specials"))
    if items:
        slides = "\n".join(slide_html(item) for item in items)
    else:
        slides = f"""    <div class="slide">
      {cross("width:140px;height:140px;right:-40px;top:-50px;")}
      {cross("width:84px;height:84px;right:44px;bottom:-28px;")}
      <div class="brand-block">
        <span class="wordmark"><span class="store">{store}</span><span class="redbar"></span></span>
        <div class="tagline">{sub}</div>
      </div>
      <div class="dates">{escape(dates_line)}</div>
    </div>"""
    # ES5 only, no external assets: the ViPlex player webview is old and may
    # be firewalled to this one page.
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="{RELOAD_SECONDS}">
<title>{store}</title>
<style>
  /* Palette: field {GREEN} / motif {MOTIF} / red device {RED}
     / text {FG} / secondary {LIGHT}. Old webview: absolute px layout,
     -webkit- twins on every transform, no var()/object-fit/webfonts. */
  html, body {{ margin:0; padding:0; background:{GREEN}; overflow:hidden; }}
  body {{ width:{W}px; height:{H}px; position:relative; color:{FG};
         font-family:Arial, Helvetica, sans-serif; font-weight:bold; }}
  .slide {{ position:absolute; top:0; left:0; width:{W}px; height:{H}px;
            background:{GREEN}; overflow:hidden; display:none; }}
  .slide.on {{ display:block; }}
  .cross {{ position:absolute; z-index:1; }}
  .cross-v {{ position:absolute; left:33.33%; top:0; width:33.34%;
              height:100%; background:{MOTIF}; border-radius:4px; }}
  .cross-h {{ position:absolute; top:33.33%; left:0; width:100%;
              height:33.34%; background:{MOTIF}; border-radius:4px; }}
  .pic {{ position:absolute; left:198px; top:6px; width:132px; height:132px;
          background-size:contain; background-position:center center;
          background-repeat:no-repeat; z-index:2; }}
  .name {{ position:absolute; left:12px; top:12px; width:182px; height:42px;
           overflow:hidden; font-size:18px; line-height:21px; color:{FG};
           text-transform:uppercase; z-index:3; }}
  .plate {{ position:absolute; left:12px; top:62px; background:{RED};
            padding:3px 12px 5px; z-index:3;
            -webkit-transform:skewX(-6deg); transform:skewX(-6deg); }}
  .plate-inner {{ display:block; color:{FG}; white-space:nowrap;
                  -webkit-transform:skewX(6deg); transform:skewX(6deg); }}
  .plate-qual {{ font-size:13px; line-height:15px; }}
  .price {{ font-size:44px; line-height:44px; }}
  .p-mid .price {{ font-size:34px; line-height:36px; }}
  .story {{ position:absolute; left:12px; top:122px; width:182px; height:16px;
            overflow:hidden; font-size:13px; color:{LIGHT};
            text-transform:uppercase; z-index:3; }}
  .brand-block {{ position:absolute; left:20px; top:32px; z-index:3; }}
  .wordmark {{ display:inline-block; }}
  .store {{ display:block; font-size:20px; line-height:24px; color:{FG};
            white-space:nowrap; }}
  .redbar {{ display:block; height:5px; background:{RED}; margin-top:3px; }}
  .tagline {{ margin-top:10px; font-size:14px; letter-spacing:2px;
              text-transform:uppercase; color:{FG}; }}
  .dates {{ position:absolute; left:20px; top:122px; font-size:12px;
            color:{LIGHT}; z-index:3; }}
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

    if args.fallback or not items:
        if not args.fallback:
            print("No items available -- building fallback so the sign "
                  "never shows stale prices.", file=sys.stderr)
        lines = cfg.get("fallback_lines", ["Weekly specials in store now"])
        (out_dir / "sign-feed.xml").write_text(rss(channel, lines, link), encoding="utf-8")
        (out_dir / "index.html").write_text(
            page(cfg, [], "See flyer in store"), encoding="utf-8")
        print(f"Fallback build written to {out_dir}/")
        return

    if cfg.get("download_images", True) and not args.no_download:
        download_images(items, out_dir)

    def ticker(item):
        deal = " ".join(p for p in (item.get("qualifier", ""),
                                    item.get("price", "")) if p)
        return f"{item['name']} \u2014 {deal}" if deal else item["name"]

    lines = [ticker(item) for item in items]
    (out_dir / "sign-feed.xml").write_text(rss(channel, lines, link), encoding="utf-8")
    (out_dir / "index.html").write_text(page(cfg, items, dates_line),
                                        encoding="utf-8")
    print(f"Wrote {len(items)} deal slides + RSS to {out_dir}/")


if __name__ == "__main__":
    main()
