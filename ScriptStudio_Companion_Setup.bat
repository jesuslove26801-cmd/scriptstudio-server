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
echo   [4/4] Grok 로컬 프록시 설치 중...
echo =================================================
echo.

set GROK_DIR=%APPDATA%\scriptstudio\grok-proxy

:: Node.js 확인
where node >nul 2>&1
if %errorlevel% neq 0 (
    echo   Node.js 다운로드 중... (약 30MB, 잠시 기다려주세요)
    curl.exe -L --output "%TEMP%\node_setup.msi" "https://nodejs.org/dist/v20.18.0/node-v20.18.0-x64.msi"
    msiexec /i "%TEMP%\node_setup.msi" /quiet /norestart
    set "PATH=%PATH%;C:\Program Files\nodejs"
    echo   Node.js 설치 완료
) else (
    echo   Node.js 이미 설치됨
)

:: grok-proxy 다운로드
if not exist "%GROK_DIR%" mkdir "%GROK_DIR%"
echo   Grok 프록시 다운로드 중...
curl.exe -L --output "%TEMP%\grok-proxy.zip" "https://scriptstudio-web.pages.dev/grok-proxy.zip"
PowerShell -ExecutionPolicy Bypass -NoProfile -Command "Expand-Archive -Path '%TEMP%\grok-proxy.zip' -DestinationPath '%GROK_DIR%' -Force"
echo   다운로드 완료

:: 시작 스크립트 등록
set STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
(
echo @echo off
echo cd /d "%GROK_DIR%"
echo set PORT=3747
echo set XAI_PROXY_API_KEY=ss-grok-local-2026
echo node server.js
) > "%APPDATA%\scriptstudio\start-grok-proxy.bat"
copy /Y "%APPDATA%\scriptstudio\start-grok-proxy.bat" "%STARTUP_DIR%\ScriptStudio-Grok-Proxy.bat" >nul

:: 즉시 실행
start "" /B cmd /c "cd /d "%GROK_DIR%" && set PORT=3747 && set XAI_PROXY_API_KEY=ss-grok-local-2026 && node server.js"
echo   Grok 프록시 백그라운드 실행 완료

echo.
echo =================================================
echo   설치 완료!
echo   브라우저로 돌아가서 [Grok 로그인] 버튼을 클릭하세요.
echo =================================================
echo.
pause
