@echo off
REM Version 1 - polling: reads only new RDB rows every POLL_SECONDS. http://localhost:8000
REM Leave KDB_HOST empty to run on simulated data.
REM set KDB_HOST=your-oms-rdb-host
REM set KDB_PORT=5000
REM set KDB_USER=
REM set KDB_PASS=
set POLL_SECONDS=1
cd /d %~dp0
..\.venv\Scripts\python -m uvicorn backend.app:app --host 0.0.0.0 --port 8000
