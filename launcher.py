"""
遊戲即時字幕：啟動視窗

給一般使用者的入口：雙擊打開這個視窗，選好設定按「開始」就好，
不用記任何指令參數。

按「開始」後，會另外開一個字幕程序（跑 overlay_subtitle.py 的 main），
它印出來的訊息會即時顯示在視窗下方的紀錄區；按「停止」就把那個程序關掉。
另外開程序、而不是在同一個程序裡跑，是因為：停止時可以整個乾淨關掉
（錄音、模型、網路連線一次釋放），字幕程序出錯也不會連啟動視窗一起當掉。

打包成 exe 時，同一支 exe 身兼兩職：
    遊戲即時字幕.exe                  → 開啟啟動視窗
    遊戲即時字幕.exe --overlay 參數⋯   → 直接跑字幕（由啟動視窗自己呼叫）

開發時：
    py launcher.py
    py launcher.py --list-apps    # 排查用：列出「只翻譯這個程式」清單會出現的程式（發出過聲音的、有開視窗的）
"""

import os
import sys
import json
import queue
import ctypes
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk, messagebox

from i18n import LANGS, current_lang, set_lang, system_lang, t, variants

FROZEN = getattr(sys, "frozen", False)
HERE = (Path(sys.executable).resolve().parent if FROZEN
        else Path(__file__).resolve().parent)
SETTINGS_FILE = HERE / "launcher_settings.json"
ENV_FILE = HERE / ".env"

UI_FONT = ("Microsoft JhengHei", 10)
# 設定檔裡「聲音來源」「只翻譯這個程式」存空字串代表不指定；
# 畫面上顯示的「Windows 預設喇叭」這類文字會跟著介面語言變，所以不能直接存它
DEFAULT_DEVICE = ""
NO_APP = ""
# 舊版設定檔直接存了中文的顯示文字，讀進來時換成空字串
LEGACY_BLANKS = {"device": "Windows 預設喇叭", "app": "（不指定，錄整台電腦的聲音）"}
MAX_LOG_LINES = 500

# 傳給字幕程式的參數值 → 畫面上顯示的文字（i18n.py 裡的 key）
DIRECTIONS = {"en-zh": "dir_en_zh", "zh-en": "dir_zh_en"}
TRANSLATORS = {"google": "tr_google", "gemini": "tr_gemini"}
MODELS = {"base": "model_base", "small": "model_small", "large-v3-turbo": "model_large"}
DEFAULTS = {"ui_lang": system_lang(), "device": DEFAULT_DEVICE, "app": NO_APP,
            "direction": "en-zh", "translator": "google", "model": "small",
            "lines": 3, "min_rms": 200, "max_seconds": 8, "click_through": False}


# ══════════════════════════════════════════════════════
#  設定與金鑰的讀寫
# ══════════════════════════════════════════════════════
def load_settings():
    """讀上次的設定；檔案不存在或壞掉就用預設值"""
    try:
        saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    settings = {**DEFAULTS, **{k: v for k, v in saved.items() if k in DEFAULTS}}
    for key, old_text in LEGACY_BLANKS.items():
        if settings[key] == old_text:
            settings[key] = ""
    if settings["ui_lang"] not in LANGS:
        settings["ui_lang"] = DEFAULTS["ui_lang"]
    return settings


