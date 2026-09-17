"""
Step 2: 即時滾動轉錄
持續監聽系統音訊，偵測到有人講完一句話就送去辨識，印在終端機。

安裝（第一次會下載模型，約 500MB，要等幾分鐘）：
    py -m pip install faster-whisper scipy

用法：
    py live_transcribe.py                 # 預設 small 模型
    py live_transcribe.py --model base    # 更快但較不準
    py live_transcribe.py --min-rms 300   # 環境吵時調高，減少誤觸發

按 Ctrl+C 停止。逐字稿會存到腳本旁邊的 transcript.txt。
"""

import os
import sys
import time
import queue
import threading
from datetime import datetime
from pathlib import Path

from collections import deque

# 第一次執行要從 Hugging Face 下載模型。它新版的下載方式（xet）實測會卡在 0% 不動，
# 改用傳統的 HTTP 下載比較穩。一定要在 import faster_whisper 之前設定才有效。
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

import numpy as np
import pyaudiowpatch as pyaudio
from scipy.signal import resample_poly
from faster_whisper import WhisperModel

import whisper_models

CHUNK = 1024
TARGET_RATE = 16000          # Whisper 要求的取樣率

MIN_RMS = 200                # 高於這個音量算「有人在講話」
SILENCE_TO_FLUSH = 1.0       # 靜音超過這麼久就送出這一段
MIN_SEGMENT_SEC = 0.4
PREROLL_SEC = 0.5            # 永遠保留這麼長的前置音，避免吃掉句子開頭
MAX_SEGMENT_SEC = 8.0        # 講太久也要強制切段（--max-seconds 可調）

OUT_FILE = Path(__file__).resolve().parent / "transcript.txt"

audio_q: "queue.Queue[np.ndarray]" = queue.Queue()


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


