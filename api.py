"""Tiny web API on the Hetzner server: the app reads and saves searches here, no key needed.

Caddy (deploy/Caddyfile) serves the app over https and forwards /api/ to this process on localhost.
Anyone may look; saving needs the family PIN (data/pin.txt, never in git), which the app asks for once.
Wrong PINs are throttled, every save is validated strictly, and the last 50 versions are kept in
data/history/ to undo mistakes.
"""

import hmac

import json
import re
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DATA = Path(__file__).parent / "data"
SEARCHES = DATA / "searches.json"
HISTORY = DATA / "history"
KEEP = 50
MAX_BODY = 64 * 1024
PIN_FILE = DATA / "pin.txt"
MAX_WRONG, WRONG_WINDOW = 5, 15 * 60   # a 4-digit PIN must not be guessable by trying them all
wrong_pins = []

FUELS = {"any", "diesel", "petrol", "hybrid", "electric", "gas"}
GEARBOXES = {"any", "manual", "automatic"}
BODIES = {"wagon", "sedan", "hatchback", "suv", "minivan", "coupe", "cabrio"}
REQUIRE = {"fuel", "gearbox", "body", "km", "price", "kw"}
NUMBERS = {"year_min": (1950, 2035), "year_max": (1950, 2035), "price_min": (0, 10**7), "price_max": (0, 10**7),
           "km_max": (0, 3 * 10**6), "kw_min": (0, 2000), "kw_max": (0, 2000)}


def text(v, limit):
    if not isinstance(v, str) or not v.strip() or len(v) > limit:
        raise ValueError("bad text")
    return v.strip()


def clean(data):
    """Rebuild the searches from only the fields we know, with checked types and ranges."""
    searches = data.get("searches") if isinstance(data, dict) else None
    if not isinstance(searches, list) or len(searches) > 30:
        raise ValueError("searches must be a list of at most 30")
    out = []
    for s in searches:
        models = s.get("models")
        if not isinstance(models, list) or not 1 <= len(models) <= 20:
            raise ValueError("1-20 models per search")
        c = {
            "id": text(s.get("id"), 40) if re.fullmatch(r"[a-z0-9]{1,40}", str(s.get("id", ""))) else None,
            "name": text(s.get("name"), 60),
            "active": bool(s.get("active", True)),
            "models": [{"make": text(m.get("make"), 40), "model": text(m.get("model"), 40)} for m in models],
            "fuel": s.get("fuel") if s.get("fuel") in FUELS else "any",
            "gearbox": s.get("gearbox") if s.get("gearbox") in GEARBOXES else "any",
            "body": [b for b in (s.get("body") if isinstance(s.get("body"), list) else [s.get("body")]) if b in BODIES],
            "require": [r for r in (s.get("require") or []) if r in REQUIRE],
        }
        if not c["id"]:
            raise ValueError("bad id")
        for k, (lo, hi) in NUMBERS.items():
            v = s.get(k)
            if v is None or v == "":
                c[k] = None
            elif isinstance(v, (int, float)) and lo <= v <= hi:
                c[k] = int(v)
            else:
                raise ValueError(f"bad {k}")
        out.append(c)
    if len({c["id"] for c in out}) != len(out):
        raise ValueError("duplicate id")
    return {"searches": out}


def save(data):
    HISTORY.mkdir(parents=True, exist_ok=True)
    if SEARCHES.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        (HISTORY / f"searches-{stamp}.json").write_bytes(SEARCHES.read_bytes())
        for old in sorted(HISTORY.glob("searches-*.json"))[:-KEEP]:
            old.unlink()
    tmp = SEARCHES.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(SEARCHES)


def pin_ok(given):
    """Constant-time PIN check; after MAX_WRONG misses in the window every attempt is refused."""
    now = time.monotonic()
    wrong_pins[:] = [t for t in wrong_pins if now - t < WRONG_WINDOW]
    if len(wrong_pins) >= MAX_WRONG:
        return None
    if hmac.compare_digest((given or "").encode(), PIN_FILE.read_text().strip().encode()):
        return True
    wrong_pins.append(now)
    return False


class Handler(BaseHTTPRequestHandler):
    last_write = 0.0

    def reply(self, code, body):
        raw = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.split("?")[0] != "/api/searches":
            return self.reply(404, {"error": "not found"})
        self.reply(200, json.loads(SEARCHES.read_text(encoding="utf-8")))

    def check_pin(self):
        ok = pin_ok(self.headers.get("X-PIN"))
        if ok is None:
            self.reply(429, {"error": "too many wrong PINs, wait 15 minutes"})
        elif not ok:
            self.reply(403, {"error": "wrong PIN"})
        return ok

    def do_POST(self):
        if self.path.split("?")[0] != "/api/pin":
            return self.reply(404, {"error": "not found"})
        if self.check_pin():
            self.reply(200, {"ok": True})

    def do_PUT(self):
        if self.path.split("?")[0] != "/api/searches":
            return self.reply(404, {"error": "not found"})
        if not self.check_pin():
            return
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_BODY:
            return self.reply(413, {"error": "too large"})
        # A person saves a few times a minute at most; anything faster is a script.
        if time.monotonic() - Handler.last_write < 1.5:
            return self.reply(429, {"error": "slow down"})
        try:
            data = clean(json.loads(self.rfile.read(length)))
        except (ValueError, AttributeError, TypeError) as e:
            return self.reply(400, {"error": str(e)})
        Handler.last_write = time.monotonic()
        save(data)
        self.reply(200, data)

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8090), Handler).serve_forever()
