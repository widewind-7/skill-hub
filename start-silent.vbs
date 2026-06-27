Set WshShell = CreateObject("WScript.Shell")
WshShell.Run "cmd /c cd /d E:\AI\workspace\skill-hub && E:\Python\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8765", 0, False
WScript.Sleep 2000
WshShell.Run "http://localhost:8765", 1, False
