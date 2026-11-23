@echo off
setlocal
set LOCALWHISPER_SUPERVISED=1

:server_loop
echo Starting LocalWhisper CUDA ASR + Agent Server (ASR: Qwen/Qwen3-ASR-1.7B, LLM: http://127.0.0.1:34501/v1)...
python "%~dp0main.py" --host 0.0.0.0 --port 8765 --model Qwen/Qwen3-ASR-1.7B --device cuda:0 --llm-base-url http://127.0.0.1:34501/v1 %*
if "%ERRORLEVEL%"=="42" (
    echo.
    echo [LocalWhisper] Update requested - running git pull in this window...
    python "%~dp0updater.py" --in-place
    echo [LocalWhisper] Relaunching server in this window...
    echo.
    goto server_loop
)
pause
