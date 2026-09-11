"""
Step 3: 遊戲即時字幕懸浮視窗

畫面底部一條半透明長條，顯示最近三句話。
英文先出現，中文翻譯隨後補上。

安裝：
    py -m pip install pyaudiowpatch numpy scipy faster-whisper
    py -m pip install nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"   # 有 NVIDIA 顯示卡才需要

翻譯設定：
    預設用 Google 翻譯，不用任何設定。
    想改用 Gemini（翻得比較口語，但慢很多）：
        到 https://aistudio.google.com/apikey 申請免費 API key
        在本檔案旁邊建立 .env，寫入一行 GEMINI_API_KEY=你的key 存檔
        （.env 已列在 .gitignore，不會被上傳到 GitHub）
        執行時加上 --translator gemini

一般使用者請直接雙擊 launcher.py（或打包好的 exe），用啟動視窗操作。

用法：
    py overlay_subtitle.py                    # 一般模式，可以拖曳視窗
    py overlay_subtitle.py --translator gemini  # 改用 Gemini 翻譯
    py overlay_subtitle.py --click-through    # 滑鼠穿透，調好位置後用這個
    py overlay_subtitle.py --model base       # 換更快的模型
    py overlay_subtitle.py --min-rms 400      # 遊戲太吵時調高
    py overlay_subtitle.py --lines 2          # 只顯示兩行
    py overlay_subtitle.py --list-devices     # 列出可抓的音訊裝置
    py overlay_subtitle.py --device CABLE     # 只抓 VB-Cable（隔絕遊戲音效）

⚠️ 遊戲必須設成「無邊框視窗」，全螢幕獨佔模式看不到字幕。
"""

import os
import sys
import json
import time
import queue
import ctypes
from ctypes import wintypes
import threading
import http.client
import tkinter as tk
from pathlib import Path
from datetime import datetime
from urllib import request as urlrequest
from urllib.error import HTTPError
from urllib.parse import urlencode

from collections import deque

# 第一次執行要從 Hugging Face 下載模型。它新版的下載方式（xet）實測會卡在 0% 不動，
# 改用傳統的 HTTP 下載比較穩。一定要在 import faster_whisper 之前設定才有效。
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

import numpy as np
import pyaudiowpatch as pyaudio
from scipy.signal import resample_poly
from faster_whisper import WhisperModel

# ── 音訊設定 ──────────────────────────────────────────
CHUNK = 1024
TARGET_RATE = 16000
SILENCE_TO_FLUSH = 1.0        # 靜音多久算一句講完
MIN_SEGMENT_SEC = 0.4
PREROLL_SEC = 0.5             # 永遠保留這麼長的前置音，避免吃掉句子開頭
MAX_SEGMENT_SEC = 15.0

# ── 外觀設定 ──────────────────────────────────────────
BG_COLOR = "#0b0b0f"
EN_COLOR = "#ffffff"
ZH_COLOR = "#8fd6ff"
EN_FONT = ("Consolas", 14)
ZH_FONT = ("Microsoft JhengHei", 13)
OPACITY = 0.82
BAR_HEIGHT_PER_LINE = 52
SIDE_MARGIN = 120
BOTTOM_MARGIN = 90

# ── Gemini 翻譯設定 ───────────────────────────────────
GEMINI_MODEL = "gemini-flash-lite-latest"   # 若失效，程式會自動列出可用型號
# 用 lite：非 lite 的型號會先「思考」，把下面 maxOutputTokens 的額度吃掉，翻譯會被切掉一半
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models"
GOOGLE_HOST = "translate.googleapis.com"

# 打包成 exe 時，.env、字幕位置、逐字稿要放在 exe 旁邊，而不是解壓縮用的暫存資料夾
HERE = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parent)
ENV_FILE = HERE / ".env"
POS_FILE = HERE / "overlay_pos.json"
TRANSCRIPT = HERE / "transcript.txt"

audio_q: "queue.Queue[np.ndarray]" = queue.Queue()
trans_q: "queue.Queue[tuple[int, str]]" = queue.Queue()
ui_q: "queue.Queue[tuple]" = queue.Queue()


