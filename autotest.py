"""
화면 없이 시나리오 자동 실행 → 엑셀 보고서 저장 → 결과 코드로 끝내기

출하 검사, 반복 시험, 야간 시험처럼 사람이 화면을 보지 않아도 되는 시험에 쓴다.
설정은 화면에서 만든 설정 파일(.json) 또는 엑셀 설정 파일(.xlsx)을 그대로 쓴다.

사용법
  python autotest.py 설정파일 [옵션]

옵션
  --tab 이름       시나리오를 실행할 탭 (여러 번 쓸 수 있음)
                   생략하면 '사용' 체크된 탭 중 시나리오 단계가 있는 탭 모두
  --report 파일    보고서 저장 위치 (기본: 설정 파일 폴더/reports/시험보고서_날짜_시각.xlsx)
  --repeat N       시나리오 반복 횟수를 이 값으로 바꿈 (0 = 무한, --max-time 과 함께)
  --wait 초        연결을 연 뒤 시험 시작 전에 기다리는 시간 (기본 1초)
  --max-time 초    전체 제한 시간. 넘으면 그때 멈춤
                   (무한 반복 탭은 '그때까지 반복'으로 보고 실패로 치지 않음)
  --no-comm        보고서에 통신 기록 시트를 넣지 않음
  --quiet          화면에 실패한 단계와 결과만 표시
  --list           설정 파일의 탭 목록만 보여 주고 끝냄

끝날 때 돌려주는 값 (배치 파일의 %ERRORLEVEL%, 셸의 $?)
  0 = 합격, 1 = 불합격, 2 = 설정/연결 오류로 시험 못 함
"""

import argparse
import json
import os
import queue
import sys
import time

import modbus as mb
import report
import scenario as sc
import serial_core as core

EXIT_PASS, EXIT_FAIL, EXIT_ERROR = 0, 1, 2

CONN_TEXT = {core.CONN_PTY: "가상 시리얼", core.CONN_VCOM: "가상 COM", core.CONN_SERIAL: "시리얼",
             core.CONN_TCP_SERVER: "TCP 서버", core.CONN_TCP_CLIENT: "TCP 클라이언트", core.CONN_UDP: "UDP",
             core.CONN_CAN: "CAN"}


class SetupError(Exception):
    pass


def parse_int(s, default=0):
    s = str(s).strip()
    if not s:
        return default
    try:
        return int(s, 0)
    except ValueError:
        return int(float(s))


def load_config(path):
    """반환: (설정 dict, 경고 목록)"""
    if not os.path.isfile(path):
        raise SetupError("설정 파일이 없습니다: %s" % path)
    if path.lower().endswith((".xlsx", ".xlsm")):
        import excel_io
        try:
            return excel_io.import_config(path)
        except excel_io.ExcelError as e:
            raise SetupError(str(e))
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f), []
    except (OSError, ValueError) as e:
        raise SetupError("설정 파일을 읽을 수 없습니다: %s" % e)


def real_conn(cfg):
    """다른 OS에서 저장한 가상 시리얼 설정도 이 OS의 가상 시리얼로 연다 (화면과 같은 규칙)"""
    conn = cfg.get("conn") or core.CONN_SERIAL
    if conn in (core.CONN_PTY, core.CONN_VCOM):
        conn = core.CONN_VCOM if core.IS_WINDOWS else core.CONN_PTY
    return conn


def conn_text(cfg):
    conn = real_conn(cfg)
    name = CONN_TEXT.get(conn, conn)
    if conn == core.CONN_PTY:
        return "%s %s" % (name, cfg.get("link_path", ""))
    if conn == core.CONN_VCOM:
        return "%s %s" % (name, cfg.get("vcom_name", ""))
    if conn == core.CONN_SERIAL:
        return "%s %s %s %s%s%s" % (name, cfg.get("port", ""), cfg.get("baudrate", ""), cfg.get("bytesize", 8),
                                    cfg.get("parity", "N"), cfg.get("stopbits", 1))
    if conn in (core.CONN_TCP_SERVER, core.CONN_TCP_CLIENT):
        return "%s %s:%s" % (name, cfg.get("host", ""), cfg.get("tcp_port", ""))
    if conn == core.CONN_UDP:
        return "%s %s:%s → %s:%s" % (name, cfg.get("udp_bind", ""), cfg.get("udp_port", ""),
                                     cfg.get("host", ""), cfg.get("tcp_port", ""))
    if conn == core.CONN_CAN:
        return "%s %s %s" % (name, cfg.get("can_iface", ""), cfg.get("can_channel", ""))
    return name


