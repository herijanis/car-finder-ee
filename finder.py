"""Search Estonian car sites for listings that match the searches in searches.json
(edited from the phone app in docs/) and post new matches to the family Telegram group."""

import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from html import escape as html_escape
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
SEARCHES_FILE = ROOT / "searches.json"
# The home PC reads the searches straight from GitHub so the app's changes reach it without a git pull.
SEARCHES_URL = "https://api.github.com/repos/herijanis/car-finder-ee/contents/searches.json"
# GitHub runs one set of sites and the home PC another (some sites block GitHub),
# so each place keeps its own memory of what it has already seen.
SEEN_FILE = ROOT / os.environ.get("SEEN_FILE", "seen.json")
DRY_RUN = os.environ.get("DRY_RUN") == "true"


def setting(name, filename):
    """A secret from the environment (GitHub) or a git-ignored file next to this script (home PC)."""
    path = ROOT / filename
    return os.environ.get(name) or (path.read_text().strip() if path.exists() else "")


TELEGRAM_TOKEN = setting("TELEGRAM_TOKEN", "telegram_token.txt")
TELEGRAM_CHAT = "" if DRY_RUN else setting("TELEGRAM_CHAT", "telegram_chat.txt")
MAX_ALERTS_PER_RUN = 15    # Telegram allows ~20 messages a minute in a group; the rest wait for the next run
DETAIL_FETCH_LIMIT = 400   # per source per run, keeps the first run polite
RECHECK_DAYS = 30          # known listings this recent are re-checked when a search is added or changed
FORGET_DAYS = 120
DELAY = 1.0                # seconds between requests to the same site
TALLINN = ZoneInfo("Europe/Tallinn")

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/140.0 Safari/537.36",
    "Accept-Language": "et-EE,et;q=0.9,en;q=0.8",
})


def get(url, **kw):
    # Sites occasionally drop a connection; retry a couple of times before giving up on the run.
    for attempt in range(3):
        time.sleep(DELAY * (1 + 5 * attempt))
        try:
            r = session.get(url, timeout=30, **kw)
            r.raise_for_status()
            return r
        except (requests.ConnectionError, requests.Timeout):
            if attempt == 2:
                raise


# ---------------------------------------------------------------------------
# Searches (from the app)
# ---------------------------------------------------------------------------
def load_searches():
    """Active searches. GitHub reads its checkout; the PC asks GitHub for the latest version."""
    data = None
    if os.environ.get("SEARCHES_FROM_GITHUB") == "true":
        try:
            r = requests.get(SEARCHES_URL, timeout=20, headers={"Accept": "application/vnd.github.raw+json"})
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            print(f"could not fetch searches from GitHub, using local copy: {e}")
    if data is None:
        data = json.loads(SEARCHES_FILE.read_text(encoding="utf-8"))
    return [s for s in data.get("searches", []) if s.get("active", True) and s.get("models")]


def slugify(text):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


MAKE_ALIASES = {
    "volkswagen": ["volkswagen", "vw"],
    "mercedes-benz": ["mercedes", "benz", "mb"],
    "skoda": ["skoda", "škoda"],
    "citroen": ["citroen", "citroën"],
    "bmw": ["bmw"],
}


def names_model(text, make, model):
    """Does the listing text name this make/model? Short model names (A4, 320, C) also need the make."""
    t = " " + re.sub(r"[-_/]", " ", (text or "").lower()) + " "
    m = re.sub(r"[-_/]", " ", model.lower()).strip()
    if not m or not re.search(rf"(?<![a-z0-9]){re.escape(m)}(?![a-z0-9])", t):
        return False
    if len(m.replace(" ", "")) >= 4:
        return True
    aliases = MAKE_ALIASES.get(slugify(make), [make.lower()])
    return any(re.search(rf"(?<![a-z0-9]){re.escape(a)}(?![a-z0-9])", t) for a in aliases)


