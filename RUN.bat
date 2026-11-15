@echo off
cd /d "%~dp0"
where py >nul 2>nul && (py fix_dates.py) || (python fix_dates.py)
echo.
pause
