# car-finder-ee

Finds used cars on Estonian sites and posts new matches to the family Telegram group.

- **App:** https://herijanis.github.io/car-finder-ee/ (in `docs/`). Add, change or switch off searches
  from a phone. Editing needs the private setup link once per phone.
- **Searches:** `searches.json`, written by the app. Several searches can run at once.
- **Sites:** autoportaal, veego, longo and bailiff auctions (every 5 min) and autodiiler, easycar
  (every 15 min) from the Hetzner server, 24/7 (`deploy/`, logs: `journalctl -u "car-finder@*"`).
  okidoki.ee and soov.ee block servers, so the home PC searches them every 15 min, 08:00-23:00.
- **Alerts** carry the photo, a price comparison with similar listings ("🔥 25% alla tavahinna"),
  price drops on listings already sent, and send a car listed on several sites only once.
- **Facebook Marketplace** is searched from the home PC hourly as a spare Facebook account
  (log in once with `python fb_login.py`; the login stays in the git-ignored fb-profile/).
- **auto24.ee** blocks programs; use its own saved-search alerts.
- **Alerts:** Telegram bot @AutoTeavitusBot, secrets `TELEGRAM_TOKEN` and `TELEGRAM_CHAT`.
  Each car is sent once per search. A newly added search also checks cars listed in the last 30 days.