def search_terms(searches):
    """What to type into a site's search box: just the model when it is distinctive, else make + model."""
    terms = []
    for s in searches:
        for mm in s["models"]:
            model = mm["model"].strip()
            term = model if len(model.replace(" ", "")) >= 4 else f"{mm['make']} {model}"
            if term.lower() not in (t.lower() for t in terms):
                terms.append(term)
    return terms


# ---------------------------------------------------------------------------
# Normalising free text into fields
# ---------------------------------------------------------------------------
YEAR_RE = re.compile(r"\b(19[5-9]\d|20[0-3]\d)\b")
KW_RE = re.compile(r"(\d{2,3})\s*kw\b", re.I)
KM_RE = re.compile(r"(\d{1,3}(?:[\s. ]?\d{3})+|\d{3,7})\s*(tkm|tuh\.?\s*km|km)\b", re.I)
# Sellers often write mileage without "km": "Kella peal 391000", "läbisõit 250t", "odomeeter 180 tuh".
KM_WORDS_RE = re.compile(
    r"(?:läbisõit|labisoit|kella\s+peal|kellal|näit|odomeeter|sõitnud|mileage)\D{0,15}?"
    r"(\d{1,3}(?:[\s. ]?\d{3})*)\s*(tkm|tuh|t\b|k\b)?", re.I)


def norm_fuel(text):
    t = (text or "").lower()
    if re.search(r"hübriid|hybrid", t):
        return "hybrid"
    if re.search(r"diis|diesel|\btdi\b|\bsdi\b|\btd\b|\bcdi\b|\bhdi\b", t):
        return "diesel"
    if re.search(r"elekter|electric|\bev\b", t):
        return "electric"
    if "gaas" in t or "lpg" in t or "cng" in t:
        return "gas"
    if re.search(r"bensiin|petrol|\btsi\b|\bfsi\b|\bturbo\b", t):
        return "petrol"
    return None


def norm_gearbox(text):
    t = (text or "").lower()
    if re.search(r"manuaal|manual|käsi", t):
        return "manual"
    if re.search(r"automaat|automatic|tiptronic|multitronic|dsg|poolautomaat", t):
        return "automatic"
    return None


def norm_body(text):
    t = (text or "").lower()
    # "mahtuniversaal" contains "universaal", so the minivan check comes first
    for body, pattern in [("minivan", r"mahtuniversaal|minivan|kaubik"),
                          ("wagon", r"universaal|avant|variant|kombi|wagon|touring|estate"),
                          ("sedan", r"sedaan|sedan"),
                          ("hatchback", r"luukpära|hatchback|sportback"),
                          ("suv", r"maastur|\bsuv\b|linnamaastur"),
                          ("coupe", r"kupee|coupe"),
                          ("cabrio", r"kabriolett|cabrio|roadster")]:
        if re.search(pattern, t):
            return body
    return None


def parse_km(text):
    text = text or ""
    if m := KM_RE.search(text):
        n = int(re.sub(r"\D", "", m.group(1)))
        return n * 1000 if m.group(2).lower().startswith(("tkm", "tuh")) else n
    if m := KM_WORDS_RE.search(text):
        n = int(re.sub(r"\D", "", m.group(1)))
        # "391" next to a mileage word, or "391t"/"391k", means thousands
        return n * 1000 if (m.group(2) or n < 1000) else n
    return None


def parse_int(text):
    digits = re.sub(r"\D", "", str(text or ""))
    return int(digits) if digits else None


def first_year(text):
    """First plausible model year; a future year is an inspection date or similar, not the car's age."""
    this_year = datetime.now().year
    return next((int(y) for y in YEAR_RE.findall(text or "") if int(y) <= this_year), None)


# In free text a year only counts as the car's year next to a word that says so
# ("2003 a.", "aasta 2003", "Esmaregistreerimine: 05.2002").
YEAR_IN_TEXT_RE = re.compile(
    r"(?:aasta|mudeliaasta|esmareg\w*|registreeri\w*|reg\.?)\D{0,12}(?:\d{1,2}[./])?(19[5-9]\d|20[0-3]\d)"
    r"|(19[5-9]\d|20[0-3]\d)\s*(?:a\.|aasta|\.a\b)", re.I)