class Session(object):
    """탭 1개 (화면의 PortTab 에서 시험에 필요한 부분만)"""

    def __init__(self, cfg, events):
        self.cfg = cfg
        self.name = cfg.get("name") or "연결"
        self.conn = real_conn(cfg)
        self.proto = cfg.get("protocol", "raw")
        self.channel = core.Channel(lambda kind, data, src: events.put((self, kind, data, src)))
        self.channel.faults.load_dict(cfg.get("faults", {}))
        self.store = None
        self.sim = []
        self.runner = None
        self.closed = False

        rules = [core.Rule.from_dict(d) for d in cfg.get("rules", [])]
        frame = cfg.get("frame") or mb.FRAME_RTU
        if self.proto == "mb_slave":
            s = cfg.get("slave", {})
            self.store = mb.DataStore()
            self.store.load_dict(s.get("data", {}))
            h = mb.SlaveHandler(self.store)
            h.unit_id = parse_int(s.get("unit_id", 1), 1)
            h.delay_ms = max(0, parse_int(s.get("delay_ms", 0)))
            h.no_response = bool(s.get("no_response", False))
            h.faults = self.channel.faults
            h.frame_type = frame
            self.sim = [mb.SimEntry.from_dict(d) for d in cfg.get("sim", [])]
        elif self.proto == "mb_master":
            h = mb.MasterHandler()
            h.frame_type = frame
        elif self.conn == core.CONN_CAN:
            h = core.CanRuleHandler(rules)
            h.enabled = bool(cfg.get("auto_enabled", True))
        else:
            h = core.RawHandler(rules)
            h.enabled = bool(cfg.get("auto_enabled", True))
        self.channel.handler = h

    def steps(self):
        return [s for s in self.cfg.get("scenario", {}).get("steps", []) if s.get("enabled", True)]

    def open(self):
        cfg = self.cfg
        conn, port = self.conn, cfg.get("port", "")
        if self.conn == core.CONN_CAN and self.proto != "raw":
            raise SetupError("CAN은 '일반 (자동 응답 규칙)' 프로토콜로만 쓸 수 있습니다.")
        if conn == core.CONN_VCOM:
            import vport_win as vport
            port, pair, created = vport.ensure_port(cfg.get("vcom_name", ""))
            conn = core.CONN_SERIAL
        if conn == core.CONN_SERIAL and not port:
            raise SetupError("시리얼 포트가 지정되지 않았습니다.")
        tcp_port = str(cfg.get("tcp_port", "")).strip()
        return self.channel.open(
            conn, link_path=str(cfg.get("link_path", "")).strip(), port=port,
            baudrate=cfg.get("baudrate", 115200), bytesize=cfg.get("bytesize", 8),
            parity=cfg.get("parity", "N"), stopbits=cfg.get("stopbits", 1),
            host=str(cfg.get("host", "")).strip(), tcp_port=parse_int(tcp_port) if tcp_port else None,
            udp_bind=str(cfg.get("udp_bind", "")).strip(), udp_local_port=parse_int(cfg.get("udp_port", 0)),
            can_iface=cfg.get("can_iface", "virtual"), can_channel=str(cfg.get("can_channel", "vcan0")).strip(),
            can_bitrate=parse_int(cfg.get("can_bitrate", 500000), 500000))


class Console(object):
    def __init__(self, quiet=False, out=None):
        self.quiet = quiet
        self.out = out or sys.stdout

    def say(self, text, always=False):
        if always or not self.quiet:
            try:
                self.out.write(text + "\n")
                self.out.flush()
            except (OSError, ValueError):
                pass


def pick_sessions(tabs, wanted):
    """(열 탭 목록, 시나리오를 실행할 탭 목록)"""
    names = [t.get("name", "") for t in tabs]
    if wanted:
        missing = [w for w in wanted if w not in names]
        if missing:
            raise SetupError("설정 파일에 없는 탭: %s  (있는 탭: %s)" % (", ".join(missing), ", ".join(names)))
        run = [t for t in tabs if t.get("name") in wanted]
    else:
        run = [t for t in tabs if t.get("enabled")]
    run = [t for t in run if any(s.get("enabled", True) for s in t.get("scenario", {}).get("steps", []))]
    opened = [t for t in tabs if t.get("enabled") or t in run]
    # 서버 쪽을 먼저 열어야 클라이언트가 붙을 수 있다
    opened.sort(key=lambda t: real_conn(t) == core.CONN_TCP_CLIENT)
    return opened, run


