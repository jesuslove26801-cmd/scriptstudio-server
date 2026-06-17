@echo off
chcp 65001 >nul
title ScriptStudio Grok 로컬 프록시 설치

echo.
echo  ╔══════════════════════════════════════════════╗
echo  ║     ScriptStudio Grok 로컬 프록시 설치       ║
echo  ╚══════════════════════════════════════════════╝
echo.

set INSTALL_DIR=%APPDATA%\scriptstudio\grok-proxy
set NODE_URL=https://nodejs.org/dist/v20.18.0/node-v20.18.0-x64.msi
set PROXY_URL=https://scriptstudio-web.pages.dev/grok-proxy.zip

:: Node.js 확인
where node >nul 2>&1
if %errorlevel% neq 0 (
    echo [1/4] Node.js 설치 중...
    set NODE_MSI=%TEMP%\node_setup.msi
    powershell -Command "Invoke-WebRequest -Uri '%NODE_URL%' -OutFile '%TEMP%\node_setup.msi' -UseBasicParsing"
    msiexec /i "%TEMP%\node_setup.msi" /quiet /norestart
    set "PATH=%PATH%;C:\Program Files\nodejs"
    echo     Node.js 설치 완료
) else (
    echo [1/4] Node.js 이미 설치됨
)

:: 설치 폴더 생성
echo [2/4] 프록시 파일 다운로드 중...
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"

powershell -Command "Invoke-WebRequest -Uri '%PROXY_URL%' -OutFile '%TEMP%\grok-proxy.zip' -UseBasicParsing"
powershell -Command "Expand-Archive -Path '%TEMP%\grok-proxy.zip' -DestinationPath '%INSTALL_DIR%' -Force"
echo     다운로드 완료

:: 서버 시작 스크립트 생성
echo [3/4] 시작 스크립트 생성 중...
(
echo @echo off
echo cd /d "%INSTALL_DIR%"
echo set PORT=3747
echo set XAI_PROXY_API_KEY=ss-grok-local-2026
echo node server.js
) > "%APPDATA%\scriptstudio\start-grok-proxy.bat"

:: 자동 시작 등록 (시작프로그램)
set STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
copy /Y "%APPDATA%\scriptstudio\start-grok-proxy.bat" "%STARTUP_DIR%\ScriptStudio-Grok-Proxy.bat" >nul
echo     시작 등록 완료

:: 즉시 실행
echo [4/4] 프록시 서버 시작 중...
start "" /B cmd /c "cd /d "%INSTALL_DIR%" && set PORT=3747 && set XAI_PROXY_API_KEY=ss-grok-local-2026 && node server.js"

timeout /t 3 /nobreak >nul

:: 상태 확인
powershell -Command "try { $r = Invoke-WebRequest -Uri 'http://localhost:3747/health' -UseBasicParsing; Write-Host '    프록시 실행 확인: OK' } catch { Write-Host '    잠시 후 자동 시작됩니다...' }"

echo.
echo  ✅ 설치 완료! ScriptStudio 웹에서 Grok 로그인 버튼을 클릭하세요.
echo.
pause
