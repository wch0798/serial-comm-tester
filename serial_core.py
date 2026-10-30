"""
통신 엔진 (시리얼 / 가상 시리얼 / TCP)

연결 방식
- PTY (Linux / macOS): os.openpty()로 가상 시리얼 포트를 직접 만든다. 드라이버가 필요 없다.
  다른 프로그램은 장치 경로(/dev/pts/N, /dev/ttysNNN) 또는 링크 경로로 접속한다.
- SERIAL: pyserial로 기존 포트를 연다. (실제 장치, 또는 Windows에서 VSPE/com0com 등으로 만든 가상 포트)
- TCP 서버: 지정한 포트에서 접속을 기다린다. 여러 클라이언트가 동시에 접속할 수 있다.
- TCP 클라이언트: 지정한 주소로 접속한다.

수신 데이터는 처리기(handler)로 넘어가고, 처리기가 돌려준 응답을 송신한다.
- RawHandler: "이 데이터가 오면 → 이 데이터를 보낸다" 규칙
- modbus.SlaveHandler / modbus.MasterHandler: Modbus RTU / TCP
"""

import os
import random
import select
import socket
import sys
import threading
import time

try:
    import serial
    import serial.tools.list_ports
except ImportError:  # PTY / TCP만 쓸 때는 pyserial 없이도 동작
    serial = None

import checks

IS_WINDOWS = sys.platform.startswith("win")
PTY_SUPPORTED = not IS_WINDOWS

CONN_PTY = "pty"
CONN_VCOM = "vcom"  # Windows 가상 시리얼 (com0com)
CONN_SERIAL = "serial"
CONN_TCP_SERVER = "tcp_server"
CONN_TCP_CLIENT = "tcp_client"
CONN_UDP = "udp"
CONN_CAN = "can"
TCP_CONNS = (CONN_TCP_SERVER, CONN_TCP_CLIENT, CONN_UDP)  # 네트워크 연결 (Modbus 기본 프레임: TCP)

FMT_TEXT = "TEXT"
FMT_HEX = "HEX"
FMT_CAN = "CAN"  # CAN 프레임: 123#01 02 03  (여러 개는 ; 로 구분)


# ---------------------------------------------------------------------------
# 데이터 변환
# ---------------------------------------------------------------------------

_ESCAPES = {"n": b"\n", "r": b"\r", "t": b"\t", "0": b"\x00", "\\": b"\\"}


def parse_text(s):
    r"""텍스트를 bytes로 변환. \r \n \t \0 \\ \xNN 이스케이프를 지원한다."""
    out = bytearray()
    i = 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            n = s[i + 1]
            if n in _ESCAPES:
                out += _ESCAPES[n]
                i += 2
                continue
            if n == "x" and _is_hex(s[i + 2:i + 4]):
                out.append(int(s[i + 2:i + 4], 16))
                i += 4
                continue
        out += c.encode("utf-8")
        i += 1
    return bytes(out)


def _is_hex(s):
    return len(s) == 2 and all(ch in "0123456789abcdefABCDEF" for ch in s)


def parse_hex(s):
    """'01 02 0A', '01020A', '0x01,0x02' 형태를 bytes로 변환."""
    cleaned = s.replace("0x", "").replace("0X", "")
    for sep in (" ", ",", ":", "-", "\t", "\n", "\r"):
        cleaned = cleaned.replace(sep, "")
    if len(cleaned) % 2:
        raise ValueError("HEX 자리수가 홀수입니다: %r" % s)
    try:
        return bytes.fromhex(cleaned)
    except ValueError:
        raise ValueError("HEX에 0~9, A~F 가 아닌 글자가 있습니다: %r" % s)


def to_bytes(s, fmt):
    if fmt == FMT_CAN:
        return can_text(s)
    return parse_hex(s) if fmt == FMT_HEX else parse_text(s)


