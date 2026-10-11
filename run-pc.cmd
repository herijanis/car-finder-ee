@echo off
rem Search okidoki and soov from this PC every 30 minutes (Windows Task Scheduler runs this).
rem They block GitHub's servers; Facebook also needs the spare account's login in fb-profile (fb_login.py).
cd /d "%~dp0"
set SOURCES=okidoki,soov,facebook
set SEEN_FILE=seen-pc.json
set SEARCHES_FROM_GITHUB=true
"%LOCALAPPDATA%\Programs\Python\Python312\python.exe" finder.py >> search-pc.log 2>&1