def year_in_text(text):
    m = YEAR_IN_TEXT_RE.search(text or "")
    return first_year(m.group(1) or m.group(2)) if m else None


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------
RULES = [  # (car field, check, search keys it needs)
    ("year", lambda v, s: (s.get("year_min") or 0) <= v <= (s.get("year_max") or 9999), ("year_min", "year_max")),
    ("price", lambda v, s: (s.get("price_min") or 0) <= v <= (s.get("price_max") or 10**9), ("price_min", "price_max")),
    ("km", lambda v, s: v <= s["km_max"], ("km_max",)),
    ("kw", lambda v, s: (s.get("kw_min") or 0) <= v <= (s.get("kw_max") or 10**4), ("kw_min", "kw_max")),
    ("fuel", lambda v, s: v == s["fuel"], ("fuel",)),
    ("gearbox", lambda v, s: v == s["gearbox"], ("gearbox",)),
    ("body", lambda v, s: v == s["body"], ("body",)),
]


def check(car, search):
    """Return (is_match, fields the ad doesn't state). A stated value that fails any rule rejects the car."""
    if not any(names_model(car.get("name"), mm["make"], mm["model"]) for mm in search["models"]):
        return False, []
    unknown = []
    for field, ok, keys in RULES:
        if not any(search.get(k) not in (None, "", "any") for k in keys):
            continue  # this search doesn't care about the field
        v = car.get(field)
        if v is None:
            unknown.append(field)
        elif not ok(v, search):
            return False, []
    # Year is essential: without it almost any matching model would slip through.
    if "year" in unknown:
        return False, []
    return True, unknown


# ---------------------------------------------------------------------------
# Sources: each returns cars it hasn't seen before, as plain dicts
# ---------------------------------------------------------------------------
def okidoki(seen, searches):
    base = "https://www.okidoki.ee"
    ids = {}
    for term in search_terms(searches):
        html = get(f"{base}/buy/1601/", params={"query": term, "pp": 200}).text
        for href, item_id in re.findall(r'href="(/item/[^"/]+/(\d+)/)"', html):
            ids[item_id] = base + href
    cars = []
    for item_id, url in ids.items():
        key = f"okidoki:{item_id}"
        if key in seen:
            continue
        if len(cars) >= DETAIL_FETCH_LIMIT:
            break
        try:
            soup = BeautifulSoup(get(url).text, "html.parser")
        except requests.RequestException as e:
            print(f"  okidoki {item_id}: {e}")
            continue
        specs = {}
        for name in soup.select(".item-specific__name"):
            data = name.find_next(class_="item-specific__data")
            if data:
                specs.setdefault(name.get_text(" ", strip=True).lower(), data.get_text(" ", strip=True))
        title = (soup.find("h1") or soup.find("title")).get_text(" ", strip=True)
        desc_el = soup.select_one(".item-description-text")
        desc = desc_el.get_text(" ", strip=True) if desc_el else ""
        blob = " ".join([title] + [f"{k} {v}" for k, v in specs.items()])
        price_el = soup.select_one("[class*=price]")
        cars.append({
            "key": key, "source": "okidoki", "url": url, "title": title,
            "name": f"{specs.get('mark ja mudel', '')} {title}",
            "year": first_year(specs.get("registreerimine")),
            "fuel": norm_fuel(specs.get("kütus")),
            "gearbox": norm_gearbox(specs.get("käigukast")),
            "body": norm_body(specs.get("keretüüp")),
            "kw": int(m.group(1)) if (m := KW_RE.search(f"{blob} {desc}")) else None,
            "km": parse_int(specs.get("läbisõit")) or parse_km(f"{title} {desc}"),
            "price": parse_int(price_el.get_text()) if price_el else None,
            "location": specs.get("asukoht"),
        })
    return cars


