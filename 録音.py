import math
import os
import sys
import struct
import threading
import traceback
import wave
from pathlib import Path
from datetime import datetime
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

import pyaudiowpatch as pyaudio
from faster_whisper import WhisperModel


# =========================
# 設定
# =========================
APP_TITLE = "文字起こしレコーダー"

BASE_DIR = Path.cwd() / "mtg_records"
BASE_DIR.mkdir(parents=True, exist_ok=True)

ERROR_LOG = BASE_DIR / "error_log.txt"

FORMAT = pyaudio.paInt16
CHUNK = 2048
LANGUAGE = "ja"

MODEL_SIZE = "medium"
COMPUTE_TYPE = "int8"


def ensure_error_log():
    try:
        if not ERROR_LOG.exists():
            ERROR_LOG.write_text("error log initialized\n", encoding="utf-8")
    except Exception:
        pass


def write_error_log(title, error):
    try:
        with open(ERROR_LOG, "a", encoding="utf-8") as f:
            f.write("\n" + "=" * 80 + "\n")
            f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - {title}\n")
            f.write("=" * 80 + "\n")
            f.write(f"{repr(error)}\n\n")
            f.write(traceback.format_exc())
            f.write("\n")
    except Exception:
        pass


def _safe_messagebox(method, title, message):
    try:
        method(title, message)
    except Exception:
        pass


def safe_messagebox_info(title, message):
    _safe_messagebox(messagebox.showinfo, title, message)


def safe_messagebox_error(title, message):
    _safe_messagebox(messagebox.showerror, title, message)


