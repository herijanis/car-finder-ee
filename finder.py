"""Search Estonian car sites for listings that match the searches in searches.json
(edited from the phone app in docs/) and post new matches to the family Telegram group."""

import base64
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from html import escape as html_escape, unescape as html_unescape
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
# The app saves searches on the Hetzner server (api.py). The server reads its file directly;
# the home PC asks the server's API (SEARCHES_URL) and falls back to its own last copy.
SEARCHES_FILE = ROOT / os.environ.get("SEARCHES_FILE", "searches.json")
SEARCHES_URL = os.environ.get("SEARCHES_URL")
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
    """Active searches, from the server's API when SEARCHES_URL is set, else from SEARCHES_FILE."""
    data = None
    if SEARCHES_URL:
        try:
            r = requests.get(SEARCHES_URL, timeout=20)
            r.raise_for_status()
            data = r.json()
            SEARCHES_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except (requests.RequestException, ValueError) as e:
            print(f"could not fetch searches, using the last copy: {e}")
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


def og_image(html):
    """The page's share picture (og:image), which sites set to the car's main photo."""
    m = (re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', html)
         or re.search(r'<meta[^>]+content="([^"]+)"[^>]+property="og:image"', html))
    return html_unescape(m.group(1)) if m else None


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
    # one body type (older searches) or a list of them ticked in the app
    ("body", lambda v, s: v in (s["body"] if isinstance(s["body"], list) else [s["body"]]), ("body",)),
]


def check(car, search):
    """Return (is_match, fields the ad doesn't state). A stated value that fails any rule rejects the car,
    and so does a missing one the search marks as required ("require" in the app)."""
    if not any(names_model(car.get("name"), mm["make"], mm["model"]) for mm in search["models"]):
        return False, []
    unknown = []
    for field, ok, keys in RULES:
        if not any(search.get(k) not in (None, "", "any", []) for k in keys):
            continue  # this search doesn't care about the field
        v = car.get(field)
        if v is None:
            unknown.append(field)
        elif not ok(v, search):
            return False, []
    # Year is essential: without it almost any matching model would slip through.
    if "year" in unknown or set(unknown) & set(search.get("require") or []):
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
            html = get(url).text
        except requests.RequestException as e:
            print(f"  okidoki {item_id}: {e}")
            continue
        soup = BeautifulSoup(html, "html.parser")
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
            "image": m.group(0) if (m := re.search(rf"https://images\.okidoki\.st/{item_id}/\d+_2\.jpg", html)) else None,
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
            html = get(url).text
        except requests.RequestException as e:
            print(f"  soov {item_id}: {e}")
            continue
        soup = BeautifulSoup(html, "html.parser")
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
            "image": og_image(html),
        })
    return cars


AUTODIILER_FUEL = {"diesel": "diesel", "petrol": "petrol", "electric": "electric",
                   "hybrid-petrol": "hybrid", "hybrid-diesel": "hybrid", "lpg": "gas", "cng": "gas"}


def autodiiler(seen, searches):
    """No server-side filters, so read every listing; the API is cheap and complete.
    Returns all listings, seen or not, so price changes are noticed."""
    api = "https://garage.autodiiler.ee/api/v1/vehicles"
    cars, page, pages = [], 1, 1
    while page <= pages:
        data = get(api, params={"page": page, "per_page": 48}, headers={"Accept": "application/json"}).json()
        pages = data["meta"]["pagination"]["total_pages"]
        for v in data["data"]:
            key = f"autodiiler:{v['id']}"
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
                "image": (v.get("seo") or {}).get("image"),
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
            html = get(url).text
        except requests.RequestException as e:
            print(f"  autoportaal {item_id}: {e}")
            continue
        soup = BeautifulSoup(html, "html.parser")
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
            "image": og_image(html),
        })
    return cars


def wanted_makes(searches):
    return list(dict.fromkeys(mm["make"] for s in searches for mm in s["models"]))