def run(args, console=None):
    """시험 실행. 반환: (종료 코드, 보고서 경로 또는 None)"""
    con = console or Console(args.quiet)
    try:
        cfg, warnings = load_config(args.config)
    except SetupError as e:
        con.say("오류: %s" % e, True)
        return EXIT_ERROR, None
    for w in warnings:
        con.say("경고: %s" % w, True)
    tabs = cfg.get("tabs", [])

    if args.list:
        con.say("탭 목록 (%d개)" % len(tabs), True)
        for t in tabs:
            n = len([s for s in t.get("scenario", {}).get("steps", []) if s.get("enabled", True)])
            con.say("  %s %-16s %-36s 시나리오 %d단계" % ("☑" if t.get("enabled") else "☐", t.get("name", ""),
                                                      conn_text(t), n), True)
        return EXIT_PASS, None

    try:
        open_cfgs, run_cfgs = pick_sessions(tabs, args.tab)
    except SetupError as e:
        con.say("오류: %s" % e, True)
        return EXIT_ERROR, None
    if not run_cfgs:
        con.say("오류: 실행할 시나리오가 없습니다. 탭의 '사용' 체크와 시나리오 단계를 확인하거나 --tab 으로 지정하세요.", True)
        return EXIT_ERROR, None

    events = queue.Queue()
    comm = None if args.no_comm else report.CommLog()
    sessions = [Session(c, events) for c in open_cfgs]
    by_name = {s.name: s for s in sessions}
    runners = [by_name[c.get("name")] for c in run_cfgs]
    results = {}
    setup_failed = False

    con.say("=== 통신 시험 시작: %s ===" % args.config, True)
    try:
        for s in sessions:
            try:
                dev = s.open()
                con.say("열림  [%s] %s  (%s)" % (s.name, conn_text(s.cfg), dev), True)
            except Exception as e:
                setup_failed = True
                con.say("열기 실패 [%s] %s: %s" % (s.name, conn_text(s.cfg), e), True)
                results[s.name] = report.error_result(s.name, conn_text(s.cfg), "열기 실패: %s" % e)
        # 중계 연결
        for s in sessions:
            peer = by_name.get(s.cfg.get("relay", ""))
            if peer is not None and peer is not s:
                s.channel.forward = peer.channel
                peer.channel.forward = s.channel

        def drain():
            while True:
                try:
                    s, kind, data, src = events.get_nowait()
                except queue.Empty:
                    return
                if comm is not None:
                    comm.add(s.name, kind, data, src)
                if kind == "rx" and s.runner is not None:
                    s.runner.feed(data)
                elif kind == "error":
                    con.say("  [%s] 오류: %s" % (s.name, data), True)
                elif kind == "closed":
                    s.closed = True
                    s.channel.close()
                    if s.runner is not None:
                        s.runner.stop(sc.END_CLOSED, time.time())

        # 연결 안정 대기 (TCP 접속, 장비 준비 등)
        end_wait = time.time() + max(0.0, args.wait)
        while time.time() < end_wait:
            drain()
            time.sleep(0.02)

        active = []
        for s in runners:
            if s.name in results:
                continue
            sc_cfg = s.cfg.get("scenario", {})
            repeat = parse_int(sc_cfg.get("repeat", 1), 1) if args.repeat is None else args.repeat
            if repeat == 0 and not args.max_time:
                con.say("경고: [%s] 무한 반복은 --max-time 이 있어야 합니다. 1회만 실행합니다." % s.name, True)
                repeat = 1
            s.runner = sc.ScenarioRunner(s.cfg.get("scenario", {}).get("steps", []), repeat,
                                         bool(sc_cfg.get("stop_on_fail", True)))
            con.say("시나리오 시작 [%s] %d단계, 반복 %s" % (s.name, len(s.steps()), repeat or "무한(제한 시간까지)"), True)
            report_events(con, s, s.runner.start(time.time()))
            active.append(s)

        deadline = time.time() + args.max_time if args.max_time else None
        while any(s.runner.running for s in active):
            now = time.time()
            drain()
            for s in sessions:
                if s.sim and s.store is not None:
                    mb.sim_tick(s.store, s.sim, now)
            for s in active:
                if s.runner.running:
                    report_events(con, s, s.runner.tick(now, s.channel.send))
            if deadline is not None and now >= deadline:
                for s in active:
                    if s.runner.running:
                        # 무한 반복은 '제한 시간까지 반복'이 정상 종료
                        s.runner.stop(sc.END_DONE if s.runner.repeat == 0 else sc.END_TIME, now)
                        con.say("  [%s] 제한 시간 %g초 도달" % (s.name, args.max_time), True)
                break
            time.sleep(0.005)
        time.sleep(0.05)
        drain()
        for s in active:
            results[s.name] = report.tab_result(s.name, conn_text(s.cfg), s.runner)
    except KeyboardInterrupt:
        con.say("사용자가 중단함 (Ctrl+C)", True)
        for s in runners:
            if s.runner is not None:
                s.runner.stop(sc.END_USER, time.time())
                results[s.name] = report.tab_result(s.name, conn_text(s.cfg), s.runner)
    finally:
        for s in sessions:
            s.channel.close()

    ordered = [results[s.name] for s in runners if s.name in results]
    ok = report.overall_ok(ordered) and not setup_failed

    con.say("=== 결과 ===", True)
    for r in ordered:
        con.say("  %-6s [%s] 통과 %d · 실패 %d · %s" % (report.PASS_TEXT if r["ok"] else report.FAIL_TEXT, r["name"],
                                                    r["passed"], r["failed"], r["end_reason"]), True)
    for name, r in results.items():
        if name not in [x["name"] for x in ordered]:
            ordered.append(r)
            con.say("  %-6s [%s] %s" % (report.FAIL_TEXT, name, r["end_reason"]), True)

    path = args.report or os.path.join(os.path.dirname(os.path.abspath(args.config)), "reports",
                                       report.default_name())
    try:
        report.save_excel(path, ordered, comm, source=os.path.abspath(args.config))
        con.say("보고서: %s" % path, True)
    except report.ReportError as e:
        con.say("오류: %s" % e, True)
        path = None
    con.say("판정: %s" % (report.PASS_TEXT if ok else report.FAIL_TEXT), True)
    if setup_failed and not any(r["records"] for r in ordered):
        return EXIT_ERROR, path
    return (EXIT_PASS if ok else EXIT_FAIL), path


