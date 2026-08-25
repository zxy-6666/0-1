@echo off
setlocal
cd /d "%~dp0"

set "KIT=%~dp0"
set "SRC=%KIT%src"
set "BPY=%KIT%build\py"

echo ============================================================
echo  Schedule App - Windows one-click build to standalone exe
echo  End users do NOT need Python installed.
echo ============================================================
echo.

echo [1/6] Preparing embedded Python 3.11 (download ~10MB first time) ...
if not exist "%BPY%\python.exe" (
    if not exist "%BPY%" mkdir "%BPY%"
    curl -L --fail -o "%BPY%\py.zip" https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip
    if errorlevel 1 (
        echo [ERROR] failed to download Python, check network.
        pause
        exit /b 1
    )
    powershell -NoProfile -Command "Expand-Archive -Path '%BPY%\py.zip' -DestinationPath '%BPY%' -Force"
    if errorlevel 1 (
        echo [ERROR] failed to unzip Python.
        pause
        exit /b 1
    )
    del /q "%BPY%\py.zip" 2>nul
    > "%BPY%\python311._pth" echo python311.zip
    >>"%BPY%\python311._pth" echo .
    >>"%BPY%\python311._pth" echo Lib/site-packages
    >>"%BPY%\python311._pth" echo.
    >>"%BPY%\python311._pth" echo import site
)

echo [2/6] Preparing pip ...
"%BPY%\python.exe" -m pip --version >nul 2>nul
if errorlevel 1 (
    curl -L --fail -o "%KIT%get-pip.py" https://bootstrap.pypa.io/get-pip.py
    if errorlevel 1 (
        echo [ERROR] failed to download pip, check network.
        pause
        exit /b 1
    )
    "%BPY%\python.exe" "%KIT%get-pip.py"
    if errorlevel 1 (
        echo [ERROR] pip bootstrap failed.
        pause
        exit /b 1
    )
)

echo [3/6] Installing dependencies (flask pandas matplotlib pyinstaller) ...
"%BPY%\python.exe" -m pip install --disable-pip-version-check -q flask pandas openpyxl matplotlib pyinstaller
if errorlevel 1 (
    echo [ERROR] dependency install failed, check network.
    pause
    exit /b 1
)

echo [4/6] Building exe with PyInstaller (first run takes 2-5 min) ...
if exist "%KIT%dist" rmdir /s /q "%KIT%dist"
if exist "%KIT%build\work" rmdir /s /q "%KIT%build\work"
"%BPY%\python.exe" -m PyInstaller --noconfirm --clean --workpath "%KIT%build\work" --distpath "%KIT%dist" --onefile --windowed --name schedule_app --paths "%SRC%" --add-data "%SRC%\web\templates;templates" --hidden-import data_loader --hidden-import models --hidden-import scheduler --hidden-import optimizer --hidden-import optimizer_config --hidden-import validation --hidden-import visualization --hidden-import snapshot_store --hidden-import outputs --hidden-import health_check --hidden-import flow_importer --hidden-import paths "%SRC%\web\app.py"
if not exist "%KIT%dist\schedule_app.exe" (
    echo [ERROR] build failed, no exe produced. See messages above.
    pause
    exit /b 1
)

echo [5/6] Assembling release folder ...
set "OUT=%KIT%publish\schedule_app_win"
if exist "%KIT%publish" rmdir /s /q "%KIT%publish"
mkdir "%OUT%"
copy /y "%KIT%dist\schedule_app.exe" "%OUT%\" >nul
xcopy /e /y /i "%KIT%data" "%OUT%\data\" >nul
if exist "%KIT%README.txt" copy /y "%KIT%README.txt" "%OUT%\" >nul
if exist "%KIT%SOP*.md" copy /y "%KIT%SOP*.md" "%OUT%\" >nul
if exist "%KIT%SOP*.docx" copy /y "%KIT%SOP*.docx" "%OUT%\" >nul
if exist "%KIT%*.md" copy /y "%KIT%*.md" "%OUT%\" >nul
if not exist "%OUT%\output" mkdir "%OUT%\output"

echo [6/6] Creating zip ...
powershell -NoProfile -Command "Compress-Archive -Path '%OUT%' -DestinationPath '%KIT%schedule_app_windows.zip' -Force"

echo.
echo ============================================================
echo  DONE!
echo    exe : %OUT%\schedule_app.exe
echo    zip : %KIT%schedule_app_windows.zip
echo  Send the publish folder to end users; no Python needed.
echo ============================================================
echo.
pause
exit /b 0
