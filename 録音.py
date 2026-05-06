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
from tkinter import ttk, messagebox

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

            self.system_device = self.p.get_device_info_by_index(system_device_index)
            self.mic_device = self.p.get_device_info_by_index(mic_device_index)

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
        try:
            return self.p.open(
                format=FORMAT,
                channels=channels,
                rate=rate,
                input=True,
                input_device_index=device_index,
                frames_per_buffer=CHUNK,
            )

        except Exception as e1:
            write_error_log(f"{label} open failed first try", e1)
            self.log(f"{label} open失敗。1chで再試行します: {e1}")

            try:
                if label == "相手音声":
                    self.system_channels = 1
                else:
                    self.mic_channels = 1

                return self.p.open(
                    format=FORMAT,
                    channels=1,
                    rate=rate,
                    input=True,
                    input_device_index=device_index,
                    frames_per_buffer=CHUNK,
                )

            except Exception as e2:
                write_error_log(f"{label} open failed fallback 1ch", e2)
                raise

    def _capture_loop(self, stream, level_attr, frame_attr, log_title, log_message):
        while self.is_recording:
            try:
                data = stream.read(CHUNK, exception_on_overflow=False)
                setattr(self, level_attr, self._calc_level(data))

                with self.frame_lock:
                    getattr(self, frame_attr).append(data)

            except Exception as e:
                write_error_log(log_title, e)
                self.log(f"{log_message}: {e}")
                self.is_recording = False
                break

    def _system_loop(self):
        self._capture_loop(
            stream=self.system_stream,
            level_attr="system_level",
            frame_attr="system_frames",
            log_title="AudioRecorder._system_loop error",
            log_message="相手音声読み取りエラー",
        )

    def _mic_loop(self):
        self._capture_loop(
            stream=self.mic_stream,
            level_attr="mic_level",
            frame_attr="mic_frames",
            log_title="AudioRecorder._mic_loop error",
            log_message="マイク読み取りエラー",
        )

    def stop(self):
        if not self.is_recording and not self.p:
            return None

        self.is_recording = False

        if self.system_thread:
            self.system_thread.join(timeout=1)
            self.system_thread = None

        if self.mic_thread:
            self.mic_thread.join(timeout=1)
            self.mic_thread = None

        try:
            if self.system_stream:
                self.system_stream.stop_stream()
                self.system_stream.close()
                self.system_stream = None
        except Exception as e:
            write_error_log("stop system_stream error", e)

        try:
            if self.mic_stream:
                self.mic_stream.stop_stream()
                self.mic_stream.close()
                self.mic_stream = None
        except Exception as e:
            write_error_log("stop mic_stream error", e)

        result = None

        try:
            if self.output_dir and self.system_frames and self.mic_frames:
                with self.frame_lock:
                    system_frames = list(self.system_frames)
                    mic_frames = list(self.mic_frames)

                self._save_wav(
                    self.system_wav,
                    system_frames,
                    self.system_channels,
                    self.system_rate
                )

                self._save_wav(
                    self.mic_wav,
                    mic_frames,
                    self.mic_channels,
                    self.mic_rate
                )

                self.log(f"相手音声保存: {self.system_wav}")
                self.log(f"マイク音声保存: {self.mic_wav}")

                result = {
                    "output_dir": self.output_dir,
                    "system_wav": self.system_wav,
                    "mic_wav": self.mic_wav,
                    "txt_out": self.txt_out,
                }

        except Exception as e:
            write_error_log("AudioRecorder.stop save error", e)
            raise

        finally:
            try:
                if self.p:
                    self.p.terminate()
                    self.p = None
            except Exception as e:
                write_error_log("terminate PyAudio error", e)

            self.system_level = 0
            self.mic_level = 0

        return result

    def _calc_level(self, data):
        if not data:
            return 0

        count = len(data) // 2
        if count <= 0:
            return 0

        try:
            samples = struct.unpack("<" + "h" * count, data[:count * 2])
            rms = math.sqrt(sum(s * s for s in samples) / count)
            return int(min(100, (rms / 32768) * 800))

        except Exception:
            return 0

    def _save_wav(self, path, frames, channels, rate):
        p = pyaudio.PyAudio()

        try:
            with wave.open(str(path), "wb") as wf:
                wf.setnchannels(channels)
                wf.setsampwidth(p.get_sample_size(FORMAT))
                wf.setframerate(rate)
                wf.writeframes(b"".join(frames))

        finally:
            p.terminate()


