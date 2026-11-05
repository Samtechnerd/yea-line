"""Yea-Line: desktop UI for the Yealink MP50 with live phone state, keypad, test calls, call history and settings.

Run:  python mp50_ui.py            (same flags as mp50_buttons.py, e.g. --log file.txt --dial-cmd ...)
Settings and call history are kept in the YeaLine folder under %APPDATA%.
"""
import os
import queue
import sys
import threading
import time
import base64
import ctypes
import io
import tkinter as tk
from tkinter import messagebox, ttk

try:  # tell Windows we draw at the real resolution; otherwise it stretches the window and everything looks blurry
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass
try:
    SCALE = ctypes.windll.user32.GetDpiForSystem() / 96.0
except Exception:
    SCALE = 1.0


def px(n):
    """A pixel size designed for 100% display scaling, scaled to this screen."""
    return int(round(n * SCALE)) if n else 0


def _px_pads(kw):
    for k in ("padx", "pady", "ipadx", "ipady"):
        v = kw.get(k)
        if isinstance(v, (int, float)):
            kw[k] = px(v)
        elif isinstance(v, (tuple, list)):
            kw[k] = tuple(px(x) for x in v)
    return kw


def _scaled(orig):
    def f(self, cnf={}, **kw):
        return orig(self, cnf, **_px_pads(kw))
    return f


for _cls, _names in ((tk.Pack, ("pack_configure", "pack")), (tk.Grid, ("grid_configure", "grid"))):
    for _n in _names:
        setattr(_cls, _n, _scaled(getattr(_cls, _n)))

HIDDEN = "--hidden" in sys.argv  # start in the system tray (used when Windows starts it at sign-in)
if HIDDEN:
    sys.argv.remove("--hidden")
if "--quiet" not in sys.argv:
    sys.argv.append("--quiet")
if "--log" not in sys.argv:  # keep a log next to the settings so problems can be diagnosed later
    _logdir = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "YeaLine")
    os.makedirs(_logdir, exist_ok=True)
    sys.argv += ["--log", os.path.join(_logdir, "log.txt")]
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mp50_audio as audio  # noqa: E402
import mp50_buttons as m  # noqa: E402  (parses sys.argv on import)
import mp50_store as store  # noqa: E402
import yea_assets as assets  # noqa: E402
import yea_cred as cred  # noqa: E402

LIGHT = dict(bg="#f4f5f7", card="#ffffff", text="#1c1e21", muted="#6b7280", border="#e5e7eb", input="#f3f4f6",
             btn="#e5e7eb", accent="#2563eb", grey="#9ca3af", green="#16a34a", red="#dc2626")
DARK = dict(bg="#0f172a", card="#1e293b", text="#f1f5f9", muted="#94a3b8", border="#334155", input="#0f172a",
            btn="#334155", accent="#3b82f6", grey="#64748b", green="#22c55e", red="#ef4444")
FONT = "Segoe UI"
THEME_NAMES = {"Follow Windows": "auto", "Light": "light", "Dark": "dark"}

events = queue.Queue()
_orig_out = m.out


def _out(line):
    _orig_out(line)
    events.put(("log", line))


m.out = _out
m.listeners.append(lambda kind, data: events.put((kind, data)))


def windows_dark():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def fmt_time(seconds):
    seconds = int(seconds)
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "Yea-Line"


def startup_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --hidden'
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return f'"{pyw if os.path.exists(pyw) else sys.executable}" "{os.path.abspath(__file__)}" --hidden'


def startup_enabled():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_NAME)
            return True
    except OSError:
        return False


def set_startup(on):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(k, RUN_NAME)
            except FileNotFoundError:
                pass


