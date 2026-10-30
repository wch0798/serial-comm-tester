"""
Windows 가상 COM 포트 관리 (com0com)

- com0com 드라이버는 drivers/com0com/setup.exe 로 함께 배포하고, 처음 쓸 때 자동으로 설치한다.
- 가상 포트는 "쌍"으로 만들어진다. A쪽(예: COM10)은 다른 프로그램이 열고,
  B쪽(예: CNCB0)은 이 프로그램이 연다. A로 보낸 데이터는 B로, B로 보낸 데이터는 A로 나온다.
- 드라이버 설치, 포트 추가/삭제는 관리자 권한이 필요하다. 여러 작업을 명령 스크립트 하나로 묶어서
  관리자 권한 확인창(UAC)이 한 번만 뜨게 한다.
- 포트 목록 읽기와 상태 진단은 관리자 권한 없이 동작한다.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time

IS_WINDOWS = sys.platform.startswith("win")
BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
BUNDLED_SETUP = os.path.join(BASE_DIR, "drivers", "com0com", "setup.exe")

PARAMS_KEY = r"SYSTEM\CurrentControlSet\Services\com0com\Parameters"
SERIALCOMM_KEY = r"HARDWARE\DEVICEMAP\SERIALCOMM"
CREATE_NO_WINDOW = 0x08000000
SW_SHOWMINNOACTIVE = 7
ERROR_CANCELLED = 1223

NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,15}$")

BLOCKED_HELP = (
    "해결 방법 (모두 무료):\n"
    "1) TCP로 시험할 수 있으면 연결 방식을 TCP 서버/클라이언트로 바꾸세요. 드라이버가 필요 없습니다.\n"
    "2) 테스트 전용 PC라면 Windows '테스트 모드'를 켜면 드라이버가 동작합니다.\n"
    "   관리자 명령 프롬프트에서: bcdedit /set testsigning on  → 재부팅\n"
    "   (보안 부팅을 꺼야 할 수 있고, 화면에 '테스트 모드' 글자가 표시됩니다. 회사 보안 규정을 먼저 확인하세요.)\n"
    "3) 메모리 무결성 때문이라면: Windows 보안 → 장치 보안 → 코어 격리 → 메모리 무결성 끄기 → 재부팅\n"
    "4) USB-시리얼 케이블 2개를 서로 연결(TX↔RX, GND↔GND)하면 드라이버 문제 없이 확실하게 동작합니다."
)


class VPortError(Exception):
    pass


# ---------------------------------------------------------------------------
# 설치 상태
# ---------------------------------------------------------------------------

def install_dir():
    pf = os.environ.get("ProgramW6432") or os.environ.get("ProgramFiles") or r"C:\Program Files"
    return os.path.join(pf, "com0com")


def find_setupc():
    dirs = [install_dir()]
    if os.environ.get("ProgramFiles(x86)"):
        dirs.append(os.path.join(os.environ["ProgramFiles(x86)"], "com0com"))
    for d in dirs:
        p = os.path.join(d, "setupc.exe")
        if os.path.isfile(p):
            return p
    return None


def is_installed():
    return find_setupc() is not None


def check_platform():
    if not IS_WINDOWS:
        raise VPortError("Windows 전용 기능입니다.")
    arch = (os.environ.get("PROCESSOR_ARCHITEW6432") or os.environ.get("PROCESSOR_ARCHITECTURE") or "").upper()
    if arch != "AMD64":
        raise VPortError("함께 들어 있는 com0com은 64비트(x64) Windows 전용입니다. (현재: %s)" % (arch or "알 수 없음"))


def check_name(name):
    if not NAME_RE.match(name or ""):
        raise VPortError("포트 이름이 올바르지 않습니다: %r (예: COM10)" % name)
    return name.upper()


# ---------------------------------------------------------------------------
# 목록 읽기 (관리자 권한 불필요)
# ---------------------------------------------------------------------------

def _reg():
    import winreg
    return winreg


def list_pairs():
    """
    [{n, a, b}] 형태로 com0com 포트 쌍 목록을 돌려준다.
    a, b 는 각 쪽의 이름이다. 이름을 따로 정하지 않은 쪽은 기본 이름(CNCA0, CNCB0 …)이다.
    """
    winreg = _reg()
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, PARAMS_KEY)
    except OSError:
        return []
    found = {}
    with key:
        i = 0
        while True:
            try:
                sub = winreg.EnumKey(key, i)
            except OSError:
                break
            i += 1
            m = re.match(r"^CNC([AB])(\d+)$", sub, re.I)
            if not m:
                continue
            side, n = m.group(1).lower(), int(m.group(2))
            name = sub.upper()
            try:
                with winreg.OpenKey(key, sub) as k:
                    for value_name in ("RealPortName", "PortName"):
                        try:
                            v, _ = winreg.QueryValueEx(k, value_name)
                        except OSError:
                            continue
                        if v and v not in ("-", "COM#"):
                            name = str(v).upper()
                            break
            except OSError:
                pass
            found.setdefault(n, {"n": n})[side] = name
    pairs = []
    for n in sorted(found):
        p = found[n]
        p.setdefault("a", "CNCA%d" % n)
        p.setdefault("b", "CNCB%d" % n)
        pairs.append(p)
    return pairs


def find_pair(name):
    name = name.upper()
    for p in list_pairs():
        if name in (p["a"], p["b"]):
            return p
    return None


def system_com_names():
    """현재 Windows에 있는 COM 포트 이름들 (실제 + 가상)"""
    winreg = _reg()
    names = set()
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, SERIALCOMM_KEY) as key:
            i = 0
            while True:
                try:
                    _, v, _ = winreg.EnumValue(key, i)
                except OSError:
                    break
                i += 1
                names.add(str(v).upper())
    except OSError:
        pass
    return names


def device_path(name):
    """이 프로그램이 포트를 열 때 쓰는 경로"""
    return "\\\\.\\" + name


# ---------------------------------------------------------------------------
# 관리자 권한 실행
# ---------------------------------------------------------------------------

def is_admin():
    import ctypes
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _shell_execute_runas(path, timeout):
    import ctypes
    from ctypes import wintypes

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
                    ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR),
                    ("lpParameters", wintypes.LPCWSTR), ("lpDirectory", wintypes.LPCWSTR),
                    ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
                    ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY), ("dwHotKey", wintypes.DWORD),
                    ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x00000040  # SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = path
    info.lpDirectory = os.path.dirname(path)
    info.nShow = SW_SHOWMINNOACTIVE
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        err = ctypes.get_last_error()
        if err == ERROR_CANCELLED:
            raise VPortError("관리자 권한 확인이 취소되었습니다.")
        raise VPortError("관리자 권한으로 실행하지 못했습니다. (오류 %d)" % err)
    try:
        if kernel32.WaitForSingleObject(info.hProcess, int(timeout * 1000)) != 0:
            raise VPortError("작업이 너무 오래 걸려 중단했습니다.")
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
        return code.value
    finally:
        kernel32.CloseHandle(info.hProcess)


def run_admin(commands, timeout=300):
    """
    명령들을 관리자 권한 스크립트 하나로 실행한다. (UAC 확인창 1번)
    반환: (종료 코드, 출력 내용)
    """
    work = tempfile.mkdtemp(prefix="vserial_")
    log = os.path.join(work, "log.txt")
    script = os.path.join(work, "run.cmd")
    lines = ["@echo off", "set RC=0"]
    for c in commands:
        lines.append('%s >> "%s" 2>&1' % (c, log))
        lines.append("if errorlevel 1 set RC=%errorlevel%")
    lines.append("exit /b %RC%")
    try:
        with open(script, "w", encoding="oem", newline="\r\n") as f:
            f.write("\n".join(lines) + "\n")
    except UnicodeEncodeError:
        raise VPortError("경로에 명령 프롬프트가 처리할 수 없는 문자가 있습니다. 프로그램을 영문 경로로 옮겨 주세요.")

    if is_admin():
        rc = subprocess.run(["cmd", "/c", script], creationflags=CREATE_NO_WINDOW, timeout=timeout).returncode
    else:
        rc = _shell_execute_runas(script, timeout)
    out = ""
    if os.path.exists(log):
        with open(log, "rb") as f:
            out = f.read().decode("oem", errors="replace").strip()
    return rc, out


# ---------------------------------------------------------------------------
# 설치 / 포트 추가·삭제
# ---------------------------------------------------------------------------

def _install_commands():
    if not os.path.isfile(BUNDLED_SETUP):
        raise VPortError("함께 들어 있어야 할 com0com 설치 파일이 없습니다:\n%s" % BUNDLED_SETUP)
    # NSIS 설치 파일: /S = 조용히 설치, /D = 설치 폴더 (반드시 마지막, 따옴표 없이)
    return ['start "" /wait "%s" /S /D=%s' % (BUNDLED_SETUP, install_dir())]


def _setupc_commands(*args):
    d = install_dir()
    return ['cd /d "%s"' % d, '"%s" --silent %s' % (os.path.join(d, "setupc.exe"), " ".join(args))]


def _wait_installed(out):
    for _ in range(20):
        if is_installed():
            return
        time.sleep(0.5)
    raise VPortError("com0com 설치를 확인하지 못했습니다.\n%s" % out)


def install_driver():
    check_platform()
    rc, out = run_admin(_install_commands())
    _wait_installed(out)


def uninstall_driver():
    check_platform()
    d = os.path.dirname(find_setupc() or os.path.join(install_dir(), "x"))
    uninst = os.path.join(d, "uninstall.exe")
    if not os.path.isfile(uninst):
        raise VPortError("com0com이 설치되어 있지 않습니다.")
    # _?= : 제거 프로그램이 자기 복사본을 따로 띄우지 않고 끝날 때까지 기다리게 한다
    run_admin(['"%s" /S _?=%s' % (uninst, d)])


def _pair_params(name):
    return "PortName=%s" % name if name and name != "-" else "-"


def create_pair(a_name, b_name=""):
    """
    포트 쌍을 만든다. 이름을 비우면 기본 이름(CNCAn/CNCBn)이 된다.
    드라이버가 없으면 같은 관리자 작업 안에서 설치까지 한다.
    """
    check_platform()
    a = check_name(a_name) if a_name else ""
    b = check_name(b_name) if b_name else ""
    if a and a == b:
        raise VPortError("두 포트 이름이 같습니다.")
    busy = system_com_names()
    pairs = list_pairs()
    for n in (a, b):
        if n and (n in busy or any(n in (p["a"], p["b"]) for p in pairs)):
            raise VPortError("%s 은(는) 이미 사용 중인 이름입니다." % n)
    before = {p["n"] for p in pairs}
    cmds = [] if is_installed() else _install_commands()
    cmds += _setupc_commands("install", _pair_params(a), _pair_params(b))
    rc, out = run_admin(cmds)
    _wait_installed(out)
    for _ in range(20):
        new = [p for p in list_pairs() if p["n"] not in before]
        if new:
            return new[0]
        time.sleep(0.5)
    raise VPortError("포트 쌍을 만들지 못했습니다.\n%s" % (out or "(출력 없음)"))


def remove_pair(n):
    check_platform()
    if not is_installed():
        raise VPortError("com0com이 설치되어 있지 않습니다.")
    rc, out = run_admin(_setupc_commands("remove", str(int(n))))
    if any(p["n"] == n for p in list_pairs()):
        raise VPortError("포트 쌍을 삭제하지 못했습니다.\n%s" % out)


def ensure_port(name):
    """
    다른 프로그램이 열 포트 이름(name, 예: COM10)을 준비하고,
    이 프로그램이 열어야 할 반대쪽 경로를 돌려준다.
    반환: (이 프로그램이 열 경로, 쌍 정보, 새로 만들었는지)
    """
    check_platform()
    name = check_name(name)
    pair = find_pair(name)
    created = False
    if pair is None:
        pair = create_pair(name, "")
        created = True
    other = pair["b"] if pair["a"] == name else pair["a"]
    path = device_path(other)
    wait_openable(path)
    return path, pair, created


def wait_openable(path, seconds=10):
    """새로 만든 포트는 준비되는 데 잠시 걸린다. 열릴 때까지 기다리고, 끝내 안 열리면 원인을 진단한다."""
    import serial
    last = None
    end = time.time() + seconds
    while time.time() < end:
        try:
            serial.Serial(path).close()
            return
        except Exception as e:
            last = e
            if "Access is denied" in str(e) or "PermissionError" in repr(e):
                return  # 이미 다른 곳에서 열려 있음 → 장치 자체는 정상
            time.sleep(0.5)
    ok, lines = diagnose()
    msg = "가상 포트를 열 수 없습니다: %s\n\n%s" % (last, "\n".join(lines))
    if not ok:
        msg += "\n\n" + BLOCKED_HELP
    raise VPortError(msg)


# ---------------------------------------------------------------------------
# 진단 (관리자 권한 불필요)
# ---------------------------------------------------------------------------

PROBLEM_TEXT = {
    52: "Windows가 드라이버 서명을 신뢰하지 않아 막았습니다 (코드 52).",
    39: "드라이버를 불러오지 못했습니다 (코드 39). 메모리 무결성(코어 격리) 또는 보안 정책 때문일 수 있습니다.",
    28: "드라이버가 설치되지 않았습니다 (코드 28).",
    10: "장치를 시작할 수 없습니다 (코드 10).",
    22: "장치가 사용 안 함으로 설정되어 있습니다 (코드 22). 장치 관리자에서 사용으로 바꾸세요.",
}


def _problem_code(v):
    if isinstance(v, int):
        return v
    s = str(v or "").upper()
    if "UNSIGNED" in s:
        return 52
    if "FAILED_LOAD" in s or "DRIVER_FAILED" in s:
        return 39
    m = re.search(r"\d+", s)
    return int(m.group()) if m else (0 if s in ("", "CM_PROB_NONE", "NONE") else -1)


def diagnose():
    """반환: (정상 여부, 설명 줄 목록)"""
    if not IS_WINDOWS:
        return False, ["Windows 전용 기능입니다."]
    lines = []
    setupc = find_setupc()
    lines.append("드라이버 설치: %s" % (os.path.dirname(setupc) if setupc else "안 됨"))
    if not setupc:
        return False, lines
    ps = ("Get-PnpDevice -PresentOnly | Where-Object { $_.FriendlyName -like '*com0com*' } | "
          "Select-Object FriendlyName,Status,Problem | ConvertTo-Json -Compress")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, timeout=40, creationflags=CREATE_NO_WINDOW)
        text = r.stdout.decode("utf-8", errors="replace").strip() or r.stdout.decode("oem", errors="replace").strip()
        devices = json.loads(text) if text else []
    except Exception as e:
        lines.append("장치 상태를 읽지 못했습니다: %s" % e)
        return False, lines
    if isinstance(devices, dict):
        devices = [devices]
    if not devices:
        lines.append("com0com 장치가 없습니다. 포트 쌍을 추가하세요.")
        return True, lines
    ok = True
    for d in devices:
        code = _problem_code(d.get("Problem"))
        status = str(d.get("Status", ""))
        if status.upper() == "OK" and code in (0, -1):
            lines.append("정상: %s" % d.get("FriendlyName"))
        else:
            ok = False
            lines.append("문제: %s → %s" % (d.get("FriendlyName"),
                                            PROBLEM_TEXT.get(code, "상태 %s, 문제 코드 %s" % (status, code))))
    return ok, lines