# =========================
# 文字起こし
# =========================
class Transcriber:
    def __init__(self, log_func):
        self.log = log_func
        self.model = None
        self.model_lock = threading.Lock()

    def preload_model(self):
        try:
            with self.model_lock:
                if self.model is not None:
                    return

                self.log("文字起こしモデルを読み込み中...")

                self.model = WhisperModel(
                    MODEL_SIZE,
                    device="cpu",
                    compute_type=COMPUTE_TYPE
                )

                self.log("文字起こしモデル読み込み完了。")

        except Exception as e:
            write_error_log("Transcriber.preload_model error", e)
            raise

    def transcribe_file(self, audio_path, speaker_label):
        try:
            self.preload_model()

            self.log(f"文字起こし開始: {speaker_label}")

            segments, info = self.model.transcribe(
                str(audio_path),
                language=LANGUAGE,
                vad_filter=True,
                vad_parameters=dict(
                    min_silence_duration_ms=500
                ),
                beam_size=10,
                temperature=0.0,
                condition_on_previous_text=False,
                word_timestamps=False,
            )

            rows = []

            for seg in segments:
                text = (seg.text or "").strip()
                if not text:
                    continue

                rows.append({
                    "start": float(seg.start),
                    "end": float(seg.end),
                    "speaker": speaker_label,
                    "text": text,
                })

            self.log(f"文字起こし完了: {speaker_label}")
            return rows

        except Exception as e:
            write_error_log(f"Transcriber.transcribe_file error: {speaker_label}", e)
            raise

    @staticmethod
    def fmt(sec):
        sec = max(0, int(sec))
        h = sec // 3600
        m = (sec % 3600) // 60
        s = sec % 60
        return f"{h:02d}:{m:02d}:{s:02d}"

    def export_transcript(self, rows, txt_path):
        try:
            rows = sorted(rows, key=lambda x: x["start"])

            with open(txt_path, "w", encoding="utf-8") as f:
                f.write("文字起こし\n")
                f.write("=" * 50 + "\n\n")

                for r in rows:
                    start = self.fmt(r["start"])
                    end = self.fmt(r["end"])
                    mark = "🟦 自分" if r["speaker"] == "自分" else "🟧 相手"

                    f.write(f"[{start} - {end}] {mark}: {r['text']}\n")

            self.log(f"文字起こしtxt保存完了: {txt_path}")

        except Exception as e:
            write_error_log("Transcriber.export_transcript error", e)
            raise


