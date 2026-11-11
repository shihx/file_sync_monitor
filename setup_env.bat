@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PY="%~dp0python\python.exe"

echo === Step 1/3: enable site-packages in embedded python ===
powershell -NoProfile -Command "(Get-Content '%~dp0python\python313._pth') -replace '#import site','import site' | Set-Content '%~dp0python\python313._pth' -Encoding ASCII"

echo === Step 2/3: install pip ===
if not exist "%~dp0python\Scripts\pip.exe" (
    powershell -NoProfile -Command "Invoke-WebRequest -Uri https://bootstrap.pypa.io/get-pip.py -OutFile '%~dp0get-pip.py' -UseBasicParsing"
    %PY% "%~dp0get-pip.py" --no-warn-script-location
)

echo === Step 3/3: install dependencies ===
%PY% -m pip install -r "%~dp0requirements.txt" --no-warn-script-location

echo.
echo === Done. You can run start.bat now. ===
pause
