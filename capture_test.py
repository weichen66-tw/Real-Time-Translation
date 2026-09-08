"""
Step 1: WASAPI Loopback 錄音驗證
目標：確認能抓到 Discord / 系統的輸出音訊

安裝：
    pip install pyaudiowpatch numpy

用法：
    python capture_test.py            # 錄 15 秒
    python capture_test.py 30         # 錄 30 秒
    python capture_test.py --list     # 只列出音訊裝置
"""

import sys
import wave
import numpy as np
import pyaudiowpatch as pyaudio

OUTPUT_FILE = "capture_test.wav"
CHUNK = 1024


def list_devices(p):
    """列出所有 WASAPI loopback 裝置，方便除錯"""
    print("\n=== 可用的 Loopback 裝置 ===")
    for dev in p.get_loopback_device_info_generator():
        print(f"  [{dev['index']}] {dev['name']}")
        print(f"       {int(dev['defaultSampleRate'])} Hz / {dev['maxInputChannels']} ch")
    print()


def find_loopback(p):
    """找到目前預設喇叭對應的 loopback 裝置"""
    try:
        wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
    except OSError:
        sys.exit("錯誤：這台機器沒有 WASAPI，請確認是 Windows。")

    speakers = p.get_device_info_by_index(wasapi["defaultOutputDevice"])

    if speakers.get("isLoopbackDevice"):
        return speakers

    for dev in p.get_loopback_device_info_generator():
        if speakers["name"] in dev["name"]:
            return dev

    sys.exit(f"錯誤：找不到「{speakers['name']}」的 loopback 裝置。\n"
             f"請用 --list 看看有哪些可用。")


def meter(rms, width=40):
    """把音量畫成文字長條，用來肉眼確認真的有收到聲音"""
    level = min(int(rms / 800 * width), width)
    return "#" * level + "-" * (width - level)


def record(seconds):
    with pyaudio.PyAudio() as p:
        dev = find_loopback(p)
        rate = int(dev["defaultSampleRate"])
        channels = dev["maxInputChannels"]

        print(f"裝置：{dev['name']}")
        print(f"格式：{rate} Hz / {channels} ch")
        print(f"錄製 {seconds} 秒 —— 現在請讓 Discord 出聲\n")

        frames = []
        peak = 0.0

        with p.open(format=pyaudio.paInt16,
                    channels=channels,
                    rate=rate,
                    input=True,
                    input_device_index=dev["index"],
                    frames_per_buffer=CHUNK) as stream:

            total = int(rate / CHUNK * seconds)
            for i in range(total):
                data = stream.read(CHUNK, exception_on_overflow=False)
                frames.append(data)

                samples = np.frombuffer(data, dtype=np.int16).astype(np.float64)
                rms = float(np.sqrt(np.mean(samples ** 2))) if samples.size else 0.0
                peak = max(peak, rms)

                if i % 8 == 0:
                    left = seconds - i * CHUNK / rate
                    print(f"\r[{meter(rms)}] {left:4.1f}s", end="", flush=True)

        print("\n")

        with wave.open(OUTPUT_FILE, "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(p.get_sample_size(pyaudio.paInt16))
            wf.setframerate(rate)
            wf.writeframes(b"".join(frames))

        print(f"已存檔：{OUTPUT_FILE}")

        if peak < 50:
            print("\n⚠️  幾乎沒收到訊號。檢查：")
            print("   1. Discord 的輸出裝置是不是就是 Windows 預設喇叭")
            print("   2. 錄的時候真的有人在講話嗎")
            print("   3. 用 --list 確認裝置選對了")
        else:
            print(f"\n✅ 有收到訊號（峰值 {peak:.0f}）")
            print("   用播放器打開 wav 確認聽得到人聲，這步就過了。")


if __name__ == "__main__":
    args = sys.argv[1:]

    if "--list" in args:
        with pyaudio.PyAudio() as p:
            list_devices(p)
        sys.exit()

    duration = 15
    for a in args:
        if a.isdigit():
            duration = int(a)

    record(duration)