def veego(seen, searches):
    """Open JSON API, mostly dealer stock (Moller, Amserv, Inchcape...). Filtered by make server-side.
    Returns all listings, seen or not, so price changes are noticed."""
    api = "https://api.veego.ee/api"
    names = lambda path: {a["id"]: a["name"] for a in get(f"{api}/attr/vehicles/{path}").json()}
    fuels, gearboxes, bodies = names("fuels"), names("gearboxes"), names("body-types")
    make_ids = {slugify(m["name"]): m["id"] for m in get(f"{api}/attr/vehicles/makes").json() if m["group_id"] == 1}
    cars = []
    for make in wanted_makes(searches):
        make_id = make_ids.get(slugify(make))
        if not make_id:
            continue
        page, pages = 1, 1
        while page <= min(pages, 60):
            time.sleep(DELAY)
            r = session.post(f"{api}/search", timeout=30, json={"is_new": 0, "group_id": 1, "page": page, "make_id": make_id})
            r.raise_for_status()
            data = r.json()
            pages = data.get("pages") or 1
            for v in data.get("results") or []:
                key = f"veego:{v['id']}"
                name = f"{v['make']['name']} {v['model']['name']} {v.get('model_name_spec') or ''}".strip()
                branches = (v.get("company") or {}).get("branches") or []
                cars.append({
                    "key": key, "source": "veego", "url": f"https://veego.ee/kasutatud-autod/{v['id']}",
                    "title": f"{name}, {v['engine_power']} kW" if v.get("engine_power") else name, "name": name,
                    "year": v.get("year"),
                    "fuel": norm_fuel(fuels.get(v.get("fuel_id"))),
                    "gearbox": norm_gearbox(gearboxes.get(v.get("gearbox_id"))),
                    # veego's "Touring" is what Estonian ads call universaal
                    "body": "wagon" if bodies.get(v.get("body_type_id")) == "Touring" else norm_body(bodies.get(v.get("body_type_id"))),
                    "kw": v.get("engine_power"),
                    "km": v.get("odometer"),
                    "price": parse_int(str(v.get("discount_price") or v.get("active_price") or v.get("price") or "").split(".")[0]),
                    "location": (branches[0]["address"].get("city") if branches and branches[0].get("address") else None)
                                or (v.get("company") or {}).get("name"),
                    "image": (v.get("images") or [{}])[0].get("thumb_data_md"),
                })
            page += 1
    return cars


