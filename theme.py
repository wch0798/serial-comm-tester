"""
화면 디자인 (모든 OS에서 같은 모양)

ttk 'clam' 테마를 바탕으로 색, 글꼴, 여백을 정리한다.
- 상단: 어두운 제목 줄 + 밝은 도구 줄
- 주요 버튼(보내기, 실행 등): 파란 강조 버튼 (Accent.TButton)
- 표/탭/입력칸: 흰 바탕, 옅은 테두리
"""

import sys
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

C = dict(
    bg="#F3F5F9", panel="#FFFFFF", border="#D6DCE5", text="#1F2937", muted="#6B7280",
    accent="#2563EB", accent_hover="#1D4ED8", accent_press="#1E40AF", accent_light="#DBEAFE",
    header="#0F172A", header_text="#F8FAFC", header_muted="#94A3B8",
    toolbar="#FFFFFF", heading="#EEF2F7", status="#E8ECF2",
    ok="#15803D", ok_bg="#DCFCE7", bad="#B91C1C", idle_bg="#E5E7EB",
)


def _family(root):
    families = set(tkfont.families(root))
    if sys.platform.startswith("win"):
        wanted = ["Malgun Gothic", "맑은 고딕", "Segoe UI"]
    elif sys.platform == "darwin":
        wanted = ["Apple SD Gothic Neo", "AppleSDGothicNeo-Regular", "Helvetica Neue"]
    else:
        wanted = ["Noto Sans CJK KR", "NanumGothic", "Noto Sans", "DejaVu Sans"]
    for f in wanted:
        if f in families:
            return f
    return tkfont.nametofont("TkDefaultFont", root).actual("family")


def make_icon(root):
    """창 아이콘: 파란 사각형 위 신호 모양"""
    size = 32
    img = tk.PhotoImage(master=root, width=size, height=size)
    img.put(C["accent"], to=(0, 0, size, size))
    wave = [16, 16, 10, 10, 22, 22, 16, 16, 8, 8, 24, 24, 16, 16, 12, 12, 20, 20, 16, 16, 14, 18, 16]
    for i, y in enumerate(wave):
        x = 4 + i
        img.put("white", to=(x, min(y, 16), x + 1, max(y, 16) + 1))
    return img


