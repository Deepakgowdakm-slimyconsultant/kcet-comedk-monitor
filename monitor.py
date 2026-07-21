"""
KCET (KEA) + COMEDK counselling update monitor.

What it does:
1. Fetches each target page.
2. Pulls out every link on the page (these sites post updates as new
   links/PDFs, so new links = new updates).
3. Compares against the last saved list in state.json.
4. If there are new links, sends you a WhatsApp message via Twilio
   listing what's new, and updates state.json.

On the very first run there's no "previous" state yet, so it just saves
a baseline and does NOT send a notification (otherwise you'd get every
existing link at once).
"""

import os
import json
import sys
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

STATE_FILE = "state.json"

# Add or edit sites here. "name" is just a label used in the WhatsApp message.
SITES = [
    {
        "name": "KEA / KCET",
        "url": "https://engineering.careers360.com/articles/kcet-latest-news-and-updates",
        # Only count links pointing to actual KCET news articles (filters out
        # the huge amount of unrelated navigation menu links on this page).
        "href_must_contain": ["news.careers360.com", "kcet"],
    },
    {
        "name": "COMEDK",
        "url": "https://www.comedk.org/",
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


def fetch_links(url: str, href_must_contain=None) -> set[str]:
    """Return a set of 'link text || full url' strings for every real link on the page.
    Retries once on failure, since some sites are flaky/slow.
    If href_must_contain is given, only links whose full URL contains ALL of
    those substrings (case-insensitive) are kept."""
    last_error = None
    for attempt in range(2):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=45)
            resp.raise_for_status()
            break
        except Exception as e:
            last_error = e
            if attempt == 0:
                print(f"[INFO] First attempt failed for {url}, retrying once...")
                continue
            raise last_error

    soup = BeautifulSoup(resp.text, "html.parser")

    links = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = a.get_text(strip=True)
        if not text or len(text) < 5:
            continue
        if href.startswith(SKIP_HREF_PREFIXES):
            continue
        full_url = urljoin(url, href)
        if href_must_contain:
            lowered = full_url.lower()
            if not all(sub in lowered for sub in href_must_contain):
                continue
        links.add(f"{text} || {full_url}")
    return links


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
    from_number = os.environ["TWILIO_WHATSAPP_FROM"]  # e.g. +14155238886 (sandbox number)
    to_number = os.environ["MY_WHATSAPP_NUMBER"]       # e.g. +91XXXXXXXXXX

    client = Client(sid, token)
    # WhatsApp messages via Twilio have a ~1600 char practical limit; trim to be safe.
    client.messages.create(
        from_=f"whatsapp:{from_number}",
        to=f"whatsapp:{to_number}",
        body=body[:1500],
    )


def main() -> None:
    state = load_state()
    any_new = False

    for site in SITES:
        name, url = site["name"], site["url"]
        href_filter = site.get("href_must_contain")
        try:
            current_links = fetch_links(url, href_must_contain=href_filter)
        except Exception as e:
            print(f"[WARN] Could not fetch {name} ({url}): {e}")
            continue

        key = name
        previous_links = set(state.get(key, []))

        if key not in state:
            # First run for this site: just establish a baseline.
            print(f"[INFO] {name}: first run, saving baseline of {len(current_links)} links.")
            state[key] = sorted(current_links)
            any_new = True  # state changed, needs saving
            continue

        new_links = current_links - previous_links
        if new_links:
            any_new = True
            print(f"[INFO] {name}: {len(new_links)} new link(s) found.")
            lines = [f"🔔 {name} update(s):"]
            for item in sorted(new_links)[:10]:  # cap so the message doesn't get huge
                text, link_url = item.split(" || ", 1)
                lines.append(f"- {text}\n  {link_url}")
            message = "\n".join(lines)

            try:
                send_whatsapp(message)
                print(f"[INFO] WhatsApp sent for {name}.")
            except Exception as e:
                print(f"[ERROR] Failed to send WhatsApp for {name}: {e}")

            state[key] = sorted(current_links)
        else:
            print(f"[INFO] {name}: no changes.")

    if any_new:
        save_state(state)


if __name__ == "__main__":
    sys.exit(main())