def nuxt_data(html):
    """Decode the __NUXT_DATA__ blob Nuxt sites embed: a flat array where values point at other indexes."""
    m = re.search(r'<script[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    raw = json.loads(m.group(1))
    done = {}

    def resolve(i):
        if not isinstance(i, int) or not 0 <= i < len(raw):
            return i
        if i in done:
            return done[i]
        v = raw[i]
        if isinstance(v, list) and v and v[0] in ("Reactive", "ShallowReactive", "Ref", "ShallowRef"):
            done[i] = resolve(v[1])
        elif isinstance(v, list):
            done[i] = out = []
            out.extend(resolve(x) for x in v)
        elif isinstance(v, dict):
            done[i] = out = {}
            out.update((k, resolve(x)) for k, x in v.items())
        else:
            done[i] = v
        return done[i]
    return resolve(0)


def easycar(seen, searches):
    """Dealers and private sellers; no make filter, so read the whole catalogue (~90 pages).
    Returns all listings, seen or not, so price changes are noticed."""
    cars = []
    for page in range(1, 150):
        state = nuxt_data(get("https://www.easycar.ee/kataloog", params={"page": page}).text)
        vehicles = state["pinia"]["vehiclesStore"]["listVehicles"]
        if not vehicles:
            break
        for v in vehicles:
            key = f"easycar:{v['id']}"
            if v.get("is_sold"):
                continue
            name = f"{v.get('make_title')} {v.get('model_title')}"
            cars.append({
                "key": key, "source": "easycar", "url": v["url"], "title": f"{name} {v.get('year') or ''}".strip(),
                "name": name, "year": v.get("year"),
                "fuel": norm_fuel(v.get("fuel_title")), "gearbox": norm_gearbox(v.get("transmission")),
                "body": norm_body(v.get("body_type_title")), "kw": v.get("power"), "km": v.get("mileage"),
                "price": v.get("price"),
                "location": ((v.get("location") or {}).get("city") or {}).get("name"),
                "image": next((i.get("original") for i in (v.get("images") or []) if isinstance(i, dict)), None),
            })
    return cars


def longo(seen, searches):
    """Big used-car dealer; the catalogue filters by make. Returns all listings so price changes are noticed."""
    cars = []
    for make in wanted_makes(searches):
        # The catalogue order changes between requests, so paging would skip cars; ask for everything at once.
        state = nuxt_data(get("https://longo.ee/kataloog", params={"make": make.upper(), "pageSize": 500}).text)
        for v in state["pinia"]["catalog"]["vehicles"]:
            key = f"longo:{v['id']}"
            name = f"{v.get('make', '').title()} {v.get('model', '').title()} {v.get('variant') or ''}".strip()
            slug = re.sub(r"\s+", "-", f"{v.get('make')}-{v.get('model')}-{v.get('year')}")
            cars.append({
                "key": key, "source": "longo", "url": f"https://longo.ee/auto/{slug}/{v.get('stockNumber')}",
                "title": name, "name": name, "year": v.get("year"),
                "fuel": norm_fuel(v.get("fuelTypeLabel")), "gearbox": norm_gearbox(v.get("transmissionLabel")),
                "body": norm_body(v.get("bodyType")), "kw": None, "km": v.get("mileage"),
                "price": v.get("price"), "location": "Longo", "image": v.get("thumbnailUrl"),
            })
    return cars


def oksjonikeskus(seen, searches):
    """Bailiff auctions (a handful of cars, often cheap). Details are free text or a two-column table."""
    base = "https://www.oksjonikeskus.ee"
    html = get(base + "/", params={"varaliik": "MS"}).text
    cars = []
    for item_id in dict.fromkeys(re.findall(r"okid=(\d+)", html)):
        key = f"oksjonikeskus:{item_id}"
        if key in seen:
            continue
        url = f"{base}/oksjon/view/?okid={item_id}"
        try:
            html = get(url).text
        except requests.RequestException as e:
            print(f"  oksjonikeskus {item_id}: {e}")
            continue
        soup = BeautifulSoup(html, "html.parser")
        text = " ".join(soup.get_text(" ", strip=True).split())
        # Either "Sõiduauto Volvo V50 (universaal; ...; ehitusaasta 2008; kütus diisel; 100 KW; ...)"
        # or a table: "Mark ja mudel AUDI A6 AVANT Registreerimismärk ... Ehitusaasta / värvus 2012 / must ..."
        # The heading repeats it as "Sõiduauto Volvo V50 (2008)", so take the longest description.
        if found := list(re.finditer(r"(Sõiduauto[^()]{0,60})\(([^)]{0,400})\)", text)):
            m = max(found, key=lambda x: len(x.group(2)))
            title, spec = m.group(1).strip(), m.group(2)
        elif m := re.search(r"Mark ja mudel (.{2,60}?) Registreerimismärk", text):
            title, spec = m.group(1).title(), text[m.start():m.start() + 1500]
        else:
            continue  # not a car
        cars.append({
            "key": key, "source": "oksjonikeskus (kohtutäituri oksjon)", "url": url, "title": title,
            "name": f"{title} {spec}",
            "year": year_in_text(spec) or first_year(title) or (first_year(spec) if len(spec) < 10 else None),
            "fuel": norm_fuel(spec), "gearbox": norm_gearbox(spec), "body": norm_body(spec),
            "kw": int(m.group(1)) if (m := KW_RE.search(spec)) else None, "km": parse_km(spec),
            "price": parse_int(m.group(1)) if (m := re.search(r"Alghind:?\s*([\d\s]+)\s*euro", text, re.I)) else None,
            "location": None,
            "image": og_image(html),
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
    if datetime.now().minute >= 15 and os.environ.get("RUN_ALL_SOURCES") != "true":
        print("  facebook: only in the first quarter of the hour")
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
                    "image": fb_field(chunk, r'"image":\{"uri":"((?:[^"\\]|\\.)*)"'),
                })
                time.sleep(2)
        finally:
            ctx.close()
    return cars


SOURCES = {"okidoki": okidoki, "soov": soov, "autodiiler": autodiiler, "autoportaal": autoportaal,
           "veego": veego, "easycar": easycar, "longo": longo, "oksjonikeskus": oksjonikeskus,
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


def matched_model(car, searches):
    """The (make, model) of the first search model this listing names, used to find similar cars."""
    return next(((mm["make"], mm["model"]) for s in searches for mm in s["models"]
                 if names_model(car.get("name"), mm["make"], mm["model"])), None)


def deal(car, model, pool):
    """Compare the price with similar listings: same model, year within 2, similar mileage and power.
    Returns (percent below the usual price, usual price, how many compared) or None when it can't be
    judged fairly (mileage unknown, or fewer than 5 similar cars)."""
    km, kw = car.get("km") or 0, car.get("kw")
    if not (model and car.get("price") and car.get("year")) or car["price"] < 300 or km < 1000:
        return None
    prices = sorted(
        c["price"] for c in pool
        if c is not car and c.get("url") != car.get("url") and c.get("price") and c["price"] >= 300
        and c.get("year") and abs(c["year"] - car["year"]) <= 2
        and (c.get("km") or 0) >= 1000 and abs(c["km"] - km) <= max(50_000, km * 0.25)
        and (not (kw and c.get("kw")) or abs(c["kw"] - kw) <= kw * 0.35)
        and names_model(c.get("name"), *model))
    if len(prices) < 5:
        return None
    mid = len(prices) // 2
    usual = prices[mid] if len(prices) % 2 else (prices[mid - 1] + prices[mid]) / 2
    return round((usual - car["price"]) / usual * 100), int(usual), len(prices)


def deal_line(d):
    if not d:
        return None
    pct, usual, n = d
    if pct >= 10:
        return f"🔥 <b>{pct}% alla tavahinna</b> (sarnased ~{num(usual)} €, {n} autot)"
    if pct <= -10:
        return f"⬆️ {-pct}% üle tavahinna (sarnased ~{num(usual)} €)"
    return f"💰 Tavaline hind (sarnased ~{num(usual)} €)"


def alert_text(car, hits, d=None, drop=None, also=()):
    """hits: [(search, unknown_fields)]; d: deal(); drop: {"from", "to"} when the price fell."""
    e = html_escape
    names = ", ".join(s["name"] for s, _ in hits)
    facts = [f"📅 {car['year']}" if car.get("year") else None,
             f"🛣 {num(car['km'])} km" if car.get("km") else None,
             f"⚡ {car['kw']} kW" if car.get("kw") else None,
             f"💶 <b>{num(car['price'])} €</b>" if car.get("price") else None]
    kind = [ET.get(car.get(f)) for f in ("fuel", "gearbox", "body")]
    unknown = sorted({f for _, u in hits for f in u})
    head = f"💸 <b>Hind langes!</b> {e(names)}" if drop else f"🚨 <b>{e(names)}</b>"
    lines = [head, f"<b>{e(car['title'])}</b>",
             " · ".join(x for x in facts if x),
             " · ".join(x for x in kind if x),
             f"📉 {num(drop['from'])} € → <b>{num(drop['to'])} €</b>" if drop else None,
             deal_line(d),
             f"📍 {e(car['location'])} · {car['source']}" if car.get("location") else f"📍 {car['source']}"]
    if also:
        lines.append("🔁 Sama auto ka: " + ", ".join(f'<a href="{e(u)}">{e(src)}</a>' for src, u in also))
    if unknown:
        lines.append(f"⚠️ Kuulutuses pole märgitud: {', '.join(ET[f] for f in unknown)}")
    lines.append(f'\n<a href="{e(car["url"])}">👉 Ava kuulutus</a>')
    return "\n".join(line for line in lines if line)


def telegram(method, **data):
    r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}", timeout=30,
                      data={"chat_id": TELEGRAM_CHAT, "parse_mode": "HTML", **data})
    r.raise_for_status()


