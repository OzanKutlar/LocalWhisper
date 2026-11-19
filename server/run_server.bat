@echo off
setlocal
echo Starting LocalWhisper CUDA ASR Server (Qwen/Qwen3-ASR-1.7B)...
python "%~dp0main.py" --host 0.0.0.0 --port 8765 --model Qwen/Qwen3-ASR-1.7B --device cuda:0 %*
pause
