#!/usr/bin/env python3
"""generate.py -- build the two things ViPlex consumes, from data/items.json:

    docs/sign-feed.xml   -> RSS 2.0 ticker (ViPlex RSS widget)
    docs/index.html      -> 336x144 rotating deal slides (ViPlex web page widget)
    docs/img/*           -> product photos on white rounded cards, self-hosted
                            so the sign never depends on Flipp's CDN

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
import base64
import datetime
import io
import json
import pathlib
import re
import shutil
import sys
from xml.sax.saxutils import escape

ROOT = pathlib.Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"

# --- LED sign design constants -------------------------------------------
# The canvas is a 336x144 physical LED matrix read from a moving car, in an
# old Android webview. Design system v4 clones Eric's hand-made ViPlex deal
# slides (OneDrive "Viplex outdoor sign slides", boost/tylenol/jamieson):
# Guardian Green field, the price set huge inside a red hexagon badge on the
# left, brand name + red rule + descriptor in the middle, and the product
# photo on a white rounded card on the right. Photos are NEVER cut out --
# the white card absorbs the studio background, so packaging can't end up
# with holes punched through its own white areas.
W, H = 336, 144
FG = "#FFFFFF"
GREEN = "#00643C"    # Guardian Green, PMS 3425 -- field on every slide
MOTIF = "#12724A"    # tonal green -- hexagon motif only
RED = "#EE3124"      # Guardian/I.D.A. Red, PMS 3556 -- badge + rule + sliver
LIGHT = "#F7F6F5"    # Light Grey -- dates line only
GREEN_RGB = (0, 100, 60)
MOTIF_RGB = (18, 114, 74)
RED_RGB = (238, 49, 36)
SLIDE_SECONDS = 7
RELOAD_SECONDS = 1800  # webview re-pulls the page every 30 min, so a
                       # Friday-morning flip reaches the sign before the
                       # store opens at 8 (was 6 h, which could lag a
                       # 7am build until 11am)

# Geometry, sign px. Measured off the hand-made slides.
BADGE_X, BADGE_Y, BADGE_W, BADGE_H = 10, 26, 88, 92   # red hexagon badge
COL_X = 108                                            # text column left edge
CARD_RIGHT = 330                                       # card right edge
CARD_PAD, CARD_RADIUS = 6, 8
SS = 4  # supersample factor for crisp hexagon/corner edges

# PT Sans, subset from the pharmacy website's self-hosted fonts (fonts/,
# built by scripts in the repo history). Embedded as data-URI TTF -- TTF
# because the player webview predates woff2 -- with Arial as fallback.
# All text is wrapped at BUILD time by measuring with these same fonts, so
# layout never depends on the webview making the same wrapping choices.
FONTS_DIR = ROOT / "fonts"
FONT_FILES = {700: FONTS_DIR / "pt-sans-700.ttf", 400: FONTS_DIR / "pt-sans-400.ttf"}

# Brands the title extractor recognizes at the start of a flyer item name
# (extend via config "display_brands"). Flyer names put brands in ALL CAPS,
# so unknown brands still work via the caps-run fallback in split_title().
DEFAULT_BRANDS = [
    "tena", "poise", "depend", "always", "always discreet", "attends",
    "prevail", "jamieson", "youtheory", "webber", "webber naturals", "olly",
    "sisu", "ddrops", "emergen-c", "centrum", "caltrate", "materna",
    "boost", "ensure", "glucerna", "resource", "tylenol", "motrin", "advil",
    "aleve", "voltaren", "aspirin", "robaxacet", "robax", "reactine",
    "claritin", "aerius", "allegra", "benadryl", "otrivin", "hydrasense",
    "dristan", "buckley's", "benylin", "cold-fx", "gravol", "imodium",
    "pepto-bismol", "gaviscon", "tums", "nexium", "dulcolax", "restoralax",
    "metamucil", "benefibre", "senokot", "colace", "polysporin", "band-aid",
    "elastoplast", "betadine", "reach", "plackers", "steripod", "colgate",
    "crest", "oral-b", "sensodyne", "listerine", "polident", "poligrip",
    "fixodent", "garnier", "garnier fructis", "whole blends",
    "color sensation", "nivea", "neutrogena", "cetaphil", "aveeno", "dove",
    "olay", "vaseline", "aquaphor", "eucerin", "cerave", "ombrelle",
    "coppertone", "jergens", "gold bond", "pantene", "tresemme", "batiste",
    "nair", "veet", "gillette", "schick", "bic", "secret", "degree",
    "old spice", "head & shoulders", "herbal essences", "q-tips", "kleenex",
    "huggies", "pampers", "stayfree", "o.b.", "playtex", "systane",
    "refresh", "biotrue", "renu", "opti-free", "blink", "visine",
    "thermacare", "salonpas", "icy hot", "motion medicine", "biofreeze",
    "natural factors", "new nordic", "a. vogel", "boiron", "echinaforce",
    "kit", "life brand", "option+", "nature's bounty", "sally hansen",
    "nosh & co.", "nosh & co", "french formula", "johnson's", "canesten",
    "footner", "honibe", "centrum silver",
]


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


# --- build-time text measurement -----------------------------------------

_fonts = {}


def _font(size, weight=700):
    from PIL import ImageFont
    key = (size, weight)
    if key not in _fonts:
        _fonts[key] = ImageFont.truetype(str(FONT_FILES[weight]), size)
    return _fonts[key]


def text_w(s, size, weight=700):
    # 5% headroom so the Arial fallback (slightly wider) still fits
    return _font(size, weight).getlength(s) * 1.05


def wrap_words(words, max_w, max_lines, space_w):
    """words: [(display, px_width)]. Greedy wrap; None if it can't fit."""
    lines, cur, cur_w = [], [], 0.0
    for disp, ww in words:
        if ww > max_w:
            return None
        add = ww if not cur else ww + space_w
        if cur and cur_w + add > max_w:
            lines.append(cur)
            cur, cur_w = [disp], ww
        else:
            cur.append(disp)
            cur_w += add
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        return None
    return [" ".join(line) for line in lines]


