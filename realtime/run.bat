@echo off
REM Version 2 - real-time: subscribes to the tickerplant, snapshot from the RDB. http://localhost:8001
REM Leave KDB_HOST or TP_HOST empty to run on simulated data.
REM Check the tickerplant first with:  ..\.venv\Scripts\python scripts\check_tickerplant.py HOST PORT
REM set KDB_HOST=your-oms-rdb-host
REM set KDB_PORT=5000
REM set TP_HOST=your-oms-tickerplant-host
REM set TP_PORT=5010
REM set KDB_USER=
REM set KDB_PASS=
REM Volume profile (row click): the VPROF gateway. Leave empty for a simulated profile.
REM set VPROF_HOST=your-vprof-gateway-host
REM set VPROF_PORT=
REM VPROF_USER / VPROF_PASS default to KDB_USER / KDB_PASS
REM Region clock of the profile and order times ("now" in the order profile). Default Asia/Hong_Kong
REM set REGION_TZ=Asia/Hong_Kong
cd /d %~dp0
..\.venv\Scripts\python -m uvicorn backend.app:app --host 0.0.0.0 --port 8001
