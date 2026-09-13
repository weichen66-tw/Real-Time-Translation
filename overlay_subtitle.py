"""
Step 3: 遊戲即時字幕懸浮視窗

畫面底部一條半透明長條，顯示最近幾句話（行數可用 --lines 調整）。
原文先出現，翻譯隨後補上。預設聽英文翻中文，--direction zh-en 改成聽中文翻英文。

字幕條可以用滑鼠拖到任何位置、拖右下角的握把調寬度；
Ctrl+Alt+方向鍵可以移動、Ctrl+Alt+L 切換鎖定（鎖定＝滑鼠穿透），遊戲中也有效。

安裝：
    py -m pip install pyaudiowpatch numpy scipy faster-whisper
    py -m pip install nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"   # 有 NVIDIA 顯示卡才需要

翻譯設定：
    預設用 Google 翻譯，不用任何設定。
    有設定 Gemini 金鑰的話，Google 沒翻到的句子會自動改用 Gemini 補翻。
    想改用 Gemini（翻得比較口語，但慢很多）：
        到 https://aistudio.google.com/apikey 申請免費 API key
        在本檔案旁邊建立 .env，寫入一行 GEMINI_API_KEY=你的key 存檔
        （.env 已列在 .gitignore，不會被上傳到 GitHub）
        執行時加上 --translator gemini

一般使用者請直接雙擊 launcher.py（或打包好的 exe），用啟動視窗操作。

用法：
    py overlay_subtitle.py                    # 一般模式，可以拖曳視窗
    py overlay_subtitle.py --direction zh-en  # 聽中文、翻成英文（預設 en-zh 聽英文翻中文）
    py overlay_subtitle.py --translator gemini  # 改用 Gemini 翻譯
    py overlay_subtitle.py --max-seconds 5    # 講不停時最多 5 秒就切一段（預設 8）
    py overlay_subtitle.py --click-through    # 一開始就鎖定（滑鼠穿透），之後可用 Ctrl+Alt+L 解鎖
    py overlay_subtitle.py --model base       # 換更快的模型
    py overlay_subtitle.py --min-rms 400      # 遊戲太吵時調高
    py overlay_subtitle.py --lines 2          # 只顯示兩行
    py overlay_subtitle.py --list-devices     # 列出可抓的音訊裝置
    py overlay_subtitle.py --device CABLE     # 只抓 VB-Cable（隔絕遊戲音效）
    py overlay_subtitle.py --app Discord.exe  # 只錄 Discord 的聲音，遊戲音效不會錄進來

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
MAX_SEGMENT_SEC = 8.0         # 對方講不停時，最多幾秒就先切一段送出（--max-seconds 可調）
MAX_SEGMENT_RANGE = (2.0, 30.0)
APP_ALIVE_CHECK_SEC = 5.0     # 用 --app 指定程式時，每隔多久確認那個程式還開著

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
MIN_WIDTH = 320               # 字幕條最窄能拉到多少
MOVE_STEP = 20                # 快速鍵一次移動幾點
HOTKEY_HELP = "快速鍵：Ctrl+Alt+方向鍵 移動字幕、Ctrl+Alt+L 鎖定／解鎖（遊戲中也有效）"

# ── Gemini 翻譯設定 ───────────────────────────────────
GEMINI_MODEL = "gemini-flash-lite-latest"   # 若失效，程式會自動列出可用型號
# 用 lite：非 lite 的型號會先「思考」，把下面 maxOutputTokens 的額度吃掉，翻譯會被切掉一半
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models"
GOOGLE_HOST = "translate.googleapis.com"
GOOGLE_RETRY_WAITS = (0.5, 1.0)   # Google 失敗後依序等這麼久再試，所以一句最多試 3 次

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
    return float_to_mono_16k(raw.astype(np.float32) / 32768.0, rate, channels)


def float_to_mono_16k(x, rate, channels):
    """
    to_mono_16k 的後半段，抽出來是因為只錄單一程式時（proc-tap）拿到的
    已經是 float32，不用再換算數值範圍。
    注意：live_transcribe.py 那邊的 to_mono_16k 仍是完整的一份，行為要一致。
    """
    if channels > 1:
        # 先切掉尾巴湊不成一組的樣本，否則 reshape 會整個錯誤，錄音就斷了
        x = x[:len(x) // channels * channels].reshape(-1, channels).mean(axis=1)
    if rate != TARGET_RATE:
        g = np.gcd(rate, TARGET_RATE)
        x = resample_poly(x, TARGET_RATE // g, rate // g)
    return x.astype(np.float32)


def find_app_pid(name):
    """
    找出這個程式的「主程序」編號。同名的程序常常有好幾個（Discord 會開一堆子程序），
    取最上層那個就好，錄音時連它底下的子程序都會一起錄到。
    """
    import psutil
    wanted = name.lower()
    # 存 info 快照而不是存程序物件：程序隨時可能結束，之後再去問它就會出錯
    procs = {p.pid: p.info for p in psutil.process_iter(["name", "ppid", "create_time"])
             if (p.info["name"] or "").lower() == wanted}
    if not procs:
        sys.exit(f"找不到正在執行的「{name}」。請先把它打開，再重新開始字幕。")
    # 父程序也同名的話代表它是子程序，只留最上層、而且開最久的那一個
    roots = [item for item in procs.items() if item[1]["ppid"] not in procs] or list(procs.items())
    return min(roots, key=lambda item: item[1]["create_time"] or 0)[0]


def capture_app_worker(pid, app_name):
    """
    只錄指定程式（含其子程序）播出來的聲音，遊戲音效、其他程式都不會錄進來。
    用的是 Windows 10 2004 之後才有的「程式音訊迴路」功能，
    proc-tap 固定給 48000 Hz、立體聲、float32 的資料。
    """
    import psutil
    from proctap import ProcessAudioCapture, STANDARD_SAMPLE_RATE, STANDARD_CHANNELS
    try:
        cap = ProcessAudioCapture(pid=pid)
        cap.start()
    except Exception as e:
        print(f"無法錄「{app_name}」的聲音（{type(e).__name__}：{e}）。"
              f"請確認它還開著，或把「只翻譯這個程式」改回不指定。")
        return
    try:
        next_check = time.time() + APP_ALIVE_CHECK_SEC
        while True:
            chunk = cap.read(timeout=1.0)
            if chunk:
                audio_q.put(float_to_mono_16k(np.frombuffer(chunk, dtype=np.float32),
                                              STANDARD_SAMPLE_RATE, STANDARD_CHANNELS))
            # 程式關掉後這裡「不會」停止收到資料，只是收到的都是無聲的，
            # 所以不能靠「沒資料」判斷，要定時主動確認那個程式還在不在
            if time.time() >= next_check:
                next_check = time.time() + APP_ALIVE_CHECK_SEC
                if not psutil.pid_exists(pid):
                    print(f"「{app_name}」已經關閉，不會再有字幕了。"
                          f"重新打開它之後，請按「停止」再按「開始」。")
                    return
    finally:
        cap.stop()
        cap.close()


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


def open_transcript():
    """
    逐字稿寫不進去也要照常跑（例如 exe 被放在 Program Files 這種不能寫入的資料夾）。
    以前寫不進去會讓整個辨識停掉，字幕完全不動又看不出原因。
    """
    try:
        out = TRANSCRIPT.open("a", encoding="utf-8")
        out.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M} =====\n")
        out.flush()
        return out
    except OSError as e:
        print(f"逐字稿存不了（{e}）。字幕照常顯示，只是不會留下紀錄。")
        return None


def stt_worker(model, min_rms, max_segment, direction):
    lang = DIRECTIONS[direction]
    buffer, speaking, silence_start, seq = [], False, None, 0
    preroll, preroll_n = deque(), 0
    out = open_transcript()

    def record(text):
        if out is None:
            return
        try:
            out.write(f"[{datetime.now():%H:%M:%S}] {text}\n")
            out.flush()
        except OSError:
            pass                # 硬碟滿了之類的，字幕還是要繼續跑

    def flush():
        nonlocal buffer, speaking, seq
        if not buffer:
            return
        audio = np.concatenate(buffer)
        buffer, speaking = [], False
        if len(audio) < TARGET_RATE * MIN_SEGMENT_SEC:
            return
        segments, _ = model.transcribe(audio, language=lang["whisper"], beam_size=1,
                                       vad_filter=True,
                                       condition_on_previous_text=False,
                                       initial_prompt=lang["hint"])
        text = " ".join(s.text.strip() for s in segments).strip()
        if not text:
            return
        seq += 1
        ui_q.put(("new", seq, text))
        trans_q.put((seq, text))
        record(text)

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

        if buffer and sum(len(b) for b in buffer) > TARGET_RATE * max_segment:
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


# 翻譯方向：命令列用 --direction 選，啟動視窗的「翻譯方向」也是傳這兩個值
#   whisper       語音辨識要聽哪種語言
#   hint          給語音辨識的開頭提示。聽中文時常會吐出簡體字，給一句繁體讓它傾向輸出繁體
#   google_sl/tl  Google 翻譯的來源語言／目標語言
#   src_font      原文（上面那行）用的字型；dst_font 是翻譯（下面那行）用的字型
DIRECTIONS = {
    "en-zh": {
        "label": "英文 → 中文",
        "whisper": "en", "hint": None,
        "google_sl": "en", "google_tl": "zh-TW",
        "src_font": EN_FONT, "dst_font": ZH_FONT,
        "prompt": ("把以下這句遊戲語音聊天的英文翻成繁體中文。"
                   "口語化、簡短，保留遊戲術語的語感。"
                   "只輸出翻譯，不要任何解釋或引號。\n\n"),
    },
    "zh-en": {
        "label": "中文 → 英文",
        "whisper": "zh", "hint": "以下是繁體中文的句子。",
        "google_sl": "zh-TW", "google_tl": "en",
        "src_font": ZH_FONT, "dst_font": EN_FONT,
        "prompt": ("把以下這句遊戲語音聊天的中文翻成英文。"
                   "口語化、簡短，保留遊戲術語的語感。"
                   "只輸出英文翻譯，不要任何解釋或引號。\n\n"),
    },
}


def gemini_translate(key, text, direction):
    body = json.dumps({
        "contents": [{"parts": [{"text": DIRECTIONS[direction]["prompt"] + text}]}],
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


def google_translate(conn, text, direction):
    """
    走 Google 翻譯網頁版背後的免費管道：不用金鑰，通常 0.1 秒內回來。
    這不是 Google 正式對外的服務，哪天被擋掉就改用 --translator gemini。

    conn 是這個翻譯執行緒自己的連線，重複使用可以省掉每次重新連線的時間
    （不重複使用每句要多等 1～2 秒）。連線閒置太久會被 Google 關掉、
    Google 偶爾也會回一次性的錯誤（例如 HTTP 500、送太快被擋的 429），
    或是回了結果卻沒有譯文。這些情況都照 GOOGLE_RETRY_WAITS 等一下再重試，
    不馬上重試是因為被擋或網路卡住時，馬上再送通常也是失敗。
    回傳 (翻譯結果, 連線)；試完都失敗就丟出錯誤，錯誤訊息是最後一次失敗的原因。
    """
    lang = DIRECTIONS[direction]
    query = urlencode({"client": "gtx", "sl": lang["google_sl"], "tl": lang["google_tl"],
                       "dt": "t", "q": text})
    error = None
    for attempt in range(len(GOOGLE_RETRY_WAITS) + 1):
        if attempt:
            time.sleep(GOOGLE_RETRY_WAITS[attempt - 1])
        if conn is None:
            conn = http.client.HTTPSConnection(GOOGLE_HOST, timeout=10)
        try:
            conn.request("GET", f"/translate_a/single?{query}")
            r = conn.getresponse()
            body = r.read()
        except (http.client.HTTPException, OSError) as e:
            conn.close()
            conn, error = None, RuntimeError(f"連線失敗（{e}）")
            continue
        if r.status != 200:
            hint = "，送太多次被暫時擋下" if r.status == 429 else ""
            error = RuntimeError(f"Google 回應 HTTP {r.status}{hint}")
            continue
        try:
            pieces = json.loads(body)[0]
            result = "".join(p[0] for p in pieces if p[0]).strip()
        except (ValueError, TypeError, IndexError):
            error = RuntimeError("Google 回應的內容看不懂")
            continue
        if result:
            return result, conn
        error = RuntimeError("Google 回了空白的結果")
    raise error


MODEL_ERROR_REPORTED = threading.Event()


def short(text, limit=30):
    """印失敗訊息時附上那句原文的開頭，方便對照是字幕上的哪一句"""
    return text if len(text) <= limit else text[:limit] + "⋯"


def try_google(conn, seq, text, direction):
    """回傳 (翻譯結果, 連線)；失敗時翻譯結果是空字串，並把原因印出來"""
    try:
        return google_translate(conn, text, direction)
    except Exception as e:
        print(f"第 {seq} 句 Google 翻譯失敗：{e}（{short(text)}）")
        return "", None


def try_gemini(key, seq, text, direction):
    """回傳翻譯結果；失敗時回傳空字串，並把原因印出來"""
    try:
        return gemini_translate(key, text, direction)
    except HTTPError as e:
        if e.code == 404 and not MODEL_ERROR_REPORTED.is_set():
            MODEL_ERROR_REPORTED.set()
            print(f"\n型號 {GEMINI_MODEL} 不可用。")
            show_available_models(key)
        print(f"第 {seq} 句 Gemini 翻譯失敗：HTTP {e.code}（{short(text)}）")
    except (KeyError, IndexError, TypeError):
        print(f"第 {seq} 句 Gemini 翻譯失敗：回應裡沒有譯文（{short(text)}）")
    except Exception as e:
        print(f"第 {seq} 句 Gemini 翻譯失敗：{e}（{short(text)}）")
    return ""


def translate_worker(translator, key, direction):
    """
    translator 是 "gemini" 就只用 Gemini。
    是 "google" 時先用 Google，失敗了而且有 Gemini 金鑰（key 不是 None），就改用 Gemini 補翻那一句。
    """
    conn = None          # 只有 Google 翻譯會用到，每個執行緒各自一條
    while True:
        seq, text = trans_q.get()
        if translator == "gemini":
            result = try_gemini(key, seq, text, direction)
        else:
            result, conn = try_google(conn, seq, text, direction)
            if not result and key:
                ui_q.put(("translated", seq, "Google 沒翻到，改用 Gemini 補翻中⋯"))
                result = try_gemini(key, seq, text, direction)
                if result:
                    print(f"第 {seq} 句已改用 Gemini 補翻成功")
        ui_q.put(("translated", seq, result))


# ══════════════════════════════════════════════════════
#  懸浮視窗
# ══════════════════════════════════════════════════════
def hotkey_worker(cmd_q):
    """
    註冊全系統的快速鍵，這樣遊戲在前景、或字幕鎖定（滑鼠穿透）時也按得到。
    Windows 的規定：註冊和收訊息一定要在同一個執行緒，所以兩件事都在這裡做。
    按下後只把「要做什麼」丟進佇列，真正動視窗由畫面主執行緒處理。
    """
    MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x0001, 0x0002, 0x4000
    WM_HOTKEY = 0x0312
    VK_LEFT, VK_UP, VK_RIGHT, VK_DOWN, VK_L = 0x25, 0x26, 0x27, 0x28, 0x4C
    # 方向鍵不加 NOREPEAT，按著不放就會一直移動
    keys = {
        1: (MOD_CONTROL | MOD_ALT, VK_LEFT, ("move", -MOVE_STEP, 0)),
        2: (MOD_CONTROL | MOD_ALT, VK_RIGHT, ("move", MOVE_STEP, 0)),
        3: (MOD_CONTROL | MOD_ALT, VK_UP, ("move", 0, -MOVE_STEP)),
        4: (MOD_CONTROL | MOD_ALT, VK_DOWN, ("move", 0, MOVE_STEP)),
        5: (MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_L, ("lock", 0, 0)),
    }
    user32 = ctypes.windll.user32
    actions = {}
    for hotkey_id, (mods, vk, action) in keys.items():
        if user32.RegisterHotKey(None, hotkey_id, mods, vk):
            actions[hotkey_id] = action
    if len(actions) < len(keys):
        print("部分快速鍵被其他程式佔用了，只有部分能用")
    if not actions:
        return
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if msg.message == WM_HOTKEY and msg.wParam in actions:
            cmd_q.put(actions[msg.wParam])


class Overlay:
    def __init__(self, n_lines, click_through, direction):
        self.n = n_lines
        self.entries = []          # [{seq, src（原文）, dst（翻譯）}]
        self.locked = click_through
        self.cmd_q = queue.Queue()  # 快速鍵按下後要做的事

        self.root = tk.Tk()
        self.root.title("Game Subtitles")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", OPACITY)
        self.root.configure(bg=BG_COLOR)

        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.h = BAR_HEIGHT_PER_LINE * n_lines
        self.w, self.x, self.y = self.load_pos(sw - SIDE_MARGIN * 2, SIDE_MARGIN,
                                               sh - self.h - BOTTOM_MARGIN)
        self.root.geometry(f"{self.w}x{self.h}+{self.x}+{self.y}")
        # 底邊固定，內容變多時視窗往上長，字幕位置才不會亂跳
        self.bottom_y = self.y + self.h

        # 上面一行永遠是原文（白字）、下面一行是翻譯（藍字），字型跟著那一行的語言走
        lang = DIRECTIONS[direction]
        self.rows = []
        for _ in range(n_lines):
            frame = tk.Frame(self.root, bg=BG_COLOR)
            frame.pack(fill="x", padx=14, pady=(5, 5))
            src = tk.Label(frame, text="", font=lang["src_font"], fg=EN_COLOR,
                           bg=BG_COLOR, anchor="w", justify="left")
            dst = tk.Label(frame, text="", font=lang["dst_font"], fg=ZH_COLOR,
                           bg=BG_COLOR, anchor="w", justify="left")
            src.pack(fill="x")
            dst.pack(fill="x")
            self.rows.append((src, dst))

        # 右下角的握把：拖它可以調字幕條的寬度
        self.grip = tk.Label(self.root, text="◢", font=("Segoe UI", 9),
                             fg="#5c6270", bg=BG_COLOR, cursor="size_nw_se")
        self.grip.bind("<Button-1>", self.resize_press)
        self.grip.bind("<B1-Motion>", self.resize_drag)
        self.grip.bind("<ButtonRelease-1>", lambda _: self.save_pos())

        self.bind_drag()
        # 等視窗真的建立好才能改它的屬性，所以延後一下
        self.root.after(300, lambda: self.set_locked(self.locked, quiet=True))
        threading.Thread(target=hotkey_worker, args=(self.cmd_q,), daemon=True).start()

        self.root.after(100, self.poll)
        self.root.after(50, self.handle_hotkeys)

    # ── 位置與大小記憶 ──
    def load_pos(self, default_w, default_x, default_y):
        """回傳 (寬度, x, y)；記住的位置不在目前螢幕上就回到預設位置"""
        try:
            d = json.loads(POS_FILE.read_text())
            w = max(MIN_WIDTH, int(d.get("w", default_w)))
            x, y = int(d["x"]), int(d["y"])
        except Exception:
            return default_w, default_x, default_y
        if not self.on_any_monitor(x + w // 2, y + self.h // 2):
            # 上次拖到的外接螢幕現在沒接，照舊開會跑到畫面外看不到；
            # 位置檔不動，下次接回那台螢幕時字幕就會回到原處
            print("上次的字幕位置不在目前的螢幕上（外接螢幕沒接？），先顯示在預設位置")
            return w, default_x, default_y
        return w, x, y

    @staticmethod
    def on_any_monitor(x, y):
        """(x, y) 這一點有沒有落在目前接著的任何一個螢幕上"""
        MONITOR_DEFAULTTONULL = 0
        user32 = ctypes.windll.user32
        user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
        user32.MonitorFromPoint.restype = wintypes.HMONITOR
        return bool(user32.MonitorFromPoint(wintypes.POINT(x, y), MONITOR_DEFAULTTONULL))

    @staticmethod
    def desktop_bounds():
        """所有螢幕合起來的範圍，用來擋住字幕被移到畫面外"""
        metric = ctypes.windll.user32.GetSystemMetrics
        left, top = metric(76), metric(77)
        return left, top, left + metric(78), top + metric(79)

    def save_pos(self):
        try:
            POS_FILE.write_text(json.dumps({"x": self.x, "y": self.y, "w": self.w}))
        except Exception:
            pass

    # ── 拖曳移動 ──
    def draggable_widgets(self):
        """
        視窗本身加上裡面每一個元件。一定要全部綁：字幕文字外面還包了一層框，
        漏掉那層的話，點到文字周邊的空白（字幕還沒出現時就是一大片空白）會拖不動。
        右下角的握把要排除，它負責調寬度。
        """
        found, stack = [self.root], [self.root]
        while stack:
            for child in stack.pop().winfo_children():
                found.append(child)
                stack.append(child)
        return [w for w in found if w is not self.grip]

    def bind_drag(self):
        def press(e):
            self._dx, self._dy = e.x_root - self.x, e.y_root - self.y

        def drag(e):
            self.place_at(e.x_root - self._dx, e.y_root - self._dy)

        def release(_):
            self.save_pos()

        for w in self.draggable_widgets():
            w.bind("<Button-1>", press)
            w.bind("<B1-Motion>", drag)
            w.bind("<ButtonRelease-1>", release)
        self.root.bind("<Escape>", lambda _: self.root.destroy())

    def place_at(self, x, y):
        """
        移到 (x, y)，但不讓它跑出所有螢幕的範圍之外。
        自己記住座標（self.x / self.y），不去問視窗現在在哪裡：
        剛下完移動指令時 Windows 還沒更新那個值，連續移動會算錯、存檔會存到舊位置。
        """
        left, top, right, bottom = self.desktop_bounds()
        h = self.root.winfo_height()
        self.x = max(left, min(x, right - self.w))
        self.y = max(top, min(y, bottom - h))
        self.root.geometry(f"+{self.x}+{self.y}")
        self.bottom_y = self.y + h

    # ── 調整寬度 ──
    def resize_press(self, e):
        self._grip_offset = e.x_root - self.w

    def resize_drag(self, e):
        left, _, right, _ = self.desktop_bounds()
        want = e.x_root - self._grip_offset
        self.w = max(MIN_WIDTH, min(want, right - left))
        self.render()          # 換行寬度也要跟著變，所以重新排版一次

    # ── 鎖定（滑鼠穿透）──
    def set_locked(self, locked, quiet=False):
        """
        鎖定 = 滑鼠穿透：滑鼠點得到後面的遊戲，但字幕不能拖也不能調大小。
        用 Ctrl+Alt+L 可以隨時切換，遊戲玩到一半也能改。
        """
        self.locked = locked
        GWL_EXSTYLE, WS_EX_LAYERED, WS_EX_TRANSPARENT = -20, 0x80000, 0x20
        user32 = ctypes.windll.user32
        # 沒有邊框的視窗有時候問不到外層視窗，那就直接用自己的
        hwnd = user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE) | WS_EX_LAYERED
        style = style | WS_EX_TRANSPARENT if locked else style & ~WS_EX_TRANSPARENT
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
        if locked:
            self.grip.place_forget()
        else:
            self.grip.place(relx=1.0, rely=1.0, anchor="se")
        if not quiet:
            print("字幕已鎖定：滑鼠可以穿透，改用快速鍵移動" if locked
                  else "字幕已解鎖：可以用滑鼠拖曳，右下角握把可以調寬度")

    # ── 快速鍵 ──
    def handle_hotkeys(self):
        while not self.cmd_q.empty():
            action, dx, dy = self.cmd_q.get()
            if action == "move":
                self.place_at(self.x + dx, self.y + dy)
                self.save_pos()
            else:
                self.set_locked(not self.locked)
        self.root.after(50, self.handle_hotkeys)

    # ── 更新畫面 ──
    def poll(self):
        changed = False
        while not ui_q.empty():
            kind, seq, text = ui_q.get()
            if kind == "new":
                self.entries.append({"seq": seq, "src": text, "dst": "翻譯中⋯"})
                self.entries = self.entries[-self.n:]
            else:
                for e in self.entries:
                    if e["seq"] == seq:
                        e["dst"] = text or "—"
            changed = True
        if changed:
            self.render()
        self.root.after(100, self.poll)

    def render(self):
        pad = self.n - len(self.entries)
        cells = [None] * pad + self.entries
        wrap = self.w - 30
        for (src_lbl, dst_lbl), item in zip(self.rows, cells):
            if item is None:
                src_lbl.config(text="")
                dst_lbl.config(text="")
            else:
                src_lbl.config(text=item["src"], wraplength=wrap)
                dst_lbl.config(text=item["dst"], wraplength=wrap)
        self.fit_height()

    def fit_height(self):
        """讓視窗高度剛好容納內容，底邊釘住不動，往上生長"""
        self.root.update_idletasks()
        need = self.root.winfo_reqheight()
        if need <= 0:
            return
        self.y = self.bottom_y - need
        self.root.geometry(f"{self.w}x{need}+{self.x}+{self.y}")

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
    app_name = opt("--app", None)
    translator = opt("--translator", "google")
    direction = opt("--direction", "en-zh")
    max_segment = float(opt("--max-seconds", MAX_SEGMENT_SEC))

    if "--list-devices" in args:
        list_loopback_devices()
        return

    if translator not in ("google", "gemini"):
        sys.exit("--translator 只能是 google 或 gemini")
    if direction not in DIRECTIONS:
        sys.exit("--direction 只能是 " + " 或 ".join(DIRECTIONS))
    print(f"翻譯方向：{DIRECTIONS[direction]['label']}")
    low, high = MAX_SEGMENT_RANGE
    if not low <= max_segment <= high:
        sys.exit(f"--max-seconds 只能在 {low:.0f} 到 {high:.0f} 之間")

    key = read_key()
    if translator == "google":
        if key:
            print("翻譯：Google 翻譯（沒翻到的句子會改用 Gemini 補翻）")
        else:
            print("翻譯：Google 翻譯（沒有 Gemini 金鑰，沒翻到的句子不會補翻）")
    else:
        if key:
            print("翻譯：Gemini 已啟用")
        else:
            print("翻譯：未設定 Gemini key，只顯示原文")
            print(f"      （要翻譯的話，在 {ENV_FILE.name} 寫入 GEMINI_API_KEY=你的key，"
                  f"或改用 Google 翻譯）")
            translator = None

    print("載入模型中，第一次執行需下載，請稍候⋯")
    model = load_model(model_name, "--cpu" in args)

    if app_name:
        pid = find_app_pid(app_name)
        print(f"聲音來源：只錄「{app_name}」這個程式（含子程序，編號 {pid}）")
        capture = threading.Thread(target=capture_app_worker,
                                   args=(pid, app_name), daemon=True)
    else:
        with pyaudio.PyAudio() as p:
            dev = find_loopback(p, device_filter)
            rate = int(dev["defaultSampleRate"])
            channels = dev["maxInputChannels"]
        print(f"裝置：{dev['name']}")
        capture = threading.Thread(target=capture_worker,
                                   args=(dev, rate, channels), daemon=True)
    capture.start()
    threading.Thread(target=stt_worker,
                     args=(model, min_rms, max_segment, direction), daemon=True).start()
    print(f"切段：安靜 {SILENCE_TO_FLUSH:.1f} 秒算講完，講不停最多 {max_segment:.0f} 秒切一段")
    if translator:
        for _ in range(2):
            threading.Thread(target=translate_worker,
                             args=(translator, key, direction), daemon=True).start()

    print("\n字幕視窗已開啟。")
    if click_through:
        print("目前是鎖定狀態：滑鼠會穿透字幕，用快速鍵移動")
    else:
        print("可以直接用滑鼠拖曳字幕，右下角的握把可以調寬度。按 Esc 關閉。")
    print(HOTKEY_HELP)
    print("位置和寬度都會自動記住")
    print("⚠️ 遊戲請設成「無邊框視窗」，全螢幕獨佔看不到字幕\n")

    Overlay(n_lines, click_through, direction).run()


if __name__ == "__main__":
    main()
