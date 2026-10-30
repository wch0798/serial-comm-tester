"""
엑셀(.xlsx)로 설정 내보내기 / 불러오기

엑셀에서 표로 설정을 쉽게 만들고 고칠 수 있게 한다.
시트 구성 (탭이름 칸으로 '연결' 시트의 탭과 이어진다)
- 설명        : 작성 방법
- 연결        : 탭(연결) 1개 = 1행
- 응답규칙    : "이 데이터가 오면 → 이 데이터를 보낸다"
- 매크로      : 자주 보내는 데이터
- 시나리오    : 보내기 / 응답 확인 / 기다리기 단계
- Modbus값    : 슬레이브 레지스터/코일 초기값
- 값자동변화  : 슬레이브 값 자동 변화
- 폴링        : Modbus 마스터 폴링 목록
- 오류주입    : 탭별 오류 주입 설정
- 목록        : 드롭다운 선택 목록 (숨김)

화면과 무관하게 설정 dict(vserial_config.json 형식)만 다룬다.
"""

import checks
import modbus as mb
import polling as pl
import scenario as sc
import serial_core as core

try:
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation
except ImportError:
    openpyxl = None

MAX_ROWS = 500  # 드롭다운/텍스트 서식을 미리 걸어 둘 행 수

YES, NO = "O", "X"
CONN_NAMES = [(core.CONN_PTY, "가상 시리얼"), (core.CONN_SERIAL, "시리얼 포트"), (core.CONN_TCP_SERVER, "TCP 서버"),
              (core.CONN_TCP_CLIENT, "TCP 클라이언트"), (core.CONN_UDP, "UDP"), (core.CONN_CAN, "CAN")]
CAN_NAMES = [(k, lbl) for k, lbl, _, _ in core.CAN_IFACES]
PROTO_NAMES = [("raw", "일반 규칙"), ("mb_slave", "Modbus 슬레이브"), ("mb_master", "Modbus 마스터")]
FRAME_NAMES = ["자동", mb.FRAME_RTU, mb.FRAME_TCP]
AREA_NAMES = [(mb.AREA_COIL, "코일"), (mb.AREA_DI, "입력 접점"), (mb.AREA_IR, "입력 레지스터"), (mb.AREA_HR, "보유 레지스터")]
FMTS = [core.FMT_TEXT, core.FMT_HEX, core.FMT_CAN]
FC_LABELS = [mb.FC_NAMES[k] for k in sorted(mb.FC_NAMES)]
EXC_LABELS = ["%02d %s" % (k, v) for k, v in sorted(mb.EXC_NAMES.items())]

