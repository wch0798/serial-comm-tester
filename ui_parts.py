"""
화면 구성 요소
- LogView: 통신 로그 (보낸/받은 데이터 색 구분, 한 줄 묶기, 파일 기록, 통계)
- MacroPanel: 매크로 (자주 보내는 데이터, 반복 송신)
- ScenarioPanel: 시나리오 (보내기 → 응답 확인 → 기다리기 자동 실행)
- FaultPanel: 오류 주입
- SimPanel: Modbus 값 자동 변화
- PollPanel / GraphWindow: Modbus 폴링 목록, 필요할 때 여는 실시간 그래프
"""

import csv
import os
import subprocess
import sys
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import checks
import modbus as mb
import polling as pl
import report
import scenario as sc
import serial_core as core

if sys.platform.startswith("win"):
    MONO = ("Consolas", 10)
elif sys.platform == "darwin":
    MONO = ("Menlo", 11)
else:
    MONO = ("DejaVu Sans Mono", 10)
MONO_BOLD = MONO + ("bold",)

FMTS = [core.FMT_TEXT, core.FMT_HEX, core.FMT_CAN]
CHECK_LABELS = [lbl for _, lbl in checks.CHECKS]
CHECK_ON = "☑"
CHECK_OFF = "☐"


def parse_int(s):
    s = str(s).strip()
    try:
        return int(s, 0)
    except ValueError:
        return int(s, 10)


