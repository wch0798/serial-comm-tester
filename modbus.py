"""
Modbus RTU / TCP 처리

- 슬레이브(장치 흉내): 요청을 받아 데이터 저장소의 값으로 응답한다.
- 마스터(요청 보내기): 요청 프레임을 만들고, 돌아온 응답을 해석한다.
- 프레임: RTU(장치주소 + PDU + CRC16), TCP(MBAP 헤더 7바이트 + PDU)
"""

import math
import random
import struct
import threading
import time

FRAME_RTU = "RTU"
FRAME_TCP = "TCP"

AREA_COIL = "coil"
AREA_DI = "di"
AREA_IR = "ir"
AREA_HR = "hr"

# (키, 화면 표시, PLC 주소 시작값)
AREAS = [
    (AREA_COIL, "코일 (0x, FC01/05/15)", 1),
    (AREA_DI, "입력 접점 (1x, FC02)", 10001),
    (AREA_IR, "입력 레지스터 (3x, FC04)", 30001),
    (AREA_HR, "보유 레지스터 (4x, FC03/06/16)", 40001),
]
AREA_BASE = {k: base for k, _, base in AREAS}
BIT_AREAS = (AREA_COIL, AREA_DI)

FC_NAMES = {
    1: "01 코일 읽기",
    2: "02 입력 접점 읽기",
    3: "03 보유 레지스터 읽기",
    4: "04 입력 레지스터 읽기",
    5: "05 코일 1개 쓰기",
    6: "06 레지스터 1개 쓰기",
    15: "15 코일 여러 개 쓰기",
    16: "16 레지스터 여러 개 쓰기",
}
FC_AREA = {1: AREA_COIL, 2: AREA_DI, 3: AREA_HR, 4: AREA_IR,
           5: AREA_COIL, 6: AREA_HR, 15: AREA_COIL, 16: AREA_HR}

EXC_NAMES = {1: "지원하지 않는 기능 코드", 2: "잘못된 주소", 3: "잘못된 값", 4: "장치 오류",
             5: "처리 중 (확인)", 6: "장치 바쁨", 8: "메모리 패리티 오류", 10: "게이트웨이 경로 없음",
             11: "게이트웨이 대상 응답 없음"}


def crc16(data):
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def add_crc(frame):
    return bytes(frame) + struct.pack("<H", crc16(frame))


