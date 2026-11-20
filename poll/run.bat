@echo off
REM Version 1 - polling: reads only new RDB rows every POLL_SECONDS. http://localhost:8000
REM Leave KDB_HOST empty to run on simulated data.
REM set KDB_HOST=your-oms-rdb-host
REM set KDB_PORT=5000
REM set KDB_USER=
REM set KDB_PASS=
set POLL_SECONDS=1
REM Volume profile (row click): the VPROF gateway. Leave empty for a simulated profile.
REM set VPROF_HOST=your-vprof-gateway-host
REM set VPROF_PORT=
REM VPROF_USER / VPROF_PASS default to KDB_USER / KDB_PASS
REM Today's traded volume (Full Day tab): qatt on the QUOTES real-time server. Leave empty for simulated trades.
REM set QATT_HOST=your-quotes-rdb-host
REM set QATT_PORT=
REM QATT_USER / QATT_PASS default to KDB_USER / KDB_PASS
REM Region clock of the profile and order times ("now" in the order profile). Default Asia/Hong_Kong
REM set REGION_TZ=Asia/Hong_Kong
cd /d %~dp0
..\.venv\Scripts\python -m uvicorn backend.app:app --host 0.0.0.0 --port 8000