def open_folder(path):
    os.makedirs(path, exist_ok=True)
    if sys.platform.startswith("win"):
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def tree_box(parent, columns, height=8):
    """(이름, 제목, 폭, 정렬, 늘어남) 목록으로 스크롤 있는 Treeview를 만든다."""
    frm = ttk.Frame(parent)
    tree = ttk.Treeview(frm, columns=[c[0] for c in columns], show="headings", height=height, selectmode="browse")
    for name, title, width, anchor, stretch in columns:
        tree.heading(name, text=title)
        tree.column(name, width=width, anchor=anchor, stretch=stretch)
    sb = ttk.Scrollbar(frm, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    tree.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    return frm, tree


def edit_buttons(parent, on_new, on_add, on_update, on_delete, on_move):
    bar = ttk.Frame(parent)
    ttk.Button(bar, text="▼", width=3, command=lambda: on_move(1)).pack(side="right")
    ttk.Button(bar, text="▲", width=3, command=lambda: on_move(-1)).pack(side="right")
    for text, cmd in (("삭제", on_delete), ("수정", on_update), ("추가", on_add), ("새로", on_new)):
        ttk.Button(bar, text=text, width=5, command=cmd).pack(side="right", padx=2)
    return bar


def selected_index(tree):
    sel = tree.selection()
    return int(sel[0]) if sel else None


def move_item(items, tree, delta):
    i = selected_index(tree)
    if i is None:
        return None
    j = i + delta
    if 0 <= j < len(items):
        items[i], items[j] = items[j], items[i]
        return j
    return None


# ---------------------------------------------------------------------------
# 통신 로그
# ---------------------------------------------------------------------------

# 종류: (표시, 글자색, 줄 배경색) - 색은 report.KIND_COLORS 에서 가져온다 (보고서와 같은 색)
KIND_LABELS_LOG = {"rx": "◀ 수신", "tx": "▶ 송신", "auto": "▶ 자동응답", "relay": "▶ 중계", "decode": "■ 해석",
                   "fault": "⚠ 오류주입", "scenario": "▣ 시나리오", "info": "● 정보", "error": "✖ 오류"}
KIND_STYLE = {k: (lbl,) + report.KIND_COLORS[k] for k, lbl in KIND_LABELS_LOG.items()}
FILTERS = [("rx", "수신"), ("tx", "송신"), ("auto", "자동응답"), ("relay", "중계"),
           ("decode", "Modbus 해석"), ("info", "정보")]
MERGE_OPTIONS = ["끄기", "10 ms", "20 ms", "50 ms", "100 ms", "200 ms", "줄바꿈 (\\n)", "CR (\\r)"]
VIEW_OPTIONS = ["TEXT", "HEX", "TEXT+HEX"]
MAX_LOG_LINES = 5000
SENT_KINDS = ("tx", "auto", "relay")


class LogView(ttk.Frame):
    def __init__(self, parent, name_getter, log_dir):
        ttk.Frame.__init__(self, parent)
        self.name_getter = name_getter
        self.log_dir = log_dir
        self.var_view = tk.StringVar(value="TEXT+HEX")
        self.var_merge = tk.StringVar(value="20 ms")
        self.var_delta = tk.BooleanVar(value=True)
        self.var_autoscroll = tk.BooleanVar(value=True)
        self.var_pause = tk.BooleanVar(value=False)
        self.var_file = tk.BooleanVar(value=False)
        self.var_file_fmt = tk.StringVar(value="TXT")
        self.var_stats = tk.StringVar()
        self.filters = {k: tk.BooleanVar(value=True) for k, _ in FILTERS}
        self.can_mode = False  # CAN 연결이면 프레임 단위로 보여준다
        self.pending = {}  # 상대 -> [첫 수신 시각, 모은 데이터]
        self._flush_job = None
        self.paused_lines = []
        self.last_t = None
        self.stats = {}
        self._file = None
        self._file_key = None
        self._build()
        self.var_pause.trace_add("write", lambda *a: self._on_pause())
        self.var_file.trace_add("write", lambda *a: self.close_file())
        self.var_file_fmt.trace_add("write", lambda *a: self.close_file())
        self._update_stats()

    def _build(self):
        r1 = ttk.Frame(self)
        r1.pack(fill="x")
        ttk.Label(r1, text="표시").pack(side="left")
        ttk.Combobox(r1, textvariable=self.var_view, values=VIEW_OPTIONS, width=9,
                     state="readonly").pack(side="left", padx=(2, 8))
        ttk.Label(r1, text="한 줄 묶기").pack(side="left")
        ttk.Combobox(r1, textvariable=self.var_merge, values=MERGE_OPTIONS, width=10,
                     state="readonly").pack(side="left", padx=(2, 8))
        mb_view = ttk.Menubutton(r1, text="보기 ▾")
        menu = tk.Menu(mb_view, tearoff=False)
        for k, label in FILTERS:
            menu.add_checkbutton(label="%s 보이기" % label, variable=self.filters[k])
        menu.add_separator()
        menu.add_checkbutton(label="앞 줄과의 시간 간격 (+ms)", variable=self.var_delta)
        menu.add_checkbutton(label="자동 스크롤", variable=self.var_autoscroll)
        mb_view["menu"] = menu
        mb_view.pack(side="left")
        ttk.Checkbutton(r1, text="일시정지", variable=self.var_pause).pack(side="left", padx=8)

        r2 = ttk.Frame(self)
        r2.pack(fill="x", pady=(4, 0))
        ttk.Checkbutton(r2, text="파일로 기록", variable=self.var_file).pack(side="left")
        ttk.Combobox(r2, textvariable=self.var_file_fmt, values=["TXT", "CSV"], width=5,
                     state="readonly").pack(side="left", padx=4)
        ttk.Button(r2, text="폴더 열기", command=lambda: open_folder(self.log_dir)).pack(side="left")
        ttk.Button(r2, text="지우기", width=6, command=self.clear).pack(side="right")
        ttk.Button(r2, text="화면 저장", command=self.save).pack(side="right", padx=2)
        ttk.Button(r2, text="줄 복사", command=self.copy_picked).pack(side="right", padx=2)

        tf = ttk.Frame(self)
        tf.pack(fill="both", expand=True, pady=(4, 0))
        self.txt = tk.Text(tf, wrap="char", width=40, height=18, font=MONO, state="disabled",
                           background="white", foreground="#1F2937", borderwidth=0, relief="flat",
                           highlightthickness=1, highlightbackground="#D6DCE5", highlightcolor="#2563EB",
                           padx=6, pady=4, spacing1=1, spacing3=1, cursor="arrow")
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        self.txt.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        ttk.Label(self, textvariable=self.var_stats, foreground="#455A64").pack(anchor="e", pady=(2, 0))
        # 줄 배경 태그를 먼저 만들어야 다른 태그(배지 등)가 그 위에 보인다
        for kind, (_, fg, bg) in KIND_STYLE.items():
            if bg:
                self.txt.tag_configure("l_" + kind, background=bg)
        for kind, (_, fg, bg) in KIND_STYLE.items():
            self.txt.tag_configure("t_" + kind, foreground=fg, font=MONO_BOLD)
            self.txt.tag_configure("b_" + kind, foreground="white", background=fg, font=MONO_BOLD)
        self.txt.tag_configure("delta", foreground="#8C8C8C")
        self.txt.tag_configure("src", foreground="#202124", font=MONO_BOLD)
        self.txt.tag_configure("meta", foreground="#8C8C8C")
        # 클릭한 줄 강조 (가장 위에 보이도록 마지막에 만든다) - 어떤 줄 배경과도 겹치지 않는 진한 노랑/주황
        self.txt.tag_configure("marked", background="#FFB74D", foreground="#111827")
        self.txt.tag_configure("picked", background="#FFF176", foreground="#111827",
                               borderwidth=1, relief="solid")
        self.txt.bind("<Button-1>", self._on_click)
        self.txt.bind("<Escape>", lambda e: self.clear_picks())
        for seq in ("<Control-c>", "<Command-c>"):
            try:
                self.txt.bind(seq, self._on_copy)
            except tk.TclError:
                pass
        self.var_pick = tk.StringVar(value="줄을 클릭하면 강조됩니다 · Ctrl(⌘)+클릭: 여러 줄 표시 · Esc: 강조 지우기")
        ttk.Label(self, textvariable=self.var_pick, style="Muted.TLabel").pack(anchor="w")

    # ---------------------------------------------------------- 줄 클릭 강조
    def _on_click(self, event):
        self.txt.focus_set()
        idx = self.txt.index("@%d,%d" % (event.x, event.y))
        line = int(idx.split(".")[0])
        if line >= int(self.txt.index("end-1c").split(".")[0]):
            return
        start, end = "%d.0" % line, "%d.0" % (line + 1)
        multi = event.state & 0x0004 or (sys.platform == "darwin" and event.state & 0x0008)
        if multi:
            if "marked" in self.txt.tag_names(start):
                self.txt.tag_remove("marked", start, end)
            else:
                self.txt.tag_add("marked", start, end)
        else:
            self.txt.tag_remove("picked", "1.0", "end")
            self.txt.tag_add("picked", start, end)
        text = self.txt.get(start, "%d.end" % line)
        marks = len(self.txt.tag_ranges("marked")) // 2
        self.var_pick.set("선택: %s%s" % (text[:90], "  (표시한 줄 %d개)" % marks if marks else ""))

    def clear_picks(self):
        self.txt.tag_remove("picked", "1.0", "end")
        self.txt.tag_remove("marked", "1.0", "end")
        self.var_pick.set("강조를 지웠습니다")

    def picked_text(self):
        lines = []
        for tag in ("marked", "picked"):
            r = self.txt.tag_ranges(tag)
            for i in range(0, len(r), 2):
                lines.append((self.txt.index(r[i]), self.txt.get(r[i], r[i + 1]).rstrip("\n")))
        return "\n".join(t for _, t in sorted(set(lines), key=lambda x: float(x[0])))

    def copy_picked(self):
        text = self.picked_text()
        if text:
            self.clipboard_clear()
            self.clipboard_append(text)
            self.var_pick.set("복사했습니다 (%d줄)" % (text.count("\n") + 1))

    def _on_copy(self, event=None):
        if self.txt.tag_ranges("sel"):
            return None  # 드래그로 고른 글자는 기본 복사
        self.copy_picked()
        return "break"

    # ---------------------------------------------------------- 추가
    def add(self, kind, data, src=None):
        if kind == "rx" and isinstance(data, (bytes, bytearray)) and self.var_merge.get() != "끄기" \
                and not self.can_mode:
            self._merge_rx(bytes(data), src)
            return
        self.flush()
        self._emit(time.time(), kind, data, src)

    def _merge_rx(self, data, src):
        """조각나서 들어오는 수신 데이터를 한 줄로 모은다."""
        now = time.time()
        entry = self.pending.get(src)
        if entry is None:
            entry = self.pending[src] = [now, bytearray()]
        entry[1] += data
        mode = self.var_merge.get()
        if mode.startswith("줄바꿈") or mode.startswith("CR"):
            sep = b"\n" if mode.startswith("줄바꿈") else b"\r"
            buf = entry[1]
            while True:
                i = buf.find(sep)
                if i < 0:
                    break
                line = bytes(buf[:i + 1])
                del buf[:i + 1]
                self._emit(entry[0], "rx", line, src)
                entry[0] = now
            if not buf:
                del self.pending[src]
            gap = 300  # 구분자가 끝내 안 오면 잠시 뒤 그대로 표시
        else:
            gap = int(mode.split()[0])
        if self._flush_job is not None:
            self.after_cancel(self._flush_job)
        self._flush_job = self.after(gap, self.flush)

    def flush(self):
        if self._flush_job is not None:
            self.after_cancel(self._flush_job)
            self._flush_job = None
        items = list(self.pending.items())
        self.pending = {}
        for src, (t, buf) in items:
            if buf:
                self._emit(t, "rx", bytes(buf), src)

    def _format(self, data):
        if self.can_mode:
            parts = []
            for line in bytes(data).decode("ascii", errors="replace").splitlines():
                ident, _, payload = line.partition("#")
                parts.append("ID %-8s [%d]  %s" % (ident, len(payload.split()), payload))
            if parts:
                return "  |  ".join(parts)
        view = self.var_view.get()
        if view == "HEX":
            return core.bytes_to_hex(data)
        if view == "TEXT":
            return core.bytes_to_text(data)
        return "%s  |  %s" % (core.bytes_to_text(data), core.bytes_to_hex(data))

    def _emit(self, t, kind, data, src):
        label, _, _ = KIND_STYLE.get(kind, (kind, None, None))
        size = ""
        is_bytes = isinstance(data, (bytes, bytearray))
        if is_bytes:
            body = self._format(data)
            size = "" if self.can_mode else " %dB" % len(data)
            st = self.stats.setdefault(kind, [0, 0])
            st[0] += 1
            st[1] += len(data)
            self._update_stats()
        else:
            body = str(data)
        ts = time.strftime("%H:%M:%S", time.localtime(t)) + ".%03d" % int((t % 1) * 1000)
        delta = ""
        if self.last_t is not None and self.var_delta.get():
            d = (t - self.last_t) * 1000
            if 0 <= d < 1000000:
                delta = "+%dms" % d
        self.last_t = t
        self._write_file(t, kind, label, data if is_bytes else None, body, src, delta)
        if kind in self.filters and not self.filters[kind].get():
            return
        segs = [(ts, "t_" + kind), (" %-9s" % delta, "delta"), (" %s " % label, "b_" + kind)]
        if src:
            segs.append((" [%s]" % src, "src"))
        if size:
            segs.append((size, "meta"))
        segs.append(("  " + body + "\n", None))
        if self.var_pause.get():
            self.paused_lines.append((kind, segs))
            del self.paused_lines[:-MAX_LOG_LINES]
        else:
            self._insert([(kind, segs)])

    def _insert(self, lines):
        self.txt.configure(state="normal")
        for kind, segs in lines:
            line_tag = ("l_" + kind,) if KIND_STYLE.get(kind, (0, 0, None))[2] else ()
            for text, tag in segs:
                self.txt.insert("end", text, line_tag + ((tag,) if tag else ()))
        count = int(self.txt.index("end-1c").split(".")[0])
        if count > MAX_LOG_LINES:
            self.txt.delete("1.0", "%d.0" % (count - MAX_LOG_LINES))
        self.txt.configure(state="disabled")
        if self.var_autoscroll.get():
            self.txt.see("end")

    def _on_pause(self):
        if not self.var_pause.get() and self.paused_lines:
            lines, self.paused_lines = self.paused_lines, []
            self._insert(lines)

    def _update_stats(self):
        rx = self.stats.get("rx", [0, 0])
        sent = [sum(self.stats.get(k, [0, 0])[i] for k in SENT_KINDS) for i in (0, 1)]
        self.var_stats.set("수신 %d건 %dB  ·  보냄 %d건 %dB" % (rx[0], rx[1], sent[0], sent[1]))

    def clear(self):
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.configure(state="disabled")
        self.paused_lines = []
        self.stats = {}
        self.last_t = None
        self._update_stats()

    def save(self):
        path = filedialog.asksaveasfilename(parent=self, defaultextension=".txt",
                                            filetypes=[("Text", "*.txt"), ("All", "*.*")])
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.txt.get("1.0", "end-1c"))

    # ---------------------------------------------------------- 파일 기록
    def _write_file(self, t, kind, label, data, body, src, delta):
        if not self.var_file.get():
            return
        fmt = self.var_file_fmt.get()
        day = time.strftime("%Y%m%d", time.localtime(t))
        name = "".join(c if c.isalnum() or c in "-_" else "_" for c in (self.name_getter() or "log"))
        key = (day, name, fmt)
        try:
            if key != self._file_key:
                self.close_file()
                os.makedirs(self.log_dir, exist_ok=True)
                path = os.path.join(self.log_dir, "%s_%s.%s" % (name, day, fmt.lower()))
                new = not os.path.exists(path) or os.path.getsize(path) == 0
                self._file = open(path, "a", encoding="utf-8-sig" if fmt == "CSV" and new else "utf-8", newline="")
                self._file_key = key
                if fmt == "CSV" and new:
                    csv.writer(self._file).writerow(["시간", "간격", "구분", "상대", "바이트", "TEXT", "HEX"])
            stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) + ".%03d" % int((t % 1) * 1000)
            if fmt == "CSV":
                if data is not None:
                    row = [stamp, delta, label, src or "", len(data), core.bytes_to_text(data), core.bytes_to_hex(data)]
                else:
                    row = [stamp, delta, label, src or "", "", body, ""]
                csv.writer(self._file).writerow(row)
            else:
                extra = (" [%s]" % src if src else "") + (" %dB" % len(data) if data is not None else "")
                self._file.write("%s %-9s %s%s  %s\r\n" % (stamp, delta, label, extra, body))
            self._file.flush()
        except OSError as e:
            self.close_file()
            self.var_file.set(False)
            messagebox.showerror("로그 파일", "로그 파일에 쓰지 못해 파일 기록을 껐습니다.\n%s" % e, parent=self)

    def close_file(self):
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
        self._file = None
        self._file_key = None

    # ---------------------------------------------------------- 설정
    def to_dict(self):
        return dict(view=self.var_view.get(), merge=self.var_merge.get(), delta=self.var_delta.get(),
                    autoscroll=self.var_autoscroll.get(), file=self.var_file.get(), file_fmt=self.var_file_fmt.get(),
                    filters={k: v.get() for k, v in self.filters.items()})

    def load_dict(self, d):
        for key, var in (("view", self.var_view), ("merge", self.var_merge), ("delta", self.var_delta),
                         ("autoscroll", self.var_autoscroll), ("file", self.var_file), ("file_fmt", self.var_file_fmt)):
            if key in d:
                var.set(d[key])
        for k, v in d.get("filters", {}).items():
            if k in self.filters:
                self.filters[k].set(bool(v))