def to_mono_16k(block: np.ndarray, rate: int, channels: int) -> np.ndarray:
    """int16 交錯資料 -> 16kHz 單聲道 float32"""
    x = block.astype(np.float32) / 32768.0
    if channels > 1:
        x = x.reshape(-1, channels).mean(axis=1)
    if rate != TARGET_RATE:
        g = np.gcd(rate, TARGET_RATE)
        x = resample_poly(x, TARGET_RATE // g, rate // g)
    return x.astype(np.float32)


def capture_thread(dev, rate, channels):
    """獨立執行緒：只負責把音訊塞進佇列，不做任何運算"""
    with pyaudio.PyAudio() as p:
        with p.open(format=pyaudio.paInt16,
                    channels=channels,
                    rate=rate,
                    input=True,
                    input_device_index=dev["index"],
                    frames_per_buffer=CHUNK) as stream:
            while True:
                data = stream.read(CHUNK, exception_on_overflow=False)
                raw = np.frombuffer(data, dtype=np.int16)
                audio_q.put(to_mono_16k(raw, rate, channels))


def add_nvidia_dll_dirs():
    """
    pip 裝的 NVIDIA 元件（nvidia-cublas-cu12、nvidia-cudnn-cu12）把 DLL 放在
    site-packages/nvidia/*/bin，Windows 預設不會去那裡找，要手動加進搜尋路徑，
    否則會出現「cublas64_12.dll is not found」然後退回 CPU。
    沒裝這些元件時什麼都不做。
    """
    for p in sys.path:
        for bin_dir in (Path(p) / "nvidia").glob("*/bin"):
            os.add_dll_directory(str(bin_dir))
            os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")


def load_model(name: str, force_cpu: bool = False):
    """
    先試 GPU，但必須「實際跑一次推論」成功才算數。
    只建立模型不會觸發 CUDA 函式庫載入，所以一定要試跑，
    否則會等到第一句話才炸掉。
    模型從哪裡來交給 whisper_models.resolve 決定，跟 overlay_subtitle.py 一致。
    """
    source = whisper_models.resolve(name)

    def warmup(m):
        dummy = (np.random.rand(TARGET_RATE).astype(np.float32) - 0.5) * 0.01
        list(m.transcribe(dummy, language="en", beam_size=1,
                          vad_filter=False)[0])

    if not force_cpu:
        add_nvidia_dll_dirs()
        try:
            m = WhisperModel(source, device="cuda", compute_type="float16")
            warmup(m)
            print(f"模型：{name} (GPU)")
            return m
        except Exception as e:
            print(f"GPU 不可用，改用 CPU（{type(e).__name__}）")

    m = WhisperModel(source, device="cpu", compute_type="int8")
    warmup(m)
    print(f"模型：{name} (CPU)")
    return m


def main(model_name: str, min_rms: float, force_cpu: bool = False,
         device_filter: str = None, max_segment: float = MAX_SEGMENT_SEC):
    print("載入模型中，第一次執行要下載，請稍候⋯")
    model = load_model(model_name, force_cpu)

    with pyaudio.PyAudio() as p:
        dev = find_loopback(p, device_filter)
        rate = int(dev["defaultSampleRate"])
        channels = dev["maxInputChannels"]

    print(f"裝置：{dev['name']}")
    print(f"音量門檻：{min_rms}（太常誤觸發就調高，漏字就調低）")
    print("開始監聽，Ctrl+C 停止\n")

    t = threading.Thread(target=capture_thread,
                         args=(dev, rate, channels), daemon=True)
    t.start()

    buffer: list[np.ndarray] = []
    silence_start = None
    speaking = False

    out = OUT_FILE.open("a", encoding="utf-8")
    out.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M} =====\n")
    out.flush()

    def flush():
        nonlocal buffer, speaking
        if not buffer:
            return
        audio = np.concatenate(buffer)
        buffer = []
        speaking = False
        if len(audio) < TARGET_RATE * MIN_SEGMENT_SEC:
            return

        segments, _ = model.transcribe(
            audio,
            language="en",
            beam_size=1,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        text = " ".join(s.text.strip() for s in segments).strip()
        if not text:
            return
        stamp = datetime.now().strftime("%H:%M:%S")
        print("\r" + " " * 78 + "\r", end="")
        print(f"[{stamp}] {text}")
        out.write(f"[{stamp}] {text}\n")
        out.flush()

    last_meter = 0.0
    peak_seen = 0.0
    preroll, preroll_n = deque(), 0

    try:
        while True:
            block = audio_q.get()
            rms = float(np.sqrt(np.mean(block ** 2))) * 32768
            peak_seen = max(peak_seen, rms)

            # 即時音量表：看得到收到多大訊號、門檻在哪
            now = time.time()
            if now - last_meter > 0.2:
                last_meter = now
                width = 40
                scale = max(min_rms * 3, 600)
                filled = min(int(rms / scale * width), width)
                mark = min(int(min_rms / scale * width), width - 1)
                bar = "".join("|" if i == mark else ("#" if i < filled else "-")
                              for i in range(width))
                state = "收音中" if speaking else "     "
                print(f"\r[{bar}] {int(rms):5d} 門檻{int(min_rms)} 峰值{int(peak_seen):5d} {state}",
                      end="", flush=True)

            if rms >= min_rms:
                if not speaking:
                    # 補回講話前的 0.5 秒，否則句子開頭的氣音會被切掉
                    buffer.extend(preroll)
                    preroll.clear()
                    preroll_n = 0
                buffer.append(block)
                speaking = True
                silence_start = None
            elif speaking:
                buffer.append(block)          # 保留一點尾音
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

            if buffer and len(np.concatenate(buffer)) > TARGET_RATE * max_segment:
                flush()

    except KeyboardInterrupt:
        print("\n收工，處理最後一段⋯")
        flush()
        out.close()
        print(f"逐字稿：{OUT_FILE}")
        print(f"本次最大音量：{int(peak_seen)}（門檻 {int(min_rms)}）")
        if peak_seen < 20:
            print("→ 幾乎沒收到訊號。檢查 Windows 音量、確認聲音真的在播、"
                  "或輸出裝置換了要重啟腳本")
        elif peak_seen < min_rms:
            print(f"→ 有收到聲音但都低於門檻。改用 --min-rms {max(int(peak_seen*0.4),20)} 再試")


if __name__ == "__main__":
    args = sys.argv[1:]
    model_name = "small"
    min_rms = MIN_RMS
    device_filter = None
    max_segment = MAX_SEGMENT_SEC

    if "--list-devices" in args:
        list_loopback_devices()
        sys.exit()

    for i, a in enumerate(args):
        if a == "--model" and i + 1 < len(args):
            model_name = args[i + 1]
        if a == "--min-rms" and i + 1 < len(args):
            min_rms = float(args[i + 1])
        if a == "--device" and i + 1 < len(args):
            device_filter = args[i + 1]
        if a == "--max-seconds" and i + 1 < len(args):
            max_segment = float(args[i + 1])

    main(model_name, min_rms, "--cpu" in args, device_filter, max_segment)
