# car-finder-ee

Searches Estonian car listings every hour and sends new matches to my phone via ntfy.

- **What it looks for:** the `CRITERIA` block at the top of `finder.py`.
- **Sites:** okidoki.ee and soov.ee every hour, autodiiler.ee every 6 hours.
  auto24.ee and osta.ee block scripts, so use their own saved-search alerts.
- **Current matches:** `MATCHES.md`.
- **Run it now:** Actions tab → "Search for cars" → Run workflow.
- **Phone:** the ntfy channel name is stored as the `NTFY_TOPIC` repo secret.

Listings where a detail (like mileage) isn't stated in the ad are still sent, marked "Not stated in ad".
