"""Search Estonian car sites for listings that match CRITERIA and push new ones to ntfy."""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# What to look for. Edit these to change the search.
# ---------------------------------------------------------------------------
CRITERIA = {
    # brand -> models (matched against the listing's make/model or title)
    "models": {"audi": ["a4", "a6"], "volkswagen": ["passat"]},
    "year_min": 1996,
    "year_max": 2004,
    "body": "wagon",       # universaal / Avant / Variant
    "fuel": "diesel",
    "gearbox": "manual",
    "kw_max": 96,
    "km_max": 200_000,
}

# Search terms per site (the model check above decides what actually matches).
SEARCH_TERMS = ["audi a4", "audi a6", "passat"]

ROOT = Path(__file__).parent
SEEN_FILE = ROOT / "seen.json"
MATCHES_FILE = ROOT / "MATCHES.md"
DRY_RUN = os.environ.get("DRY_RUN") == "true"
NTFY_TOPIC = "" if DRY_RUN else os.environ.get("NTFY_TOPIC", "")
MAX_ALERTS_PER_RUN = 15
DETAIL_FETCH_LIMIT = 400   # per source per run, keeps the first run polite
DELAY = 1.0                # seconds between requests to the same site

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/140.0 Safari/537.36",
    "Accept-Language": "et-EE,et;q=0.9,en;q=0.8",
})


def get(url, **kw):
    time.sleep(DELAY)
    r = session.get(url, timeout=30, **kw)
    r.raise_for_status()
    return r


# ---------------------------------------------------------------------------
# Normalising free text into fields
# ---------------------------------------------------------------------------
YEAR_RE = re.compile(r"\b(19[5-9]\d|20[0-3]\d)\b")
KW_RE = re.compile(r"(\d{2,3})\s*kw\b", re.I)
KM_RE = re.compile(r"(\d{1,3}(?:[\s. ]?\d{3})+|\d{3,7})\s*(tkm|tuh\.?\s*km|km)\b", re.I)


def norm_fuel(text):
    t = (text or "").lower()
    if re.search(r"diis|diesel|\btdi\b|\bsdi\b|\btd\b", t):
        return "diesel"
    if re.search(r"bensiin|petrol|\btsi\b|\bfsi\b|\bturbo\b", t):
        return "petrol"
    if "gaas" in t or "lpg" in t:
        return "gas"
    return None


def norm_gearbox(text):
    t = (text or "").lower()
    if re.search(r"manuaal|manual|käsi", t):
        return "manual"
    if re.search(r"automaat|automatic|tiptronic|multitronic|dsg", t):
        return "automatic"
    return None


def norm_body(text):
    t = (text or "").lower()
    if re.search(r"universaal|avant|variant|kombi|wagon|touring|estate", t):
        return "wagon"
    if re.search(r"sedaan|sedan", t):
        return "sedan"
    if re.search(r"luukpära|hatchback|kupee|kabriolett|maastur", t):
        return "other"
    return None


def parse_km(text):
    m = KM_RE.search(text or "")
    if not m:
        return None
    n = int(re.sub(r"\D", "", m.group(1)))
    if m.group(2).lower().startswith(("tkm", "tuh")):
        n *= 1000
    return n


def parse_int(text):
    digits = re.sub(r"\D", "", str(text or ""))
    return int(digits) if digits else None