# ---------------------------------------------------------------------------
# CAN 프레임 표기: "123#01 02 03" (표준 ID 3자리), "18FF50E5#11 22" (확장 ID 8자리)
# 프로그램 안에서는 프레임 하나를 이 글자 + 줄바꿈으로 주고받는다.
# ---------------------------------------------------------------------------

def parse_can(s):
    """'123#0102; 18FF50E5#11 22' → [(ID, 확장 여부, 데이터), ...]"""
    frames = []
    for part in s.replace("\n", ";").replace("\r", ";").split(";"):
        part = part.strip()
        if not part:
            continue
        if "#" not in part:
            raise ValueError("CAN 프레임은 ID#데이터 형식입니다 (예: 123#01 02): %r" % part)
        id_text, data_text = part.split("#", 1)
        id_text = id_text.strip()
        if id_text.lower().startswith("0x"):
            id_text = id_text[2:]
        try:
            arb = int(id_text, 16)
        except ValueError:
            raise ValueError("CAN ID는 16진수입니다: %r" % id_text)
        ext = len(id_text) > 3 or arb > 0x7FF
        if arb > 0x1FFFFFFF:
            raise ValueError("CAN ID가 너무 큽니다: %r" % id_text)
        data = parse_hex(data_text) if data_text.strip() else b""
        if len(data) > 8:
            raise ValueError("CAN 데이터는 8바이트까지입니다: %r" % part)
        frames.append((arb, ext, data))
    return frames


def format_can(arb, ext, data):
    return ("%08X" % arb if ext else "%03X" % arb) + "#" + bytes_to_hex(data)


def can_text(s):
    """사용자가 쓴 CAN 프레임들을 표준 표기 글자(bytes)로"""
    return "".join(format_can(*f) + "\n" for f in parse_can(s)).encode("ascii")


def bytes_to_hex(b):
    return " ".join("%02X" % x for x in b)


def bytes_to_text(b):
    """제어문자는 이스케이프 형태로 보여준다."""
    out = []
    for x in b:
        if x == 0x0D:
            out.append("\\r")
        elif x == 0x0A:
            out.append("\\n")
        elif x == 0x09:
            out.append("\\t")
        elif 0x20 <= x < 0x7F:
            out.append(chr(x))
        else:
            out.append("\\x%02X" % x)
    return "".join(out)


def list_serial_ports():
    if serial is None:
        return []
    return [p.device for p in serial.tools.list_ports.comports()]


# ---------------------------------------------------------------------------
# 자동 응답 규칙
# ---------------------------------------------------------------------------

class Rule(object):
    def __init__(self, enabled=True, match="", match_fmt=FMT_TEXT,
                 response="", response_fmt=FMT_TEXT, delay_ms=0, name="", response_check="none"):
        self.enabled = enabled
        self.match = match
        self.match_fmt = match_fmt
        self.response = response
        self.response_fmt = response_fmt
        self.delay_ms = int(delay_ms)
        self.name = name
        self.response_check = response_check

    def match_bytes(self):
        return to_bytes(self.match, self.match_fmt)

    def response_bytes(self):
        return checks.append(to_bytes(self.response, self.response_fmt), self.response_check)

    def to_dict(self):
        return dict(enabled=self.enabled, match=self.match, match_fmt=self.match_fmt,
                    response=self.response, response_fmt=self.response_fmt,
                    delay_ms=self.delay_ms, name=self.name, response_check=self.response_check)

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: d[k] for k in ("enabled", "match", "match_fmt", "response",
                                        "response_fmt", "delay_ms", "name", "response_check") if k in d})


