import math
import os
import sys
import re
import audioop
import json
import struct
import threading
import subprocess
import traceback
import wave
import ctypes
import mimetypes
import uuid
import time
import urllib.error
import urllib.request
from pathlib import Path
from datetime import datetime
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

import pyaudiowpatch as pyaudio
from faster_whisper import WhisperModel


# =========================
# 設定
# =========================
APP_TITLE = "レコードApp"

BASE_DIR = Path.cwd() / "mtg_records"
BASE_DIR.mkdir(parents=True, exist_ok=True)

ERROR_LOG = BASE_DIR / "error_log.txt"

FORMAT = pyaudio.paInt16
CHUNK = 2048
LANGUAGE = "ja"

MODEL_SIZE = "large-v3"
COMPUTE_TYPE = "int8"
BEAM_SIZE = 5
LIDCLOSE_GUID = "5ca83367-6e45-459f-a27b-476b1d01c936"

MODEL_CHOICES = {
    "medium": "medium（標準・軽め）",
    "large-v3": "large-v3（高精度・推奨）",
    "large-v3-turbo": "large-v3-turbo（高速）",
    "gpt-4o-mini-transcribe": "gpt-4o-mini-transcribe（API）",
    "gpt-4o-transcribe": "gpt-4o-transcribe（API・高精度）",
}

MODEL_FILENAME_CODES = {
    "medium": "m",
    "large-v3": "la3",
    "large-v3-turbo": "la3t",
    "gpt-4o-mini-transcribe": "4ominit",
    "gpt-4o-transcribe": "4ot",
}

MODE_CHOICES = {
    "高速": 1,
    "標準": 5,
    "高精度": 10,
}

DEFAULT_SETTINGS = {
    "model_size": MODEL_SIZE,
    "mode": "標準",
    "beam_size": BEAM_SIZE,
    "compute_type": COMPUTE_TYPE,
    "openai_api_key": "",
}