def soov(seen, searches):
    ids = {}
    for term in search_terms(searches):
        html = get(f"https://soov.ee/577-autod/keyword-{slugify(term)}/listings.html").text
        for url, item_id in re.findall(r'href="(https://soov\.ee/(\d+)-[^"]*/details\.html)"', html):
            ids[item_id] = url
    cars = []
    for item_id, url in ids.items():
        key = f"soov:{item_id}"
        if key in seen:
            continue
        if len(cars) >= DETAIL_FETCH_LIMIT:
            break
        try:
            soup = BeautifulSoup(get(url).text, "html.parser")
        except requests.RequestException as e:
            print(f"  soov {item_id}: {e}")
            continue
        title = (soup.find("h1") or soup.find("title")).get_text(" ", strip=True)
        desc_el = soup.find("meta", attrs={"name": "description"})
        desc = desc_el["content"] if desc_el else ""
        text = f"{title}\n{desc}"
        price = parse_int(m.group(1)) if (m := re.search(r"(\d[\d\s]{1,7})\s*(€|eur)", text, re.I)) else None
        cars.append({
            "key": key, "source": "soov", "url": url, "title": title, "name": text,
            "year": first_year(title) or first_year(desc),
            "fuel": norm_fuel(text),
            "gearbox": norm_gearbox(text),
            "body": norm_body(text),
            "kw": int(m.group(1)) if (m := KW_RE.search(text)) else None,
            "km": parse_km(text),
            "price": price,
            "location": None,
        })
    return cars


AUTODIILER_FUEL = {"diesel": "diesel", "petrol": "petrol", "electric": "electric",
                   "hybrid-petrol": "hybrid", "hybrid-diesel": "hybrid", "lpg": "gas", "cng": "gas"}


def autodiiler(seen, searches):
    """No server-side filters, so read every listing; the API is cheap and complete."""
    api = "https://garage.autodiiler.ee/api/v1/vehicles"
    cars, page, pages = [], 1, 1
    while page <= pages:
        data = get(api, params={"page": page, "per_page": 48}, headers={"Accept": "application/json"}).json()
        pages = data["meta"]["pagination"]["total_pages"]
        for v in data["data"]:
            key = f"autodiiler:{v['id']}"
            if key in seen:
                continue
            brand = (v.get("brand") or {}).get("name", "")
            model = (v.get("brandModel") or {}).get("name", "")
            cars.append({
                "key": key, "source": "autodiiler", "url": f"https://autodiiler.ee/et/vehicles/{v['id']}",
                "title": v.get("name", ""), "name": f"{brand} {model} {v.get('name', '')}",
                "year": first_year((v.get("seo") or {}).get("description")),
                "fuel": AUTODIILER_FUEL.get((v.get("fuel_type") or {}).get("value"), "other"),
                "gearbox": norm_gearbox((v.get("transmission_type") or {}).get("value")),
                "body": norm_body((v.get("vehicleBodyType") or {}).get("name")),
                "kw": v.get("engine_power"),
                "km": v.get("mileage"),
                "price": v.get("price"),
                "location": v.get("location_name"),
            })
        page += 1
    return cars