class RuleMatcher(object):
    """수신 스트림에서 규칙 패턴을 찾는다. 패킷이 여러 번에 나눠 들어와도 매칭된다."""

    MAX_BUFFER = 4096

    def __init__(self):
        self.buffer = bytearray()

    def reset(self):
        self.buffer = bytearray()

    def feed(self, data, rules):
        """data를 버퍼에 추가하고, 매칭된 규칙 목록을 순서대로 반환한다."""
        compiled = []
        for r in rules:
            if not r.enabled:
                continue
            try:
                m = r.match_bytes()
            except ValueError:
                continue
            if m:
                compiled.append((r, m))
        if not compiled:
            self.reset()
            return []

        self.buffer += data
        hits = []
        while True:
            best = None  # (위치, 규칙, 패턴)
            for r, m in compiled:
                pos = self.buffer.find(m)
                if pos >= 0 and (best is None or pos < best[0]):
                    best = (pos, r, m)
            if best is None:
                break
            pos, r, m = best
            hits.append(r)
            del self.buffer[:pos + len(m)]

        # 다음 수신과 이어질 수 있는 꼬리만 남긴다
        keep = max(len(m) for _, m in compiled) - 1
        if len(self.buffer) > keep:
            del self.buffer[:len(self.buffer) - keep]
        if len(self.buffer) > self.MAX_BUFFER:
            del self.buffer[:-self.MAX_BUFFER]
        return hits


class RawHandler(object):
    """규칙 기반 자동 응답. 접속한 상대(src)마다 따로 매칭한다."""

    def __init__(self, rules):
        self.rules = rules
        self.enabled = True
        self.matchers = {}

    def reset(self):
        self.matchers = {}

    def on_data(self, data, src, emit):
        if not self.enabled:
            return []
        matcher = self.matchers.get(src)
        if matcher is None:
            matcher = self.matchers[src] = RuleMatcher()
        out = []
        for rule in matcher.feed(data, list(self.rules)):
            try:
                resp = rule.response_bytes()
            except ValueError as e:
                emit("error", "응답 데이터 오류: %s" % e)
                continue
            if resp:
                out.append((resp, rule.delay_ms))
        return out