# ---------------------------------------------------------------------------
# 매크로
# ---------------------------------------------------------------------------

def macro_bytes(m):
    data = core.to_bytes(m.get("data", ""), m.get("fmt", core.FMT_TEXT))
    return data if m.get("fmt") == core.FMT_CAN else checks.append(data, m.get("check", "none"))


class MacroPanel(ttk.Frame):
    def __init__(self, parent, tab):
        ttk.Frame.__init__(self, parent, padding=6)
        self.tab = tab
        self.macros = []
        self.jobs = {}  # 매크로 번호 -> 반복 예약
        self.var_name = tk.StringVar()
        self.var_data = tk.StringVar()
        self.var_fmt = tk.StringVar(value=core.FMT_TEXT)
        self.var_check = tk.StringVar(value=CHECK_LABELS[0])
        self.var_repeat = tk.StringVar(value="1000")

        ttk.Label(self, style="Muted.TLabel", wraplength=480, justify="left",
                  text="자주 보내는 데이터를 저장합니다. 더블클릭하거나 로그 아래 버튼을 누르면 보냅니다.").pack(anchor="w")
        frm, self.tree = tree_box(self, [("name", "이름", 80, "w", False), ("data", "데이터", 130, "w", True),
                                         ("fmt", "형식", 45, "center", False), ("check", "검사값", 90, "w", False),
                                         ("repeat", "반복(ms)", 60, "e", False), ("state", "상태", 60, "center", False)])
        frm.pack(fill="both", expand=True, pady=4)
        self.tree.bind("<Double-1>", lambda e: self.send_selected())
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.load_selected())

        ed = ttk.LabelFrame(self, text="매크로 편집", padding=6)
        ed.pack(fill="x")
        ed.columnconfigure(1, weight=1)
        ttk.Label(ed, text="이름").grid(row=0, column=0, sticky="w")
        ttk.Entry(ed, textvariable=self.var_name, width=14).grid(row=0, column=1, sticky="w", padx=4)
        ttk.Label(ed, text="데이터").grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Entry(ed, textvariable=self.var_data).grid(row=1, column=1, sticky="ew", padx=4, pady=(4, 0))
        ttk.Combobox(ed, textvariable=self.var_fmt, values=FMTS, width=6, state="readonly").grid(row=1, column=2, pady=(4, 0))
        r = ttk.Frame(ed)
        r.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        ttk.Label(r, text="검사값").pack(side="left")
        ttk.Combobox(r, textvariable=self.var_check, values=CHECK_LABELS, width=26, state="readonly").pack(side="left", padx=4)
        ttk.Label(r, text="반복 간격(ms)").pack(side="left", padx=(8, 0))
        ttk.Spinbox(r, from_=10, to=3600000, increment=100, textvariable=self.var_repeat, width=8).pack(side="left", padx=4)
        edit_buttons(ed, self.clear_editor, self.add, self.update_item, self.delete, self.move).grid(
            row=3, column=0, columnspan=3, sticky="ew", pady=(6, 0))

        act = ttk.Frame(self)
        act.pack(fill="x", pady=(6, 0))
        ttk.Button(act, text="▶ 보내기", style="Accent.TButton", command=self.send_selected).pack(side="left")
        ttk.Button(act, text="⟳ 반복 시작/정지", command=self.toggle_selected).pack(side="left", padx=4)
        ttk.Button(act, text="■ 모두 정지", command=self.stop_all).pack(side="left")

    def _from_editor(self):
        m = dict(name=self.var_name.get().strip() or "매크로", data=self.var_data.get(), fmt=self.var_fmt.get(),
                 check=checks.key_of(self.var_check.get()))
        try:
            m["repeat_ms"] = max(10, parse_int(self.var_repeat.get() or 1000))
            if not macro_bytes(m):
                raise ValueError("데이터를 입력하세요.")
        except ValueError as e:
            messagebox.showerror("매크로", str(e), parent=self)
            return None
        return m

    def refresh(self, select=None):
        self.tree.delete(*self.tree.get_children())
        for i, m in enumerate(self.macros):
            self.tree.insert("", "end", iid=str(i), values=(
                m["name"], m["data"], m["fmt"], checks.label_of(m.get("check", "none")),
                m.get("repeat_ms", 1000), "반복 중" if i in self.jobs else ""))
        if select is not None and 0 <= select < len(self.macros):
            self.tree.selection_set(str(select))
            self.tree.see(str(select))
        self.tab.rebuild_macro_bar()

    def load_selected(self):
        i = selected_index(self.tree)
        if i is None:
            return
        m = self.macros[i]
        self.var_name.set(m["name"])
        self.var_data.set(m["data"])
        self.var_fmt.set(m["fmt"])
        self.var_check.set(checks.label_of(m.get("check", "none")))
        self.var_repeat.set(str(m.get("repeat_ms", 1000)))

    def clear_editor(self):
        self.tree.selection_remove(*self.tree.selection())
        self.var_name.set("")
        self.var_data.set("")

    def add(self):
        m = self._from_editor()
        if m:
            self.macros.append(m)
            self.refresh(len(self.macros) - 1)

    def update_item(self):
        i = selected_index(self.tree)
        if i is None:
            return self.add()
        m = self._from_editor()
        if m:
            self.macros[i] = m
            self.refresh(i)

    def delete(self):
        i = selected_index(self.tree)
        if i is not None:
            self.stop_all()
            del self.macros[i]
            self.refresh()

    def move(self, delta):
        self.stop_all()
        j = move_item(self.macros, self.tree, delta)
        if j is not None:
            self.refresh(j)

    def send_index(self, i):
        m = self.macros[i]
        try:
            self.tab.channel.send(macro_bytes(m))
            return True
        except Exception as e:
            self.tab.log("error", "매크로 '%s' 보내기 실패: %s" % (m["name"], e))
            return False

    def send_selected(self):
        i = selected_index(self.tree)
        if i is not None:
            self.send_index(i)

    def toggle_selected(self):
        i = selected_index(self.tree)
        if i is None:
            return
        if i in self.jobs:
            self.after_cancel(self.jobs.pop(i))
        else:
            self._repeat(i)
        self.refresh(i)

    def _repeat(self, i):
        if not self.send_index(i):
            self.jobs.pop(i, None)
            self.refresh()
            return
        self.jobs[i] = self.after(self.macros[i].get("repeat_ms", 1000), lambda: self._repeat(i))

    def stop_all(self):
        for job in self.jobs.values():
            self.after_cancel(job)
        had = bool(self.jobs)
        self.jobs = {}
        if had:
            self.refresh()

    def to_list(self):
        return [dict(m) for m in self.macros]

    def load_list(self, items):
        self.macros = [dict(name=m.get("name", "매크로"), data=m.get("data", ""), fmt=m.get("fmt", core.FMT_TEXT),
                            check=m.get("check", "none"), repeat_ms=int(m.get("repeat_ms", 1000))) for m in items]
        self.refresh()


