#!/usr/bin/env python3
"""
Berlin Rent Radar - single-file versie voor GitHub Actions.

Draait EEN ronde en stopt. De planning doet GitHub Actions (cron).
Status wordt bewaard in een tekstbestand dat de workflow terugcommit.

Instellingen komen uit omgevingsvariabelen (zie .github/workflows/radar.yml).
"""
from __future__ import annotations

import html
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("radar")

BASE = "https://www.inberlinwohnen.de"
FINDER = f"{BASE}/wohnungsfinder"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
LANDLORDS = {
    "wbm.de": "WBM",
    "howoge.de": "HOWOGE",
    "degewo.de": "degewo",
    "gewobag.de": "Gewobag",
    "stadtundland.de": "STADT UND LAND",
    "gesobau.de": "GESOBAU",
    "berlinovo.de": "berlinovo",
}


# ----------------------------------------------------------------- instellingen

def env_float(name: str, default=None) -> Optional[float]:
    v = (os.environ.get(name) or "").strip()
    if not v or v.lower() in ("none", "null", ""):
        return default
    try:
        return float(v)
    except ValueError:
        log.warning("%s='%s' is geen getal, genegeerd", name, v)
        return default


def env_bool(name: str, default: bool = False) -> bool:
    v = (os.environ.get(name) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "ja", "on")


def env_list(name: str) -> list[str]:
    v = (os.environ.get(name) or "").strip()
    return [x.strip() for x in v.split(",") if x.strip()]


CFG = {
    "token": os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
    "chat_id": os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
    "state_file": os.environ.get("STATE_FILE", "state/seen.txt"),
    "max_kalt": env_float("MAX_KALT", 1400),
    "max_warm": env_float("MAX_WARM", 1400),
    "min_size": env_float("MIN_SIZE_M2", 35),
    "max_size": env_float("MAX_SIZE_M2"),
    "min_rooms": env_float("MIN_ROOMS", 1),
    "max_rooms": env_float("MAX_ROOMS"),
    "allow_wbs": env_bool("ALLOW_WBS", False),
    "districts": env_list("DISTRICTS"),
    "exclude_districts": env_list("EXCLUDE_DISTRICTS"),
    "blocklist": env_list("TITLE_BLOCKLIST"),
    "nk_factor": env_float("UNKNOWN_NK_FACTOR", 1.30),
    "pages": int(env_float("PAGES", 4) or 4),
    "seed_pages": int(env_float("SEED_PAGES", 30) or 30),
    "max_alerts": int(env_float("MAX_ALERTS", 20) or 20),
}


# ----------------------------------------------------------------- parse-helpers

_NUM_RE = re.compile(r"(\d{1,3}(?:\.\d{3})*(?:,\d+)?|\d+(?:,\d+)?)")


def de_number(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    low = text.lower()
    if "unbekannt" in low or "k.a." in low:
        return None
    m = _NUM_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(".", "").replace(",", "."))
    except ValueError:
        return None


def label_value(block_text: str, label: str) -> Optional[str]:
    m = re.search(
        rf"^{re.escape(label)}\s*:?\s*\n?\s*(.+?)$",
        block_text,
        re.MULTILINE | re.IGNORECASE,
    )
    return m.group(1).strip() if m and m.group(1).strip() else None


# ----------------------------------------------------------------- model

class Listing:
    __slots__ = ("url", "title", "address", "district", "zip_code", "rooms",
                 "size_m2", "kalt", "nebenkosten", "warm", "wbs_required",
                 "available_from", "posted_at", "landlord", "image_url", "extras")

    def __init__(self, **kw):
        for s in self.__slots__:
            setattr(self, s, kw.get(s))
        self.extras = kw.get("extras") or []

    @property
    def uid(self) -> str:
        return re.sub(r"\s+", "", self.url)

    @property
    def effective_warm(self) -> Optional[float]:
        if self.warm is not None:
            return self.warm
        if self.kalt is not None and self.nebenkosten is not None:
            return self.kalt + self.nebenkosten
        return None

    def ppm(self) -> Optional[float]:
        if self.kalt and self.size_m2:
            return round(self.kalt / self.size_m2, 2)
        return None