# (시트 이름, [(머리글, 폭, 목록 이름 또는 None, 텍스트 서식 여부)])
SHEETS = {
    "연결": [("탭이름", 16, None, True), ("사용", 6, "예아니오", False), ("연결방식", 14, "연결방식", False),
             ("프로토콜", 16, "프로토콜", False), ("프레임", 7, "프레임", False),
             ("가상포트(Windows)", 14, None, True), ("링크경로(Mac/Linux)", 18, None, True),
             ("시리얼포트", 14, None, True), ("Baud", 9, "Baud", False), ("Data", 6, "Data", False),
             ("Parity", 7, "Parity", False), ("Stop", 6, "Stop", False),
             ("IP(상대)", 15, None, True), ("포트(상대)", 10, None, False),
             ("UDP 내 IP", 13, None, True), ("UDP 내 포트", 11, None, False), ("중계대상", 14, None, True),
             ("자동응답사용", 12, "예아니오", False), ("장치주소", 9, None, False), ("응답지연(ms)", 12, None, False),
             ("마스터 기능코드", 22, "기능코드", False), ("마스터 시작주소", 14, None, False),
             ("마스터 개수", 11, None, False), ("마스터 주기(ms, 0=끔)", 20, None, False),
             ("시나리오 반복(0=무한)", 19, None, False), ("시나리오 실패시 멈춤", 18, "예아니오", False),
             ("CAN 인터페이스", 26, "CAN", False), ("CAN 채널", 14, None, True), ("CAN 비트레이트", 13, "CAN속도", False),
             ("폴링 자동 시작", 13, "예아니오", False), ("시나리오 보고서 자동저장", 22, "예아니오", False)],
    "응답규칙": [("탭이름", 16, None, True), ("사용", 6, "예아니오", False), ("수신 데이터", 28, None, True),
               ("수신 형식", 9, "형식", False), ("응답 데이터", 28, None, True), ("응답 형식", 9, "형식", False),
               ("응답 검사값", 26, "검사값", False), ("지연(ms)", 9, None, False)],
    "매크로": [("탭이름", 16, None, True), ("이름", 14, None, True), ("데이터", 30, None, True),
              ("형식", 7, "형식", False), ("검사값", 26, "검사값", False), ("반복(ms)", 9, None, False)],
    "시나리오": [("탭이름", 16, None, True), ("사용", 6, "예아니오", False), ("종류", 11, "단계", False),
               ("데이터", 30, None, True), ("형식", 7, "형식", False), ("검사값", 26, "검사값", False),
               ("시간(ms)", 9, None, False)],
    "Modbus값": [("탭이름", 16, None, True), ("영역", 14, "영역", False), ("주소", 8, None, False), ("값", 10, None, False)],
    "값자동변화": [("탭이름", 16, None, True), ("사용", 6, "예아니오", False), ("영역", 14, "영역", False),
                ("주소", 8, None, False), ("방식", 16, "방식", False), ("최소", 9, None, False),
                ("최대", 9, None, False), ("증감", 7, None, False), ("간격(ms)", 9, None, False)],
    "폴링": [("탭이름", 16, None, True), ("사용", 6, "예아니오", False), ("이름", 14, None, True),
            ("장치", 6, None, False), ("기능코드", 22, "폴링기능", False), ("주소", 8, None, False),
            ("형식", 10, "데이터형식", False), ("순서", 18, "순서", False), ("배율", 8, None, False),
            ("오프셋", 8, None, False), ("간격(ms)", 9, None, False), ("그래프", 7, "예아니오", False)],
    "오류주입": [("탭이름", 16, None, True), ("사용", 6, "예아니오", False), ("응답 안 함 %", 12, None, False),
              ("데이터 변조 %", 13, None, False), ("잘라 보내기 %", 13, None, False), ("두 번 보내기 %", 13, None, False),
              ("쓰레기 바이트 %", 14, None, False), ("Modbus 예외 %", 13, None, False),
              ("예외 코드", 24, "예외코드", False), ("추가 지연 최소(ms)", 17, None, False),
              ("추가 지연 최대(ms)", 17, None, False)],
}

LISTS = {
    "예아니오": [YES, NO],
    "연결방식": [n for _, n in CONN_NAMES],
    "프로토콜": [n for _, n in PROTO_NAMES],
    "프레임": FRAME_NAMES,
    "Baud": ["1200", "2400", "4800", "9600", "19200", "38400", "57600", "115200", "230400", "460800", "921600"],
    "Data": ["5", "6", "7", "8"],
    "Parity": ["N", "E", "O", "M", "S"],
    "Stop": ["1", "1.5", "2"],
    "형식": FMTS,
    "검사값": [lbl for _, lbl in checks.CHECKS],
    "단계": [lbl for _, lbl in sc.STEP_KINDS],
    "영역": [n for _, n in AREA_NAMES],
    "방식": [lbl for _, lbl in mb.SIM_MODES],
    "기능코드": FC_LABELS,
    "예외코드": EXC_LABELS,
    "CAN": [n for _, n in CAN_NAMES],
    "CAN속도": core.CAN_BITRATES,
    "폴링기능": [mb.FC_NAMES[k] for k in pl.READ_FCS],
    "데이터형식": [n for _, n in pl.DTYPES],
    "순서": [n for _, n in pl.ORDERS],
}