API_TRANSCRIBE_MODELS = {"gpt-4o-mini-transcribe", "gpt-4o-transcribe"}


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
        self.mixed_wav = None
        self.mixed_compressed = None
        self.txt_out = None

    @staticmethod
    def _device_name_lower(device):
        return (device.get("name", "") or "").lower()

    @staticmethod
    def _is_headset_device(device):
        name = AudioRecorder._device_name_lower(device)
        keywords = [
            "ヘッドセット",
            "headset",
            "hands-free",
            "hands free",
            "hfp",
            "hsp",
            "soundpeats",
        ]
        return any(keyword in name for keyword in keywords)

    @staticmethod
    def _is_headphone_device(device):
        name = AudioRecorder._device_name_lower(device)
        keywords = [
            "ヘッドホン",
            "ヘッドフォン",
            "headphone",
            "headphones",
            "bluetooth",
        ]
        return any(keyword in name for keyword in keywords)

    @staticmethod
    def device_label(device, fallback_kind="デバイス"):
        raw_name = device.get("name", "") or "不明なデバイス"
        display_name = raw_name.replace("[Loopback]", "").replace("(Loopback)", "").strip()

        if AudioRecorder._is_headset_device(device):
            kind = "ヘッドセット"
        elif AudioRecorder._is_headphone_device(device):
            kind = "ヘッドホン"
        elif "スピーカー" in raw_name or "speaker" in raw_name.lower():
            kind = "スピーカー"
        elif "マイク" in raw_name or "microphone" in raw_name.lower() or "mic" in raw_name.lower():
            kind = "マイク"
        else:
            kind = fallback_kind

        display_name = re.sub(
            r"^(ヘッドセット|ヘッドホン|ヘッドフォン|headset|headphones?|hands-free|hands free|スピーカー|speaker|マイク|microphone|mic)\s*[\(（]?\s*\d*\s*[-:：]?\s*",
            "",
            display_name,
            flags=re.IGNORECASE,
        )
        display_name = re.sub(r"\s+", " ", display_name).strip(" )）(（")
        display_name = display_name or raw_name

        return f"{kind}: {display_name}"

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
            name = d["name"].lower()
            score = 0

            if AudioRecorder._is_headset_device(d):
                score += 300
            if AudioRecorder._is_headphone_device(d):
                score += 250
            if "スピーカー" in d["name"] or "speaker" in name:
                score += 80
            if d["rate"] == 48000:
                score += 60
            if d["rate"] == 44100:
                score += 40
            if "realtek" in name:
                score += 30

            return score

        def mic_priority(d):
            name = d["name"].lower()
            score = 0

            if AudioRecorder._is_headset_device(d):
                score += 300
            if AudioRecorder._is_headphone_device(d):
                score += 250
            if "マイク" in d["name"] or "microphone" in name or "mic" in name:
                score += 80
            if d["rate"] == 48000:
                score += 60
            if d["rate"] == 44100:
                score += 40
            if d["channels"] >= 2:
                score += 30
            if "realtek" in name:
                score += 20

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

            now_prefix = datetime.now().strftime("%Y年%m月%d日%H時%M分")
            folder_name = now_prefix
            self.output_dir = BASE_DIR / folder_name
            self.output_dir.mkdir(parents=True, exist_ok=True)

            self.system_wav = self.output_dir / "system.wav"
            self.mic_wav = self.output_dir / "mic.wav"
            self.mixed_wav = self.output_dir / "mixed.wav"
            self.mixed_compressed = self.output_dir / "mixed.m4a"
            self.txt_out = self.output_dir / f"{folder_name}.txt"

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
        dev = self.p.get_device_info_by_index(device_index)
        max_ch = max(1, int(dev.get("maxInputChannels", 1)))
        default_rate = int(dev.get("defaultSampleRate", rate))

        channel_candidates = []
        for ch in [channels, max_ch, min(2, max_ch), 1]:
            if ch and ch not in channel_candidates:
                channel_candidates.append(ch)

        rate_candidates = []
        for r in [rate, default_rate, 48000, 44100, 32000, 16000]:
            if r and r not in rate_candidates:
                rate_candidates.append(r)

        last_error = None
        for ch in channel_candidates:
            for r in rate_candidates:
                try:
                    stream = self.p.open(
                        format=FORMAT,
                        channels=ch,
                        rate=int(r),
                        input=True,
                        input_device_index=device_index,
                        frames_per_buffer=CHUNK,
                    )
                    if label == "相手音声":
                        self.system_channels = ch
                        self.system_rate = int(r)
                    else:
                        self.mic_channels = ch
                        self.mic_rate = int(r)
                    return stream
                except Exception as e:
                    last_error = e

        write_error_log(f"{label} open failed all candidates", last_error)
        raise RuntimeError(f"{label} の録音開始に失敗しました: {last_error}")

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
            if self.output_dir and (self.system_frames or self.mic_frames):
                with self.frame_lock:
                    system_frames = list(self.system_frames)
                    mic_frames = list(self.mic_frames)

                if system_frames:
                    self._save_wav(
                        self.system_wav,
                        system_frames,
                        self.system_channels,
                        self.system_rate
                    )
                    self.log(f"相手音声保存: {self.system_wav}")
                else:
                    self.log("相手音声は保存データがありませんでした。")

                if mic_frames:
                    self._save_wav(
                        self.mic_wav,
                        mic_frames,
                        self.mic_channels,
                        self.mic_rate
                    )
                    self.log(f"マイク音声保存: {self.mic_wav}")
                else:
                    self.log("マイク音声は保存データがありませんでした。")

                mixed_wav = None
                if system_frames and mic_frames:
                    try:
                        self._save_mixed_wav(
                            self.mixed_wav,
                            system_frames,
                            self.system_channels,
                            self.system_rate,
                            mic_frames,
                            self.mic_channels,
                            self.mic_rate,
                        )
                        compressed = self._compress_mixed_audio(self.mixed_wav, self.mixed_compressed)
                        if compressed:
                            mixed_wav = self.mixed_compressed
                            self.log(f"MIX音声圧縮保存: {self.mixed_compressed}")
                        else:
                            mixed_wav = self.mixed_wav
                            self.log(f"MIX音声保存(非圧縮): {self.mixed_wav}")
                    except Exception as e:
                        write_error_log("AudioRecorder.stop mixed wav error", e)
                        self.log(f"MIX音声保存失敗: {e}")
                else:
                    self.log("MIX音声は作成しませんでした。")

                result = {
                    "output_dir": self.output_dir,
                    "system_wav": self.system_wav,
                    "mic_wav": self.mic_wav,
                    "mixed_wav": mixed_wav,
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

    @staticmethod
    def _frames_to_mono_pcm(frames, channels, rate, target_rate):
        pcm = b"".join(frames)
        sample_width = 2

        if channels > 1:
            weights = [1 / channels] * channels
            pcm = audioop.tomono(pcm, sample_width, weights[0], weights[1] if channels > 1 else weights[0])

        if rate != target_rate:
            pcm, _ = audioop.ratecv(pcm, sample_width, 1, rate, target_rate, None)

        return audioop.mul(pcm, sample_width, 0.7)

    def _save_mixed_wav(
        self,
        path,
        system_frames,
        system_channels,
        system_rate,
        mic_frames,
        mic_channels,
        mic_rate,
    ):
        target_rate = system_rate
        system_pcm = self._frames_to_mono_pcm(system_frames, system_channels, system_rate, target_rate)
        mic_pcm = self._frames_to_mono_pcm(mic_frames, mic_channels, mic_rate, target_rate)

        max_len = max(len(system_pcm), len(mic_pcm))
        system_pcm = system_pcm.ljust(max_len, b"\x00")
        mic_pcm = mic_pcm.ljust(max_len, b"\x00")
        mixed_pcm = audioop.add(system_pcm, mic_pcm, 2)

        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(target_rate)
            wf.writeframes(mixed_pcm)

    def _compress_mixed_audio(self, src_wav, dst_m4a):
        try:
            cmd = [
                "ffmpeg",
                "-y",
                "-i",
                str(src_wav),
                "-c:a",
                "aac",
                "-b:a",
                "96k",
                str(dst_m4a),
            ]
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            if dst_m4a.exists():
                try:
                    src_wav.unlink()
                except Exception as e:
                    write_error_log("AudioRecorder._compress_mixed_audio unlink warning", e)
                return True
            return False
        except Exception as e:
            write_error_log("AudioRecorder._compress_mixed_audio error", e)
            return False


# =========================
# 文字起こし
# =========================
class Transcriber:
    def __init__(self, log_func):
        self.log = log_func
        self.model = None
        self.model_size = MODEL_SIZE
        self.compute_type = COMPUTE_TYPE
        self.beam_size = BEAM_SIZE
        self.openai_api_key = ""
        self.model_lock = threading.Lock()

    def set_settings(self, settings):
        with self.model_lock:
            model_size = settings["model_size"]
            compute_type = settings["compute_type"]
            self.openai_api_key = (settings.get("openai_api_key", "") or "").strip()
            if model_size != self.model_size or compute_type != self.compute_type:
                self.model = None
            self.model_size = model_size
            self.compute_type = compute_type
            self.beam_size = int(settings["beam_size"])

    def preload_model(self):
        try:
            with self.model_lock:
                if self.model_size in API_TRANSCRIBE_MODELS:
                    return
                if self.model is not None:
                    return

                self.log(f"文字起こしモデルを読み込み中: {self.model_size}")

                self.model = WhisperModel(
                    self.model_size,
                    device="cpu",
                    compute_type=self.compute_type
                )

                self.log("文字起こしモデル読み込み完了。")

        except Exception as e:
            write_error_log("Transcriber.preload_model error", e)
            raise

    def transcribe_file(self, audio_path, speaker_label):
        try:
            self.log(f"文字起こし開始: {speaker_label}")
            rows = []
            if self.model_size in API_TRANSCRIBE_MODELS:
                rows = self._transcribe_file_api(audio_path, speaker_label)
            else:
                self.preload_model()
                segments, info = self.model.transcribe(
                    str(audio_path),
                    language=LANGUAGE,
                    vad_filter=True,
                    vad_parameters=dict(
                        min_silence_duration_ms=500
                    ),
                    beam_size=self.beam_size,
                    temperature=0.0,
                    condition_on_previous_text=False,
                    word_timestamps=False,
                )

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

    def _transcribe_file_api(self, audio_path, speaker_label):
        api_key = (self.openai_api_key or "").strip()
        if not api_key:
            raise RuntimeError("APIモデル利用時はOpenAI APIキーを入力してください。")

        audio_path = Path(audio_path)
        upload_audio_path = audio_path

        def _prepare_audio_for_api(src_path):
            converted_path = src_path.with_name(f"{src_path.stem}_api.wav")
            cmd = [
                "ffmpeg",
                "-y",
                "-i",
                str(src_path),
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                str(converted_path),
            ]
            result = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode != 0:
                err = result.stderr.decode("utf-8", errors="replace").strip()
                raise RuntimeError(f"ffmpeg変換失敗: {err}")
            return converted_path

        def _build_request(send_audio_path, response_format):
            audio_bytes = send_audio_path.read_bytes()
            mime_type = mimetypes.guess_type(str(send_audio_path))[0] or "application/octet-stream"
            boundary = f"----recordapp{uuid.uuid4().hex}"
            parts = []

            def add_field(name, value):
                parts.append(f"--{boundary}\r\n".encode("utf-8"))
                parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
                parts.append(f"{value}\r\n".encode("utf-8"))

            add_field("model", self.model_size)
            add_field("language", LANGUAGE)
            add_field("response_format", response_format)

            parts.append(f"--{boundary}\r\n".encode("utf-8"))
            parts.append(
                f'Content-Disposition: form-data; name="file"; filename="{send_audio_path.name}"\r\n'.encode("utf-8")
            )
            parts.append(f"Content-Type: {mime_type}\r\n\r\n".encode("utf-8"))
            parts.append(audio_bytes)
            parts.append(b"\r\n")
            parts.append(f"--{boundary}--\r\n".encode("utf-8"))
            body = b"".join(parts)

            return urllib.request.Request(
                "https://api.openai.com/v1/audio/transcriptions",
                data=body,
                method="POST",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                },
            )
        max_attempts = 4
        backoff_seconds = 2
        last_error = None
        payload = None

        def _read_http_error_detail(http_error):
            try:
                raw = http_error.read()
                if not raw:
                    return ""
                text = raw.decode("utf-8", errors="replace").strip()
                if not text:
                    return ""
                try:
                    data = json.loads(text)
                    err = data.get("error", {}) if isinstance(data, dict) else {}
                    message = (err.get("message", "") or "").strip()
                    code = (err.get("code", "") or "").strip()
                    type_name = (err.get("type", "") or "").strip()
                    parts = [p for p in [message, code, type_name] if p]
                    return " / ".join(parts) if parts else text
                except Exception:
                    return text
            except Exception:
                return ""

        response_formats = ["json", "verbose_json"]
        used_response_format = response_formats[0]
        attempted_audio_convert = False

        for attempt in range(1, max_attempts + 1):
            try:
                req = _build_request(upload_audio_path, used_response_format)
                with urllib.request.urlopen(req, timeout=600) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as e:
                last_error = e
                error_detail = _read_http_error_detail(e)
                if (
                    e.code == 400
                    and "response_format" in (error_detail or "")
                    and attempt == 1
                ):
                    if used_response_format == "verbose_json":
                        used_response_format = "json"
                    elif used_response_format == "json":
                        used_response_format = "text"
                    else:
                        used_response_format = "json"
                    self.log(
                        f"API文字起こし形式を切替えて再試行します ({used_response_format}): {speaker_label}"
                    )
                    continue
                if (
                    e.code == 400
                    and not attempted_audio_convert
                    and ("Audio file might be corrupted or unsupported" in (error_detail or ""))
                ):
                    attempted_audio_convert = True
                    upload_audio_path = _prepare_audio_for_api(audio_path)
                    self.log(
                        f"音声形式エラーのため16kHzモノラルWAVへ変換して再試行します: {speaker_label}"
                    )
                    continue
                is_retryable = e.code in {429, 500, 502, 503, 504}
                if not is_retryable or attempt >= max_attempts:
                    detail_msg = f" 詳細: {error_detail}" if error_detail else ""
                    raise RuntimeError(
                        f"API文字起こしに失敗しました (HTTP {e.code}, model={self.model_size}, format={used_response_format}).{detail_msg}"
                    ) from e
                wait_sec = backoff_seconds * (2 ** (attempt - 1))
                detail_msg = f" / {error_detail}" if error_detail else ""
                self.log(
                    f"API文字起こし一時エラー(HTTP {e.code})。{wait_sec}秒後に再試行します "
                    f"({attempt}/{max_attempts}): {speaker_label}{detail_msg}"
                )
                time.sleep(wait_sec)
            except urllib.error.URLError as e:
                last_error = e
                if attempt >= max_attempts:
                    raise
                wait_sec = backoff_seconds * (2 ** (attempt - 1))
                self.log(
                    f"API通信エラー。{wait_sec}秒後に再試行します "
                    f"({attempt}/{max_attempts}): {speaker_label}"
                )
                time.sleep(wait_sec)

        if payload is None and last_error is not None:
            raise last_error

        rows = []
        segments = payload.get("segments", []) or []
        if segments:
            for seg in segments:
                text = (seg.get("text", "") or "").strip()
                if not text:
                    continue
                rows.append({
                    "start": float(seg.get("start", 0.0)),
                    "end": float(seg.get("end", 0.0)),
                    "speaker": speaker_label,
                    "text": text,
                })
        else:
            text = (payload.get("text", "") or "").strip()
            if text:
                rows.append({
                    "start": 0.0,
                    "end": 0.0,
                    "speaker": speaker_label,
                    "text": text,
                })
        if upload_audio_path != audio_path and upload_audio_path.exists():
            try:
                upload_audio_path.unlink()
            except Exception:
                pass
        return rows

    @staticmethod
    def fmt(sec):
        sec = max(0, int(sec))
        h = sec // 3600
        m = (sec % 3600) // 60
        s = sec % 60
        return f"{h:02d}:{m:02d}:{s:02d}"

    def export_transcript(self, rows, txt_path, memo_text=""):
        try:
            rows = sorted(rows, key=lambda x: x["start"])

            with open(txt_path, "w", encoding="utf-8") as f:
                memo_text = (memo_text or "").strip()
                if memo_text:
                    f.write("メモ\n")
                    f.write("=" * 50 + "\n")
                    f.write(memo_text)
                    f.write("\n\n")

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
        self.root.geometry("430x480")
        self.root.minsize(400, 480)
        self.bg_color = "#fff8fb"
        self.card_color = "#ffffff"
        self.text_color = "#1f2937"
        self.muted_color = "#6b7280"
        self.accent_color = "#ff5c8a"
        self.accent_soft = "#ffe7ef"
        self.border_color = "#f8cdd9"
        self.style = ttk.Style()
        self.style.theme_use("clam")
        self.style.configure("TNotebook", background=self.bg_color, borderwidth=0)
        self.style.configure("TNotebook.Tab", padding=(26, 11), font=("Yu Gothic UI", 9), borderwidth=0)
        self.style.map(
            "TNotebook.Tab",
            foreground=[("selected", self.accent_color), ("!selected", self.text_color)],
            background=[("selected", "#fff0f5"), ("!selected", "#ffffff")],
        )
        self.style.configure("Tab.TFrame", background=self.bg_color)
        self.style.configure("TFrame", background=self.bg_color)
        self.style.configure("Card.TFrame", background=self.card_color)
        self.style.configure("TLabel", background=self.bg_color, foreground=self.text_color, font=("Yu Gothic UI", 9))
        self.style.configure("Card.TLabel", background=self.card_color, foreground=self.text_color, font=("Yu Gothic UI", 9))
        self.style.configure("Muted.Card.TLabel", background=self.card_color, foreground=self.muted_color, font=("Yu Gothic UI", 8))
        self.style.configure("Title.TLabel", background=self.bg_color, foreground=self.text_color, font=("Yu Gothic UI", 15, "bold"))
        self.style.configure("Section.Card.TLabel", background=self.card_color, foreground=self.text_color, font=("Yu Gothic UI", 10, "bold"))
        self.style.configure("Accent.TButton", foreground=self.accent_color, font=("Yu Gothic UI", 9, "bold"), padding=(14, 8))
        self.style.configure("Primary.TButton", foreground="#ffffff", background=self.accent_color, font=("Yu Gothic UI", 9, "bold"), padding=(14, 9))
        self.style.configure("Small.Primary.TButton", foreground="#ffffff", background=self.accent_color, font=("Yu Gothic UI", 8, "bold"), padding=(10, 6))
        self.style.configure("Small.TButton", font=("Yu Gothic UI", 8), padding=(10, 6))
        self.style.map("Primary.TButton", background=[("active", "#ff477d"), ("disabled", "#f3c4d1")])
        self.style.map("Small.Primary.TButton", background=[("active", "#ff477d"), ("disabled", "#f3c4d1")])
        self.style.configure("Soft.Horizontal.TProgressbar", troughcolor="#ffe6ee", background=self.accent_color, bordercolor="#ffe6ee", lightcolor=self.accent_color, darkcolor=self.accent_color)

        self.main_thread_id = threading.get_ident()

        self.recorder = AudioRecorder(self.add_log)
        self.transcriber = Transcriber(self.add_log)
        self.app_settings = DEFAULT_SETTINGS.copy()
        self.transcriber.set_settings(self.app_settings)

        self.system_devices = []
        self.mic_devices = []

        self.status_var = tk.StringVar(value="起動中")
        self.timer_var = tk.StringVar(value="録音時間: 00:00")
        self.status_title_var = tk.StringVar(value="停止中")
        self.status_detail_var = tk.StringVar(value="録音は開始されていません")
        self.current_transcription_var = tk.StringVar(value="文字起こし: 待機中")
        self.model_var = tk.StringVar(value=MODEL_CHOICES[self.app_settings["model_size"]])
        self.mode_var = tk.StringVar(value=self.app_settings["mode"])
        self.api_key_var = tk.StringVar(value=self.app_settings["openai_api_key"])
        self.settings_summary_var = tk.StringVar()
        self.recording_system_device_var = tk.StringVar(value="相手音声")
        self.recording_mic_device_var = tk.StringVar(value="マイク")
        self.recording_system_percent_var = tk.StringVar(value="0%")
        self.recording_mic_percent_var = tk.StringVar(value="0%")

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
        self.original_lid_action = None
        self.lid_action_available = False
        self.lid_action_changed = False
        self.transcription_queue = []
        self.transcription_running = False
        self.status_var.trace_add("write", lambda *_: self.refresh_status_banner())

        self.build_ui()
        self.update_recording_visual_state(is_recording=False)
        self.load_lid_action_settings()

        self.root.after(100, self.load_devices)

    def _card(self, parent, padx=14, pady=14):
        card = tk.Frame(
            parent,
            bg=self.card_color,
            bd=0,
            highlightthickness=1,
            highlightbackground="#f4d9e2",
            highlightcolor="#f4d9e2",
        )
        inner = ttk.Frame(card, style="Card.TFrame", padding=(padx, pady))
        inner.pack(fill="both", expand=True)
        return card, inner

    def build_ui(self):
        self.root.configure(bg=self.bg_color)
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=14, pady=14)

        home_tab = ttk.Frame(self.notebook, style="Tab.TFrame")
        analysis_tab = ttk.Frame(self.notebook, style="Tab.TFrame")
        settings_tab = ttk.Frame(self.notebook, style="Tab.TFrame")

        self.notebook.add(home_tab, text="⌂  ホーム")
        self.notebook.add(analysis_tab, text="▥  分析")
        self.notebook.add(settings_tab, text="⚙  設定")

        top_frame = ttk.Frame(home_tab, padding=(4, 4, 4, 12), style="Tab.TFrame")
        top_frame.pack(fill="x")

        self.status_banner = tk.Frame(
            top_frame,
            bg="#ffffff",
            highlightthickness=1,
            highlightbackground="#f4d9e2",
            highlightcolor="#f4d9e2",
        )
        self.status_banner.pack(fill="x")
        self.status_title_label = tk.Label(
            self.status_banner,
            textvariable=self.status_title_var,
            bg="#ffffff",
            fg=self.text_color,
            font=("Yu Gothic UI", 15, "bold"),
            anchor="w",
            padx=14,
            pady=8,
        )
        self.status_title_label.pack(fill="x")
        self.status_detail_label = tk.Label(
            self.status_banner,
            textvariable=self.status_detail_var,
            bg="#ffffff",
            fg=self.muted_color,
            font=("Yu Gothic UI", 9),
            anchor="w",
            padx=14,
            pady=6,
        )
        self.status_detail_label.pack(fill="x")
        self.current_transcription_label = tk.Label(
            self.status_banner,
            textvariable=self.current_transcription_var,
            bg="#ffffff",
            fg=self.muted_color,
            font=("Yu Gothic UI", 8),
            anchor="w",
            padx=14,
            pady=6,
        )
        self.current_transcription_label.pack(fill="x")
        self.refresh_status_banner()

        device_card, device_frame = self._card(home_tab)
        device_card.pack(fill="x", pady=(0, 10))
        ttk.Label(device_frame, text="🎙  録音デバイス選択", style="Section.Card.TLabel").grid(
            row=0,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(0, 12),
        )

        ttk.Label(device_frame, text="スピーカー / 相手音声:", style="Card.TLabel").grid(
            row=1,
            column=0,
            sticky="w"
        )

        self.system_combo = ttk.Combobox(
            device_frame,
            state="readonly",
            width=80
        )
        self.system_combo.grid(row=2, column=0, columnspan=2, pady=(4, 10), sticky="ew")

        ttk.Label(device_frame, text="マイク / 自分の声:", style="Card.TLabel").grid(
            row=3,
            column=0,
            sticky="w"
        )

        self.mic_combo = ttk.Combobox(
            device_frame,
            state="readonly",
            width=80
        )
        self.mic_combo.grid(row=4, column=0, columnspan=2, pady=(4, 10), sticky="ew")

        self.system_combo.bind("<<ComboboxSelected>>", self.on_device_changed)
        self.mic_combo.bind("<<ComboboxSelected>>", self.on_device_changed)

        self.reload_btn = ttk.Button(
            device_frame,
            text="⟳  デバイス再読み込み",
            style="Accent.TButton",
            command=self.load_devices
        )
        self.reload_btn.grid(row=5, column=1, sticky="e", pady=(2, 0))

        device_frame.columnconfigure(0, weight=1)
        device_frame.columnconfigure(1, weight=0)

        btn_frame = ttk.Frame(home_tab, padding=(0, 0, 0, 6), style="Tab.TFrame")
        btn_frame.pack(fill="x")

        self.start_btn = ttk.Button(
            btn_frame,
            text="◎  録音開始",
            style="Primary.TButton",
            command=self.start_recording,
            state="disabled"
        )
        self.start_btn.pack(side="left", padx=(0, 10))

        self.stop_btn = ttk.Button(
            btn_frame,
            text="◉  録音停止",
            command=self.stop_recording,
            state="disabled"
        )
        self.stop_btn.pack(side="left", padx=(0, 10))

        self.open_folder_btn = ttk.Button(
            btn_frame,
            text="□  録音フォルダを開く",
            style="Accent.TButton",
            command=self.open_output_folder,
            state="normal"
        )
        self.open_folder_btn.pack(side="left", padx=(0, 10))

        self.build_recording_view()

        analysis_top_frame = ttk.Frame(analysis_tab, padding=(4, 24, 4, 12), style="Tab.TFrame")
        analysis_top_frame.pack(fill="x")
        self.start_transcribe_btn = ttk.Button(
            analysis_top_frame,
            text="✐  文字起こし開始",
            style="Accent.TButton",
            command=self.start_transcription_queue,
            state="normal"
        )
        self.start_transcribe_btn.pack(side="left")

        ttk.Label(analysis_tab, text="文字起こしキュー", font=("Yu Gothic UI", 10, "bold")).pack(anchor="w", padx=4, pady=(0, 8))
        self.queue_count_label = ttk.Label(analysis_tab, text="追加済みファイル（0 件）", font=("Yu Gothic UI", 10, "bold"))
        self.queue_count_label.pack(anchor="w", padx=4, pady=(0, 8))
        list_card, list_frame = self._card(analysis_tab, padx=12, pady=12)
        list_card.pack(fill="both", expand=True, pady=(0, 8))
        self.queue_listbox = tk.Listbox(
            list_frame,
            height=8,
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            bg=self.card_color,
            fg=self.text_color,
            selectbackground=self.accent_soft,
            selectforeground=self.text_color,
            font=("Yu Gothic UI", 9),
        )
        self.queue_listbox.pack(fill="both", expand=True)

        queue_btn_frame = ttk.Frame(analysis_tab, style="Tab.TFrame")
        queue_btn_frame.pack(fill="x", pady=(0, 12))
        ttk.Button(
            queue_btn_frame,
            text="+  フォルダ追加",
            style="Small.Primary.TButton",
            command=self.add_queue_from_dialog,
            width=13,
        ).pack(side="left")
        ttk.Button(
            queue_btn_frame,
            text="♲  選択削除",
            style="Small.TButton",
            command=self.remove_selected_queue,
            width=12,
        ).pack(side="left", padx=(8, 0))

        settings_top_frame = ttk.Frame(settings_tab, padding=(4, 24, 4, 12), style="Tab.TFrame")
        settings_top_frame.pack(fill="x")

        settings_card, settings_frame = self._card(settings_tab, padx=12, pady=12)
        settings_card.pack(fill="x", pady=(0, 12))
        ttk.Label(settings_frame, text="文字起こし設定", style="Section.Card.TLabel").grid(
            row=0,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(0, 12),
        )
        ttk.Label(settings_frame, text="モデル", style="Card.TLabel").grid(row=1, column=0, sticky="w")
        self.model_combo = ttk.Combobox(
            settings_frame,
            textvariable=self.model_var,
            values=list(MODEL_CHOICES.values()),
            state="readonly",
            width=30,
        )
        self.model_combo.grid(row=1, column=1, sticky="ew", padx=(12, 0), pady=(0, 8))

        ttk.Label(settings_frame, text="処理モード", style="Card.TLabel").grid(row=2, column=0, sticky="w")
        self.mode_combo = ttk.Combobox(
            settings_frame,
            textvariable=self.mode_var,
            values=list(MODE_CHOICES.keys()),
            state="readonly",
            width=30,
        )
        self.mode_combo.grid(row=2, column=1, sticky="ew", padx=(12, 0), pady=(0, 8))

        ttk.Label(settings_frame, text="OpenAI APIキー", style="Card.TLabel").grid(row=3, column=0, sticky="w")
        self.api_key_entry = ttk.Entry(settings_frame, textvariable=self.api_key_var, width=30, show="*")
        self.api_key_entry.grid(row=3, column=1, sticky="ew", padx=(12, 0), pady=(0, 8))
        self.api_key_entry.bind("<FocusOut>", self.on_transcription_setting_changed)

        ttk.Label(
            settings_frame,
            textvariable=self.settings_summary_var,
            style="Muted.Card.TLabel",
            wraplength=340,
            justify="left",
        ).grid(row=4, column=0, columnspan=2, sticky="ew", pady=(2, 10))

        settings_frame.columnconfigure(1, weight=1)
        self.model_combo.bind("<<ComboboxSelected>>", self.on_transcription_setting_changed)
        self.mode_combo.bind("<<ComboboxSelected>>", self.on_transcription_setting_changed)
        self.refresh_settings_summary()

        self.open_error_btn = ttk.Button(
            settings_top_frame,
            text="▤  エラーログを開く",
            style="Accent.TButton",
            command=self.open_error_log
        )
        self.open_error_btn.pack(side="left")

        ttk.Label(settings_tab, text="ログ", font=("Yu Gothic UI", 10, "bold")).pack(anchor="w", padx=4, pady=(0, 8))
        log_card, log_frame = self._card(settings_tab, padx=12, pady=12)
        log_card.pack(fill="both", expand=True, pady=(0, 0))

        self.log_text = tk.Text(
            log_frame,
            height=20,
            wrap="word",
            relief="flat",
            borderwidth=0,
            bg=self.card_color,
            fg=self.text_color,
            insertbackground=self.accent_color,
            font=("Yu Gothic UI", 9),
        )
        self.log_text.pack(fill="both", expand=True)

        self.add_log("アプリを起動しました。")
        self.add_log(f"録音フォルダ: {BASE_DIR}")
        self.add_log(f"エラーログ: {ERROR_LOG}")
        self.add_log(
            f"文字起こし設定: model={self.app_settings['model_size']}, "
            f"mode={self.app_settings['mode']}, beam_size={self.app_settings['beam_size']}, "
            f"compute_type={self.app_settings['compute_type']}"
        )

    def build_recording_view(self):
        self.recording_frame = ttk.Frame(self.root, style="Tab.TFrame", padding=14)

        self.recording_stop_btn = ttk.Button(
            self.recording_frame,
            text="停止",
            style="Primary.TButton",
            command=self.stop_recording,
        )
        self.recording_stop_btn.pack(anchor="w", pady=(0, 12))

        levels_frame = ttk.Frame(self.recording_frame, style="Tab.TFrame")
        levels_frame.pack(fill="x", pady=(0, 12))

        system_card, system_frame = self._card(levels_frame, padx=10, pady=10)
        system_card.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        ttk.Label(system_frame, text="相手音声", style="Section.Card.TLabel").pack(anchor="w")
        self.recording_system_device_label = ttk.Label(
            system_frame,
            textvariable=self.recording_system_device_var,
            style="Muted.Card.TLabel",
            wraplength=170,
            justify="left",
        )
        self.recording_system_device_label.pack(anchor="w", fill="x", pady=(6, 4))
        system_meter_frame = ttk.Frame(system_frame, style="Card.TFrame")
        system_meter_frame.pack(fill="x")
        self.recording_system_bar = ttk.Progressbar(
            system_meter_frame,
            orient="horizontal",
            mode="determinate",
            maximum=100,
            style="Soft.Horizontal.TProgressbar",
        )
        self.recording_system_bar.pack(side="left", fill="x", expand=True, padx=(0, 6))
        ttk.Label(
            system_meter_frame,
            textvariable=self.recording_system_percent_var,
            style="Card.TLabel",
            width=4,
            anchor="e",
        ).pack(side="right")

        mic_card, mic_frame = self._card(levels_frame, padx=10, pady=10)
        mic_card.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        ttk.Label(mic_frame, text="マイク", style="Section.Card.TLabel").pack(anchor="w")
        self.recording_mic_device_label = ttk.Label(
            mic_frame,
            textvariable=self.recording_mic_device_var,
            style="Muted.Card.TLabel",
            wraplength=170,
            justify="left",
        )
        self.recording_mic_device_label.pack(anchor="w", fill="x", pady=(6, 4))
        mic_meter_frame = ttk.Frame(mic_frame, style="Card.TFrame")
        mic_meter_frame.pack(fill="x")
        self.recording_mic_bar = ttk.Progressbar(
            mic_meter_frame,
            orient="horizontal",
            mode="determinate",
            maximum=100,
            style="Soft.Horizontal.TProgressbar",
        )
        self.recording_mic_bar.pack(side="left", fill="x", expand=True, padx=(0, 6))
        ttk.Label(
            mic_meter_frame,
            textvariable=self.recording_mic_percent_var,
            style="Card.TLabel",
            width=4,
            anchor="e",
        ).pack(side="right")

        levels_frame.columnconfigure(0, weight=1)
        levels_frame.columnconfigure(1, weight=1)

        memo_card, memo_frame = self._card(self.recording_frame, padx=10, pady=10)
        memo_card.pack(fill="both", expand=True)
        self.memo_text = tk.Text(
            memo_frame,
            height=12,
            wrap="word",
            relief="flat",
            borderwidth=0,
            bg=self.card_color,
            fg=self.text_color,
            insertbackground=self.accent_color,
            font=("Yu Gothic UI", 10),
        )
        self.memo_text.pack(fill="both", expand=True)

    def show_recording_view(self):
        self.notebook.pack_forget()
        self.recording_frame.pack(fill="both", expand=True)

    def show_main_view(self):
        self.recording_frame.pack_forget()
        self.notebook.pack(fill="both", expand=True, padx=14, pady=14)

    def update_recording_device_labels(self):
        system_name = self.system_combo.get() or "相手音声"
        mic_name = self.mic_combo.get() or "マイク"
        self.recording_system_device_var.set(system_name)
        self.recording_mic_device_var.set(mic_name)
        self.update_recording_level_values(self.recorder.system_level, self.recorder.mic_level)

    def update_recording_level_values(self, system_level, mic_level):
        self.recording_system_percent_var.set(f"{system_level}%")
        self.recording_mic_percent_var.set(f"{mic_level}%")
        if hasattr(self, "recording_system_bar"):
            self.recording_system_bar["value"] = system_level
        if hasattr(self, "recording_mic_bar"):
            self.recording_mic_bar["value"] = mic_level

    def save_recording_memo(self, output_dir):
        memo = self.memo_text.get("1.0", "end").strip()
        if not memo:
            return None

        memo_path = Path(output_dir) / "memo.txt"
        memo_path.write_text(memo, encoding="utf-8")
        self.add_log(f"メモ保存: {memo_path}")
        return memo_path

    def transcript_txt_path(self, output_dir, settings=None):
        settings = settings or self.app_settings
        output_dir = Path(output_dir)
        model_size = settings.get("model_size", DEFAULT_SETTINGS["model_size"])
        model_code = MODEL_FILENAME_CODES.get(model_size, model_size.replace("-", ""))
        beam_size = int(settings.get("beam_size", DEFAULT_SETTINGS["beam_size"]))
        return output_dir / f"{output_dir.name}_{model_code}_{beam_size}.txt"

    def selected_model_size(self):
        selected = self.model_var.get()
        for model_size, label in MODEL_CHOICES.items():
            if label == selected:
                return model_size
        return DEFAULT_SETTINGS["model_size"]

    def run_on_ui_thread(self, func, *args, **kwargs):
        if threading.get_ident() == self.main_thread_id:
            func(*args, **kwargs)
        else:
            self.root.after(0, lambda: func(*args, **kwargs))

    def refresh_settings_summary(self):
        if not hasattr(self, "settings_summary_var"):
            return
        model_size = self.selected_model_size()
        mode = self.mode_var.get() if self.mode_var.get() in MODE_CHOICES else DEFAULT_SETTINGS["mode"]
        beam_size = MODE_CHOICES[mode]
        self.settings_summary_var.set(
            f"選択中の起動中だけ有効です。次回の文字起こしから反映します。model={model_size}, "
            f"beam_size={beam_size}, compute_type={COMPUTE_TYPE}。"
            "初回利用時はモデルのダウンロードに時間がかかります。"
        )

    def on_transcription_setting_changed(self, event=None):
        if self.transcription_running:
            self.model_var.set(MODEL_CHOICES[self.app_settings["model_size"]])
            self.mode_var.set(self.app_settings["mode"])
            self.api_key_var.set(self.app_settings.get("openai_api_key", ""))
            self.refresh_settings_summary()
            safe_messagebox_error("エラー", "文字起こし中は設定を変更できません。完了後に変更してください。")
            return

        mode = self.mode_var.get() if self.mode_var.get() in MODE_CHOICES else DEFAULT_SETTINGS["mode"]
        settings = {
            "model_size": self.selected_model_size(),
            "mode": mode,
            "beam_size": MODE_CHOICES[mode],
            "compute_type": COMPUTE_TYPE,
            "openai_api_key": self.api_key_var.get().strip(),
        }

        self.app_settings = settings
        self.transcriber.set_settings(settings)
        self.refresh_settings_summary()
        self.add_log(
            f"文字起こし設定を変更: model={settings['model_size']}, "
            f"mode={settings['mode']}, beam_size={settings['beam_size']}"
        )

    def load_devices(self):
        try:
            self.status_var.set("デバイス読み込み中")

            self.system_devices, self.mic_devices = AudioRecorder.list_devices()

            system_names = [
                AudioRecorder.device_label(d, fallback_kind="スピーカー")
                for d in self.system_devices
            ]

            mic_names = [
                AudioRecorder.device_label(d, fallback_kind="マイク")
                for d in self.mic_devices
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

            self.memo_text.delete("1.0", "end")
            self.recorder.start(system_device_index, mic_device_index)
            self.set_lid_action_keep_running()

            self.status_var.set("録音中")
            self.current_transcription_var.set("文字起こし: 待機中")
            self.update_recording_device_labels()
            self.show_recording_view()
            self.update_recording_visual_state(is_recording=True)
            self.elapsed_seconds = 0
            self.update_timer()
            self.update_level_meter()

            threading.Thread(
                target=self.transcriber.preload_model,
                daemon=True
            ).start()

            self.start_btn.config(state="disabled")
            self.stop_btn.config(state="normal")
            self.recording_stop_btn.config(state="normal")
            self.reload_btn.config(state="disabled")
            self.system_combo.config(state="disabled")
            self.mic_combo.config(state="disabled")

        except Exception as e:
            write_error_log("App.start_recording error", e)
            self.restore_lid_action()
            self.status_var.set("エラー")
            self.add_log(f"録音開始失敗: {e}")
            if "デバイス情報の取得に失敗" in str(e):
                self.add_log("デバイス構成が変化した可能性があります。デバイスを再読み込みします。")
                self.load_devices()
            safe_messagebox_error(
                "エラー",
                f"録音開始に失敗しました。\n\n{e}\n\n詳細は {ERROR_LOG} を確認してください。"
            )

    def stop_recording(self):
        try:
            self.status_var.set("録音停止処理中")
            self.stop_btn.config(state="disabled")
            self.recording_stop_btn.config(state="disabled")

            if self.timer_job:
                self.root.after_cancel(self.timer_job)
                self.timer_job = None

            if self.level_job:
                self.root.after_cancel(self.level_job)
                self.level_job = None

            result = self.recorder.stop()
            self.restore_lid_action()
            self.update_recording_visual_state(is_recording=False)
            self.show_main_view()

            self.reset_level_meter()

            if not result:
                self.status_var.set("待機中")
                self.enable_controls()
                self.schedule_preview_level_meter()
                return

            self.last_output_dir = result["output_dir"]
            result["memo_path"] = self.save_recording_memo(result["output_dir"])

            self.enqueue_transcription_job(result)
            self.add_log("録音停止後に文字起こしキューへ追加しました。文字起こし開始ボタンを押してください。")
            self.status_var.set("待機中")
            self.enable_controls()
            self.schedule_preview_level_meter()

        except Exception as e:
            write_error_log("App.stop_recording error", e)
            self.status_var.set("エラー")
            self.add_log(f"録音停止失敗: {e}")
            self.enable_controls()
            safe_messagebox_error(
                "エラー",
                f"録音停止に失敗しました。\n\n{e}\n\n詳細は {ERROR_LOG} を確認してください。"
            )
            self.update_recording_visual_state(is_recording=False)
            self.show_main_view()
            self.restore_lid_action()

    def run_transcription(self, result, notify=True):
        try:
            job_name = Path(result["output_dir"]).name
            self.root.after(0, lambda name=job_name: self.set_transcription_status(name))
            result["txt_out"] = self.transcript_txt_path(result["output_dir"], self.app_settings)
            system_rows = self.transcriber.transcribe_file(
                result["system_wav"],
                "相手"
            )

            mic_rows = self.transcriber.transcribe_file(
                result["mic_wav"],
                "自分"
            )

            all_rows = system_rows + mic_rows
            memo_path = result.get("memo_path")
            memo_text = ""
            if memo_path and Path(memo_path).exists():
                memo_text = Path(memo_path).read_text(encoding="utf-8")

            self.transcriber.export_transcript(all_rows, result["txt_out"], memo_text=memo_text)
            if memo_path and Path(memo_path).exists():
                Path(memo_path).unlink()
                self.add_log(f"メモ一時ファイル削除: {memo_path}")

            self.add_log("文字起こしが完了しました。")
            self.add_log(f"保存フォルダ: {result['output_dir']}")

            self.run_on_ui_thread(self.status_var.set, "完了")
            self.run_on_ui_thread(self.current_transcription_var.set, "文字起こし: 完了")
            if notify:
                self.run_on_ui_thread(
                    safe_messagebox_info,
                    "完了",
                    f"文字起こしが完了しました。\n\n保存先:\n{result['txt_out']}"
                )
            return True

        except Exception as e:
            write_error_log("App.run_transcription error", e)
            self.run_on_ui_thread(self.status_var.set, "エラー")
            self.run_on_ui_thread(self.current_transcription_var.set, "文字起こし: エラー")
            self.add_log(f"文字起こしエラー: {e}")
            self.run_on_ui_thread(
                safe_messagebox_error,
                "エラー",
                f"文字起こしに失敗しました。\n\n{e}\n\n詳細は {ERROR_LOG} を確認してください。"
            )
            return False

    def enqueue_transcription_job(self, result):
        self.transcription_queue.append(result)
        self.refresh_queue_listbox()
        self.add_log(f"文字起こしキュー追加: {result['output_dir']}")

    def refresh_queue_listbox(self):
        self.queue_listbox.delete(0, "end")
        for idx, item in enumerate(self.transcription_queue, start=1):
            self.queue_listbox.insert("end", f"{idx}. {item['output_dir']}")
        if hasattr(self, "queue_count_label"):
            self.queue_count_label.config(text=f"追加済みファイル（{len(self.transcription_queue)} 件）")

    def set_transcription_status(self, job_name):
        self.current_transcription_var.set(f"文字起こし: {job_name}")
        self.status_var.set(f"文字起こし中: {job_name}")
        self.refresh_status_banner()

    def add_queue_from_dialog(self):
        folder = filedialog.askdirectory(title="文字起こし対象フォルダを選択")
        if not folder:
            return
        folder = Path(folder)
        job = {
            "output_dir": folder,
            "system_wav": folder / "system.wav",
            "mic_wav": folder / "mic.wav",
            "txt_out": self.transcript_txt_path(folder),
            "memo_path": folder / "memo.txt",
        }
        if not job["system_wav"].exists() or not job["mic_wav"].exists():
            safe_messagebox_error("エラー", "system.wav と mic.wav が見つかりません。")
            return
        self.enqueue_transcription_job(job)

    def remove_selected_queue(self):
        selected = self.queue_listbox.curselection()
        if not selected:
            return
        for i in reversed(selected):
            del self.transcription_queue[i]
        self.refresh_queue_listbox()

    def start_transcription_queue(self):
        if self.transcription_running:
            self.add_log("文字起こしキューは既に実行中です。")
            return
        if not self.transcription_queue:
            self.add_log("文字起こしキューが空です。")
            self.current_transcription_var.set("文字起こし: キューが空です")
            return
        self.transcription_running = True
        self.status_var.set("文字起こしキュー実行中")
        threading.Thread(target=self._run_transcription_queue_worker, daemon=True).start()

    def _run_transcription_queue_worker(self):
        try:
            total = len(self.transcription_queue)
            done = 0
            while self.transcription_queue:
                job = self.transcription_queue.pop(0)
                self.root.after(0, self.refresh_queue_listbox)
                self.root.after(0, lambda p=job["output_dir"]: self.add_log(f"キュー処理開始: {p}"))
                ok = self.run_transcription(job, notify=False)
                if ok:
                    done += 1
                    self.root.after(0, lambda d=done, t=total: self.status_var.set(f"文字起こしキュー実行中 ({d}/{t})"))
            self.root.after(0, lambda d=done, t=total: safe_messagebox_info("完了", f"文字起こしキューが完了しました。{d}/{t} 件成功。"))
        finally:
            self.transcription_running = False
            self.root.after(0, lambda: self.status_var.set("待機中"))
            self.root.after(0, lambda: self.current_transcription_var.set("文字起こし: 待機中"))

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
        self.recording_stop_btn.config(state="normal")
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
        tried = set()
        dev_info = self.preview_audio.get_device_info_by_index(device["index"])
        max_input_channels = int(dev_info.get("maxInputChannels", 0))
        candidate_channels = [device.get("channels", 1), max_input_channels, 1]

        for channels in candidate_channels:
            channels = int(channels) if channels else 0
            if channels <= 0 or channels in tried:
                continue
            tried.add(channels)
            if max_input_channels > 0 and channels > max_input_channels:
                continue
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
        self.add_log(
            f"事前感度表示をスキップ: {device.get('name', 'unknown')} ({last_error})"
        )
        return None

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
                AudioRecorder._is_headset_device({"name": system_name})
                or AudioRecorder._is_headphone_device({"name": system_name})
                or AudioRecorder._is_headset_device({"name": mic_name})
                or AudioRecorder._is_headphone_device({"name": mic_name})
            )

            if unstable_pair:
                self.add_log("Bluetooth/ハンズフリー機器を検出。事前感度表示はマイクのみ有効化します。")
                mic_stream = self._open_preview_stream(mic_device)
                if mic_stream:
                    self.preview_streams.append(("mic_level", mic_stream))
                self.recorder.system_level = 0
            else:
                system_stream = self._open_preview_stream(system_device)
                mic_stream = self._open_preview_stream(mic_device)
                if system_stream:
                    self.preview_streams.append(("system_level", system_stream))
                if mic_stream:
                    self.preview_streams.append(("mic_level", mic_stream))

            if not self.preview_streams:
                self.add_log("選択デバイスでは事前感度表示を開始できませんでした。録音は実行可能です。")
                self.recorder.system_level = 0
                self.recorder.mic_level = 0
                return

            self.preview_running = True
            self.preview_thread = threading.Thread(target=self._preview_loop, daemon=True)
            self.preview_thread.start()
            self.update_level_meter()
        except Exception as e:
            write_error_log("App.start_preview_level_meter fatal error", e)
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
        self.refresh_status_banner()

        self.elapsed_seconds += 1
        self.timer_job = self.root.after(1000, self.update_timer)

    def update_level_meter(self):
        if not self.recorder.is_recording and not self.preview_running:
            self.reset_level_meter()
            return

        system_level = self.recorder.system_level
        mic_level = self.recorder.mic_level

        if self.recorder.is_recording:
            self.update_recording_level_values(system_level, mic_level)

        self.level_job = self.root.after(100, self.update_level_meter)

    def reset_level_meter(self):
        self.recording_system_device_var.set("相手音声")
        self.recording_mic_device_var.set("マイク")
        self.update_recording_level_values(0, 0)

    def update_recording_visual_state(self, is_recording):
        self.root.configure(bg=self.bg_color)
        self.root.title(APP_TITLE)
        self.refresh_status_banner()
        self._flash_taskbar_once()

    def refresh_status_banner(self):
        if not hasattr(self, "status_banner"):
            return

        status = self.status_var.get()
        if "録音中" in status:
            bg = self.accent_color
            title_fg = "#ffffff"
            detail_fg = "#ffffff"
            self.status_title_var.set("● 録音中")
            self.status_detail_var.set(self.timer_var.get())
        elif "文字起こし中" in status or self.transcription_running:
            bg = "#fff0f5"
            title_fg = self.accent_color
            detail_fg = self.text_color
            self.status_title_var.set("文字起こし中")
            self.status_detail_var.set("録音データを txt に変換しています")
        elif "エラー" in status:
            bg = "#fff1f2"
            title_fg = "#be123c"
            detail_fg = "#be123c"
            self.status_title_var.set("エラー")
            self.status_detail_var.set("詳細はログを確認してください")
        elif "デバイス" in status:
            bg = "#ffffff"
            title_fg = self.text_color
            detail_fg = self.muted_color
            self.status_title_var.set(status)
            self.status_detail_var.set("録音デバイスの状態を確認しています")
        else:
            bg = "#ffffff"
            title_fg = self.text_color
            detail_fg = self.muted_color
            self.status_title_var.set("停止中")
            self.status_detail_var.set("録音は開始されていません")

        for widget in (
            self.status_banner,
            self.status_title_label,
            self.status_detail_label,
            self.current_transcription_label,
        ):
            widget.configure(bg=bg)
        self.status_title_label.configure(fg=title_fg)
        self.status_detail_label.configure(fg=detail_fg)
        self.current_transcription_label.configure(fg=detail_fg)

    def _flash_taskbar_once(self):
        if os.name != "nt":
            return
        try:
            hwnd = self.root.winfo_id()
            class FLASHWINFO(ctypes.Structure):
                _fields_ = [
                    ("cbSize", ctypes.c_uint),
                    ("hwnd", ctypes.c_void_p),
                    ("dwFlags", ctypes.c_uint),
                    ("uCount", ctypes.c_uint),
                    ("dwTimeout", ctypes.c_uint),
                ]
            FLASHW_TRAY = 0x2
            info = FLASHWINFO(
                cbSize=ctypes.sizeof(FLASHWINFO),
                hwnd=ctypes.c_void_p(hwnd),
                dwFlags=FLASHW_TRAY,
                uCount=2,
                dwTimeout=0,
            )
            ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
        except Exception as e:
            write_error_log("App._flash_taskbar_once error", e)

    def _set_lid_action(self, ac_value, dc_value):
        if os.name != "nt":
            self.add_log("この機能は Windows のみ対応です。")
            return
        commands = [
            ["powercfg", "/SETACVALUEINDEX", "SCHEME_CURRENT", "SUB_BUTTONS", LIDCLOSE_GUID, str(ac_value)],
            ["powercfg", "/SETDCVALUEINDEX", "SCHEME_CURRENT", "SUB_BUTTONS", LIDCLOSE_GUID, str(dc_value)],
            ["powercfg", "/SETACTIVE", "SCHEME_CURRENT"],
        ]
        for cmd in commands:
            subprocess.run(cmd, check=True, capture_output=True, text=True)

    def _get_lid_action_value(self, power_mode):
        result = subprocess.run(
            ["powercfg", f"/GET{power_mode}VALUEINDEX", "SCHEME_CURRENT", "SUB_BUTTONS", LIDCLOSE_GUID],
            check=True,
            capture_output=True,
            text=True,
        )
        matches = re.findall(r"0x[0-9a-fA-F]+|\b\d+\b", result.stdout)
        if not matches:
            raise RuntimeError(f"powercfg {power_mode} value not found: {result.stdout.strip()}")
        return int(matches[-1], 0)

    def _format_powercfg_error(self, error):
        if isinstance(error, subprocess.CalledProcessError):
            detail = (error.stderr or error.stdout or "").strip()
            if detail:
                return detail
            return f"powercfg exited with code {error.returncode}"
        return str(error)

    def load_lid_action_settings(self):
        if os.name != "nt":
            self.add_log("電源設定確認をスキップ: Windows 以外の環境です。")
            return

        try:
            self.original_lid_action = (
                self._get_lid_action_value("AC"),
                self._get_lid_action_value("DC"),
            )
            self.lid_action_available = True
            self.add_log(
                "電源設定取得: カバーを閉じたときの動作 "
                f"AC={self.original_lid_action[0]}, DC={self.original_lid_action[1]}"
            )
        except Exception as e:
            self.original_lid_action = None
            self.lid_action_available = False
            self.add_log(
                "カバー動作設定を取得できませんでした。デスクトップPCまたは非対応環境のため、"
                "録音中の電源設定変更をスキップします。"
            )
            self.add_log(f"powercfg取得結果: {self._format_powercfg_error(e)}")

    def set_lid_action_keep_running(self):
        try:
            if not self.lid_action_available or self.original_lid_action is None:
                self.add_log("電源設定変更をスキップ: カバー動作設定を起動時に取得できませんでした。")
                return
            self.lid_action_changed = True
            self._set_lid_action(ac_value=0, dc_value=0)
            self.add_log("電源設定変更: カバーを閉じたときの動作 = 何もしない")
        except Exception as e:
            write_error_log("App.set_lid_action_keep_running error", e)
            self.add_log(f"電源設定変更失敗（録音中）: {e}")

    def restore_lid_action(self):
        if os.name != "nt" or not self.lid_action_changed or self.original_lid_action is None:
            return
        try:
            ac_value, dc_value = self.original_lid_action
            self._set_lid_action(ac_value=ac_value, dc_value=dc_value)
            self.add_log(
                "電源設定復元: カバーを閉じたときの動作 "
                f"AC={ac_value}, DC={dc_value}"
            )
            self.lid_action_changed = False
        except Exception as e:
            write_error_log("App.restore_lid_action error", e)
            self.add_log(f"電源設定復元失敗（停止時）: {e}")

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
            self.restore_lid_action()
            self.update_recording_visual_state(is_recording=False)

        except Exception as e:
            write_error_log("App.on_close error", e)

        finally:
            self.root.destroy()


def global_exception_handler(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        return

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
    try:
        root.mainloop()
    except KeyboardInterrupt:
        try:
            app.on_close()
        except tk.TclError:
            pass


if __name__ == "__main__":
    main()