def autoportaal(seen, searches):
    """Model pages list newest first, 20 per page; read pages until one has nothing new."""
    base = "https://autoportaal.ee/et"
    slugs = list(dict.fromkeys(slugify(f"{mm['make']} {mm['model']}") for s in searches for mm in s["models"]))
    ids = {}
    for slug in slugs:
        for page in range(1, 11):
            try:
                html = get(f"{base}/{slug}", params={"page": page}).text
            except requests.HTTPError:
                print(f"  autoportaal has no page for {slug}")
                break
            fresh = [i for i in dict.fromkeys(re.findall(r"kasutatud-autod/(\d+)", html))
                     if f"autoportaal:{i}" not in seen]
            ids.update((i, f"{base}/kasutatud-autod/{i}") for i in fresh)
            if not fresh:
                break
    cars = []
    for item_id, url in ids.items():
        if len(cars) >= DETAIL_FETCH_LIMIT:
            break
        try:
            soup = BeautifulSoup(get(url).text, "html.parser")
        except requests.RequestException as e:
            print(f"  autoportaal {item_id}: {e}")
            continue
        # Specs are rendered as a label line followed by its value line.
        lines = [s.strip() for s in soup.get_text("\n").split("\n") if s.strip()]
        spec = {}
        for label, value in zip(lines, lines[1:]):
            spec.setdefault(label.rstrip(":").lower(), value)
        title_el = soup.select_one("#vehicleTitle") or soup.find("h1")
        title = " ".join(title_el.get_text(" ", strip=True).split()) if title_el else ""
        desc = " ".join(lines)
        # The price sits right after a model-years line like "2014-2015", so don't let digits run together.
        price = None
        if m := re.search(r"(?<![\d-])(\d{1,3}(?:[  ]\d{3})+|\d{1,6})\s*€", desc[desc.find(title[:20]):]):
            price = parse_int(m.group(1))
        cars.append({
            "key": f"autoportaal:{item_id}", "source": "autoportaal", "url": url, "title": title, "name": title,
            "year": first_year(spec.get("esmareg.")),
            "fuel": norm_fuel(spec.get("kütus")),
            "gearbox": norm_gearbox(spec.get("käigukast")),
            "body": norm_body(spec.get("keretüüp")),
            "kw": parse_int(spec.get("võimsus")),
            "km": parse_int(spec.get("läbisõit")),
            "price": price,
            "location": spec.get("asukoht"),
        })
    return cars


FB_PROFILE = ROOT / "fb-profile"   # the spare account's login, made with fb_login.py; never leaves the PC
FB_ITEM_LIMIT = 10                 # ad pages opened per run: Facebook watches for automated use
FB_ITEM_RE = re.compile(r'\{"__typename":"GroupCommerceProductItem","id":"(\d+)"')


def fb_field(chunk, pattern):
    m = re.search(pattern, chunk)
    return json.loads(f'"{m.group(1)}"') if m else None


def facebook(seen, searches):
    """Marketplace around Tallinn, read as the spare account. Hourly only, and few ad pages per run."""
    if datetime.now().minute >= 30 and os.environ.get("RUN_ALL_SOURCES") != "true":
        print("  facebook: only on the first half of the hour")
        return []
    from playwright.sync_api import sync_playwright  # only the home PC has a browser installed

    cars = []
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(FB_PROFILE, headless=True, locale="et-EE")
        try:
            page = ctx.new_page()
            listings = {}
            for term in search_terms(searches):
                page.goto(f"https://www.facebook.com/marketplace/tallinn/search/?query={requests.utils.quote(term)}",
                          wait_until="domcontentloaded")
                page.wait_for_timeout(4000)
                if not any(c["name"] == "c_user" for c in ctx.cookies()):
                    raise RuntimeError("Facebook login has expired: run fb_login.py again")
                for _ in range(2):  # a couple of scrolls loads ~50 newest results
                    page.mouse.wheel(0, 4000)
                    page.wait_for_timeout(2000)
                html = page.content()
                starts = [(m.start(), m.group(1)) for m in FB_ITEM_RE.finditer(html)]
                for (pos, item_id), nxt in zip(starts, starts[1:] + [(len(html), None)]):
                    chunk = html[pos:nxt[0]]
                    title = fb_field(chunk, r'"marketplace_listing_title":"((?:[^"\\]|\\.)*)"')
                    if title and f"facebook:{item_id}" not in seen:
                        listings[item_id] = (title, chunk)
            # Only open ads whose title names a wanted model; the rest are remembered and skipped.
            opened = 0
            for item_id, (title, chunk) in listings.items():
                key = f"facebook:{item_id}"
                if not any(names_model(title, mm["make"], mm["model"]) for s in searches for mm in s["models"]):
                    seen[key] = {"first_seen": datetime.now(timezone.utc).isoformat(), "alerted": []}
                    continue
                if opened >= FB_ITEM_LIMIT:
                    continue  # picked up next run
                opened += 1
                url = f"https://www.facebook.com/marketplace/item/{item_id}/"
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_timeout(3000)
                main = page.query_selector('[role="main"]')
                text = main.inner_text() if main else ""
                # The seller's own text sits between these markers; below it are other people's ads.
                desc = text.split("Seller's description", 1)[-1].split("Location is approximate", 1)[0]
                subtitle = fb_field(chunk, r'"subtitle":"((?:[^"\\]|\\.)*)"') or ""
                km = (int(float(m.group(1).replace(",", ".")) * 1000)
                      if (m := re.search(r"(\d+(?:[.,]\d+)?)\s*K\s*km", subtitle, re.I)) else parse_km(f"{subtitle} {desc}"))
                blob = f"{title}\n{desc}"
                cars.append({
                    "key": key, "source": "facebook", "url": url, "title": title, "name": blob,
                    "year": first_year(title) or year_in_text(desc),
                    "fuel": norm_fuel(blob),
                    "gearbox": norm_gearbox(blob),
                    "body": norm_body(blob),
                    "kw": int(m.group(1)) if (m := KW_RE.search(blob)) else None,
                    "km": km,
                    "price": parse_int((fb_field(chunk, r'"amount":"([\d.]+)"') or "").split(".")[0]),
                    "location": fb_field(chunk, r'"city":"((?:[^"\\]|\\.)*)"'),
                })
                time.sleep(2)
        finally:
            ctx.close()
    return cars