HELP = [
    "통신 테스트 도구 - 엑셀 설정 파일",
    "",
    "■ 사용 방법",
    "1. 각 시트에 한 줄씩 입력합니다. 노란 머리글 아래부터 쓰면 됩니다.",
    "2. '연결' 시트의 한 줄이 프로그램의 탭 하나가 됩니다.",
    "3. 다른 시트의 '탭이름'은 '연결' 시트의 탭이름과 똑같이 써야 그 탭에 들어갑니다.",
    "4. 회색 칸 없이 드롭다운(▼)이 있는 칸은 목록에서 고르세요.",
    "5. 다 쓰면 저장하고, 프로그램에서 [엑셀 불러오기]를 누르세요.",
    "",
    "■ 값 쓰는 법",
    "- 사용: O 또는 X  (예/아니오, Y/N 도 됩니다)",
    "- TEXT 데이터: AT\\r  처럼 씁니다. \\r \\n \\t \\xNN 사용 가능",
    "- HEX 데이터: 02 31 03  또는  023103",
    "- 프레임: '자동'이면 시리얼은 RTU, TCP/UDP는 TCP로 정해집니다.",
    "- 가상 시리얼: Windows는 '가상포트(Windows)' 칸(예: COM10), Mac/Linux는 '링크경로' 칸(예: /tmp/vcom1)을 씁니다.",
    "- 포트(상대): TCP 서버는 기다릴 포트, TCP 클라이언트/UDP는 상대 포트입니다.",
    "- 장치주소/응답지연: Modbus 슬레이브는 응답할 장치 번호, 마스터는 요청할 장치 번호입니다.",
    "- 시나리오 종류: 보내기 / 응답 확인 / 기다리기.  시간(ms)은 응답 확인의 최대 대기 시간 또는 기다릴 시간입니다.",
    "- Modbus값: 주소는 0부터 시작하는 번호입니다 (PLC 주소 40001 = 보유 레지스터 0번).",
    "- CAN: 연결방식을 CAN으로 하고, 데이터 형식을 CAN으로 고른 뒤 123#01 02 03 처럼 씁니다 (여러 프레임은 ; 로 구분).",
    "       확장 ID는 18FF50E5#11 22 처럼 8자리로 씁니다. 응답규칙의 수신 데이터 123# 은 'ID 123 이면 데이터 상관없이'라는 뜻입니다.",
    "- 폴링: Modbus 마스터 탭에서 주기적으로 읽을 값입니다. 값 = 읽은 값 × 배율 + 오프셋.",
    "",
    "■ 주의",
    "- 시트 이름과 머리글(첫 줄)은 바꾸지 마세요. 열 순서는 바뀌어도 됩니다.",
    "- 불러올 때 문제가 있는 줄은 건너뛰고, 어느 시트 몇 번째 줄인지 알려줍니다.",
]


class ExcelError(Exception):
    pass


def _require():
    if openpyxl is None:
        raise ExcelError("엑셀 기능에 필요한 openpyxl이 없습니다.\n명령 창에서 설치하세요: pip install openpyxl")


def _name_of(pairs, key, default=""):
    for k, n in pairs:
        if k == key:
            return n
    return default


def _yn(v):
    return YES if v else NO


# ---------------------------------------------------------------------------
# 내보내기
# ---------------------------------------------------------------------------

