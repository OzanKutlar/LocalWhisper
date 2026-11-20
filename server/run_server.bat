@echo off
setlocal
echo Starting LocalWhisper CUDA ASR + Agent Server (ASR: Qwen/Qwen3-ASR-1.7B, LLM: http://127.0.0.1:34501/v1)...
python "%~dp0main.py" --host 0.0.0.0 --port 8765 --model Qwen/Qwen3-ASR-1.7B --device cuda:0 --llm-base-url http://127.0.0.1:34501/v1 %*
pause
