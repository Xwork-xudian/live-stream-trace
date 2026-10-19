@echo off
if exist "%~dp0dist\douyin-live-stream-trace.exe" (
    "%~dp0dist\douyin-live-stream-trace.exe" %*
) else (
    python "%~dp0analyze_room.py" %*
)
