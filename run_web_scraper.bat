@echo off
cd /d "%~dp0"
python web_scraper_local.py >> web_scraper.log 2>&1
