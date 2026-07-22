"""
KCET (KEA) + COMEDK counselling update monitor.

Tracks TWO kinds of change per site:
1. New links appearing (new notices/PDFs/articles).
2. General visible text changing anywhere on the page (banner text,
   dates, captions, image-replacement-with-new-text, etc.) - this catches
   updates that don't come with a brand new link, like a placeholder
   "date to be announced" being swapped for an actual date.

Both are compared against the last saved snapshot in state.json.
On the very first run there's no "previous" state yet, so it just saves
a baseline and does NOT send a notification (otherwise you'd get
everything that already exists on the page at once).
"""

import os
import json
import sys
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

STATE_FILE = "state.json"

# Add or edit sites here. "name" is just a label used in the WhatsApp message.
# track_text=True also watches for any visible text change on the page,
# not just new links - useful for sites that update banners/dates in place.
SITES = [
    {
        "name": "KEA / KCET",
        "url": "https://engineering.careers360.com/articles/kcet-latest-news-and-updates",
        # Only count links pointing to actual KCET news articles (filters out
        # the huge amount of unrelated navigation menu links on this page).
        "href_must_contain": ["news.careers360.com", "kcet"],
        "track_text": True,
    },
    {
        "name": "COMEDK",
        "url": "https://www.comedk.org/",
        "track_text": True,
    },
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

# Links with these hrefs are just navigation/junk, not content updates.
SKIP_HREF_PREFIXES = ("javascript:", "#", "mailto:", "tel:")

# Minimum length for a text chunk to count - filters out single-word
# nav labels / button text so we don't get pinged for trivial noise.
MIN_TEXT_CHUNK_LEN = 15


def fetch_html(url: str) -> str:
    """Fetch a page's HTML, retrying once on failure."""
    last_error = None
    for attempt in range(2):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=45)
            resp.raise_for_status()
            return resp.text
        except Exception as e:
            last_error = e
            if attempt == 0:
                print(f"[INFO] First attempt failed for {url}, retrying once...")
                continue
    raise last_error


def extract_links(html: str, base_url: str, href_must_contain=None) -> set[str]:
    """Return a set of 'link text || full url' strings for real links on the page.
    If href_must_contain is given, only links whose full URL contains ALL of
    those substrings (case-insensitive) are kept."""
    soup = BeautifulSoup(html, "html.parser")
    links = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = a.get_text(strip=True)
        if not text or len(text) < 5:
            continue
        if href.startswith(SKIP_HREF_PREFIXES):
            continue
        full_url = urljoin(base_url, href)
        if href_must_contain:
            lowered = full_url.lower()
            if not all(sub in lowered for sub in href_must_contain):
                continue
        links.add(f"{text} || {full_url}")
    return links


def extract_text_chunks(html: str) -> set[str]:
    """Return a set of distinct visible text snippets on the page (one per
    tag's own text), ignoring scripts/styles and very short fragments."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    chunks = set()
    for s in soup.stripped_strings:
        s = " ".join(s.split())  # collapse internal whitespace
        if len(s) >= MIN_TEXT_CHUNK_LEN:
            chunks.add(s)
    return chunks


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def send_whatsapp(body: str) -> None:
    from twilio.rest import Client

    sid = os.environ["TWILIO_ACCOUNT_SID"]
    token = os.environ["TWILIO_AUTH_TOKEN"]
    from_number = os.environ["TWILIO_WHATSAPP_FROM"]
    to_number = os.environ["MY_WHATSAPP_NUMBER"]

    client = Client(sid, token)
    client.messages.create(
        from_=f"whatsapp:{from_number}",
        to=f"whatsapp:{to_number}",
        body=body[:1500],
    )


def main() -> None:
    state = load_state()
    any_new = False

    for site in SITES:
        name = site["name"]
        url = site["url"]
        href_filter = site.get("href_must_contain")
        track_text = site.get("track_text", False)

        try:
            html = fetch_html(url)
        except Exception as e:
            print(f"[WARN] Could not fetch {name} ({url}): {e}")
            continue

        current_links = extract_links(html, url, href_must_contain=href_filter)
        site_state = state.get(name)

        # First time ever seeing this site: just save a baseline.
        if site_state is None:
            entry = {"links": sorted(current_links)}
            if track_text:
                current_text = extract_text_chunks(html)
                entry["text"] = sorted(current_text)
                print(f"[INFO] {name}: first run, saving baseline "
                      f"({len(current_links)} links, {len(current_text)} text chunks).")
            else:
                print(f"[INFO] {name}: first run, saving baseline ({len(current_links)} links).")
            state[name] = entry
            any_new = True
            continue

        # Backward-compat: older state.json versions stored a plain list.
        if isinstance(site_state, list):
            site_state = {"links": site_state}

        messages = []

        # --- Check 1: new links ---
        previous_links = set(site_state.get("links", []))
        new_links = current_links - previous_links
        if new_links:
            lines = [f"🔔 {name} - new link(s):"]
            for item in sorted(new_links)[:10]:
                text, link_url = item.split(" || ", 1)
                lines.append(f"- {text}\n  {link_url}")
            messages.append("\n".join(lines))
            site_state["links"] = sorted(current_links)
            any_new = True
        else:
            print(f"[INFO] {name}: no new links.")

        # --- Check 2: general text/content changes ---
        if track_text:
            current_text = extract_text_chunks(html)
            if "text" not in site_state:
                # Enabling text-tracking for the first time on an existing site.
                site_state["text"] = sorted(current_text)
                any_new = True
                print(f"[INFO] {name}: starting text-change tracking "
                      f"baseline ({len(current_text)} chunks).")
            else:
                previous_text = set(site_state["text"])
                new_text = current_text - previous_text
                if new_text:
                    lines = [f"🔔 {name} - page content changed:"]
                    for chunk in sorted(new_text)[:5]:
                        preview = chunk if len(chunk) <= 200 else chunk[:200] + "..."
                        lines.append(f"- {preview}")
                    messages.append("\n".join(lines))
                    site_state["text"] = sorted(current_text)
                    any_new = True
                else:
                    print(f"[INFO] {name}: no text changes.")

        state[name] = site_state

        for msg in messages:
            try:
                send_whatsapp(msg)
                print(f"[INFO] WhatsApp sent for {name}.")
            except Exception as e:
                print(f"[ERROR] Failed to send WhatsApp for {name}: {e}")

    if any_new:
        save_state(state)


if __name__ == "__main__":
    sys.exit(main())