# --- image assets: backgrounds, badge, product cards ---------------------

def hex_pts(cx, cy, w, h):
    """Pointy-top hexagon, proportions from the website's signature hex."""
    return [(cx, cy - h / 2), (cx + w / 2, cy - h / 4), (cx + w / 2, cy + h / 4),
            (cx, cy + h / 2), (cx - w / 2, cy + h / 4), (cx - w / 2, cy - h / 4)]


def _hex(draw, cx, cy, w, h, fill=None, outline=None, stroke=3):
    pts = [(x * SS, y * SS) for x, y in hex_pts(cx, cy, w, h)]
    if fill:
        draw.polygon(pts, fill=fill)
    else:
        draw.polygon(pts, outline=outline, width=stroke * SS)


# Motif scatter per slide (cycles). Tonal hexes + the red edge sliver from
# the hand-made slides; the card zone (x > 210) stays clear.
BG_VARIANTS = [
    [(-4, -6, 126, 132, "fill"), (170, 150, 46, 48, "fill")],
    [(61, 81, 88, 92, "fill"), (196, -12, 56, 58, "line"), (-34, 46, 76, 80, "red")],
    [(2, -22, 118, 124, "line"), (174, 148, 42, 44, "fill"), (-34, 110, 76, 80, "red")],
]