# ----------------------------------------------------------------- scraper

def find_blocks(soup: BeautifulSoup):
    for a in soup.find_all("a", href=True):
        title_attr = (a.get("title") or "").lower()
        text_attr = a.get_text(strip=True).lower()
        if "expose" not in title_attr and "exposé" not in title_attr \
                and "alle details" not in text_attr:
            continue
        node = a
        for _ in range(10):
            node = node.parent
            if node is None:
                break
            text = node.get_text("\n", strip=True)
            if "Kaltmiete" in text and "Adresse" in text:
                links = [
                    x for x in node.find_all("a", href=True)
                    if "expose" in (x.get("title") or "").lower()
                    or "exposé" in (x.get("title") or "").lower()
                    or "alle details" in x.get_text(strip=True).lower()
                ]
                if len(links) == 1:
                    yield node
                break


def parse_block(block) -> Optional[Listing]:
    text = block.get_text("\n", strip=True)

    link = None
    for a in block.find_all("a", href=True):
        t = (a.get("title") or "").lower()
        if "expose" in t or "exposé" in t or "alle details" in a.get_text(strip=True).lower():
            link = urljoin(BASE, a["href"])
            break
    if not link:
        return None

    address = (label_value(text, "Adresse") or "").strip()
    parts = [p.strip() for p in address.split(",") if p.strip()]
    zip_code = next((p for p in parts if re.fullmatch(r"1\d{4}", p)), "")
    district = parts[-1] if parts and not re.fullmatch(r"1\d{4}", parts[-1]) else ""

    wbs_raw = (label_value(text, "WBS") or "").lower()
    wbs_required = None
    if wbs_raw:
        wbs_required = "nicht erforderlich" not in wbs_raw and "erforderlich" in wbs_raw

    title = "Wohnung"
    for tag in ("h2", "h3", "h4", "h5"):
        el = block.find(tag)
        if el:
            t = el.get_text(" ", strip=True)
            if t and len(t) > 3:
                title = t
                break
    else:
        skip = re.compile(
            r"^(Adresse|Zimmeranzahl|Wohnfläche|Kaltmiete|Nebenkosten|Gesamtmiete|"
            r"Bezugsfertig|Eingestellt|WBS|Etage|Badezimmer|Baujahr|Heizung|"
            r"Hauptenergieträger|Energie|Alle Details|Eine Wohnung)", re.IGNORECASE)
        for line in text.split("\n"):
            line = line.strip()
            if len(line) < 8 or skip.match(line):
                continue
            if re.match(r"^[\d,.\s]+(Zimmer|m²|€)", line):
                continue
            title = line[:160]
            break

    image = ""
    img = block.find("img", src=True)
    if img and "flat-dummy" not in img["src"] and not img["src"].endswith(".svg"):
        image = urljoin(BASE, img["src"])

    extras = [c for c in ("Balkon / Loggia / Terrasse", "Garten", "Aufzug", "Keller",
                          "Badewanne", "Dusche", "Barrierefrei", "Gäste WC")
              if c in text]

    return Listing(
        url=link,
        title=title,
        address=address,
        district=district,
        zip_code=zip_code,
        rooms=de_number(label_value(text, "Zimmeranzahl")),
        size_m2=de_number(label_value(text, "Wohnfläche")),
        kalt=de_number(label_value(text, "Kaltmiete")),
        nebenkosten=de_number(label_value(text, "Nebenkosten")),
        warm=de_number(label_value(text, "Gesamtmiete")),
        wbs_required=wbs_required,
        available_from=(label_value(text, "Bezugsfertig ab") or "").strip(),
        posted_at=(label_value(text, "Eingestellt am") or "").strip(),
        landlord=next((n for d, n in LANDLORDS.items() if d in link), ""),
        image_url=image,
        extras=extras,
    )