SOURCES = {"okidoki": okidoki, "soov": soov, "autodiiler": autodiiler, "autoportaal": autoportaal,
           "facebook": facebook}
if os.environ.get("SOURCES"):
    SOURCES = {name: SOURCES[name] for name in os.environ["SOURCES"].split(",")}


def in_search_hours(t):
    """Search 08:00-23:00 Estonian time, so alerts never arrive at night."""
    return 8 <= t.hour < 23 or (t.hour == 23 and t.minute < 30)


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
ET = {"diesel": "diisel", "petrol": "bensiin", "hybrid": "hübriid", "electric": "elekter", "gas": "gaas",
      "manual": "manuaal", "automatic": "automaat", "wagon": "universaal", "sedan": "sedaan",
      "hatchback": "luukpära", "suv": "maastur", "coupe": "kupee", "cabrio": "kabriolett",
      "minivan": "mahtuniversaal", "year": "aasta", "price": "hind", "km": "läbisõit", "kw": "võimsus",
      "fuel": "kütus", "gearbox": "käigukast", "body": "keretüüp"}


def num(n):
    return f"{n:,}".replace(",", " ")


def alert_text(car, hits):
    """hits: [(search, unknown_fields)]"""
    e = html_escape
    names = ", ".join(s["name"] for s, _ in hits)
    facts = [f"📅 {car['year']}" if car.get("year") else None,
             f"🛣 {num(car['km'])} km" if car.get("km") else None,
             f"⚡ {car['kw']} kW" if car.get("kw") else None,
             f"💶 <b>{num(car['price'])} €</b>" if car.get("price") else None]
    kind = [ET.get(car.get(f)) for f in ("fuel", "gearbox", "body")]
    unknown = sorted({f for _, u in hits for f in u})
    lines = [f"🚨 <b>{e(names)}</b>", f"<b>{e(car['title'])}</b>",
             " · ".join(x for x in facts if x),
             " · ".join(x for x in kind if x),
             f"📍 {e(car['location'])} · {car['source']}" if car.get("location") else f"📍 {car['source']}"]
    if unknown:
        lines.append(f"⚠️ Kuulutuses pole märgitud: {', '.join(ET[f] for f in unknown)}")
    lines.append(f'\n<a href="{e(car["url"])}">👉 Ava kuulutus</a>')
    return "\n".join(line for line in lines if line)