def save_settings(settings):
    try:
        SETTINGS_FILE.write_text(json.dumps(settings, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
    except OSError:
        pass            # 存不了只是下次要重選，不影響這次使用


def read_env_lines():
    try:
        return ENV_FILE.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return []


def is_key_line(line):
    return line.partition("=")[0].strip() == "GEMINI_API_KEY"


def has_gemini_key():
    """跟 overlay_subtitle.read_key() 的找法一致：先看 .env，再看系統環境變數"""
    for line in read_env_lines():
        if is_key_line(line) and line.partition("=")[2].strip().strip("\"'"):
            return True
    return bool(os.environ.get("GEMINI_API_KEY", "").strip())


def save_gemini_key(key):
    """把金鑰寫進 .env：原本有 GEMINI_API_KEY 那一行就換掉，其他行原封不動"""
    lines = [line for line in read_env_lines() if not is_key_line(line)]
    lines.append(f"GEMINI_API_KEY={key}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def list_devices():
    """列出可以錄的喇叭（loopback）裝置名稱；抓不到就回傳空清單"""
    try:
        import pyaudiowpatch as pyaudio
        with pyaudio.PyAudio() as p:
            return [d["name"] for d in p.get_loopback_device_info_generator()]
    except Exception:
        return []


def list_sounding_apps():
    """正在（或剛剛）發出聲音的程式。程式沒播過聲音時，Windows 這邊就查不到它"""
    try:
        from pycaw.pycaw import AudioUtilities
        return {s.Process.name() for s in AudioUtilities.GetAllSessions() if s.Process}
    except Exception:
        return set()


def list_windowed_apps():
    """有開視窗的程式。Discord 開著但還沒有人講話時，就是靠這個才選得到"""
    try:
        import psutil
        user32 = ctypes.windll.user32
        pids = set()

        def collect(hwnd, _):
            if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0:
                pid = ctypes.c_ulong()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                pids.add(pid.value)
            return True

        proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        user32.EnumWindows(proto(collect), 0)
        windows_dir = os.environ.get("SystemRoot", r"C:\Windows").lower()
        names = set()
        for pid in pids:
            try:
                p = psutil.Process(pid)
                # 濾掉 Windows 自己的背景視窗（檔案總管、輸入法等），使用者不會想選它們
                if not (p.exe() or "").lower().startswith(windows_dir):
                    names.add(p.name())
            except Exception:
                pass
        return names
    except Exception:
        return set()


def list_apps():
    """可以選的程式：正在發聲的排前面，後面接有開視窗的"""
    sounding = sorted(list_sounding_apps(), key=str.lower)
    windowed = sorted(list_windowed_apps() - set(sounding), key=str.lower)
    return sounding + windowed


def build_overlay_args(s):
    """把畫面上的設定轉成字幕程式的指令參數"""
    args = ["--overlay", "--model", s["model"], "--min-rms", str(s["min_rms"]),
            "--lines", str(s["lines"]), "--direction", s["direction"],
            "--translator", s["translator"],
            "--max-seconds", str(s["max_seconds"]), "--ui-lang", s["ui_lang"]]
    if s["app"]:
        args += ["--app", s["app"]]          # 指定程式時就不看聲音來源了
    elif s["device"]:
        args += ["--device", s["device"]]
    if s["click_through"]:
        args.append("--click-through")
    return args


def label_of(mapping, value):
    """由參數值找回畫面上顯示的文字；找不到（例如設定檔被改壞）就用第一個"""
    return t(mapping.get(value, next(iter(mapping.values()))))


def value_of(mapping, label):
    """由畫面上顯示的文字找回參數值；找不到就用第一個"""
    for value, key in mapping.items():
        if t(key) == label:
            return value
    return next(iter(mapping))


def labels_of(mapping):
    return [t(key) for key in mapping.values()]


# ══════════════════════════════════════════════════════
#  啟動視窗
# ══════════════════════════════════════════════════════
class Launcher:
    def __init__(self):
        self.proc = None
        self.stopping = False
        self.last_was_progress = False
        self.log_q = queue.Queue()
        self.inputs = []            # 執行中要鎖住的輸入元件
        self.outer = None

        self.root = tk.Tk()
        self.root.minsize(600, 600)
        self.root.option_add("*Font", UI_FONT)
        ttk.Style().configure(".", font=UI_FONT)
        self.build(load_settings())

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self.poll)

    # ── 畫面 ──
    def build(self, s):
        """用 s 的設定和語言把整個畫面畫出來；換語言時會先拆掉舊畫面再呼叫一次"""
        set_lang(s["ui_lang"])
        self.root.title(t("app_title"))
        self.inputs = []
        self.outer = ttk.Frame(self.root, padding=14)
        self.outer.pack(fill="both", expand=True)
        self.build_lang_bar(self.outer)
        self.build_tips(self.outer)
        self.build_form(self.outer, s)
        self.build_controls(self.outer)
        self.build_log(self.outer)

    def build_lang_bar(self, parent):
        """最上方右邊的語言選單。標籤中英文都寫，看不懂中文的人也找得到"""
        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=(0, 8))
        self.lang_var = tk.StringVar(value=LANGS[current_lang()])
        combo = ttk.Combobox(bar, textvariable=self.lang_var, values=list(LANGS.values()),
                             state="readonly", width=12)
        combo.pack(side="right")
        combo.bind("<<ComboboxSelected>>", lambda _: self.switch_lang())
        ttk.Label(bar, text=t("lang_label")).pack(side="right", padx=(0, 6))
        self.inputs.append(combo)

    def switch_lang(self):
        """
        換介面語言：先用舊語言讀出目前填好的設定，再用新語言把畫面整個重畫。
        執行中語言選單是鎖住的，所以只會在沒執行時發生；執行紀錄的內容會保留。
        """
        lang = next(code for code, name in LANGS.items() if name == self.lang_var.get())
        if lang == current_lang():
            return
        s = {**self.read_form(), "ui_lang": lang}
        save_settings(s)
        log_text = self.log.get("1.0", "end-1c")
        self.outer.destroy()
        self.build(s)
        if log_text:
            self.log.configure(state="normal")
            self.log.insert("end", log_text + "\n")
            self.log.configure(state="disabled")

    def build_tips(self, parent):
        box = ttk.LabelFrame(parent, text=t("tips_title"), padding=10)
        box.pack(fill="x")
        ttk.Label(box, text=t("tips"), justify="left").pack(anchor="w")

    def build_form(self, parent, s):
        box = ttk.LabelFrame(parent, text=t("settings_title"), padding=10)
        box.pack(fill="x", pady=(10, 0))
        box.columnconfigure(1, weight=1)

        self.app_var = tk.StringVar()
        self.app_box = self.add_combo(box, 0, t("app_label"), self.app_var, [])
        self.app_box.bind("<<ComboboxSelected>>", lambda _: self.refresh_source_state())
        refresh_app = ttk.Button(box, text=t("refresh"), command=self.refresh_apps)
        refresh_app.grid(row=0, column=2, padx=(6, 0))
        self.inputs.append(refresh_app)
        self.add_hint(box, 1, t("app_hint"))
        self.refresh_apps(select=s["app"])

        self.device_var = tk.StringVar()
        self.device_box = self.add_combo(box, 2, t("device_label"), self.device_var, [])
        self.device_refresh = ttk.Button(box, text=t("refresh"), command=self.refresh_devices)
        self.device_refresh.grid(row=2, column=2, padx=(6, 0))
        self.device_hint = self.add_hint(box, 3, "")
        self.refresh_devices(select=s["device"])
        self.refresh_source_state()

        self.direction_var = tk.StringVar(value=label_of(DIRECTIONS, s["direction"]))
        self.add_combo(box, 4, t("direction_label"), self.direction_var, labels_of(DIRECTIONS))
        self.add_hint(box, 5, t("direction_hint"))

        self.translator_var = tk.StringVar(value=label_of(TRANSLATORS, s["translator"]))
        combo = self.add_combo(box, 6, t("translator_label"), self.translator_var,
                               labels_of(TRANSLATORS))
        combo.bind("<<ComboboxSelected>>", lambda _: self.refresh_key_state())

        ttk.Label(box, text=t("key_label")).grid(row=7, column=0, sticky="w", pady=4)
        self.key_var = tk.StringVar()
        self.key_entry = ttk.Entry(box, textvariable=self.key_var, show="•")
        self.key_entry.grid(row=7, column=1, sticky="ew", pady=4)
        self.key_hint = self.add_hint(box, 8, "")
        self.refresh_key_state()

        self.model_var = tk.StringVar(value=label_of(MODELS, s["model"]))
        self.add_combo(box, 9, t("model_label"), self.model_var, labels_of(MODELS))

        ttk.Label(box, text=t("lines_label")).grid(row=10, column=0, sticky="w", pady=4)
        self.lines_var = tk.StringVar(value=str(s["lines"]))
        spin = ttk.Spinbox(box, from_=1, to=5, width=5, textvariable=self.lines_var,
                           state="readonly")
        spin.grid(row=10, column=1, sticky="w", pady=4)
        self.inputs.append(spin)

        ttk.Label(box, text=t("seconds_label")).grid(row=11, column=0, sticky="w", pady=4)
        self.seconds_var = tk.StringVar(value=str(s["max_seconds"]))
        seconds = ttk.Spinbox(box, from_=2, to=30, width=5, state="readonly",
                              textvariable=self.seconds_var)
        seconds.grid(row=11, column=1, sticky="w", pady=4)
        self.inputs.append(seconds)
        self.add_hint(box, 12, t("seconds_hint"))

        self.build_rms_row(box, 13, s["min_rms"])

        self.click_var = tk.BooleanVar(value=bool(s["click_through"]))
        check = ttk.Checkbutton(box, variable=self.click_var, text=t("click_through"))
        check.grid(row=15, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self.inputs.append(check)

    def build_rms_row(self, box, row, value):
        ttk.Label(box, text=t("rms_label")).grid(row=row, column=0, sticky="w", pady=4)
        self.rms_var = tk.IntVar(value=int(value))
        value_label = ttk.Label(box, width=5)

        def on_move(v):
            self.rms_var.set(int(float(v) // 10 * 10))
            value_label.config(text=str(self.rms_var.get()))

        scale = ttk.Scale(box, from_=50, to=1500, command=on_move)
        scale.set(self.rms_var.get())
        on_move(self.rms_var.get())
        scale.grid(row=row, column=1, sticky="ew", pady=4)
        value_label.grid(row=row, column=2, padx=(6, 0))
        self.inputs.append(scale)
        self.add_hint(box, row + 1, t("rms_hint"))

    def add_combo(self, box, row, label, var, values):
        ttk.Label(box, text=label).grid(row=row, column=0, sticky="w", pady=4)
        combo = ttk.Combobox(box, textvariable=var, values=values, state="readonly")
        combo.grid(row=row, column=1, sticky="ew", pady=4)
        self.inputs.append(combo)
        return combo

    def add_hint(self, box, row, text):
        hint = ttk.Label(box, text=text, foreground="#777777")
        hint.grid(row=row, column=1, columnspan=2, sticky="w")
        return hint

    def build_controls(self, parent):
        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=(12, 0))
        self.start_btn = ttk.Button(bar, text=t("start"), command=self.start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(bar, text=t("stop"), command=self.stop)
        self.stop_btn.pack(side="left", padx=(8, 0))
        self.stop_btn.state(["disabled"])
        self.status_var = tk.StringVar(value=t("status_idle"))
        ttk.Label(bar, textvariable=self.status_var).pack(side="left", padx=(14, 0))

    def build_log(self, parent):
        box = ttk.LabelFrame(parent, text=t("log_title"), padding=6)
        box.pack(fill="both", expand=True, pady=(10, 0))
        self.log = tk.Text(box, height=10, state="disabled", wrap="word",
                           font=("Consolas", 9), bg="#f7f7f7", relief="flat")
        bar = ttk.Scrollbar(box, command=self.log.yview)
        self.log.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)

    # ── 畫面狀態 ──
    def refresh_devices(self, select=None):
        """select 是設定檔裡存的值（空字串＝預設喇叭）；沒給就保留畫面上目前選的"""
        default = t("default_device")
        names = [default] + list_devices()
        self.device_box.config(values=names)
        want = self.device_var.get() if select is None else (select or default)
        self.device_var.set(want if want in names else default)

    def refresh_apps(self, select=None):
        """select 是設定檔裡存的值（空字串＝不指定）；沒給就保留畫面上目前選的"""
        none = t("no_app")
        names = [none] + list_apps()
        self.app_box.config(values=names)
        want = self.app_var.get() if select is None else (select or none)
        self.app_var.set(want if want in names else none)

    def is_running(self):
        return self.proc is not None and self.proc.poll() is None

    def refresh_source_state(self):
        """指定了程式之後，「聲音來源」就用不到了，把它變灰避免誤會"""
        whole_pc = self.app_var.get() == t("no_app")
        running = self.is_running()
        for w in (self.device_box, self.device_refresh):
            w.state(["!disabled"] if whole_pc and not running else ["disabled"])
        self.device_hint.config(text=t("device_hint") if whole_pc else t("device_hint_disabled"))

    def refresh_key_state(self):
        using_gemini = value_of(TRANSLATORS, self.translator_var.get()) == "gemini"
        running = self.is_running()
        self.key_entry.state(["!disabled"] if using_gemini and not running else ["disabled"])
        if not using_gemini:
            self.key_hint.config(text=t("key_hint_google"))
        elif has_gemini_key():
            self.key_hint.config(text=t("key_hint_saved"))
        else:
            self.key_hint.config(text=t("key_hint_missing"))

    def set_running(self, running):
        for w in self.inputs:
            w.state(["disabled"] if running else ["!disabled"])
        self.start_btn.state(["disabled"] if running else ["!disabled"])
        self.stop_btn.state(["!disabled"] if running else ["disabled"])
        self.refresh_key_state()
        self.refresh_source_state()

    def read_form(self):
        try:
            lines = min(max(int(self.lines_var.get()), 1), 5)
        except ValueError:
            lines = DEFAULTS["lines"]
        try:
            max_seconds = min(max(int(self.seconds_var.get()), 2), 30)
        except ValueError:
            max_seconds = DEFAULTS["max_seconds"]
        device, app = self.device_var.get(), self.app_var.get()
        return {
            "ui_lang": current_lang(),
            "device": DEFAULT_DEVICE if device == t("default_device") else device,
            "app": NO_APP if app == t("no_app") else app,
            "direction": value_of(DIRECTIONS, self.direction_var.get()),
            "translator": value_of(TRANSLATORS, self.translator_var.get()),
            "model": value_of(MODELS, self.model_var.get()),
            "lines": lines,
            "max_seconds": max_seconds,
            "min_rms": self.rms_var.get(),
            "click_through": self.click_var.get(),
        }

    def ensure_gemini_key(self):
        new_key = self.key_var.get().strip()
        if new_key:
            try:
                save_gemini_key(new_key)
            except OSError as e:
                messagebox.showerror(t("app_title"), t("err_key_save", error=e))
                return False
            self.key_var.set("")
            return True
        if has_gemini_key():
            return True
        messagebox.showwarning(t("app_title"), t("warn_no_key"))
        return False

    # ── 開始／停止 ──
    def start(self):
        if self.is_running():
            return
        s = self.read_form()
        if s["translator"] == "gemini" and not self.ensure_gemini_key():
            return
        save_settings(s)

        cmd = [sys.executable] + ([] if FROZEN else [str(Path(__file__).resolve())])
        cmd += build_overlay_args(s)
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=HERE, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                encoding="utf-8", errors="replace",
                env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"},
                creationflags=subprocess.CREATE_NO_WINDOW)
        except OSError as e:
            messagebox.showerror(t("app_title"), t("err_start", error=e))
            return

        self.stopping = False
        self.clear_log()
        # exe 版第一次開啟時，Windows 防毒會先掃描約 2GB 的顯示卡元件，實測要等一分鐘多
        self.status_var.set(t("status_starting"))
        self.set_running(True)
        threading.Thread(target=self.pump, args=(self.proc,), daemon=True).start()

    def stop(self):
        if self.is_running():
            self.stopping = True
            self.status_var.set(t("status_stopping"))
            self.proc.terminate()

    def on_close(self):
        self.stop()
        self.root.destroy()

    # ── 接收字幕程序的輸出 ──
    def pump(self, proc):
        """在背景一行一行讀字幕程序印出來的訊息，交給畫面的主執行緒顯示"""
        for line in proc.stdout:
            self.log_q.put(("line", line.rstrip()))
        self.log_q.put(("exit", proc.wait()))

    def poll(self):
        while not self.log_q.empty():
            kind, value = self.log_q.get()
            if kind == "line":
                self.append_log(value)
                self.update_status(value)
            else:
                self.on_exit(value)
        self.root.after(100, self.poll)

    def update_status(self, line):
        """認字幕程式印出來的特定訊息來更新狀態列；中英文版本都認，文字都在 i18n.py"""
        if any(text in line for text in variants("ov_loading")):
            self.status_var.set(t("status_loading"))
        elif any(line.startswith(prefix) for prefix in variants("ov_model_prefix")):
            self.status_var.set(t("status_model", line=line))
        elif any(text.strip() in line for text in variants("ov_window_open")):
            self.status_var.set(t("status_running"))

    def on_exit(self, code):
        self.set_running(False)
        if self.stopping or code == 0:
            self.status_var.set(t("status_stopped"))
        else:
            self.status_var.set(t("status_crashed", code=code))

    def append_log(self, line):
        # 下載模型時的進度條會一直印新的一行，只保留最新那一行
        progress = "%|" in line
        self.log.configure(state="normal")
        if progress and self.last_was_progress:
            self.log.delete("end-2l", "end-1l")
        self.log.insert("end", line + "\n")
        if int(self.log.index("end-1c").split(".")[0]) > MAX_LOG_LINES:
            self.log.delete("1.0", "2.0")
        self.log.see("end")
        self.log.configure(state="disabled")
        self.last_was_progress = progress

    def clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.last_was_progress = False

    def run(self):
        self.root.mainloop()


# ══════════════════════════════════════════════════════
def run_overlay(argv):
    """字幕程序的入口：啟動視窗用 --overlay 呼叫自己時走這裡"""
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:
            # 一行一行立刻送出，啟動視窗的紀錄區才會即時更新
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    import overlay_subtitle
    sys.argv = [sys.argv[0], *argv]
    overlay_subtitle.main()


def main():
    if "--overlay" in sys.argv:
        run_overlay(sys.argv[sys.argv.index("--overlay") + 1:])
        return
    if "--list-apps" in sys.argv:
        # 排查用：下拉選單抓不到程式時，用這個確認是清單抓不到還是視窗的問題
        for name in list_apps():
            print(name)
        return
    try:
        # 讓視窗在高解析度螢幕上不會糊糊的（字幕程序是另一個程序，不受影響）
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    Launcher().run()


if __name__ == "__main__":
    main()
