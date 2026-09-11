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

FROZEN = getattr(sys, "frozen", False)
HERE = (Path(sys.executable).resolve().parent if FROZEN
        else Path(__file__).resolve().parent)
SETTINGS_FILE = HERE / "launcher_settings.json"
ENV_FILE = HERE / ".env"

APP_TITLE = "遊戲即時字幕"
UI_FONT = ("Microsoft JhengHei", 10)
DEFAULT_DEVICE = "Windows 預設喇叭"
NO_APP = "（不指定，錄整台電腦的聲音）"
MAX_LOG_LINES = 500

# 畫面上顯示的文字 → 傳給字幕程式的參數值
TRANSLATORS = {
    "Google 翻譯（快，不用金鑰）": "google",
    "Gemini（比較口語，但慢，需要金鑰）": "gemini",
}
MODELS = {
    "快速（base，比較不準）": "base",
    "標準（small）": "small",
    "精準（large-v3-turbo，需要 NVIDIA 顯示卡）": "large-v3-turbo",
}
DEFAULTS = {"device": DEFAULT_DEVICE, "app": NO_APP, "translator": "google", "model": "small",
            "lines": 3, "min_rms": 200, "max_seconds": 8, "click_through": False}

TIPS = ("① 遊戲請設成「無邊框視窗」（全螢幕獨佔會看不到字幕）\n"
        "② 選好下面的設定，按「開始」\n"
        "③ 字幕出現後，用滑鼠把它拖到喜歡的位置（會自動記住）\n"
        "④ 位置調好後，可以勾「滑鼠穿透」再重新開始，字幕就不會擋到遊戲操作")


# ══════════════════════════════════════════════════════
#  設定與金鑰的讀寫
# ══════════════════════════════════════════════════════
def load_settings():
    """讀上次的設定；檔案不存在或壞掉就用預設值"""
    try:
        saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    return {**DEFAULTS, **{k: v for k, v in saved.items() if k in DEFAULTS}}


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


def list_audio_apps():
    """
    列出「已經發出過聲音」的程式名稱。Windows 只有在程式實際播過聲音之後
    才會有這筆紀錄，所以剛開的程式可能還不在清單裡，要按「重新整理」再看一次。
    """
    try:
        from pycaw.pycaw import AudioUtilities
        names = {s.Process.name() for s in AudioUtilities.GetAllSessions() if s.Process}
        return sorted(names, key=str.lower)
    except Exception:
        return []


def build_overlay_args(s):
    """把畫面上的設定轉成字幕程式的指令參數"""
    args = ["--overlay", "--model", s["model"], "--min-rms", str(s["min_rms"]),
            "--lines", str(s["lines"]), "--translator", s["translator"],
            "--max-seconds", str(s["max_seconds"])]
    if s["app"] != NO_APP:
        args += ["--app", s["app"]]          # 指定程式時就不看聲音來源了
    elif s["device"] != DEFAULT_DEVICE:
        args += ["--device", s["device"]]
    if s["click_through"]:
        args.append("--click-through")
    return args