def send_telegram(html):
    """Post to the family Telegram group. Returns quietly if Telegram isn't set up."""
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT):
        print("  (Telegram not set up, not sending)")
        return
    r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", timeout=20, data={
        "chat_id": TELEGRAM_CHAT, "text": html, "parse_mode": "HTML"})
    r.raise_for_status()


def tell_once_a_day(topic, html):
    """A warning for the group that must not repeat every 30 minutes."""
    marker = ROOT / f".told-{topic}"
    today = datetime.now(TALLINN).date().isoformat()
    if marker.exists() and marker.read_text() == today:
        return
    try:
        send_telegram(html)
        marker.write_text(today)
    except requests.RequestException:
        pass


# ---------------------------------------------------------------------------
def main():
    now = datetime.now(timezone.utc)
    local = now.astimezone(TALLINN)
    all_day = os.environ.get("SEARCH_ALL_DAY") == "true" or os.environ.get("RUN_ALL_SOURCES") == "true"
    if not all_day and not in_search_hours(local):
        print(f"{local:%H:%M} in Estonia: outside 08:00-23:00, not searching")
        return 0
    searches = load_searches()
    print(f"{len(searches)} active searches: {', '.join(s['name'] for s in searches)}")
    seen = json.loads(SEEN_FILE.read_text(encoding="utf-8")) if SEEN_FILE.exists() else {}

    failures = []
    if searches:
        for name, fn in SOURCES.items():
            print(f"{name}: searching")
            try:
                cars = fn(seen, searches)
            except Exception as e:  # one broken site must not stop the others
                print(f"{name}: FAILED {e!r}")
                failures.append(name)
                if name == "facebook" and "login" in str(e):
                    tell_once_a_day("fb_login", "⚠️ Facebooki sisselogimine aegus. Marketplace'i ei otsita, "
                                    "kuni teine konto uuesti sisse logib (fb_login.py arvutis).")
                continue
            for car in cars:
                seen[car.pop("key")] = {"first_seen": now.isoformat(), "car": car, "alerted": []}
            print(f"{name}: {len(cars)} new listings")

    # Check every recent listing against every search, so a search added in the app
    # also finds cars that were already listed. Each car is sent once per search.
    recent = (now - timedelta(days=RECHECK_DAYS)).isoformat()
    pending = []
    for key, entry in seen.items():
        if "car" not in entry or entry["first_seen"] < recent:
            continue
        hits = [(s, unknown) for s in searches if s["id"] not in entry["alerted"]
                for ok, unknown in [check(entry["car"], s)] if ok]
        if hits:
            pending.append((entry, hits))
    pending.sort(key=lambda p: p[0]["first_seen"], reverse=True)

    sent = 0
    for entry, hits in pending[:MAX_ALERTS_PER_RUN]:
        print(f"MATCH {entry['car']['url']} for {[s['name'] for s, _ in hits]}")
        try:
            send_telegram(alert_text(entry["car"], hits))
        except requests.RequestException as e:
            print(f"  telegram failed, will retry next run: {e}")
            continue
        if not DRY_RUN:
            entry["alerted"] += [s["id"] for s, _ in hits]
        sent += 1
        time.sleep(1)
    if len(pending) > MAX_ALERTS_PER_RUN:
        print(f"{len(pending) - MAX_ALERTS_PER_RUN} more matches wait for the next run")
    if searches and len(failures) == len(SOURCES) and local.hour == 9 and local.minute < 30:
        try:
            send_telegram(f"⚠️ Autootsing ei saanud täna hommikul ühtegi saiti lugeda: {', '.join(failures)}")
        except requests.RequestException:
            pass

    forget = (now - timedelta(days=FORGET_DAYS)).isoformat()
    seen = {k: v for k, v in seen.items() if v["first_seen"] >= forget}
    SEEN_FILE.write_text(json.dumps(seen, ensure_ascii=False, indent=0, sort_keys=True), encoding="utf-8")
    print(f"sent {sent} alerts")
    return 1 if searches and len(failures) == len(SOURCES) else 0


if __name__ == "__main__":
    sys.exit(main())
