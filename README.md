# car-finder-ee

Finds used cars on Estonian sites and posts new matches to the family Telegram group.

- **App:** https://herijanis.github.io/car-finder-ee/ (in `docs/`). Add, change or switch off searches
  from a phone. Editing needs the private setup link once per phone.
- **Searches:** `searches.json`, written by the app. Several searches can run at once.
- **Sites:** autodiiler.ee and autoportaal.ee from GitHub every 30 minutes (works with the PC off).
  okidoki.ee and soov.ee block GitHub, so the home PC searches those (Windows task "Car finder").
  Only 08:00-23:00 Estonian time. A search saved in the app is searched right away on GitHub.
- **auto24.ee and Facebook Marketplace** block programs; use their own saved-search alerts.
- **Alerts:** Telegram bot @AutoTeavitusBot, secrets `TELEGRAM_TOKEN` and `TELEGRAM_CHAT`.
  Each car is sent once per search. A newly added search also checks cars listed in the last 30 days.