# ══════════════════════════════════════════════════════
#  音訊擷取
# ══════════════════════════════════════════════════════
def list_loopback_devices():
    """列出所有可抓的 loopback 裝置"""
    with pyaudio.PyAudio() as p:
        print("\n=== 可用的 loopback 裝置 ===")
        for dev in p.get_loopback_device_info_generator():
            print(f"  {dev['name']}")
        print("\n用 --device 加上名稱的一部分即可指定，例如：--device CABLE\n")


def find_loopback(p, name_filter=None):
    """
    name_filter 有給就找名稱包含該字串的裝置（例如 CABLE），
    沒給就用 Windows 目前的預設輸出裝置。
    """
    if name_filter:
        for dev in p.get_loopback_device_info_generator():
            if name_filter.lower() in dev["name"].lower():
                return dev
        sys.exit(f"找不到名稱包含「{name_filter}」的 loopback 裝置。\n"
                 f"用 --list-devices 看看有哪些可選。")

    wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
    speakers = p.get_device_info_by_index(wasapi["defaultOutputDevice"])
    if speakers.get("isLoopbackDevice"):
        return speakers
    for dev in p.get_loopback_device_info_generator():
        if speakers["name"] in dev["name"]:
            return dev
    sys.exit("找不到 loopback 裝置，請先確認 capture_test.py 跑得起來")


def to_mono_16k(raw, rate, channels):
    x = raw.astype(np.float32) / 32768.0
    if channels > 1:
        x = x.reshape(-1, channels).mean(axis=1)
    if rate != TARGET_RATE:
        g = np.gcd(rate, TARGET_RATE)
        x = resample_poly(x, TARGET_RATE // g, rate // g)
    return x.astype(np.float32)


def capture_worker(dev, rate, channels):
    with pyaudio.PyAudio() as p:
        with p.open(format=pyaudio.paInt16, channels=channels, rate=rate,
                    input=True, input_device_index=dev["index"],
                    frames_per_buffer=CHUNK) as stream:
            while True:
                data = stream.read(CHUNK, exception_on_overflow=False)
                audio_q.put(to_mono_16k(np.frombuffer(data, dtype=np.int16),
                                        rate, channels))


# ══════════════════════════════════════════════════════
#  語音辨識
# ══════════════════════════════════════════════════════
def add_nvidia_dll_dirs():
    """
    pip 裝的 NVIDIA 元件（nvidia-cublas-cu12、nvidia-cudnn-cu12）把 DLL 放在
    site-packages/nvidia/*/bin，Windows 預設不會去那裡找，要手動加進搜尋路徑，
    否則會出現「cublas64_12.dll is not found」然後退回 CPU。
    沒裝這些元件時什麼都不做。打包成 exe 後這些 DLL 也在 sys.path 底下，一樣找得到。
    """
    for p in sys.path:
        for bin_dir in (Path(p) / "nvidia").glob("*/bin"):
            os.add_dll_directory(str(bin_dir))
            os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")


def load_model(name, force_cpu=False):
    """
    先試 GPU，但必須「實際跑一次推論」成功才算數。
    只建立模型不會觸發 CUDA 函式庫載入，所以一定要試跑。
    """
    def warmup(m):
        dummy = (np.random.rand(TARGET_RATE).astype(np.float32) - 0.5) * 0.01
        list(m.transcribe(dummy, language="en", beam_size=1,
                          vad_filter=False)[0])

    if not force_cpu:
        add_nvidia_dll_dirs()
        try:
            m = WhisperModel(name, device="cuda", compute_type="float16")
            warmup(m)
            print(f"模型：{name} (GPU)")
            return m
        except Exception as e:
            print(f"GPU 不可用，改用 CPU（{type(e).__name__}）")

    m = WhisperModel(name, device="cpu", compute_type="int8")
    warmup(m)
    print(f"模型：{name} (CPU)")
    return m


def stt_worker(model, min_rms):
    buffer, speaking, silence_start, seq = [], False, None, 0
    preroll, preroll_n = deque(), 0
    out = TRANSCRIPT.open("a", encoding="utf-8")
    out.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M} =====\n")
    out.flush()

    def flush():
        nonlocal buffer, speaking, seq
        if not buffer:
            return
        audio = np.concatenate(buffer)
        buffer, speaking = [], False
        if len(audio) < TARGET_RATE * MIN_SEGMENT_SEC:
            return
        segments, _ = model.transcribe(audio, language="en", beam_size=1,
                                       vad_filter=True,
                                       condition_on_previous_text=False)
        text = " ".join(s.text.strip() for s in segments).strip()
        if not text:
            return
        seq += 1
        ui_q.put(("new", seq, text))
        trans_q.put((seq, text))
        out.write(f"[{datetime.now():%H:%M:%S}] {text}\n")
        out.flush()

    while True:
        block = audio_q.get()
        rms = float(np.sqrt(np.mean(block ** 2))) * 32768
        if rms >= min_rms:
            if not speaking:
                # 把講話前那 0.5 秒補進來，否則開頭的氣音會被切掉
                buffer.extend(preroll)
                preroll.clear()
                preroll_n = 0
            buffer.append(block)
            speaking, silence_start = True, None
        elif speaking:
            buffer.append(block)
            if silence_start is None:
                silence_start = time.time()
            elif time.time() - silence_start >= SILENCE_TO_FLUSH:
                flush()
                silence_start = None
        else:
            preroll.append(block)
            preroll_n += len(block)
            while preroll_n > TARGET_RATE * PREROLL_SEC and preroll:
                preroll_n -= len(preroll.popleft())

        if buffer and sum(len(b) for b in buffer) > TARGET_RATE * MAX_SEGMENT_SEC:
            flush()


