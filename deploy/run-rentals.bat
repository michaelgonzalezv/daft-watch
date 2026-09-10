@echo off
REM DaftWatch rentals — one scrape/enrich/export/publish/email cycle.
REM Run this by hand once first (the first cycle detail-fetches every
REM <=EUR800 share in 3 cities and takes ~20-30 min); Task Scheduler
REM then calls it every 30 min for fast incremental cycles.

cd /d D:\Github\daft-watch

REM Load KEY=VALUE pairs from .env (skip blank and #-comment lines).
REM tokens=1,* keeps the whole value even if it contains spaces or '='.
for /f "usebackq tokens=1,* delims==" %%a in (`type "D:\Github\daft-watch\.env" ^| findstr /r "^[^#].*="`) do set "%%a=%%b"

REM Uses the global Python on PATH. If 'python' is not found, replace the
REM next line with the full path, e.g.:
REM   "C:\Users\joale\AppData\Local\Programs\Python\Python312\python.exe" -m daftwatch run --config data\config.yaml --db data\daft.db >> data\rentals.log 2>&1
python -m daftwatch run --config data\config.yaml --db data\daft.db >> data\rentals.log 2>&1
