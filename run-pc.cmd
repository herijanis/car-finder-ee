@echo off
rem Hourly search from this PC (Windows Task Scheduler runs this).
cd /d "%~dp0"
set SOURCES=okidoki,soov
set SEEN_FILE=seen-pc.json
set MATCHES_FILE=MATCHES-pc.md
"%LOCALAPPDATA%\Programs\Python\Python312\python.exe" finder.py >> search-pc.log 2>&1