def model_of(text):
    """Return 'Audi A4' / 'Audi A6' / 'Volkswagen Passat' if text names one of the wanted models."""
    t = (text or "").lower().replace("-", " ")
    for brand, models in CRITERIA["models"].items():
        for model in models:
            if re.search(rf"\b{model}\b", t):
                # 'passat' is unambiguous; 'a4'/'a6' need the brand nearby to skip e.g. paper sizes
                if brand == "audi" and "audi" not in t:
                    continue
                return f"{brand.title()} {model.upper() if len(model) <= 2 else model.title()}"
    return None


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------
def check(car):
    """Return (is_match, list_of_unknown_fields). A known value that fails any rule rejects the car."""
    c = CRITERIA
    if not car.get("model"):
        return False, []
    unknown = []
    rules = [
        ("year", lambda v: c["year_min"] <= v <= c["year_max"]),
        ("body", lambda v: v == c["body"]),
        ("fuel", lambda v: v == c["fuel"]),
        ("gearbox", lambda v: v == c["gearbox"]),
        ("kw", lambda v: v <= c["kw_max"]),
        ("km", lambda v: v <= c["km_max"]),
    ]
    for field, ok in rules:
        v = car.get(field)
        if v is None:
            unknown.append(field)
        elif not ok(v):
            return False, []
    # Year is essential: without it almost any Passat would slip through.
    if "year" in unknown:
        return False, []
    return True, unknown


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------
def okidoki(seen):
    base = "https://www.okidoki.ee"
    ids = {}
    for term in SEARCH_TERMS:
        html = get(f"{base}/buy/1601/", params={"query": term, "pp": 200}).text
        for href, item_id in re.findall(r'href="(/item/[^"/]+/(\d+)/)"', html):
            ids[item_id] = base + href
    cars = []
    for item_id, url in list(ids.items()):
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
        blob = " ".join([title] + [f"{k} {v}" for k, v in specs.items()])
        price_el = soup.select_one("[class*=price]")
        cars.append({
            "key": key, "source": "okidoki", "url": url, "title": title,
            "model": model_of(specs.get("mark ja mudel", "") + " " + title),
            "year": int(m.group(1)) if (m := YEAR_RE.search(specs.get("registreerimine", ""))) else None,
            "fuel": norm_fuel(specs.get("kütus")),
            "gearbox": norm_gearbox(specs.get("käigukast")),
            "body": norm_body(specs.get("keretüüp")),
            "kw": int(m.group(1)) if (m := KW_RE.search(blob)) else None,
            "km": parse_int(specs.get("läbisõit")),
            "price": parse_int(price_el.get_text()) if price_el else None,
            "location": specs.get("asukoht"),
        })
    return cars


def soov(seen):
    ids = {}
    for term in SEARCH_TERMS:
        slug = term.replace(" ", "-")
        html = get(f"https://soov.ee/577-autod/keyword-{slug}/listings.html").text
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
        years = [int(y) for y in YEAR_RE.findall(title)] or [int(y) for y in YEAR_RE.findall(desc)]
        km = parse_km(text)
        if km is None and (m := re.search(r"läbisõit\s*:?\s*([\d\s.]+)", text, re.I)):
            km = parse_int(m.group(1))
        price = None
        if m := re.search(r"(\d[\d\s]{1,7})\s*(€|eur)", text, re.I):
            price = parse_int(m.group(1))
        cars.append({
            "key": key, "source": "soov", "url": url, "title": title,
            "model": model_of(text),
            "year": years[0] if years else None,
            "fuel": norm_fuel(text),
            "gearbox": norm_gearbox(text),
            "body": norm_body(text),
            "kw": int(m.group(1)) if (m := KW_RE.search(text)) else None,
            "km": km,
            "price": price,
            "location": None,
        })
    return cars


def autodiiler(seen):
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
            seo = (v.get("seo") or {}).get("description", "")
            year = int(m.group(1)) if (m := YEAR_RE.search(seo)) else None
            cars.append({
                "key": key, "source": "autodiiler", "url": f"https://autodiiler.ee/et/vehicles/{v['id']}",
                "title": v.get("name", ""),
                "model": model_of(f"{brand} {model}"),
                "year": year,
                "fuel": {"diesel": "diesel", "petrol": "petrol"}.get((v.get("fuel_type") or {}).get("value"), "other"),
                "gearbox": norm_gearbox((v.get("transmission_type") or {}).get("value")),
                "body": norm_body((v.get("vehicleBodyType") or {}).get("name")),
                "kw": v.get("engine_power"),
                "km": v.get("mileage"),
                "price": v.get("price"),
                "location": v.get("location_name"),
            })
        page += 1
    return cars


