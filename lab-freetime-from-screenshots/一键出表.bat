@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
set "PYTHONIOENCODING=utf-8"
pushd "%~dp0"

rem ------------------------------------------------------------------
rem  Usage:
rem    double click                         -> interactive prompts
rem    or:  一键出表.bat  <shots folder>  <week no>  <date range>  <monday date>
rem ------------------------------------------------------------------

set "SHOTS=%~1"
set "WNO=%~2"
set "WRANGE=%~3"
set "MON=%~4"

echo ==========================================
echo   Timetable screenshots   to   Freetime + PDF
echo ==========================================
echo   Put screenshots in ONE folder.
echo   File name must contain the student name:
echo     ZhuZhenyi-3.jpg                (one student)
echo     HaoJiaYing-XuMingchun-3.jpg    (same class, shared)
echo.

if not defined SHOTS set /p SHOTS=Shots folder (Enter = .\shots): 
if not defined SHOTS set "SHOTS=%~dp0shots"
if not exist "%SHOTS%" (
  echo [ERROR] folder not found: %SHOTS%
  pause
  exit /b 1
)

if not defined WNO set /p WNO=Week number (e.g. 3): 
if not defined WRANGE set /p WRANGE=Date range (e.g. 9.14-9.20): 
if not defined MON set /p MON=Monday date (e.g. 9.14): 

set "OUT=%~dp0output"
if not exist "%OUT%" mkdir "%OUT%"

rem ---- find a WORKING python: WorkBuddy bundled one first, then system python
set "PY="
set "PYW=%USERPROFILE%\.workbuddy\binaries\python\versions"
if exist "%PYW%\3.13.12\python.exe" set "PY=%PYW%\3.13.12\python.exe"
if not defined PY if exist "%PYW%" (
  for /d %%D in ("%PYW%\*") do if exist "%%~fD\python.exe" set "PY=%%~fD\python.exe"
)
if not defined PY (
  set "SYSPY="
  for /f "delims=" %%P in ('where python 2^>nul') do if not defined SYSPY set "SYSPY=%%P"
  echo !SYSPY! | findstr /i "WindowsApps" >nul
  if errorlevel 1 (
    if defined SYSPY (
      "!SYSPY!" -c "import numpy,PIL,openpyxl,matplotlib" >nul 2>nul && set "PY=!SYSPY!"
    )
  )
)
if not defined PY (
  echo [ERROR] No usable Python found.
  echo         Install Python and run:  pip install numpy pillow openpyxl matplotlib
  echo         Or simply use WorkBuddy instead of this .bat
  pause
  exit /b 1
)
echo   using: %PY%
echo.

echo [1/3] reading screenshots ...
"%PY%" "scripts\shots_to_json.py" "%SHOTS%" -o "%OUT%\timetable_data.json" --week-no "%WNO%" --week "%WRANGE%" --monday "%MON%" --debug
if errorlevel 1 (
  echo [ERROR] step 1 failed, read the message above.
  pause
  exit /b 1
)

echo.
echo [2/3] rendering tables ...
"%PY%" "scripts\render_freetime.py" "%OUT%\timetable_data.json" -o "%OUT%"

echo.
echo [3/3] rendering PDF ...
if exist "%~dp0groups.json" (
  if exist "%~dp0hoods.json" (
    "%PY%" "scripts\render_report_pdf.py" "%OUT%\timetable_data.json" --groups "%~dp0groups.json" --hoods "%~dp0hoods.json" -o "%OUT%"
  ) else (
    "%PY%" "scripts\render_report_pdf.py" "%OUT%\timetable_data.json" --groups "%~dp0groups.json" -o "%OUT%"
  )
) else (
  "%PY%" "scripts\render_report_pdf.py" "%OUT%\timetable_data.json" -o "%OUT%"
)

echo.
echo ==========================================
echo   DONE.  Output folder: %OUT%
echo ==========================================
start "" "%OUT%"
pause
