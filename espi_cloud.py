#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ESPI Cloud — wersja do darmowej chmury (GitHub Actions).
Działa BEZ Twojego komputera: cron w chmurze co 10 min sprawdza feed i
wysyła nowe komunikaty na Telegram (z oceną wpływu heurystyczną).

Konfiguracja przez zmienne środowiskowe:
  TELEGRAM_TOKEN   — token bota
  TELEGRAM_CHAT_ID — Twój chat_id
Stan (seen.json) trzymany w repo — skrypt commituje go po każdym uruchomieniu.

Test lokalny:  TELEGRAM_TOKEN=... TELEGRAM_CHAT_ID=... python3 espi_cloud.py
"""

import json
import os
import re
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime

FEED = "https://www.bankier.pl/rss/espi.xml"
SEEN_FILE = os.environ.get("SEEN_FILE", "seen_cloud.json")

RULES = [
    (r"upadł|niewypłacaln|wniosek o ogłoszenie upadłości", 9, -1),
    (r"restrukturyzacj|sanacj", 7, -1),
    (r"utrat\w+ płynności|zaległoś\w+ w spłacie", 7, -1),
    (r"obniżen\w+ (prognoz|szacunk)", 8, -1),
    (r"podwyższen\w+ (prognoz|szacunk)", 8, +1),
    (r"wezwani[ea] do zapisywania|notyfikacja o planowanym ogłoszeniu wezwania", 7, +1),
    (r"karencj|wypowiedzia\w+ umow|wypowiedzen\w+ umow", 6, -1),
    (r"kar\w+ finansow\w+|sankcj|nałożen\w+ kary", 6, -1),
    (r"postanowienie sądu|uchyleni\w+ układu|postępowan\w+ (upadłościow|restrukturyzacyjn)", 5, -1),
    (r"wygaśnięci|niepowodzeni|rezygnacj|odstąpien\w+ od umow", 5, -1),
    (r"emisj\w+ (akcj|obligacji)|podwyższen\w+ kapitału|prawa poboru", 5, -1),
    (r"zby\w+ (pakiet|akcj|udział)|sprzedaż\w+ (pakiet|akcj|udział)", 4, -1),
    (r"zawarc\w+ (umow|porozumien)|podpisan\w+ (umow|porozumien)|kontrakt|zamówien\w+ (o wartości|publiczn)", 5, +1),
    (r"wygr\w+ (przetarg|konkurs)|wyłonien\w+ (oferty|wykonawc)", 5, +1),
    (r"przedwstępn\w+ umow|umowa ramowa|list intencj", 4, +1),
    (r"naby\w+ (pakiet|akcj|udział)|nabyci\w+ (pakiet|akcj|udział)", 4, +1),
    (r"dywidend", 4, +1),
    (r"przejęci\w+|fuzj\w+|połączeni\w+ spółek|podaż do publicznej wiadomości oferty", 6, +1),
    (r"przedłużen\w+ (karencj|terminu)|opóźnien\w+ (raport|publikacj)", 4, -1),
    (r"akcjonariusz\w+ (poniżej|powyżej)|dużego pakietu", 3, 0),
    (r"zmian\w+ (w )?(zarząd|radzie)|odwołan\w+|powołan\w+", 2, 0),
    (r"zwołan\w+ waln\w+|waln\w+ zgromadzen", 2, 0),
    (r"raport|wynik\w+ finansow|sprawozdan|kwartal|śródroczn|roczn\w+ raport|financial report", 5, 0),
    (r"prezentacj|szkoleni|konferencj", 1, 0),
]

EBI_HINTS = re.compile(
    r"raport (okresow|roczn|śródroczn|kwartaln)|sprawozdan|jednostkow\w+|"
    r"skonsolidowan\w+|QSr|Q[rR]\s|RR\b|PSr|financial report", re.IGNORECASE)


def norm(s):
    return unicodedata.normalize("NFC", s).lower()


def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
        "Accept-Language": "pl-PL,pl;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_feed():
    root = ET.fromstring(http_get(FEED))
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        pub = (it.findtext("pubDate") or "").strip()
        if title and link:
            out.append({"title": title, "link": link, "pubDate": pub})
    return out


def company_of(title):
    m = re.match(r"^(.+?)(?::| – | - )", title)
    return m.group(1).strip() if m else "???"


def score(title):
    t = norm(title)
    s, d = 0, 0
    for pat, w, dirn in RULES:
        if re.search(pat, t, re.IGNORECASE):
            s += w
            d += dirn
    s = min(10, s)
    return s, ("wzrost" if d > 0 else ("spadek" if d < 0 else "neutralny"))


def probability(score_):
    return {0: "~10-20%", 2: "~20-35%", 4: "~35-55%", 6: "~55-70%", 8: "~70-85%"}.get(
        next((k for k in sorted([0, 2, 4, 6, 8], reverse=True) if score_ >= k)), "~10-20%")


def tg_send(token, chat_id, text):
    url = ("https://api.telegram.org/bot" + token + "/sendMessage?" +
           urllib.parse.urlencode({"chat_id": chat_id, "text": text[:4000],
                                   "parse_mode": "HTML",
                                   "disable_web_page_preview": "true"}))
    try:
        urllib.request.urlopen(url, timeout=15)
        return True
    except Exception as e:
        print(f"Telegram błąd: {e}")
        return False


def main():
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("❌ Ustaw zmienne TELEGRAM_TOKEN i TELEGRAM_CHAT_ID")
        return

    seen = set()
    if os.path.exists(SEEN_FILE):
        try:
            seen = set(json.load(open(SEEN_FILE)))
        except Exception:
            pass

    items = sorted(fetch_feed(), key=lambda x: x["pubDate"])
    new = [i for i in items if i["link"] not in seen]
    print(f"Feed: {len(items)} pozycji, nowych: {len(new)}")

    sent = 0
    for it in new:
        seen.add(it["link"])
        if EBI_HINTS.search(it["title"]):
            continue  # w chmurze powiadamiamy tylko ESPI (na żywo)
        s, d = score(it["title"])
        arrow = "📈" if d == "wzrost" else ("📉" if d == "spadek" else "➖")
        text = (f"{arrow} <b>{company_of(it['title'])}</b> — {s}/10 ({d})\n"
                f"Ruch: {probability(s)}\n"
                f"{it['title'][:160]}\n"
                f"{it['link']}")
        if tg_send(token, chat_id, text):
            sent += 1

    json.dump(sorted(seen)[-5000:], open(SEEN_FILE, "w"))
    print(f"Wysłano: {sent} | stan: {len(seen)} pozycji | "
          f"{datetime.now():%Y-%m-%d %H:%M:%S}")


if __name__ == "__main__":
    main()