def scrape(pages: int) -> list[Listing]:
    session = requests.Session()
    session.headers.update({
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.6",
    })

    out: list[Listing] = []
    seen_urls: set[str] = set()

    for page in range(1, pages + 1):
        url = FINDER if page == 1 else f"{FINDER}?page={page}"
        try:
            r = session.get(url, timeout=30)
        except requests.RequestException as e:
            log.error("Netwerkfout op pagina %d: %s", page, e)
            break
        if not r.ok:
            log.error("HTTP %s op pagina %d", r.status_code, page)
            break

        blocks = list(find_blocks(BeautifulSoup(r.text, "html.parser")))
        if not blocks:
            if page == 1:
                log.error(
                    "Geen aanbiedingen gevonden op pagina 1. "
                    "De HTML-structuur van inberlinwohnen.de is waarschijnlijk gewijzigd."
                )
            break

        added = 0
        for b in blocks:
            li = parse_block(b)
            if li and li.uid not in seen_urls:
                seen_urls.add(li.uid)
                out.append(li)
                added += 1

        log.info("Pagina %d: %d aanbiedingen", page, added)
        if added == 0:
            break
        if page < pages:
            time.sleep(1.2)

    return out


# ----------------------------------------------------------------- filter

def matches(li: Listing) -> tuple[bool, str]:
    c = CFG
    if c["max_kalt"] is not None and li.kalt is not None and li.kalt > c["max_kalt"]:
        return False, f"kalt {li.kalt:.0f} > {c['max_kalt']:.0f}"

    if c["max_warm"] is not None:
        warm = li.effective_warm
        if warm is None and li.kalt is not None:
            warm = li.kalt * c["nk_factor"]
        if warm is not None and warm > c["max_warm"]:
            return False, f"warm {warm:.0f} > {c['max_warm']:.0f}"

    if c["min_rooms"] is not None and li.rooms is not None and li.rooms < c["min_rooms"]:
        return False, f"kamers {li.rooms:g}"
    if c["max_rooms"] is not None and li.rooms is not None and li.rooms > c["max_rooms"]:
        return False, f"kamers {li.rooms:g}"
    if c["min_size"] is not None and li.size_m2 is not None and li.size_m2 < c["min_size"]:
        return False, f"opp {li.size_m2:g}"
    if c["max_size"] is not None and li.size_m2 is not None and li.size_m2 > c["max_size"]:
        return False, f"opp {li.size_m2:g}"

    if not c["allow_wbs"] and li.wbs_required is True:
        return False, "WBS vereist"

    hay = f"{li.district} {li.address}".lower()
    if c["districts"] and not any(d.lower() in hay for d in c["districts"]):
        return False, f"district {li.district}"
    for d in c["exclude_districts"]:
        if d.lower() in hay:
            return False, f"district {d} uitgesloten"

    tl = (li.title or "").lower()
    for bad in c["blocklist"]:
        if bad.lower() in tl:
            return False, f"titel '{bad}'"

    return True, ""


# ----------------------------------------------------------------- telegram

def tg_call(method: str, payload: dict) -> bool:
    if not CFG["token"] or not CFG["chat_id"]:
        log.warning("Telegram niet geconfigureerd.")
        return False
    url = f"https://api.telegram.org/bot{CFG['token']}/{method}"
    for attempt in range(3):
        try:
            r = requests.post(url, json=payload, timeout=25)
            if r.status_code == 429:
                wait = int(r.json().get("parameters", {}).get("retry_after", 5))
                time.sleep(wait + 1)
                continue
            if r.ok:
                return True
            log.error("Telegram %s: %s %s", method, r.status_code, r.text[:200])
            return False
        except requests.RequestException as e:
            log.warning("Telegram netwerkfout: %s", e)
            time.sleep(2 * (attempt + 1))
    return False


