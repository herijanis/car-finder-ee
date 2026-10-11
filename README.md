# car-finder-ee

Finds used cars on Estonian sites and posts new matches to the family Telegram group.

- **App:** https://herijanis.github.io/car-finder-ee/ (in `docs/`). Add, change or switch off searches
  from a phone. Editing needs the private setup link once per phone.
- **Searches:** `searches.json`, written by the app. Several searches can run at once.
- **Sites:** autoportaal.ee (every 10 min) and autodiiler.ee, veego.ee, easycar.ee, longo.ee and
  oksjonikeskus.ee (every 30 min) from the Hetzner server, 24/7
  (`deploy/`, logs: `journalctl -u "car-finder@*"`).
  okidoki.ee and soov.ee block GitHub, so the home PC searches those (Windows task "Car finder").
  The home PC searches only 08:00-23:00; the server picks up app changes within 10 minutes.
- **Facebook Marketplace** is searched from the home PC hourly as a spare Facebook account
  (log in once with `python fb_login.py`; the login stays in the git-ignored fb-profile/).
- **auto24.ee** blocks programs; use its own saved-search alerts.
- **Alerts:** Telegram bot @AutoTeavitusBot, secrets `TELEGRAM_TOKEN` and `TELEGRAM_CHAT`.
  Each car is sent once per search. A newly added search also checks cars listed in the last 30 days.