class Faults(object):
    """
    오류 주입: 자동 응답을 일부러 망가뜨려서 상대 프로그램이 오류를 잘 처리하는지 시험한다.
    확률은 % 단위. Modbus 예외 응답은 modbus.SlaveHandler 에서 처리한다.
    """

    FIELDS = ("enabled", "drop_pct", "corrupt_pct", "truncate_pct", "duplicate_pct", "noise_pct",
              "delay_min_ms", "delay_max_ms", "exception_pct", "exception_code")

    def __init__(self):
        self.enabled = False
        self.drop_pct = 0.0        # 응답 안 함
        self.corrupt_pct = 0.0     # 마지막 바이트 변조 (CRC/체크섬 오류)
        self.truncate_pct = 0.0    # 앞부분만 보내기
        self.duplicate_pct = 0.0   # 두 번 보내기
        self.noise_pct = 0.0       # 앞에 쓰레기 바이트 붙이기
        self.delay_min_ms = 0      # 추가 지연 (최소~최대 사이 무작위)
        self.delay_max_ms = 0
        self.exception_pct = 0.0   # Modbus 예외 응답
        self.exception_code = 4

    def to_dict(self):
        return {k: getattr(self, k) for k in self.FIELDS}

    def load_dict(self, d):
        for k in self.FIELDS:
            if k in d:
                setattr(self, k, type(getattr(self, k))(d[k]))

    @staticmethod
    def _hit(pct):
        return pct > 0 and random.random() * 100 < pct

    def apply(self, data, delay_ms):
        """반환: ([(데이터, 지연ms), ...], [설명, ...])"""
        if not self.enabled:
            return [(data, delay_ms)], []
        notes = []
        if self._hit(self.drop_pct):
            return [], ["응답 안 함"]
        extra = 0
        if self.delay_max_ms > 0:
            extra = random.randint(min(self.delay_min_ms, self.delay_max_ms), self.delay_max_ms)
            if extra:
                notes.append("지연 +%dms" % extra)
        data = bytearray(data)
        if data and self._hit(self.corrupt_pct):
            data[-1] ^= 0xFF
            notes.append("마지막 바이트 변조")
        if len(data) > 1 and self._hit(self.truncate_pct):
            del data[len(data) // 2:]
            notes.append("앞부분 %d바이트만 보냄" % len(data))
        if self._hit(self.noise_pct):
            data[0:0] = bytes(random.randint(0, 255) for _ in range(random.randint(1, 3)))
            notes.append("앞에 쓰레기 바이트 추가")
        out = [(bytes(data), delay_ms + extra)]
        if self._hit(self.duplicate_pct):
            out.append((bytes(data), delay_ms + extra + 5))
            notes.append("두 번 보냄")
        return out, notes


# ---------------------------------------------------------------------------
# 연결 구현: read()는 (데이터, 보낸 상대) 를 반환한다. 상대는 TCP 서버에서만 의미가 있다.
# ---------------------------------------------------------------------------

class PtyPort(object):
    """Linux/macOS 가상 시리얼 포트 (PTY)."""

    def __init__(self, link_path=""):
        self.link_path = link_path
        self.master = None
        self.slave = None

    def open(self):
        import tty
        self.master, self.slave = os.openpty()
        tty.setraw(self.slave)  # 에코/줄 단위 처리 끄기 → 순수 바이트 전달
        device = os.ttyname(self.slave)
        if self.link_path:
            try:
                if os.path.islink(self.link_path):
                    os.unlink(self.link_path)
                os.symlink(device, self.link_path)
            except OSError as e:
                self.close()
                raise OSError("링크 생성 실패 (%s): %s" % (self.link_path, e))
        return device

    def read(self, timeout=0.1):
        r, _, _ = select.select([self.master], [], [], timeout)
        if not r:
            return b"", None
        try:
            return os.read(self.master, 4096), None
        except OSError:
            # 상대가 닫은 직후 등 일시적인 오류
            time.sleep(timeout)
            return b"", None

    def write(self, data, target=None):
        os.write(self.master, data)

    def close(self):
        if self.link_path and os.path.islink(self.link_path):
            try:
                os.unlink(self.link_path)
            except OSError:
                pass
        for fd in (self.master, self.slave):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self.master = self.slave = None


class SerialPort(object):
    """pyserial 포트 (실제 장치, Windows 가상 COM 포트 등)."""

    def __init__(self, port, baudrate=115200, bytesize=8, parity="N", stopbits=1):
        self.port = port
        self.baudrate = baudrate
        self.bytesize = bytesize
        self.parity = parity
        self.stopbits = stopbits
        self.ser = None

    def open(self):
        if serial is None:
            raise RuntimeError("pyserial이 설치되어 있지 않습니다: pip install pyserial")
        self.ser = serial.Serial(self.port, baudrate=self.baudrate, bytesize=self.bytesize,
                                 parity=self.parity, stopbits=self.stopbits, timeout=0.1)
        return self.port

    def read(self, timeout=0.1):
        n = self.ser.in_waiting
        return self.ser.read(n if n else 1), None

    def write(self, data, target=None):
        self.ser.write(data)

    def close(self):
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None


class TcpServerPort(object):
    """접속을 기다리는 TCP 서버. 응답은 요청을 보낸 클라이언트에게만 보낸다."""

    def __init__(self, host, port, notify):
        self.host = host or "0.0.0.0"
        self.port = int(port)
        self.notify = notify
        self.sock = None
        self.clients = {}  # 소켓 -> "IP:포트"
        self.lock = threading.Lock()

    def open(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if not IS_WINDOWS:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((self.host, self.port))
            s.listen(8)
        except OSError:
            s.close()
            raise
        self.sock = s
        return "TCP 서버 %s:%d" % (self.host, self.port)

    def read(self, timeout=0.1):
        with self.lock:
            socks = [self.sock] + list(self.clients)
        r, _, _ = select.select(socks, [], [], timeout)
        for s in r:
            if s is self.sock:
                conn, addr = s.accept()
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                name = "%s:%d" % addr[:2]
                with self.lock:
                    self.clients[conn] = name
                self.notify("info", "클라이언트 접속: %s" % name)
                continue
            name = self.clients.get(s)
            try:
                data = s.recv(4096)
            except OSError:
                data = b""
            if data:
                return data, name
            self._drop(s)
        return b"", None

    def _drop(self, s):
        with self.lock:
            name = self.clients.pop(s, None)
        try:
            s.close()
        except OSError:
            pass
        if name:
            self.notify("info", "클라이언트 끊김: %s" % name)

    def write(self, data, target=None):
        with self.lock:
            targets = [s for s, n in self.clients.items() if target is None or n == target]
        if not targets:
            raise RuntimeError("접속한 클라이언트가 없습니다.")
        for s in targets:
            try:
                s.sendall(data)
            except OSError:
                self._drop(s)

    def close(self):
        with self.lock:
            socks = list(self.clients) + ([self.sock] if self.sock else [])
            self.clients = {}
            self.sock = None
        for s in socks:
            try:
                s.close()
            except OSError:
                pass


class TcpClientPort(object):
    """지정한 서버로 접속하는 TCP 클라이언트."""

    def __init__(self, host, port):
        self.host = host or "127.0.0.1"
        self.port = int(port)
        self.sock = None

    def open(self):
        s = socket.create_connection((self.host, self.port), timeout=3)
        s.settimeout(None)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock = s
        return "TCP 접속 %s:%d" % (self.host, self.port)

    def read(self, timeout=0.1):
        r, _, _ = select.select([self.sock], [], [], timeout)
        if not r:
            return b"", None
        data = self.sock.recv(4096)
        if not data:
            raise ConnectionError("서버가 연결을 끊었습니다.")
        return data, None

    def write(self, data, target=None):
        self.sock.sendall(data)

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None


class UdpPort(object):
    """UDP 송수신. 응답은 보낸 상대에게, 직접 보내기는 지정한 상대(없으면 마지막으로 보낸 상대)에게."""

    def __init__(self, bind_host, bind_port, remote_host, remote_port, notify):
        self.bind_host = bind_host or "0.0.0.0"
        self.bind_port = int(bind_port or 0)
        self.remote = (remote_host, int(remote_port)) if remote_host and remote_port else None
        self.notify = notify
        self.sock = None
        self.last_peer = None

    def open(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.bind((self.bind_host, self.bind_port))
        except OSError:
            s.close()
            raise
        self.sock = s
        local = s.getsockname()
        text = "UDP %s:%d" % (local[0], local[1])
        if self.remote:
            text += " → %s:%d" % self.remote
        return text

    def read(self, timeout=0.1):
        r, _, _ = select.select([self.sock], [], [], timeout)
        if not r:
            return b"", None
        try:
            data, addr = self.sock.recvfrom(65535)
        except (ConnectionResetError, OSError) as e:
            # Windows: 상대 포트가 닫혀 있으면 다음 수신에서 이 오류가 난다 → 무시
            if isinstance(e, ConnectionResetError) or getattr(e, "winerror", None) == 10054:
                return b"", None
            raise
        self.last_peer = addr[:2]
        return data, "%s:%d" % addr[:2]

    def write(self, data, target=None):
        if target:
            host, port = target.rsplit(":", 1)
            dest = (host, int(port))
        else:
            dest = self.remote or self.last_peer
        if dest is None:
            raise RuntimeError("보낼 상대가 없습니다. 상대 IP와 포트를 입력하세요.")
        self.sock.sendto(data, dest)

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None


CAN_IFACES = [
    ("virtual", "가상 CAN (프로그램 안 탭끼리)", "vcan0", "같은 채널 이름을 쓰는 탭끼리 주고받습니다. 장비 없이 시험할 때"),
    ("udp_multicast", "가상 CAN (네트워크)", "239.74.163.2", "같은 네트워크의 다른 PC/프로그램과 주고받습니다 (멀티캐스트)"),
    ("slcan", "시리얼 CAN 어댑터 (slcan)", "COM5", "CANable·USBtin 등. 채널에 어댑터의 COM 포트를 씁니다"),
    ("pcan", "PCAN (Peak)", "PCAN_USBBUS1", "Peak의 무료 PCAN-Basic 드라이버가 필요합니다"),
    ("kvaser", "Kvaser", "0", "Kvaser의 무료 CANlib 드라이버가 필요합니다"),
    ("vector", "Vector", "0", "Vector의 무료 드라이버가 필요합니다"),
    ("ixxat", "IXXAT", "0", "IXXAT VCI 드라이버가 필요합니다"),
    ("gs_usb", "candleLight (gs_usb)", "0", "candleLight 펌웨어 어댑터"),
    ("socketcan", "SocketCAN (Linux)", "can0", "Linux 전용. 비트레이트는 OS에서 설정합니다 (ip link)"),
]
CAN_BITRATES = ["10000", "20000", "50000", "100000", "125000", "250000", "500000", "800000", "1000000"]
CAN_NO_BITRATE = ("virtual", "udp_multicast", "socketcan")


class CanPort(object):
    """python-can 으로 CAN 버스에 연결. 프레임 하나 = '123#01 02' + 줄바꿈"""

    def __init__(self, iface, channel, bitrate):
        self.iface = iface
        self.channel = channel
        self.bitrate = int(bitrate or 500000)
        self.bus = None

    def open(self):
        try:
            import can
        except ImportError:
            raise RuntimeError("CAN 기능에 필요한 python-can이 없습니다: pip install python-can")
        import logging
        logging.getLogger("can").setLevel(logging.ERROR)  # 라이브러리 안내 메시지 숨김
        kwargs = dict(interface=self.iface, channel=self.channel)
        if self.iface not in CAN_NO_BITRATE:
            kwargs["bitrate"] = self.bitrate
        if self.iface == "virtual":
            kwargs["receive_own_messages"] = False
        try:
            self.bus = can.Bus(**kwargs)
        except Exception as e:
            raise RuntimeError("CAN 장치를 열 수 없습니다 (%s, 채널 %s): %s" % (self.iface, self.channel, e))
        text = "CAN %s 채널 %s" % (self.iface, self.channel)
        if self.iface not in CAN_NO_BITRATE:
            text += " %dkbps" % (self.bitrate // 1000)
        return text

    def read(self, timeout=0.1):
        msg = self.bus.recv(timeout)
        if msg is None:
            return b"", None
        if msg.is_error_frame:
            return b"ERR#" + bytes_to_hex(bytes(msg.data)).encode("ascii") + b"\n", None
        return (format_can(msg.arbitration_id, msg.is_extended_id, bytes(msg.data)) + "\n").encode("ascii"), None

    def write(self, data, target=None):
        import can
        try:
            frames = parse_can(data.decode("ascii"))
        except (UnicodeDecodeError, ValueError) as e:
            raise ValueError("CAN 연결에서는 형식을 CAN으로 하고 123#01 02 처럼 쓰세요. (%s)" % e)
        for arb, ext, payload in frames:
            self.bus.send(can.Message(arbitration_id=arb, is_extended_id=ext, data=payload))

    def close(self):
        if self.bus is not None:
            try:
                self.bus.shutdown()
            except Exception:
                pass
        self.bus = None


class CanRuleHandler(RawHandler):
    """CAN 규칙: 받은 프레임이 '123#01' 처럼 쓴 수신 패턴으로 시작하면 응답 프레임을 보낸다."""

    def on_data(self, data, src, emit):
        if not self.enabled:
            return []
        out = []
        for line in data.decode("ascii", errors="replace").splitlines():
            for rule in list(self.rules):
                if not rule.enabled:
                    continue
                try:
                    pattern = to_bytes(rule.match, rule.match_fmt).decode("ascii", errors="replace").strip()
                    if not pattern or not line.startswith(pattern):
                        continue
                    resp = to_bytes(rule.response, rule.response_fmt)
                except ValueError as e:
                    emit("error", "CAN 규칙 오류: %s" % e)
                    continue
                if resp:
                    out.append((resp, rule.delay_ms))
        return out


# ---------------------------------------------------------------------------
# 채널: 연결 1개 + 수신 스레드 + 처리기
# ---------------------------------------------------------------------------

class Channel(object):
    """
    on_event(kind, data, src) 콜백으로 이벤트를 전달한다. (수신 스레드에서도 호출됨)
      kind: 'rx' | 'tx' | 'auto' | 'relay' | 'info' | 'error' | 'fault' | 'closed'
            | 'decode' | 'mb_changed' | 'mb_result'
    forward: 다른 Channel을 넣으면 받은 데이터를 그대로 그쪽으로 보낸다 (중계)
    """

    def __init__(self, on_event):
        self.on_event = on_event
        self.handler = None
        self.forward = None
        self.faults = Faults()
        self.port = None
        self._thread = None
        self._stop = threading.Event()
        self._write_lock = threading.Lock()

    @property
    def is_open(self):
        return self.port is not None

    def emit(self, kind, data=None, src=None):
        self.on_event(kind, data, src)

    def open(self, conn, **opts):
        if self.is_open:
            return
        if conn == CONN_PTY:
            if not PTY_SUPPORTED:
                raise RuntimeError("Windows에서는 PTY를 지원하지 않습니다.")
            port = PtyPort(opts.get("link_path", ""))
        elif conn == CONN_SERIAL:
            port = SerialPort(opts["port"], int(opts.get("baudrate", 115200)),
                              int(opts.get("bytesize", 8)), opts.get("parity", "N"),
                              float(opts.get("stopbits", 1)))
        elif conn == CONN_TCP_SERVER:
            port = TcpServerPort(opts.get("host"), opts["tcp_port"], self.emit)
        elif conn == CONN_TCP_CLIENT:
            port = TcpClientPort(opts.get("host"), opts["tcp_port"])
        elif conn == CONN_UDP:
            port = UdpPort(opts.get("udp_bind"), opts.get("udp_local_port"),
                           opts.get("host"), opts.get("tcp_port"), self.emit)
        elif conn == CONN_CAN:
            port = CanPort(opts.get("can_iface", "virtual"), opts.get("can_channel", "vcan0"),
                           opts.get("can_bitrate", 500000))
        else:
            raise ValueError("알 수 없는 연결 방식: %s" % conn)
        device = port.open()
        if self.handler is not None:
            self.handler.reset()
        self.port = port
        self._stop.clear()
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()
        return device

    def close(self):
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)
        self._thread = None
        if self.port is not None:
            self.port.close()
        self.port = None

    def send(self, data, kind="tx", target=None):
        port = self.port
        if port is None:
            raise RuntimeError("포트가 열려 있지 않습니다.")
        with self._write_lock:
            port.write(data, target)
        self.emit(kind, data, target)

    def _reader(self):
        port = self.port
        while not self._stop.is_set():
            try:
                data, src = port.read(0.1)
            except Exception as e:
                if not self._stop.is_set():
                    self.emit("error", "수신 오류: %s" % e)
                    self.emit("closed")
                break
            if not data:
                continue
            self.emit("rx", data, src)
            peer = self.forward
            if peer is not None and peer.is_open:
                try:
                    peer.send(data, kind="relay")
                except Exception as e:
                    self.emit("error", "중계 실패: %s" % e)
            handler = self.handler
            if handler is None:
                continue
            try:
                responses = handler.on_data(data, src, self.emit)
            except Exception as e:
                self.emit("error", "처리 오류: %s" % e)
                continue
            for resp, delay_ms in responses:
                self._respond(resp, delay_ms, src)

    def _respond(self, data, delay_ms, target):
        sends, notes = self.faults.apply(data, delay_ms)
        if notes:
            self.emit("fault", ", ".join(notes))
        for d, delay in sends:
            self._send_later(d, delay, target)

    def _send_later(self, data, delay_ms, target):
        def do_send():
            try:
                self.send(data, kind="auto", target=target)
            except Exception as e:
                self.emit("error", "자동 응답 송신 실패: %s" % e)

        if delay_ms > 0:
            t = threading.Timer(delay_ms / 1000.0, do_send)
            t.daemon = True
            t.start()
        else:
            do_send()