def format_listing(li: Listing) -> str:
    e = html.escape
    lines = [f"🏠 <b>{e(li.title or 'Wohnung')}</b>"]
    if li.address:
        lines.append(f"📍 {e(li.address)}")

    facts = []
    if li.rooms is not None:
        facts.append(f"{li.rooms:g} Zi.")
    if li.size_m2 is not None:
        facts.append(f"{li.size_m2:g} m²")
    if li.ppm():
        facts.append(f"{li.ppm():.2f} €/m²")
    if facts:
        lines.append("📐 " + " · ".join(facts))

    prices = []
    if li.kalt is not None:
        prices.append(f"kalt <b>{li.kalt:,.0f} €</b>".replace(",", "."))
    if li.effective_warm is not None:
        prices.append(f"warm <b>{li.effective_warm:,.0f} €</b>".replace(",", "."))
    if prices:
        lines.append("💶 " + " | ".join(prices))

    if li.wbs_required is True:
        lines.append("⚠️ <b>WBS vereist</b>")
    if li.available_from:
        lines.append(f"🗓 Vanaf {e(li.available_from)}")
    if li.landlord:
        lines.append(f"🏢 {e(li.landlord)}")
    if li.extras:
        lines.append("✨ " + e(", ".join(li.extras[:6])))

    lines.append(f"\n🔗 {e(li.url)}")
    return "\n".join(lines)


def send_listing(li: Listing) -> bool:
    text = format_listing(li)
    if li.image_url:
        if tg_call("sendPhoto", {"chat_id": CFG["chat_id"], "photo": li.image_url,
                                 "caption": text, "parse_mode": "HTML"}):
            return True
    return tg_call("sendMessage", {"chat_id": CFG["chat_id"], "text": text,
                                   "parse_mode": "HTML"})


# ----------------------------------------------------------------- state

def load_state(path: str) -> set[str]:
    p = Path(path)
    if not p.exists():
        return set()
    return {l.strip() for l in p.read_text(encoding="utf-8").splitlines() if l.strip()}


def save_state(path: str, uids: set[str]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(sorted(uids)) + "\n", encoding="utf-8")


# ----------------------------------------------------------------- main

def main() -> int:
    dry = "--dry-run" in sys.argv

    seen = load_state(CFG["state_file"])
    first_run = not seen
    if first_run:
        log.warning("Geen status gevonden - EERSTE RONDE, geen meldingen per woning.")

    pages = CFG["seed_pages"] if first_run else CFG["pages"]
    listings = scrape(pages)

    if not listings:
        log.error("Niets opgehaald. Zie melding hierboven.")
        return 1

    log.info("%d aanbiedingen opgehaald, %d al bekend.", len(listings), len(seen))

    hits, new_total = [], 0
    for li in listings:
        if li.uid in seen:
            continue
        new_total += 1
        seen.add(li.uid)
        ok, reason = matches(li)
        if ok:
            hits.append(li)
        else:
            log.debug("weg: %s (%s)", li.title[:50], reason)

    log.info("%d nieuw, %d voldoen aan je filters.", new_total, len(hits))

    if first_run:
        passing = sum(1 for x in listings if matches(x)[0])
        if not dry:
            tg_call("sendMessage", {
                "chat_id": CFG["chat_id"],
                "parse_mode": "HTML",
                "text": (f"✅ <b>Berlin Rent Radar staat aan</b>\n"
                         f"{len(listings)} bestaande aanbiedingen ingelezen, "
                         f"waarvan {passing} binnen je criteria.\n"
                         f"Vanaf nu krijg je alleen nieuwe treffers."),
            })
    else:
        for li in hits[:CFG["max_alerts"]]:
            log.info("TREFFER: %s | %s | kalt %s",
                     li.title[:50], li.district,
                     f"{li.kalt:.0f}" if li.kalt else "?")
            if dry:
                print(format_listing(li), "\n")
            else:
                send_listing(li)
                time.sleep(0.5)
        if len(hits) > CFG["max_alerts"]:
            log.warning("%d treffers niet verstuurd (limiet).",
                        len(hits) - CFG["max_alerts"])

    if not dry:
        save_state(CFG["state_file"], seen)
        log.info("Status opgeslagen: %d aanbiedingen.", len(seen))
    return 0


if __name__ == "__main__":
    sys.exit(main())
