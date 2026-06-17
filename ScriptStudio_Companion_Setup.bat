@echo off
title ScriptStudio Companion Setup

echo.
echo =================================================
echo   ScriptStudio Companion Setup
echo =================================================
echo.
echo   Enables: CapCut export, Premiere, ChatMock
echo   Install path: %LOCALAPPDATA%\ScriptStudio\
echo   Size: ~8MB
echo.
pause

echo.
echo   Stopping old Companion if running...
taskkill /F /IM ScriptStudioCompanion.exe >nul 2>&1
timeout /t 1 >nul

echo.
echo [1/3] Downloading Companion...
if not exist "%LOCALAPPDATA%\ScriptStudio" mkdir "%LOCALAPPDATA%\ScriptStudio"
curl.exe -L --output "%LOCALAPPDATA%\ScriptStudio\ScriptStudioCompanion.exe" "https://scriptstudio-web.pages.dev/ScriptStudioCompanion.exe"
if errorlevel 1 goto DOWNLOAD_ERR
echo   Done.
goto AFTER_DOWNLOAD

:DOWNLOAD_ERR
echo ERROR: Download failed. Check internet connection.
pause
exit /b 1

:AFTER_DOWNLOAD
echo.
echo [2/3] Unblocking file...
PowerShell -ExecutionPolicy Bypass -NoProfile -Command "Unblock-File '%LOCALAPPDATA%\ScriptStudio\ScriptStudioCompanion.exe'"
echo   Done.

echo.
echo [3/3] Registering autostart...
PowerShell -ExecutionPolicy Bypass -NoProfile -Command "Set-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'ScriptStudioCompanion' -Value '%LOCALAPPDATA%\ScriptStudio\ScriptStudioCompanion.exe'"
echo   Done. (Runs automatically at Windows startup)

echo.
echo   Starting Companion...
start "" "%LOCALAPPDATA%\ScriptStudio\ScriptStudioCompanion.exe"

echo.
echo =================================================
echo   Setup Complete!
echo   Return to browser and click [Start] button
echo =================================================
echo.
pause