# =========================
# UI
# =========================
class App:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("900x680")

        self.main_thread_id = threading.get_ident()

        self.recorder = AudioRecorder(self.add_log)
        self.transcriber = Transcriber(self.add_log)

        self.system_devices = []
        self.mic_devices = []

        self.status_var = tk.StringVar(value="起動中")
        self.timer_var = tk.StringVar(value="録音時間: 00:00")

        self.elapsed_seconds = 0
        self.timer_job = None
        self.level_job = None
        self.preview_streams = []
        self.preview_audio = None
        self.preview_thread = None
        self.preview_running = False
        self.preview_start_job = None
        self.preview_delay_ms = 1500

        self.last_output_dir = None

        self.build_ui()

        self.root.after(100, self.load_devices)

    def build_ui(self):
        top_frame = ttk.Frame(self.root, padding=12)
        top_frame.pack(fill="x")

        ttk.Label(
            top_frame,
            text="文字起こしレコーダー",
            font=("Yu Gothic UI", 16, "bold")
        ).pack(anchor="w")

        desc = (
            "録音開始時だけ音声デバイスを開きます。\n"
            "録音停止後、文字起こしのみを txt 保存します。"
        )
        ttk.Label(top_frame, text=desc).pack(anchor="w", pady=(6, 10))

        device_frame = ttk.LabelFrame(self.root, text="録音デバイス選択", padding=12)
        device_frame.pack(fill="x", padx=12, pady=(0, 8))

        ttk.Label(device_frame, text="スピーカー / 相手音声:").grid(
            row=0,
            column=0,
            sticky="w"
        )

        self.system_combo = ttk.Combobox(
            device_frame,
            state="readonly",
            width=80
        )
        self.system_combo.grid(row=0, column=1, padx=8, pady=4, sticky="ew")

        ttk.Label(device_frame, text="マイク / 自分の声:").grid(
            row=1,
            column=0,
            sticky="w"
        )

        self.mic_combo = ttk.Combobox(
            device_frame,
            state="readonly",
            width=80
        )
        self.mic_combo.grid(row=1, column=1, padx=8, pady=4, sticky="ew")

        self.system_combo.bind("<<ComboboxSelected>>", self.on_device_changed)
        self.mic_combo.bind("<<ComboboxSelected>>", self.on_device_changed)

        self.reload_btn = ttk.Button(
            device_frame,
            text="デバイス再読み込み",
            command=self.load_devices
        )
        self.reload_btn.grid(row=2, column=1, sticky="e", pady=(8, 0))

        device_frame.columnconfigure(1, weight=1)

        status_frame = ttk.Frame(self.root, padding=(12, 0))
        status_frame.pack(fill="x")

        ttk.Label(status_frame, text="状態:").pack(side="left")
        ttk.Label(status_frame, textvariable=self.status_var).pack(
            side="left",
            padx=(4, 24)
        )
        ttk.Label(status_frame, textvariable=self.timer_var).pack(side="left")

        level_frame = ttk.LabelFrame(self.root, text="録音中の入力レベル", padding=12)
        level_frame.pack(fill="x", padx=12, pady=(8, 0))

        ttk.Label(level_frame, text="相手音声:").grid(row=0, column=0, sticky="w")

        self.system_level_bar = ttk.Progressbar(
            level_frame,
            orient="horizontal",
            mode="determinate",
            maximum=100
        )
        self.system_level_bar.grid(row=0, column=1, padx=8, pady=4, sticky="ew")

        self.system_level_label = ttk.Label(level_frame, text="0%")
        self.system_level_label.grid(row=0, column=2, sticky="e")

        ttk.Label(level_frame, text="マイク:").grid(row=1, column=0, sticky="w")

        self.mic_level_bar = ttk.Progressbar(
            level_frame,
            orient="horizontal",
            mode="determinate",
            maximum=100
        )
        self.mic_level_bar.grid(row=1, column=1, padx=8, pady=4, sticky="ew")

        self.mic_level_label = ttk.Label(level_frame, text="0%")
        self.mic_level_label.grid(row=1, column=2, sticky="e")

        level_frame.columnconfigure(1, weight=1)

        btn_frame = ttk.Frame(self.root, padding=12)
        btn_frame.pack(fill="x")

        self.start_btn = ttk.Button(
            btn_frame,
            text="録音開始",
            command=self.start_recording,
            state="disabled"
        )
        self.start_btn.pack(side="left", padx=(0, 10))

        self.stop_btn = ttk.Button(
            btn_frame,
            text="録音停止",
            command=self.stop_recording,
            state="disabled"
        )
        self.stop_btn.pack(side="left", padx=(0, 10))

        self.open_folder_btn = ttk.Button(
            btn_frame,
            text="録音フォルダを開く",
            command=self.open_output_folder,
            state="normal"
        )
        self.open_folder_btn.pack(side="left", padx=(0, 10))

        self.open_error_btn = ttk.Button(
            btn_frame,
            text="エラーログを開く",
            command=self.open_error_log
        )
        self.open_error_btn.pack(side="left")

        log_frame = ttk.LabelFrame(self.root, text="ログ", padding=12)
        log_frame.pack(fill="both", expand=True, padx=12, pady=12)

        self.log_text = tk.Text(log_frame, height=20, wrap="word")
        self.log_text.pack(fill="both", expand=True)

        self.add_log("アプリを起動しました。")
        self.add_log(f"録音フォルダ: {BASE_DIR}")
        self.add_log(f"エラーログ: {ERROR_LOG}")

    def load_devices(self):
        try:
            self.status_var.set("デバイス読み込み中")

            self.system_devices, self.mic_devices = AudioRecorder.list_devices()

            system_names = [
                f"{d['name']} / {d['rate']}Hz / {d['channels']}ch"
                for d in self.system_devices
            ]

            mic_names = [
                f"おすすめ{idx + 1}: {d['name']} / {d['rate']}Hz / {d['channels']}ch"
                for idx, d in enumerate(self.mic_devices)
            ]

            self.system_combo["values"] = system_names
            self.mic_combo["values"] = mic_names

            if system_names:
                self.system_combo.current(0)
            else:
                self.system_combo.set("loopbackデバイスが見つかりません")

            if mic_names:
                self.mic_combo.current(0)
            else:
                self.mic_combo.set("おすすめマイクが見つかりません")

            self.add_log("録音デバイスを読み込みました。")
            self.add_log(f"相手音声候補: {len(self.system_devices)} 件")
            self.add_log(f"おすすめマイク候補: {len(self.mic_devices)} 件")

            if self.system_devices and self.mic_devices:
                self.start_btn.config(state="normal")
                self.status_var.set("待機中")
                self.schedule_preview_level_meter()
            else:
                self.start_btn.config(state="disabled")
                self.status_var.set("デバイス未検出")

        except Exception as e:
            write_error_log("App.load_devices error", e)
            self.status_var.set("エラー")
            self.add_log(f"デバイス取得失敗: {e}")
            safe_messagebox_error(
                "エラー",
                f"デバイス一覧の取得に失敗しました。\n\n{e}\n\n詳細は {ERROR_LOG} を確認してください。"
            )

    def on_device_changed(self, event=None):
        if self.recorder.is_recording:
            return

        self.status_var.set("待機中")
        self.add_log("デバイスを変更しました。録音開始時に反映します。")
        self.schedule_preview_level_meter()

    def start_recording(self):
        try:
            self.stop_preview_level_meter()
            system_pos = self.system_combo.current()
            mic_pos = self.mic_combo.current()

            if system_pos < 0 or not self.system_devices:
                raise RuntimeError("相手音声デバイスが選択されていません。")

            if mic_pos < 0 or not self.mic_devices:
                raise RuntimeError("マイクデバイスが選択されていません。")

            system_device_index = self.system_devices[system_pos]["index"]
            mic_device_index = self.mic_devices[mic_pos]["index"]

            self.recorder.start(system_device_index, mic_device_index)

            self.status_var.set("録音中")
            self.elapsed_seconds = 0
            self.update_timer()
            self.update_level_meter()

            threading.Thread(
                target=self.transcriber.preload_model,
                daemon=True
            ).start()

            self.start_btn.config(state="disabled")
            self.stop_btn.config(state="normal")
            self.reload_btn.config(state="disabled")
            self.system_combo.config(state="disabled")
            self.mic_combo.config(state="disabled")

        except Exception as e:
            write_error_log("App.start_recording error", e)
            self.status_var.set("エラー")
            self.add_log(f"録音開始失敗: {e}")
            safe_messagebox_error(
                "エラー",
                f"録音開始に失敗しました。\n\n{e}\n\n詳細は {ERROR_LOG} を確認してください。"
            )

    def stop_recording(self):
        try:
            self.status_var.set("録音停止処理中")
            self.stop_btn.config(state="disabled")

            if self.timer_job:
                self.root.after_cancel(self.timer_job)
                self.timer_job = None

            if self.level_job:
                self.root.after_cancel(self.level_job)
                self.level_job = None

            result = self.recorder.stop()

            self.reset_level_meter()

            if not result:
                self.status_var.set("待機中")
                self.enable_controls()
                self.schedule_preview_level_meter()
                return

            self.last_output_dir = result["output_dir"]

            self.status_var.set("文字起こし中")
            self.add_log("文字起こしを開始します。")

            threading.Thread(
                target=self.run_transcription,
                args=(result,),
                daemon=True
            ).start()

        except Exception as e:
            write_error_log("App.stop_recording error", e)
            self.status_var.set("エラー")
            self.add_log(f"録音停止失敗: {e}")
            self.enable_controls()
            safe_messagebox_error(
                "エラー",
                f"録音停止に失敗しました。\n\n{e}\n\n詳細は {ERROR_LOG} を確認してください。"
            )

    def run_transcription(self, result):
        try:
            system_rows = self.transcriber.transcribe_file(
                result["system_wav"],
                "相手"
            )

            mic_rows = self.transcriber.transcribe_file(
                result["mic_wav"],
                "自分"
            )

            all_rows = system_rows + mic_rows
            self.transcriber.export_transcript(all_rows, result["txt_out"])

            self.add_log("文字起こしが完了しました。")
            self.add_log(f"保存フォルダ: {result['output_dir']}")

            self.status_var.set("完了")

            safe_messagebox_info(
                "完了",
                f"文字起こしが完了しました。\n\n保存先:\n{result['txt_out']}"
            )

        except Exception as e:
            write_error_log("App.run_transcription error", e)
            self.status_var.set("エラー")
            self.add_log(f"文字起こしエラー: {e}")
            safe_messagebox_error(
                "エラー",
                f"文字起こしに失敗しました。\n\n{e}\n\n詳細は {ERROR_LOG} を確認してください。"
            )

        finally:
            self.enable_controls()
            self.schedule_preview_level_meter()

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
        for channels in [device["channels"], 1]:
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
                write_error_log("App._open_preview_stream error", e)
        raise RuntimeError(f"preview stream open failed: {last_error}")

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
            self.preview_streams = [
                ("system_level", self._open_preview_stream(system_device)),
                ("mic_level", self._open_preview_stream(mic_device)),
            ]
            self.preview_running = True
            self.preview_thread = threading.Thread(target=self._preview_loop, daemon=True)
            self.preview_thread.start()
            self.update_level_meter()
        except Exception as e:
            write_error_log("App.start_preview_level_meter error", e)
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