# ---------------------------------------------------------------------------
# 시나리오
# ---------------------------------------------------------------------------

KIND_LABELS = [lbl for _, lbl in sc.STEP_KINDS]


class ScenarioPanel(ttk.Frame):
    def __init__(self, parent, tab):
        ttk.Frame.__init__(self, parent, padding=6)
        self.tab = tab
        self.steps = []
        self.results = {}
        self.runner = None
        self.last_runner = None  # 보고서용 마지막 실행 결과
        self.comm = report.CommLog()
        self._job = None
        self.var_auto_report = tk.BooleanVar(value=False)
        self.var_kind = tk.StringVar(value=KIND_LABELS[0])
        self.var_data = tk.StringVar()
        self.var_fmt = tk.StringVar(value=core.FMT_TEXT)
        self.var_check = tk.StringVar(value=CHECK_LABELS[0])
        self.var_ms = tk.StringVar(value="1000")
        self.var_repeat = tk.StringVar(value="1")
        self.var_stop = tk.BooleanVar(value=True)
        self.var_summary = tk.StringVar(value="")

        frm, self.tree = tree_box(self, [("no", "번호", 38, "center", False), ("on", "사용", 38, "center", False),
                                         ("desc", "단계", 240, "w", True), ("result", "결과", 160, "w", False)])
        frm.pack(fill="both", expand=True)
        self.tree.tag_configure("pass", foreground="#1B5E20")
        self.tree.tag_configure("fail", foreground="#C62828")
        self.tree.tag_configure("run", foreground="#0D47A1")
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.load_selected())

        ed = ttk.LabelFrame(self, text="단계 편집", padding=6)
        ed.pack(fill="x", pady=(4, 0))
        ed.columnconfigure(1, weight=1)
        r0 = ttk.Frame(ed)
        r0.grid(row=0, column=0, columnspan=3, sticky="ew")
        ttk.Label(r0, text="종류").pack(side="left")
        ttk.Combobox(r0, textvariable=self.var_kind, values=KIND_LABELS, width=10, state="readonly").pack(side="left", padx=4)
        ttk.Label(r0, text="시간(ms)").pack(side="left", padx=(8, 0))
        ttk.Spinbox(r0, from_=0, to=3600000, increment=100, textvariable=self.var_ms, width=8).pack(side="left", padx=4)
        ttk.Label(r0, text="검사값").pack(side="left", padx=(8, 0))
        ttk.Combobox(r0, textvariable=self.var_check, values=CHECK_LABELS, width=14, state="readonly").pack(side="left", padx=4)
        ttk.Label(ed, text="데이터").grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Entry(ed, textvariable=self.var_data).grid(row=1, column=1, sticky="ew", padx=4, pady=(4, 0))
        ttk.Combobox(ed, textvariable=self.var_fmt, values=FMTS, width=6, state="readonly").grid(row=1, column=2, pady=(4, 0))
        ttk.Label(ed, style="Muted.TLabel", wraplength=470, justify="left", text=(
            "보내기: 데이터를 보냄 (검사값 자동 추가) / 응답 확인: 데이터가 들어오면 통과, 시간(ms)을 넘으면 실패 "
            "(데이터를 비우면 아무 응답이나 통과) / 기다리기: 시간(ms)만큼 기다림")).grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))
        edit_buttons(ed, self.clear_editor, self.add, self.update_item, self.delete, self.move).grid(
            row=3, column=0, columnspan=3, sticky="ew", pady=(6, 0))

        run = ttk.Frame(self)
        run.pack(fill="x", pady=(6, 0))
        ttk.Label(run, text="반복").pack(side="left")
        ttk.Spinbox(run, from_=0, to=1000000, textvariable=self.var_repeat, width=6).pack(side="left", padx=4)
        ttk.Label(run, text="회 (0=무한)").pack(side="left")
        ttk.Checkbutton(run, text="실패하면 멈춤", variable=self.var_stop).pack(side="left", padx=8)
        self.btn_run = ttk.Button(run, text="▶ 실행", style="Accent.TButton", command=self.start)
        self.btn_run.pack(side="left")
        ttk.Button(run, text="■ 정지", command=self.stop).pack(side="left", padx=4)
        rep = ttk.Frame(self)
        rep.pack(fill="x", pady=(4, 0))
        ttk.Label(rep, text="보고서").pack(side="left")
        ttk.Checkbutton(rep, text="끝나면 자동 저장", variable=self.var_auto_report).pack(side="left", padx=6)
        self.btn_report = ttk.Button(rep, text="보고서 저장…", command=self.save_report, state="disabled")
        self.btn_report.pack(side="left")
        ttk.Button(rep, text="보고서 폴더", command=self.open_report_dir).pack(side="left", padx=4)
        self.lbl_summary = ttk.Label(self, textvariable=self.var_summary)
        self.lbl_summary.pack(anchor="w", pady=(4, 0))

    # ---------------------------------------------------------- 편집
    def _from_editor(self):
        kind = sc.STEP_KINDS[KIND_LABELS.index(self.var_kind.get())][0]
        step = sc.new_step(kind, self.var_data.get(), self.var_fmt.get(), checks.key_of(self.var_check.get()))
        try:
            step["ms"] = max(0, parse_int(self.var_ms.get() or 0))
            if kind == sc.STEP_SEND and not sc.step_bytes(step):
                raise ValueError("보낼 데이터를 입력하세요.")
            if kind == sc.STEP_EXPECT:
                sc.step_bytes(step)
        except ValueError as e:
            messagebox.showerror("시나리오", str(e), parent=self)
            return None
        return step

    def refresh(self, select=None):
        self.tree.delete(*self.tree.get_children())
        for i, s in enumerate(self.steps):
            status, msg = self.results.get(i, ("", ""))
            tag = {sc.PASS: "pass", sc.FAIL: "fail", sc.RUN: "run"}.get(status, "")
            self.tree.insert("", "end", iid=str(i), tags=(tag,), values=(
                i + 1, CHECK_ON if s.get("enabled", True) else CHECK_OFF, sc.describe(s),
                ("%s %s" % (status, msg)).strip()))
        if select is not None and 0 <= select < len(self.steps):
            self.tree.selection_set(str(select))
            self.tree.see(str(select))

    def _on_click(self, event):
        if self.tree.identify_region(event.x, event.y) == "cell" and self.tree.identify_column(event.x) == "#2":
            row = self.tree.identify_row(event.y)
            if row:
                s = self.steps[int(row)]
                s["enabled"] = not s.get("enabled", True)
                self.refresh(int(row))
                return "break"

    def load_selected(self):
        i = selected_index(self.tree)
        if i is None:
            return
        s = self.steps[i]
        self.var_kind.set(dict(sc.STEP_KINDS)[s["kind"]])
        self.var_data.set(s.get("data", ""))
        self.var_fmt.set(s.get("fmt", core.FMT_TEXT))
        self.var_check.set(checks.label_of(s.get("check", "none")))
        self.var_ms.set(str(s.get("ms", 1000)))

    def clear_editor(self):
        self.tree.selection_remove(*self.tree.selection())
        self.var_data.set("")

    def add(self):
        s = self._from_editor()
        if s:
            self.steps.append(s)
            self.refresh(len(self.steps) - 1)

    def update_item(self):
        i = selected_index(self.tree)
        if i is None:
            return self.add()
        s = self._from_editor()
        if s:
            s["enabled"] = self.steps[i].get("enabled", True)
            self.steps[i] = s
            self.refresh(i)

    def delete(self):
        i = selected_index(self.tree)
        if i is not None and not self.is_running():
            del self.steps[i]
            self.results = {}
            self.refresh()

    def move(self, delta):
        if self.is_running():
            return
        j = move_item(self.steps, self.tree, delta)
        if j is not None:
            self.results = {}
            self.refresh(j)

    # ---------------------------------------------------------- 실행
    def is_running(self):
        return self.runner is not None and self.runner.running

    def start(self):
        if self.is_running():
            return
        if not self.tab.channel.is_open:
            messagebox.showerror("시나리오", "먼저 연결을 여세요. (☑ 사용)", parent=self)
            return
        try:
            repeat = max(0, parse_int(self.var_repeat.get() or 1))
        except ValueError:
            repeat = 1
        self.results = {}
        self.comm = report.CommLog()
        self.runner = sc.ScenarioRunner(self.steps, repeat, self.var_stop.get())
        self.tab.log("scenario", "시작 (반복 %s)" % (repeat or "무한"))
        self._handle(self.runner.start(time.time()))
        self._schedule()

    def stop(self, reason=sc.END_USER):
        if self.is_running():
            self.runner.stop(reason, time.time())
            self.tab.log("scenario", "정지: %s" % reason)
            self._finish()

    def feed(self, data):
        if self.is_running():
            self.runner.feed(data)

    def capture(self, kind, data, src):
        """실행 중 주고받은 데이터를 보고서용으로 모은다."""
        if self.is_running():
            self.comm.add(self.tab.var_name.get(), kind, data, src)

    def _schedule(self):
        if self.is_running():
            self._job = self.after(20, self._tick)
        else:
            self._finish()

    def _tick(self):
        self._job = None
        if not self.is_running():
            return
        self._handle(self.runner.tick(time.time(), self.tab.channel.send))
        self._schedule()

    def _handle(self, events):
        for idx, status, msg in events:
            if idx is None:
                self.tab.log("scenario", msg)
                continue
            self.results[idx] = (status, msg)
            if self.tree.exists(str(idx)):
                tag = {sc.PASS: "pass", sc.FAIL: "fail", sc.RUN: "run"}.get(status, "")
                self.tree.item(str(idx), tags=(tag,), values=(
                    idx + 1, CHECK_ON if self.steps[idx].get("enabled", True) else CHECK_OFF,
                    sc.describe(self.steps[idx]), ("%s %s" % (status, msg)).strip()))
            if status != sc.RUN:
                self.tab.log("scenario", "%d단계 %s: %s" % (idx + 1, status, msg))
        r = self.runner
        if r is not None:
            self.var_summary.set("%d회차 진행 중 · 통과 %d · 실패 %d" % (r.loop, r.passed, r.failed))

    def _finish(self):
        if self._job is not None:
            self.after_cancel(self._job)
            self._job = None
        r = self.runner
        if r is None:
            return
        text = "%s: 통과 %d · 실패 %d" % (r.end_reason or sc.END_DONE, r.passed, r.failed)
        self.var_summary.set(text)
        self.lbl_summary.configure(foreground="#1B5E20" if r.ok() else "#C62828")
        self.tab.log("scenario", text)
        self.runner = None
        if r.started_at is not None:
            self.last_runner = r
            self.btn_report.state(["!disabled"])
            if self.var_auto_report.get():
                self.after(300, self._auto_report)  # 마지막 응답이 기록에 들어오도록 잠깐 뒤에 저장

    # ---------------------------------------------------------- 보고서
    def report_dir(self):
        return os.path.join(os.path.dirname(self.tab.app.log_dir), "reports")

    def _report_name(self):
        name = "".join(ch for ch in self.tab.var_name.get() if ch not in '\\/:*?"<>|').strip() or "시험"
        return report.default_name("시험보고서_" + name)

    def _write_report(self, path):
        import autotest
        r = self.last_runner
        result = report.tab_result(self.tab.var_name.get(), autotest.conn_text(self.tab.to_config()), r)
        report.save_excel(path, [result], self.comm, source="화면에서 실행 (%s)" % self.tab.var_name.get())

    def _auto_report(self):
        path = os.path.join(self.report_dir(), self._report_name())
        try:
            self._write_report(path)
        except Exception as e:
            self.tab.log("error", "보고서 자동 저장 실패: %s" % e)
            return
        self.tab.log("info", "보고서 저장: %s" % path)

    def save_report(self):
        if self.last_runner is None:
            return
        folder = self.report_dir()
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError:
            folder = None
        path = filedialog.asksaveasfilename(parent=self, defaultextension=".xlsx", initialdir=folder,
                                            initialfile=self._report_name(), filetypes=[("엑셀 파일", "*.xlsx")])
        if not path:
            return
        try:
            self._write_report(path)
        except Exception as e:
            messagebox.showerror("보고서", str(e), parent=self)
            return
        self.tab.log("info", "보고서 저장: %s" % path)

    def open_report_dir(self):
        folder = self.report_dir()
        try:
            os.makedirs(folder, exist_ok=True)
            open_folder(folder)
        except OSError as e:
            messagebox.showerror("보고서", str(e), parent=self)

    def to_dict(self):
        return dict(steps=[dict(s) for s in self.steps], repeat=self.var_repeat.get(), stop_on_fail=self.var_stop.get(),
                    auto_report=self.var_auto_report.get())

    def load_dict(self, d):
        self.steps = [sc.new_step(s.get("kind", sc.STEP_SEND), s.get("data", ""), s.get("fmt", core.FMT_TEXT),
                                  s.get("check", "none"), s.get("ms", 1000), s.get("enabled", True))
                      for s in d.get("steps", [])]
        self.var_repeat.set(str(d.get("repeat", "1")))
        self.var_stop.set(bool(d.get("stop_on_fail", True)))
        self.var_auto_report.set(bool(d.get("auto_report", False)))
        self.refresh()


