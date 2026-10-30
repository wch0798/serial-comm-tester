"""
실행 파일 만들기 (Windows / macOS / Linux 공통)

  python build.py

지금 쓰는 OS용 실행 파일을 만든다. (다른 OS용은 그 OS에서 실행하거나 GitHub 자동 빌드를 쓴다)
- 화면 프로그램 : SerialTester      (Windows .exe / Mac .app / Linux 실행 파일)
- 자동 실행     : SerialTesterCLI   (화면 없이 시나리오 실행 → 보고서)
결과: release/SerialTester-<OS>-<CPU>.zip (Linux는 .tar.gz)
필요: pip install -r requirements.txt pyinstaller
"""

import os
import platform
import shutil
import subprocess
import sys

NAME = "SerialTester"
CLI = "SerialTesterCLI"
ROOT = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(ROOT, "dist")
WORK = os.path.join(ROOT, "build")
RELEASE = os.path.join(ROOT, "release")

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

GUIDE = {
    "windows": """통신 테스트 도구 - Windows

1. 이 폴더를 원하는 곳에 통째로 풀어 둡니다. (exe 하나만 따로 옮기면 실행되지 않습니다)
2. SerialTester.exe 를 실행합니다.
   - "Windows의 PC 보호" 창이 뜨면 [추가 정보] → [실행]을 누르세요. (서명하지 않은 무료 프로그램이라 뜨는 창입니다)
3. 가상 COM 포트는 연결 방식 "가상 시리얼 (COM 자동 생성)"을 고르고 사용 체크하면 자동으로 만들어집니다.
   (처음 한 번 관리자 권한 확인창과 드라이버 설치 창이 뜹니다)

자동 실행 (화면 없이 시험): 명령 창에서
   SerialTesterCLI.exe 설정파일.json
설정 파일은 이 폴더의 vserial_config.json 에 자동 저장됩니다.
""",
    "macos": """통신 테스트 도구 - macOS

1. SerialTester.app 을 [응용 프로그램] 폴더 등 원하는 곳으로 옮깁니다.
2. 처음 실행할 때 "확인되지 않은 개발자" 경고가 뜨면:
   - Finder에서 SerialTester.app 을 Control+클릭(오른쪽 클릭) → [열기] → [열기]
   - 그래도 안 되면 터미널에서:  xattr -dr com.apple.quarantine SerialTester.app
   (서명하지 않은 무료 프로그램이라 뜨는 경고입니다)
3. 가상 시리얼은 연결 방식 "가상 시리얼 (PTY)"를 쓰면 /tmp/vcom1 같은 경로가 생깁니다.

자동 실행 (화면 없이 시험): 터미널에서
   ./SerialTesterCLI ~/.vserial/vserial_config.json
설정 파일은 ~/.vserial/vserial_config.json 에 자동 저장됩니다.
""",
    "linux": """통신 테스트 도구 - Linux

1. 압축을 풀고 SerialTester 폴더 안의 SerialTester 를 실행합니다.
     ./SerialTester/SerialTester
2. 시리얼 포트(/dev/ttyUSB0 등)를 쓰려면 dialout 그룹 권한이 필요할 수 있습니다:
     sudo usermod -aG dialout $USER   (다시 로그인)
3. 한글이 네모로 보이면 한글 글꼴을 설치하세요:  sudo apt install fonts-noto-cjk

자동 실행 (화면 없이 시험):
   ./SerialTester/SerialTesterCLI 설정파일.json
설정 파일은 프로그램 폴더(쓰기 불가면 ~/.vserial)에 자동 저장됩니다.
""",
}


def os_tag():
    if IS_WIN:
        return "windows"
    if IS_MAC:
        return "macos"
    return "linux"


def cpu_tag():
    m = platform.machine().lower()
    if m in ("x86_64", "amd64"):
        return "x64"
    if m in ("arm64", "aarch64"):
        return "arm64"
    return m or "unknown"


def pyinstaller(*args):
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", DIST, "--workpath", WORK,
           "--specpath", WORK, "--collect-submodules", "can"]
    if IS_WIN:
        cmd += ["--add-data", os.path.join(ROOT, "drivers") + os.pathsep + "drivers"]
    cmd += list(args)
    print(">", " ".join(cmd), flush=True)
    subprocess.check_call(cmd, cwd=ROOT)


def main():
    for d in (DIST, WORK):
        shutil.rmtree(d, ignore_errors=True)
    os.makedirs(RELEASE, exist_ok=True)

    gui = ["--windowed", "--name", NAME]
    if IS_MAC:
        gui += ["--osx-bundle-identifier", "kr.serialtester.app"]
    pyinstaller(*(gui + [os.path.join(ROOT, "main.py")]))
    pyinstaller("--onefile", "--console", "--name", CLI, os.path.join(ROOT, "autotest.py"))

    # 배포 폴더 구성
    tag = "%s-%s-%s" % (NAME, os_tag(), cpu_tag())
    pkg = os.path.join(DIST, "package", NAME)
    shutil.rmtree(os.path.dirname(pkg), ignore_errors=True)
    if IS_MAC:
        os.makedirs(pkg)
        subprocess.check_call(["ditto", os.path.join(DIST, NAME + ".app"), os.path.join(pkg, NAME + ".app")])
    else:
        shutil.copytree(os.path.join(DIST, NAME), pkg, symlinks=True)
    cli = CLI + (".exe" if IS_WIN else "")
    shutil.copy2(os.path.join(DIST, cli), os.path.join(pkg, cli))
    for f in ("README.md", "통신설정_양식.xlsx"):
        shutil.copy2(os.path.join(ROOT, f), os.path.join(pkg, f))
    newline = "\r\n" if IS_WIN else "\n"
    with open(os.path.join(pkg, "실행방법.txt"), "w", encoding="utf-8-sig" if IS_WIN else "utf-8", newline=newline) as f:
        f.write(GUIDE[os_tag()])

    # 압축
    if IS_MAC:
        out = os.path.join(RELEASE, tag + ".zip")
        if os.path.exists(out):
            os.remove(out)
        subprocess.check_call(["ditto", "-c", "-k", "--keepParent", pkg, out])  # .app 안의 링크를 지킨다
    elif IS_WIN:
        out = shutil.make_archive(os.path.join(RELEASE, tag), "zip", os.path.dirname(pkg), NAME)
    else:
        out = shutil.make_archive(os.path.join(RELEASE, tag), "gztar", os.path.dirname(pkg), NAME)
    print("완료:", out, "(%.1f MB)" % (os.path.getsize(out) / 1e6), flush=True)


if __name__ == "__main__":
    main()
