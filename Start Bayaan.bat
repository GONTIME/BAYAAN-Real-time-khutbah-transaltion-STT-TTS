@echo off
rem ===========================================================================
rem  BAYAAN - double-click launcher for the Control Panel
rem  Opens the panel in your browser. Start/stop Bayaan and edit settings there.
rem ===========================================================================
setlocal enabledelayedexpansion
title Bayaan Control Panel
cd /d "%~dp0"

echo.
echo  ============================================================
echo   BAYAAN - Control Panel launcher
echo  ============================================================
echo.

rem ---- 1. Find a Python that can run the panel ----
set "PYEXE="
if exist "%~dp0backend\.venv\Scripts\python.exe" set "PYEXE=%~dp0backend\.venv\Scripts\python.exe"
if not defined PYEXE if exist "%~dp0.venv\Scripts\python.exe" set "PYEXE=%~dp0.venv\Scripts\python.exe"
if not defined PYEXE (
    for /f "delims=" %%i in ('where python 2^>nul') do (
        if not defined PYEXE set "PYEXE=%%i"
    )
)
if not defined PYEXE (
    echo  [X] Python was not found.
    echo      Install Python 3.11 or newer from https://www.python.org/downloads/
    echo      and tick "Add python.exe to PATH" during installation.
    echo.
    pause
    exit /b 1
)

rem ---- 2. Make sure the panel script exists ----
if not exist "%~dp0backend\tools\control_panel.py" (
    echo  [X] Could not find "backend\tools\control_panel.py".
    echo      Make sure this file was copied together with the rest of BAYAAN.
    echo.
    pause
    exit /b 1
)

rem ---- 3. Launch the panel ----
echo  Starting the Control Panel...
echo  Keep this black window open while you use BAYAAN.
echo  (Closing it closes the panel only - servers keep running until stopped.)
echo.
"%PYEXE%" "%~dp0backend\tools\control_panel.py" %*
set "EXITCODE=%ERRORLEVEL%"

echo.
if "%EXITCODE%"=="0" (
    echo  The Control Panel closed. Bayaan servers are not affected.
) else (
    echo  The Control Panel exited with code %EXITCODE%.
    echo.
    echo  Most common fix - the panel port 8765 is already in use:
    echo    * Close other browser tabs pointing at 127.0.0.1:8765, or
    echo    * start again after a minute.
    echo  Other problems will show a message above - keep this window open.
)
pause
endlocal