def apply(root):
    family = _family(root)
    size = 9 if sys.platform.startswith("win") else 11 if sys.platform == "darwin" else 10
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont",
                 "TkSmallCaptionFont", "TkTooltipFont", "TkIconFont"):
        try:
            tkfont.nametofont(name, root).configure(family=family, size=size)
        except tk.TclError:
            pass
    bold = (family, size, "bold")

    root.configure(background=C["bg"])
    root.option_add("*Toplevel.background", C["bg"])
    root.option_add("*TCombobox*Listbox.selectBackground", C["accent"])
    root.option_add("*TCombobox*Listbox.selectForeground", "white")
    root.option_add("*TCombobox*Listbox.background", "white")

    st = ttk.Style(root)
    st.theme_use("clam")
    st.configure(".", background=C["bg"], foreground=C["text"], bordercolor=C["border"],
                 lightcolor=C["panel"], darkcolor=C["border"], focuscolor=C["accent"],
                 troughcolor="#E5E7EB", selectbackground=C["accent"], selectforeground="white",
                 insertcolor=C["text"])
    st.configure("TFrame", background=C["bg"])
    st.configure("TLabel", background=C["bg"], foreground=C["text"])
    st.configure("Muted.TLabel", background=C["bg"], foreground=C["muted"])
    st.configure("TLabelframe", background=C["bg"], bordercolor=C["border"], relief="solid", borderwidth=1)
    st.configure("TLabelframe.Label", background=C["bg"], foreground=C["accent"], font=bold)

    # 버튼
    st.configure("TButton", background=C["panel"], foreground=C["text"], bordercolor=C["border"],
                 lightcolor=C["panel"], darkcolor=C["panel"], padding=(7, 3), focusthickness=0)
    st.map("TButton", background=[("disabled", "#F3F4F6"), ("pressed", "#E2E8F0"), ("active", "#F1F5F9")],
           bordercolor=[("active", C["accent"])], foreground=[("disabled", "#9CA3AF")])
    st.configure("Accent.TButton", background=C["accent"], foreground="white", bordercolor=C["accent"],
                 lightcolor=C["accent"], darkcolor=C["accent"], font=bold)
    st.map("Accent.TButton", background=[("disabled", "#93C5FD"), ("pressed", C["accent_press"]),
                                         ("active", C["accent_hover"])],
           lightcolor=[("active", C["accent_hover"])], darkcolor=[("active", C["accent_hover"])],
           bordercolor=[("active", C["accent_hover"])], foreground=[("disabled", "white")])
    st.configure("Tool.TButton", background=C["toolbar"], bordercolor=C["toolbar"], lightcolor=C["toolbar"],
                 darkcolor=C["toolbar"], padding=(6, 4))
    st.map("Tool.TButton", background=[("pressed", "#DBEAFE"), ("active", "#EFF6FF")],
           bordercolor=[("active", "#BFDBFE")], foreground=[("active", C["accent"])])

    # 제목 줄 / 도구 줄 / 상태 줄
    st.configure("Header.TFrame", background=C["header"])
    st.configure("Header.TLabel", background=C["header"], foreground=C["header_text"], font=(family, size + 4, "bold"))
    st.configure("HeaderSub.TLabel", background=C["header"], foreground=C["header_muted"])
    st.configure("Toolbar.TFrame", background=C["toolbar"])
    st.configure("Toolbar.TLabel", background=C["toolbar"], foreground=C["muted"])
    st.configure("Toolbar.TSeparator", background=C["border"])
    st.configure("Status.TFrame", background=C["status"])
    st.configure("Status.TLabel", background=C["status"], foreground=C["muted"])

    # 연결 상태 표시 (알약 모양 대신 색 바탕)
    st.configure("Open.TLabel", background=C["ok_bg"], foreground=C["ok"], font=bold, padding=(8, 1))
    st.configure("Closed.TLabel", background=C["idle_bg"], foreground=C["muted"], font=bold, padding=(8, 1))

    # 탭
    st.configure("TNotebook", background=C["bg"], borderwidth=0, tabmargins=(2, 4, 2, 0))
    st.configure("TNotebook.Tab", padding=(10, 5), background="#E2E8F0", foreground=C["muted"],
                 bordercolor=C["border"], lightcolor="#E2E8F0")
    st.map("TNotebook.Tab", background=[("selected", C["panel"]), ("active", "#EEF2F7")],
           foreground=[("selected", C["accent"])], lightcolor=[("selected", C["panel"])],
           font=[("selected", bold)])

    # 표
    st.configure("Treeview", background=C["panel"], fieldbackground=C["panel"], foreground=C["text"],
                 bordercolor=C["border"], rowheight=int(size * 2.3) + 2)
    st.map("Treeview", background=[("selected", C["accent_light"])], foreground=[("selected", C["text"])])
    st.configure("Treeview.Heading", background=C["heading"], foreground=C["text"], font=bold,
                 relief="flat", padding=(6, 4), bordercolor=C["border"])
    st.map("Treeview.Heading", background=[("active", "#E2E8F0")])

    # 입력칸
    for w in ("TEntry", "TCombobox", "TSpinbox"):
        st.configure(w, fieldbackground="white", background="white", bordercolor=C["border"],
                     lightcolor="white", darkcolor="white", arrowcolor=C["muted"], padding=2)
        st.map(w, bordercolor=[("focus", C["accent"])], lightcolor=[("focus", C["accent_light"])],
               fieldbackground=[("disabled", "#F3F4F6"), ("readonly", "white")],
               foreground=[("disabled", "#9CA3AF")])
    st.configure("TCheckbutton", background=C["bg"], indicatorbackground="white", indicatorforeground="white",
                 indicatormargin=(0, 0, 6, 0))
    st.map("TCheckbutton", indicatorbackground=[("selected", C["accent"]), ("active", "#EFF6FF")],
           background=[("active", C["bg"])])
    st.configure("TScrollbar", background="#E5E7EB", troughcolor=C["bg"], bordercolor=C["bg"],
                 arrowcolor=C["muted"], lightcolor="#E5E7EB", darkcolor="#E5E7EB")
    st.map("TScrollbar", background=[("active", "#CBD5E1")])
    st.configure("TPanedwindow", background=C["bg"])
    st.configure("Sash", sashthickness=6, gripcount=0, background=C["bg"])
    st.configure("TMenubutton", background=C["panel"], bordercolor=C["border"], padding=(8, 3), arrowcolor=C["muted"])
    try:
        root.iconphoto(True, make_icon(root))
    except tk.TclError:
        pass
    return dict(family=family, size=size)
