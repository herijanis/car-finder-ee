# car-finder-ee

Searches Estonian car listings every hour and sends new matches to my phone via ntfy.

- **What it looks for:** the `CRITERIA` block at the top of `finder.py`.
- **Sites (Estonia only):** autodiiler.ee and autoportaal.ee from GitHub every 30 minutes (always on), okidoki.ee and soov.ee from the home PC every 30 minutes (they block GitHub). Only 08:00-23:00 Estonian time.
  auto24.ee and osta.ee block scripts, so use their own saved-search alerts.
- **Current matches:** `MATCHES.md`.
- **Run it now:** Actions tab → "Search for cars" → Run workflow.
- **Phone:** the ntfy channel name is stored as the `NTFY_TOPIC` repo secret.

Listings where a detail (like mileage) isn't stated in the ad are still sent, marked "Not stated in ad".
