# car-finder-ee

Searches Estonian car listings every hour and sends new matches to my phone via ntfy.

- **What it looks for:** the `CRITERIA` block at the top of `finder.py`.
- **Sites:** okidoki.ee, soov.ee, autoportaal.ee, autoscout24 (Europe) and ss.lv (Latvia) from the home PC every 30 minutes (Windows task "Car finder"), autodiiler.ee from GitHub hourly. Only 08:00-23:00 Estonian time.
- **Abroad is stricter:** max 200 000 km (Estonia 250 000), and mileage, fuel, gearbox and body type must be stated in the ad.
  auto24.ee and osta.ee block scripts, so use their own saved-search alerts.
- **Current matches:** `MATCHES.md`.
- **Run it now:** Actions tab → "Search for cars" → Run workflow.
- **Phone:** the ntfy channel name is stored as the `NTFY_TOPIC` repo secret.

Listings where a detail (like mileage) isn't stated in the ad are still sent, marked "Not stated in ad".