# ---------------------------------------------------------------------------
# 오류 주입
# ---------------------------------------------------------------------------

EXC_LABELS = ["%02d %s" % (k, v) for k, v in sorted(mb.EXC_NAMES.items())]


class FaultPanel(ttk.Frame):
    PCT_FIELDS = [("drop_pct", "응답 안 함", "응답을 보내지 않습니다 (타임아웃 시험)"),
                  ("corrupt_pct", "데이터 변조", "마지막 바이트를 바꿔 CRC/체크섬 오류를 만듭니다"),
                  ("truncate_pct", "잘라 보내기", "응답의 앞부분만 보냅니다"),
                  ("duplicate_pct", "두 번 보내기", "같은 응답을 한 번 더 보냅니다"),
                  ("noise_pct", "쓰레기 바이트", "응답 앞에 무작위 바이트를 붙입니다"),
                  ("exception_pct", "Modbus 예외", "Modbus 슬레이브일 때 예외 응답을 보냅니다")]

    def __init__(self, parent, tab):
        ttk.Frame.__init__(self, parent, padding=8)
        self.tab = tab
        self.faults = tab.channel.faults
        self.var_enabled = tk.BooleanVar(value=False)
        self.vars = {k: tk.StringVar(value="0") for k, _, _ in self.PCT_FIELDS}
        self.var_dmin = tk.StringVar(value="0")
        self.var_dmax = tk.StringVar(value="0")
        self.var_exc = tk.StringVar(value=EXC_LABELS[3])

        ttk.Checkbutton(self, text="오류 주입 사용", variable=self.var_enabled).pack(anchor="w")
        ttk.Label(self, style="Muted.TLabel", justify="left", wraplength=480, text=(
            "자동 응답(규칙, Modbus 슬레이브)을 일부러 망가뜨려서 상대 프로그램이 오류를 잘 처리하는지 시험합니다. "
            "각 항목은 응답마다 정해진 확률(%)로 일어납니다. 켜져 있으면 탭 이름 앞에 ⚠ 표시가 붙습니다.")).pack(anchor="w", pady=(4, 8))
        g = ttk.Frame(self)
        g.pack(fill="x")
        for row, (key, label, desc) in enumerate(self.PCT_FIELDS):
            ttk.Label(g, text=label).grid(row=row, column=0, sticky="w", pady=2)
            ttk.Spinbox(g, from_=0, to=100, increment=5, textvariable=self.vars[key], width=5).grid(row=row, column=1, padx=4)
            ttk.Label(g, text="%").grid(row=row, column=2, sticky="w")
            ttk.Label(g, text=desc, style="Muted.TLabel", wraplength=330, justify="left").grid(row=row, column=3, sticky="w", padx=8)
        r = len(self.PCT_FIELDS)
        ttk.Label(g, text="예외 코드").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Combobox(g, textvariable=self.var_exc, values=EXC_LABELS, width=26, state="readonly").grid(
            row=r, column=1, columnspan=3, sticky="w", padx=4)
        ttk.Label(g, text="추가 지연").grid(row=r + 1, column=0, sticky="w", pady=2)
        d = ttk.Frame(g)
        d.grid(row=r + 1, column=1, columnspan=3, sticky="w", padx=4)
        ttk.Spinbox(d, from_=0, to=60000, increment=50, textvariable=self.var_dmin, width=7).pack(side="left")
        ttk.Label(d, text="~").pack(side="left", padx=2)
        ttk.Spinbox(d, from_=0, to=60000, increment=50, textvariable=self.var_dmax, width=7).pack(side="left")
        ttk.Label(d, text="ms 사이 무작위로 늦게 응답", style="Muted.TLabel").pack(side="left", padx=8)

        for var in [self.var_enabled, self.var_dmin, self.var_dmax, self.var_exc] + list(self.vars.values()):
            var.trace_add("write", lambda *a: self.apply())

    def apply(self):
        f = self.faults
        f.enabled = self.var_enabled.get()
        for key, var in self.vars.items():
            try:
                setattr(f, key, max(0.0, min(100.0, float(var.get() or 0))))
            except ValueError:
                pass
        try:
            f.delay_min_ms = max(0, parse_int(self.var_dmin.get() or 0))
            f.delay_max_ms = max(0, parse_int(self.var_dmax.get() or 0))
        except ValueError:
            pass
        f.exception_code = int(self.var_exc.get()[:2])
        self.tab.update_title()

    def to_dict(self):
        return self.faults.to_dict()

    def load_dict(self, d):
        self.faults.load_dict(d)
        f = self.faults
        for key, var in self.vars.items():
            var.set("%g" % getattr(f, key))
        self.var_dmin.set(str(f.delay_min_ms))
        self.var_dmax.set(str(f.delay_max_ms))
        for lbl in EXC_LABELS:
            if int(lbl[:2]) == f.exception_code:
                self.var_exc.set(lbl)
        self.var_enabled.set(f.enabled)


