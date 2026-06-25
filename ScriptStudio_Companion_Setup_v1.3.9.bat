@echo off
set "INSTALL_DIR=%LOCALAPPDATA%\ScriptStudio"
set "EXE_NAME=ScriptStudioCompanion_v1.3.9.exe"
set "DOWNLOAD_URL=https://scriptstudio-web.pages.dev/ScriptStudioCompanion_v1.3.9.exe"
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"
echo Stopping all Companion versions...
wmic process where "name like 'ScriptStudioCompanion%%'" delete >nul 2>&1
timeout /t 2 /nobreak >nul
if exist "%INSTALL_DIR%\%EXE_NAME%" del "%INSTALL_DIR%\%EXE_NAME%"
echo Downloading %EXE_NAME%...
powershell -Command "Invoke-WebRequest -Uri '%DOWNLOAD_URL%' -OutFile '%INSTALL_DIR%\%EXE_NAME%' -UseBasicParsing"
if not exist "%INSTALL_DIR%\%EXE_NAME%" (
    echo Download failed. Check your internet connection.
    pause
    exit /b 1
)
echo Adding to startup...
powershell -Command "& { $s=(New-Object -COM WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Startup')+'\ScriptStudioCompanion.lnk'); $s.TargetPath='%INSTALL_DIR%\%EXE_NAME%'; $s.Save() }"
echo Starting Companion v1.3.9...
start "" "%INSTALL_DIR%\%EXE_NAME%"
echo.
echo Done! Companion v1.3.9 is running in background.
pause
