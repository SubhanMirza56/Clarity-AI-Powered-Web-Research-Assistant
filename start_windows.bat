@echo off
cd /d "%~dp0"
uv run --python 3.12 --with-requirements requirements.txt uvicorn app:app --host 127.0.0.1 --port 8000 --reload
pause
