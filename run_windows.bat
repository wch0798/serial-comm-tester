@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist ".venv-win\Scripts\pythonw.exe" goto run

echo 처음 실행: 필요한 구성 요소를 준비합니다. 잠시 기다려 주세요...
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY goto nopython
%PY% -m venv .venv-win
if errorlevel 1 goto nopython
".venv-win\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
  echo pyserial 설치에 실패했습니다. 인터넷 연결을 확인하세요.
  pause
  exit /b 1
)

:run
start "" ".venv-win\Scripts\pythonw.exe" main.py
exit /b 0

:nopython
echo Python이 설치되어 있지 않습니다.
echo https://www.python.org/downloads/ 에서 Python을 설치하세요.
echo 설치 첫 화면에서 "Add python.exe to PATH"를 꼭 체크하세요.
pause
exit /b 1