def render_background(variant):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (W * SS, H * SS), GREEN_RGB)
    d = ImageDraw.Draw(img)
    for cx, cy, w, h, style in BG_VARIANTS[variant % len(BG_VARIANTS)]:
        color = RED_RGB if style == "red" else MOTIF_RGB
        if style == "line":
            _hex(d, cx, cy, w, h, outline=color)
        else:
            _hex(d, cx, cy, w, h, fill=color)
    img = img.resize((W, H), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def render_badge():
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (BADGE_W * SS, BADGE_H * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    _hex(d, BADGE_W / 2, BADGE_H / 2, BADGE_W, BADGE_H, fill=RED_RGB + (255,))
    img = img.resize((BADGE_W * 2, BADGE_H * 2), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def data_uri(data, mime="image/png"):
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


def card_packshot(data):
    """Product photo on a white rounded card, like the hand-made slides.

    The photo is composited whole -- only uniform white MARGINS are trimmed,
    so white areas inside the packaging can never be punched out (the flaw
    in the old flood-fill cutout). Card is baked at 2x display size for the
    LED. Returns (png_bytes, w2x, h2x) or None to keep the original file.
    """
    from PIL import Image, ImageDraw

    img = Image.open(io.BytesIO(data)).convert("RGBA")
    if max(img.size) > 1600:  # phone-camera originals
        img.thumbnail((1600, 1600), Image.LANCZOS)
    flat = Image.new("RGB", img.size, (255, 255, 255))
    flat.paste(img, mask=img.getchannel("A"))

    mask = flat.convert("L").point(lambda v: 255 if v < 248 else 0)
    bbox = mask.getbbox()
    if not bbox:
        return None  # blank image -- keep the original
    pad = 2
    bbox = (max(0, bbox[0] - pad), max(0, bbox[1] - pad),
            min(flat.width, bbox[2] + pad), min(flat.height, bbox[3] + pad))
    flat = flat.crop(bbox)

    aspect = flat.width / flat.height
    if aspect >= 1.3:      # wide (multipacks): wider card, text column narrows
        box = (104, 78)
    elif aspect <= 0.72:   # tall (bottles, tubes)
        box = (62, 96)
    else:
        box = (82, 82)
    flat.thumbnail((box[0] * 2, box[1] * 2), Image.LANCZOS)

    pad2, rad2 = CARD_PAD * 2, CARD_RADIUS * 2
    cw, ch = flat.width + 2 * pad2, flat.height + 2 * pad2
    card = Image.new("RGBA", (cw, ch), (255, 255, 255, 255))
    card.paste(flat, (pad2, pad2))
    m = Image.new("L", (cw * SS, ch * SS), 0)
    ImageDraw.Draw(m).rounded_rectangle(
        (0, 0, cw * SS - 1, ch * SS - 1), radius=rad2 * SS, fill=255)
    card.putalpha(m.resize((cw, ch), Image.LANCZOS))
    out = io.BytesIO()
    card.save(out, "PNG", optimize=True)
    return out.getvalue(), cw, ch


# Evergreen slides: ready-made 336x144 images in evergreen/ that run every
# week regardless of the flyer (store promos: prescriptions, vaccinations,
# now-hiring...). Shown in filename order, woven evenly between deal slides,
# and included in the scrape-failure fallback -- they can't go stale.
EVERGREEN_DIR = ROOT / "evergreen"


def evergreen_slide_uris():
    from PIL import Image
    uris = []
    if not EVERGREEN_DIR.is_dir():
        return uris
    for path in sorted(EVERGREEN_DIR.iterdir()):
        if path.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
            continue
        try:
            data = path.read_bytes()
            with Image.open(io.BytesIO(data)) as im:
                if im.size == (W, H) and path.suffix.lower() == ".png":
                    pass  # exact fit: embed the original bytes untouched
                else:  # letterbox anything else onto the green field
                    im = im.convert("RGB")
                    im.thumbnail((W, H), Image.LANCZOS)
                    canvas = Image.new("RGB", (W, H), GREEN_RGB)
                    canvas.paste(im, ((W - im.width) // 2, (H - im.height) // 2))
                    buf = io.BytesIO()
                    canvas.save(buf, "PNG", optimize=True)
                    data = buf.getvalue()
            uris.append(data_uri(data))
            print(f"  evergreen: {path.name}")
        except Exception as exc:  # noqa: BLE001 -- a bad file skips, never breaks
            print(f"  evergreen {path.name} skipped ({exc})", file=sys.stderr)
    return uris


def weave(primary, extra):
    """Merge two slide lists proportionally so extras spread through the
    rotation instead of clumping at the end."""
    if not primary or not extra:
        return primary or extra
    merged = []
    pi = ei = 0
    while pi < len(primary) or ei < len(extra):
        if pi < len(primary) and (ei >= len(extra)
                                  or pi * len(extra) <= ei * len(primary)):
            merged.append(primary[pi])
            pi += 1
        else:
            merged.append(extra[ei])
            ei += 1
    return merged


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
    """Self-host carded photos under docs/img/. On failure keep the remote URL."""
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
                carded = card_packshot(data)
            except Exception as exc:  # noqa: BLE001 -- carding is best-effort
                print(f"  image {index:02d} kept as-is ({exc})", file=sys.stderr)
                carded = None
            if carded:
                data, suffix = carded[0], ".png"
                item["_w"], item["_h"] = carded[1], carded[2]
            local = img_dir / f"{index:02d}{suffix}"
            local.write_bytes(data)
            item["image"] = f"img/{local.name}"
            try:  # data URI for the self-contained page
                if not carded:
                    from PIL import Image
                    with Image.open(io.BytesIO(data)) as im:
                        item["_w"], item["_h"] = im.size
                mime = "image/png" if suffix == ".png" else "image/jpeg"
                item["_data_uri"] = data_uri(data, mime)
            except Exception:  # noqa: BLE001 -- fall back to the file path
                pass
        except Exception as exc:  # noqa: BLE001 -- any failure => hotlink instead
            print(f"  image {index:02d} kept remote ({exc})", file=sys.stderr)


# --- title / descriptor / price parsing ----------------------------------

CONNECTORS = {"or", "and", "&", "+"}


def _norm(tok):
    return tok.strip(",").lower()


def _is_caps(tok):
    core = tok.strip(",&+*")
    return len(core) >= 2 and core.isupper() and any(c.isalpha() for c in core)


def split_title(name, brands):
    """Flyer name -> (title tokens, remainder tokens).

    Title = the leading brand(s): known brands (incl. "or"/","-joined brand
    lists like "TYLENOL or MOTRIN") when recognized, else the leading
    ALL-CAPS run, else the first word. Mirrors how the hand-made slides
    pull "BOOST" out of "BOOST CARB SMART ... Selected Types and Sizes".
    """
    tokens = name.split()
    if not tokens:
        return [], []
    seqs = sorted((b.split() for b in brands if b.strip()), key=len, reverse=True)

    def match_at(i):
        for seq in seqs:
            n = len(seq)
            if [_norm(t) for t in tokens[i:i + n]] == seq:
                return n
        return 0

    n = match_at(0)
    if n:
        i = n
        while i < len(tokens):
            if _norm(tokens[i]) in CONNECTORS:
                m = match_at(i + 1)
                if m:
                    i += 1 + m
                    continue
            elif tokens[i - 1].endswith(","):
                m = match_at(i)
                if m:
                    i += m
                    continue
            break
        return tokens[:i], tokens[i:]

    run = 0
    k = 0
    while k < len(tokens):
        if _is_caps(tokens[k]):
            run = k + 1
            k += 1
            continue
        if run and _norm(tokens[k]) in CONNECTORS and k + 1 < len(tokens) \
                and _is_caps(tokens[k + 1]):
            k += 1
            continue
        break
    i = run if run else 1
    return tokens[:i], tokens[i:]


def build_descriptor(rest):
    """Remainder tokens -> the hand-made slides' descriptor voice:
    "Carb Smart, Plus Calories or High Protein ... · Selected Types & Sizes".
    ALL-CAPS variant names become Title Case; other casing is left alone so
    proper nouns (Ddrops, mL) survive."""
    s = " ".join(rest)
    s = re.sub(r"(?i)\b(?:On )?Selected (Types|Products|Varieties)( and Sizes)?\b",
               lambda m: "· Selected " + m.group(1).capitalize()
               + (" & Sizes" if m.group(2) else ""), s)
    words = []
    for t in s.split():
        if _norm(t) == "and":
            words.append("&")
        elif _is_caps(t):
            words.append(t.capitalize())
        else:
            words.append(t)
    s = " ".join(words).strip(" ,")
    s = re.sub(r"\s+", " ", s)
    # The name—description separator from scrape.py lands at the start of
    # the remainder (or against the qualifier dot) once the title is split
    # off; a dangling connector can survive odd phrasings too. Neither
    # reads as anything at sign scale -- drop them.
    s = re.sub(r"^[—–-]\s*", "", s)
    s = re.sub(r"(?:\s*[—–-])+\s*(?=·)", " ", s)
    s = re.sub(r"\s(?:or|&|and)\s(?=·)", " ", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip(" ,")
    return s[1:].strip() if s.startswith("·") else s


def parse_deal(item):
    price = (item.get("price") or "").strip()
    qual = (item.get("qualifier") or "").strip()
    story = (item.get("story") or "").strip()
    m = re.match(r"^(\d+)\s*%\s*OFF\*?$", price, re.I)
    if m:
        return {"kind": "percent", "num": m.group(1), "qual": qual, "story": story}
    m = re.match(r"^(\d+)\s*/\s*\$\s*(\d+)(?:\.(\d{2}))?$", price)
    if m:
        cents = None if m.group(3) in (None, "00") else m.group(3)
        return {"kind": "money", "dollars": m.group(2), "cents": cents,
                "qual": qual or f"{m.group(1)} for", "story": story}
    m = re.match(r"^\$\s*(\d+)(?:\.(\d{2}))?$", price)
    if m:
        cents = None if m.group(2) in (None, "00") else m.group(2)
        return {"kind": "money", "dollars": m.group(1), "cents": cents,
                "qual": qual, "story": story}
    return {"kind": "raw", "text": price, "qual": qual, "story": story}


# --- badge composition ---------------------------------------------------

def _fit_size(s, sizes, weight, max_w):
    for size in sizes:
        if text_w(s, size, weight) <= max_w:
            return size
    return sizes[-1]


def _brow(top, height, size, inner, extra=""):
    return (f'<div class="brow" style="top:{top}px;height:{height}px;'
            f'line-height:{height}px;font-size:{size}px;{extra}">{inner}</div>')


def _money_row_html(dollars, cents, main):
    cur, sup = int(round(main * 0.48)), int(round(main * 0.44))
    lift = int(round(main * 0.33))
    parts = [f'<span class="bsup" style="font-size:{cur}px;top:-{lift}px">$</span>',
             escape(dollars)]
    if cents:
        parts.append(f'<span class="bsup" style="font-size:{sup}px;'
                     f'top:-{lift}px">{escape(cents)}</span>')
    return "".join(parts)


def _money_row_w(dollars, cents, main):
    w = text_w("$", int(round(main * 0.48))) + text_w(dollars, main)
    if cents:
        w += text_w(cents, int(round(main * 0.44))) + 2
    return w


def badge_html(deal):
    """Rows of text laid over the red hexagon, sized to its taper."""
    rows = []  # (height, builder(top) -> html)
    if deal["kind"] == "percent":
        num = deal["num"]
        main = 40 if len(num) <= 2 else 32
        lift = int(round(main * 0.33))
        num_html = (escape(num) + f'<span class="bsup" style="font-size:'
                    f'{int(round(main * 0.5))}px;top:-{lift}px">%</span>')
        rows.append((main, lambda t, h=main, x=num_html: _brow(t, h, h, x)))
        rows.append((20, lambda t: _brow(t, 20, 18, "OFF")))
    elif deal["kind"] == "money":
        if deal["qual"]:
            q = escape(deal["qual"].upper())
            rows.append((14, lambda t, q=q: _brow(
                t, 14, _fit_size(deal["qual"].upper(), (13, 12, 11, 10), 700, 64), q)))
        main = 40
        while main > 26 and _money_row_w(deal["dollars"], deal["cents"], main) > 82:
            main -= 4
        rows.append((main, lambda t, m=main: _brow(
            t, m, m, _money_row_html(deal["dollars"], deal["cents"], m))))
        # "each" is the default unit line, but on multi-buy pricing ("2 FOR
        # $13") it contradicts the qualifier -- show it only for unit deals.
        bottom = deal["story"] or ("" if deal["qual"] else "each")
        if bottom:
            rows.append((13, lambda t, b=bottom: _brow(
                t, 13, _fit_size(b, (12, 11, 10, 9), 700, 60), escape(b))))
    else:
        text = deal["text"] or "SEE FLYER"
        size = _fit_size(text, (20, 17, 14, 12), 700, 78)
        words = [(t, text_w(t, size)) for t in text.split()]
        lines = wrap_words(words, 78, 2, text_w(" ", size)) or [text]
        for line in lines:
            rows.append((size + 3, lambda t, s=size, x=escape(line): _brow(t, s + 3, s, x)))
        if deal["story"]:
            rows.append((12, lambda t: _brow(
                t, 12, _fit_size(deal["story"], (11, 10, 9), 700, 60),
                escape(deal["story"]))))

    gap = 3
    total = sum(h for h, _ in rows) + gap * (len(rows) - 1)
    top = int(round(BADGE_Y + (BADGE_H - total) / 2))
    out = []
    for height, build in rows:
        out.append(build(top))
        top += height + gap
    return "".join(out)


# --- slide assembly ------------------------------------------------------

def title_lines_html(tokens, col_w):
    """Fit the brand title: bold caps, connectors small + lowercase.
    Returns (font_size, [line_html]), wrapped at build time."""
    for size in (32, 28, 25, 22, 19, 17, 15, 13):
        small = max(11, int(round(size * 0.6)))
        words = []
        for t in tokens:
            if _norm(t) in CONNECTORS:
                words.append((t.lower(), text_w(t.lower(), small, 400)))
            else:
                disp = t.upper()
                words.append((disp, text_w(disp, size)))
        lines = wrap_words(words, col_w, 2, text_w(" ", size))
        if lines:
            break
    else:
        lines = [" ".join(t.upper() for t in tokens)]
        size, small = 13, 11
    html_lines = []
    for line in lines:
        parts = []
        for t in line.split(" "):
            if t.lower() in CONNECTORS and t == t.lower():
                parts.append(f'<span class="tor" style="font-size:{small}px">'
                             f'{escape(t)}</span>')
            else:
                parts.append(escape(t))
        html_lines.append(" ".join(parts))
    return size, html_lines


def desc_lines(s, col_w):
    """Descriptor at 11px, dropping to 10px, then truncating with an ellipsis.
    A "·" that lands at a line start reads as a bullet -- that's fine; a
    dangling "·" right before the ellipsis is not, so truncation strips it."""
    if not s:
        return 11, []
    for size in (11, 10):
        words = [(t, text_w(t, size, 400)) for t in s.split()]
        lines = wrap_words(words, col_w, 2, text_w(" ", size, 400))
        if lines:
            return size, lines
    size = 10
    words = s.split()
    while len(words) > 1:
        words = words[:-1]
        while words and (words[-1] in ("·", "&", ",", "or") or not words[-1]):
            words = words[:-1]
        if not words:
            break
        words[-1] = words[-1].rstrip(",")
        test = " ".join(words) + "…"
        wlist = [(t, text_w(t, size, 400)) for t in test.split()]
        lines = wrap_words(wlist, col_w, 2, text_w(" ", size, 400))
        if lines:
            return size, lines
    return size, [(words[0] if words else s)[:12] + "…"]


def slide_html(item, index, assets, brands):
    bg = assets["bgs"][index % len(assets["bgs"])]
    name = re.sub(r"[®™]", "", item.get("name", "")).strip()

    # Product card: baked 2x PNG placed at exact display pixels.
    card = ""
    col_right = 324
    if item.get("_data_uri") and item.get("_w"):
        cw, ch = item["_w"] // 2, item["_h"] // 2
        left = CARD_RIGHT - cw
        top = 72 - ch // 2
        card = (f'<img class="card" style="left:{left}px;top:{top}px;'
                f'width:{cw}px;height:{ch}px;" src="{item["_data_uri"]}" alt="">')
        col_right = left - 8
    elif item.get("image"):  # --no-download testing: hotlink, nominal box
        img = escape(item["image"], {'"': "&quot;", "'": "&#39;"})
        card = f'<img class="card" style="left:226px;top:26px;width:92px;" src="{img}" alt="">'
        col_right = 218

    col_w = max(84, col_right - COL_X)
    title_tokens, rest = split_title(name, brands)
    t_size, t_lines = title_lines_html(title_tokens, col_w)
    d_size, d_lines = desc_lines(build_descriptor(rest), col_w)

    t_lh = int(round(t_size * 1.16))
    d_lh = d_size + 4
    block_h = len(t_lines) * t_lh + 16 + len(d_lines) * d_lh
    y = max(14, int(round(72 - block_h / 2)))

    texts = []
    for line in t_lines:
        texts.append(f'<div class="tline" style="top:{y}px;left:{COL_X}px;'
                     f'width:{col_w}px;height:{t_lh}px;line-height:{t_lh}px;'
                     f'font-size:{t_size}px">{line}</div>')
        y += t_lh
    texts.append(f'<div class="rule" style="top:{y + 6}px;left:{COL_X}px"></div>')
    y += 16
    for line in d_lines:
        texts.append(f'<div class="dline" style="top:{y}px;left:{COL_X}px;'
                     f'width:{col_w}px;height:{d_lh}px;line-height:{d_lh}px;'
                     f'font-size:{d_size}px">{escape(line)}</div>')
        y += d_lh

    return f"""    <div class="slide">
      <img class="bg" src="{bg}" alt="">
      <img class="bimg" src="{assets["badge"]}" alt="">
{badge_html(parse_deal(item))}
      {card}
      {"".join(texts)}
    </div>"""


def fallback_slide(cfg, assets, dates_line):
    store = escape(cfg.get("store_name", "Weekly Specials"))
    sub = escape(cfg.get("brand_sub", "This Week's Specials"))
    return f"""    <div class="slide">
      <img class="bg" src="{assets["bgs"][0]}" alt="">
      <div class="tline" style="top:36px;left:20px;width:300px;height:26px;line-height:26px;font-size:22px">{store}</div>
      <div class="rule" style="top:70px;left:20px"></div>
      <div class="dline" style="top:84px;left:20px;width:300px;height:16px;line-height:16px;font-size:12px;letter-spacing:2px;text-transform:uppercase">{sub}</div>
      <div class="dates">{escape(dates_line)}</div>
    </div>"""


def font_face_css():
    css = []
    for weight, path in FONT_FILES.items():
        if path.exists():
            uri = data_uri(path.read_bytes(), "font/truetype")
            css.append(f"""  @font-face {{ font-family:'PT Sans'; font-weight:{weight};
    src:url({uri}) format('truetype'); }}""")
    return "\n".join(css)


def page(cfg, items, dates_line):
    """Deal slides only. Eric supplies brand/promo slides in ViPlex weekly;
    the lone exception is the scrape-failure fallback (items empty), which
    shows a single evergreen brand card rather than a blank sign."""
    store = escape(cfg.get("store_name", "Weekly Specials"))
    brands = [b.strip().lower() for b in
              (cfg.get("display_brands") or []) + DEFAULT_BRANDS if b.strip()]
    assets = {
        "bgs": [data_uri(render_background(v)) for v in range(len(BG_VARIANTS))],
        "badge": data_uri(render_badge()),
    }
    evergreen = [f'''    <div class="slide">
      <img class="bg" src="{uri}" alt="">
    </div>''' for uri in evergreen_slide_uris()]
    if items:
        deal_slides = [slide_html(item, i, assets, brands)
                       for i, item in enumerate(items)]
        slides = "\n".join(weave(deal_slides, evergreen))
    else:
        slides = "\n".join([fallback_slide(cfg, assets, dates_line)] + evergreen)
    # ES5 only, no external requests: the ViPlex player webview is old and
    # may be firewalled to this one page. Backgrounds, badge, cards and the
    # PT Sans subset all ride inside the HTML as data URIs.
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width={W}, initial-scale=1">
<meta http-equiv="refresh" content="{RELOAD_SECONDS}">
<title>{store}</title>
<style>
  /* Palette: field {GREEN} / motif {MOTIF} / red device {RED}. Old webview:
     absolute px layout, no transforms, no SVG -- shapes are baked PNGs;
     text is pre-wrapped at build time with the same PT Sans it renders. */
{font_face_css()}
  html, body {{ margin:0; padding:0; background:{GREEN}; overflow:hidden; }}
  body {{ width:{W}px; height:{H}px; position:relative; color:{FG};
         font-family:'PT Sans', Arial, Helvetica, sans-serif; }}
  .slide {{ position:absolute; top:0; left:0; width:{W}px; height:{H}px;
            overflow:hidden; opacity:0;
            -webkit-transition:opacity 0.7s; transition:opacity 0.7s; }}
  .slide.on {{ opacity:1; }}
  img {{ border:0; }}
  .bg {{ position:absolute; left:0; top:0; width:{W}px; height:{H}px; }}
  .bimg {{ position:absolute; left:{BADGE_X}px; top:{BADGE_Y}px;
           width:{BADGE_W}px; height:{BADGE_H}px; }}
  .card {{ position:absolute; }}
  .brow {{ position:absolute; left:{BADGE_X}px; width:{BADGE_W}px;
           text-align:center; color:{FG}; font-weight:bold;
           white-space:nowrap; overflow:hidden; }}
  .bsup {{ position:relative; }}
  .tline {{ position:absolute; color:{FG}; font-weight:bold;
            white-space:nowrap; overflow:hidden; }}
  .tor {{ font-weight:normal; }}
  .rule {{ position:absolute; width:40px; height:4px; background:{RED}; }}
  .dline {{ position:absolute; color:{FG}; font-weight:normal;
            white-space:nowrap; overflow:hidden; }}
  .dates {{ position:absolute; left:20px; top:122px; font-size:11px;
            color:{LIGHT}; font-weight:normal; }}
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
        return f"{item['name']} — {deal}" if deal else item["name"]

    lines = [ticker(item) for item in items]
    (out_dir / "sign-feed.xml").write_text(rss(channel, lines, link), encoding="utf-8")
    (out_dir / "index.html").write_text(page(cfg, items, dates_line),
                                        encoding="utf-8")

    # Machine-readable feed for the pharmacy website's "This Week's
    # Specials" section (fetched cross-origin; GitHub Pages sends ACAO:*).
    # Image paths are docs/-relative; the site resolves them against this
    # feed's own URL. No data URIs here -- keep it light.
    payload = json.loads((ROOT / "data" / "items.json").read_text(encoding="utf-8")) \
        if (ROOT / "data" / "items.json").exists() else {}
    specials = {
        "publication": payload.get("publication", {}),
        "generated_at": payload.get("generated_at", ""),
        "flyer_url": link,
        "items": [{k: item.get(k, "") for k in
                   ("name", "price", "qualifier", "story", "image")}
                  for item in items],
    }
    (out_dir / "specials.json").write_text(
        json.dumps(specials, indent=1) + "\n", encoding="utf-8")
    print(f"Wrote {len(items)} deal slides + RSS + specials.json to {out_dir}/")


if __name__ == "__main__":
    main()
