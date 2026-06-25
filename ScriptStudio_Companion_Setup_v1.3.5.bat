@echo off
chcp 437 >nul
title ScriptStudio Companion Setup

echo.
echo =================================================
echo   ScriptStudio Companion Setup v1.3.5
echo =================================================
echo.
echo   Enables: CapCut export, ChatMock, Grok, Google Flow
echo   Install path: D:\ScriptStudio\Companion\
echo   Size: ~8MB
echo.
pause

echo.
echo   Stopping old Companion if running...
taskkill /F /IM ScriptStudioCompanion.exe >nul 2>&1
timeout /t 1 >nul

echo.
echo [1/4] Downloading Companion...
if not exist "D:\ScriptStudio\Companion" mkdir "D:\ScriptStudio\Companion"
curl.exe -L --output "D:\ScriptStudio\Companion\ScriptStudioCompanion.exe" "https://scriptstudio-web.pages.dev/ScriptStudioCompanion_v1.3.5.exe"
if errorlevel 1 goto DOWNLOAD_ERR
echo   Done.
goto AFTER_DOWNLOAD

:DOWNLOAD_ERR
echo ERROR: Download failed. Check internet connection.
pause
exit /b 1

:AFTER_DOWNLOAD
echo.
echo [2/4] Unblocking file...
PowerShell -ExecutionPolicy Bypass -NoProfile -Command "Unblock-File 'D:\ScriptStudio\Companion\ScriptStudioCompanion.exe'"
echo   Done.

echo.
echo [3/4] Registering autostart...
PowerShell -ExecutionPolicy Bypass -NoProfile -Command "Set-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'ScriptStudioCompanion' -Value 'D:\ScriptStudio\Companion\ScriptStudioCompanion.exe'"
echo   Done.

echo.
echo   Starting Companion...
start "" "D:\ScriptStudio\Companion\ScriptStudioCompanion.exe"

echo.
echo =================================================
echo   [4/4] Installing Grok Local Proxy...
echo =================================================
echo.

set GROK_DIR=%APPDATA%\scriptstudio\grok-proxy

where node >nul 2>&1
if %errorlevel% neq 0 (
    echo   Downloading Node.js... (30MB, please wait)
    curl.exe -L --output "%TEMP%\node_setup.msi" "https://nodejs.org/dist/v20.18.0/node-v20.18.0-x64.msi"
    msiexec /i "%TEMP%\node_setup.msi" /quiet /norestart
    set "PATH=%PATH%;C:\Program Files\nodejs"
    echo   Node.js installed.
) else (
    echo   Node.js already installed.
)

if not exist "%GROK_DIR%" mkdir "%GROK_DIR%"
echo   Downloading Grok proxy...
curl.exe -L --output "%TEMP%\grok-proxy.zip" "https://scriptstudio-web.pages.dev/grok-proxy.zip"
PowerShell -ExecutionPolicy Bypass -NoProfile -Command "Expand-Archive -Path '%TEMP%\grok-proxy.zip' -DestinationPath '%GROK_DIR%' -Force"
echo   Download complete.

set STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
(
echo @echo off
echo cd /d "%GROK_DIR%"
echo set PORT=3747
echo set XAI_PROXY_API_KEY=ss-grok-local-2026
echo node server.js
) > "%APPDATA%\scriptstudio\start-grok-proxy.bat"
copy /Y "%APPDATA%\scriptstudio\start-grok-proxy.bat" "%STARTUP_DIR%\ScriptStudio-Grok-Proxy.bat" >nul

start "" /B cmd /c "cd /d "%GROK_DIR%" && set PORT=3747 && set XAI_PROXY_API_KEY=ss-grok-local-2026 && node server.js"
echo   Grok proxy started.

PowerShell -ExecutionPolicy Bypass -NoProfile -Command "Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.MessageBox]::Show('Companion v1.3.5 installed successfully!`n`nCompanion is running in the background.`nGoogle Flow login is now automatic.`n`nPC will auto-start Companion on next boot.', 'Setup Complete!', 'OK', [System.Windows.Forms.MessageBoxIcon]::Information)"
