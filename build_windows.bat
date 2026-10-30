@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 실행 파일(.exe)을 만듭니다. 이 PC에 Python이 설치되어 있어야 합니다.
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo Python이 없습니다. https://www.python.org/downloads/ 에서 설치하세요.
  pause
  exit /b 1
)
if not exist ".venv-build\Scripts\python.exe" %PY% -m venv .venv-build
".venv-build\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt pyinstaller
if errorlevel 1 goto fail
".venv-build\Scripts\python.exe" build.py
if errorlevel 1 goto fail
echo.
echo 완료: release 폴더의 SerialTester-windows-x64.zip 을 다른 PC에 풀어서 SerialTester.exe 를 실행하면 됩니다.
echo       (dist\package\SerialTester 폴더가 압축 전 내용입니다. 자동 실행은 SerialTesterCLI.exe)
echo (그 PC에는 Python이 없어도 됩니다.)
pause
exit /b 0
:fail
echo 실패했습니다. 위의 오류 메시지를 확인하세요.
pause
exit /b 1
