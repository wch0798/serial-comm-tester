"""
통신 테스트 도구 (Windows / Linux / macOS)
- 연결: 가상 시리얼(PTY), 시리얼 포트, TCP 서버, TCP 클라이언트
- 프로토콜: 일반(자동 응답 규칙), Modbus 슬레이브, Modbus 마스터

실행: python main.py
"""

import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

import checks
import excel_io
import modbus as mb
import polling
import serial_core as core
import theme
import ui_parts as ui
import vport_win as vport

APP_TITLE = "통신 테스트 도구 (시리얼 / TCP / Modbus)"
AUTOSAVE_MS = 3000


def default_config_path():
    """설정 파일 위치: 프로그램 폴더 (쓰기 불가면 사용자 폴더)"""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    if not os.access(base, os.W_OK):
        base = os.path.join(os.path.expanduser("~"), ".vserial")
        os.makedirs(base, exist_ok=True)
    return os.path.join(base, "vserial_config.json")


CONFIG_PATH = default_config_path()

BAUDRATES = ["1200", "2400", "4800", "9600", "19200", "38400", "57600", "115200", "230400", "460800", "921600"]
LINE_ENDINGS = {"없음": b"", "CR": b"\r", "LF": b"\n", "CRLF": b"\r\n"}

CHECK_ON = "☑"
CHECK_OFF = "☐"

if core.IS_WINDOWS:
    VIRTUAL_CONN = (core.CONN_VCOM, "가상 시리얼 (COM 자동 생성)")
else:
    VIRTUAL_CONN = (core.CONN_PTY, "가상 시리얼 (PTY)")
CONN_LABELS = [VIRTUAL_CONN, (core.CONN_SERIAL, "시리얼 포트 (COM)"),
               (core.CONN_TCP_SERVER, "TCP 서버"), (core.CONN_TCP_CLIENT, "TCP 클라이언트"), (core.CONN_UDP, "UDP"),
               (core.CONN_CAN, "CAN")]
CAN_IFACE_LABELS = [(k, lbl) for k, lbl, _, _ in core.CAN_IFACES]
NO_RELAY = "(없음)"

PROTO_RAW = "raw"
PROTO_SLAVE = "mb_slave"
PROTO_MASTER = "mb_master"
PROTO_LABELS = [(PROTO_RAW, "일반 (자동 응답 규칙)"), (PROTO_SLAVE, "Modbus 슬레이브 (장치 흉내)"),
                (PROTO_MASTER, "Modbus 마스터 (요청 보내기)")]

AREA_LABELS = [(k, label) for k, label, _ in mb.AREAS]
VALUE_FORMATS = ["10진", "16진", "부호 10진"]
FC_LABELS = [mb.FC_NAMES[k] for k in sorted(mb.FC_NAMES)]


def key_of(pairs, label, default=None):
    for k, lbl in pairs:
        if lbl == label:
            return k
    return default


def label_of(pairs, key):
    for k, lbl in pairs:
        if k == key:
            return lbl
    return pairs[0][1]


def parse_int(s):
    s = str(s).strip()
    try:
        return int(s, 0)
    except ValueError:
        return int(s, 10)


def run_async(widget, func, done):
    """func를 별도 스레드에서 실행하고, 끝나면 화면 스레드에서 done(결과, 오류)를 부른다."""
    result = {}

    def work():
        try:
            result["value"] = func()
        except Exception as e:
            result["error"] = e

    t = threading.Thread(target=work, daemon=True)
    t.start()

    def check():
        if t.is_alive():
            widget.after(100, check)
        else:
            done(result.get("value"), result.get("error"))

    widget.after(100, check)


def format_value(v, fmt, is_bit):
    if is_bit:
        return "1 (ON)" if v else "0 (OFF)"
    if fmt == "16진":
        return "0x%04X" % v
    if fmt == "부호 10진":
        return str(v - 0x10000 if v & 0x8000 else v)
    return str(v)