# ---------------------------------------------------------------------------
# Modbus 값 자동 변화
# ---------------------------------------------------------------------------

AREA_SHORT = {mb.AREA_COIL: "코일", mb.AREA_DI: "입력 접점", mb.AREA_IR: "입력 레지스터", mb.AREA_HR: "보유 레지스터"}
MODE_LABELS = [lbl for _, lbl in mb.SIM_MODES]


class SimPanel(ttk.Frame):
    def __init__(self, parent, tab):
        ttk.Frame.__init__(self, parent, padding=6)
        self.tab = tab
        self.entries = []
        self.var_on = tk.BooleanVar(value=True)
        self.var_area = tk.StringVar(value=AREA_SHORT[mb.AREA_HR])
        self.var_addr = tk.StringVar(value="0")
        self.var_mode = tk.StringVar(value=MODE_LABELS[0])
        self.var_min = tk.StringVar(value="0")
        self.var_max = tk.StringVar(value="100")
        self.var_step = tk.StringVar(value="1")
        self.var_interval = tk.StringVar(value="1000")

        ttk.Label(self, style="Muted.TLabel", text="정한 간격마다 값이 저절로 바뀝니다. 실제 센서처럼 움직이는 값을 시험할 때 씁니다.").pack(anchor="w")
        frm, self.tree = tree_box(self, [("on", "사용", 40, "center", False), ("area", "영역", 80, "w", False),
                                         ("addr", "주소", 50, "e", False), ("mode", "방식", 85, "w", False),
                                         ("min", "최소", 55, "e", False), ("max", "최대", 55, "e", False),
                                         ("step", "증감", 45, "e", False), ("iv", "간격(ms)", 65, "e", False)], height=6)
        frm.pack(fill="both", expand=True, pady=4)
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.load_selected())

        ed = ttk.Frame(self)
        ed.pack(fill="x")
        r1 = ttk.Frame(ed)
        r1.pack(fill="x")
        ttk.Combobox(r1, textvariable=self.var_area, values=list(AREA_SHORT.values()), width=12,
                     state="readonly").pack(side="left")
        ttk.Label(r1, text="주소").pack(side="left", padx=(6, 0))
        ttk.Entry(r1, textvariable=self.var_addr, width=6).pack(side="left", padx=2)
        ttk.Combobox(r1, textvariable=self.var_mode, values=MODE_LABELS, width=14, state="readonly").pack(side="left", padx=6)
        ttk.Checkbutton(r1, text="사용", variable=self.var_on).pack(side="left")
        r2 = ttk.Frame(ed)
        r2.pack(fill="x", pady=(4, 0))
        for label, var, w in (("최소", self.var_min, 7), ("최대", self.var_max, 7), ("증감", self.var_step, 5),
                              ("간격(ms)", self.var_interval, 7)):
            ttk.Label(r2, text=label).pack(side="left", padx=(0, 2))
            ttk.Entry(r2, textvariable=var, width=w).pack(side="left", padx=(0, 8))
        ttk.Label(ed, style="Muted.TLabel",
                  text="증감: 증가/감소는 한 번에 바뀌는 양, 사인파는 한 번에 도는 각도(도). 음수 범위도 됩니다.").pack(anchor="w", pady=(2, 0))
        edit_buttons(ed, lambda: self.tree.selection_remove(*self.tree.selection()), self.add, self.update_item,
                     self.delete, self.move).pack(fill="x", pady=(4, 0))

    def _from_editor(self):
        area = [k for k, v in AREA_SHORT.items() if v == self.var_area.get()][0]
        mode = mb.SIM_MODES[MODE_LABELS.index(self.var_mode.get())][0]
        try:
            return mb.SimEntry(self.var_on.get(), area, parse_int(self.var_addr.get()), mode,
                               parse_int(self.var_min.get()), parse_int(self.var_max.get()),
                               float(self.var_step.get() or 1), parse_int(self.var_interval.get() or 1000))
        except ValueError as e:
            messagebox.showerror("값 자동 변화", "숫자를 확인하세요: %s" % e, parent=self)
            return None

    def refresh(self, select=None):
        self.tree.delete(*self.tree.get_children())
        modes = dict(mb.SIM_MODES)
        for i, e in enumerate(self.entries):
            self.tree.insert("", "end", iid=str(i), values=(
                CHECK_ON if e.enabled else CHECK_OFF, AREA_SHORT[e.area], e.addr, modes.get(e.mode, e.mode),
                e.vmin, e.vmax, "%g" % e.step, e.interval_ms))
        if select is not None and 0 <= select < len(self.entries):
            self.tree.selection_set(str(select))

    def _on_click(self, event):
        if self.tree.identify_region(event.x, event.y) == "cell" and self.tree.identify_column(event.x) == "#1":
            row = self.tree.identify_row(event.y)
            if row:
                e = self.entries[int(row)]
                e.enabled = not e.enabled
                self.refresh(int(row))
                return "break"

    def load_selected(self):
        i = selected_index(self.tree)
        if i is None:
            return
        e = self.entries[i]
        self.var_on.set(e.enabled)
        self.var_area.set(AREA_SHORT[e.area])
        self.var_addr.set(str(e.addr))
        self.var_mode.set(dict(mb.SIM_MODES)[e.mode])
        self.var_min.set(str(e.vmin))
        self.var_max.set(str(e.vmax))
        self.var_step.set("%g" % e.step)
        self.var_interval.set(str(e.interval_ms))

    def add(self):
        e = self._from_editor()
        if e:
            self.entries.append(e)
            self.refresh(len(self.entries) - 1)

    def update_item(self):
        i = selected_index(self.tree)
        if i is None:
            return self.add()
        e = self._from_editor()
        if e:
            self.entries[i] = e
            self.refresh(i)

    def delete(self):
        i = selected_index(self.tree)
        if i is not None:
            del self.entries[i]
            self.refresh()

    def move(self, delta):
        j = move_item(self.entries, self.tree, delta)
        if j is not None:
            self.refresh(j)

    def tick(self, now):
        return mb.sim_tick(self.tab.store, self.entries, now)

    def to_list(self):
        return [e.to_dict() for e in self.entries]

    def load_list(self, items):
        self.entries = [mb.SimEntry.from_dict(d) for d in items]
        self.refresh()