def _rows_from_config(cfg):
    rows = {name: [] for name in SHEETS}
    for t in cfg.get("tabs", []):
        name = t.get("name", "")
        conn = t.get("conn", core.CONN_PTY)
        if conn == core.CONN_VCOM:
            conn = core.CONN_PTY
        slave = t.get("slave", {})
        master = t.get("master", {})
        proto = t.get("protocol", "raw")
        scen = t.get("scenario", {})
        unit = master.get("unit_id", "1") if proto == "mb_master" else slave.get("unit_id", "1")
        rows["연결"].append([
            name, _yn(t.get("enabled")), _name_of(CONN_NAMES, conn), _name_of(PROTO_NAMES, proto),
            t.get("frame", "자동"), t.get("vcom_name", ""), t.get("link_path", ""), t.get("port", ""),
            t.get("baudrate", "115200"), t.get("bytesize", "8"), t.get("parity", "N"), t.get("stopbits", "1"),
            t.get("host", ""), _num(t.get("tcp_port", "")), t.get("udp_bind", ""), _num(t.get("udp_port", "")),
            "" if t.get("relay") in (None, "", "(없음)") else t.get("relay"), _yn(t.get("auto_enabled", True)),
            _num(unit), _num(slave.get("delay_ms", 0)), master.get("fc", mb.FC_NAMES[3]),
            _num(master.get("addr", 0)), _num(master.get("count", 10)),
            _num(master.get("poll_ms", 1000)) if master.get("poll") else 0,
            _num(scen.get("repeat", 1)), _yn(scen.get("stop_on_fail", True)),
            _name_of(CAN_NAMES, t.get("can_iface", "virtual"), CAN_NAMES[0][1]), t.get("can_channel", "vcan0"),
            _num(t.get("can_bitrate", 500000)), _yn(master.get("poll_list", {}).get("running", False)),
            _yn(scen.get("auto_report", False))])
        for it in master.get("poll_list", {}).get("items", []):
            rows["폴링"].append([name, _yn(it.get("enabled", True)), it.get("name", ""), it.get("unit", 1),
                               mb.FC_NAMES.get(int(it.get("fc", 3)), mb.FC_NAMES[3]), it.get("addr", 0),
                               dict(pl.DTYPES).get(it.get("dtype"), "UINT16"), dict(pl.ORDERS).get(it.get("order"), "ABCD (기본)"),
                               _num(it.get("scale", 1)), _num(it.get("offset", 0)), it.get("interval_ms", 1000),
                               _yn(it.get("graph", True))])
        for r in t.get("rules", []):
            rows["응답규칙"].append([name, _yn(r.get("enabled", True)), r.get("match", ""), r.get("match_fmt", "TEXT"),
                                 r.get("response", ""), r.get("response_fmt", "TEXT"),
                                 checks.label_of(r.get("response_check", "none")), r.get("delay_ms", 0)])
        for m in t.get("macros", []):
            rows["매크로"].append([name, m.get("name", ""), m.get("data", ""), m.get("fmt", "TEXT"),
                                checks.label_of(m.get("check", "none")), m.get("repeat_ms", 1000)])
        for s in scen.get("steps", []):
            rows["시나리오"].append([name, _yn(s.get("enabled", True)), dict(sc.STEP_KINDS).get(s.get("kind"), "보내기"),
                                 s.get("data", ""), s.get("fmt", "TEXT"), checks.label_of(s.get("check", "none")),
                                 s.get("ms", 1000)])
        for area, values in slave.get("data", {}).items():
            for addr, v in sorted(values.items(), key=lambda kv: int(kv[0])):
                rows["Modbus값"].append([name, _name_of(AREA_NAMES, area), int(addr), int(v)])
        for e in t.get("sim", []):
            rows["값자동변화"].append([name, _yn(e.get("enabled", True)), _name_of(AREA_NAMES, e.get("area")),
                                  e.get("addr", 0), dict(mb.SIM_MODES).get(e.get("mode"), "증가"),
                                  e.get("vmin", 0), e.get("vmax", 100), e.get("step", 1), e.get("interval_ms", 1000)])
        f = t.get("faults", {})
        if f and (f.get("enabled") or any(f.get(k) for k in ("drop_pct", "corrupt_pct", "truncate_pct",
                                                                "duplicate_pct", "noise_pct", "exception_pct",
                                                                "delay_max_ms"))):
            code = int(f.get("exception_code", 4))
            rows["오류주입"].append([name, _yn(f.get("enabled")), f.get("drop_pct", 0), f.get("corrupt_pct", 0),
                                 f.get("truncate_pct", 0), f.get("duplicate_pct", 0), f.get("noise_pct", 0),
                                 f.get("exception_pct", 0),
                                 next((lbl for lbl in EXC_LABELS if int(lbl[:2]) == code), EXC_LABELS[3]),
                                 f.get("delay_min_ms", 0), f.get("delay_max_ms", 0)])
    return rows


def _num(v):
    """숫자로 바꿀 수 있으면 숫자로 (엑셀에서 숫자로 보이게)"""
    try:
        f = float(v)
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return v


def export_config(cfg, path):
    _require()
    wb = openpyxl.Workbook()
    head_fill = PatternFill("solid", fgColor="FFE699")
    head_font = Font(bold=True)

    ws = wb.active
    ws.title = "설명"
    for i, line in enumerate(HELP, 1):
        ws.cell(row=i, column=1, value=line)
    ws["A1"].font = Font(bold=True, size=14)
    ws.column_dimensions["A"].width = 110

    # 드롭다운 목록 시트
    lst = wb.create_sheet("목록")
    list_refs = {}
    for col, (lname, values) in enumerate(LISTS.items(), 1):
        letter = get_column_letter(col)
        lst.cell(row=1, column=col, value=lname).font = head_font
        for r, v in enumerate(values, 2):
            lst.cell(row=r, column=col, value=v)
        list_refs[lname] = "'목록'!$%s$2:$%s$%d" % (letter, letter, len(values) + 1)
    lst.sheet_state = "hidden"

    rows = _rows_from_config(cfg)
    for sname, cols in SHEETS.items():
        ws = wb.create_sheet(sname, index=len(wb.sheetnames) - 1)
        for c, (title, width, lname, is_text) in enumerate(cols, 1):
            cell = ws.cell(row=1, column=c, value=title)
            cell.fill = head_fill
            cell.font = head_font
            cell.alignment = Alignment(horizontal="center")
            letter = get_column_letter(c)
            ws.column_dimensions[letter].width = width
            if lname:
                dv = DataValidation(type="list", formula1=list_refs[lname], allow_blank=True,
                                    showErrorMessage=False)
                dv.add("%s2:%s%d" % (letter, letter, MAX_ROWS))
                ws.add_data_validation(dv)
            if is_text:
                # 0102 같은 HEX 값을 엑셀이 숫자로 바꾸지 않도록 텍스트 서식
                for r in range(2, MAX_ROWS + 1):
                    ws.cell(row=r, column=c).number_format = "@"
        for r, row in enumerate(rows[sname], 2):
            for c, v in enumerate(row, 1):
                ws.cell(row=r, column=c, value=v)
        ws.freeze_panes = "B2"
    wb.active = 1  # '연결' 시트가 먼저 보이게
    wb.save(path)


