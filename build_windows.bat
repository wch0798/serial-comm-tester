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
".venv-build\Scripts\pyinstaller.exe" --noconfirm --clean --windowed --name SerialTester --add-data "drivers;drivers" --collect-submodules can main.py
if errorlevel 1 goto fail
rem 화면 없는 자동 실행용 (명령 창에서 실행, 결과 코드 0/1/2)
".venv-build\Scripts\pyinstaller.exe" --noconfirm --onefile --console --name SerialTesterCLI --add-data "drivers;drivers" --collect-submodules can autotest.py
if errorlevel 1 goto fail
copy /y "dist\SerialTesterCLI.exe" "dist\SerialTester\" >nul
echo.
echo 완료: dist\SerialTester 폴더를 통째로 다른 PC에 복사해서 SerialTester.exe 를 실행하면 됩니다.
echo       자동 실행(화면 없이 시험)은 같은 폴더의 SerialTesterCLI.exe 를 쓰세요.
echo (그 PC에는 Python이 없어도 됩니다.)
pause
exit /b 0
:fail
echo 실패했습니다. 위의 오류 메시지를 확인하세요.
pause
exit /b 1