def pack_bits(bits):
    out = bytearray((len(bits) + 7) // 8)
    for i, b in enumerate(bits):
        if b:
            out[i // 8] |= 1 << (i % 8)
    return bytes(out)


def unpack_bits(data, count):
    return [(data[i // 8] >> (i % 8)) & 1 for i in range(count)]


def wrap(frame_type, unit, pdu, tid=0):
    if frame_type == FRAME_TCP:
        return struct.pack(">HHHB", tid & 0xFFFF, 0, len(pdu) + 1, unit) + pdu
    return add_crc(bytes([unit]) + pdu)


def exception_name(code):
    return EXC_NAMES.get(code, "알 수 없는 예외")


# ---------------------------------------------------------------------------
# 데이터 저장소 (슬레이브)
# ---------------------------------------------------------------------------

class DataStore(object):
    def __init__(self):
        self.lock = threading.Lock()
        self.areas = {k: {} for k, _, _ in AREAS}

    def get(self, area, addr):
        return self.areas[area].get(addr, 0)

    def set(self, area, addr, value):
        value = (1 if value else 0) if area in BIT_AREAS else int(value) & 0xFFFF
        with self.lock:
            if value:
                self.areas[area][addr] = value
            else:
                self.areas[area].pop(addr, None)

    def clear(self, area):
        with self.lock:
            self.areas[area].clear()

    def to_dict(self):
        return {a: {str(k): v for k, v in sorted(vals.items())} for a, vals in self.areas.items()}

    def load_dict(self, d):
        for a in self.areas:
            self.areas[a] = {int(k): int(v) for k, v in d.get(a, {}).items() if int(v)}


def process_request(store, pdu):
    """요청 PDU를 처리하고 (응답 PDU, 저장소 변경 여부)를 반환한다."""
    fc = pdu[0]

    def exc(code):
        return bytes([fc | 0x80, code]), False

    try:
        if fc in (1, 2, 3, 4):
            addr, count = struct.unpack(">HH", pdu[1:5])
            limit = 2000 if fc in (1, 2) else 125
            if not 1 <= count <= limit:
                return exc(3)
            if addr + count > 0x10000:
                return exc(2)
            area = FC_AREA[fc]
            values = [store.get(area, addr + i) for i in range(count)]
            if fc in (1, 2):
                data = pack_bits(values)
            else:
                data = struct.pack(">%dH" % count, *values)
            return bytes([fc, len(data)]) + data, False

        if fc == 5:
            addr, val = struct.unpack(">HH", pdu[1:5])
            if val not in (0xFF00, 0x0000):
                return exc(3)
            store.set(AREA_COIL, addr, val == 0xFF00)
            return bytes(pdu[:5]), True

        if fc == 6:
            addr, val = struct.unpack(">HH", pdu[1:5])
            store.set(AREA_HR, addr, val)
            return bytes(pdu[:5]), True

        if fc in (15, 16):
            addr, count, bc = struct.unpack(">HHB", pdu[1:6])
            data = pdu[6:6 + bc]
            if fc == 15:
                ok = 1 <= count <= 1968 and bc == (count + 7) // 8
            else:
                ok = 1 <= count <= 123 and bc == count * 2
            if not ok or len(data) != bc:
                return exc(3)
            if addr + count > 0x10000:
                return exc(2)
            if fc == 15:
                for i, b in enumerate(unpack_bits(data, count)):
                    store.set(AREA_COIL, addr + i, b)
            else:
                for i, v in enumerate(struct.unpack(">%dH" % count, data)):
                    store.set(AREA_HR, addr + i, v)
            return bytes(pdu[:5]), True
    except struct.error:
        return exc(3)
    return exc(1)


def describe_request(pdu):
    fc = pdu[0]
    name = FC_NAMES.get(fc, "FC%02d" % fc)
    try:
        if fc in (1, 2, 3, 4):
            addr, count = struct.unpack(">HH", pdu[1:5])
            return "%s  주소=%d 개수=%d" % (name, addr, count)
        if fc in (5, 6):
            addr, val = struct.unpack(">HH", pdu[1:5])
            if fc == 5:
                return "%s  주소=%d 값=%s" % (name, addr, "ON" if val == 0xFF00 else "OFF")
            return "%s  주소=%d 값=%d" % (name, addr, val)
        if fc in (15, 16):
            addr, count = struct.unpack(">HH", pdu[1:5])
            return "%s  주소=%d 개수=%d" % (name, addr, count)
    except struct.error:
        pass
    return name


def describe_response(pdu):
    fc = pdu[0]
    if fc & 0x80:
        code = pdu[1] if len(pdu) > 1 else 0
        return "예외 응답 %02d (%s)" % (code, exception_name(code))
    if fc in (1, 2, 3, 4):
        return "정상 응답 %d바이트" % (pdu[1] if len(pdu) > 1 else 0)
    return "정상 응답 (쓰기 완료)"


# ---------------------------------------------------------------------------
# 스트림에서 프레임 잘라내기
# ---------------------------------------------------------------------------

class FrameParser(object):
    """
    is_request=True  : 요청 프레임을 자른다 (슬레이브 입장)
    is_request=False : 응답 프레임을 자른다 (마스터 입장)
    RTU는 길이 규칙 + CRC로 프레임을 찾고, 수신이 잠시 끊기면 남은 조각을 버린다.
    """

    RTU_GAP = 0.2  # 초. 이보다 오래 수신이 없으면 이전 조각은 버린다

    def __init__(self, frame_type, is_request):
        self.frame_type = frame_type
        self.is_request = is_request
        self.buf = bytearray()
        self.last = 0.0

    def reset(self):
        self.buf = bytearray()

    def feed(self, data):
        """반환: [(unit, pdu, tid, raw), ...] / 잘못된 바이트는 버린다"""
        now = time.time()
        if self.frame_type == FRAME_RTU and now - self.last > self.RTU_GAP:
            self.buf = bytearray()
        self.last = now
        self.buf += data
        if self.frame_type == FRAME_TCP:
            return self._feed_tcp()
        return self._feed_rtu()

    def _feed_tcp(self):
        out = []
        buf = self.buf
        while len(buf) >= 7:
            tid, pid, ln = struct.unpack(">HHH", buf[:6])
            if pid != 0 or not 2 <= ln <= 254:
                del buf[0]
                continue
            total = 6 + ln
            if len(buf) < total:
                break
            out.append((buf[6], bytes(buf[7:total]), tid, bytes(buf[:total])))
            del buf[:total]
        return out

    def _expected_rtu_len(self):
        buf = self.buf
        fc = buf[1]
        if self.is_request:
            if fc in (1, 2, 3, 4, 5, 6):
                return 8
            if fc in (15, 16):
                return 9 + buf[6] if len(buf) >= 7 else 0
            return -1
        if fc & 0x80:
            return 5
        if fc in (1, 2, 3, 4):
            return 5 + buf[2] if len(buf) >= 3 else 0
        if fc in (5, 6, 15, 16):
            return 8
        return -1

    def _feed_rtu(self):
        out = []
        buf = self.buf
        while len(buf) >= 4:
            n = self._expected_rtu_len()
            if n == 0:
                break  # 길이를 알려면 더 받아야 함
            if n < 0:
                del buf[0]
                continue
            if len(buf) < n:
                break
            frame = bytes(buf[:n])
            if crc16(frame[:-2]) != struct.unpack("<H", frame[-2:])[0]:
                del buf[0]
                continue
            out.append((frame[0], frame[1:-2], None, frame))
            del buf[:n]
        return out


# ---------------------------------------------------------------------------
# 마스터 요청 만들기 / 응답 해석
# ---------------------------------------------------------------------------

def build_request_pdu(fc, addr, count, values):
    if fc in (1, 2, 3, 4):
        return struct.pack(">BHH", fc, addr, count)
    if not values:
        raise ValueError("쓰기 값을 입력하세요.")
    if fc == 5:
        return struct.pack(">BHH", 5, addr, 0xFF00 if values[0] else 0)
    if fc == 6:
        return struct.pack(">BHH", 6, addr, values[0] & 0xFFFF)
    if fc == 15:
        data = pack_bits(values)
        return struct.pack(">BHHB", 15, addr, len(values), len(data)) + data
    if fc == 16:
        n = len(values)
        return struct.pack(">BHHB", 16, addr, n, n * 2) + struct.pack(">%dH" % n, *[v & 0xFFFF for v in values])
    raise ValueError("지원하지 않는 기능 코드: %d" % fc)


def parse_response(req, pdu):
    """
    req: dict(fc, addr, count)
    반환: dict(ok, rows=[(주소, 값)], text)
    """
    fc = req["fc"]
    if pdu[0] == fc | 0x80:
        code = pdu[1] if len(pdu) > 1 else 0
        return dict(ok=False, rows=[], text="예외 응답 %02d: %s" % (code, exception_name(code)))
    if pdu[0] != fc:
        return dict(ok=False, rows=[], text="기능 코드가 다른 응답 (FC%02d)" % pdu[0])
    try:
        if fc in (1, 2):
            bits = unpack_bits(pdu[2:2 + pdu[1]], req["count"])
            return dict(ok=True, rows=[(req["addr"] + i, b) for i, b in enumerate(bits)], text="읽기 성공")
        if fc in (3, 4):
            n = pdu[1] // 2
            regs = struct.unpack(">%dH" % n, pdu[2:2 + n * 2])
            return dict(ok=True, rows=[(req["addr"] + i, v) for i, v in enumerate(regs)], text="읽기 성공")
        return dict(ok=True, rows=[], text="쓰기 성공")
    except (struct.error, IndexError):
        return dict(ok=False, rows=[], text="응답 길이 오류")


# ---------------------------------------------------------------------------
# 채널에 붙는 처리기
# ---------------------------------------------------------------------------

class SlaveHandler(object):
    """요청을 받아서 저장소 값으로 응답한다."""

    def __init__(self, store):
        self.store = store
        self.frame_type = FRAME_RTU
        self.unit_id = 1        # 0이면 모든 장치 주소에 응답
        self.delay_ms = 0
        self.no_response = False  # 타임아웃 테스트용
        self.faults = None        # serial_core.Faults (예외 응답 주입)
        self.parsers = {}

    def reset(self):
        self.parsers = {}

    def on_data(self, data, src, emit):
        parser = self.parsers.get(src)
        if parser is None:
            parser = self.parsers[src] = FrameParser(self.frame_type, True)
        out = []
        for unit, pdu, tid, raw in parser.feed(data):
            req_text = "[장치 %d] %s" % (unit, describe_request(pdu))
            if self.unit_id and unit != self.unit_id and unit != 0:
                emit("decode", req_text + "  → 장치 주소가 달라 무시")
                continue
            resp, changed = process_request(self.store, pdu)
            if changed:
                emit("mb_changed", None)
            f = self.faults
            if f is not None and f.enabled and f._hit(f.exception_pct):
                resp = bytes([pdu[0] | 0x80, int(f.exception_code) & 0xFF])
                emit("fault", "Modbus 예외 응답 %02d (%s)" % (resp[1], exception_name(resp[1])))
            if self.no_response:
                emit("decode", req_text + "  → 응답 안 함 (설정)")
                continue
            if unit == 0 and self.frame_type == FRAME_RTU:
                emit("decode", req_text + "  → 브로드캐스트라 응답 없음")
                continue
            emit("decode", req_text + "  → " + describe_response(resp))
            out.append((wrap(self.frame_type, unit, resp, tid or 0), self.delay_ms))
        return out


class MasterHandler(object):
    """요청을 보내고 응답을 기다린다."""

    def __init__(self):
        self.frame_type = FRAME_RTU
        self.lock = threading.Lock()
        self.pending = {}  # 키(tid 또는 'rtu') -> 요청 정보
        self.tid = 0
        self.parser = FrameParser(FRAME_RTU, False)

    def reset(self):
        with self.lock:
            self.pending = {}
        self.parser = FrameParser(self.frame_type, False)

    def make_request(self, unit, fc, addr, count, values):
        pdu = build_request_pdu(fc, addr, count, values)
        with self.lock:
            self.tid = (self.tid + 1) & 0xFFFF
            key = self.tid if self.frame_type == FRAME_TCP else "rtu"
            if self.frame_type == FRAME_RTU:
                self.pending.clear()  # RTU는 한 번에 하나만 기다린다
            count = len(values) if fc in (15, 16) else count
            self.pending[key] = dict(unit=unit, fc=fc, addr=addr, count=count, sent=time.time(), key=key)
        return wrap(self.frame_type, unit, pdu, self.tid), key

    def check_timeout(self, key):
        with self.lock:
            return self.pending.pop(key, None)

    def on_data(self, data, src, emit):
        for unit, pdu, tid, raw in self.parser.feed(data):
            key = tid if self.frame_type == FRAME_TCP else "rtu"
            with self.lock:
                req = self.pending.pop(key, None)
            if req is None:
                emit("decode", "[장치 %d] 기다리지 않던 응답: %s" % (unit, describe_response(pdu)))
                continue
            result = parse_response(req, pdu)
            result["req"] = req
            result["elapsed_ms"] = int((time.time() - req["sent"]) * 1000)
            emit("mb_result", result)
        return []


# ---------------------------------------------------------------------------
# 값 자동 변화 (센서 흉내)
# ---------------------------------------------------------------------------

SIM_MODES = [("inc", "증가"), ("dec", "감소"), ("random", "무작위"), ("sine", "사인파"), ("toggle", "토글 (최소↔최대)")]


class SimEntry(object):
    """
    한 주소의 값을 간격(ms)마다 바꾼다.
    증감값: 증가/감소는 한 번에 바뀌는 양, 사인파는 한 번에 진행하는 각도(도)
    """

    FIELDS = ("enabled", "area", "addr", "mode", "vmin", "vmax", "step", "interval_ms")

    def __init__(self, enabled=True, area=AREA_HR, addr=0, mode="inc", vmin=0, vmax=100, step=1, interval_ms=1000):
        self.enabled = bool(enabled)
        self.area = area
        self.addr = int(addr)
        self.mode = mode
        self.vmin = int(vmin)
        self.vmax = int(vmax)
        self.step = float(step)
        self.interval_ms = max(10, int(interval_ms))
        self.next_due = 0.0
        self.phase = 0.0

    def to_dict(self):
        return {k: getattr(self, k) for k in self.FIELDS}

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: d[k] for k in cls.FIELDS if k in d})

    def next_value(self, cur):
        lo, hi = min(self.vmin, self.vmax), max(self.vmin, self.vmax)
        if self.area in BIT_AREAS:
            lo, hi = max(0, min(lo, 1)), max(0, min(hi, 1))
        elif lo < 0 and cur >= 0x8000:
            cur -= 0x10000  # 음수 범위면 부호 있는 값으로 계산
        step = self.step
        if self.mode == "inc":
            v = cur + int(step)
            return lo if v > hi or v < lo else v
        if self.mode == "dec":
            v = cur - int(step)
            return hi if v < lo or v > hi else v
        if self.mode == "random":
            return random.randint(lo, hi)
        if self.mode == "sine":
            self.phase = (self.phase + step) % 360.0
            mid, amp = (lo + hi) / 2.0, (hi - lo) / 2.0
            return int(round(mid + amp * math.sin(math.radians(self.phase))))
        if self.mode == "toggle":
            return lo if cur == hi else hi
        return cur


def sim_tick(store, entries, now):
    """때가 된 항목의 값을 바꾼다. 하나라도 바뀌면 True"""
    changed = False
    for e in entries:
        if not e.enabled or now < e.next_due:
            continue
        e.next_due = now + e.interval_ms / 1000.0
        store.set(e.area, e.addr, e.next_value(store.get(e.area, e.addr)))
        changed = True
    return changed