def send_telegram(html, image=None):
    """Post to the family Telegram group, with the car's photo when there is one.
    Returns quietly if Telegram isn't set up."""
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT):
        print("  (Telegram not set up, not sending)")
        return
    if image and len(html) <= 1024:  # photo captions are limited to 1024 characters
        try:
            return telegram("sendPhoto", photo=image, caption=html)
        except requests.HTTPError as e:
            print(f"  photo refused ({e}), sending text only")
    telegram("sendMessage", text=html)


STATUS_TOKEN = setting("STATUS_TOKEN", "github_token.txt")
STATUS_NAME = os.environ.get("STATUS_NAME", "pc")  # "server" or "pc": each writes its own file
STATUS_URL = f"https://api.github.com/repos/herijanis/car-finder-ee/contents/status-{STATUS_NAME}.json"


def put_status_file(name, update):
    """Read-modify-write one JSON file on the "status" branch (kept off main so the app isn't rebuilt).
    update(old_dict) returns the new dict."""
    if not STATUS_TOKEN or DRY_RUN:
        return
    url = f"https://api.github.com/repos/herijanis/car-finder-ee/contents/{name}"
    headers = {"Authorization": f"Bearer {STATUS_TOKEN}", "Accept": "application/vnd.github+json"}
    try:
        r = requests.get(url, params={"ref": "status"}, headers=headers, timeout=20)
        old, sha = {}, None
        if r.status_code == 200:
            sha = r.json()["sha"]
            old = json.loads(base64.b64decode(r.json()["content"]))
        body = {"message": f"{name} from {STATUS_NAME}", "branch": "status",
                "content": base64.b64encode(json.dumps(update(old), ensure_ascii=False).encode()).decode()}
        if sha:
            body["sha"] = sha
        requests.put(url, headers=headers, json=body, timeout=30).raise_for_status()
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"{name} not published: {e}")


