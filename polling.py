"""
Modbus 폴링: 여러 값을 목록으로 등록해 정해진 간격으로 읽는다.

- PollItem: 읽을 값 하나 (장치, 기능 코드, 주소, 데이터 형식, 배율 …)
- PollScheduler: 요청을 한 번에 하나씩 보내고 응답/타임아웃을 기다린 뒤 다음 요청을 보낸다.
  RTU는 한 번에 하나만 기다릴 수 있으므로, 화면의 '단일 요청'도 같은 순서에 넣어 서로 엉키지 않게 한다.
화면과 무관한 로직이다. 화면은 주기적으로 tick()을 부르고, 응답(mb_result)을 on_result()로 넘긴다.
"""

import collections
import struct

import modbus as mb

DTYPES = [("bit", "비트"), ("u16", "UINT16"), ("s16", "INT16"), ("u32", "UINT32"), ("s32", "INT32"),
          ("f32", "FLOAT32")]
ORDERS = [("ABCD", "ABCD (기본)"), ("CDAB", "CDAB (워드 스왑)"), ("BADC", "BADC (바이트 스왑)"),
          ("DCBA", "DCBA (리틀엔디언)")]
READ_FCS = (1, 2, 3, 4)
HISTORY_MAX = 20000  # 값마다 보관할 그래프 점 수


class PollItem(object):
    FIELDS = ("enabled", "name", "unit", "fc", "addr", "dtype", "order", "scale", "offset", "interval_ms", "graph")

    def __init__(self, enabled=True, name="", unit=1, fc=3, addr=0, dtype="u16", order="ABCD",
                 scale=1.0, offset=0.0, interval_ms=1000, graph=True):
        self.enabled = bool(enabled)
        self.name = name or "값"
        self.unit = int(unit)
        self.fc = int(fc)
        self.addr = int(addr)
        self.dtype = dtype
        self.order = order
        self.scale = float(scale)
        self.offset = float(offset)
        self.interval_ms = max(20, int(interval_ms))
        self.graph = bool(graph)
        self.reset_runtime()

    def reset_runtime(self):
        self.next_due = 0.0
        self.value = None
        self.status = ""
        self.ok = None
        self.updated = None
        self.ok_count = 0
        self.err_count = 0
        self.history = collections.deque(maxlen=HISTORY_MAX)

    def validate(self):
        if self.fc not in READ_FCS:
            raise ValueError("폴링은 읽기 기능 코드(01~04)만 됩니다.")
        is_bit = self.dtype == "bit"
        if is_bit != (self.fc in (1, 2)):
            raise ValueError("비트 형식은 FC01/02, 숫자 형식은 FC03/04 와 함께 써야 합니다.")
        if not 0 <= self.addr <= 0xFFFF or self.addr + self.reg_count() > 0x10000:
            raise ValueError("주소는 0~65535 입니다.")
        if not 0 <= self.unit <= 247:
            raise ValueError("장치 주소는 0~247 입니다.")

    def reg_count(self):
        return 2 if self.dtype in ("u32", "s32", "f32") else 1

    def to_dict(self):
        return {k: getattr(self, k) for k in self.FIELDS}

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: d[k] for k in cls.FIELDS if k in d})

    def decode(self, words):
        """읽은 값(레지스터 또는 비트 목록) → 배율/오프셋 적용한 값"""
        if self.dtype == "bit":
            raw = words[0]
        elif self.dtype == "u16":
            raw = words[0]
        elif self.dtype == "s16":
            raw = words[0] - 0x10000 if words[0] & 0x8000 else words[0]
        else:
            r0, r1 = words[0], words[1]
            b = {"ABCD": struct.pack(">HH", r0, r1), "CDAB": struct.pack(">HH", r1, r0),
                 "BADC": struct.pack("<HH", r0, r1), "DCBA": struct.pack("<HH", r1, r0)}[self.order]
            raw = struct.unpack({"u32": ">I", "s32": ">i", "f32": ">f"}[self.dtype], b)[0]
        if self.scale == 1.0 and self.offset == 0.0:
            return raw
        return raw * self.scale + self.offset

    def describe_addr(self):
        base = mb.AREA_BASE[mb.FC_AREA[self.fc]]
        return "FC%02d %d" % (self.fc, base + self.addr)


