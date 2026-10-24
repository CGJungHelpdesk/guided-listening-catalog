#!/usr/bin/env python3
"""Post the day's Jung quote to the Telegram channel.

Reads quotes.json in this repo (the app's quote file, English wording,
blacklist already applied, located quotes only), picks one quote per day by
a fixed rule so the app, the widget and the channel show the same one, and
posts it with the Telegram bot API.

Environment: TELEGRAM_BOT_TOKEN (repository secret), TELEGRAM_CHAT_ID
(the channel, e.g. @cgjunghelpdesk). With --dry-run it only prints.
Deterministic day rule: index = days since 2026-01-01 modulo the number of
quotes, over the list sorted by id.
"""
import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request

EPOCH = dt.date(2026, 1, 1)


def pick(quotes, day):
    ordered = sorted(quotes, key=lambda q: q["id"])
    return ordered[(day - EPOCH).days % len(ordered)]


def message(q):
    ref = q.get("reference") or ""
    book = q.get("book") or ""
    tail = ", ".join(x for x in (book, ref) if x)
    return f"“{q['text']}”\n\nC.G. Jung, {tail}" if tail else f"“{q['text']}”\n\nC.G. Jung"


def main():
    dry = "--dry-run" in sys.argv
    with open(os.path.join(os.path.dirname(__file__), "..", "quotes.json"), encoding="utf-8") as f:
        quotes = json.load(f)["quotes"]
    q = pick(quotes, dt.date.today())
    text = message(q)
    if dry:
        print(text)
        return
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat = os.environ.get("TELEGRAM_CHAT_ID", "@cgjunghelpdesk")
    data = urllib.parse.urlencode({"chat_id": chat, "text": text, "disable_web_page_preview": "true"}).encode()
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=30) as r:
        print(r.status)


if __name__ == "__main__":
    main()