# ---------------------------------------------------------------------------
# 불러오기
# ---------------------------------------------------------------------------

def _text(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def _bool(v, default=True):
    s = _text(v).upper()
    if not s:
        return default
    return s in ("O", "Y", "YES", "TRUE", "1", "예", "사용", "☑", "V", "ON")


def _int(v, default=0):
    s = _text(v)
    if not s:
        return default
    try:
        return int(float(s)) if "x" not in s.lower() else int(s, 0)
    except ValueError:
        raise ValueError("숫자가 아닙니다: %r" % s)


def _float(v, default=0.0):
    s = _text(v).replace("%", "")
    return float(s) if s else default


def _key(pairs, v, what, default=None):
    s = _text(v)
    if not s:
        if default is None:
            raise ValueError("%s 값이 비어 있습니다" % what)
        return default
    for k, n in pairs:
        if s == n or s.lower() == str(k).lower():
            return k
    raise ValueError("%s 값이 올바르지 않습니다: %r" % (what, s))


def _fmt(v):
    s = _text(v).upper() or core.FMT_TEXT
    if s not in FMTS:
        raise ValueError("형식은 TEXT 또는 HEX 입니다: %r" % s)
    return s


def _check(v):
    s = _text(v)
    if not s or s == "없음":
        return "none"
    for k, lbl in checks.CHECKS:
        if s in (k, lbl) or lbl.startswith(s):
            return k
    raise ValueError("검사값이 올바르지 않습니다: %r" % s)


def _read_sheet(wb, name):
    """반환: [(엑셀 행 번호, {머리글: 값}), ...] - 빈 줄은 건너뜀"""
    if name not in wb.sheetnames:
        return []
    ws = wb[name]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    heads = [_text(h) for h in rows[0]]
    out = []
    for i, row in enumerate(rows[1:], 2):
        if row is None or all(_text(v) == "" for v in row):
            continue
        out.append((i, {h: row[c] if c < len(row) else None for c, h in enumerate(heads) if h}))
    return out


def import_config(path):
    """반환: (설정 dict, 경고 목록)"""
    _require()
    try:
        wb = openpyxl.load_workbook(path, data_only=True)
    except Exception as e:
        raise ExcelError("엑셀 파일을 열 수 없습니다: %s" % e)
    if "연결" not in wb.sheetnames:
        raise ExcelError("'연결' 시트가 없습니다. 프로그램에서 만든 엑셀 양식을 사용하세요.")
    warnings = []
    tabs = {}
    order = []

    def warn(sheet, row, msg):
        warnings.append("%s 시트 %d행: %s" % (sheet, row, msg))

    for row_no, r in _read_sheet(wb, "연결"):
        try:
            name = _text(r.get("탭이름"))
            if not name:
                raise ValueError("탭이름이 비어 있습니다")
            if name in tabs:
                raise ValueError("탭이름 '%s' 이(가) 중복됩니다" % name)
            conn = _key(CONN_NAMES, r.get("연결방식"), "연결방식", core.CONN_PTY)
            proto = _key(PROTO_NAMES, r.get("프로토콜"), "프로토콜", "raw")
            frame = _text(r.get("프레임")).upper()
            if frame in ("", "자동"):
                frame = mb.FRAME_TCP if conn in core.TCP_CONNS else mb.FRAME_RTU
            elif frame not in (mb.FRAME_RTU, mb.FRAME_TCP):
                raise ValueError("프레임은 자동/RTU/TCP 입니다: %r" % frame)
            fc = _text(r.get("마스터 기능코드")) or mb.FC_NAMES[3]
            fc_num = _int(fc[:2]) if fc[:2].isdigit() else _int(fc)
            if fc_num not in mb.FC_NAMES:
                raise ValueError("마스터 기능코드가 올바르지 않습니다: %r" % fc)
            poll_ms = _int(r.get("마스터 주기(ms, 0=끔)"), 0)
            unit = str(_int(r.get("장치주소"), 1))
            relay = _text(r.get("중계대상")) or "(없음)"
            t = dict(
                name=name, enabled=_bool(r.get("사용"), False), conn=conn, protocol=proto, frame=frame,
                vcom_name=_text(r.get("가상포트(Windows)")) or "COM%d" % (10 + len(order)),
                link_path=_text(r.get("링크경로(Mac/Linux)")), port=_text(r.get("시리얼포트")),
                baudrate=_text(r.get("Baud")) or "115200", bytesize=_text(r.get("Data")) or "8",
                parity=_text(r.get("Parity")).upper() or "N", stopbits=_text(r.get("Stop")) or "1",
                host=_text(r.get("IP(상대)")) or "127.0.0.1", tcp_port=str(_int(r.get("포트(상대)"), 5020)),
                udp_bind=_text(r.get("UDP 내 IP")) or "0.0.0.0", udp_port=str(_int(r.get("UDP 내 포트"), 0)),
                relay=relay, auto_enabled=_bool(r.get("자동응답사용"), True),
                can_iface=_key(CAN_NAMES, r.get("CAN 인터페이스"), "CAN 인터페이스", "virtual"),
                can_channel=_text(r.get("CAN 채널")) or "vcan0", can_bitrate=str(_int(r.get("CAN 비트레이트"), 500000)),
                rules=[], macros=[], sim=[], faults={},
                scenario=dict(steps=[], repeat=str(_int(r.get("시나리오 반복(0=무한)"), 1)),
                              stop_on_fail=_bool(r.get("시나리오 실패시 멈춤"), True),
                              auto_report=_bool(r.get("시나리오 보고서 자동저장"), False)),
                slave=dict(unit_id=unit, delay_ms=str(_int(r.get("응답지연(ms)"), 0)),
                           data={a: {} for a, _ in AREA_NAMES}),
                master=dict(unit_id=unit, fc=mb.FC_NAMES[fc_num], addr=str(_int(r.get("마스터 시작주소"), 0)),
                            count=str(_int(r.get("마스터 개수"), 10)), poll=poll_ms > 0,
                            poll_ms=str(poll_ms or 1000),
                            poll_list=dict(items=[], running=_bool(r.get("폴링 자동 시작"), False))))
        except ValueError as e:
            warn("연결", row_no, "%s → 이 탭은 건너뜀" % e)
            continue
        tabs[name] = t
        order.append(name)

    def tab_of(sheet, row_no, r):
        name = _text(r.get("탭이름"))
        if name not in tabs:
            warn(sheet, row_no, "탭이름 '%s' 이(가) 연결 시트에 없습니다 → 건너뜀" % name)
            return None
        return tabs[name]

    for row_no, r in _read_sheet(wb, "응답규칙"):
        t = tab_of("응답규칙", row_no, r)
        if t is None:
            continue
        try:
            rule = core.Rule(enabled=_bool(r.get("사용")), match=_text(r.get("수신 데이터")),
                             match_fmt=_fmt(r.get("수신 형식")), response=_text(r.get("응답 데이터")),
                             response_fmt=_fmt(r.get("응답 형식")), delay_ms=_int(r.get("지연(ms)"), 0),
                             response_check=_check(r.get("응답 검사값")))
            if not rule.match_bytes():
                raise ValueError("수신 데이터가 비어 있습니다")
            rule.response_bytes()
        except ValueError as e:
            warn("응답규칙", row_no, "%s → 건너뜀" % e)
            continue
        t["rules"].append(rule.to_dict())

    for row_no, r in _read_sheet(wb, "매크로"):
        t = tab_of("매크로", row_no, r)
        if t is None:
            continue
        try:
            m = dict(name=_text(r.get("이름")) or "매크로", data=_text(r.get("데이터")), fmt=_fmt(r.get("형식")),
                     check=_check(r.get("검사값")), repeat_ms=max(10, _int(r.get("반복(ms)"), 1000)))
            if not core.to_bytes(m["data"], m["fmt"]):
                raise ValueError("데이터가 비어 있습니다")
        except ValueError as e:
            warn("매크로", row_no, "%s → 건너뜀" % e)
            continue
        t["macros"].append(m)

    kinds = [(k, lbl) for k, lbl in sc.STEP_KINDS]
    for row_no, r in _read_sheet(wb, "시나리오"):
        t = tab_of("시나리오", row_no, r)
        if t is None:
            continue
        try:
            step = sc.new_step(_key(kinds, r.get("종류"), "종류"), _text(r.get("데이터")), _fmt(r.get("형식")),
                               _check(r.get("검사값")), _int(r.get("시간(ms)"), 1000), _bool(r.get("사용")))
            data = sc.step_bytes(step)
            if step["kind"] == sc.STEP_SEND and not data:
                raise ValueError("보낼 데이터가 비어 있습니다")
        except ValueError as e:
            warn("시나리오", row_no, "%s → 건너뜀" % e)
            continue
        t["scenario"]["steps"].append(step)

    for row_no, r in _read_sheet(wb, "Modbus값"):
        t = tab_of("Modbus값", row_no, r)
        if t is None:
            continue
        try:
            area = _key(AREA_NAMES, r.get("영역"), "영역")
            addr = _int(r.get("주소"))
            val = _int(r.get("값"), 0)
            if not 0 <= addr <= 0xFFFF:
                raise ValueError("주소는 0~65535 입니다")
            if area in mb.BIT_AREAS:
                val = 1 if val else 0
            elif not -0x8000 <= val <= 0xFFFF:
                raise ValueError("값은 -32768~65535 입니다")
        except ValueError as e:
            warn("Modbus값", row_no, "%s → 건너뜀" % e)
            continue
        if val:
            t["slave"]["data"][area][str(addr)] = val & 0xFFFF

    modes = [(k, lbl) for k, lbl in mb.SIM_MODES]
    for row_no, r in _read_sheet(wb, "값자동변화"):
        t = tab_of("값자동변화", row_no, r)
        if t is None:
            continue
        try:
            e = mb.SimEntry(_bool(r.get("사용")), _key(AREA_NAMES, r.get("영역"), "영역"), _int(r.get("주소")),
                            _key(modes, r.get("방식"), "방식"), _int(r.get("최소"), 0), _int(r.get("최대"), 100),
                            _float(r.get("증감"), 1.0), _int(r.get("간격(ms)"), 1000))
        except ValueError as e:
            warn("값자동변화", row_no, "%s → 건너뜀" % e)
            continue
        t["sim"].append(e.to_dict())

    dtypes = [(k, n) for k, n in pl.DTYPES]
    orders = [(k, n) for k, n in pl.ORDERS] + [(k, k) for k, _ in pl.ORDERS]
    for row_no, r in _read_sheet(wb, "폴링"):
        t = tab_of("폴링", row_no, r)
        if t is None:
            continue
        try:
            fc = _text(r.get("기능코드")) or "03"
            it = pl.PollItem(_bool(r.get("사용")), _text(r.get("이름")), _int(r.get("장치"), 1),
                             _int(fc[:2]) if fc[:2].isdigit() else _int(fc), _int(r.get("주소"), 0),
                             _key(dtypes, r.get("형식"), "형식", "u16"), _key(orders, r.get("순서"), "순서", "ABCD"),
                             _float(r.get("배율"), 1.0), _float(r.get("오프셋"), 0.0), _int(r.get("간격(ms)"), 1000),
                             _bool(r.get("그래프")))
            it.validate()
        except ValueError as e:
            warn("폴링", row_no, "%s → 건너뜀" % e)
            continue
        t["master"]["poll_list"]["items"].append(it.to_dict())

    for row_no, r in _read_sheet(wb, "오류주입"):
        t = tab_of("오류주입", row_no, r)
        if t is None:
            continue
        try:
            code = _text(r.get("예외 코드")) or "04"
            t["faults"] = dict(
                enabled=_bool(r.get("사용"), False), drop_pct=_float(r.get("응답 안 함 %")),
                corrupt_pct=_float(r.get("데이터 변조 %")), truncate_pct=_float(r.get("잘라 보내기 %")),
                duplicate_pct=_float(r.get("두 번 보내기 %")), noise_pct=_float(r.get("쓰레기 바이트 %")),
                exception_pct=_float(r.get("Modbus 예외 %")), exception_code=_int(code[:2]),
                delay_min_ms=_int(r.get("추가 지연 최소(ms)"), 0), delay_max_ms=_int(r.get("추가 지연 최대(ms)"), 0))
        except ValueError as e:
            warn("오류주입", row_no, "%s → 건너뜀" % e)

    for name in order:
        relay = tabs[name]["relay"]
        if relay != "(없음)" and relay not in tabs:
            warnings.append("연결 시트: '%s' 탭의 중계대상 '%s' 이(가) 없습니다 → 중계 안 함" % (name, relay))
            tabs[name]["relay"] = "(없음)"
    if not order:
        raise ExcelError("불러올 탭이 없습니다.\n" + "\n".join(warnings[:20]))
    return {"version": 2, "tabs": [tabs[n] for n in order]}, warnings


# ---------------------------------------------------------------------------
# 양식 (예제 포함)
# ---------------------------------------------------------------------------

def example_config():
    """엑셀 양식에 넣을 예제"""
    return {"version": 2, "tabs": [
        dict(name="장비 흉내", enabled=False, conn=core.CONN_PTY, protocol="raw", frame="RTU",
             vcom_name="COM10", link_path="/tmp/vcom1", baudrate="9600",
             rules=[dict(enabled=True, match="AT\\r", match_fmt="TEXT", response="OK\\r\\n", response_fmt="TEXT",
                         delay_ms=0, response_check="none"),
                    dict(enabled=True, match="02 01", match_fmt="HEX", response="02 81 00", response_fmt="HEX",
                         delay_ms=20, response_check="crc16_modbus")],
             macros=[dict(name="상태 요청", data="STAT\\r\\n", fmt="TEXT", check="none", repeat_ms=1000)],
             scenario=dict(steps=[sc.new_step(sc.STEP_SEND, "02 01", "HEX", "crc16_modbus"),
                                  sc.new_step(sc.STEP_EXPECT, "02 81", "HEX", "none", 500),
                                  sc.new_step(sc.STEP_WAIT, "", "TEXT", "none", 200)], repeat="1", stop_on_fail=True)),
        dict(name="Modbus 장치", enabled=False, conn=core.CONN_TCP_SERVER, protocol="mb_slave", frame="TCP",
             host="127.0.0.1", tcp_port="5020",
             slave=dict(unit_id="1", delay_ms="0", data={mb.AREA_HR: {"0": 1234, "1": 5678}, mb.AREA_COIL: {"0": 1}}),
             sim=[dict(enabled=True, area=mb.AREA_IR, addr=0, mode="sine", vmin=0, vmax=1000, step=10,
                       interval_ms=500)],
             faults=dict(enabled=False, drop_pct=10, exception_pct=0, exception_code=6)),
        dict(name="Modbus 읽기", enabled=False, conn=core.CONN_TCP_CLIENT, protocol="mb_master", frame="TCP",
             host="127.0.0.1", tcp_port="5020",
             master=dict(unit_id="1", fc=mb.FC_NAMES[3], addr="0", count="10", poll=False, poll_ms="1000",
                         poll_list=dict(running=False, items=[
                             pl.PollItem(True, "값0", 1, 3, 0, "u16").to_dict(),
                             pl.PollItem(True, "센서(×0.1)", 1, 4, 0, "s16", scale=0.1).to_dict(),
                             pl.PollItem(True, "코일0", 1, 1, 0, "bit", graph=False).to_dict()]))),
        dict(name="CAN ECU", enabled=False, conn=core.CONN_CAN, protocol="raw", can_iface="virtual",
             can_channel="vcan0", can_bitrate="500000",
             rules=[dict(enabled=True, match="7DF#02 01 0C", match_fmt="CAN", response="7E8#04 41 0C 1A F8",
                         response_fmt="CAN", delay_ms=5, response_check="none")],
             macros=[dict(name="하트비트", data="700#05", fmt="CAN", check="none", repeat_ms=1000)]),
        dict(name="CAN 테스터", enabled=False, conn=core.CONN_CAN, protocol="raw", can_iface="virtual",
             can_channel="vcan0", can_bitrate="500000",
             scenario=dict(steps=[sc.new_step(sc.STEP_SEND, "7DF#02 01 0C", "CAN"),
                                  sc.new_step(sc.STEP_EXPECT, "7E8#04 41 0C", "CAN", "none", 500)],
                           repeat="1", stop_on_fail=True)),
    ]}