def publish_status(results, known, found):
    """Tell the app what this machine searched and found."""
    put_status_file(f"status-{STATUS_NAME}.json", lambda old: {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": {**old.get("sources", {}), **results},  # the fast and slow runs cover different sites
        "known": known,
        "recent": (found + old.get("recent", []))[:30],
    })


MARKET_URL = "https://raw.githubusercontent.com/herijanis/car-finder-ee/status/market.json"


def publish_market(pool, searches):
    """The server sees most listings; share prices of the wanted models so the PC's deal check has data."""
    rows = [{k: c.get(k) for k in ("name", "year", "km", "price")} for c in pool
            if c.get("price") and c.get("year") and matched_model(c, searches)]
    for r in rows:
        r["name"] = (r["name"] or "")[:80]
    put_status_file("market.json", lambda old: {"updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                                "cars": rows})


def load_market():
    try:
        return requests.get(MARKET_URL, timeout=20).json().get("cars", [])
    except (requests.RequestException, ValueError):
        return []


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

    failures, results = [], {}
    stamp = now.isoformat()
    if searches:
        for name, fn in SOURCES.items():
            print(f"{name}: searching")
            try:
                cars = fn(seen, searches)
            except Exception as e:  # one broken site must not stop the others
                print(f"{name}: FAILED {e!r}")
                failures.append(name)
                results[name] = {"ok": False, "at": now.isoformat(timespec="seconds")}
                if name == "facebook" and "login" in str(e):
                    tell_once_a_day("fb_login", "⚠️ Facebooki sisselogimine aegus. Marketplace'i ei otsita, "
                                    "kuni teine konto uuesti sisse logib (fb_login.py arvutis).")
                continue
            new = 0
            for car in cars:
                key = car.pop("key")
                entry = seen.get(key)
                if entry is None:
                    seen[key] = {"first_seen": stamp, "last_seen": stamp, "car": car, "alerted": []}
                    new += 1
                    continue
                # Listing seen before: some sites list everything each time, which lets us spot price drops.
                old = (entry.get("car") or {}).get("price")
                if old and car.get("price") and car["price"] <= old * 0.97 and old - car["price"] >= 100:
                    entry.setdefault("drops", []).append({"from": old, "to": car["price"], "at": stamp})
                    print(f"  price drop {old} -> {car['price']}: {car['url']}")
                entry["car"] = {**(entry.get("car") or {}), **{k: v for k, v in car.items() if v is not None}}
                entry["last_seen"] = stamp
            print(f"{name}: {new} new listings ({len(cars)} read)")
            results[name] = {"ok": True, "new": new, "at": now.isoformat(timespec="seconds")}

    pool = [e["car"] for e in seen.values() if "car" in e]
    market = pool + (load_market() if STATUS_NAME != "server" else [])

    # Check every recent listing against every search, so a search added in the app also finds cars
    # already listed. A new match is sent once per search; a later price drop is sent again.
    recent, alive = (now - timedelta(days=RECHECK_DAYS)).isoformat(), (now - timedelta(days=2)).isoformat()
    pending = []
    for key, entry in seen.items():
        if "car" not in entry or (entry["first_seen"] < recent and entry.get("last_seen", "") < alive):
            continue
        matches = [(s, unknown) for s in searches for ok, unknown in [check(entry["car"], s)] if ok]
        fresh = [(s, u) for s, u in matches if s["id"] not in entry["alerted"]]
        drop = (entry.get("drops") or [None])[-1]
        dropped = [(s, u) for s, u in matches if s["id"] in entry["alerted"]] if (
            drop and drop["to"] != entry.get("drop_alerted") and drop["at"] >= alive) else []
        if fresh:
            pending.append((key, entry, fresh, drop if drop and drop["at"] >= alive else None))
        elif dropped:
            pending.append((key, entry, dropped, drop))
    pending.sort(key=lambda p: p[1]["first_seen"], reverse=True)

    # The same car is often listed on several dealer sites: send it once, mentioning the others.
    def fingerprint(car):
        return (car.get("year"), car.get("km")) if car.get("year") and (car.get("km") or 0) >= 1000 else None
    told = {}
    for key, entry in seen.items():
        if entry.get("alerted") and "car" in entry and (fp := fingerprint(entry["car"])):
            told.setdefault(fp, set()).update(entry["alerted"])

    sent, found = 0, []
    for key, entry, hits, drop in pending:
        car = entry["car"]
        fp = fingerprint(car)
        model = matched_model(car, searches)
        if fp and not drop and all(s["id"] in told.get(fp, ()) for s, _ in hits) and model:
            print(f"  duplicate of an already sent car, skipped: {car['url']}")
            entry["alerted"] += [s["id"] for s, _ in hits]
            continue
        if sent >= MAX_ALERTS_PER_RUN:
            break
        also = [(e["car"]["source"], e["car"]["url"]) for k, e, _, _ in pending
                if k != key and fp and fingerprint(e["car"]) == fp and e["car"]["source"] != car["source"]]
        d = deal(car, model, market)
        print(f"{'DROP' if drop else 'MATCH'} {car['url']} for {[s['name'] for s, _ in hits]} deal={d}")
        try:
            send_telegram(alert_text(car, hits, d, drop, also), car.get("image"))
        except requests.RequestException as e:
            print(f"  telegram failed, will retry next run: {e}")
            continue
        if not DRY_RUN:
            entry["alerted"] += [s["id"] for s, _ in hits if s["id"] not in entry["alerted"]]
            if drop:
                entry["drop_alerted"] = drop["to"]
            if fp:
                told.setdefault(fp, set()).update(entry["alerted"])
        sent += 1
        found.append({k: car.get(k) for k in ("title", "year", "km", "kw", "price", "fuel", "gearbox",
                                               "location", "source", "url", "image")}
                     | {"searches": [s["name"] for s, _ in hits], "at": now.isoformat(timespec="seconds"),
                        "deal": d[0] if d else None, "drop": drop})
        time.sleep(1.5)
    if len(pending) > sent:
        print(f"{len(pending) - sent} more matches wait for the next run")
    if searches and len(failures) == len(SOURCES) and local.hour == 9 and local.minute < 30:
        try:
            send_telegram(f"⚠️ Autootsing ei saanud täna hommikul ühtegi saiti lugeda: {', '.join(failures)}")
        except requests.RequestException:
            pass

    forget = (now - timedelta(days=FORGET_DAYS)).isoformat()
    seen = {k: v for k, v in seen.items() if max(v["first_seen"], v.get("last_seen", "")) >= forget}
    SEEN_FILE.write_text(json.dumps(seen, ensure_ascii=False, indent=0, sort_keys=True), encoding="utf-8")
    publish_status(results, len(pool), found)
    if STATUS_NAME == "server" and local.minute < 10:  # an hourly sample is plenty for price comparisons
        publish_market(pool, searches)
    print(f"sent {sent} alerts")
    return 1 if searches and len(failures) == len(SOURCES) else 0


if __name__ == "__main__":
    sys.exit(main())