SOURCES = {"okidoki": okidoki, "soov": soov, "autodiiler": autodiiler}
# autodiiler has no server-side filters, so reading it means ~100 pages; only do that every 6 hours.
SLOW_SOURCES = {"autodiiler": 6}


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def describe(car, unknown):
    bits = [f"{car['km']:,} km".replace(",", " ") if car.get("km") else None,
            f"{car['kw']} kW" if car.get("kw") else None,
            f"{car['price']:,} €".replace(",", " ") if car.get("price") else None,
            car.get("location")]
    line = " · ".join(b for b in bits if b)
    if unknown:
        line += f"\nNot stated in ad: {', '.join(unknown)}"
    return f"{car['title']}\n{line}\n{car['source']}"


def notify(car, unknown):
    if not NTFY_TOPIC:
        print("  (NTFY_TOPIC not set, not sending)")
        return
    title = f"{car.get('year', '')} {car['model']}".strip()
    requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=describe(car, unknown).encode("utf-8"),
        headers={"Title": title.encode("utf-8"), "Click": car["url"], "Tags": "red_car"},
        timeout=20,
    )


def notify_text(title, message):
    if NTFY_TOPIC:
        requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=message.encode("utf-8"),
                      headers={"Title": title}, timeout=20)


def write_matches(seen):
    rows = sorted((v for v in seen.values() if v.get("match")), key=lambda v: v["first_seen"], reverse=True)
    lines = ["# Current matches", "", f"Updated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC", ""]
    if not rows:
        lines.append("Nothing yet.")
    for v in rows:
        lines.append(f"- **{v['year']} {v['model']}**: [{v['title']}]({v['url']}) · {v['summary']} "
                     f"· {v['source']} · found {v['first_seen'][:10]}")
    MATCHES_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    seen = json.loads(SEEN_FILE.read_text(encoding="utf-8")) if SEEN_FILE.exists() else {}
    first_run = not seen
    now = datetime.now(timezone.utc)
    force_all = os.environ.get("RUN_ALL_SOURCES") == "true"

    new_matches, failures = [], []
    for name, fn in SOURCES.items():
        every = SLOW_SOURCES.get(name)
        if every and not (first_run or force_all or now.hour % every == 0):
            print(f"{name}: skipped this hour")
            continue
        print(f"{name}: searching")
        try:
            cars = fn(seen)
        except Exception as e:  # one broken site must not stop the others
            print(f"{name}: FAILED {e!r}")
            failures.append(name)
            continue
        found = 0
        for car in cars:
            ok, unknown = check(car)
            if not ok and car.get("model") and car.get("year") and 1996 <= car["year"] <= 2004:
                print(f"  near miss: {car['year']} {car['model']} body={car.get('body')} fuel={car.get('fuel')} "
                      f"gearbox={car.get('gearbox')} kw={car.get('kw')} km={car.get('km')} {car['url']}")
            entry = {"first_seen": now.isoformat(), "match": ok}
            if ok:
                found += 1
                entry.update({k: car.get(k) for k in ("url", "title", "model", "year", "source")})
                entry["summary"] = describe(car, unknown).split("\n")[1]
                new_matches.append((car, unknown))
            seen[car["key"]] = entry
        print(f"{name}: {len(cars)} new listings checked, {found} match")

    for car, unknown in new_matches[:MAX_ALERTS_PER_RUN]:
        print(f"MATCH {car['url']}")
        notify(car, unknown)
    if len(new_matches) > MAX_ALERTS_PER_RUN:
        notify_text("More car matches", f"{len(new_matches) - MAX_ALERTS_PER_RUN} more matches this run. "
                                        "See MATCHES.md in the car-finder-ee repo.")
    if len(failures) == len(SOURCES) and now.hour == 8:
        notify_text("Car finder: site problem", f"Could not search: {', '.join(failures)}")

    # Forget listings first seen over a year ago so the file stays small.
    cutoff = (now - timedelta(days=365)).isoformat()
    seen = {k: v for k, v in seen.items() if v["first_seen"] >= cutoff}
    SEEN_FILE.write_text(json.dumps(seen, ensure_ascii=False, indent=0, sort_keys=True), encoding="utf-8")
    write_matches(seen)
    return 1 if len(failures) == len(SOURCES) else 0


if __name__ == "__main__":
    sys.exit(main())