def format_value(v):
    if v is None:
        return "-"
    if isinstance(v, float):
        if v != v:  # NaN
            return "NaN"
        return ("%.4f" % v).rstrip("0").rstrip(".")
    return str(v)


class Job(object):
    """보낼 요청 하나. item 이 있으면 폴링, 없으면 화면의 단일 요청"""

    def __init__(self, unit, fc, addr, count, values=None, item=None):
        self.unit, self.fc, self.addr, self.count = unit, fc, addr, count
        self.values = values or []
        self.item = item
        self.key = None
        self.sent = 0.0


class PollScheduler(object):
    def __init__(self, master_handler):
        self.handler = master_handler
        self.items = []
        self.running = False      # 폴링 목록 실행 여부 (단일 요청은 항상 처리)
        self.queue = []           # 단일 요청 대기열
        self.inflight = None
        self.requests = 0
        self.timeouts = 0

    def reset(self):
        self.queue = []
        self.inflight = None
        for it in self.items:
            it.next_due = 0.0

    def submit(self, job):
        self.queue.append(job)

    def busy(self):
        return self.inflight is not None

    def _next_job(self, now):
        if self.queue:
            return self.queue.pop(0)
        if not self.running:
            return None
        due = [it for it in self.items if it.enabled and it.next_due <= now]
        if not due:
            return None
        it = min(due, key=lambda x: x.next_due)
        it.next_due = max(it.next_due + it.interval_ms / 1000.0, now)
        return Job(it.unit, it.fc, it.addr, it.reg_count(), item=it)

    def tick(self, now, send, timeout_ms):
        """
        send(bytes): 실제 송신 함수
        반환: [("timeout", job, None) | ("error", job, 메시지)]  (응답은 on_result 로 따로 온다)
        """
        events = []
        if self.inflight is not None:
            if (now - self.inflight.sent) * 1000 < timeout_ms:
                return events
            job, self.inflight = self.inflight, None
            self.handler.check_timeout(job.key)
            self.timeouts += 1
            if job.item is not None:
                self._fail(job.item, "타임아웃", now)
            events.append(("timeout", job, None))
        job = self._next_job(now)
        if job is None:
            return events
        try:
            frame, job.key = self.handler.make_request(job.unit, job.fc, job.addr, job.count, job.values)
            send(frame)
        except Exception as e:
            if job.key is not None:
                self.handler.check_timeout(job.key)
            if job.item is not None:
                self._fail(job.item, "보내기 실패", now)
            events.append(("error", job, str(e)))
            return events
        job.sent = now
        self.inflight = job
        self.requests += 1
        return events

    def on_result(self, result, now):
        """응답이 지금 기다리는 요청 것이면 그 Job 을 돌려준다 (폴링 값은 여기서 갱신)."""
        job = self.inflight
        if job is None or result["req"].get("key") != job.key:
            return None
        self.inflight = None
        it = job.item
        if it is not None:
            if result["ok"] and len(result["rows"]) >= it.reg_count():
                try:
                    it.value = it.decode([v for _, v in result["rows"][:it.reg_count()]])
                    it.ok = True
                    it.status = "OK %dms" % result["elapsed_ms"]
                    it.updated = now
                    it.ok_count += 1
                    it.history.append((now, float(it.value)))
                except (struct.error, ValueError, OverflowError) as e:
                    self._fail(it, "해석 오류: %s" % e, now)
            else:
                self._fail(it, result["text"], now)
        return job

    def _fail(self, it, text, now):
        it.ok = False
        it.status = text
        it.err_count += 1
        it.updated = now
