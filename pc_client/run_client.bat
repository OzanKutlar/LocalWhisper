@echo off
setlocal
REM Change 127.0.0.1 to your Server PC's LAN IP if running on a separate PC.
set SERVER_URL=ws://127.0.0.1:8765
echo Starting LocalWhisper PC Client (AutoHotkey v1.1) -> %SERVER_URL%
python "%~dp0client.py" --server %SERVER_URL% %*
pause
