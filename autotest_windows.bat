@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 화면 없이 시나리오 자동 실행 → reports 폴더에 엑셀 보고서 저장
rem 사용법: autotest_windows.bat                     (vserial_config.json 으로 실행)
rem         autotest_windows.bat 설정.xlsx --repeat 10 --tab 시험기
if not exist ".venv-win\Scripts\python.exe" (
  echo 먼저 run_windows.bat 를 한 번 실행해서 준비하세요.
  exit /b 2
)
if "%~1"=="" (
  ".venv-win\Scripts\python.exe" autotest.py vserial_config.json
) else (
  ".venv-win\Scripts\python.exe" autotest.py %*
)
set "RC=%ERRORLEVEL%"
echo 결과 코드: %RC%  (0=합격, 1=불합격, 2=설정/연결 오류)
exit /b %RC%