class ScrollFrame(tk.Frame):
    """A page that scrolls (scroll bar + mouse wheel) when its content is taller than the window."""

    def __init__(self, parent, bg):
        super().__init__(parent, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0)
        self.bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.body = tk.Frame(self.canvas, bg=bg)
        self.win = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.needs_bar = False
        self.body.bind("<Configure>", self._fit)
        self.canvas.bind("<Configure>", self._fit)
        self.bind_all("<MouseWheel>", self._wheel, add="+")

    def _fit(self, _e=None):
        cw, ch, rh = self.canvas.winfo_width(), self.canvas.winfo_height(), self.body.winfo_reqheight()
        need = rh > ch + 1
        if need != self.needs_bar:
            self.needs_bar = need
            if need:
                self.bar.pack(side="right", fill="y", before=self.canvas)
            else:
                self.bar.pack_forget()
                self.canvas.yview_moveto(0)
        height = max(rh, ch)
        self.canvas.itemconfigure(self.win, width=cw, height=height)
        self.canvas.configure(scrollregion=(0, 0, cw, height))

    def _wheel(self, e):
        if not self.needs_bar:
            return
        w = self.winfo_containing(e.x_root, e.y_root)  # only scroll when the pointer is over this page
        while w is not None and w is not self:
            w = w.master
        if w is self:
            self.canvas.yview_scroll(int(-e.delta / 120) or (-1 if e.delta > 0 else 1), "units")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Yea-Line")
        self._set_icon()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = min(px(660), sw - px(40)), min(px(820), sh - px(100))  # never taller than a small laptop screen
        self.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 4)}")
        self.minsize(min(px(520), w), min(px(420), h))
        self.tray = None

        # state
        self.off_hook = self.bot_active = self.ringing = self.testing = False
        self.number = ""
        self.call = None
        self.level = 0.0
        self.log_lines = []
        self.pad = {}
        self.ring_kind, self.ring_from, self.sip_note = "Incoming test", "", ""
        self.sip_live = ""
        self.sip_state, self.sip_detail = "stopped", "not configured"
        self.assist_active, self.assist_state, self.assist_heard, self.assist_reply = False, "", "", ""
        self.assist_msg, self.assist_ok = "", None

        # persisted settings
        cfg = store.load_settings()
        if cfg.get("test_number"):
            m.args.test_number = cfg["test_number"]
        if cfg.get("dial_cmd") and not m.args.dial_cmd:
            m.args.dial_cmd = cfg["dial_cmd"]
        m.ivr.mic = cfg.get("mic") or None
        self.test_var = tk.StringVar(value=m.args.test_number)
        self.dial_var = tk.StringVar(value=m.args.dial_cmd or "")
        sc = cfg.get("sip", {})
        self.sip_enabled = tk.BooleanVar(value=bool(sc.get("enabled")))
        self.sip_server = tk.StringVar(value=sc.get("server", ""))
        self.sip_port = tk.StringVar(value=str(sc.get("port", 5060)))
        self.sip_user = tk.StringVar(value=sc.get("user", ""))
        self.sip_pass = tk.StringVar()
        ac = cfg.get("assist", {})
        self.ha_url = tk.StringVar(value=ac.get("url", ""))
        self.ha_token = tk.StringVar()
        self.ha_pipeline = tk.StringVar(value=ac.get("pipeline", ""))
        self.assist_number = tk.StringVar(value=ac.get("number", m.args.assist_number))
        self.assist_number.trace_add("write", lambda *_: self._on_assist_number())
        if ac.get("number"):
            m.args.assist_number = ac["number"]
        self.mic_var = tk.StringVar()
        self.theme_var = tk.StringVar(value={v: k for k, v in THEME_NAMES.items()}.get(cfg.get("theme", "auto"), "Follow Windows"))
        self.test_var.trace_add("write", lambda *_: self._on_test_number())
        self.dial_var.trace_add("write", lambda *_: self._on_dial_cmd())
        self.theme_var.trace_add("write", lambda *_: (self._save(), self._apply_theme()))
        self.bg_var = tk.BooleanVar(value=bool(cfg.get("run_in_background", True)))
        self.bg_var.trace_add("write", lambda *_: self._save())
        self.startup_var = tk.BooleanVar(value=startup_enabled())
        self.startup_var.trace_add("write", lambda *_: self._on_startup_toggle())
        sd = cfg.get("speed_dial", {})
        self.sd_enabled = tk.BooleanVar(value=bool(sd.get("enabled")))
        self.sd_name, self.sd_num = {}, {}
        for d in "123456789":
            e = (sd.get("entries") or {}).get(d, {})
            self.sd_name[d] = tk.StringVar(value=e.get("name", ""))
            self.sd_num[d] = tk.StringVar(value=e.get("number", ""))
            self.sd_name[d].trace_add("write", lambda *_: self._apply_speed())
            self.sd_num[d].trace_add("write", lambda *_: self._apply_speed())
        self.sd_enabled.trace_add("write", lambda *_: self._apply_speed())
        self._apply_speed(save=False)

        self.dark = self._want_dark()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._start_tray()
        if HIDDEN and self.tray:
            self.withdraw()

        threading.Thread(target=m.main, daemon=True).start()
        self.after(100, self._poll)
        self.after(800, lambda: self._sip_connect(startup=True))
        self.after(900, lambda: self._assist_connect(startup=True))
        self.after(250, self._tick)
        self.after(500, self._check_phone)

    def _set_icon(self):
        try:  # own taskbar identity so Windows shows the Yea-Line icon instead of Python's
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("YeaLine.App")
        except Exception:
            pass
        try:  # the icons are embedded in the program (yea_assets), so nothing can go missing next to the exe
            self._icons = [tk.PhotoImage(data=assets.ICONS[n]) for n in sorted(assets.ICONS)]
            self.iconphoto(True, *self._icons)
        except Exception:
            pass

    def _logo_image(self):
        n = min(assets.HEADER, key=lambda v: abs(v - px(28)))
        try:
            return tk.PhotoImage(data=assets.HEADER[n])
        except tk.TclError:
            return None

    # ---- tray, startup, speed dial ------------------------------------
    def _apply_speed(self, save=True):
        m.speed_dial["enabled"] = self.sd_enabled.get()
        m.speed_dial["entries"] = {d: self.sd_num[d].get().strip() for d in "123456789"}
        m.speed_dial["names"] = {d: self.sd_name[d].get().strip() for d in "123456789"}
        if save:
            self._save()

    def _on_startup_toggle(self):
        try:
            set_startup(self.startup_var.get())
        except OSError as e:
            messagebox.showerror("Yea-Line", f"Could not change the startup setting: {e}")

    def _start_tray(self):
        try:
            import pystray
            from PIL import Image
            img = Image.open(io.BytesIO(base64.b64decode(assets.ICONS[64])))
            menu = pystray.Menu(pystray.MenuItem("Open Yea-Line", lambda: events.put(("tray_show", {})), default=True),
                                pystray.MenuItem("Quit", lambda: events.put(("tray_quit", {}))))
            self.tray = pystray.Icon("Yea-Line", img, "Yea-Line", menu)
            self.tray.run_detached()
        except Exception as e:
            self.tray = None
            m.out(f"  (tray icon unavailable: {e})")

    def _show_window(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    def _on_close(self):
        if self.tray and self.bg_var.get():
            self.withdraw()
            if not getattr(self, "_told_tray", False):
                self._told_tray = True
                try:
                    self.tray.notify("Yea-Line is still running here. Right-click the icon to quit.", "Yea-Line")
                except Exception:
                    pass
        else:
            self._quit()

    def _quit(self):
        try:
            if self.tray:
                self.tray.stop()
        except Exception:
            pass
        t = threading.Thread(target=m.sip.stop, daemon=True)
        t.start()
        t.join(1.5)
        self.destroy()
        os._exit(0)

    # ---- settings -----------------------------------------------------
    def _save(self):
        store.save_settings({"run_in_background": self.bg_var.get(),
                             "speed_dial": {"enabled": self.sd_enabled.get(),
                                            "entries": {d: {"name": self.sd_name[d].get().strip(),
                                                            "number": self.sd_num[d].get().strip()} for d in "123456789"}},
                             "test_number": m.args.test_number, "dial_cmd": m.args.dial_cmd or "",
                             "mic": m.ivr.mic or "", "theme": THEME_NAMES.get(self.theme_var.get(), "auto"),
                             "assist": {"url": self.ha_url.get().strip(), "pipeline": self.ha_pipeline.get().strip(),
                                        "number": m.args.assist_number},
                             "sip": {"enabled": self.sip_enabled.get(), "server": self.sip_server.get().strip(),
                                     "port": self.sip_port.get().strip(), "user": self.sip_user.get().strip()}})

    def _on_test_number(self):
        v = self.test_var.get().strip()
        if v:  # an empty test number would match every number-less pick-up
            m.args.test_number = v
            self._save()

    def _on_dial_cmd(self):
        m.args.dial_cmd = self.dial_var.get().strip() or None
        self._save()

    def _want_dark(self):
        pref = THEME_NAMES.get(self.theme_var.get(), "auto")
        return pref == "dark" or (pref == "auto" and windows_dark())

    def _apply_theme(self):
        want = self._want_dark()
        if want != self.dark:
            self.dark = want
            self._build()

    # ---- layout -------------------------------------------------------
    def _style(self):
        t = self.t
        self.configure(bg=t["bg"])
        st = ttk.Style(self)
        st.theme_use("clam")
        st.configure(".", background=t["bg"], foreground=t["text"], font=(FONT, 11))
        st.configure("TFrame", background=t["bg"])
        edge = dict(bordercolor=t["border"], lightcolor=t["bg"], darkcolor=t["bg"])
        st.configure("TNotebook", background=t["bg"], borderwidth=0, tabmargins=(px(12), px(6), px(12), 0), **edge)
        st.configure("TNotebook.Tab", background=t["bg"], foreground=t["muted"], padding=(px(20), px(9)), font=(FONT, 11, "bold"),
                     borderwidth=0, focuscolor=t["bg"], bordercolor=t["bg"], lightcolor=t["bg"], darkcolor=t["bg"])
        st.map("TNotebook.Tab", background=[("selected", t["card"]), ("active", t["btn"])], foreground=[("selected", t["text"])],
               bordercolor=[("selected", t["card"])], lightcolor=[("selected", t["card"])], darkcolor=[("selected", t["card"])],
               expand=[("selected", [0, 0, 0, 0])])
        st.configure("TButton", background=t["btn"], foreground=t["text"], font=(FONT, 11), padding=(px(16), px(8)), borderwidth=0, focuscolor=t["btn"])
        st.map("TButton", background=[("active", t["border"]), ("disabled", t["bg"])], foreground=[("disabled", t["grey"])])
        st.configure("Accent.TButton", background=t["accent"], foreground="white")
        st.map("Accent.TButton", background=[("active", t["accent"]), ("disabled", t["btn"])], foreground=[("disabled", t["grey"])])
        st.configure("Treeview", background=t["card"], fieldbackground=t["card"], foreground=t["text"], rowheight=px(30),
                     font=(FONT, 11), borderwidth=0, bordercolor=t["card"], lightcolor=t["card"], darkcolor=t["card"])
        st.configure("Treeview.Heading", background=t["btn"], foreground=t["muted"], font=(FONT, 10, "bold"), borderwidth=0,
                     padding=px(6), bordercolor=t["btn"], lightcolor=t["btn"], darkcolor=t["btn"])
        st.map("Treeview.Heading", background=[("active", t["border"])], foreground=[("active", t["text"])])
        st.map("Treeview", background=[("selected", t["accent"])], foreground=[("selected", "white")])
        st.configure("TCombobox", fieldbackground=t["input"], background=t["btn"], foreground=t["text"], arrowcolor=t["text"],
                     borderwidth=0, padding=px(6))
        st.map("TCombobox", fieldbackground=[("readonly", t["input"])], foreground=[("readonly", t["text"])],
               selectbackground=[("readonly", t["input"])], selectforeground=[("readonly", t["text"])])
        st.configure("Vertical.TScrollbar", background=t["btn"], troughcolor=t["card"], borderwidth=0, arrowcolor=t["muted"],
                     bordercolor=t["card"], lightcolor=t["btn"], darkcolor=t["btn"])
        st.map("Vertical.TScrollbar", background=[("active", t["border"])])
        self.option_add("*TCombobox*Listbox.background", t["card"])
        self.option_add("*TCombobox*Listbox.foreground", t["text"])
        self.option_add("*TCombobox*Listbox.selectBackground", t["accent"])
        self.option_add("*TCombobox*Listbox.font", (FONT, 11))

    def _card(self, parent, title, expand=False):
        t = self.t
        frame = tk.Frame(parent, bg=t["card"], highlightbackground=t["border"], highlightthickness=1)
        frame.pack(fill="both" if expand else "x", expand=expand, padx=14, pady=(12, 0))
        # long explanatory text always wraps to the card width, so a narrow window never cuts it off
        frame.bind("<Configure>", lambda e, f=frame: [w.configure(wraplength=max(px(180), e.width - px(40)))
                                                      for w in f.winfo_children()
                                                      if isinstance(w, tk.Label) and str(w.cget("wraplength")) not in ("0", "")])
        tk.Label(frame, text=title, bg=t["card"], fg=t["muted"], font=(FONT, 9, "bold")).pack(anchor="w", padx=16, pady=(12, 0))
        return frame

    def _label(self, parent, text, muted=False, size=11, bold=False, **kw):
        t = self.t
        return tk.Label(parent, text=text, bg=t["card"], fg=t["muted"] if muted else t["text"],
                        font=(FONT, size, "bold") if bold else (FONT, size), **kw)

    def _entry(self, parent, var, **kw):
        t = self.t
        return tk.Entry(parent, textvariable=var, bg=t["input"], fg=t["text"], insertbackground=t["text"], relief="flat",
                        highlightthickness=1, highlightbackground=t["border"], highlightcolor=t["accent"], **kw)

    def _build(self):
        for child in self.winfo_children():
            child.destroy()
        self.t = DARK if self.dark else LIGHT
        self._style()
        hdr = tk.Frame(self, bg=self.t["bg"])
        hdr.pack(fill="x", padx=14, pady=(12, 0))
        self._logo = self._logo_image()
        if self._logo:
            tk.Label(hdr, image=self._logo, bg=self.t["bg"]).pack(side="left")
        tk.Label(hdr, text="Yea-Line", font=(FONT, 16, "bold"), bg=self.t["bg"], fg=self.t["text"]).pack(side="left", padx=10)
        tk.Label(hdr, text="SIP phone for the Yealink MP50", font=(FONT, 10), bg=self.t["bg"], fg=self.t["muted"]).pack(side="left", pady=(4, 0))
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True)
        phone, hist, sett = ScrollFrame(nb, self.t["bg"]), ttk.Frame(nb), ScrollFrame(nb, self.t["bg"])
        nb.add(phone, text="Phone")
        nb.add(hist, text="History")
        nb.add(sett, text="Settings")
        nb.select(phone)
        self.after(150, lambda: nb.select(phone))  # the first focus event can otherwise land on another tab
        self._build_phone(phone.body)
        self._build_history(hist)
        self._build_settings(sett.body)
        for line in self.log_lines:
            self._log_insert(line)
        self._render()

    def _build_phone(self, page):
        t = self.t
        # --- status
        card = self._card(page, "PHONE")
        body = tk.Frame(card, bg=t["card"])
        body.pack(fill="x", padx=16, pady=(4, 14))
        left = tk.Frame(body, bg=t["card"])
        left.pack(side="left", fill="both", expand=True)
        row = tk.Frame(left, bg=t["card"])
        row.pack(fill="x")
        self.dot = tk.Canvas(row, width=px(20), height=px(20), bg=t["card"], highlightthickness=0)
        self.dot.pack(side="left")
        self.state = self._label(row, "", size=26, bold=True)
        self.state.pack(side="left", padx=10)
        self.timer = self._label(left, "", size=34, bold=True)
        self.timer.pack(anchor="w", pady=(2, 0))
        self.detail = self._label(left, "", muted=True, size=12, anchor="w", justify="left", wraplength=px(300))
        self.detail.pack(fill="x", pady=(2, 0))
        self.audio_lbl = self._label(left, "", muted=True, size=10, anchor="w")
        self.audio_lbl.pack(fill="x", pady=(6, 0))
        self.sip_lbl = self._label(left, "", muted=True, size=10, anchor="w", wraplength=px(300), justify="left")
        self.sip_lbl.pack(fill="x", pady=(2, 0))
        pad = tk.Frame(body, bg=t["card"])
        pad.pack(side="right", padx=(12, 0))
        self.pad = {}
        for i, k in enumerate("123456789*0#"):
            lb = tk.Label(pad, text=k, width=3, bg=t["input"], fg=t["text"], font=(FONT, 15, "bold"), pady=px(5))
            lb.grid(row=i // 3, column=i % 3, padx=3, pady=3)
            self.pad[k] = lb

        # --- test call
        card = self._card(page, "TEST CALL")
        row = tk.Frame(card, bg=t["card"])
        row.pack(fill="x", padx=16, pady=(8, 0))
        self.start_btn = ttk.Button(row, text="Start test call", style="Accent.TButton", command=m.start_bot)
        self.start_btn.pack(side="left")
        self.ring_btn = ttk.Button(row, text="Ring phone", command=m.ring_phone)
        self.ring_btn.pack(side="left", padx=8)
        self.stop_btn = ttk.Button(row, text="Stop", command=self._stop)
        self.stop_btn.pack(side="left")
        self._label(card, "In a test call: 1 = echo test, 2 = settings (2 up, 8 down, 5 select, 0 back). "
                    "Or dial the test number on the phone and pick up.", muted=True, size=10,
                    wraplength=px(560), justify="left", anchor="w").pack(fill="x", padx=16, pady=(8, 0))
        row = tk.Frame(card, bg=t["card"])
        row.pack(fill="x", padx=16, pady=(12, 0))
        self._label(row, "Mic level", size=10, muted=True).pack(side="left")
        self.mictest_btn = ttk.Button(row, text="Test microphone", command=self._mic_test)
        self.mictest_btn.pack(side="right")
        self.meter = tk.Canvas(row, height=px(14), bg=t["input"], highlightthickness=0)
        self.meter.pack(side="left", fill="x", expand=True, padx=10)
        self.mic_lbl = self._label(card, "", muted=True, size=10, anchor="w", justify="left", wraplength=px(560))
        self.mic_lbl.pack(fill="x", padx=16, pady=(6, 14))
        self._refresh_mic_label()

        # --- activity
        card = self._card(page, "ACTIVITY", expand=True)
        box = tk.Frame(card, bg=t["card"])
        box.pack(fill="both", expand=True, padx=16, pady=(6, 4))
        self.log = tk.Text(box, height=6, font=("Consolas", 10), bg=t["input"], fg=t["text"], relief="flat",
                           state="disabled", wrap="word", padx=px(8), pady=px(6))
        self.log.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(box, command=self.log.yview)
        sb.pack(side="right", fill="y")
        self.log.configure(yscrollcommand=sb.set)
        ttk.Button(card, text="Clear", command=self._clear_log).pack(anchor="e", padx=16, pady=(0, 12))
        card.pack_configure(pady=(12, 14))

    def _build_history(self, page):
        t = self.t
        card = self._card(page, "CALL HISTORY", expand=True)
        box = tk.Frame(card, bg=t["card"])
        box.pack(fill="both", expand=True, padx=16, pady=(8, 4))
        cols = ("when", "type", "number", "length")
        self.tree = ttk.Treeview(box, columns=cols, show="headings", selectmode="browse")
        for c, label, width, anchor in (("when", "When", 150, "w"), ("type", "Type", 105, "w"),
                                        ("number", "Number", 100, "w"), ("length", "Length", 62, "e")):
            self.tree.heading(c, text=label, anchor=anchor)
            self.tree.column(c, width=px(width), minwidth=px(48), anchor=anchor, stretch=True)
        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(box, command=self.tree.yview)
        sb.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=sb.set)
        for h in reversed(store.load_history()):
            self._tree_add(h)
        ttk.Button(card, text="Clear history", command=self._clear_history).pack(anchor="e", padx=16, pady=(4, 12))
        card.pack_configure(pady=(12, 14))

    def _check(self, parent, text, var):
        t = self.t
        return tk.Checkbutton(parent, text=text, variable=var, bg=t["card"], fg=t["text"], activebackground=t["card"],
                              activeforeground=t["text"], selectcolor=t["input"], font=(FONT, 11), anchor="w")

    def _build_settings(self, page):
        t = self.t
        card = self._card(page, "STARTUP AND BACKGROUND")
        self._check(card, "Keep running in the system tray when I close the window", self.bg_var).pack(fill="x", padx=16, pady=(8, 0))
        self._check(card, "Start Yea-Line when I sign in to Windows (starts hidden in the tray)", self.startup_var).pack(
            fill="x", padx=16, pady=(2, 0))
        self._label(card, "While hidden, Yea-Line still takes calls and shows a notification when one comes in. "
                    "Open it again from the tray icon.", muted=True, size=10, anchor="w", wraplength=px(560),
                    justify="left").pack(fill="x", padx=16, pady=(6, 14))

        card = self._card(page, "TEST CALL")
        row = tk.Frame(card, bg=t["card"])
        row.pack(fill="x", padx=16, pady=(8, 0))
        self._label(row, "Test number").pack(side="left")
        self._entry(row, self.test_var, width=10, font=(FONT, 14), justify="center").pack(side="left", padx=10, ipady=3)
        self._label(card, "Dial this on the phone and pick up to start the test call.", muted=True, size=10,
                    anchor="w").pack(fill="x", padx=16, pady=(6, 14))

        card = self._card(page, "MICROPHONE")
        row = tk.Frame(card, bg=t["card"])
        row.pack(fill="x", padx=16, pady=(8, 0))
        self.mic_combo = ttk.Combobox(row, textvariable=self.mic_var, state="readonly", font=(FONT, 11))
        self.mic_combo.pack(side="left", fill="x", expand=True)
        self.mic_combo.bind("<<ComboboxSelected>>", lambda e: self._mic_chosen(self.mic_var.get()))
        ttk.Button(row, text="Refresh", command=self._refresh_mics).pack(side="left", padx=(8, 0))
        self._label(card, "Used for the echo test. You can also pick it on the phone: press 2 during a test call.",
                    muted=True, size=10, anchor="w", wraplength=px(560), justify="left").pack(fill="x", padx=16, pady=(6, 14))
        self._refresh_mics()

        card = self._card(page, "HOME ASSISTANT ASSIST")
        grid = tk.Frame(card, bg=t["card"])
        grid.pack(fill="x", padx=16, pady=(8, 0))
        grid.columnconfigure(1, weight=1)
        for r, (label, var, show) in enumerate((("Address", self.ha_url, ""), ("Access token", self.ha_token, "\u2022"),
                                                ("Pipeline", self.ha_pipeline, ""), ("Dial number", self.assist_number, ""))):
            self._label(grid, label).grid(row=r, column=0, sticky="w", pady=4)
            self._entry(grid, var, show=show, font=(FONT, 11)).grid(row=r, column=1, sticky="ew", padx=(12, 0), pady=4, ipady=3)
        row = tk.Frame(card, bg=t["card"])
        row.pack(fill="x", padx=16, pady=(6, 0))
        ttk.Button(row, text="Ask Assist now", command=m.start_assist).pack(side="left")
        ttk.Button(row, text="Save & test", style="Accent.TButton", command=self._assist_connect).pack(side="right")
        self.assist_set_lbl = self._label(card, "", muted=True, size=10, anchor="w", wraplength=px(560), justify="left")
        self.assist_set_lbl.pack(fill="x", padx=16, pady=(8, 0))
        self._label(card, "Address like http://homeassistant.local:8123. Create the token in Home Assistant under your profile, "
                    "Security, Long-lived access tokens. It is stored in Windows Credential Manager. Leave the pipeline blank "
                    "for your preferred one. Dial the number on the phone and pick up to talk to Assist.",
                    muted=True, size=10, anchor="w", wraplength=px(560), justify="left").pack(fill="x", padx=16, pady=(4, 14))
        self._render_assist()

        card = self._card(page, "SIP ACCOUNT (YOUR PBX)")
        grid = tk.Frame(card, bg=t["card"])
        grid.pack(fill="x", padx=16, pady=(8, 0))
        grid.columnconfigure(1, weight=1)
        for r, (label, var, show) in enumerate((("Server", self.sip_server, ""), ("Port", self.sip_port, ""),
                                                ("Extension", self.sip_user, ""), ("Password", self.sip_pass, "\u2022"))):
            self._label(grid, label).grid(row=r, column=0, sticky="w", pady=4)
            self._entry(grid, var, show=show, font=(FONT, 11)).grid(row=r, column=1, sticky="ew", padx=(12, 0), pady=4, ipady=3)
        row = tk.Frame(card, bg=t["card"])
        row.pack(fill="x", padx=16, pady=(6, 0))
        tk.Checkbutton(row, text="Enable SIP", variable=self.sip_enabled, bg=t["card"], fg=t["text"],
                       activebackground=t["card"], activeforeground=t["text"], selectcolor=t["input"],
                       font=(FONT, 11)).pack(side="left")
        ttk.Button(row, text="Save & connect", style="Accent.TButton", command=self._sip_connect).pack(side="right")
        self.sip_set_lbl = self._label(card, "", muted=True, size=10, anchor="w", wraplength=px(560), justify="left")
        self.sip_set_lbl.pack(fill="x", padx=16, pady=(8, 0))
        self._label(card, "Leave the password blank to keep the saved one. It is stored in Windows Credential Manager, "
                    "not in the settings file. Dial a number on the phone and pick up to place a SIP call.",
                    muted=True, size=10, anchor="w", wraplength=px(560), justify="left").pack(fill="x", padx=16, pady=(4, 14))

        card = self._card(page, "SPEED DIAL (OPTIONAL)")
        self._check(card, "Dial *1 to *9 on the phone to call a saved number", self.sd_enabled).pack(fill="x", padx=16, pady=(8, 0))
        grid = tk.Frame(card, bg=t["card"])
        grid.pack(fill="x", padx=16, pady=(6, 0))
        grid.columnconfigure(1, weight=1)
        self._label(grid, "Name (optional)", muted=True, size=10).grid(row=0, column=1, sticky="w", padx=(12, 0))
        self._label(grid, "Number", muted=True, size=10).grid(row=0, column=2, sticky="w", padx=(12, 0))
        for r, d in enumerate("123456789", start=1):
            self._label(grid, "*" + d, bold=True).grid(row=r, column=0, sticky="w", pady=3)
            self._entry(grid, self.sd_name[d], font=(FONT, 11)).grid(row=r, column=1, sticky="ew", padx=(12, 0), pady=3, ipady=3)
            self._entry(grid, self.sd_num[d], width=14, font=(FONT, 11)).grid(row=r, column=2, padx=(12, 0), pady=3, ipady=3)
        self._label(card, "Press * then a digit on the phone and pick up. Digits you leave empty dial normally.",
                    muted=True, size=10, anchor="w", wraplength=px(560), justify="left").pack(fill="x", padx=16, pady=(6, 14))

        card = self._card(page, "DIAL COMMAND (OPTIONAL)")
        self._entry(card, self.dial_var, font=("Consolas", 11)).pack(fill="x", padx=16, pady=(8, 0), ipady=4)
        self._label(card, 'Run when you pick up with any other number. {number} is replaced, e.g.  start "" "sip:{number}"',
                    muted=True, size=10, anchor="w", wraplength=px(560), justify="left").pack(fill="x", padx=16, pady=(6, 14))

        card = self._card(page, "APPEARANCE")
        row = tk.Frame(card, bg=t["card"])
        row.pack(fill="x", padx=16, pady=(8, 14))
        self._label(row, "Theme").pack(side="left")
        ttk.Combobox(row, textvariable=self.theme_var, values=list(THEME_NAMES), state="readonly",
                     width=16, font=(FONT, 11)).pack(side="left", padx=10)

    # ---- Home Assistant Assist ----------------------------------------
    def _on_assist_number(self):
        v = self.assist_number.get().strip()
        if v:
            m.args.assist_number = v
            self._save()

    def _assist_connect(self, startup=False):
        url = self.ha_url.get().strip()
        tok = self.ha_token.get().strip()
        if tok and url:
            try:
                cred.set_secret("YeaLine/assist", "assist", tok)
                self.ha_token.set("")
            except OSError as e:
                self._assist_note(False, f"could not save the token: {e}")
                return
        self._save()
        secret = cred.get_secret("YeaLine/assist") if url else None
        m.assist.configure(url, secret or "", self.ha_pipeline.get())
        if startup and not m.assist.configured():
            return
        self._assist_note(None, "testing the connection...")
        threading.Thread(target=lambda: events.put(("assist_test", dict(zip(("ok", "message"), m.assist.test())))),
                         daemon=True).start()

    def _assist_note(self, ok, message):
        self.assist_ok, self.assist_msg = ok, message
        self._render_assist()

    def _render_assist(self):
        lb = getattr(self, "assist_set_lbl", None)
        if lb is None:
            return
        t = self.t
        color = t["muted"] if self.assist_ok is None else (t["green"] if self.assist_ok else t["red"])
        lb.configure(text=self.assist_msg, fg=color)

    # ---- SIP ----------------------------------------------------------
    def _sip_target(self):
        return f"YeaLine/sip/{self.sip_user.get().strip()}@{self.sip_server.get().strip()}"

    def _sip_connect(self, startup=False):
        server, user = self.sip_server.get().strip(), self.sip_user.get().strip()
        pw = self.sip_pass.get()
        try:
            port = int(self.sip_port.get().strip() or 5060)
        except ValueError:
            self._sip_status("error", "port must be a number")
            return
        if pw and server and user:
            try:
                cred.set_secret(self._sip_target(), user, pw)
                self.sip_pass.set("")
            except OSError as e:
                self._sip_status("error", f"could not save the password: {e}")
                return
        self._save()
        if not self.sip_enabled.get():
            threading.Thread(target=m.sip.stop, daemon=True).start()
            if not startup:
                self._sip_status("stopped", "SIP is turned off")
            return
        secret = cred.get_secret(self._sip_target()) if (server and user) else None
        if not (server and user and secret):
            self._sip_status("stopped", "enter the server, extension and password, then Save & connect")
            return
        m.sip.configure(server, port, user, secret)
        threading.Thread(target=m.sip.start, daemon=True).start()

    def _sip_status(self, state, detail=""):
        self.sip_state, self.sip_detail = state, detail
        self._render_sip()

    def _render_sip(self):
        t = self.t
        text = {"registered": f"SIP: registered as {self.sip_detail}",
                "registering": f"SIP: connecting to {self.sip_detail}...",
                "failed": f"SIP: {self.sip_detail}", "error": f"SIP: {self.sip_detail}",
                "stopped": f"SIP: {self.sip_detail or 'off'}"}.get(self.sip_state, "SIP")
        if self.sip_state in ("failed", "error"):
            color = t["red"]
        elif self.sip_state == "registered":
            color = t["green"]
        else:
            color = t["muted"]
        for name in ("sip_lbl", "sip_set_lbl"):
            lb = getattr(self, name, None)
            if lb is not None:
                lb.configure(text=text, fg=color)

    # ---- microphone ---------------------------------------------------
    def _refresh_mics(self):
        names = audio.list_devices("in")
        self.mic_combo.configure(values=names)
        cur = m.ivr.mic if m.ivr.mic in names else None
        if cur is None:
            i = audio.find_device("in")
            cur = names[i] if i is not None else (names[0] if names else "")
        self.mic_var.set(cur)

    def _mic_chosen(self, name):
        m.ivr.mic = name
        self._save()
        self._refresh_mic_label()

    def _refresh_mic_label(self):
        names = audio.list_devices("in")
        if m.ivr.mic in names:
            name = m.ivr.mic
        else:
            i = audio.find_device("in")
            name = names[i] if i is not None else "none found"
        self.mic_lbl.configure(text=f"Microphone: {audio.speakable(name)}")

    def _mic_test(self):
        if self.testing:
            return
        idx, name = m.ivr._device_index()
        if name is None:
            self.mic_lbl.configure(text="No microphone found")
            return
        self.testing = True
        self.mic_lbl.configure(text=f"Listening to {audio.speakable(name)} for 3 seconds - say something")
        self._render()
        peak = [0.0]

        def level(v):
            peak[0] = max(peak[0], v)
            events.put(("level", {"value": v}))

        def run():
            try:
                audio.record(3, idx, on_level=level)
                events.put(("mictest_done", {"peak": peak[0], "name": name}))
            except Exception as e:
                events.put(("mictest_done", {"error": str(e)}))
        threading.Thread(target=run, daemon=True).start()

    # ---- actions ------------------------------------------------------
    def _stop(self):
        m.stop_ring()
        m.stop_bot()

    def _clear_log(self):
        self.log_lines = []
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _log_insert(self, line):
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _add_log(self, line):
        line = line.strip()
        self.log_lines = (self.log_lines + [line])[-300:]
        self._log_insert(line)

    def _tree_add(self, h):
        self.tree.insert("", 0, values=(h.get("start", ""), h.get("kind", ""), h.get("number", "") or "-",
                                        fmt_time(h.get("seconds", 0))))

    def _clear_history(self):
        if messagebox.askyesno("Clear history", "Delete all saved call history?", parent=self):
            store.clear_history()
            self.tree.delete(*self.tree.get_children())

    # ---- call tracking ------------------------------------------------
    def _begin_call(self, kind, number=""):
        self.call = {"start": time.time(), "mono": time.monotonic(), "kind": kind, "number": number}

    def _end_call(self):
        if not self.call:
            return
        c, self.call = self.call, None
        entry = {"start": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(c["start"])), "kind": c["kind"],
                 "number": c["number"], "seconds": int(time.monotonic() - c["mono"])}
        store.add_history(entry)
        self._tree_add(entry)

    def _on_event(self, kind, data):
        if kind == "log":
            self._add_log(data)
        elif kind == "pickup":
            self.off_hook, self.number = True, data.get("number", "")
            if self.ringing:
                label = self.ring_kind
            elif self.number == m.args.test_number:
                label = "Test call"
            elif self.number == m.args.assist_number:
                label = "Assist"
            elif self.number:
                label = "Outgoing"
            else:
                label = "Call"
            self._end_call()
            self._begin_call(label, self.ring_from if label == "Incoming call" else self.number)
        elif kind == "hangup":
            self.off_hook = False
            self.sip_live = ""
            self._end_call()
        elif kind == "bot_start":
            self.bot_active = True
            if not self.call:
                self._begin_call("Test call")
        elif kind in ("bot_stop", "bot_done"):
            self.bot_active = False
            if not self.off_hook:
                self._end_call()
        elif kind == "assist_test":
            self._assist_note(data.get("ok"), data.get("message", ""))
        elif kind == "assist_start":
            self.assist_active, self.assist_state, self.assist_heard, self.assist_reply = True, "listening", "", ""
        elif kind == "assist_state":
            self.assist_state = data.get("state", "")
        elif kind == "assist_heard":
            self.assist_heard, self.assist_reply = data.get("text", ""), ""
        elif kind == "assist_reply":
            self.assist_reply = data.get("text", "")
        elif kind == "assist_error":
            self.assist_reply = "Error: " + data.get("message", "")
        elif kind == "assist_end":
            self.assist_active = False
        elif kind == "sip_status":
            self._sip_status(data.get("state", ""), data.get("detail", ""))
        elif kind == "sip_incoming":
            self.ringing, self.ring_kind, self.ring_from = True, "Incoming call", data.get("caller", "")
            if self.tray and not self.winfo_viewable():
                try:
                    self.tray.notify(f"Incoming call from {self.ring_from or 'unknown'}", "Yea-Line")
                except Exception:
                    pass
        elif kind == "tray_show":
            self._show_window()
        elif kind == "tray_quit":
            self._quit()
        elif kind == "sip_call":
            st = data.get("state")
            num = data.get("number", "")
            self.sip_live = st if st in ("dialing", "ringing", "answered") else ""
            if st == "dialing":
                self.sip_note = f"Calling {num}..."
            elif st == "ringing":
                self.sip_note = f"Ringing {num}..."
            elif st == "answered":
                self.sip_note = f"Connected to {num}" if num else "Connected"
                if data.get("direction") == "in":
                    self.ringing = False
            elif st == "ended":
                was_ringing = self.ringing and data.get("direction") == "in"
                self.ringing = False
                reason = data.get("reason", "")
                self.sip_note = f"Call ended: {reason}" if reason and reason != "missed" else ""
                if was_ringing and reason in ("missed", "no answer"):
                    entry = {"start": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": "Missed",
                             "number": self.ring_from, "seconds": 0}
                    store.add_history(entry)
                    self._tree_add(entry)
                else:
                    self._end_call()
        elif kind == "ring_start":
            self.ringing, self.ring_kind, self.ring_from = True, "Incoming test", ""
        elif kind == "ring_stop":
            self.ringing = False
            if data.get("missed"):
                entry = {"start": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": "Missed test", "number": "", "seconds": 0}
                store.add_history(entry)
                self._tree_add(entry)
        elif kind == "key":
            self._flash_key(data.get("key", ""))
        elif kind == "level":
            self.level = max(self.level, min(1.0, data.get("value", 0) * 6))
        elif kind == "mic":
            name = data.get("name", "")
            self.mic_var.set(name)
            self._save()
            self._refresh_mic_label()
        elif kind == "mictest_done":
            self.testing = False
            if "error" in data:
                text = f"Microphone test failed: {data['error']}"
            elif data["peak"] < 0.002:
                text = (f"Silent on {audio.speakable(data['name'])}. The phone may only send microphone audio "
                        "during a call, or it is muted.")
            else:
                text = f"Heard you on {audio.speakable(data['name'])} (peak {min(100, int(data['peak'] * 600))}%)"
            self.mic_lbl.configure(text=text)

    def _flash_key(self, key):
        lb = self.pad.get(key)
        if not lb:
            return
        lb.configure(bg=self.t["accent"], fg="white")

        def reset(l=lb):
            try:
                l.configure(bg=self.t["input"], fg=self.t["text"])
            except tk.TclError:
                pass
        self.after(350, reset)

    # ---- loops --------------------------------------------------------
    def _poll(self):
        try:
            while True:
                kind, data = events.get_nowait()
                try:
                    self._on_event(kind, data)
                except Exception as e:  # a bad event must not kill the UI loop
                    print("ui event error:", kind, e, file=sys.stderr)
                self._render()
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _tick(self):
        self.level *= 0.8
        w = max(self.meter.winfo_width(), 1)
        self.meter.delete("all")
        if self.level > 0.01:
            self.meter.create_rectangle(0, 0, w * self.level, px(14), fill=self.t["red"] if self.level > 0.9 else self.t["green"], width=0)
        self.timer.configure(text=fmt_time(time.monotonic() - self.call["mono"]) if self.call else "")
        if int(time.time()) % 5 == 0:
            self._apply_theme()
        self.after(250, self._tick)

    def _check_phone(self):
        try:
            found = m.phone_audio_present()
        except Exception:
            found = False
        self.audio_lbl.configure(text="Phone audio: " + ("connected" if found else "not found - check the USB cable"),
                                 fg=self.t["muted"] if found else self.t["red"])
        self.after(3000, self._check_phone)

    def _render(self):
        t = self.t
        kind = self.call["kind"] if self.call else "Test call"
        if self.ringing:
            text, color = "Ringing", t["red"]
            detail = (f"Call from {self.ring_from} - pick up to answer" if self.ring_from
                      else "Incoming test call - pick up the phone to answer")
        elif self.assist_active:
            text, color = "Assist", t["accent"]
            lines = [self.assist_state.capitalize() + "..." if self.assist_state else "Connecting..."]
            if self.assist_heard:
                lines.append(f"You: {self.assist_heard}")
            if self.assist_reply:
                lines.append(f"Assist: {self.assist_reply}")
            detail = "\n".join(lines)
        elif self.bot_active:
            text, color, detail = kind, t["accent"], "Keypad menu is running through the phone"
        elif self.off_hook and self.sip_live in ("dialing", "ringing"):
            text, color, detail = "Calling", t["accent"], self.sip_note
        elif self.off_hook and self.sip_live == "answered":
            text, color, detail = "In call", t["green"], self.sip_note
        elif self.off_hook:
            text, color = "Off hook", t["green"]
            detail = self.sip_note or (f"Number: {self.number}" if self.number else "No number dialled")
        else:
            text, color = "On hook", t["grey"]
            detail = "Waiting for a call" + (f"  (last number: {self.number})" if self.number else "")
        self.state.configure(text=text)
        self.detail.configure(text=detail)
        self._render_sip()
        self.dot.delete("all")
        self.dot.create_oval(px(3), px(3), px(17), px(17), fill=color, outline=color)
        busy = self.bot_active or self.ringing or self.assist_active
        for b in (self.start_btn, self.ring_btn):
            b.state(["disabled"] if busy else ["!disabled"])
        self.stop_btn.state(["!disabled"] if busy else ["disabled"])
        self.mictest_btn.state(["disabled"] if (self.testing or self.bot_active) else ["!disabled"])


if __name__ == "__main__":
    App().mainloop()