class PortTab(ttk.Frame):
    """탭 1개 = 연결 1개 + 처리 방식(규칙 / Modbus) + 로그"""

    def __init__(self, app, notebook, index, cfg=None):
        ttk.Frame.__init__(self, notebook, padding=6)
        self.app = app
        self.notebook = notebook
        self.channel = core.Channel(lambda kind, data, src: app.events.put((self, kind, data, src)))

        self.rules = []
        self.raw_handler = core.RawHandler(self.rules)
        self.can_handler = core.CanRuleHandler(self.rules)
        self.store = mb.DataStore()
        self.slave_handler = mb.SlaveHandler(self.store)
        self.master_handler = mb.MasterHandler()
        self.poller = polling.PollScheduler(self.master_handler)
        self._mloop = None
        self._last_poll_ui = 0.0
        self.editing_index = None
        self._slave_refresh_pending = False
        self._poll_job = None

        v = tk.StringVar
        self.var_name = v(value="연결 %d" % index)
        self.var_enabled = tk.BooleanVar(value=False)
        self.var_conn = v(value=CONN_LABELS[0][1])
        self.var_proto = v(value=PROTO_LABELS[0][1])
        self.var_frame = v(value=mb.FRAME_RTU)
        self.var_link = v(value="/tmp/vcom%d" % index if core.PTY_SUPPORTED else "")
        self.var_vcom = v(value="COM%d" % (9 + index))
        self.var_port = v()
        self.var_baud = v(value="115200")
        self.var_bytesize = v(value="8")
        self.var_parity = v(value="N")
        self.var_stopbits = v(value="1")
        self.var_host = v(value="127.0.0.1")
        self.var_tcp_port = v(value=str(5019 + index))
        self.var_udp_bind = v(value="0.0.0.0")
        self.var_udp_port = v(value=str(5029 + index))
        self.var_relay = v(value=NO_RELAY)
        self.var_can_iface = v(value=CAN_IFACE_LABELS[0][1])
        self.var_can_channel = v(value="vcan0")
        self.var_can_bitrate = v(value="500000")
        self.var_device = v(value="")
        self.var_status = v(value="닫힘")

        # 일반 규칙
        self.var_auto = tk.BooleanVar(value=True)
        self.var_r_enabled = tk.BooleanVar(value=True)
        self.var_r_match = v()
        self.var_r_match_fmt = v(value=core.FMT_TEXT)
        self.var_r_resp = v()
        self.var_r_resp_fmt = v(value=core.FMT_TEXT)
        self.var_r_delay = v(value="0")
        self.var_r_check = v(value=ui.CHECK_LABELS[0])

        # Modbus 슬레이브
        self.var_s_unit = v(value="1")
        self.var_s_delay = v(value="0")
        self.var_s_noresp = tk.BooleanVar(value=False)
        self.var_s_area = v(value=label_of(AREA_LABELS, mb.AREA_HR))
        self.var_s_start = v(value="0")
        self.var_s_count = v(value="50")
        self.var_s_fmt = v(value=VALUE_FORMATS[0])

        # Modbus 마스터
        self.var_m_unit = v(value="1")
        self.var_m_fc = v(value=mb.FC_NAMES[3])
        self.var_m_addr = v(value="0")
        self.var_m_count = v(value="10")
        self.var_m_values = v()
        self.var_m_poll = tk.BooleanVar(value=False)
        self.var_m_poll_ms = v(value="1000")
        self.var_m_timeout = v(value="1000")
        self.var_m_fmt = v(value=VALUE_FORMATS[0])
        self.var_m_status = v(value="")

        # 보내기
        self.var_send = v()
        self.var_send_check = v(value=ui.CHECK_LABELS[0])
        self.var_send_repeat = tk.BooleanVar(value=False)
        self.var_send_repeat_ms = v(value="1000")
        self._send_job = None
        self.var_send_fmt = v(value=core.FMT_TEXT)
        self.var_send_eol = v(value="없음")

        self._build()
        if cfg:
            self.load_config(cfg)

        self.var_name.trace_add("write", lambda *a: self.update_title())
        self.var_conn.trace_add("write", lambda *a: self.on_conn_changed())
        self.var_proto.trace_add("write", lambda *a: self.update_proto_widgets())
        self.var_auto.trace_add("write", lambda *a: self.apply_handler_settings())
        for var in (self.var_s_unit, self.var_s_delay, self.var_s_noresp):
            var.trace_add("write", lambda *a: self.apply_handler_settings())
        for var in (self.var_s_area, self.var_s_start, self.var_s_count, self.var_s_fmt):
            var.trace_add("write", lambda *a: self.refresh_slave_tree())
        self.var_m_fc.trace_add("write", lambda *a: self.update_master_widgets())
        self.var_relay.trace_add("write", lambda *a: self.app.update_relays())
        self.var_can_iface.trace_add("write", lambda *a: self.on_can_iface_changed())

        self.update_conn_widgets()
        self.update_proto_widgets()
        self.update_master_widgets()
        self.apply_handler_settings()
        self.refresh_slave_tree()

    # ------------------------------------------------------------------ 상태 값
    @property
    def conn(self):
        return key_of(CONN_LABELS, self.var_conn.get(), CONN_LABELS[0][0])

    @property
    def proto(self):
        return key_of(PROTO_LABELS, self.var_proto.get(), PROTO_RAW)

    @property
    def slave_area(self):
        return key_of(AREA_LABELS, self.var_s_area.get(), mb.AREA_HR)

    # ------------------------------------------------------------------ 화면 구성
    def _build(self):
        top = ttk.LabelFrame(self, text="연결 설정", padding=6)
        top.pack(fill="x")

        row1 = ttk.Frame(top)
        row1.pack(fill="x")
        self.chk_enabled = ttk.Checkbutton(row1, text="사용 (연결 열기)", variable=self.var_enabled,
                                           command=self.on_toggle_enabled)
        self.chk_enabled.pack(side="left")
        ttk.Label(row1, text="  이름").pack(side="left")
        ttk.Entry(row1, textvariable=self.var_name, width=12).pack(side="left", padx=4)
        ttk.Label(row1, text="  연결 방식").pack(side="left")
        self.cb_conn = ttk.Combobox(row1, textvariable=self.var_conn, state="readonly", width=15,
                                    values=[lbl for _, lbl in CONN_LABELS])
        self.cb_conn.pack(side="left", padx=4)
        ttk.Label(row1, text="  프로토콜").pack(side="left")
        self.cb_proto = ttk.Combobox(row1, textvariable=self.var_proto, state="readonly", width=22,
                                     values=[lbl for _, lbl in PROTO_LABELS])
        self.cb_proto.pack(side="left", padx=4)
        self.frm_frame = ttk.Frame(row1)
        ttk.Label(self.frm_frame, text="  프레임").pack(side="left")
        self.cb_frame = ttk.Combobox(self.frm_frame, textvariable=self.var_frame, state="readonly", width=5,
                                     values=[mb.FRAME_RTU, mb.FRAME_TCP])
        self.cb_frame.pack(side="left", padx=4)

        row_status = ttk.Frame(top)
        row_status.pack(fill="x", pady=(6, 0))
        ttk.Label(row_status, text="상태:").pack(side="left")
        self.lbl_status = ttk.Label(row_status, textvariable=self.var_status, style="Closed.TLabel")
        self.lbl_status.pack(side="left", padx=4)
        ttk.Entry(row_status, textvariable=self.var_device, width=30, state="readonly").pack(side="left", padx=4)
        ttk.Button(row_status, text="경로 복사", command=self.copy_device).pack(side="left")
        ttk.Label(row_status, text="    중계 대상").pack(side="left")
        self.cb_relay = ttk.Combobox(row_status, textvariable=self.var_relay, width=14, state="readonly",
                                     postcommand=self.refresh_relay_choices)
        self.cb_relay.pack(side="left", padx=4)
        ttk.Label(row_status, text="(받은 데이터를 그 탭으로 전달)", style="Muted.TLabel").pack(side="left")

        # 연결 방식별 설정
        self.frm_pty = ttk.Frame(top)
        ttk.Label(self.frm_pty, text="링크 경로(선택)").pack(side="left")
        self.ent_link = ttk.Entry(self.frm_pty, textvariable=self.var_link, width=26)
        self.ent_link.pack(side="left", padx=4)
        ttk.Label(self.frm_pty, text="← 다른 프로그램은 이 경로(또는 상태에 표시된 장치 경로)로 접속",
                  style="Muted.TLabel").pack(side="left")

        self.frm_vcom = ttk.Frame(top)
        ttk.Label(self.frm_vcom, text="가상 포트 이름").pack(side="left")
        self.ent_vcom = ttk.Entry(self.frm_vcom, textvariable=self.var_vcom, width=10)
        self.ent_vcom.pack(side="left", padx=4)
        ttk.Label(self.frm_vcom, style="Muted.TLabel",
                  text="← 다른 프로그램은 이 COM 포트로 접속 (없으면 자동으로 만듦)").pack(side="left")
        ttk.Button(self.frm_vcom, text="가상 포트 관리…",
                   command=lambda: self.app.open_vport_manager()).pack(side="left", padx=8)

        self.frm_ser = ttk.Frame(top)
        ttk.Label(self.frm_ser, text="포트").pack(side="left")
        self.cb_port = ttk.Combobox(self.frm_ser, textvariable=self.var_port, width=16, postcommand=self.refresh_ports)
        self.cb_port.pack(side="left", padx=4)
        ttk.Label(self.frm_ser, text="Baud").pack(side="left")
        self.cb_baud = ttk.Combobox(self.frm_ser, textvariable=self.var_baud, values=BAUDRATES, width=8)
        self.cb_baud.pack(side="left", padx=4)
        ttk.Label(self.frm_ser, text="Data").pack(side="left")
        self.cb_bytesize = ttk.Combobox(self.frm_ser, textvariable=self.var_bytesize, values=["5", "6", "7", "8"],
                                        width=3, state="readonly")
        self.cb_bytesize.pack(side="left", padx=4)
        ttk.Label(self.frm_ser, text="Parity").pack(side="left")
        self.cb_parity = ttk.Combobox(self.frm_ser, textvariable=self.var_parity, values=["N", "E", "O", "M", "S"],
                                      width=3, state="readonly")
        self.cb_parity.pack(side="left", padx=4)
        ttk.Label(self.frm_ser, text="Stop").pack(side="left")
        self.cb_stopbits = ttk.Combobox(self.frm_ser, textvariable=self.var_stopbits, values=["1", "1.5", "2"],
                                        width=4, state="readonly")
        self.cb_stopbits.pack(side="left", padx=4)
        if core.IS_WINDOWS:
            ttk.Label(self.frm_ser, style="Muted.TLabel",
                      text="(가상 포트는 VSPE, com0com 등 무료 도구로 만든 포트를 선택)").pack(side="left", padx=4)

        self.frm_can = ttk.Frame(top)
        ttk.Label(self.frm_can, text="인터페이스").pack(side="left")
        self.cb_can_iface = ttk.Combobox(self.frm_can, textvariable=self.var_can_iface, state="readonly", width=23,
                                         values=[lbl for _, lbl in CAN_IFACE_LABELS])
        self.cb_can_iface.pack(side="left", padx=4)
        ttk.Label(self.frm_can, text="채널").pack(side="left", padx=(6, 0))
        self.ent_can_channel = ttk.Entry(self.frm_can, textvariable=self.var_can_channel, width=14)
        self.ent_can_channel.pack(side="left", padx=4)
        ttk.Label(self.frm_can, text="비트레이트").pack(side="left", padx=(6, 0))
        self.cb_can_bitrate = ttk.Combobox(self.frm_can, textvariable=self.var_can_bitrate, width=8,
                                           values=core.CAN_BITRATES)
        self.cb_can_bitrate.pack(side="left", padx=4)
        self.lbl_can_hint = ttk.Label(self.frm_can, style="Muted.TLabel", wraplength=330, justify="left")
        self.lbl_can_hint.pack(side="left", padx=6)

        self.frm_tcp = ttk.Frame(top)
        self.frm_udp_local = ttk.Frame(self.frm_tcp)
        ttk.Label(self.frm_udp_local, text="내 IP").pack(side="left")
        self.ent_udp_bind = ttk.Entry(self.frm_udp_local, textvariable=self.var_udp_bind, width=12)
        self.ent_udp_bind.pack(side="left", padx=4)
        ttk.Label(self.frm_udp_local, text="내 포트").pack(side="left")
        self.ent_udp_port = ttk.Entry(self.frm_udp_local, textvariable=self.var_udp_port, width=7)
        self.ent_udp_port.pack(side="left", padx=(4, 12))
        self.lbl_host = ttk.Label(self.frm_tcp, text="IP 주소")
        self.lbl_host.pack(side="left")
        self.ent_host = ttk.Entry(self.frm_tcp, textvariable=self.var_host, width=16)
        self.ent_host.pack(side="left", padx=4)
        self.lbl_port = ttk.Label(self.frm_tcp, text="포트 번호")
        self.lbl_port.pack(side="left")
        self.ent_tcp_port = ttk.Entry(self.frm_tcp, textvariable=self.var_tcp_port, width=7)
        self.ent_tcp_port.pack(side="left", padx=4)
        self.lbl_tcp_hint = ttk.Label(self.frm_tcp, style="Muted.TLabel")
        self.lbl_tcp_hint.pack(side="left", padx=4)

        self.setting_widgets = [self.cb_conn, self.cb_proto, self.cb_frame, self.ent_link, self.ent_vcom, self.cb_port,
                                self.cb_baud, self.cb_bytesize, self.cb_parity, self.cb_stopbits,
                                self.ent_host, self.ent_tcp_port, self.ent_udp_bind, self.ent_udp_port,
                                self.cb_can_iface, self.ent_can_channel, self.cb_can_bitrate]

        # 본문: 왼쪽 처리 방식 / 오른쪽 로그
        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True, pady=(6, 0))
        self.left_nb = ttk.Notebook(paned)
        self.left = ttk.Frame(self.left_nb, padding=4)
        self.proto_frames = {
            PROTO_RAW: self._build_raw(self.left),
            PROTO_SLAVE: self._build_slave(self.left),
            PROTO_MASTER: self._build_master(self.left),
        }
        log_frame = self._build_log(paned)
        self.macro_panel = ui.MacroPanel(self.left_nb, self)
        self.scenario_panel = ui.ScenarioPanel(self.left_nb, self)
        self.fault_panel = ui.FaultPanel(self.left_nb, self)
        self.left_nb.add(self.left, text="응답 규칙")
        self.left_nb.add(self.macro_panel, text="매크로")
        self.left_nb.add(self.scenario_panel, text="시나리오")
        self.left_nb.add(self.fault_panel, text="오류 주입")
        paned.add(self.left_nb, weight=1)
        paned.add(log_frame, weight=1)

    def _build_raw(self, parent):
        frm = ttk.LabelFrame(parent, text="자동 응답 규칙 (이 데이터가 오면 → 이 데이터를 보낸다)", padding=6)

        bar = ttk.Frame(frm)
        bar.pack(fill="x")
        ttk.Checkbutton(bar, text="자동 응답 사용", variable=self.var_auto).pack(side="left")
        ttk.Label(bar, text="  (☑/☐ 칸을 클릭하면 규칙별 사용 전환)", style="Muted.TLabel").pack(side="left")

        cols = ("on", "match", "mfmt", "resp", "rfmt", "chk", "delay")
        tree_frm = ttk.Frame(frm)
        tree_frm.pack(fill="both", expand=True, pady=4)
        self.tree = ttk.Treeview(tree_frm, columns=cols, show="headings", height=10, selectmode="browse")
        for c, title, w, anchor in (("on", "사용", 38, "center"), ("match", "수신 데이터", 115, "w"),
                                    ("mfmt", "형식", 45, "center"), ("resp", "응답 데이터", 115, "w"),
                                    ("rfmt", "형식", 45, "center"), ("chk", "검사값", 70, "w"),
                                    ("delay", "지연(ms)", 55, "e")):
            self.tree.heading(c, text=title)
            self.tree.column(c, width=w, anchor=anchor, stretch=c in ("match", "resp"))
        sb = ttk.Scrollbar(tree_frm, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Button-1>", self.on_tree_click)
        self.tree.bind("<<TreeviewSelect>>", self.on_tree_select)
        self.tree.bind("<Delete>", lambda e: self.delete_rule())

        ed = ttk.LabelFrame(frm, text="규칙 편집", padding=6)
        ed.pack(fill="x")
        ed.columnconfigure(1, weight=1)
        ttk.Label(ed, text="수신").grid(row=0, column=0, sticky="w")
        ttk.Entry(ed, textvariable=self.var_r_match).grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Combobox(ed, textvariable=self.var_r_match_fmt, values=ui.FMTS,
                     width=6, state="readonly").grid(row=0, column=2)
        ttk.Label(ed, text="응답").grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Entry(ed, textvariable=self.var_r_resp).grid(row=1, column=1, sticky="ew", padx=4, pady=(4, 0))
        ttk.Combobox(ed, textvariable=self.var_r_resp_fmt, values=ui.FMTS,
                     width=6, state="readonly").grid(row=1, column=2, pady=(4, 0))
        chk = ttk.Frame(ed)
        chk.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        ttk.Label(chk, text="응답 끝에 검사값 자동 추가").pack(side="left")
        ttk.Combobox(chk, textvariable=self.var_r_check, values=ui.CHECK_LABELS, width=22,
                     state="readonly").pack(side="left", padx=4)

        opt = ttk.Frame(ed)
        opt.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        ttk.Checkbutton(opt, text="사용", variable=self.var_r_enabled).pack(side="left")
        ttk.Label(opt, text="  지연(ms)").pack(side="left")
        ttk.Spinbox(opt, from_=0, to=60000, increment=10, textvariable=self.var_r_delay, width=7).pack(side="left", padx=4)
        ttk.Button(opt, text="▼", width=3, command=lambda: self.move_rule(1)).pack(side="right")
        ttk.Button(opt, text="▲", width=3, command=lambda: self.move_rule(-1)).pack(side="right")
        ttk.Button(opt, text="삭제", width=5, command=self.delete_rule).pack(side="right", padx=2)
        ttk.Button(opt, text="수정", width=5, command=self.update_rule).pack(side="right", padx=2)
        ttk.Button(opt, text="추가", width=5, command=self.add_rule).pack(side="right", padx=2)
        ttk.Button(opt, text="새로", width=5, command=self.clear_editor).pack(side="right", padx=2)

        ttk.Label(ed, style="Muted.TLabel",
                  text="TEXT: \\r \\n \\t \\xNN 사용 가능  /  HEX: 01 02 0A 또는 01020A").grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(4, 0))
        return frm

    def _build_slave(self, parent):
        frm = ttk.LabelFrame(parent, text="Modbus 슬레이브 (요청이 오면 아래 값으로 응답)", padding=6)

        r1 = ttk.Frame(frm)
        r1.pack(fill="x")
        ttk.Label(r1, text="장치 주소").pack(side="left")
        ttk.Spinbox(r1, from_=0, to=247, textvariable=self.var_s_unit, width=5).pack(side="left", padx=4)
        ttk.Label(r1, text="(0=모두)", style="Muted.TLabel").pack(side="left")
        ttk.Label(r1, text="  응답 지연(ms)").pack(side="left")
        ttk.Spinbox(r1, from_=0, to=60000, increment=10, textvariable=self.var_s_delay, width=7).pack(side="left", padx=4)
        ttk.Checkbutton(r1, text="응답 안 함", variable=self.var_s_noresp).pack(side="left", padx=4)

        sub = ttk.Notebook(frm)
        sub.pack(fill="both", expand=True, pady=(6, 0))
        page = ttk.Frame(sub, padding=4)
        self.sim_panel = ui.SimPanel(sub, self)
        sub.add(page, text="값 표")
        sub.add(self.sim_panel, text="값 자동 변화")

        r2 = ttk.Frame(page)
        r2.pack(fill="x", pady=(6, 0))
        ttk.Label(r2, text="영역").pack(side="left")
        ttk.Combobox(r2, textvariable=self.var_s_area, values=[lbl for _, lbl in AREA_LABELS],
                     width=24, state="readonly").pack(side="left", padx=4)
        ttk.Label(r2, text="시작").pack(side="left")
        ttk.Entry(r2, textvariable=self.var_s_start, width=6).pack(side="left", padx=4)
        ttk.Label(r2, text="개수").pack(side="left")
        ttk.Entry(r2, textvariable=self.var_s_count, width=5).pack(side="left", padx=4)
        ttk.Combobox(r2, textvariable=self.var_s_fmt, values=VALUE_FORMATS, width=8,
                     state="readonly").pack(side="left", padx=4)

        tree_frm = ttk.Frame(page)
        tree_frm.pack(fill="both", expand=True, pady=4)
        self.s_tree = ttk.Treeview(tree_frm, columns=("addr", "plc", "value"), show="headings", height=12)
        for c, title, w in (("addr", "주소", 70), ("plc", "PLC 주소", 80), ("value", "값", 140)):
            self.s_tree.heading(c, text=title)
            self.s_tree.column(c, width=w, anchor="e" if c != "value" else "w", stretch=c == "value")
        sb = ttk.Scrollbar(tree_frm, orient="vertical", command=self.s_tree.yview)
        self.s_tree.configure(yscrollcommand=sb.set)
        self.s_tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.s_tree.bind("<Double-1>", self.edit_slave_value)
        self.s_tree.bind("<Return>", self.edit_slave_value)

        r3 = ttk.Frame(page)
        r3.pack(fill="x")
        ttk.Label(r3, text="값을 더블클릭하면 수정합니다. 마스터가 쓴 값도 바로 반영됩니다.",
                  style="Muted.TLabel").pack(side="left")
        ttk.Button(r3, text="이 영역 모두 0", command=self.clear_slave_area).pack(side="right")
        return frm

    def _build_master(self, parent):
        frm = ttk.LabelFrame(parent, text="Modbus 마스터 (요청을 보내고 응답을 확인)", padding=6)
        sub = ttk.Notebook(frm)
        sub.pack(fill="both", expand=True)
        page = ttk.Frame(sub, padding=4)
        self.poll_panel = ui.PollPanel(sub, self)
        sub.add(page, text="단일 요청")
        sub.add(self.poll_panel, text="폴링 목록")

        g = ttk.Frame(page)
        g.pack(fill="x")
        ttk.Label(g, text="장치 주소").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(g, from_=0, to=247, textvariable=self.var_m_unit, width=5).grid(row=0, column=1, sticky="w", padx=4)
        ttk.Label(g, text="기능 코드").grid(row=0, column=2, sticky="w", padx=(12, 0))
        ttk.Combobox(g, textvariable=self.var_m_fc, values=FC_LABELS, width=22,
                     state="readonly").grid(row=0, column=3, sticky="w", padx=4)
        ttk.Label(g, text="시작 주소").grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Entry(g, textvariable=self.var_m_addr, width=7).grid(row=1, column=1, sticky="w", padx=4, pady=(4, 0))
        self.lbl_m_count = ttk.Label(g, text="개수")
        self.lbl_m_count.grid(row=1, column=2, sticky="w", padx=(12, 0), pady=(4, 0))
        self.ent_m_count = ttk.Entry(g, textvariable=self.var_m_count, width=7)
        self.ent_m_count.grid(row=1, column=3, sticky="w", padx=4, pady=(4, 0))

        w = ttk.Frame(page)
        w.pack(fill="x", pady=(4, 0))
        self.lbl_m_values = ttk.Label(w, text="쓰기 값")
        self.lbl_m_values.pack(side="left")
        self.ent_m_values = ttk.Entry(w, textvariable=self.var_m_values)
        self.ent_m_values.pack(side="left", fill="x", expand=True, padx=4)
        self.ent_m_values.bind("<Return>", lambda e: self.send_master_request())
        ttk.Label(w, text="예: 100, 0x1F, -1 / 코일: 1,0,1", style="Muted.TLabel").pack(side="left")

        b = ttk.Frame(page)
        b.pack(fill="x", pady=(6, 0))
        ttk.Button(b, text="요청 보내기", style="Accent.TButton", command=self.send_master_request).pack(side="left")
        ttk.Checkbutton(b, text="주기 반복", variable=self.var_m_poll, command=self.on_poll_toggle).pack(side="left", padx=(12, 0))
        ttk.Spinbox(b, from_=50, to=600000, increment=100, textvariable=self.var_m_poll_ms, width=7).pack(side="left", padx=4)
        ttk.Label(b, text="ms   응답 대기").pack(side="left")
        ttk.Spinbox(b, from_=50, to=60000, increment=100, textvariable=self.var_m_timeout, width=6).pack(side="left", padx=4)
        ttk.Label(b, text="ms").pack(side="left")

        st = ttk.Frame(page)
        st.pack(fill="x", pady=(6, 0))
        ttk.Label(st, text="결과:").pack(side="left")
        self.lbl_m_status = ttk.Label(st, textvariable=self.var_m_status)
        self.lbl_m_status.pack(side="left", padx=4)
        ttk.Combobox(st, textvariable=self.var_m_fmt, values=VALUE_FORMATS, width=8,
                     state="readonly").pack(side="right")

        tree_frm = ttk.Frame(page)
        tree_frm.pack(fill="both", expand=True, pady=4)
        self.m_tree = ttk.Treeview(tree_frm, columns=("addr", "plc", "value"), show="headings", height=10)
        for c, title, wd in (("addr", "주소", 70), ("plc", "PLC 주소", 80), ("value", "값", 140)):
            self.m_tree.heading(c, text=title)
            self.m_tree.column(c, width=wd, anchor="e" if c != "value" else "w", stretch=c == "value")
        sb = ttk.Scrollbar(tree_frm, orient="vertical", command=self.m_tree.yview)
        self.m_tree.configure(yscrollcommand=sb.set)
        self.m_tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.var_m_fmt.trace_add("write", lambda *a: self.show_master_rows())
        self.m_rows = []
        self.m_area = mb.AREA_HR
        return frm

    def _build_log(self, parent):
        frm = ttk.LabelFrame(parent, text="통신 로그", padding=6)
        self.logview = ui.LogView(frm, lambda: self.var_name.get(), self.app.log_dir)
        self.logview.pack(fill="both", expand=True)

        self.macro_bar = ttk.Frame(frm)
        self.macro_bar.pack(fill="x", pady=(4, 0))

        send = ttk.Frame(frm)
        send.pack(fill="x", pady=(4, 0))
        ent = ttk.Entry(send, textvariable=self.var_send)
        ent.pack(side="left", fill="x", expand=True)
        ent.bind("<Return>", lambda e: self.send_manual())
        ttk.Combobox(send, textvariable=self.var_send_fmt, values=ui.FMTS,
                     width=6, state="readonly").pack(side="left", padx=4)
        ttk.Combobox(send, textvariable=self.var_send_eol, values=list(LINE_ENDINGS),
                     width=6, state="readonly").pack(side="left")
        ttk.Button(send, text="보내기", style="Accent.TButton", command=self.send_manual).pack(side="left", padx=4)

        send2 = ttk.Frame(frm)
        send2.pack(fill="x", pady=(4, 0))
        ttk.Label(send2, text="검사값").pack(side="left")
        ttk.Combobox(send2, textvariable=self.var_send_check, values=ui.CHECK_LABELS, width=20,
                     state="readonly").pack(side="left", padx=4)
        ttk.Checkbutton(send2, text="반복 보내기", variable=self.var_send_repeat,
                        command=self.on_send_repeat).pack(side="left", padx=(8, 0))
        ttk.Spinbox(send2, from_=10, to=3600000, increment=100, textvariable=self.var_send_repeat_ms,
                   width=7).pack(side="left", padx=4)
        ttk.Label(send2, text="ms").pack(side="left")
        return frm

    def rebuild_macro_bar(self):
        """로그 아래 매크로 버튼 줄"""
        for w in self.macro_bar.winfo_children():
            w.destroy()
        macros = self.macro_panel.macros
        if not macros:
            ttk.Label(self.macro_bar, text="매크로 버튼: '매크로' 탭에서 추가하면 여기에 나타납니다.",
                      style="Muted.TLabel").pack(side="left")
            return
        per_row = 6
        for i, m in enumerate(macros):
            b = ttk.Button(self.macro_bar, text=m["name"], command=lambda i=i: self.macro_panel.send_index(i))
            b.grid(row=i // per_row, column=i % per_row, sticky="ew", padx=1, pady=1)
        for c in range(min(per_row, len(macros))):
            self.macro_bar.columnconfigure(c, weight=1, uniform="macro")

    # ------------------------------------------------------------------ 화면 전환
    def on_conn_changed(self):
        # 연결 방식에 맞는 Modbus 프레임을 기본으로 고른다 (필요하면 직접 바꿀 수 있음)
        self.var_frame.set(mb.FRAME_TCP if self.conn in core.TCP_CONNS else mb.FRAME_RTU)
        if self.conn == core.CONN_CAN:
            # CAN은 일반 규칙만, 데이터 형식은 CAN(ID#데이터)
            self.var_proto.set(label_of(PROTO_LABELS, PROTO_RAW))
            for var in (self.var_send_fmt, self.var_r_match_fmt, self.var_r_resp_fmt):
                var.set(core.FMT_CAN)
        else:
            for var in (self.var_send_fmt, self.var_r_match_fmt, self.var_r_resp_fmt):
                if var.get() == core.FMT_CAN:
                    var.set(core.FMT_TEXT)
        self.update_conn_widgets()

    @property
    def can_iface(self):
        return key_of(CAN_IFACE_LABELS, self.var_can_iface.get(), "virtual")

    def on_can_iface_changed(self):
        for k, _, channel, hint in core.CAN_IFACES:
            if k == self.can_iface:
                self.var_can_channel.set(channel)
                self.lbl_can_hint.configure(text=hint)
        self.update_can_hint()

    def update_can_hint(self):
        for k, _, _, hint in core.CAN_IFACES:
            if k == self.can_iface:
                self.lbl_can_hint.configure(text=hint)
        self.cb_can_bitrate.state(["disabled"] if self.can_iface in core.CAN_NO_BITRATE else ["!disabled"])

    def update_conn_widgets(self):
        for f in (self.frm_pty, self.frm_vcom, self.frm_ser, self.frm_tcp, self.frm_can):
            f.pack_forget()
        conn = self.conn
        if conn == core.CONN_PTY:
            self.frm_pty.pack(fill="x", pady=(6, 0))
        elif conn == core.CONN_VCOM:
            self.frm_vcom.pack(fill="x", pady=(6, 0))
        elif conn == core.CONN_SERIAL:
            self.frm_ser.pack(fill="x", pady=(6, 0))
        elif conn == core.CONN_CAN:
            self.frm_can.pack(fill="x", pady=(6, 0))
            self.update_can_hint()
        else:
            self.frm_tcp.pack(fill="x", pady=(6, 0))
            self.frm_udp_local.pack_forget()
            if conn == core.CONN_UDP:
                self.frm_udp_local.pack(side="left", before=self.lbl_host)
                self.lbl_host.configure(text="상대 IP")
                self.lbl_port.configure(text="상대 포트")
                self.lbl_tcp_hint.configure(text="상대를 비우면 마지막으로 보낸 쪽에 응답")
            else:
                self.lbl_host.configure(text="IP 주소")
                self.lbl_port.configure(text="포트 번호")
                if conn == core.CONN_TCP_SERVER:
                    self.lbl_tcp_hint.configure(text="이 주소/포트에서 접속을 기다림 (0.0.0.0 = 다른 PC에서도 접속 허용)")
                else:
                    self.lbl_tcp_hint.configure(text="이 주소/포트의 서버로 접속")

    def update_proto_widgets_for(self, proto):
        """창 폭 재기용: 잠시 다른 프로토콜 화면을 붙였다 뗀다 (None이면 원래대로)"""
        for f in self.proto_frames.values():
            f.pack_forget()
        self.proto_frames[proto or self.proto].pack(fill="both", expand=True)

    def update_proto_widgets(self):
        for f in self.proto_frames.values():
            f.pack_forget()
        self.proto_frames[self.proto].pack(fill="both", expand=True)
        self.left_nb.tab(self.left, text={PROTO_RAW: "응답 규칙", PROTO_SLAVE: "Modbus 슬레이브",
                                          PROTO_MASTER: "Modbus 마스터"}[self.proto])
        if self.proto == PROTO_RAW:
            self.frm_frame.pack_forget()
        else:
            self.frm_frame.pack(side="left")

    def update_master_widgets(self):
        fc = int(self.var_m_fc.get()[:2])
        reads = fc in (1, 2, 3, 4)
        self.ent_m_count.state(["!disabled"] if reads else ["disabled"])
        self.ent_m_values.state(["disabled"] if reads else ["!disabled"])

    def update_title(self):
        mark = ("● " if self.channel.is_open else "") + ("⚠ " if self.channel.faults.enabled else "")
        try:
            self.notebook.tab(self, text=mark + (self.var_name.get() or "연결"))
        except tk.TclError:
            pass

    def refresh_relay_choices(self):
        self.cb_relay["values"] = [NO_RELAY] + [t.var_name.get() for t in self.app.tabs() if t is not self]

    def refresh_ports(self):
        self.cb_port["values"] = core.list_serial_ports()

    def copy_device(self):
        dev = self.var_device.get()
        if dev:
            self.clipboard_clear()
            self.clipboard_append(dev)

    def apply_handler_settings(self):
        self.raw_handler.enabled = self.can_handler.enabled = self.var_auto.get()
        try:
            self.slave_handler.unit_id = int(self.var_s_unit.get() or 0)
        except ValueError:
            pass
        try:
            self.slave_handler.delay_ms = max(0, int(self.var_s_delay.get() or 0))
        except ValueError:
            pass
        self.slave_handler.no_response = self.var_s_noresp.get()

    # ------------------------------------------------------------------ 연결 열기/닫기
    def on_toggle_enabled(self):
        if self.var_enabled.get():
            self.open_port()
        else:
            self.close_port()

    def open_port(self):
        if self.conn == core.CONN_VCOM:
            self._open_vcom()
        else:
            self._open(self.conn, self.var_port.get().strip())

    def _open_vcom(self):
        """Windows: 가상 COM 포트가 없으면 (필요하면 드라이버 설치까지) 자동으로 만들고 연다."""
        name = self.var_vcom.get().strip().upper()
        self.var_vcom.set(name)
        self.var_enabled.set(False)
        if not vport.IS_WINDOWS:
            self.log("error", "가상 COM 포트 자동 생성은 Windows 전용입니다.")
            return
        if not vport.is_installed() and not messagebox.askyesno(
                APP_TITLE,
                "가상 COM 포트를 만들려면 com0com 드라이버가 필요합니다.\n"
                "지금 자동으로 설치할까요? (처음 한 번만)\n\n"
                "• 관리자 권한 확인창이 뜨면 '예'를 누르세요.\n"
                "• Windows 보안 창이 뜨면 '항상 신뢰'를 체크하고 '설치'를 누르세요.", parent=self):
            return
        self.chk_enabled.state(["disabled"])
        self.var_status.set("가상 포트 준비 중… (%s)" % name)
        self.log("info", "가상 포트 %s 준비 중… 관리자 권한 확인창이 뜨면 '예'를 누르세요." % name)

        def done(result, error):
            self.chk_enabled.state(["!disabled"])
            if error is not None:
                self.var_status.set("닫힘")
                self.log("error", "가상 포트 준비 실패: %s" % error)
                messagebox.showerror(APP_TITLE, str(error), parent=self)
                return
            path, pair, created = result
            if created:
                self.log("info", "가상 포트 쌍을 만들었습니다: %s ⇄ %s" % (pair["a"], pair["b"]))
            self._open(core.CONN_SERIAL, path, label="%s (내부: %s)" % (name, path))

        run_async(self, lambda: vport.ensure_port(name), done)

    def _open(self, conn, port, label=None):
        proto = self.proto
        handler = {PROTO_RAW: self.can_handler if conn == core.CONN_CAN else self.raw_handler,
                   PROTO_SLAVE: self.slave_handler, PROTO_MASTER: self.master_handler}[proto]
        if proto != PROTO_RAW:
            handler.frame_type = self.var_frame.get()
        self.slave_handler.faults = self.channel.faults
        self.apply_handler_settings()
        self.channel.handler = handler
        self.logview.can_mode = conn == core.CONN_CAN
        try:
            if conn == core.CONN_SERIAL and not port:
                raise ValueError("포트를 선택하세요.")
            if conn == core.CONN_CAN and proto != PROTO_RAW:
                raise ValueError("CAN은 '일반 (자동 응답 규칙)' 프로토콜로만 쓸 수 있습니다.")
            device = self.channel.open(
                conn, link_path=self.var_link.get().strip(), port=port,
                baudrate=self.var_baud.get(), bytesize=self.var_bytesize.get(),
                parity=self.var_parity.get(), stopbits=self.var_stopbits.get(),
                host=self.var_host.get().strip(),
                tcp_port=parse_int(self.var_tcp_port.get()) if self.var_tcp_port.get().strip() else None,
                udp_bind=self.var_udp_bind.get().strip(),
                udp_local_port=parse_int(self.var_udp_port.get() or 0),
                can_iface=self.can_iface, can_channel=self.var_can_channel.get().strip(),
                can_bitrate=parse_int(self.var_can_bitrate.get() or 500000))
        except Exception as e:
            self.var_enabled.set(False)
            self.log("error", "열기 실패: %s" % e)
            return
        self.var_enabled.set(True)
        link = self.var_link.get().strip() if conn == core.CONN_PTY else ""
        self.var_device.set(label or link or device)
        self.var_status.set("열림")
        self.lbl_status.configure(style="Open.TLabel")
        for w in self.setting_widgets:
            w.state(["disabled"])
        msg = "열림: %s" % device
        if link:
            msg += "  (링크: %s)" % link
        if proto != PROTO_RAW:
            msg += "  [%s, %s]" % (self.var_proto.get(), self.var_frame.get())
        self.log("info", msg)
        self.update_title()
        self.app.update_relays()
        self.poller.reset()
        if proto == PROTO_MASTER:
            self.ensure_master_loop()
            if self.var_m_poll.get():
                self.schedule_poll()
        if self.var_send_repeat.get():
            self.on_send_repeat()

    def close_port(self):
        was_open = self.channel.is_open
        self.channel.close()
        self.cancel_poll()
        self.stop_master_loop()
        self.poller.reset()
        self.cancel_send_repeat()
        self.macro_panel.stop_all()
        self.scenario_panel.stop(ui.sc.END_CLOSED)
        self.var_enabled.set(False)
        self.var_device.set("")
        self.var_status.set("닫힘")
        self.lbl_status.configure(style="Closed.TLabel")
        for w in self.setting_widgets:
            w.state(["!disabled"])
        if was_open:
            self.log("info", "닫힘")
        self.update_title()

    def manual_bytes(self):
        data = core.to_bytes(self.var_send.get(), self.var_send_fmt.get())
        if self.var_send_fmt.get() == core.FMT_CAN:
            return data  # CAN은 검사값/줄끝을 붙이지 않는다 (CRC는 CAN 장치가 계산)
        data = checks.append(data, checks.key_of(self.var_send_check.get())) if data else data
        return data + LINE_ENDINGS[self.var_send_eol.get()]

    def send_manual(self):
        try:
            data = self.manual_bytes()
            if not data:
                return False
            self.channel.send(data)
            return True
        except Exception as e:
            self.log("error", "보내기 실패: %s" % e)
            return False

    def on_send_repeat(self):
        self.cancel_send_repeat()
        if self.var_send_repeat.get() and self.channel.is_open:
            self._send_tick()

    def _send_tick(self):
        self._send_job = None
        if not (self.var_send_repeat.get() and self.channel.is_open):
            return
        if not self.send_manual():
            self.var_send_repeat.set(False)
            return
        try:
            interval = max(10, parse_int(self.var_send_repeat_ms.get() or 1000))
        except ValueError:
            interval = 1000
        self._send_job = self.after(interval, self._send_tick)

    def cancel_send_repeat(self):
        if self._send_job is not None:
            self.after_cancel(self._send_job)
            self._send_job = None

    # ------------------------------------------------------------------ 일반 규칙
    def _rule_from_editor(self):
        rule = core.Rule(enabled=self.var_r_enabled.get(), match=self.var_r_match.get(),
                         match_fmt=self.var_r_match_fmt.get(), response=self.var_r_resp.get(),
                         response_fmt=self.var_r_resp_fmt.get(),
                         response_check=checks.key_of(self.var_r_check.get()))
        try:
            rule.delay_ms = max(0, int(self.var_r_delay.get() or 0))
            if not rule.match_bytes():
                raise ValueError("수신 데이터를 입력하세요.")
            rule.response_bytes()
        except ValueError as e:
            messagebox.showerror(APP_TITLE, str(e), parent=self)
            return None
        return rule

    def refresh_tree(self, select=None):
        self.tree.delete(*self.tree.get_children())
        for i, r in enumerate(self.rules):
            self.tree.insert("", "end", iid=str(i), values=(
                CHECK_ON if r.enabled else CHECK_OFF, r.match, r.match_fmt,
                r.response, r.response_fmt,
                "" if r.response_check == "none" else checks.label_of(r.response_check).split(" (")[0],
                r.delay_ms))
        if select is not None and 0 <= select < len(self.rules):
            self.tree.selection_set(str(select))
            self.tree.see(str(select))

    def add_rule(self):
        rule = self._rule_from_editor()
        if rule:
            self.rules.append(rule)
            self.refresh_tree(len(self.rules) - 1)

    def update_rule(self):
        if self.editing_index is None:
            return self.add_rule()
        rule = self._rule_from_editor()
        if rule:
            self.rules[self.editing_index] = rule
            self.refresh_tree(self.editing_index)

    def delete_rule(self):
        sel = self.tree.selection()
        if not sel:
            return
        del self.rules[int(sel[0])]
        self.editing_index = None
        self.refresh_tree()

    def move_rule(self, delta):
        sel = self.tree.selection()
        if not sel:
            return
        i = int(sel[0])
        j = i + delta
        if 0 <= j < len(self.rules):
            self.rules[i], self.rules[j] = self.rules[j], self.rules[i]
            self.refresh_tree(j)

    def clear_editor(self):
        self.editing_index = None
        self.tree.selection_remove(*self.tree.selection())
        self.var_r_enabled.set(True)
        self.var_r_match.set("")
        self.var_r_resp.set("")
        self.var_r_delay.set("0")

    def on_tree_click(self, event):
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        if self.tree.identify_column(event.x) != "#1":
            return
        row = self.tree.identify_row(event.y)
        if row:
            r = self.rules[int(row)]
            r.enabled = not r.enabled
            self.refresh_tree(int(row))
            return "break"

    def on_tree_select(self, event=None):
        sel = self.tree.selection()
        if not sel:
            return
        i = int(sel[0])
        r = self.rules[i]
        self.editing_index = i
        self.var_r_enabled.set(r.enabled)
        self.var_r_match.set(r.match)
        self.var_r_match_fmt.set(r.match_fmt)
        self.var_r_resp.set(r.response)
        self.var_r_resp_fmt.set(r.response_fmt)
        self.var_r_delay.set(str(r.delay_ms))
        self.var_r_check.set(checks.label_of(r.response_check))

    # ------------------------------------------------------------------ Modbus 슬레이브
    def _slave_range(self):
        try:
            start = max(0, min(0xFFFF, parse_int(self.var_s_start.get())))
            count = max(1, min(2000, parse_int(self.var_s_count.get())))
        except ValueError:
            return None
        return start, min(count, 0x10000 - start)

    def refresh_slave_tree(self):
        self._slave_refresh_pending = False
        rng = self._slave_range()
        if rng is None:
            return
        start, count = rng
        area = self.slave_area
        base = mb.AREA_BASE[area]
        is_bit = area in mb.BIT_AREAS
        fmt = self.var_s_fmt.get()
        sel = self.s_tree.selection()
        yview = self.s_tree.yview()[0]
        self.s_tree.delete(*self.s_tree.get_children())
        for addr in range(start, start + count):
            self.s_tree.insert("", "end", iid=str(addr), values=(
                addr, base + addr, format_value(self.store.get(area, addr), fmt, is_bit)))
        if sel and self.s_tree.exists(sel[0]):
            self.s_tree.selection_set(sel[0])
        self.s_tree.yview_moveto(yview)

    def schedule_slave_refresh(self):
        if not self._slave_refresh_pending:
            self._slave_refresh_pending = True
            self.after(150, self.refresh_slave_tree)

    def edit_slave_value(self, event=None):
        sel = self.s_tree.selection()
        if not sel:
            return
        addr = int(sel[0])
        area = self.slave_area
        is_bit = area in mb.BIT_AREAS
        cur = self.store.get(area, addr)
        prompt = "%s  주소 %d (PLC %d)\n%s" % (
            self.var_s_area.get(), addr, mb.AREA_BASE[area] + addr,
            "0 또는 1을 입력하세요." if is_bit else "0~65535, 음수(-32768~-1), 0x1234 형식 가능")
        s = simpledialog.askstring(APP_TITLE, prompt, initialvalue=str(cur), parent=self)
        if s is None:
            return
        try:
            val = parse_int(s)
            if is_bit and val not in (0, 1):
                raise ValueError
            if not is_bit and not -0x8000 <= val <= 0xFFFF:
                raise ValueError
        except ValueError:
            messagebox.showerror(APP_TITLE, "잘못된 값입니다: %s" % s, parent=self)
            return
        self.store.set(area, addr, val)
        self.refresh_slave_tree()

    def clear_slave_area(self):
        if messagebox.askyesno(APP_TITLE, "%s 값을 모두 0으로 만들까요?" % self.var_s_area.get(), parent=self):
            self.store.clear(self.slave_area)
            self.refresh_slave_tree()

    # ------------------------------------------------------------------ Modbus 마스터
    def send_master_request(self):
        """단일 요청: 폴링과 같은 대기열에 넣어 한 번에 하나씩 보낸다 (RTU에서 서로 엉키지 않게)."""
        if not self.channel.is_open:
            self.log("error", "연결이 열려 있지 않습니다.")
            return
        try:
            unit = parse_int(self.var_m_unit.get())
            fc = int(self.var_m_fc.get()[:2])
            addr = parse_int(self.var_m_addr.get())
            count = parse_int(self.var_m_count.get()) if fc in (1, 2, 3, 4) else 0
            values = [parse_int(x) for x in self.var_m_values.get().replace(" ", "").split(",") if x] \
                if fc not in (1, 2, 3, 4) else []
            if not 0 <= addr <= 0xFFFF:
                raise ValueError("시작 주소는 0~65535 입니다.")
            mb.build_request_pdu(fc, addr, count, values)
        except Exception as e:
            self.set_master_status("요청 오류: %s" % e, ok=False)
            return
        if any(j.item is None for j in self.poller.queue):
            return  # 앞 요청이 아직 대기 중 (주기 반복이 너무 빠른 경우)
        self.m_area = mb.FC_AREA[fc]
        self.poller.submit(polling.Job(unit, fc, addr, count, values))
        self.ensure_master_loop()

    def ensure_master_loop(self):
        if self._mloop is None and self.channel.is_open and self.proto == PROTO_MASTER:
            self._mloop = self.after(20, self._master_tick)

    def stop_master_loop(self):
        if self._mloop is not None:
            self.after_cancel(self._mloop)
            self._mloop = None

    def _master_tick(self):
        self._mloop = None
        if not (self.channel.is_open and self.proto == PROTO_MASTER):
            return
        try:
            timeout = max(50, int(self.var_m_timeout.get() or 1000))
        except ValueError:
            timeout = 1000
        for kind, job, msg in self.poller.tick(time.time(), self.channel.send, timeout):
            if job.item is None:
                if kind == "timeout":
                    text = "응답 없음 (타임아웃) - %s" % mb.FC_NAMES.get(job.fc, "")
                else:
                    text = "보내기 실패: %s" % msg
                self.set_master_status(text, ok=False)
                self.log("error", text)
            else:
                self._log_poll_state(job.item)
        now = time.time()
        if now - self._last_poll_ui > 0.25:
            self._last_poll_ui = now
            self.poll_panel.refresh_values()
        self._mloop = self.after(20, self._master_tick)

    def _log_poll_state(self, item):
        """폴링 항목이 실패/회복할 때만 로그에 남긴다 (매번 남기면 너무 많음)"""
        state = "ok" if item.ok else item.status
        if getattr(item, "_logstate", "ok") == state:
            return
        item._logstate = state
        if item.ok:
            self.log("info", "폴링 '%s' 다시 정상" % item.name)
        else:
            self.log("error", "폴링 '%s' %s" % (item.name, item.status))

    def on_master_result(self, result):
        req = result["req"]
        text = "%s → %s (%d ms)" % (mb.FC_NAMES.get(req["fc"], ""), result["text"], result["elapsed_ms"])
        self.set_master_status(text, ok=result["ok"])
        self.log("decode", "[장치 %d] %s" % (req["unit"], text))
        if result["rows"]:
            self.m_area = mb.FC_AREA[req["fc"]]
            self.m_rows = result["rows"]
            self.show_master_rows()

    def show_master_rows(self):
        base = mb.AREA_BASE[self.m_area]
        is_bit = self.m_area in mb.BIT_AREAS
        fmt = self.var_m_fmt.get()
        self.m_tree.delete(*self.m_tree.get_children())
        for addr, val in self.m_rows:
            self.m_tree.insert("", "end", values=(addr, base + addr, format_value(val, fmt, is_bit)))

    def set_master_status(self, text, ok=True):
        self.var_m_status.set(text)
        self.lbl_m_status.configure(foreground="#2E7D32" if ok else "#C62828")

    def on_poll_toggle(self):
        if self.var_m_poll.get() and self.channel.is_open:
            self.schedule_poll()
        else:
            self.cancel_poll()

    def schedule_poll(self):
        self.cancel_poll()
        try:
            interval = max(50, int(self.var_m_poll_ms.get() or 1000))
        except ValueError:
            interval = 1000
        self._poll_job = self.after(interval, self._poll_tick)

    def _poll_tick(self):
        self._poll_job = None
        if self.var_m_poll.get() and self.channel.is_open and self.proto == PROTO_MASTER:
            self.send_master_request()
            self.schedule_poll()

    def cancel_poll(self):
        if self._poll_job is not None:
            self.after_cancel(self._poll_job)
            self._poll_job = None

    # ------------------------------------------------------------------ 로그
    def log(self, kind, data, src=None):
        self.logview.add(kind, data, src)

    def handle_event(self, kind, data, src):
        if kind == "closed":
            self.close_port()
        elif kind == "mb_changed":
            self.schedule_slave_refresh()
        elif kind == "mb_result":
            job = self.poller.on_result(data, time.time())
            if job is None:
                self.log("decode", "기다리지 않던 응답 (타임아웃 뒤 도착?): %s" % data["text"])
            elif job.item is None:
                self.on_master_result(data)
            else:
                self._log_poll_state(job.item)
        else:
            if kind == "rx":
                self.scenario_panel.feed(data)
            self.scenario_panel.capture(kind, data, src)
            self.log(kind, data, src)

    # ------------------------------------------------------------------ 설정 저장/불러오기
    def to_config(self):
        return dict(
            name=self.var_name.get(), enabled=self.var_enabled.get(), conn=self.conn, protocol=self.proto,
            frame=self.var_frame.get(), link_path=self.var_link.get(), vcom_name=self.var_vcom.get(),
            port=self.var_port.get(),
            baudrate=self.var_baud.get(), bytesize=self.var_bytesize.get(), parity=self.var_parity.get(),
            stopbits=self.var_stopbits.get(), host=self.var_host.get(), tcp_port=self.var_tcp_port.get(),
            udp_bind=self.var_udp_bind.get(), udp_port=self.var_udp_port.get(), relay=self.var_relay.get(),
            can_iface=self.can_iface, can_channel=self.var_can_channel.get(), can_bitrate=self.var_can_bitrate.get(),
            auto_enabled=self.var_auto.get(), rules=[r.to_dict() for r in self.rules],
            send=dict(data=self.var_send.get(), fmt=self.var_send_fmt.get(), eol=self.var_send_eol.get(),
                      check=checks.key_of(self.var_send_check.get()), repeat=self.var_send_repeat.get(),
                      repeat_ms=self.var_send_repeat_ms.get()),
            log=self.logview.to_dict(), macros=self.macro_panel.to_list(), scenario=self.scenario_panel.to_dict(),
            faults=self.fault_panel.to_dict(), sim=self.sim_panel.to_list(),
            slave=dict(unit_id=self.var_s_unit.get(), delay_ms=self.var_s_delay.get(),
                       no_response=self.var_s_noresp.get(), area=self.slave_area,
                       start=self.var_s_start.get(), count=self.var_s_count.get(), fmt=self.var_s_fmt.get(),
                       data=self.store.to_dict()),
            master=dict(unit_id=self.var_m_unit.get(), fc=self.var_m_fc.get(), addr=self.var_m_addr.get(),
                        count=self.var_m_count.get(), values=self.var_m_values.get(), poll=self.var_m_poll.get(),
                        poll_ms=self.var_m_poll_ms.get(), timeout_ms=self.var_m_timeout.get(),
                        fmt=self.var_m_fmt.get(), poll_list=self.poll_panel.to_dict()))

    def load_config(self, cfg):
        def put(d, key, var):
            if key in d:
                var.set(d[key] if isinstance(var, tk.BooleanVar) else str(d[key]))

        conn = cfg.get("conn")
        if conn is None:  # 이전 버전 설정 파일
            conn = core.CONN_PTY if cfg.get("mode") == "PTY" else core.CONN_SERIAL
        if conn in (core.CONN_PTY, core.CONN_VCOM):
            conn = VIRTUAL_CONN[0]  # 다른 OS에서 저장한 설정도 그 OS의 가상 시리얼로 연다
        self.var_conn.set(label_of(CONN_LABELS, conn))
        self.var_proto.set(label_of(PROTO_LABELS, cfg.get("protocol", PROTO_RAW)))
        for key, var in (("name", self.var_name), ("frame", self.var_frame), ("link_path", self.var_link),
                         ("vcom_name", self.var_vcom),
                         ("port", self.var_port), ("baudrate", self.var_baud), ("bytesize", self.var_bytesize),
                         ("parity", self.var_parity), ("stopbits", self.var_stopbits), ("host", self.var_host),
                         ("tcp_port", self.var_tcp_port), ("auto_enabled", self.var_auto),
                         ("udp_bind", self.var_udp_bind), ("udp_port", self.var_udp_port), ("relay", self.var_relay),
                         ("can_channel", self.var_can_channel), ("can_bitrate", self.var_can_bitrate)):
            put(cfg, key, var)
        if "can_iface" in cfg:
            self.var_can_iface.set(label_of(CAN_IFACE_LABELS, cfg["can_iface"]))
            self.var_can_channel.set(cfg.get("can_channel", self.var_can_channel.get()))
        self.rules[:] = [core.Rule.from_dict(d) for d in cfg.get("rules", [])]
        self.refresh_tree()

        snd = cfg.get("send", {})
        for key, var in (("data", self.var_send), ("fmt", self.var_send_fmt), ("eol", self.var_send_eol),
                         ("repeat", self.var_send_repeat), ("repeat_ms", self.var_send_repeat_ms)):
            put(snd, key, var)
        if "check" in snd:
            self.var_send_check.set(checks.label_of(snd["check"]))
        self.logview.load_dict(cfg.get("log", {}))
        self.macro_panel.load_list(cfg.get("macros", []))
        self.scenario_panel.load_dict(cfg.get("scenario", {}))
        self.fault_panel.load_dict(cfg.get("faults", {}))
        self.sim_panel.load_list(cfg.get("sim", []))

        s = cfg.get("slave", {})
        for key, var in (("unit_id", self.var_s_unit), ("delay_ms", self.var_s_delay),
                         ("no_response", self.var_s_noresp), ("start", self.var_s_start),
                         ("count", self.var_s_count), ("fmt", self.var_s_fmt)):
            put(s, key, var)
        if "area" in s:
            self.var_s_area.set(label_of(AREA_LABELS, s["area"]))
        self.store.load_dict(s.get("data", {}))

        m = cfg.get("master", {})
        for key, var in (("unit_id", self.var_m_unit), ("fc", self.var_m_fc), ("addr", self.var_m_addr),
                         ("count", self.var_m_count), ("values", self.var_m_values), ("poll", self.var_m_poll),
                         ("poll_ms", self.var_m_poll_ms), ("timeout_ms", self.var_m_timeout),
                         ("fmt", self.var_m_fmt)):
            put(m, key, var)
        self.poll_panel.load_dict(m.get("poll_list", {}))


class VPortManager(tk.Toplevel):
    """Windows 가상 COM 포트(com0com) 관리 창"""

    def __init__(self, master):
        tk.Toplevel.__init__(self, master)
        self.title("가상 포트 관리 (com0com)")
        self.geometry("620x460")
        self.minsize(560, 400)
        self.busy = False
        self.var_driver = tk.StringVar()
        self.var_a = tk.StringVar(value="COM20")
        self.var_b = tk.StringVar(value="")
        self.var_msg = tk.StringVar()

        frm = ttk.Frame(self, padding=10)
        frm.pack(fill="both", expand=True)

        top = ttk.Frame(frm)
        top.pack(fill="x")
        ttk.Label(top, text="드라이버:").pack(side="left")
        ttk.Label(top, textvariable=self.var_driver).pack(side="left", padx=4)
        self.btn_uninstall = ttk.Button(top, text="드라이버 제거", command=self.uninstall)
        self.btn_uninstall.pack(side="right")
        self.btn_install = ttk.Button(top, text="드라이버 설치", command=self.install)
        self.btn_install.pack(side="right", padx=4)

        ttk.Label(frm, style="Muted.TLabel", justify="left", text=(
            "가상 포트는 두 개가 한 쌍입니다. 한쪽으로 보낸 데이터는 다른 쪽으로 나옵니다.\n"
            "• 이 프로그램과 다른 프로그램을 연결: 탭에서 '가상 시리얼'을 고르고 이름만 넣으면 자동으로 만들어집니다.\n"
            "• 다른 프로그램 두 개를 서로 연결: 아래에서 두 이름을 모두 COM 이름으로 정해 쌍을 추가하세요.")
        ).pack(fill="x", pady=(8, 4))

        tree_frm = ttk.Frame(frm)
        tree_frm.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tree_frm, columns=("n", "a", "b"), show="headings", height=8, selectmode="browse")
        for c, title, w in (("n", "번호", 60), ("a", "A쪽 이름", 200), ("b", "B쪽 이름", 200)):
            self.tree.heading(c, text=title)
            self.tree.column(c, width=w, anchor="center" if c == "n" else "w")
        sb = ttk.Scrollbar(tree_frm, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        add = ttk.LabelFrame(frm, text="쌍 추가", padding=6)
        add.pack(fill="x", pady=(8, 0))
        ttk.Label(add, text="A쪽").pack(side="left")
        ttk.Entry(add, textvariable=self.var_a, width=10).pack(side="left", padx=4)
        ttk.Label(add, text="⇄  B쪽").pack(side="left")
        ttk.Entry(add, textvariable=self.var_b, width=10).pack(side="left", padx=4)
        ttk.Label(add, text="(비우면 기본 이름)", style="Muted.TLabel").pack(side="left")
        self.btn_add = ttk.Button(add, text="추가", command=self.add_pair)
        self.btn_add.pack(side="right")

        bar = ttk.Frame(frm)
        bar.pack(fill="x", pady=(8, 0))
        self.btn_remove = ttk.Button(bar, text="선택한 쌍 삭제", command=self.remove_pair)
        self.btn_remove.pack(side="left")
        ttk.Button(bar, text="새로고침", command=self.refresh).pack(side="left", padx=4)
        self.btn_diag = ttk.Button(bar, text="상태 진단", command=self.diagnose)
        self.btn_diag.pack(side="left")
        ttk.Label(frm, textvariable=self.var_msg, foreground="#1565C0", wraplength=580,
                  justify="left").pack(fill="x", pady=(8, 0))

        self.action_buttons = [self.btn_install, self.btn_uninstall, self.btn_add, self.btn_remove, self.btn_diag]
        self.refresh()

    def refresh(self):
        installed = vport.is_installed()
        self.var_driver.set("설치됨 (%s)" % vport.install_dir() if installed else "설치 안 됨 (쌍을 추가하면 자동으로 설치)")
        self.btn_install.state(["disabled"] if installed or self.busy else ["!disabled"])
        self.btn_uninstall.state(["!disabled"] if installed and not self.busy else ["disabled"])
        self.tree.delete(*self.tree.get_children())
        try:
            pairs = vport.list_pairs() if installed else []
        except Exception as e:
            self.var_msg.set("목록을 읽지 못했습니다: %s" % e)
            return
        for p in pairs:
            self.tree.insert("", "end", iid=str(p["n"]), values=(p["n"], p["a"], p["b"]))

    def _run(self, message, func, ok_message):
        """관리자 작업을 별도 스레드에서 실행 (그동안 버튼 잠금)"""
        if self.busy:
            return
        self.busy = True
        for b in self.action_buttons:
            b.state(["disabled"])
        self.var_msg.set(message + "  관리자 권한 확인창이 뜨면 '예'를 누르세요.")

        def done(result, error):
            self.busy = False
            for b in self.action_buttons:
                b.state(["!disabled"])
            if error is not None:
                self.var_msg.set("실패했습니다.")
                messagebox.showerror("가상 포트 관리", str(error), parent=self)
            else:
                self.var_msg.set(ok_message(result) if callable(ok_message) else ok_message)
            self.refresh()

        run_async(self, func, done)

    def install(self):
        self._run("드라이버 설치 중…", vport.install_driver, "드라이버를 설치했습니다.")

    def uninstall(self):
        if messagebox.askyesno("가상 포트 관리", "com0com 드라이버와 모든 가상 포트를 제거할까요?", parent=self):
            self._run("드라이버 제거 중…", vport.uninstall_driver, "드라이버를 제거했습니다.")

    def add_pair(self):
        a, b = self.var_a.get().strip(), self.var_b.get().strip()
        self._run("포트 쌍을 만드는 중…", lambda: vport.create_pair(a, b),
                  lambda p: "만들었습니다: %s ⇄ %s" % (p["a"], p["b"]))

    def remove_pair(self):
        sel = self.tree.selection()
        if not sel:
            return
        n = int(sel[0])
        self._run("포트 쌍 삭제 중…", lambda: vport.remove_pair(n), "삭제했습니다.")

    def diagnose(self):
        def done(result, error):
            self.btn_diag.state(["!disabled"])
            if error is not None:
                self.var_msg.set("진단 실패: %s" % error)
                return
            ok, lines = result
            text = "\n".join(lines)
            if not ok:
                text += "\n\n" + vport.BLOCKED_HELP
            self.var_msg.set("")
            (messagebox.showinfo if ok else messagebox.showwarning)("상태 진단", text, parent=self)

        self.btn_diag.state(["disabled"])
        self.var_msg.set("진단 중…")
        run_async(self, vport.diagnose, done)


class ShowTextDialog(tk.Toplevel):
    """긴 안내/경고 목록을 보여주는 창"""

    def __init__(self, master, title, message, text, copy_text=None):
        tk.Toplevel.__init__(self, master)
        self.title(title)
        self.geometry("720x360")
        ttk.Label(self, text=message, padding=8).pack(anchor="w")
        frm = ttk.Frame(self, padding=(8, 0))
        frm.pack(fill="both", expand=True)
        txt = tk.Text(frm, wrap="word", height=12)
        sb = ttk.Scrollbar(frm, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        txt.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        txt.insert("1.0", text)
        txt.configure(state="disabled")
        btns = ttk.Frame(self)
        btns.pack(pady=8)
        if copy_text:
            def copy():
                self.clipboard_clear()
                self.clipboard_append(copy_text)
                b_copy.configure(text="복사됨 ✓")
            b_copy = ttk.Button(btns, text="명령 복사", style="Accent.TButton", command=copy)
            b_copy.pack(side="left", padx=4)
        ttk.Button(btns, text="닫기", command=self.destroy).pack(side="left", padx=4)


class App(object):
    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()
        self.tab_counter = 0
        self.log_dir = os.path.join(os.path.dirname(CONFIG_PATH), "logs")

        root.title(APP_TITLE)
        root.geometry("1360x880")
        root.minsize(1100, 680)

        # 제목 줄
        header = ttk.Frame(root, style="Header.TFrame", padding=(14, 10))
        header.pack(fill="x")
        ttk.Label(header, text="통신 테스트 도구", style="Header.TLabel").pack(side="left")
        ttk.Label(header, text="  시리얼 · 가상 COM · TCP/UDP · Modbus · CAN", style="HeaderSub.TLabel").pack(side="left")
        self.var_saved = tk.StringVar(value="설정 자동 저장")
        ttk.Label(header, textvariable=self.var_saved, style="HeaderSub.TLabel").pack(side="right")

        # 도구 줄
        bar = ttk.Frame(root, style="Toolbar.TFrame", padding=(8, 4))
        bar.pack(fill="x")

        def tool(text, cmd, accent=False):
            ttk.Button(bar, text=text, command=cmd, style="Accent.TButton" if accent else "Tool.TButton").pack(
                side="left", padx=1)

        def sep():
            ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8, pady=3)

        tool("＋ 새 연결", self.add_tab, accent=True)
        tool("✕ 탭 닫기", self.close_current_tab)
        sep()
        tool("설정 저장", lambda: self.save_config(CONFIG_PATH, notify=True))
        tool("다른 이름으로 저장", self.save_config_as)
        tool("설정 열기", self.load_config_from)
        sep()
        ttk.Label(bar, text="엑셀", style="Toolbar.TLabel").pack(side="left", padx=(0, 4))
        tool("저장", self.export_excel)
        tool("불러오기", self.import_excel)
        tool("양식 만들기", self.make_excel_template)
        sep()
        tool("자동 실행 명령", self.show_autotest_help)
        if core.IS_WINDOWS:
            sep()
            tool("가상 포트 관리", self.open_vport_manager)
        ttk.Frame(root, height=1, style="Status.TFrame").pack(fill="x")

        # 상태 줄 (아래)
        status = ttk.Frame(root, style="Status.TFrame", padding=(10, 3))
        status.pack(side="bottom", fill="x")
        self.var_status = tk.StringVar()
        ttk.Label(status, textvariable=self.var_status, style="Status.TLabel").pack(side="left")
        info = "" if core.serial is not None else "pyserial 미설치 (시리얼 포트 사용 불가)"
        ttk.Label(status, text=info, style="Status.TLabel").pack(side="right")

        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=(8, 6))

        self._last_saved = None
        self.vport_manager = None
        if not self.load_config(CONFIG_PATH):
            self.add_tab()

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(30, self.poll_events)
        root.after(AUTOSAVE_MS, self.autosave)
        root.after(100, self.sim_tick)
        root.after(500, self.update_status)
        self.fit_window()

    def fit_window(self):
        """글꼴이 OS마다 달라서, 화면이 잘리지 않는 폭을 직접 재서 최소 크기로 쓴다."""
        root = self.root
        tab = self.add_tab() if not self.tabs() else None
        probe = self.tabs()[0]
        need = 0
        saved = probe.var_proto.get()
        for proto in (PROTO_RAW, PROTO_SLAVE, PROTO_MASTER):  # 가장 넓은 화면 기준
            probe.update_proto_widgets_for(proto)
            root.update_idletasks()
            need = max(need, root.winfo_reqwidth())
        probe.update_proto_widgets_for(None)
        probe.var_proto.set(saved)
        if tab is not None:
            self.notebook.forget(tab)
            tab.destroy()
        screen = root.winfo_screenwidth() - 40
        need = min(need + 30, screen)  # 두 칸이 반씩 나누므로 여유를 둔다
        root.minsize(min(max(1100, need), screen), 680)
        width = min(max(1360, need), screen)
        root.geometry("%dx%d" % (width, min(880, root.winfo_screenheight() - 80)))

    def update_status(self):
        tabs = self.tabs()
        opened = [t for t in tabs if t.channel.is_open]
        self.var_status.set("연결 %d개 중 %d개 열림   ·   로그 폴더: %s" % (len(tabs), len(opened), self.log_dir))
        self.root.after(1000, self.update_status)

    def sim_tick(self):
        """Modbus 값 자동 변화"""
        now = time.time()
        for t in self.tabs():
            if t.proto == PROTO_SLAVE and t.sim_panel.entries and t.sim_panel.tick(now):
                t.schedule_slave_refresh()
        self.root.after(100, self.sim_tick)

    def update_relays(self):
        """탭의 '중계 대상' 설정대로 연결끼리 양방향으로 잇는다."""
        tabs = self.tabs()
        by_name = {t.var_name.get(): t for t in tabs}
        for t in tabs:
            t.channel.forward = None
        for t in tabs:
            peer = by_name.get(t.var_relay.get())
            if peer is not None and peer is not t:
                t.channel.forward = peer.channel
                peer.channel.forward = t.channel

    def show_autotest_help(self):
        """화면 없이 시나리오를 돌리는 명령을 보여 준다 (지금 설정을 먼저 저장)."""
        self.save_config(CONFIG_PATH)
        folder = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
        if getattr(sys, "frozen", False):
            exe = os.path.join(folder, "SerialTesterCLI.exe" if core.IS_WINDOWS else "SerialTesterCLI")
            cmd = '"%s" "%s"' % (exe, CONFIG_PATH)
        else:
            cmd = '"%s" "%s" "%s"' % (sys.executable, os.path.join(folder, "autotest.py"), CONFIG_PATH)
        report_dir = os.path.join(os.path.dirname(CONFIG_PATH), "reports")
        text = (
            "지금 설정(자동 저장 파일)으로 시나리오를 화면 없이 실행하는 명령입니다.\n"
            "명령 창(cmd, 터미널)이나 배치 파일에 붙여 넣어 쓰세요.\n\n"
            "■ 기본 실행 ('사용' 체크된 탭 중 시나리오가 있는 탭 모두)\n%s\n\n"
            "■ 자주 쓰는 옵션\n"
            "  --tab 탭이름        이 탭의 시나리오만 실행 (여러 번 쓸 수 있음)\n"
            "  --repeat 100        100회 반복\n"
            "  --repeat 0 --max-time 3600   1시간 동안 계속 반복 (장시간 시험)\n"
            "  --report 결과.xlsx   보고서 저장 위치 지정\n"
            "  --wait 3            연결 후 3초 기다렸다가 시작 (장비 준비 시간)\n"
            "  --quiet             실패한 단계와 결과만 표시\n"
            "  --list              탭 목록만 보기\n\n"
            "■ 결과\n"
            "  보고서 기본 위치: %s\n"
            "  끝날 때 돌려주는 값: 0 = 합격, 1 = 불합격, 2 = 설정/연결 오류\n"
            "  (배치 파일에서는 %%ERRORLEVEL%% 로 확인)\n\n"
            "※ 화면 프로그램이 같은 포트를 열고 있으면 자동 실행이 포트를 못 엽니다. 화면 쪽 연결을 먼저 닫으세요.\n"
            "※ 엑셀 설정 파일(.xlsx)도 그대로 쓸 수 있습니다."
        ) % (cmd, report_dir)
        ShowTextDialog(self.root, "자동 실행 명령", "화면 없이 시험하고 엑셀 보고서를 남기는 방법", text, copy_text=cmd)

    def open_vport_manager(self):
        if self.vport_manager is not None and self.vport_manager.winfo_exists():
            self.vport_manager.lift()
            return
        self.vport_manager = VPortManager(self.root)

    def tabs(self):
        return [self.notebook.nametowidget(t) for t in self.notebook.tabs()]

    def add_tab(self, cfg=None):
        self.tab_counter += 1
        tab = PortTab(self, self.notebook, self.tab_counter, cfg)
        self.notebook.add(tab, text=tab.var_name.get())
        self.notebook.select(tab)
        tab.update_title()
        self.update_relays()
        if cfg and cfg.get("enabled"):
            tab.open_port()
        return tab

    def close_current_tab(self):
        cur = self.notebook.select()
        if not cur:
            return
        tab = self.notebook.nametowidget(cur)
        if tab.channel.is_open and not messagebox.askyesno(APP_TITLE, "연결이 열려 있습니다. 닫고 탭을 삭제할까요?"):
            return
        tab.close_port()
        tab.logview.close_file()
        self.notebook.forget(tab)
        tab.destroy()
        self.update_relays()

    def poll_events(self):
        live = set(self.tabs())
        try:
            for _ in range(500):
                tab, kind, data, src = self.events.get_nowait()
                if tab in live:
                    tab.handle_event(kind, data, src)
        except queue.Empty:
            pass
        self.root.after(30, self.poll_events)

    # ------------------------------------------------------------------ 설정 파일
    def config_text(self):
        data = {"version": 2, "tabs": [t.to_config() for t in self.tabs()]}
        return json.dumps(data, ensure_ascii=False, indent=2)

    def save_config(self, path, notify=False, text=None):
        text = self.config_text() if text is None else text
        tmp = path + ".tmp"
        try:
            # 임시 파일에 먼저 쓰고 바꿔치기 → 저장 중에 꺼져도 설정 파일이 깨지지 않는다
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, path)
        except OSError as e:
            if notify:
                messagebox.showerror(APP_TITLE, "저장 실패: %s" % e)
            return False
        if path == CONFIG_PATH:
            self._last_saved = text
            self.var_saved.set("설정 자동 저장됨 %s" % time.strftime("%H:%M:%S"))
        if notify:
            messagebox.showinfo(APP_TITLE, "저장했습니다.\n%s" % path)
        return True

    def autosave(self):
        """설정이 바뀌었으면 자동으로 저장한다."""
        try:
            text = self.config_text()
            if text != self._last_saved:
                self.save_config(CONFIG_PATH, text=text)
        finally:
            self.root.after(AUTOSAVE_MS, self.autosave)

    def save_config_as(self):
        path = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON", "*.json")])
        if path:
            self.save_config(path, notify=True)

    def load_config(self, path):
        if not os.path.exists(path):
            return False
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            messagebox.showerror(APP_TITLE, "설정 파일 오류: %s" % e)
            return False
        cfgs = data.get("tabs", [])
        for cfg in cfgs:
            self.add_tab(cfg)
        if cfgs:
            self.notebook.select(0)
        return bool(cfgs)

    def remove_all_tabs(self):
        for tab in self.tabs():
            tab.close_port()
            tab.logview.close_file()
            self.notebook.forget(tab)
            tab.destroy()
        self.tab_counter = 0

    def load_config_from(self):
        path = filedialog.askopenfilename(filetypes=[("JSON", "*.json"), ("All", "*.*")])
        if not path:
            return
        self.remove_all_tabs()
        if not self.load_config(path):
            self.add_tab()

    # ------------------------------------------------------------------ 엑셀
    def export_excel(self):
        path = filedialog.asksaveasfilename(defaultextension=".xlsx", initialfile="통신설정.xlsx",
                                            filetypes=[("Excel", "*.xlsx")])
        if not path:
            return
        try:
            excel_io.export_config(json.loads(self.config_text()), path)
        except Exception as e:
            messagebox.showerror(APP_TITLE, "엑셀 저장 실패: %s" % e)
            return
        messagebox.showinfo(APP_TITLE, "엑셀로 저장했습니다.\n%s\n\n엑셀에서 고친 뒤 [엑셀 불러오기]로 다시 읽을 수 있습니다." % path)

    def make_excel_template(self):
        path = filedialog.asksaveasfilename(defaultextension=".xlsx", initialfile="통신설정_양식.xlsx",
                                            filetypes=[("Excel", "*.xlsx")])
        if not path:
            return
        try:
            excel_io.export_config(excel_io.example_config(), path)
        except Exception as e:
            messagebox.showerror(APP_TITLE, "양식 만들기 실패: %s" % e)
            return
        messagebox.showinfo(APP_TITLE, "예제가 들어 있는 엑셀 양식을 만들었습니다.\n%s\n\n"
                                       "'설명' 시트를 보고 채운 뒤 [엑셀 불러오기]를 누르세요." % path)

    def import_excel(self):
        path = filedialog.askopenfilename(filetypes=[("Excel", "*.xlsx"), ("All", "*.*")])
        if not path:
            return
        try:
            cfg, warnings = excel_io.import_config(path)
        except Exception as e:
            messagebox.showerror(APP_TITLE, str(e))
            return
        answer = messagebox.askyesnocancel(
            APP_TITLE, "엑셀에서 탭 %d개를 읽었습니다.\n\n예: 지금 탭을 모두 엑셀 내용으로 바꾸기\n"
                       "아니오: 지금 탭 뒤에 추가하기" % len(cfg["tabs"]))
        if answer is None:
            return
        if answer:
            self.remove_all_tabs()
        else:
            names = {t.var_name.get() for t in self.tabs()}
            for t in cfg["tabs"]:
                base, n = t["name"], 2
                while t["name"] in names:
                    t["name"] = "%s (%d)" % (base, n)
                    n += 1
                names.add(t["name"])
        for t in cfg["tabs"]:
            self.add_tab(t)
        self.update_relays()
        if warnings:
            ShowTextDialog(self.root, "엑셀 불러오기 - 확인할 내용", "아래 줄은 건너뛰었거나 고쳐서 읽었습니다.",
                           "\n".join(warnings))

    def on_close(self):
        self.save_config(CONFIG_PATH)
        for tab in self.tabs():
            tab.channel.close()
            tab.logview.close_file()
        self.root.destroy()


def main():
    root = tk.Tk()
    theme.apply(root)
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