# ---------------------------------------------------------------------------
# Modbus 폴링 목록
# ---------------------------------------------------------------------------

POLL_FC_LABELS = [mb.FC_NAMES[k] for k in pl.READ_FCS]
DTYPE_LABELS = [lbl for _, lbl in pl.DTYPES]
ORDER_LABELS = [lbl for _, lbl in pl.ORDERS]


class PollPanel(ttk.Frame):
    def __init__(self, parent, tab):
        ttk.Frame.__init__(self, parent, padding=6)
        self.tab = tab
        self.poller = tab.poller
        self.graph_window = None
        self.var_name = tk.StringVar(value="온도")
        self.var_unit = tk.StringVar(value="1")
        self.var_fc = tk.StringVar(value=mb.FC_NAMES[3])
        self.var_addr = tk.StringVar(value="0")
        self.var_dtype = tk.StringVar(value="UINT16")
        self.var_order = tk.StringVar(value=ORDER_LABELS[0])
        self.var_scale = tk.StringVar(value="1")
        self.var_offset = tk.StringVar(value="0")
        self.var_interval = tk.StringVar(value="1000")
        self.var_on = tk.BooleanVar(value=True)
        self.var_graph = tk.BooleanVar(value=True)
        self.var_summary = tk.StringVar(value="")

        bar = ttk.Frame(self)
        bar.pack(fill="x")
        self.btn_run = ttk.Button(bar, text="▶ 폴링 시작", style="Accent.TButton", command=self.toggle_run)
        self.btn_run.pack(side="left")
        ttk.Button(bar, text="그래프 보기", command=self.open_graph).pack(side="left", padx=6)
        ttk.Label(bar, textvariable=self.var_summary, style="Muted.TLabel").pack(side="left", padx=6)

        frm, self.tree = tree_box(self, [("on", "사용", 34, "center", False), ("gr", "그래프", 44, "center", False),
                                         ("name", "이름", 70, "w", True), ("dev", "장치", 34, "center", False),
                                         ("addr", "주소", 80, "w", False), ("type", "형식", 58, "w", False),
                                         ("value", "값", 70, "e", False), ("status", "상태", 80, "w", False)], height=9)
        frm.pack(fill="both", expand=True, pady=4)
        self.tree.tag_configure("ok", foreground="#1B5E20")
        self.tree.tag_configure("fail", foreground="#C62828")
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.load_selected())

        ed = ttk.LabelFrame(self, text="항목 편집", padding=6)
        ed.pack(fill="x")
        r1 = ttk.Frame(ed)
        r1.pack(fill="x")
        ttk.Label(r1, text="이름").pack(side="left")
        ttk.Entry(r1, textvariable=self.var_name, width=8).pack(side="left", padx=(2, 6))
        ttk.Label(r1, text="장치").pack(side="left")
        ttk.Spinbox(r1, from_=0, to=247, textvariable=self.var_unit, width=4).pack(side="left", padx=(2, 8))
        ttk.Combobox(r1, textvariable=self.var_fc, values=POLL_FC_LABELS, width=16, state="readonly").pack(side="left")
        ttk.Label(r1, text="주소").pack(side="left", padx=(8, 0))
        ttk.Entry(r1, textvariable=self.var_addr, width=6).pack(side="left", padx=2)
        r2 = ttk.Frame(ed)
        r2.pack(fill="x", pady=(4, 0))
        ttk.Label(r2, text="형식").pack(side="left")
        ttk.Combobox(r2, textvariable=self.var_dtype, values=DTYPE_LABELS, width=8, state="readonly").pack(side="left", padx=(2, 4))
        ttk.Combobox(r2, textvariable=self.var_order, values=ORDER_LABELS, width=14, state="readonly").pack(side="left", padx=(0, 6))
        ttk.Label(r2, text="배율").pack(side="left")
        ttk.Entry(r2, textvariable=self.var_scale, width=6).pack(side="left", padx=(2, 6))
        ttk.Label(r2, text="오프셋").pack(side="left")
        ttk.Entry(r2, textvariable=self.var_offset, width=6).pack(side="left", padx=2)
        r3 = ttk.Frame(ed)
        r3.pack(fill="x", pady=(4, 0))
        ttk.Label(r3, text="간격(ms)").pack(side="left")
        ttk.Spinbox(r3, from_=20, to=3600000, increment=100, textvariable=self.var_interval, width=7).pack(side="left", padx=(2, 8))
        ttk.Checkbutton(r3, text="사용", variable=self.var_on).pack(side="left")
        ttk.Checkbutton(r3, text="그래프에 표시", variable=self.var_graph).pack(side="left", padx=6)
        ttk.Label(ed, style="Muted.TLabel", wraplength=480, justify="left", text=(
            "값 = 읽은 값 × 배율 + 오프셋.  32비트 형식(UINT32/INT32/FLOAT32)은 레지스터 2개를 읽으며, "
            "장비에 따라 순서(ABCD/CDAB…)를 맞춰야 합니다.")).pack(anchor="w", pady=(4, 0))
        edit_buttons(ed, lambda: self.tree.selection_remove(*self.tree.selection()), self.add, self.update_item,
                     self.delete, self.move).pack(fill="x", pady=(4, 0))
        self.update_summary()

    # ---------------------------------------------------------- 편집
    def _from_editor(self):
        try:
            it = pl.PollItem(self.var_on.get(), self.var_name.get().strip(), parse_int(self.var_unit.get()),
                             int(self.var_fc.get()[:2]), parse_int(self.var_addr.get()),
                             pl.DTYPES[DTYPE_LABELS.index(self.var_dtype.get())][0],
                             pl.ORDERS[ORDER_LABELS.index(self.var_order.get())][0],
                             float(self.var_scale.get() or 1), float(self.var_offset.get() or 0),
                             parse_int(self.var_interval.get() or 1000), self.var_graph.get())
            it.validate()
            return it
        except ValueError as e:
            messagebox.showerror("폴링", str(e), parent=self)
            return None

    def refresh(self, select=None):
        self.tree.delete(*self.tree.get_children())
        types = dict(pl.DTYPES)
        for i, it in enumerate(self.poller.items):
            self.tree.insert("", "end", iid=str(i), values=(
                CHECK_ON if it.enabled else CHECK_OFF, CHECK_ON if it.graph else CHECK_OFF, it.name, it.unit,
                it.describe_addr(), types.get(it.dtype, it.dtype), pl.format_value(it.value), it.status))
        if select is not None and 0 <= select < len(self.poller.items):
            self.tree.selection_set(str(select))
            self.tree.see(str(select))

    def refresh_values(self):
        """값과 상태 칸만 바꾼다 (자주 불림)"""
        for i, it in enumerate(self.poller.items):
            iid = str(i)
            if self.tree.exists(iid):
                self.tree.set(iid, "value", pl.format_value(it.value))
                self.tree.set(iid, "status", it.status)
                self.tree.item(iid, tags=("ok",) if it.ok else ("fail",) if it.ok is False else ())
        self.update_summary()

    def update_summary(self):
        p = self.poller
        state = "폴링 중" if p.running else "멈춤"
        self.var_summary.set("%s · 항목 %d · 요청 %d · 타임아웃 %d" % (state, len(p.items), p.requests, p.timeouts))
        self.btn_run.configure(text="■ 폴링 정지" if p.running else "▶ 폴링 시작")

    def _on_click(self, event):
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        col = self.tree.identify_column(event.x)
        row = self.tree.identify_row(event.y)
        if row and col in ("#1", "#2"):
            it = self.poller.items[int(row)]
            if col == "#1":
                it.enabled = not it.enabled
            else:
                it.graph = not it.graph
            self.refresh(int(row))
            return "break"

    def load_selected(self):
        i = selected_index(self.tree)
        if i is None:
            return
        it = self.poller.items[i]
        self.var_on.set(it.enabled)
        self.var_graph.set(it.graph)
        self.var_name.set(it.name)
        self.var_unit.set(str(it.unit))
        self.var_fc.set(mb.FC_NAMES[it.fc])
        self.var_addr.set(str(it.addr))
        self.var_dtype.set(dict(pl.DTYPES)[it.dtype])
        self.var_order.set(dict(pl.ORDERS)[it.order])
        self.var_scale.set("%g" % it.scale)
        self.var_offset.set("%g" % it.offset)
        self.var_interval.set(str(it.interval_ms))

    def add(self):
        it = self._from_editor()
        if it:
            self.poller.items.append(it)
            self.refresh(len(self.poller.items) - 1)

    def update_item(self):
        i = selected_index(self.tree)
        if i is None:
            return self.add()
        it = self._from_editor()
        if it:
            old = self.poller.items[i]
            if (old.fc, old.addr, old.dtype, old.order, old.unit) == (it.fc, it.addr, it.dtype, it.order, it.unit):
                it.history = old.history  # 같은 값을 읽으면 그래프 기록 유지
            self.poller.items[i] = it
            self.refresh(i)

    def delete(self):
        i = selected_index(self.tree)
        if i is not None:
            del self.poller.items[i]
            self.refresh()

    def move(self, delta):
        j = move_item(self.poller.items, self.tree, delta)
        if j is not None:
            self.refresh(j)

    # ---------------------------------------------------------- 실행
    def toggle_run(self):
        p = self.poller
        if not p.running and not self.tab.channel.is_open:
            messagebox.showerror("폴링", "먼저 연결을 여세요. (☑ 사용)", parent=self)
            return
        if not p.running and not p.items:
            messagebox.showerror("폴링", "읽을 항목을 먼저 추가하세요.", parent=self)
            return
        p.running = not p.running
        if p.running:
            for it in p.items:
                it.next_due = 0.0
            self.tab.log("info", "폴링 시작 (항목 %d개)" % len(p.items))
        else:
            self.tab.log("info", "폴링 정지")
        self.update_summary()

    def open_graph(self):
        if self.graph_window is not None and self.graph_window.winfo_exists():
            self.graph_window.lift()
            return
        self.graph_window = GraphWindow(self, self.poller, lambda: self.tab.var_name.get())

    def to_dict(self):
        return dict(items=[it.to_dict() for it in self.poller.items], running=self.poller.running)

    def load_dict(self, d):
        self.poller.items[:] = [pl.PollItem.from_dict(x) for x in d.get("items", [])]
        self.poller.running = bool(d.get("running", False))
        self.refresh()
        self.update_summary()


