"""Opens a separate browser so the spare Facebook account can log in once.
The login is kept in fb-profile/ (git-ignored, never leaves this PC) and the car search reuses it."""

from pathlib import Path

from playwright.sync_api import sync_playwright

PROFILE = Path(__file__).parent / "fb-profile"

with sync_playwright() as p:
    browser = p.chromium.launch_persistent_context(PROFILE, headless=False, locale="et-EE",
                                                   viewport={"width": 1200, "height": 850})
    page = browser.pages[0] if browser.pages else browser.new_page()
    page.goto("https://www.facebook.com/marketplace/")
    print("Logi aknas sisse ja sulge aken, kui Marketplace on näha.")
    page.wait_for_event("close", timeout=0)
    browser.close()