def report_events(con, s, events):
    # 결과 이벤트와 기록은 같은 순서로 쌓이므로, 회차 번호는 기록에서 가져온다
    n = sum(1 for idx, status, _ in events if idx is not None and status != sc.RUN)
    recs = s.runner.records[-n:] if n else []
    for idx, status, msg in events:
        if idx is None:
            con.say("  [%s] %s" % (s.name, msg), True)
        elif status != sc.RUN:
            rec = recs.pop(0) if recs else {"loop": s.runner.loop}
            con.say("  [%s] %d회 %d단계 %s: %s" % (s.name, rec["loop"], idx + 1, status, msg),
                    always=status == sc.FAIL)


class KoreanHelp(argparse.HelpFormatter):
    def add_usage(self, usage, actions, groups, prefix=None):
        return argparse.HelpFormatter.add_usage(self, usage, actions, groups, prefix="사용법: ")


class KoreanParser(argparse.ArgumentParser):
    MESSAGES = [("the following arguments are required", "꼭 넣어야 하는 값이 없습니다"),
                ("unrecognized arguments", "알 수 없는 옵션"), ("expected one argument", "값을 하나 넣어야 합니다"),
                ("invalid int value", "정수가 아닙니다"), ("invalid float value", "숫자가 아닙니다"),
                ("argument ", "")]

    def error(self, message):
        for en, ko in self.MESSAGES:
            message = message.replace(en, ko)
        self.print_usage(sys.stderr)
        self.exit(EXIT_ERROR, "오류: %s\n" % message)


def make_parser():
    p = KoreanParser(
        prog="autotest", description="화면 없이 시나리오를 자동 실행하고 엑셀 보고서를 저장합니다.",
        epilog="결과 코드: 0 합격, 1 불합격, 2 설정/연결 오류", formatter_class=KoreanHelp, add_help=False)
    p._positionals.title = "필수"
    p._optionals.title = "옵션"
    p.add_argument("-h", "--help", action="help", help="이 도움말 보기")
    p.add_argument("config", help="설정 파일 (.json 또는 .xlsx)")
    p.add_argument("--tab", action="append", default=[], metavar="이름", help="시나리오를 실행할 탭 (여러 번 가능)")
    p.add_argument("--report", metavar="파일", help="보고서 저장 위치 (.xlsx)")
    p.add_argument("--repeat", type=int, metavar="N", help="반복 횟수 바꾸기 (0 = 무한, --max-time 필요)")
    p.add_argument("--wait", type=float, default=1.0, metavar="초", help="연결 후 시작 전 대기 (기본 1)")
    p.add_argument("--max-time", type=float, default=0, metavar="초", help="전체 제한 시간")
    p.add_argument("--no-comm", action="store_true", help="통신 기록 시트 생략")
    p.add_argument("--quiet", action="store_true", help="실패와 결과만 표시")
    p.add_argument("--list", action="store_true", help="탭 목록만 보기")
    return p


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    args = make_parser().parse_args(argv)
    code, _ = run(args)
    return code


if __name__ == "__main__":
    sys.exit(main())
