@echo off
title Skill Hub
echo Starting Skill Hub at http://localhost:8765 ...
cd /d E:\AI\workspace\skill-hub
start http://localhost:8765
E:\Python\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8765
pause