def label_of(mapping, value):
    """由參數值找回畫面上顯示的文字；找不到（例如設定檔被改壞）就用第一個"""
    for label, v in mapping.items():
        if v == value:
            return label
    return next(iter(mapping))


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

        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        self.root.minsize(600, 600)
        self.root.option_add("*Font", UI_FONT)
        ttk.Style().configure(".", font=UI_FONT)

        outer = ttk.Frame(self.root, padding=14)
        outer.pack(fill="both", expand=True)
        self.build_tips(outer)
        self.build_form(outer, load_settings())
        self.build_controls(outer)
        self.build_log(outer)

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self.poll)

    # ── 畫面 ──
    def build_tips(self, parent):
        box = ttk.LabelFrame(parent, text="使用步驟", padding=10)
        box.pack(fill="x")
        ttk.Label(box, text=TIPS, justify="left").pack(anchor="w")

    def build_form(self, parent, s):
        box = ttk.LabelFrame(parent, text="設定", padding=10)
        box.pack(fill="x", pady=(10, 0))
        box.columnconfigure(1, weight=1)

        self.app_var = tk.StringVar()
        self.app_box = self.add_combo(box, 0, "只翻譯這個程式", self.app_var, [])
        self.app_box.bind("<<ComboboxSelected>>", lambda _: self.refresh_source_state())
        refresh_app = ttk.Button(box, text="重新整理", command=self.refresh_apps)
        refresh_app.grid(row=0, column=2, padx=(6, 0))
        self.inputs.append(refresh_app)
        self.add_hint(box, 1, "選 Discord 就只翻譯 Discord 的聲音，遊戲角色講話不會被翻。"
                              "\n清單裡沒有的話，先讓那個程式發出一次聲音，再按「重新整理」")
        self.refresh_apps(select=s["app"])

        self.device_var = tk.StringVar()
        self.device_box = self.add_combo(box, 2, "聲音來源", self.device_var, [])
        self.device_refresh = ttk.Button(box, text="重新整理", command=self.refresh_devices)
        self.device_refresh.grid(row=2, column=2, padx=(6, 0))
        self.device_hint = self.add_hint(box, 3, "")
        self.refresh_devices(select=s["device"])
        self.refresh_source_state()

        self.translator_var = tk.StringVar(value=label_of(TRANSLATORS, s["translator"]))
        combo = self.add_combo(box, 4, "翻譯方式", self.translator_var, list(TRANSLATORS))
        combo.bind("<<ComboboxSelected>>", lambda _: self.refresh_key_state())

        ttk.Label(box, text="Gemini 金鑰").grid(row=5, column=0, sticky="w", pady=4)
        self.key_var = tk.StringVar()
        self.key_entry = ttk.Entry(box, textvariable=self.key_var, show="•")
        self.key_entry.grid(row=5, column=1, sticky="ew", pady=4)
        self.key_hint = self.add_hint(box, 6, "")
        self.refresh_key_state()

        self.model_var = tk.StringVar(value=label_of(MODELS, s["model"]))
        self.add_combo(box, 7, "辨識準確度", self.model_var, list(MODELS))

        ttk.Label(box, text="字幕行數").grid(row=8, column=0, sticky="w", pady=4)
        self.lines_var = tk.StringVar(value=str(s["lines"]))
        spin = ttk.Spinbox(box, from_=1, to=5, width=5, textvariable=self.lines_var,
                           state="readonly")
        spin.grid(row=8, column=1, sticky="w", pady=4)
        self.inputs.append(spin)

        ttk.Label(box, text="切段秒數").grid(row=9, column=0, sticky="w", pady=4)
        self.seconds_var = tk.StringVar(value=str(s["max_seconds"]))
        seconds = ttk.Spinbox(box, from_=2, to=30, width=5, state="readonly",
                              textvariable=self.seconds_var)
        seconds.grid(row=9, column=1, sticky="w", pady=4)
        self.inputs.append(seconds)
        self.add_hint(box, 10, "講不停時最多幾秒就先送出字幕 → 調小出現得快，句子易被切斷")

        self.build_rms_row(box, 11, s["min_rms"])

        self.click_var = tk.BooleanVar(value=bool(s["click_through"]))
        check = ttk.Checkbutton(box, variable=self.click_var,
                                text="滑鼠穿透（不會擋到遊戲操作，但字幕不能拖曳，要按「停止」才能關）")
        check.grid(row=13, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self.inputs.append(check)

    def build_rms_row(self, box, row, value):
        ttk.Label(box, text="音量門檻").grid(row=row, column=0, sticky="w", pady=4)
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
        self.add_hint(box, row + 1, "遊戲音效太吵、字幕一直亂跳 → 調高；別人講話沒反應 → 調低")

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
        self.start_btn = ttk.Button(bar, text="▶ 開始", command=self.start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(bar, text="■ 停止", command=self.stop)
        self.stop_btn.pack(side="left", padx=(8, 0))
        self.stop_btn.state(["disabled"])
        self.status_var = tk.StringVar(value="尚未開始")
        ttk.Label(bar, textvariable=self.status_var).pack(side="left", padx=(14, 0))

    def build_log(self, parent):
        box = ttk.LabelFrame(parent, text="執行紀錄", padding=6)
        box.pack(fill="both", expand=True, pady=(10, 0))
        self.log = tk.Text(box, height=10, state="disabled", wrap="word",
                           font=("Consolas", 9), bg="#f7f7f7", relief="flat")
        bar = ttk.Scrollbar(box, command=self.log.yview)
        self.log.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)

    # ── 畫面狀態 ──
    def refresh_devices(self, select=None):
        names = [DEFAULT_DEVICE] + list_devices()
        self.device_box.config(values=names)
        want = select or self.device_var.get()
        self.device_var.set(want if want in names else DEFAULT_DEVICE)

    def refresh_apps(self, select=None):
        names = [NO_APP] + list_audio_apps()
        self.app_box.config(values=names)
        want = select or self.app_var.get()
        self.app_var.set(want if want in names else NO_APP)

    def refresh_source_state(self):
        """指定了程式之後，「聲音來源」就用不到了，把它變灰避免誤會"""
        whole_pc = self.app_var.get() == NO_APP
        running = self.proc is not None and self.proc.poll() is None
        for w in (self.device_box, self.device_refresh):
            w.state(["!disabled"] if whole_pc and not running else ["disabled"])
        self.device_hint.config(
            text="別人講話的聲音從哪個喇叭／耳機播出來，就選哪個" if whole_pc
            else "已指定程式，這項用不到")

    def refresh_key_state(self):
        using_gemini = TRANSLATORS[self.translator_var.get()] == "gemini"
        running = self.proc is not None and self.proc.poll() is None
        self.key_entry.state(["!disabled"] if using_gemini and not running else ["disabled"])
        if not using_gemini:
            self.key_hint.config(text="用 Google 翻譯不需要金鑰")
        elif has_gemini_key():
            self.key_hint.config(text="✔ 已設定（要換新金鑰才需要填）")
        else:
            self.key_hint.config(text="到 https://aistudio.google.com/apikey 免費申請後貼上")

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
        return {
            "device": self.device_var.get() or DEFAULT_DEVICE,
            "app": self.app_var.get() or NO_APP,
            "translator": TRANSLATORS[self.translator_var.get()],
            "model": MODELS[self.model_var.get()],
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
                messagebox.showerror(APP_TITLE, f"金鑰存檔失敗：{e}")
                return False
            self.key_var.set("")
            return True
        if has_gemini_key():
            return True
        messagebox.showwarning(APP_TITLE, "選了 Gemini 翻譯，但還沒有金鑰。\n\n"
                               "請到 https://aistudio.google.com/apikey 免費申請，"
                               "貼到「Gemini 金鑰」欄位；或改用 Google 翻譯。")
        return False

    # ── 開始／停止 ──
    def start(self):
        if self.proc and self.proc.poll() is None:
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
            messagebox.showerror(APP_TITLE, f"無法啟動字幕程式：{e}")
            return

        self.stopping = False
        self.clear_log()
        # exe 版第一次開啟時，Windows 防毒會先掃描約 2GB 的顯示卡元件，實測要等一分鐘多
        self.status_var.set("啟動中⋯（第一次開啟可能要等 1～2 分鐘，防毒軟體在檢查檔案）")
        self.set_running(True)
        threading.Thread(target=self.pump, args=(self.proc,), daemon=True).start()

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.stopping = True
            self.status_var.set("停止中⋯")
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
        if "載入模型中" in line:
            self.status_var.set("載入語音辨識模型中⋯（第一次使用要下載模型，可能要幾分鐘）")
        elif line.startswith("模型："):
            self.status_var.set(f"{line}，準備錄音⋯")
        elif "字幕視窗已開啟" in line:
            self.status_var.set("✅ 運作中：有人講話就會出現字幕")

    def on_exit(self, code):
        self.set_running(False)
        if self.stopping or code == 0:
            self.status_var.set("已停止")
        else:
            self.status_var.set(f"⚠️ 字幕程式意外結束（代碼 {code}），請看下方紀錄")

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
    try:
        # 讓視窗在高解析度螢幕上不會糊糊的（字幕程序是另一個程序，不受影響）
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    Launcher().run()


if __name__ == "__main__":
    main()
