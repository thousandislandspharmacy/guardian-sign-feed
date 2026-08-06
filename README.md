# Guardian flyer → LED sign feed

Fully automatic pipeline: every Thursday morning a GitHub Action pulls the
current Guardian flyer for store 7063802 from Flipp's API (the same data that
powers the flyer page on guardian-ida-remedysrx.ca), picks the best deals, and
publishes two things to GitHub Pages that the sign consumes:

| Output | URL after setup | ViPlex widget |
|---|---|---|
| `docs/index.html` | `https://YOURNAME.github.io/REPO/` | **Web page** — rotating 336×144 deal slides, product photos on white cards |
| `docs/sign-feed.xml` | `https://YOURNAME.github.io/REPO/sign-feed.xml` | **RSS** — scrolling text ticker of the same deals |

After the one-time setup below, you touch nothing. Flyer flips Thursday,
sign updates itself.

## One-time setup

### 1. Capture the Flipp API calls (2 minutes, once)

The flyer viewer talks to Flipp's API using a merchant ID and access token
that are embedded in the public page. You just need to catch them once:

1. Open the flyer page in Chrome:
   `https://www.guardian-ida-remedysrx.ca/en/flyer?storeCode=7063802&retailerId=guardian`
2. Press **F12** → **Network** tab.
3. In the Network tab's filter box, type **flipp** — then reload the page
   (Ctrl+R) and let the flyer fully load.
4. Right-click any request in the list → **"Save all as HAR with content"**
   → save as `capture.har` in this folder.
   *(Alternative: right-click a few of the requests → Copy → Copy URL, and
   paste them one per line into a text file. Either input works.)*

### 2. Generate the config

```bash
python discover.py capture.har
```

This writes `config.json` with the API base, merchant ID, token, and store
code it found. The token is not a secret — it ships to every visitor of the
public flyer page — so it's fine in the repo. If discover.py reports
`mode='unknown'`, paste the `observed_urls` list from config.json back to
Claude and the scraper gets adapted to whatever API the viewer is using.

### 3. Test locally

```bash
pip install -r requirements.txt
python scrape.py
python generate.py
```

Open `docs/index.html` in a browser — you should see the brand card then
deal slides rotating every 7 seconds. If `scrape.py` errors about field
names, send `data/raw_sample.json` to Claude; it's a de-identified dump of
two raw products so the key mapping can be fixed in one line.

### 4. Publish

1. Create a **public** GitHub repo and push this folder to it.
2. Repo **Settings → Pages → Deploy from a branch → main → /docs → Save**.
3. Wait a minute, then confirm both URLs above load.
4. **Actions** tab → enable workflows → run **Update sign feed** once
   manually (Run workflow) to confirm the whole loop works in the cloud.

### 5. Point ViPlex at it

In your solution for the 336×144 screen:

- **Visual deals:** add a **Web page** widget, full screen (336×144), URL =
  your Pages URL. Set the widget/page refresh interval if your version
  exposes one; the page also self-reloads every 6 h via meta refresh.
- **Optional ticker:** add an **RSS** widget (e.g., a strip across the
  bottom) pointed at the `sign-feed.xml` URL.
- The Taurus player must have internet access for either widget.

## How failures are handled

Stale prices on a road sign are worse than no prices, so the workflow is
built around that:

- If the scrape fails **for any reason** (Flipp changes the API, rotates the
  token, no flyer valid that day), the Action publishes an evergreen
  fallback — brand card only, generic "weekly specials in store" ticker
  lines from `fallback_lines` in config.json — and the job is marked
  **failed**, which sends you GitHub's failure email.
- A Friday retry run self-heals a late-posted Thursday flyer.
- To fix a broken scrape: reload the flyer page, re-export the HAR, re-run
  `discover.py` (token rotation), or send `data/raw_sample.json` /
  `observed_urls` to Claude (field/endpoint changes).

## Tuning (config.json)

- `max_items` — how many deals make the sign (default 12).
- `exclude_keywords` — drop items by name substring (e.g., `"lottery"`).
- `require_image` — set `false` to allow text-only slides.
- `store_name`, `brand_sub`, `fallback_lines` — sign copy.
- `download_images` — self-hosts carded photos under `docs/img/` (default
  true; recommended so the sign never depends on Flipp's CDN).
- `display_brands` — extra brand names the slide titles can recognize at
  the start of an item name (a built-in list covers the common ones).

Items are ranked by discount depth when Flipp provides an original price,
so the deepest cuts lead.

## Hero image overrides (overrides/)

Drop a product photo into `overrides/` and it replaces the scraped flyer
image whenever that product is on sale — the filename is the match
keyword: `tena.png` covers any item whose name contains "tena",
`always-discreet.png` matches "always discreet". Any photo works — the
build trims the margins, sets it on the slide's white rounded card, and
resizes automatically (transparent PNGs land on the same white card).
Add files by committing them, or on github.com:
open the `overrides` folder → **Add file → Upload files**. An override
only ever appears in a week when the flyer actually features that
product, so nothing can go stale.

## Honest caveats

This rides Flipp's **unofficial** embedded-viewer API, reading your own
store's published flyer. It can break without notice — that's what the
fallback + email path is for. If it ever breaks permanently, the same repo
works in manual mode: hand-edit a `data/items.json` and run
`generate.py`, and the sign URLs never change.