# ---------------------------------------------------------------------------
# 실시간 그래프 (필요할 때만 여는 창)
# ---------------------------------------------------------------------------

GRAPH_COLORS = ["#2563EB", "#DC2626", "#16A34A", "#D97706", "#7C3AED", "#0891B2", "#DB2777", "#4B5563"]
SPANS = [("10초", 10), ("30초", 30), ("1분", 60), ("5분", 300), ("10분", 600), ("30분", 1800)]


class GraphWindow(tk.Toplevel):
    def __init__(self, master, poller, name_getter):
        tk.Toplevel.__init__(self, master)
        self.poller = poller
        self.title("실시간 그래프 - %s" % name_getter())
        self.geometry("820x480")
        self.minsize(520, 320)
        self.var_span = tk.StringVar(value="1분")
        self.var_pause = tk.BooleanVar(value=False)
        self.var_auto = tk.BooleanVar(value=True)
        self.var_ymin = tk.StringVar(value="0")
        self.var_ymax = tk.StringVar(value="100")
        self._job = None
        self._frozen_now = None

        bar = ttk.Frame(self, padding=(8, 8, 8, 0))
        bar.pack(fill="x")
        ttk.Label(bar, text="시간 범위").pack(side="left")
        ttk.Combobox(bar, textvariable=self.var_span, values=[n for n, _ in SPANS], width=6,
                     state="readonly").pack(side="left", padx=(4, 12))
        ttk.Checkbutton(bar, text="일시정지", variable=self.var_pause).pack(side="left")
        ttk.Checkbutton(bar, text="Y축 자동", variable=self.var_auto).pack(side="left", padx=(12, 4))
        ttk.Entry(bar, textvariable=self.var_ymin, width=7).pack(side="left")
        ttk.Label(bar, text="~").pack(side="left", padx=2)
        ttk.Entry(bar, textvariable=self.var_ymax, width=7).pack(side="left")
        ttk.Button(bar, text="CSV 저장", command=self.save_csv).pack(side="right")
        ttk.Button(bar, text="기록 지우기", command=self.clear).pack(side="right", padx=4)

        self.canvas = tk.Canvas(self, background="white", highlightthickness=1, highlightbackground="#D0D7E2")
        self.canvas.pack(fill="both", expand=True, padx=8, pady=8)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._tick()

    def close(self):
        if self._job is not None:
            self.after_cancel(self._job)
        self.destroy()

    def _tick(self):
        self.draw()
        self._job = self.after(300, self._tick)

    def clear(self):
        for it in self.poller.items:
            it.history.clear()
        self.draw()

    def draw(self):
        c = self.canvas
        if self.var_pause.get():
            if self._frozen_now is None:
                self._frozen_now = time.time()
            now = self._frozen_now
        else:
            self._frozen_now = None
            now = time.time()
        span = dict(SPANS).get(self.var_span.get(), 60)
        w, h = max(c.winfo_width(), 200), max(c.winfo_height(), 150)
        left, right, top, bottom = 64, 16, 34, 28
        pw, ph = w - left - right, h - top - bottom
        c.delete("all")
        items = [it for it in self.poller.items if it.graph]
        series = []
        for i, it in enumerate(items):
            pts = [(t, v) for t, v in it.history if now - span <= t <= now]
            series.append((it, GRAPH_COLORS[i % len(GRAPH_COLORS)], pts))
        values = [v for _, _, pts in series for _, v in pts]
        if self.var_auto.get() or not values:
            lo, hi = (min(values), max(values)) if values else (0.0, 1.0)
            if lo == hi:
                lo, hi = lo - 1, hi + 1
            pad = (hi - lo) * 0.08
            lo, hi = lo - pad, hi + pad
        else:
            try:
                lo, hi = float(self.var_ymin.get()), float(self.var_ymax.get())
            except ValueError:
                lo, hi = 0.0, 1.0
            if lo == hi:
                hi = lo + 1
        # 눈금
        for k in range(6):
            y = top + ph * k / 5.0
            val = hi - (hi - lo) * k / 5.0
            c.create_line(left, y, left + pw, y, fill="#EEF1F5")
            c.create_text(left - 6, y, text=pl.format_value(round(val, 4)), anchor="e", fill="#6B7280", font=("", 9))
        for k in range(7):
            x = left + pw * k / 6.0
            c.create_line(x, top, x, top + ph, fill="#F3F4F6")
            sec = span - span * k / 6.0
            c.create_text(x, top + ph + 12, text="-%gs" % round(sec, 1) if sec else "지금", fill="#6B7280", font=("", 9))
        c.create_rectangle(left, top, left + pw, top + ph, outline="#CBD5E1")
        # 선
        for it, color, pts in series:
            if len(pts) > pw:  # 화면 폭보다 많은 점은 솎아낸다
                step = len(pts) / float(pw)
                pts = [pts[int(i * step)] for i in range(int(pw))] + [pts[-1]]
            coords = []
            for t, v in pts:
                coords += [left + pw * (t - (now - span)) / span, top + ph * (hi - v) / (hi - lo)]
            if len(coords) >= 4:
                c.create_line(*coords, fill=color, width=2)
            if coords:
                c.create_oval(coords[-2] - 3, coords[-1] - 3, coords[-2] + 3, coords[-1] + 3, fill=color, outline=color)
        # 범례
        x = left
        for it, color, pts in series:
            label = "%s: %s" % (it.name, pl.format_value(it.value))
            c.create_rectangle(x, 12, x + 12, 22, fill=color, outline=color)
            tid = c.create_text(x + 16, 17, text=label, anchor="w", fill="#111827", font=("", 10))
            bbox = c.bbox(tid)
            x = (bbox[2] if bbox else x + 100) + 16
        if not items:
            c.create_text(w / 2, h / 2, text="폴링 목록에서 '그래프' 칸을 ☑ 로 바꾼 항목이 여기에 그려집니다.",
                          fill="#6B7280", font=("", 11))
        elif not values:
            c.create_text(w / 2, h / 2, text="아직 읽은 값이 없습니다. 폴링을 시작하세요.", fill="#6B7280", font=("", 11))

    def save_csv(self):
        path = filedialog.asksaveasfilename(parent=self, defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if not path:
            return
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            wr = csv.writer(f)
            wr.writerow(["시간", "이름", "값"])
            rows = [(t, it.name, v) for it in self.poller.items for t, v in it.history]
            for t, name, v in sorted(rows):
                wr.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) + ".%03d" % int((t % 1) * 1000),
                             name, pl.format_value(v)])
