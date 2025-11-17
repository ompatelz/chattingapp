@echo off
echo Killing chat server on port 8765...

for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8765') do (
    echo Found server PID: %%a
    taskkill /PID %%a /F
)

echo Done.
pause
