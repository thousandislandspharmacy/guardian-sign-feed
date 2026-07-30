#!/usr/bin/env python3
"""capture_playwright.py -- headless capture of the flyer viewer's API calls.

Loads the public flyer page in headless Chromium, records every request whose
URL looks like Flipp infrastructure, and writes the list to urls.txt so
discover.py can rebuild config.json. Only needed for the one-time setup or if
Flipp rotates the access token (scrape.py starts getting 401/403).

Needs:   pip install playwright && playwright install chromium
Usage:   python capture_playwright.py [--headed]
         python discover.py urls.txt

--headed opens a visible browser; use it if the headless run captures no
Flipp traffic, and click a flyer tile to trigger the item fetches.

Alternative without Playwright: the manual Chrome DevTools HAR export
described in README.md feeds discover.py just as well.
"""
import pathlib
import sys

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent
FLYER_URL = ("https://www.guardian-ida-remedysrx.ca/en/flyer"
             "?storeCode=7063802&retailerId=guardian")
HOST_KEYWORDS = ("flipp", "wishabi")


def main():
    headed = "--headed" in sys.argv
    urls = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        page = browser.new_page()
        page.on("request", lambda req: urls.append(req.url))
        try:
            page.goto(FLYER_URL, wait_until="load", timeout=90000)
        except Exception as exc:  # noqa: BLE001 -- capture whatever loaded
            print(f"Page load did not finish cleanly ({exc}); "
                  "continuing with what was captured.", file=sys.stderr)
        # The embedded viewer fetches publications/products after page load.
        page.wait_for_timeout(15000 if headed else 10000)
        browser.close()

    flippish = list(dict.fromkeys(
        u for u in urls if any(k in u.lower() for k in HOST_KEYWORDS)))
    if not flippish:
        sys.exit("No Flipp traffic captured. Re-run with --headed and click "
                 "a flyer tile, or use the manual HAR export in README.md.")
    (ROOT / "urls.txt").write_text("\n".join(flippish) + "\n", encoding="utf-8")
    print(f"Captured {len(flippish)} Flipp request URLs -> urls.txt")
    print("Next: python discover.py urls.txt")


if __name__ == "__main__":
    main()
