@echo off
cd /d "%~dp0"
python -c "import youtube_transcript_api, reportlab, anthropic" 2>nul || python -m pip install youtube-transcript-api reportlab anthropic
start "" http://localhost:8000
python server.py
