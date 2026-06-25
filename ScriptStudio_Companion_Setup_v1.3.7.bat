@echo off
chcp 65001 >nul
echo ScriptStudio Companion v1.3.7 설치 프로그램
echo ================================================
echo.

set "INSTALL_DIR=%LOCALAPPDATA%\ScriptStudio"
set "EXE_NAME=ScriptStudioCompanion_v1.3.7.exe"
set "DOWNLOAD_URL=https://scriptstudio-web.pages.dev/ScriptStudioCompanion_v1.3.7.exe"

if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"

echo 다운로드 중: %EXE_NAME%
powershell -Command "Invoke-WebRequest -Uri '%DOWNLOAD_URL%' -OutFile '%INSTALL_DIR%\%EXE_NAME%' -UseBasicParsing"

if not exist "%INSTALL_DIR%\%EXE_NAME%" (
    echo 다운로드 실패. 인터넷 연결을 확인하세요.
    pause
    exit /b 1
)

echo 시작 프로그램에 등록 중...
powershell -Command "& { $s=(New-Object -COM WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Startup')+'\ScriptStudioCompanion.lnk'); $s.TargetPath='%INSTALL_DIR%\%EXE_NAME%'; $s.Save() }"

echo 실행 중...
start "" "%INSTALL_DIR%\%EXE_NAME%"

echo.
echo 설치 완료! Companion이 백그라운드에서 실행됩니다.
echo PC 재시작 시 자동으로 실행됩니다.
pause
