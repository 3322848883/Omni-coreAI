@echo off
rem 停止 brooks-btc 相关进程
cd /d "%~dp0.."
for /f "tokens=2" %%p in ('tasklist /fi "imagename eq python.exe" /fo list ^| findstr PID') do (
  wmic process where "ProcessId=%%p" get CommandLine 2>nul | findstr "gate_bot" >nul && taskkill /pid %%p /f
)
echo stopped if any