# ══════════════════════════════════════════════════════
#  翻譯
# ══════════════════════════════════════════════════════
def read_key():
    """先找 .env 裡的 GEMINI_API_KEY=...，找不到再讀系統環境變數"""
    if ENV_FILE.exists():
        # utf-8-sig：記事本存檔可能在開頭多塞一個看不見的字元，這樣可以自動略過
        for line in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
            name, sep, value = line.partition("=")
            if sep and name.strip() == "GEMINI_API_KEY":
                k = value.strip().strip('"').strip("'")
                if k:
                    return k
    return os.environ.get("GEMINI_API_KEY", "").strip() or None


PROMPT = (
    "把以下這句遊戲語音聊天的英文翻成繁體中文。"
    "口語化、簡短，保留遊戲術語的語感。"
    "只輸出翻譯，不要任何解釋或引號。\n\n"
)


def gemini_translate(key, text):
    body = json.dumps({
        "contents": [{"parts": [{"text": PROMPT + text}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 200},
    }).encode()
    url = f"{GEMINI_URL}/{GEMINI_MODEL}:generateContent?key={key}"
    req = urlrequest.Request(url, data=body,
                             headers={"Content-Type": "application/json"})
    with urlrequest.urlopen(req, timeout=15) as r:
        data = json.load(r)
    return data["candidates"][0]["content"]["parts"][0]["text"].strip()


def show_available_models(key):
    """型號失效時，印出這把 key 能用的型號，方便自己改常數"""
    try:
        with urlrequest.urlopen(f"{GEMINI_URL}?key={key}", timeout=15) as r:
            data = json.load(r)
        print("\n可用的型號（請修改檔案上方的 GEMINI_MODEL）：")
        for m in data.get("models", []):
            if "generateContent" in m.get("supportedGenerationMethods", []):
                print("   ", m["name"].replace("models/", ""))
        print()
    except Exception as e:
        print("列出型號失敗：", e)


def google_translate(conn, text):
    """
    走 Google 翻譯網頁版背後的免費管道：不用金鑰，通常 0.1 秒內回來。
    這不是 Google 正式對外的服務，哪天被擋掉就改用 --translator gemini。

    conn 是這個翻譯執行緒自己的連線，重複使用可以省掉每次重新連線的時間
    （不重複使用每句要多等 1～2 秒）。連線閒置太久會被 Google 關掉、
    Google 偶爾也會回一次性的錯誤（例如 HTTP 500），這兩種情況都自動重試一次。
    回傳 (翻譯結果, 連線)。
    """
    query = urlencode({"client": "gtx", "sl": "en", "tl": "zh-TW",
                       "dt": "t", "q": text})
    error = None
    for _ in range(2):
        if conn is None:
            conn = http.client.HTTPSConnection(GOOGLE_HOST, timeout=10)
        try:
            conn.request("GET", f"/translate_a/single?{query}")
            r = conn.getresponse()
            body = r.read()
        except (http.client.HTTPException, OSError) as e:
            conn.close()
            conn, error = None, e
            continue
        if r.status == 200:
            pieces = json.loads(body)[0]
            return "".join(p[0] for p in pieces if p[0]).strip(), conn
        error = RuntimeError(f"Google 翻譯回應 HTTP {r.status}")
    raise error


MODEL_ERROR_REPORTED = threading.Event()
GOOGLE_ERROR_REPORTED = threading.Event()


def translate_worker(translator, key):
    conn = None          # 只有 Google 翻譯會用到，每個執行緒各自一條
    while True:
        seq, text = trans_q.get()
        try:
            if translator == "gemini":
                zh = gemini_translate(key, text)
            else:
                zh, conn = google_translate(conn, text)
            ui_q.put(("zh", seq, zh))
        except HTTPError as e:
            if e.code == 404 and not MODEL_ERROR_REPORTED.is_set():
                MODEL_ERROR_REPORTED.set()
                print(f"\n型號 {GEMINI_MODEL} 不可用。")
                show_available_models(key)
            ui_q.put(("zh", seq, ""))
        except Exception as e:
            if translator == "google" and not GOOGLE_ERROR_REPORTED.is_set():
                GOOGLE_ERROR_REPORTED.set()
                print(f"\nGoogle 翻譯失敗（{e}）。一直失敗的話請改用 Gemini 翻譯。")
            ui_q.put(("zh", seq, ""))


# ══════════════════════════════════════════════════════
#  懸浮視窗
# ══════════════════════════════════════════════════════
class Overlay:
    def __init__(self, n_lines, click_through):
        self.n = n_lines
        self.entries = []          # [{seq, en, zh}]

        self.root = tk.Tk()
        self.root.title("Game Subtitles")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", OPACITY)
        self.root.configure(bg=BG_COLOR)

        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.w = sw - SIDE_MARGIN * 2
        self.h = BAR_HEIGHT_PER_LINE * n_lines
        x, y = self.load_pos(default=(SIDE_MARGIN, sh - self.h - BOTTOM_MARGIN))
        self.root.geometry(f"{self.w}x{self.h}+{x}+{y}")
        # 底邊固定，內容變多時視窗往上長，字幕位置才不會亂跳
        self.bottom_y = y + self.h

        self.rows = []
        for _ in range(n_lines):
            frame = tk.Frame(self.root, bg=BG_COLOR)
            frame.pack(fill="x", padx=14, pady=(5, 5))
            en = tk.Label(frame, text="", font=EN_FONT, fg=EN_COLOR,
                          bg=BG_COLOR, anchor="w", justify="left")
            zh = tk.Label(frame, text="", font=ZH_FONT, fg=ZH_COLOR,
                          bg=BG_COLOR, anchor="w", justify="left")
            en.pack(fill="x")
            zh.pack(fill="x")
            self.rows.append((en, zh))

        if click_through:
            self.root.after(300, self.make_click_through)
        else:
            self.bind_drag()

        self.root.after(100, self.poll)

    # ── 位置記憶 ──
    def load_pos(self, default):
        try:
            d = json.loads(POS_FILE.read_text())
            x, y = int(d["x"]), int(d["y"])
        except Exception:
            return default
        if not self.on_any_monitor(x + self.w // 2, y + self.h // 2):
            # 上次拖到的外接螢幕現在沒接，照舊開會跑到畫面外看不到；
            # 位置檔不動，下次接回那台螢幕時字幕就會回到原處
            print("上次的字幕位置不在目前的螢幕上（外接螢幕沒接？），先顯示在預設位置")
            return default
        return x, y

    @staticmethod
    def on_any_monitor(x, y):
        """(x, y) 這一點有沒有落在目前接著的任何一個螢幕上"""
        MONITOR_DEFAULTTONULL = 0
        user32 = ctypes.windll.user32
        user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
        user32.MonitorFromPoint.restype = wintypes.HMONITOR
        return bool(user32.MonitorFromPoint(wintypes.POINT(x, y), MONITOR_DEFAULTTONULL))

    def save_pos(self):
        try:
            POS_FILE.write_text(json.dumps(
                {"x": self.root.winfo_x(), "y": self.root.winfo_y()}))
        except Exception:
            pass

    # ── 拖曳移動 ──
    def bind_drag(self):
        def press(e):
            self._dx, self._dy = e.x_root - self.root.winfo_x(), \
                                 e.y_root - self.root.winfo_y()

        def drag(e):
            self.root.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

        def release(_):
            self.bottom_y = self.root.winfo_y() + self.root.winfo_height()
            self.save_pos()

        for w in [self.root] + [x for r in self.rows for x in r]:
            w.bind("<Button-1>", press)
            w.bind("<B1-Motion>", drag)
            w.bind("<ButtonRelease-1>", release)
        self.root.bind("<Escape>", lambda _: self.root.destroy())

    # ── 滑鼠穿透 ──
    def make_click_through(self):
        GWL_EXSTYLE, WS_EX_LAYERED, WS_EX_TRANSPARENT = -20, 0x80000, 0x20
        hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
        style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        ctypes.windll.user32.SetWindowLongW(
            hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TRANSPARENT)

    # ── 更新畫面 ──
    def poll(self):
        changed = False
        while not ui_q.empty():
            kind, seq, text = ui_q.get()
            if kind == "new":
                self.entries.append({"seq": seq, "en": text, "zh": "翻譯中⋯"})
                self.entries = self.entries[-self.n:]
            else:
                for e in self.entries:
                    if e["seq"] == seq:
                        e["zh"] = text or "—"
            changed = True
        if changed:
            self.render()
        self.root.after(100, self.poll)

    def render(self):
        pad = self.n - len(self.entries)
        cells = [None] * pad + self.entries
        wrap = self.w - 30
        for (en_lbl, zh_lbl), item in zip(self.rows, cells):
            if item is None:
                en_lbl.config(text="")
                zh_lbl.config(text="")
            else:
                en_lbl.config(text=item["en"], wraplength=wrap)
                zh_lbl.config(text=item["zh"], wraplength=wrap)
        self.fit_height()

    def fit_height(self):
        """讓視窗高度剛好容納內容，底邊釘住不動，往上生長"""
        self.root.update_idletasks()
        need = self.root.winfo_reqheight()
        if need <= 0:
            return
        x = self.root.winfo_x()
        self.root.geometry(f"{self.w}x{need}+{x}+{self.bottom_y - need}")

    def run(self):
        self.root.mainloop()


# ══════════════════════════════════════════════════════
def main():
    args = sys.argv[1:]

    def opt(name, default):
        if name in args:
            i = args.index(name)
            if i + 1 < len(args):
                return args[i + 1]
        return default

    model_name = opt("--model", "small")
    min_rms = float(opt("--min-rms", 200))
    n_lines = int(opt("--lines", 3))
    click_through = "--click-through" in args
    device_filter = opt("--device", None)
    translator = opt("--translator", "google")

    if "--list-devices" in args:
        list_loopback_devices()
        return

    if translator not in ("google", "gemini"):
        sys.exit("--translator 只能是 google 或 gemini")

    key = None
    if translator == "google":
        print("翻譯：Google 翻譯")
    else:
        key = read_key()
        if key:
            print("翻譯：Gemini 已啟用")
        else:
            print("翻譯：未設定 Gemini key，只顯示英文")
            print(f"      （要翻譯的話，在 {ENV_FILE.name} 寫入 GEMINI_API_KEY=你的key，"
                  f"或改用 Google 翻譯）")
            translator = None

    print("載入模型中，第一次執行需下載，請稍候⋯")
    model = load_model(model_name, "--cpu" in args)

    with pyaudio.PyAudio() as p:
        dev = find_loopback(p, device_filter)
        rate = int(dev["defaultSampleRate"])
        channels = dev["maxInputChannels"]
    print(f"裝置：{dev['name']}")

    threading.Thread(target=capture_worker,
                     args=(dev, rate, channels), daemon=True).start()
    threading.Thread(target=stt_worker,
                     args=(model, min_rms), daemon=True).start()
    if translator:
        for _ in range(2):
            threading.Thread(target=translate_worker,
                             args=(translator, key), daemon=True).start()

    print("\n字幕視窗已開啟。")
    if click_through:
        print("滑鼠穿透模式：無法拖曳，關閉請回這個視窗按 Ctrl+C")
    else:
        print("可以用滑鼠拖曳視窗，位置會自動記住。按 Esc 關閉。")
    print("⚠️ 遊戲請設成「無邊框視窗」，全螢幕獨佔看不到字幕\n")

    Overlay(n_lines, click_through).run()


if __name__ == "__main__":
    main()
