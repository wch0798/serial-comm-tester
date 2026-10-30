"""
시나리오 실행: 보내기 → 응답 확인 → 기다리기 … 를 순서대로 자동 실행하고 통과/실패를 기록한다.

화면과 무관한 순수 로직이다. 화면은 주기적으로 tick()을 부르고, 수신 데이터를 feed()로 넣어준다.
"""

import checks
import serial_core as core

STEP_SEND = "send"
STEP_EXPECT = "expect"
STEP_WAIT = "wait"
STEP_KINDS = [(STEP_SEND, "보내기"), (STEP_EXPECT, "응답 확인"), (STEP_WAIT, "기다리기")]

PASS = "통과"
FAIL = "실패"
RUN = "실행 중"

MAX_RX = 65536
MAX_RECORDS = 100000  # 보고서용 단계 결과 보관 수 (무한 반복 대비)

END_DONE = "완료"
END_FAIL = "실패로 멈춤"
END_USER = "사용자 정지"
END_TIME = "제한 시간 초과"
END_CLOSED = "연결 끊김"


def new_step(kind=STEP_SEND, data="", fmt=core.FMT_TEXT, check="none", ms=1000, enabled=True):
    return dict(kind=kind, data=data, fmt=fmt, check=check, ms=int(ms), enabled=bool(enabled))


def describe(step):
    kind = step.get("kind")
    if kind == STEP_WAIT:
        return "%d ms 기다리기" % step.get("ms", 0)
    data = step.get("data", "")
    text = "%s [%s]" % (data, step.get("fmt"))
    if kind == STEP_SEND:
        if step.get("check", "none") != "none":
            text += " + " + checks.label_of(step["check"])
        return "보내기: " + text
    if not data:
        return "아무 응답이나 확인 (최대 %d ms)" % step.get("ms", 0)
    return "응답에 %s 포함 확인 (최대 %d ms)" % (text, step.get("ms", 0))


def step_bytes(step):
    data = core.to_bytes(step.get("data", ""), step.get("fmt", core.FMT_TEXT))
    if step.get("kind") == STEP_SEND and step.get("fmt") != core.FMT_CAN:
        data = checks.append(data, step.get("check", "none"))
    return data


class ScenarioRunner(object):
    def __init__(self, steps, repeat=1, stop_on_fail=True):
        # (원래 목록 번호, 단계) - 꺼진 단계는 건너뛴다
        self.steps = [(i, s) for i, s in enumerate(steps) if s.get("enabled", True)]
        self.repeat = int(repeat)  # 0 = 무한 반복
        self.stop_on_fail = stop_on_fail
        self.loop = 0
        self.pos = 0
        self.running = False
        self.rx = bytearray()
        self.step_start = 0.0
        self.passed = 0
        self.failed = 0
        # 보고서용 기록
        self.records = []        # [dict(loop, no, desc, status, msg, time, ms)]
        self.dropped = 0         # MAX_RECORDS 를 넘어 버린 기록 수
        self.started_at = None
        self.ended_at = None
        self.end_reason = ""

    def start(self, now):
        if not self.steps:
            self.end_reason = "실행할 단계 없음"
            return [(None, FAIL, "실행할 단계가 없습니다.")]
        self.running = True
        self.loop = 1
        self.pos = 0
        self.passed = self.failed = 0
        self.records = []
        self.dropped = 0
        self.started_at = now
        self.ended_at = None
        self.end_reason = ""
        self.step_start = now
        self.rx = bytearray()
        return [(self.steps[0][0], RUN, "")]

    def stop(self, reason=END_USER, now=None):
        if self.running:
            self.running = False
            self._end(reason, now)

    def _end(self, reason, now=None):
        if not self.end_reason:
            self.end_reason = reason
        if self.ended_at is None:
            self.ended_at = now if now is not None else self.step_start

    def ok(self):
        """끝까지 실패 없이 돌았는가"""
        return self.end_reason == END_DONE and self.failed == 0 and self.passed > 0

    def feed(self, data):
        if self.running:
            self.rx += data
            if len(self.rx) > MAX_RX:
                del self.rx[:-MAX_RX]

    def _advance(self, now, events):
        self.pos += 1
        if self.pos >= len(self.steps):
            self.pos = 0
            self.loop += 1
            if self.repeat and self.loop > self.repeat:
                self.loop = self.repeat
                self.running = False
                self._end(END_DONE, now)
                return
        self.step_start = now
        events.append((self.steps[self.pos][0], RUN, ""))

    def _result(self, ok, msg, now, events):
        idx, step = self.steps[self.pos]
        if ok:
            self.passed += 1
        else:
            self.failed += 1
        status = PASS if ok else FAIL
        events.append((idx, status, msg))
        if len(self.records) >= MAX_RECORDS:
            del self.records[:1000]
            self.dropped += 1000
        self.records.append(dict(loop=self.loop, no=idx + 1, desc=describe(step), status=status, msg=msg,
                                 time=now, ms=int((now - self.step_start) * 1000)))
        if not ok and self.stop_on_fail:
            self.running = False
            self._end(END_FAIL, now)
            return
        self._advance(now, events)

    def tick(self, now, send):
        """
        send(bytes): 실제로 보내는 함수 (실패 시 예외)
        반환: [(단계 번호, 상태, 메시지), ...]
        """
        events = []
        guard = 0
        while self.running and guard < 100:
            guard += 1
            idx, step = self.steps[self.pos]
            kind = step.get("kind")
            elapsed_ms = int((now - self.step_start) * 1000)
            if kind == STEP_SEND:
                try:
                    data = step_bytes(step)
                    self.rx = bytearray()  # 이 송신에 대한 응답만 확인하도록 비운다
                    send(data)
                except Exception as e:
                    self._result(False, "보내기 실패: %s" % e, now, events)
                    continue
                if step.get("fmt") == core.FMT_CAN:
                    self._result(True, "CAN 프레임 %d개 보냄" % data.count(b"\n"), now, events)
                else:
                    self._result(True, "%d바이트 보냄" % len(data), now, events)
            elif kind == STEP_WAIT:
                if elapsed_ms < step.get("ms", 0):
                    break
                self._result(True, "%d ms 기다림" % elapsed_ms, now, events)
            elif kind == STEP_EXPECT:
                try:
                    pattern = step_bytes(step)
                except ValueError as e:
                    self._result(False, "기대값 형식 오류: %s" % e, now, events)
                    continue
                if step.get("fmt") == core.FMT_CAN and pattern:
                    # CAN: 받은 프레임 중 하나가 기대 프레임(ID#데이터 앞부분)으로 시작하면 통과
                    pats = [p.strip() for p in pattern.split(b"\n") if p.strip()]
                    lines = bytes(self.rx).split(b"\n")
                    found = all(any(line.startswith(p) for line in lines) for p in pats)
                else:
                    found = (pattern in self.rx) if pattern else bool(self.rx)
                if found:
                    self._result(True, "응답 확인 (%d ms)" % elapsed_ms, now, events)
                elif elapsed_ms >= step.get("ms", 1000):
                    if step.get("fmt") == core.FMT_CAN:
                        got = " / ".join(bytes(self.rx).decode("ascii", "replace").split("\n")[-4:-1]) or "(받은 프레임 없음)"
                    else:
                        got = core.bytes_to_hex(bytes(self.rx[-32:])) or "(받은 데이터 없음)"
                    self._result(False, "시간 초과 %d ms - 받은 데이터: %s" % (elapsed_ms, got), now, events)
                else:
                    break
            else:
                self._result(False, "알 수 없는 단계", now, events)
        return events