# =========================
# 録音
# =========================
class AudioRecorder:
    def __init__(self, log_func):
        self.log = log_func

        self.p = None

        self.system_stream = None
        self.mic_stream = None

        self.system_thread = None
        self.mic_thread = None

        self.is_recording = False

        self.system_level = 0
        self.mic_level = 0

        self.system_frames = []
        self.mic_frames = []
        self.frame_lock = threading.Lock()

        self.system_device = None
        self.mic_device = None

        self.system_rate = None
        self.mic_rate = None
        self.system_channels = None
        self.mic_channels = None

        self.output_dir = None
        self.system_wav = None
        self.mic_wav = None
        self.txt_out = None

    @staticmethod
    def list_devices():
        p = pyaudio.PyAudio()
        system_devices = []
        mic_devices = []

        try:
            # 相手音声: loopback
            for dev in p.get_loopback_device_info_generator():
                name = dev.get("name", "")
                rate = int(dev.get("defaultSampleRate", 0))
                ch = int(dev.get("maxInputChannels", 0))

                # Bluetoothヘッドセット系loopbackは落ちやすいので除外
                

                if ch <= 0:
                    continue
                if rate < 44100:
                    continue

                system_devices.append({
                    "index": int(dev["index"]),
                    "name": name,
                    "rate": rate,
                    "channels": min(2, max(1, ch)),
                })

            # 自分の声: マイク
            for i in range(p.get_device_count()):
                dev = p.get_device_info_by_index(i)

                name = dev.get("name", "")
                rate = int(dev.get("defaultSampleRate", 0))
                ch = int(dev.get("maxInputChannels", 0))

                if ch <= 0:
                    continue
                if dev.get("isLoopbackDevice", False):
                    continue
                if "Microsoft サウンド マッパー" in name:
                    continue
                if "プライマリ サウンド キャプチャ ドライバー" in name:
                    continue
                if rate < 44100:
                    continue
                if ch < 2:
                    continue

                mic_devices.append({
                    "index": int(dev["index"]),
                    "name": name,
                    "rate": rate,
                    "channels": min(2, max(1, ch)),
                })

        except Exception as e:
            write_error_log("AudioRecorder.list_devices error", e)
            raise

        finally:
            p.terminate()

        def system_priority(d):
            name = d["name"]
            score = 0

            if d["rate"] == 48000:
                score += 60
            if d["rate"] == 44100:
                score += 40
            if "スピーカー" in name or "Speaker" in name:
                score += 40
            if "Realtek" in name:
                score += 30

            return score

        def mic_priority(d):
            name = d["name"]
            score = 0

            if "Realtek" in name:
                score += 100
            if d["rate"] == 48000:
                score += 60
            if d["rate"] == 44100:
                score += 40
            if d["channels"] >= 2:
                score += 30
            if "SOUNDPEATS" in name:
                score += 20
            if "ヘッドセット" in name:
                score += 10

            return score

        system_devices = sorted(system_devices, key=system_priority, reverse=True)[:3]
        mic_devices = sorted(mic_devices, key=mic_priority, reverse=True)[:3]

        return system_devices, mic_devices

    def start(self, system_device_index, mic_device_index):
        try:
            if self.is_recording:
                return

            self.p = pyaudio.PyAudio()

            try:
                self.system_device = self.p.get_device_info_by_index(system_device_index)
                self.mic_device = self.p.get_device_info_by_index(mic_device_index)
            except Exception as e:
                write_error_log("AudioRecorder.start device lookup error", e)
                raise RuntimeError("選択デバイス情報の取得に失敗しました。デバイス再読み込み後に再実行してください。") from e

            self.system_rate = int(self.system_device["defaultSampleRate"])
            self.mic_rate = int(self.mic_device["defaultSampleRate"])

            self.system_channels = min(
                2,
                max(1, int(self.system_device["maxInputChannels"]))
            )
            self.mic_channels = min(
                2,
                max(1, int(self.mic_device["maxInputChannels"]))
            )

            now = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.output_dir = BASE_DIR / now
            self.output_dir.mkdir(parents=True, exist_ok=True)

            self.system_wav = self.output_dir / "system.wav"
            self.mic_wav = self.output_dir / "mic.wav"
            self.txt_out = self.output_dir / "transcript.txt"

            self.system_frames = []
            self.mic_frames = []
            self.system_level = 0
            self.mic_level = 0

            self.log(
                f"相手音声 open: index={system_device_index}, "
                f"rate={self.system_rate}, ch={self.system_channels}, "
                f"name={self.system_device['name']}"
            )
            self.log(
                f"マイク open: index={mic_device_index}, "
                f"rate={self.mic_rate}, ch={self.mic_channels}, "
                f"name={self.mic_device['name']}"
            )

            self.system_stream = self._open_stream_with_fallback(
                device_index=system_device_index,
                rate=self.system_rate,
                channels=self.system_channels,
                label="相手音声"
            )

            self.mic_stream = self._open_stream_with_fallback(
                device_index=mic_device_index,
                rate=self.mic_rate,
                channels=self.mic_channels,
                label="マイク"
            )

            self.is_recording = True

            self.system_thread = threading.Thread(
                target=self._system_loop,
                daemon=True
            )
            self.mic_thread = threading.Thread(
                target=self._mic_loop,
                daemon=True
            )

            self.system_thread.start()
            self.mic_thread.start()

            self.log(f"録音保存先: {self.output_dir}")
            self.log("録音を開始しました。")

        except Exception as e:
            write_error_log("AudioRecorder.start error", e)
            self.stop()
            raise

    def _open_stream_with_fallback(self, device_index, rate, channels, label):


    def open_output_folder(self):
        folder = self.last_output_dir or BASE_DIR

        try:
            folder = Path(folder).resolve()

            if not folder.exists():
                folder = BASE_DIR.resolve()

            os.startfile(str(folder))

        except Exception as e:
            write_error_log("App.open_output_folder error", e)
            self.add_log(f"フォルダを開けませんでした: {e}")
            safe_messagebox_error(
                "エラー",
                f"フォルダを開けませんでした。\n\n{e}"
            )

    def open_error_log(self):
        try:
            ensure_error_log()
            os.startfile(str(ERROR_LOG.resolve()))

        except Exception as e:
            self.add_log(f"エラーログを開けませんでした: {e}")
            safe_messagebox_error(
                "エラー",
                f"エラーログを開けませんでした。\n\n{e}"
            )

    def enable_controls(self):
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.reload_btn.config(state="normal")
        self.system_combo.config(state="readonly")
        self.mic_combo.config(state="readonly")

    def schedule_preview_level_meter(self):
        if self.preview_start_job:
            self.root.after_cancel(self.preview_start_job)
            self.preview_start_job = None
        self.preview_start_job = self.root.after(
            self.preview_delay_ms,
            self.start_preview_level_meter
        )

    def _open_preview_stream(self, device):
        last_error = None

            try:
                return self.preview_audio.open(
                    format=FORMAT,
                    channels=channels,
                    rate=device["rate"],
                    input=True,
                    input_device_index=device["index"],
                    frames_per_buffer=CHUNK,
                )
            except Exception as e:
                last_error = e


    @staticmethod
    def _is_bluetooth_or_handsfree(name):
        lower = (name or "").lower()
        keywords = [
            "hands-free",
            "hands free",
            "hfp",
            "hsp",
            "bluetooth",
            "ヘッドセット",
            "ハンズフリー",
        ]
        return any(k in lower for k in keywords)

    def start_preview_level_meter(self):
        self.preview_start_job = None
        self.stop_preview_level_meter()
        if self.recorder.is_recording:
            return

        system_pos = self.system_combo.current()
        mic_pos = self.mic_combo.current()
        if system_pos < 0 or mic_pos < 0:
            self.reset_level_meter()
            return

        try:
            self.preview_audio = pyaudio.PyAudio()
            system_device = self.system_devices[system_pos]
            mic_device = self.mic_devices[mic_pos]
            self.preview_streams = []

            # Bluetooth/ハンズフリー機器は profile 切替時にデバイスが再初期化され、
            # system + mic を同時に preview open すると不安定になることがあるため
            # preview はマイク側優先にする。
            system_name = system_device.get("name", "")
            mic_name = mic_device.get("name", "")
            unstable_pair = (
                self._is_bluetooth_or_handsfree(system_name)
                or self._is_bluetooth_or_handsfree(mic_name)
            )

            if unstable_pair:
                self.add_log("Bluetooth/ハンズフリー機器を検出。事前感度表示はマイクのみ有効化します。")


            self.preview_running = True
            self.preview_thread = threading.Thread(target=self._preview_loop, daemon=True)
            self.preview_thread.start()
            self.update_level_meter()
        except Exception as e:

            self.stop_preview_level_meter()
            self.add_log(f"入力レベルの事前表示に失敗: {e}")

    def _preview_loop(self):
        while self.preview_running and not self.recorder.is_recording:
            try:
                for attr, stream in self.preview_streams:
                    data = stream.read(CHUNK, exception_on_overflow=False)
                    setattr(self.recorder, attr, self.recorder._calc_level(data))
            except Exception as e:
                write_error_log("App._preview_loop error", e)
                self.add_log(f"入力レベル監視エラー: {e}")
                break

    def stop_preview_level_meter(self):
        self.preview_running = False
        if self.preview_start_job:
            self.root.after_cancel(self.preview_start_job)
            self.preview_start_job = None
        if self.preview_thread:
            self.preview_thread.join(timeout=1)
            self.preview_thread = None

        for _, stream in self.preview_streams:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass
        self.preview_streams = []

        if self.preview_audio:
            try:
                self.preview_audio.terminate()
            except Exception:
                pass
            self.preview_audio = None

    def update_timer(self):
        minutes = self.elapsed_seconds // 60
        seconds = self.elapsed_seconds % 60

        self.timer_var.set(f"録音時間: {minutes:02d}:{seconds:02d}")

        self.elapsed_seconds += 1
        self.timer_job = self.root.after(1000, self.update_timer)

    def update_level_meter(self):
        if not self.recorder.is_recording and not self.preview_running:
            self.reset_level_meter()
            return

        system_level = self.recorder.system_level
        mic_level = self.recorder.mic_level

        self.system_level_bar["value"] = system_level
        self.mic_level_bar["value"] = mic_level

        self.system_level_label.config(text=f"{system_level}%")
        self.mic_level_label.config(text=f"{mic_level}%")

        self.level_job = self.root.after(100, self.update_level_meter)

    def reset_level_meter(self):
        self.system_level_bar["value"] = 0
        self.mic_level_bar["value"] = 0
        self.system_level_label.config(text="0%")
        self.mic_level_label.config(text="0%")

    def add_log(self, text):
        if threading.get_ident() != self.main_thread_id:
            self.root.after(0, lambda: self.add_log(text))
            return

        now = datetime.now().strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{now}] {text}\n")
        self.log_text.see("end")
        self.root.update_idletasks()

    def on_close(self):
        try:
            if self.timer_job:
                self.root.after_cancel(self.timer_job)

            if self.level_job:
                self.root.after_cancel(self.level_job)

            self.stop_preview_level_meter()
            self.recorder.stop()

        except Exception as e:
            write_error_log("App.on_close error", e)

        finally:
            self.root.destroy()


def global_exception_handler(exc_type, exc_value, exc_traceback):
    try:
        ensure_error_log()

        with open(ERROR_LOG, "a", encoding="utf-8") as f:
            f.write("\n" + "=" * 80 + "\n")
            f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - GLOBAL ERROR\n")
            f.write("=" * 80 + "\n")
            traceback.print_exception(exc_type, exc_value, exc_traceback, file=f)
            f.write("\n")

    except Exception:
        pass


def main():
    ensure_error_log()
    sys.excepthook = global_exception_handler

    root = tk.Tk()

    style = ttk.Style()

    try:
        style.theme_use("vista")
    except Exception:
        pass

    app = App(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
