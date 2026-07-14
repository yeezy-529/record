import math
import os
import sys
import re
import audioop
import difflib
import json
import struct
import threading
import time
import subprocess
import traceback
import wave
import ctypes
import mimetypes
import uuid
import urllib.request
import urllib.error
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from datetime import datetime
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import customtkinter as ctk
from PIL import Image, ImageDraw, ImageTk

import pyaudiowpatch as pyaudio
from faster_whisper import WhisperModel

# --- ノイズ除去（任意）: 未インストールでも起動できるようにフラグで分岐する ---
try:
    import numpy as np
except Exception:
    np = None
try:
    import noisereduce as nr
except Exception:
    nr = None
NOISEREDUCE_AVAILABLE = nr is not None and np is not None

# --- OSマイクミュート連携（任意・Windows/pycaw）: 未インストールでも起動できるようにする ---
try:
    import comtypes
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    from pycaw.constants import EDataFlow
    from comtypes import CLSCTX_ALL
    PYCAW_AVAILABLE = True
except Exception:
    PYCAW_AVAILABLE = False


# =========================
# 設定
# =========================
APP_TITLE = "レコードApp"
# PRごとにこのバージョンを更新し、PRタイトルにも同じバージョンを含める。
APP_VERSION = "1.08"

BASE_DIR = Path.cwd() / "mtg_records"
BASE_DIR.mkdir(parents=True, exist_ok=True)

ERROR_LOG = BASE_DIR / "error_log.txt"

FORMAT = pyaudio.paInt16
CHUNK = 2048
LANGUAGE = "ja"

NOISE_GATE_PERCENT = 5  # これ未満の音量(%)は無音として扱う（暗騒音カット）
MIC_MUTE_POLL_INTERVAL_SEC = 0.4  # OS側マイクミュート状態を確認する間隔(秒)

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
    "gpt-4o-transcribe-diarize": "gpt-4o-transcribe-diarize（API・話者分離）",
}

MODEL_FILENAME_CODES = {
    "medium": "m",
    "large-v3": "la3",
    "large-v3-turbo": "la3t",
    "gpt-4o-mini-transcribe": "4ominit",
    "gpt-4o-transcribe": "4ot",
    "gpt-4o-transcribe-diarize": "4otd",
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
    "vocab_prompt": "",
    "noise_reduction": False,
}

API_TRANSCRIBE_MODELS = {"gpt-4o-mini-transcribe", "gpt-4o-transcribe", "gpt-4o-transcribe-diarize"}

# OpenAI 音声文字起こしAPIは 1リクエストあたり 25MB まで。
# 25MB の上限に対して安全マージンを取ったサイズを分割の基準にする。
API_UPLOAD_LIMIT_BYTES = 25 * 1024 * 1024
API_UPLOAD_SAFETY_BYTES = 24 * 1024 * 1024
# 送信音声のビットレート。話者分離の精度を確保するため 32k から引き上げる。
API_CHUNK_BITRATE = "64k"
API_CHUNK_BITRATE_BPS = 64000
# ビットレートとサイズ上限からチャンクの最大秒数を逆算する。
# コンテナ等のオーバーヘッド分を割り引く。約48分となり、大半の会議は
# 分割不要（＝話者分離がチャンクをまたがない）になる。
API_CHUNK_SECONDS = int(API_UPLOAD_SAFETY_BYTES * 8 / API_CHUNK_BITRATE_BPS * 0.92)
API_OVERLAP_SECONDS = 3
API_KEY_MASK = "******************"
# 文字起こしAPI/ローカルモデルへ渡す言語ヒント。日本語であることを明示して
# 英語混入や言語の取り違えを減らす。固有名詞（設定画面入力）と結合して使う。
API_JAPANESE_PROMPT_HINT = "以下は日本語の会議音声です。"

TRANSCRIPTION_PATTERNS = {
    "online_one_to_one": "オンラインMTG: 自分 + 相手1人",
    "online_multi": "オンラインMTG: 自分 + 相手複数",
    "offline_mic_only": "オフラインMTG: 自分マイクのみ",
    "manual": "マニュアル: 文字起こし設定を使う",
}
DEFAULT_TRANSCRIPTION_PATTERN = "online_one_to_one"
PARALLEL_TRANSCRIPTION_PATTERNS = {"online_one_to_one", "online_multi"}

PATTERN_ROUTES = {
    "online_one_to_one": (
        {"audio_key": "mic_wav", "speaker": "自分", "source": "mic", "model": "large-v3"},
        {"audio_key": "system_wav", "speaker": "相手", "source": "system", "model": "gpt-4o-transcribe"},
    ),
    "online_multi": (
        {"audio_key": "mic_wav", "speaker": "自分", "source": "mic", "model": "large-v3"},
        {
            "audio_key": "system_wav",
            "speaker": "相手",
            "source": "system",
            "model": "gpt-4o-transcribe-diarize",
            "diarized_prefix": "相手",
        },
    ),
    "offline_mic_only": (
        {
            "audio_key": "mic_wav",
            "speaker": "話者",
            "source": "mic",
            "model": "gpt-4o-transcribe-diarize",
            "diarized_prefix": "話者",
        },
    ),
}


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

        # ミュート・ノイズ関連
        self.mic_muted_by_os = False       # OS側マイクミュートと同期した状態（自動）
        self.system_muted_by_user = False  # UIからのスピーカー（相手音声）録音ミュート（手動）
        self.noise_reduction_enabled = False
        self._mic_mute_poll_running = False
        self._mic_mute_poll_thread = None

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

            self._start_mic_mute_watch(self.mic_device.get("name", ""))

            self.log(f"録音保存先: {self.output_dir}")
            self.log("録音を開始しました。")
            if not PYCAW_AVAILABLE:
                self.log("pycawが利用できないため、OSマイクミュートとの同期は無効です。")

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
        # プレビュー用ストリームを閉じた直後などは、WASAPI 側でデバイスの
        # 解放が完了しておらず open が一時的に失敗することがある。
        # 少し待って数回リトライすることで「録音がエラーで始まらない」現象を防ぐ。
        for stream_attempt in range(3):
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
            if stream_attempt < 2:
                self.log(
                    f"{label} のデバイスがまだ使用中の可能性があります。"
                    f"少し待って再試行します（{stream_attempt + 1}/3）。"
                )
                time.sleep(0.4)

        write_error_log(f"{label} open failed all candidates", last_error)
        raise RuntimeError(f"{label} の録音開始に失敗しました: {last_error}")

    def _capture_loop(self, stream, level_attr, frame_attr, log_title, log_message, kind):
        while self.is_recording:
            try:
                data = stream.read(CHUNK, exception_on_overflow=False)
                level = self._calc_level(data)
                data, level = self._apply_mute_and_gate(kind, data, level)
                setattr(self, level_attr, level)

                with self.frame_lock:
                    getattr(self, frame_attr).append(data)

            except Exception as e:
                write_error_log(log_title, e)
                self.log(f"{log_message}: {e}")
                self.is_recording = False
                break

    def _apply_mute_and_gate(self, kind, data, level):
        # OS側マイクミュート（自動）・UIからのスピーカーミュート（手動）・
        # 5%未満の暗騒音カット（常時）を反映し、対象なら無音データに置き換える。
        muted = False
        if kind == "mic" and self.mic_muted_by_os:
            muted = True
        elif kind == "system" and self.system_muted_by_user:
            muted = True
        if not muted and level < NOISE_GATE_PERCENT:
            muted = True
        if muted:
            return b"\x00" * len(data), 0
        return data, level

    def _system_loop(self):
        self._capture_loop(
            stream=self.system_stream,
            level_attr="system_level",
            frame_attr="system_frames",
            log_title="AudioRecorder._system_loop error",
            log_message="相手音声読み取りエラー",
            kind="system",
        )

    def _mic_loop(self):
        self._capture_loop(
            stream=self.mic_stream,
            level_attr="mic_level",
            frame_attr="mic_frames",
            log_title="AudioRecorder._mic_loop error",
            log_message="マイク読み取りエラー",
            kind="mic",
        )

    def set_system_muted(self, muted):
        self.system_muted_by_user = bool(muted)

    def set_noise_reduction(self, enabled):
        self.noise_reduction_enabled = bool(enabled)

    def _start_mic_mute_watch(self, device_name):
        self._stop_mic_mute_watch()
        if not PYCAW_AVAILABLE:
            return
        self._mic_mute_poll_running = True
        self._mic_mute_poll_thread = threading.Thread(
            target=self._mic_mute_poll_loop, args=(device_name,), daemon=True
        )
        self._mic_mute_poll_thread.start()

    def _stop_mic_mute_watch(self):
        self._mic_mute_poll_running = False
        if self._mic_mute_poll_thread:
            self._mic_mute_poll_thread.join(timeout=1)
            self._mic_mute_poll_thread = None
        self.mic_muted_by_os = False

    def _mic_mute_poll_loop(self, device_name):
        try:
            comtypes.CoInitialize()
        except Exception:
            pass
        try:
            while self._mic_mute_poll_running:
                muted = self._query_capture_mute(device_name)
                if muted is not None:
                    self.mic_muted_by_os = muted
                time.sleep(MIC_MUTE_POLL_INTERVAL_SEC)
        finally:
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass

    @staticmethod
    def _query_capture_mute(device_name):
        # pyaudiowpatch(WASAPI)のマイク名とWindows Core Audioのエンドポイント名を
        # 部分一致でマッチングし、そのエンドポイントのミュート状態を返す。
        # 一致しない/エラー時はNone（呼び出し側は既存の状態を維持する）。
        if not PYCAW_AVAILABLE or not device_name:
            return None
        target = str(device_name).strip().lower()
        try:
            enumerator = AudioUtilities.GetDeviceEnumerator()
            collection = enumerator.EnumAudioEndpoints(EDataFlow.eCapture.value, 1)
            for i in range(collection.GetCount()):
                dev = collection.Item(i)
                try:
                    friendly = (AudioUtilities.CreateDevice(dev).FriendlyName or "").strip().lower()
                except Exception:
                    friendly = ""
                if friendly and (friendly in target or target in friendly):
                    iface = dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
                    vol = iface.QueryInterface(IAudioEndpointVolume)
                    return bool(vol.GetMute())
        except Exception as e:
            write_error_log("AudioRecorder._query_capture_mute error", e)
        return None

    def stop(self):
        if not self.is_recording and not self.p:
            return None

        self.is_recording = False
        self._stop_mic_mute_watch()

        # キャプチャスレッドの stream.read() はブロッキング呼び出し。
        # デバイス切断などで read() が返らないまま別スレッドから stream.close()
        # を呼ぶと PortAudio がネイティブレベルでクラッシュし、アプリが落ちて
        # 録音データも保存されないことがある（「停止したら落ちて録画できていない」現象）。
        # そのため「スレッドが確実に終了してから」stream を閉じる。
        system_alive = self._join_capture_thread("system")
        mic_alive = self._join_capture_thread("mic")

        self._safe_close_stream("system", thread_alive=system_alive)
        self._safe_close_stream("mic", thread_alive=mic_alive)

        streams_stuck = system_alive or mic_alive

        result = None

        try:
            if self.output_dir and (self.system_frames or self.mic_frames):
                with self.frame_lock:
                    system_frames = list(self.system_frames)
                    mic_frames = list(self.mic_frames)

                if system_frames and self.noise_reduction_enabled:
                    system_frames = self._maybe_reduce_noise(
                        system_frames, self.system_channels, self.system_rate, "相手音声"
                    )
                if mic_frames and self.noise_reduction_enabled:
                    mic_frames = self._maybe_reduce_noise(
                        mic_frames, self.mic_channels, self.mic_rate, "マイク"
                    )

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
            # スレッドがまだ read() でブロック中の場合、p.terminate() も
            # 内部でストリームを閉じてクラッシュしうるため terminate しない。
            # PyAudio インスタンスはリークするが、次回録音開始時に作り直す。
            try:
                if self.p and not streams_stuck:
                    self.p.terminate()
                    self.p = None
                elif streams_stuck:
                    self.log("オーディオデバイスの解放をスキップしました（次回録音時に再初期化されます）。")
            except Exception as e:
                write_error_log("terminate PyAudio error", e)

            self.system_level = 0
            self.mic_level = 0

        return result

    def _join_capture_thread(self, kind):
        thread = getattr(self, f"{kind}_thread")
        if not thread:
            return False
        # 通常 read() は数十msで返るため、十分な猶予をとって待つ。
        thread.join(timeout=5)
        alive = thread.is_alive()
        if not alive:
            setattr(self, f"{kind}_thread", None)
        else:
            write_error_log(
                f"stop {kind} thread still alive",
                RuntimeError(f"{kind} capture thread did not stop in time"),
            )
        return alive

    def _safe_close_stream(self, kind, thread_alive):
        stream = getattr(self, f"{kind}_stream")
        if not stream:
            return
        if thread_alive:
            # スレッドが read() でブロック中の可能性があるため close しない。
            # ハンドルはリークするが、次回録音時に PyAudio を作り直すため許容する。
            self.log(f"{kind} のキャプチャが停止しないため、ストリームを安全に閉じられませんでした。")
            setattr(self, f"{kind}_stream", None)
            return
        try:
            stream.stop_stream()
            stream.close()
        except Exception as e:
            write_error_log(f"stop {kind}_stream error", e)
        finally:
            setattr(self, f"{kind}_stream", None)

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

    def _maybe_reduce_noise(self, frames, channels, rate, label):
        if not NOISEREDUCE_AVAILABLE:
            self.log(f"{label}: noisereduceが利用できないためノイズ除去をスキップしました。")
            return frames
        try:
            pcm = b"".join(frames)
            min_bytes = rate * 2 * max(1, channels)
            if len(pcm) < min_bytes:
                # 1秒未満はノイズプロファイルの推定に不十分なためスキップする。
                return frames
            arr = np.frombuffer(pcm, dtype=np.int16)
            if channels > 1:
                arr = arr.reshape(-1, channels)
                reduced_cols = [
                    nr.reduce_noise(y=arr[:, c], sr=rate, stationary=True, prop_decrease=0.85)
                    for c in range(channels)
                ]
                reduced = np.stack(reduced_cols, axis=1).reshape(-1).astype(np.int16)
            else:
                reduced = nr.reduce_noise(
                    y=arr, sr=rate, stationary=True, prop_decrease=0.85
                ).astype(np.int16)
            self.log(f"{label}: ノイズ除去を適用しました。")
            return [reduced.tobytes()]
        except Exception as e:
            write_error_log(f"AudioRecorder._maybe_reduce_noise error: {label}", e)
            self.log(f"{label}: ノイズ除去に失敗しました（元の音声のまま保存します）: {e}")
            return frames

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
            subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
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
        self.openai_api_key_from_env = False
        self.vocab_prompt = ""
        self.model_lock = threading.Lock()

    def resolve_api_key(self, user_api_key=""):
        env_key = (os.getenv("OPENAI_API_KEY", "") or "").strip()
        if env_key:
            self.openai_api_key_from_env = True
            self.openai_api_key = env_key
            return env_key
        self.openai_api_key_from_env = False
        self.openai_api_key = (user_api_key or "").strip()
        return self.openai_api_key

    def set_settings(self, settings):
        with self.model_lock:
            model_size = settings["model_size"]
            compute_type = settings["compute_type"]
            self.resolve_api_key(settings.get("openai_api_key", ""))
            if model_size != self.model_size or compute_type != self.compute_type:
                self.model = None
            self.model_size = model_size
            self.compute_type = compute_type
            self.beam_size = int(settings["beam_size"])
            self.vocab_prompt = (settings.get("vocab_prompt", "") or "").strip()

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

    def use_model(self, model_size):
        with self.model_lock:
            if model_size != self.model_size:
                self.model = None
                self.model_size = model_size

    def transcribe_file(self, audio_path, speaker_label, source="unknown", model_size=None, diarized_prefix=None):
        try:
            if model_size:
                self.use_model(model_size)
            self.log(f"文字起こし開始: {speaker_label}")
            rows = []
            if self.model_size in API_TRANSCRIBE_MODELS:
                rows = self._transcribe_file_api(audio_path, speaker_label, diarized_prefix=diarized_prefix)
            else:
                self.preload_model()
                # 言語ヒント + 固有名詞を initial_prompt に渡す。
                # Whisper の prompt は末尾約224トークンのみ有効なため、
                # 長すぎる場合は末尾側（固有名詞側）を優先して丸める。
                initial_prompt = self._build_api_prompt() or None
                if initial_prompt and len(initial_prompt) > 200:
                    initial_prompt = initial_prompt[-200:]
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
                    initial_prompt=initial_prompt,
                )

                for seg in segments:
                    text = (seg.text or "").strip()
                    if not text:
                        continue

                    rows.append({
                        "start": float(seg.start),
                        "end": float(seg.end),
                        "speaker": speaker_label,
                        "source": source,
                        "model": self.model_size,
                        "text": text,
                    })

            for row in rows:
                row.setdefault("source", source)
                row.setdefault("model", self.model_size)
            self._warn_possible_english(rows, speaker_label)
            self.log(f"文字起こし完了: {speaker_label}")
            return rows

        except Exception as e:
            write_error_log(f"Transcriber.transcribe_file error: {speaker_label}", e)
            raise

    def _transcribe_file_api(self, audio_path, speaker_label, diarized_prefix=None):
        api_key = self.resolve_api_key(self.openai_api_key)
        if not api_key:
            raise RuntimeError("APIモデル利用時はOpenAI APIキーを入力してください。")
        return self._transcribe_api_with_chunking(Path(audio_path), speaker_label, diarized_prefix=diarized_prefix)

    def _transcribe_api_with_chunking(self, audio_path, speaker_label, diarized_prefix=None):
        duration = self._probe_duration_seconds(audio_path)
        if duration <= API_CHUNK_SECONDS:
            return self._transcribe_api_single(audio_path, speaker_label, offset_sec=0.0, diarized_prefix=diarized_prefix)

        is_diarize = self.model_size == "gpt-4o-transcribe-diarize"
        chunk_row_lists = []
        start_sec = 0.0
        chunk_index = 1
        while start_sec < duration:
            end_sec = min(duration, start_sec + API_CHUNK_SECONDS)
            clip_start = max(0.0, start_sec - API_OVERLAP_SECONDS if chunk_index > 1 else 0.0)
            clip_end = min(duration, end_sec + API_OVERLAP_SECONDS)
            chunk_rows = self._transcribe_api_chunk_with_retry(
                audio_path=audio_path,
                speaker_label=speaker_label,
                start_sec=clip_start,
                end_sec=clip_end,
                offset_sec=clip_start,
                diarized_prefix=diarized_prefix,
                diarized_chunk_index=chunk_index,
            )
            chunk_row_lists.append(chunk_rows)
            start_sec = end_sec
            chunk_index += 1

        # 話者分離ではチャンクをまたいで同一話者をできるだけ同じラベルに統合する。
        if is_diarize:
            self._unify_diarized_speakers(
                chunk_row_lists,
                diarized_prefix or speaker_label or "話者",
            )

        rows = []
        for chunk_rows in chunk_row_lists:
            rows = self._merge_api_chunk_rows(rows, chunk_rows)
        return rows

    def _unify_diarized_speakers(self, chunk_row_lists, prefix):
        # チャンク分割された diarize 結果で、チャンクをまたいで同一話者を
        # できるだけ同じラベルにまとめる。チャンク境界のオーバーラップ区間で
        # 同じ発話（テキスト一致）が現れることを手がかりにマッチングする。
        # 確実な同定はできないため、マッチしない話者には新しい番号を振る
        # （＝誤って別人を同一化しない安全側の割り当て）。
        global_map = {}
        next_global = [1]
        prev_rows = []

        def assign_new(key):
            gid = next_global[0]
            next_global[0] += 1
            global_map[key] = gid
            return gid

        for chunk_pos, chunk_rows in enumerate(chunk_row_lists):
            first_row_by_raw = {}
            for row in chunk_rows:
                raw = row.get("api_speaker")
                if raw is None:
                    continue
                if raw not in first_row_by_raw:
                    first_row_by_raw[raw] = row

            for raw, first_row in first_row_by_raw.items():
                ci = first_row.get("api_chunk_index", chunk_pos + 1)
                key = (ci, raw)
                if key in global_map:
                    continue
                matched_gid = None
                if chunk_pos > 0:
                    normalized = self._normalized_text(first_row.get("text", ""))
                    start = float(first_row.get("start", 0.0))
                    if normalized:
                        for old in prev_rows:
                            near = abs(float(old.get("start", 0.0)) - start) <= (API_OVERLAP_SECONDS * 2)
                            if near and self._normalized_text(old.get("text", "")) == normalized:
                                matched_gid = old.get("_global_speaker")
                                if matched_gid is not None:
                                    break
                if matched_gid is not None:
                    global_map[key] = matched_gid
                else:
                    assign_new(key)

            for row in chunk_rows:
                raw = row.get("api_speaker")
                if raw is None:
                    continue
                ci = row.get("api_chunk_index", chunk_pos + 1)
                gid = global_map.get((ci, raw))
                if gid is None:
                    gid = assign_new((ci, raw))
                row["_global_speaker"] = gid
                row["speaker"] = f"{prefix}{gid}"

            prev_rows = chunk_rows

    def _probe_duration_seconds(self, audio_path):
        cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nk=1:nw=1", str(audio_path)]
        result = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return float((result.stdout or "0").strip() or 0.0)

    def _transcribe_api_chunk_with_retry(
        self,
        audio_path,
        speaker_label,
        start_sec,
        end_sec,
        offset_sec,
        diarized_prefix=None,
        diarized_chunk_index=None,
    ):
        span = max(0.1, end_sec - start_sec)
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".m4a") as temp_file:
                temp_path = Path(temp_file.name)
            cmd = [
                "ffmpeg",
                "-y",
                "-ss",
                str(start_sec),
                "-t",
                str(span),
                "-i",
                str(audio_path),
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "aac",
                "-b:a",
                API_CHUNK_BITRATE,
                str(temp_path),
            ]
            subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            try:
                return self._transcribe_api_single(
                    temp_path,
                    speaker_label,
                    offset_sec=offset_sec,
                    diarized_prefix=diarized_prefix,
                    diarized_chunk_index=diarized_chunk_index,
                )
            except Exception as e:
                message = str(e).lower()
                if "input too large" in message and span > 250:
                    mid = start_sec + (span / 2.0)
                    left = self._transcribe_api_chunk_with_retry(
                        audio_path,
                        speaker_label,
                        start_sec,
                        mid,
                        start_sec,
                        diarized_prefix=diarized_prefix,
                        diarized_chunk_index=diarized_chunk_index,
                    )
                    right = self._transcribe_api_chunk_with_retry(
                        audio_path,
                        speaker_label,
                        mid,
                        end_sec,
                        mid,
                        diarized_prefix=diarized_prefix,
                        diarized_chunk_index=diarized_chunk_index,
                    )
                    return left + right
                raise
        finally:
            if 'temp_path' in locals() and temp_path.exists():
                temp_path.unlink()

    def _build_api_prompt(self):
        # 言語ヒント（日本語）+ 固有名詞を結合したプロンプトを作る。
        # 英語混入・言語の取り違え・固有名詞の誤変換を減らすヒントになる。
        parts = [API_JAPANESE_PROMPT_HINT]
        vocab = (getattr(self, "vocab_prompt", "") or "").strip()
        if vocab:
            parts.append(vocab)
        return " ".join(p for p in parts if p).strip()

    def _call_openai_transcription(self, audio_path):
        # 公式 openai SDK 経由で文字起こしを実行する。
        # SDK が 429/5xx を Retry-After 付き指数バックオフで自動再試行するため、
        # 自前のHTTP実装・リトライループは不要になった。
        api_key = self.resolve_api_key(self.openai_api_key)
        if not api_key:
            raise RuntimeError("APIモデル利用時はOpenAI APIキーを入力してください。")
        try:
            from openai import OpenAI
            import openai as openai_mod
        except Exception as e:
            raise RuntimeError(
                "OpenAI APIモデルを使うには openai パッケージが必要です。"
                "`pip install openai` を実行してください。"
            ) from e

        client = OpenAI(api_key=api_key, max_retries=5, timeout=600.0)
        kwargs = {"model": self.model_size, "language": LANGUAGE}
        prompt = self._build_api_prompt()
        if prompt:
            kwargs["prompt"] = prompt
        if self.model_size == "gpt-4o-transcribe-diarize":
            kwargs["response_format"] = "diarized_json"
            kwargs["chunking_strategy"] = "auto"
        else:
            kwargs["response_format"] = "json"
            # 出力のブレを抑えるため温度を 0 にする（diarize は temperature 非対応）。
            kwargs["temperature"] = 0

        try:
            with open(audio_path, "rb") as fh:
                result = client.audio.transcriptions.create(file=fh, **kwargs)
        except openai_mod.BadRequestError as e:
            # 「input too large」等はチャンク分割側が文字列で検知するため保持する。
            raise RuntimeError(str(e)) from e
        except openai_mod.APIStatusError as e:
            raise RuntimeError(f"OpenAI APIエラー (HTTP {e.status_code}): {e}") from e

        if hasattr(result, "model_dump"):
            return result.model_dump()
        if isinstance(result, dict):
            return result
        return {"text": str(result)}

    def _transcribe_api_single(
        self,
        audio_path,
        speaker_label,
        offset_sec=0.0,
        diarized_prefix=None,
        diarized_chunk_index=None,
    ):
        payload = self._call_openai_transcription(audio_path)

        rows = []
        diarized_speakers = {}
        segments = payload.get("segments", []) or []
        for seg in segments:
            text = (seg.get("text", "") or "").strip()
            if not text:
                continue
            segment_speaker = speaker_label
            if self.model_size == "gpt-4o-transcribe-diarize":
                raw_speaker = str(seg.get("speaker", "") or "speaker").strip()
                if raw_speaker not in diarized_speakers:
                    diarized_speakers[raw_speaker] = len(diarized_speakers) + 1
                prefix = diarized_prefix or speaker_label or "話者"
                speaker_index = diarized_speakers[raw_speaker]
                # チャンク分割時も暫定的に {prefix}{n} を付け、後段の
                # _unify_diarized_speakers でチャンクをまたいだ話者ラベルを統合する。
                segment_speaker = f"{prefix}{speaker_index}"
            row = {
                "start": float(seg.get("start", 0.0)) + offset_sec,
                "end": float(seg.get("end", 0.0)) + offset_sec,
                "speaker": segment_speaker,
                "api_speaker": seg.get("speaker"),
                "text": text,
            }
            if diarized_chunk_index is not None:
                row["api_chunk_index"] = diarized_chunk_index
            rows.append(row)
        if not rows and self.model_size != "gpt-4o-transcribe-diarize":
            text = (payload.get("text", "") or "").strip()
            if text:
                rows.append({
                    "start": float(offset_sec),
                    "end": float(offset_sec),
                    "speaker": speaker_label,
                    "text": text,
                })
        return rows

    @staticmethod
    def _normalized_text(text):
        return re.sub(r"\s+", "", (text or "")).strip().lower()

    @staticmethod
    def _text_similarity(a, b):
        if not a or not b:
            return 0.0
        return difflib.SequenceMatcher(None, a, b).ratio()

    @staticmethod
    def _looks_like_english(text):
        # 日本語文字を1つも含まず、ラテン文字だけがある程度続く行を
        # 「英語が混入した可能性が高い」とみなす（保守的判定）。
        t = (text or "").strip()
        if len(t) < 8:
            return False
        latin = sum(1 for ch in t if "a" <= ch.lower() <= "z")
        jp = sum(1 for ch in t if ("\u3040" <= ch <= "\u30ff") or ("\u4e00" <= ch <= "\u9fff"))
        return jp == 0 and latin >= 8

    def _warn_possible_english(self, rows, speaker_label):
        # 自動修正はせず、後で見直せるようにログへ警告を出すだけ。
        for r in rows:
            if self._looks_like_english(r.get("text", "")):
                ts = self.fmt(r.get("start", 0.0))
                snippet = (r.get("text", "") or "")[:40]
                self.log(f"⚠ 英語が混入している可能性 [{ts}] {speaker_label}: {snippet}")

    def _merge_api_chunk_rows(self, existing_rows, chunk_rows):
        if not existing_rows or not chunk_rows:
            return existing_rows + chunk_rows

        merged = list(existing_rows)
        prior_tail = existing_rows[-6:]
        for row in chunk_rows:
            normalized = self._normalized_text(row.get("text", ""))
            duplicate = False
            if normalized:
                r_start = float(row.get("start", 0.0))
                r_end = float(row.get("end", r_start))
                for old in prior_tail:
                    o_start = float(old.get("start", 0.0))
                    o_end = float(old.get("end", o_start))
                    # 時間帯が近接/重複している境界付近のみ重複候補とみなす。
                    near = (r_start <= o_end + API_OVERLAP_SECONDS) and (o_start <= r_end + API_OVERLAP_SECONDS)
                    if not near:
                        continue
                    old_norm = self._normalized_text(old.get("text", ""))
                    # 完全一致だけでなく、高い類似度や包含関係も重複とみなす。
                    # APIは同じ区間でも毎回わずかに違う書き起こしを返すため、
                    # 完全一致のみだと重複が二重に残りやすい。
                    if old_norm and (
                        normalized == old_norm
                        or self._text_similarity(normalized, old_norm) >= 0.82
                        or (len(normalized) >= 6 and (normalized in old_norm or old_norm in normalized))
                    ):
                        duplicate = True
                        break
            if not duplicate:
                merged.append(row)
        return merged

    @staticmethod
    def fmt(sec):
        sec = max(0, int(sec))
        h = sec // 3600
        m = (sec % 3600) // 60
        s = sec % 60
        return f"{h:02d}:{m:02d}:{s:02d}"

    def export_transcript(self, rows, txt_path, memo_text="", metadata=None):
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
                    mark_icon = "🟦" if r["speaker"] == "自分" else "🟧"
                    mark = f"{mark_icon} {r['speaker']}"

                    f.write(f"[{start} - {end}] {mark}: {r['text']}\n")

            self.export_transcript_json(rows, txt_path, memo_text=memo_text, metadata=metadata)
            self.log(f"文字起こしtxt保存完了: {txt_path}")

        except Exception as e:
            write_error_log("Transcriber.export_transcript error", e)
            raise

    def export_transcript_json(self, rows, txt_path, memo_text="", metadata=None):
        json_path = Path(txt_path).with_suffix(".json")
        payload = {
            "memo": (memo_text or "").strip(),
            "metadata": metadata or {},
            "segments": rows,
        }
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# =========================
# UI
# =========================
ASSET_DIR = Path(__file__).resolve().parent


def _hex_to_rgb(value):
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def _make_gradient_image(width, height, color_left, color_right, radius, bg_color):
    width = max(1, int(width))
    height = max(1, int(height))
    c1 = _hex_to_rgb(color_left)
    c2 = _hex_to_rgb(color_right)
    row = Image.new("RGB", (width, 1))
    for x in range(width):
        t = x / max(1, width - 1)
        row.putpixel((x, 0), (
            int(c1[0] + (c2[0] - c1[0]) * t),
            int(c1[1] + (c2[1] - c1[1]) * t),
            int(c1[2] + (c2[2] - c1[2]) * t),
        ))
    grad = row.resize((width, height))
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, width - 1, height - 1), radius=radius, fill=255)
    out = Image.new("RGB", (width, height), _hex_to_rgb(bg_color))
    out.paste(grad, (0, 0), mask)
    return out


class GradientButton(tk.Canvas):
    """PIL でグラデーションを描いた角丸ボタン（CustomTkinter はグラデ非対応のため自作）。"""

    def __init__(self, parent, text="", command=None, width=200, height=48,
                 colors=("#3a5bf0", "#7b45f5"), hover_colors=("#4f6dff", "#9160ff"),
                 disabled_colors=("#2a2d40", "#2a2d40"), radius=16,
                 font=("Yu Gothic UI", 15, "bold"), text_color="#ffffff",
                 disabled_text="#6b7086", bg="#0b0c14"):
        super().__init__(parent, width=width, height=height, highlightthickness=0, bd=0, bg=bg)
        self._text = text
        self._command = command
        self._colors = colors
        self._hover = hover_colors
        self._disabled = disabled_colors
        self._radius = radius
        self._font = font
        self._text_color = text_color
        self._disabled_text = disabled_text
        self._state = "normal"
        self._hovering = False
        self._photo = None
        self.bind("<Configure>", lambda _e: self._render())
        self.bind("<Button-1>", self._on_click)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)

    def _colors_now(self):
        if self._state == "disabled":
            return self._disabled
        return self._hover if self._hovering else self._colors

    def _render(self):
        w = self.winfo_width()
        h = self.winfo_height()
        if w <= 1 or h <= 1:
            return
        c1, c2 = self._colors_now()
        img = _make_gradient_image(w, h, c1, c2, self._radius, self["bg"])
        self._photo = ImageTk.PhotoImage(img)
        self.delete("all")
        self.create_image(0, 0, anchor="nw", image=self._photo)
        fill = self._text_color if self._state == "normal" else self._disabled_text
        self.create_text(w // 2, h // 2, text=self._text, fill=fill, font=self._font)

    def _on_click(self, _e):
        if self._state == "normal" and self._command:
            self._command()

    def _on_enter(self, _e):
        if self._state == "normal":
            self._hovering = True
            super().configure(cursor="hand2")
            self._render()

    def _on_leave(self, _e):
        self._hovering = False
        self._render()

    def configure(self, **kwargs):
        rerender = False
        if "state" in kwargs:
            self._state = kwargs.pop("state")
            rerender = True
        if "text" in kwargs:
            self._text = kwargs.pop("text")
            rerender = True
        if "command" in kwargs:
            self._command = kwargs.pop("command")
        if kwargs:
            super().configure(**kwargs)
        if rerender:
            self._render()

    config = configure


class App:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("470x600")
        self.root.minsize(440, 560)
        try:
            _ico = ASSET_DIR / "icon.ico"
            if _ico.exists():
                self.root.iconbitmap(default=str(_ico))
        except Exception:
            pass

        # 配色（モックアップに合わせたダークネイビー + ブルー〜パープル）
        self.bg_color = "#0b0c14"
        self.bg_hex = "#0b0c14"
        self.card_color = "#141621"
        self.border_color = "#272b3d"
        self.accent_color = "#5566f5"
        self.accent_hover = "#4353e8"
        self.accent_soft = "#20233a"
        self.text_color = "#eef0f8"
        self.muted_color = "#8b90ab"
        self.purple = "#8b5cf6"
        self.diamond = "#6b7bff"
        self.grad_left = "#3a5bf0"
        self.grad_right = "#7b45f5"
        self.grad_left_h = "#4f6dff"
        self.grad_right_h = "#9160ff"

        self.font_body = ("Yu Gothic UI", 13)
        self.font_small = ("Yu Gothic UI", 12)
        self.font_section = ("Yu Gothic UI", 15, "bold")
        self.font_title = ("Yu Gothic UI", 22, "bold")
        self.font_status = ("Yu Gothic UI", 26, "bold")

        self.logo_image = None
        try:
            _logo = ASSET_DIR / "logo.png"
            if _logo.exists():
                _img = Image.open(_logo)
                self.logo_image = ctk.CTkImage(light_image=_img, dark_image=_img, size=(58, 58))
        except Exception:
            self.logo_image = None

        self.root.configure(fg_color=self.bg_color)

        self.main_thread_id = threading.get_ident()

        self.recorder = AudioRecorder(self.add_log)
        self.transcriber = Transcriber(self.add_log)
        self.app_settings = DEFAULT_SETTINGS.copy()
        self.transcriber.set_settings(self.app_settings)
        self.recorder.set_noise_reduction(self.app_settings.get("noise_reduction", False))

        self.system_devices = []
        self.mic_devices = []
        self._system_values = []
        self._mic_values = []

        self.status_var = tk.StringVar(value="起動中")
        self.timer_var = tk.StringVar(value="録音時間: 00:00")
        self.status_title_var = tk.StringVar(value="停止中")
        self.status_detail_var = tk.StringVar(value="録音は開始されていません")
        self.current_transcription_var = tk.StringVar(value="文字起こし: 待機中")
        self.model_var = tk.StringVar(value=MODEL_CHOICES[self.app_settings["model_size"]])
        self.mode_var = tk.StringVar(value=self.app_settings["mode"])
        self.api_key_var = tk.StringVar(value=self.app_settings["openai_api_key"])
        self.vocab_prompt_var = tk.StringVar(value=self.app_settings.get("vocab_prompt", ""))
        self.noise_reduction_var = tk.BooleanVar(value=self.app_settings.get("noise_reduction", False))
        self.system_muted_var = tk.BooleanVar(value=False)
        self.system_device_var = tk.StringVar()
        self.mic_device_var = tk.StringVar()
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

    def _mode_color(self, light, dark):
        return dark if ctk.get_appearance_mode() == "Dark" else light

    def _option_index(self, value, values):
        try:
            return list(values).index(value)
        except (ValueError, TypeError):
            return -1

    def _card(self, parent):
        return ctk.CTkFrame(
            parent, fg_color=self.card_color, corner_radius=16,
            border_width=1, border_color=self.border_color,
        )

    def _gradient_button(self, parent, text, command, height=48, width=200,
                         font=("Yu Gothic UI", 15, "bold"), radius=16):
        return GradientButton(
            parent, text=text, command=command, height=height, width=width,
            font=font, radius=radius, bg=self.bg_hex,
            colors=(self.grad_left, self.grad_right),
            hover_colors=(self.grad_left_h, self.grad_right_h),
        )

    def _ghost_button(self, parent, text, command, border=None, text_color=None, **kwargs):
        return ctk.CTkButton(
            parent, text=text, command=command, fg_color="transparent",
            text_color=text_color or self.accent_color, hover_color=self.accent_soft,
            border_width=1, border_color=border or self.accent_color,
            corner_radius=10, **kwargs
        )

    def _option_menu(self, parent, variable, values, command):
        return ctk.CTkOptionMenu(
            parent, variable=variable, values=values, command=command,
            fg_color="#1b1e2e", button_color=self.accent_color,
            button_hover_color=self.accent_hover, text_color=self.text_color,
            dropdown_fg_color=self.card_color, dropdown_hover_color=self.accent_color,
            dropdown_text_color=self.text_color, corner_radius=10, height=40,
            font=self.font_small,
        )

    def _section_header(self, parent, text):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        ctk.CTkLabel(row, text="◆", font=("Yu Gothic UI", 12),
                     text_color=self.diamond).pack(side="left")
        ctk.CTkLabel(row, text=text, font=self.font_section,
                     text_color=self.text_color).pack(side="left", padx=(8, 0))
        return row

    def _mute_toggle_button(self, parent):
        return ctk.CTkButton(
            parent, text="🔊", width=30, height=26, corner_radius=8,
            fg_color="transparent", hover_color=self.accent_soft,
            border_width=1, border_color=self.border_color,
            text_color=self.text_color, font=self.font_small,
            command=self.toggle_system_mute,
        )

    def toggle_system_mute(self):
        muted = not self.system_muted_var.get()
        self.system_muted_var.set(muted)
        self.recorder.set_system_muted(muted)
        icon = "🔇" if muted else "🔊"
        color = "#fb7185" if muted else self.text_color
        for btn in (getattr(self, "system_mute_btn", None), getattr(self, "system_mute_btn_record", None)):
            if btn is not None:
                btn.configure(text=icon, text_color=color)
        self.add_log(
            "スピーカー（相手音声）の録音をミュートにしました。" if muted
            else "スピーカー（相手音声）の録音のミュートを解除しました。"
        )

    def on_noise_reduction_changed(self):
        enabled = self.noise_reduction_var.get()
        self.app_settings["noise_reduction"] = enabled
        self.recorder.set_noise_reduction(enabled)
        if enabled and not NOISEREDUCE_AVAILABLE:
            self.add_log(
                "ノイズ除去を有効にしましたが、noisereduce が未インストールのため動作しません"
                "（pip install noisereduce numpy）。"
            )
        else:
            self.add_log("ノイズ除去を有効にしました。" if enabled else "ノイズ除去を無効にしました。")

    def build_ui(self):
        self.tabview = ctk.CTkTabview(
            self.root,
            fg_color=self.bg_color,
            segmented_button_fg_color=self.card_color,
            segmented_button_selected_color=self.accent_color,
            segmented_button_selected_hover_color=self.accent_hover,
            segmented_button_unselected_color=self.card_color,
            text_color=self.text_color,
            corner_radius=12,
        )
        self.tabview.pack(fill="both", expand=True, padx=14, pady=(6, 14))

        home_tab = self.tabview.add("ホーム")
        analysis_tab = self.tabview.add("分析")
        settings_tab = self.tabview.add("設定")

        # ---- ホーム ----
        self.status_banner = ctk.CTkFrame(
            home_tab, fg_color=self.card_color, corner_radius=16,
            border_width=1, border_color=self.border_color,
        )
        self.status_banner.pack(fill="x", pady=(4, 12))
        badge = ctk.CTkFrame(self.status_banner, fg_color=self.accent_soft,
                             corner_radius=18, width=88, height=88)
        badge.pack(side="left", padx=16, pady=16)
        badge.pack_propagate(False)
        if self.logo_image is not None:
            ctk.CTkLabel(badge, text="", image=self.logo_image).pack(expand=True)
        else:
            ctk.CTkLabel(badge, text="◆", font=("Yu Gothic UI", 36),
                         text_color=self.accent_color).pack(expand=True)
        text_col = ctk.CTkFrame(self.status_banner, fg_color="transparent")
        text_col.pack(side="left", fill="both", expand=True, padx=(4, 16), pady=16)
        self.status_title_label = ctk.CTkLabel(
            text_col, textvariable=self.status_title_var, font=self.font_status,
            text_color=self.text_color, anchor="w")
        self.status_title_label.pack(fill="x")
        self.status_detail_label = ctk.CTkLabel(
            text_col, textvariable=self.status_detail_var, font=self.font_body,
            text_color=self.muted_color, anchor="w")
        self.status_detail_label.pack(fill="x", pady=(2, 0))
        self.current_transcription_label = ctk.CTkLabel(
            text_col, textvariable=self.current_transcription_var, font=self.font_small,
            text_color=self.accent_color, anchor="w")
        self.current_transcription_label.pack(fill="x", pady=(8, 0))
        self.refresh_status_banner()

        device_card = self._card(home_tab)
        device_card.pack(fill="x", pady=(0, 12))
        self._section_header(device_card, "録音デバイス選択").pack(fill="x", padx=16, pady=(14, 10))
        system_label_row = ctk.CTkFrame(device_card, fg_color="transparent")
        system_label_row.pack(fill="x", padx=16)
        ctk.CTkLabel(system_label_row, text="スピーカー / 相手音声", font=self.font_small,
                     text_color=self.muted_color, anchor="w").pack(side="left")
        self.system_mute_btn = self._mute_toggle_button(system_label_row)
        self.system_mute_btn.pack(side="right")
        self.system_combo = self._option_menu(device_card, self.system_device_var,
                                               ["(読み込み中)"], self.on_device_changed)
        self.system_combo.pack(fill="x", padx=16, pady=(4, 12))
        ctk.CTkLabel(device_card, text="マイク / 自分の声", font=self.font_small,
                     text_color=self.muted_color, anchor="w").pack(fill="x", padx=16)
        self.mic_combo = self._option_menu(device_card, self.mic_device_var,
                                            ["(読み込み中)"], self.on_device_changed)
        self.mic_combo.pack(fill="x", padx=16, pady=(4, 12))
        self.reload_btn = self._ghost_button(device_card, "⟳  デバイス再読み込み",
                                             self.load_devices, font=self.font_small, height=34)
        self.reload_btn.pack(anchor="e", padx=16, pady=(2, 14))

        self.start_btn = self._gradient_button(home_tab, "●  録音開始", self.start_recording,
                                               height=52, font=("Yu Gothic UI", 16, "bold"))
        self.start_btn.pack(fill="x", pady=(2, 4))
        self.start_btn.configure(state="disabled")

        self.build_recording_view()

        # ---- 分析 ----
        analysis_top = ctk.CTkFrame(analysis_tab, fg_color="transparent")
        analysis_top.pack(fill="x", pady=(8, 12))
        self.start_transcribe_btn = self._gradient_button(
            analysis_top, "✐  文字起こし開始", self.start_transcription_queue,
            height=44, width=220, font=self.font_section)
        self.start_transcribe_btn.pack(side="left")
        self.open_folder_btn = self._ghost_button(analysis_top, "🗀  録音フォルダを開く",
                                                  self.open_output_folder, font=self.font_small, height=34)
        self.open_folder_btn.pack(side="left", padx=(10, 0))

        count_row = ctk.CTkFrame(analysis_tab, fg_color="transparent")
        count_row.pack(fill="x", padx=4, pady=(0, 8))
        ctk.CTkLabel(count_row, text="◆", font=("Yu Gothic UI", 12),
                     text_color=self.diamond).pack(side="left")
        self.queue_count_label = ctk.CTkLabel(count_row, text="追加済みファイル（0 件）",
                                              font=self.font_section, text_color=self.text_color)
        self.queue_count_label.pack(side="left", padx=(8, 0))

        list_card = self._card(analysis_tab)
        list_card.pack(fill="both", expand=True, pady=(0, 10))
        self.queue_placeholder = ctk.CTkFrame(list_card, fg_color="transparent")
        ctk.CTkLabel(self.queue_placeholder, text="🗀", font=("Yu Gothic UI", 46),
                     text_color=self.muted_color).pack(pady=(46, 10))
        ctk.CTkLabel(self.queue_placeholder, text="追加されたファイルはここに表示されます",
                     font=self.font_body, text_color=self.text_color).pack()
        ctk.CTkLabel(self.queue_placeholder, text="フォルダや動画を追加して、文字起こしを開始しましょう",
                     font=self.font_small, text_color=self.muted_color).pack(pady=(6, 46))
        self.queue_listbox = tk.Listbox(
            list_card, height=8, relief="flat", borderwidth=0, highlightthickness=0,
            bg=self.card_color, fg=self.text_color,
            selectbackground=self.accent_color, selectforeground="#ffffff",
            font=("Yu Gothic UI", 12), activestyle="none",
        )
        self.queue_placeholder.pack(fill="both", expand=True, padx=12, pady=12)

        queue_btn_frame = ctk.CTkFrame(analysis_tab, fg_color="transparent")
        queue_btn_frame.pack(fill="x", pady=(0, 12))
        self._ghost_button(queue_btn_frame, "＋ フォルダ追加", self.add_queue_from_dialog,
                           font=self.font_small, height=36, width=118).pack(side="left")
        self._ghost_button(queue_btn_frame, "▶ 動画追加", self.add_video_queue_from_dialog,
                           font=self.font_small, height=36, width=108).pack(side="left", padx=(8, 0))
        self._ghost_button(queue_btn_frame, "🗑 選択削除", self.remove_selected_queue,
                           border=self.purple, text_color=self.purple,
                           font=self.font_small, height=36, width=108).pack(side="left", padx=(8, 0))

        # ---- 設定 ----
        settings_top = ctk.CTkFrame(settings_tab, fg_color="transparent")
        settings_top.pack(fill="x", pady=(8, 12))
        self.open_error_btn = self._ghost_button(settings_top, "▤  エラーログを開く",
                                                 self.open_error_log, font=self.font_small, height=34)
        self.open_error_btn.pack(side="left")
        settings_right = ctk.CTkFrame(settings_top, fg_color="transparent")
        settings_right.pack(side="right")
        ctk.CTkLabel(settings_right, text=f"v{APP_VERSION}", font=self.font_small,
                     text_color=self.muted_color).pack(anchor="e")
        self._ghost_button(settings_right, "ⓘ アプリ説明", self.show_app_description,
                           border=self.border_color, text_color=self.muted_color,
                           font=self.font_small, width=110, height=30).pack(anchor="e", pady=(6, 0))

        settings_card = self._card(settings_tab)
        settings_card.pack(fill="x", pady=(0, 12))
        self._section_header(settings_card, "文字起こし設定").pack(fill="x", padx=16, pady=(14, 6))
        settings_frame = ctk.CTkFrame(settings_card, fg_color="transparent")
        settings_frame.pack(fill="x", padx=16, pady=(0, 14))
        ctk.CTkLabel(settings_frame, text="モデル", font=self.font_body,
                     text_color=self.text_color).grid(row=0, column=0, sticky="w", pady=6)
        self.model_combo = self._option_menu(settings_frame, self.model_var,
                                              list(MODEL_CHOICES.values()), self.on_transcription_setting_changed)
        self.model_combo.grid(row=0, column=1, sticky="ew", padx=(12, 0), pady=6)
        ctk.CTkLabel(settings_frame, text="処理モード", font=self.font_body,
                     text_color=self.text_color).grid(row=1, column=0, sticky="w", pady=6)
        self.mode_combo = self._option_menu(settings_frame, self.mode_var,
                                             list(MODE_CHOICES.keys()), self.on_transcription_setting_changed)
        self.mode_combo.grid(row=1, column=1, sticky="ew", padx=(12, 0), pady=6)
        ctk.CTkLabel(settings_frame, text="OpenAI APIキー", font=self.font_body,
                     text_color=self.text_color).grid(row=2, column=0, sticky="w", pady=6)
        self.api_key_entry = ctk.CTkEntry(settings_frame, textvariable=self.api_key_var,
                                           show="*", font=self.font_small, height=38,
                                           fg_color="#1b1e2e", border_color=self.border_color)
        self.api_key_entry.grid(row=2, column=1, sticky="ew", padx=(12, 0), pady=6)
        self.api_key_entry.bind("<FocusOut>", self.on_transcription_setting_changed)
        self.api_key_note_var = tk.StringVar()
        self.api_key_note_label = ctk.CTkLabel(settings_frame, textvariable=self.api_key_note_var,
                                               font=self.font_small, text_color=self.muted_color,
                                               wraplength=360, justify="left", anchor="w")
        self.api_key_note_label.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        ctk.CTkLabel(settings_frame, text="固有名詞・専門用語", font=self.font_body,
                     text_color=self.text_color).grid(row=4, column=0, sticky="nw", pady=6)
        self.vocab_prompt_entry = ctk.CTkEntry(settings_frame, textvariable=self.vocab_prompt_var,
                                               font=self.font_small, height=38,
                                               fg_color="#1b1e2e", border_color=self.border_color)
        self.vocab_prompt_entry.grid(row=4, column=1, sticky="ew", padx=(12, 0), pady=6)
        self.vocab_prompt_entry.bind("<FocusOut>", self.on_transcription_setting_changed)
        ctk.CTkLabel(settings_frame,
                     text="会議でよく出る固有名詞・専門用語をカンマ区切りで入力すると、"
                          "文字起こしの誤変換や言語の取り違えを減らせます"
                          "（例: 伴走型DX相談, IPA, 西端, ハピネス社）。",
                     font=self.font_small, text_color=self.muted_color, wraplength=360,
                     justify="left", anchor="w").grid(row=5, column=0, columnspan=2, sticky="ew", pady=(2, 4))
        ctk.CTkLabel(settings_frame, text="ノイズ除去", font=self.font_body,
                     text_color=self.text_color).grid(row=6, column=0, sticky="w", pady=6)
        self.noise_reduction_switch = ctk.CTkSwitch(
            settings_frame, text="", variable=self.noise_reduction_var,
            command=self.on_noise_reduction_changed,
            fg_color=self.border_color, progress_color=self.accent_color,
        )
        self.noise_reduction_switch.grid(row=6, column=1, sticky="w", padx=(12, 0), pady=6)
        ctk.CTkLabel(settings_frame,
                     text="録音停止時に背景ノイズを軽減します（5%未満の小さな音は常にカットされます）。"
                          "録音が長いと停止処理に数秒〜数十秒余分にかかる場合があります。",
                     font=self.font_small, text_color=self.muted_color, wraplength=360,
                     justify="left", anchor="w").grid(row=7, column=0, columnspan=2, sticky="ew", pady=(2, 4))
        settings_frame.columnconfigure(1, weight=1)
        self.refresh_api_key_entry_state()
        self.refresh_settings_summary()

        self._section_header(settings_tab, "ログ").pack(anchor="w", padx=4, pady=(0, 6))
        log_card = self._card(settings_tab)
        log_card.pack(fill="both", expand=True)
        self.log_text = ctk.CTkTextbox(log_card, wrap="word", font=("Yu Gothic UI", 12),
                                       fg_color="transparent", text_color=self.text_color)
        self.log_text.pack(fill="both", expand=True, padx=12, pady=12)

        self.add_log("アプリを起動しました。")
        self.add_log(f"録音フォルダ: {BASE_DIR}")
        self.add_log(f"エラーログ: {ERROR_LOG}")
        self.add_log(
            f"文字起こし設定: model={self.app_settings['model_size']}, "
            f"mode={self.app_settings['mode']}, beam_size={self.app_settings['beam_size']}, "
            f"compute_type={self.app_settings['compute_type']}"
        )
        if not PYCAW_AVAILABLE:
            self.add_log("pycawが利用できないため、OSマイクミュートとの同期は無効です（pip install pycaw comtypes）。")
        if not NOISEREDUCE_AVAILABLE:
            self.add_log("noisereduceが利用できないため、ノイズ除去機能は無効です（pip install noisereduce numpy）。")

    def build_recording_view(self):
        self.recording_frame = ctk.CTkFrame(self.root, fg_color=self.bg_color)

        self.recording_stop_btn = self._gradient_button(self.recording_frame, "■  停止",
                                                        self.stop_recording,
                                                        font=("Yu Gothic UI", 16, "bold"), height=52)
        self.recording_stop_btn.pack(fill="x", pady=(0, 12))

        levels_frame = ctk.CTkFrame(self.recording_frame, fg_color="transparent")
        levels_frame.pack(fill="x", pady=(0, 12))

        system_card = self._card(levels_frame)
        system_card.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        sys_header = ctk.CTkFrame(system_card, fg_color="transparent")
        sys_header.pack(fill="x", padx=12, pady=(10, 2))
        ctk.CTkLabel(sys_header, text="相手音声", font=self.font_section,
                     text_color=self.text_color, anchor="w").pack(side="left")
        self.system_mute_btn_record = self._mute_toggle_button(sys_header)
        self.system_mute_btn_record.pack(side="right")
        self.recording_system_device_label = ctk.CTkLabel(
            system_card, textvariable=self.recording_system_device_var,
            font=self.font_small, text_color=self.muted_color, wraplength=180,
            justify="left", anchor="w",
        )
        self.recording_system_device_label.pack(fill="x", padx=12, pady=(0, 6))
        sys_meter = ctk.CTkFrame(system_card, fg_color="transparent")
        sys_meter.pack(fill="x", padx=12, pady=(0, 12))
        self.recording_system_bar = ctk.CTkProgressBar(sys_meter, progress_color=self.accent_color, height=14)
        self.recording_system_bar.set(0)
        self.recording_system_bar.pack(side="left", fill="x", expand=True, padx=(0, 6))
        ctk.CTkLabel(sys_meter, textvariable=self.recording_system_percent_var, font=self.font_small,
                     text_color=self.text_color, width=44, anchor="e").pack(side="right")

        mic_card = self._card(levels_frame)
        mic_card.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        ctk.CTkLabel(mic_card, text="マイク", font=self.font_section,
                     text_color=self.text_color, anchor="w").pack(fill="x", padx=12, pady=(10, 2))
        self.recording_mic_device_label = ctk.CTkLabel(
            mic_card, textvariable=self.recording_mic_device_var,
            font=self.font_small, text_color=self.muted_color, wraplength=180,
            justify="left", anchor="w",
        )
        self.recording_mic_device_label.pack(fill="x", padx=12, pady=(0, 6))
        self.mic_os_mute_label = ctk.CTkLabel(
            mic_card, text="", font=self.font_small, text_color="#fb7185",
            wraplength=180, justify="left", anchor="w",
        )
        self.mic_os_mute_label.pack(fill="x", padx=12, pady=(0, 4))
        mic_meter = ctk.CTkFrame(mic_card, fg_color="transparent")
        mic_meter.pack(fill="x", padx=12, pady=(0, 12))
        self.recording_mic_bar = ctk.CTkProgressBar(mic_meter, progress_color=self.accent_color, height=14)
        self.recording_mic_bar.set(0)
        self.recording_mic_bar.pack(side="left", fill="x", expand=True, padx=(0, 6))
        ctk.CTkLabel(mic_meter, textvariable=self.recording_mic_percent_var, font=self.font_small,
                     text_color=self.text_color, width=44, anchor="e").pack(side="right")

        levels_frame.columnconfigure(0, weight=1)
        levels_frame.columnconfigure(1, weight=1)

        memo_card = self._card(self.recording_frame)
        memo_card.pack(fill="both", expand=True)
        self.memo_text = ctk.CTkTextbox(memo_card, wrap="word", font=("Yu Gothic UI", 13),
                                        fg_color="transparent", text_color=self.text_color)
        self.memo_text.pack(fill="both", expand=True, padx=12, pady=12)

    def show_recording_view(self):
        self.tabview.pack_forget()
        self.recording_frame.pack(fill="both", expand=True, padx=14, pady=(6, 14))

    def show_main_view(self):
        self.recording_frame.pack_forget()
        self.tabview.pack(fill="both", expand=True, padx=14, pady=(6, 14))

    def update_recording_device_labels(self):
        system_name = self.system_device_var.get() or "相手音声"
        mic_name = self.mic_device_var.get() or "マイク"
        self.recording_system_device_var.set(system_name)
        self.recording_mic_device_var.set(mic_name)
        self.update_recording_level_values(self.recorder.system_level, self.recorder.mic_level)

    def update_recording_level_values(self, system_level, mic_level):
        self.recording_system_percent_var.set(f"{system_level}%")
        self.recording_mic_percent_var.set(f"{mic_level}%")
        if hasattr(self, "recording_system_bar"):
            self.recording_system_bar.set(max(0, min(100, system_level)) / 100)
        if hasattr(self, "recording_mic_bar"):
            self.recording_mic_bar.set(max(0, min(100, mic_level)) / 100)
        if hasattr(self, "mic_os_mute_label"):
            muted = bool(self.recorder.is_recording and self.recorder.mic_muted_by_os)
            self.mic_os_mute_label.configure(text="🔇 マイクはOS側でミュート中です" if muted else "")

    def save_recording_memo(self, output_dir):
        memo = self.memo_text.get("1.0", "end").strip()
        if not memo:
            return None

        memo_path = Path(output_dir) / "memo.txt"
        memo_path.write_text(memo, encoding="utf-8")
        self.add_log(f"メモ保存: {memo_path}")
        return memo_path

    def transcript_txt_path(self, output_dir, settings=None, transcription_pattern=None):
        settings = settings or self.app_settings
        output_dir = Path(output_dir)
        model_size = settings.get("model_size", DEFAULT_SETTINGS["model_size"])
        model_code = MODEL_FILENAME_CODES.get(model_size, model_size.replace("-", ""))
        beam_size = int(settings.get("beam_size", DEFAULT_SETTINGS["beam_size"]))
        routes = PATTERN_ROUTES.get(transcription_pattern)
        if routes:
            route_model_codes = [
                MODEL_FILENAME_CODES.get(route["model"], route["model"].replace("-", ""))
                for route in routes
            ]
            model_code = "_".join(route_model_codes)
            return output_dir / f"{output_dir.name}_{transcription_pattern}_{model_code}_{beam_size}.txt"
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
            f"録音フォルダはキュー追加時のパターンでモデルを自動選択します。"
            f"マニュアルパターンと動画文字起こしはここで選ぶ model={model_size} を使います。"
            f"large-v3 の beam_size={beam_size}, compute_type={COMPUTE_TYPE}。"
            "初回利用時はモデルのダウンロードに時間がかかります。"
        )

    def show_app_description(self):
        safe_messagebox_info(
            "アプリ説明",
            "【ホーム画面】\n"
            "録音に使う相手音声デバイスとマイクを選択し、録音を開始する画面です。\n"
            "録音中は相手音声とマイクの音量レベルを確認できます。\n\n"
            "録音中にメモを入力できます。\n"
            "入力したメモは、文字起こし完了後に出力されるテキストファイルへ追加されます。\n"
            "会議中の補足、確認したい点、文字起こしだけでは残しにくい情報を記録したい場合に使えます。\n\n"
            "録音を停止すると録音データが保存され、文字起こしキューへ追加できます。\n\n\n"
            "【分析画面】\n"
            "録音フォルダや動画を文字起こしキューに追加し、文字起こしを実行する画面です。\n"
            "追加した対象は一覧で確認でき、不要な項目は選択して削除できます。\n"
            "録音フォルダを開いて、保存済みの音声や文字起こし結果を確認することもできます。\n\n"
            "動画ファイルも追加できます。\n"
            "動画追加では、動画から音声を取り出して文字起こしを行います。\n\n"
            "フォルダ追加では、録音内容に合う文字起こしパターンを選びます。\n\n"
            "・オンラインMTG: 自分 + 相手1人\n"
            "  自分のマイク音声と相手音声を分けて文字起こしします。\n"
            "  1対1のオンライン会議向けです。\n"
            "  使用モデル\n"
            "    自分: large-v3\n"
            "    相手: gpt-4o-transcribe\n\n"
            "・オンラインMTG: 自分 + 相手複数\n"
            "  自分のマイク音声と相手側の音声を使って文字起こしします。\n"
            "  相手側は複数人の発話を想定した話者分離を使います。\n"
            "  使用モデル\n"
            "    自分: large-v3\n"
            "    相手: gpt-4o-transcribe-diarize\n\n"
            "・オフラインMTG: 自分マイクのみ\n"
            "  マイクで録音した音声だけを使って文字起こしします。\n"
            "  対面会議など、1本のマイク音声に複数人の声が入る場合向けです。\n"
            "  使用モデル\n"
            "    マイク音声: gpt-4o-transcribe-diarize\n\n"
            "・マニュアル\n"
            "  設定画面で選択したモデルを使って文字起こしします。\n\n"
            "上記パターンでは、OpenAI APIモデルを使う経路があります。\n"
            "APIモデルを使う場合は、設定画面でOpenAI APIキーを設定してください。\n\n\n"
            "【設定画面】\n"
            "文字起こしに使うモデル、処理モード、OpenAI APIキーを設定する画面です。\n\n"
            "モデルは、文字起こしに使う方式を選びます。\n"
            "ローカルモデルはPC上で処理します。\n"
            "精度を重視する場合は large-v3 がおすすめです。\n"
            "ただし会社PCはスペックが低いため、large-v3 では録音時間と同程度の処理時間がかかる場合があります。\n\n"
            "・medium\n"
            "  標準的で比較的軽いローカルモデルです。\n\n"
            "・large-v3\n"
            "  精度重視のローカルモデルです。\n\n"
            "・large-v3-turbo\n"
            "  large-v3 より速度を重視したローカルモデルです。\n\n"
            "・gpt-4o-mini-transcribe\n"
            "  OpenAI APIを使う文字起こしモデルです。\n\n"
            "・gpt-4o-transcribe\n"
            "  OpenAI APIを使う高精度な文字起こしモデルです。\n\n"
            "・gpt-4o-transcribe-diarize\n"
            "  OpenAI APIを使い、話者分離を行う文字起こしモデルです。\n\n"
            "処理モードは、ローカル文字起こしの速度と精度のバランスを選びます。\n\n"
            "・高速\n"
            "  処理速度を優先します。\n\n"
            "・標準\n"
            "  速度と精度のバランスを取ります。\n\n"
            "・高精度\n"
            "  精度を優先しますが、処理時間は長くなります。\n\n"
            "録音フォルダの文字起こしでは、追加時に選んだパターンに応じてモデルが自動選択されます。\n"
            "マニュアルパターンや動画文字起こしでは、ここで選んだモデル設定を使います。\n\n"
            "OpenAI APIモデルを使う場合はAPIキーが必要です。\n"
            "OPENAI_API_KEY 環境変数が設定されている場合は、そのキーを優先して使います。",
        )

    def refresh_api_key_entry_state(self):
        env_key = (os.getenv("OPENAI_API_KEY", "") or "").strip()
        if env_key:
            self.api_key_var.set(API_KEY_MASK)
            self.api_key_entry.configure(state="disabled")
            self.api_key_note_var.set("OPENAI_API_KEY環境変数が設定されているため、そのキーを使用します。")
        else:
            if self.api_key_var.get() == API_KEY_MASK:
                self.api_key_var.set("")
            self.api_key_entry.configure(state="normal")
            self.api_key_note_var.set("OPENAI_API_KEY環境変数が未設定のため、ここにAPIキーを入力してください。")

    def on_transcription_setting_changed(self, event=None):
        self.refresh_api_key_entry_state()
        if self.transcription_running:
            self.model_var.set(MODEL_CHOICES[self.app_settings["model_size"]])
            self.mode_var.set(self.app_settings["mode"])
            self.api_key_var.set(self.app_settings.get("openai_api_key", ""))
            self.vocab_prompt_var.set(self.app_settings.get("vocab_prompt", ""))
            self.refresh_settings_summary()
            safe_messagebox_error("エラー", "文字起こし中は設定を変更できません。完了後に変更してください。")
            return

        mode = self.mode_var.get() if self.mode_var.get() in MODE_CHOICES else DEFAULT_SETTINGS["mode"]
        settings = {
            "model_size": self.selected_model_size(),
            "mode": mode,
            "beam_size": MODE_CHOICES[mode],
            "compute_type": COMPUTE_TYPE,
            "openai_api_key": "" if self.api_key_var.get() == API_KEY_MASK else self.api_key_var.get().strip(),
            "vocab_prompt": self.vocab_prompt_var.get().strip(),
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

            self._system_values = system_names
            self._mic_values = mic_names
            self.system_combo.configure(values=system_names or ["loopbackデバイスが見つかりません"])
            self.mic_combo.configure(values=mic_names or ["おすすめマイクが見つかりません"])

            if system_names:
                self.system_device_var.set(system_names[0])
            else:
                self.system_device_var.set("loopbackデバイスが見つかりません")

            if mic_names:
                self.mic_device_var.set(mic_names[0])
            else:
                self.mic_device_var.set("おすすめマイクが見つかりません")

            self.add_log("録音デバイスを読み込みました。")
            self.add_log(f"相手音声候補: {len(self.system_devices)} 件")
            self.add_log(f"おすすめマイク候補: {len(self.mic_devices)} 件")

            if self.system_devices and self.mic_devices:
                self.start_btn.configure(state="normal")
                self.status_var.set("待機中")
                self.schedule_preview_level_meter()
            else:
                self.start_btn.configure(state="disabled")
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
            system_pos = self._option_index(self.system_device_var.get(), self._system_values)
            mic_pos = self._option_index(self.mic_device_var.get(), self._mic_values)

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

            self.start_btn.configure(state="disabled")
            self.recording_stop_btn.configure(state="normal")
            self.reload_btn.configure(state="disabled")
            self.system_combo.configure(state="disabled")
            self.mic_combo.configure(state="disabled")

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
            self.recording_stop_btn.configure(state="disabled")

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

            queued = self.enqueue_transcription_job(result)
            if queued:
                self.add_log("録音停止後に文字起こしキューへ追加しました。文字起こし開始ボタンを押してください。")
            else:
                self.add_log("録音は保存しました。文字起こしキューには追加していません。")
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
            if result.get("video_path"):
                self.transcriber.set_settings(self.app_settings)
                self.add_log(f"動画から音声抽出: {result['video_path']}")
                with tempfile.NamedTemporaryFile(delete=False, suffix=".m4a") as tmp_audio:
                    temp_audio_path = Path(tmp_audio.name)
                try:
                    cmd = [
                        "ffmpeg", "-y", "-i", str(result["video_path"]), "-vn", "-ac", "1", "-ar", "16000", str(temp_audio_path)
                    ]
                    subprocess.run(
                        cmd,
                        check=True,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                    )
                    all_rows = self.transcriber.transcribe_file(temp_audio_path, "動画")
                finally:
                    if temp_audio_path.exists():
                        temp_audio_path.unlink()
                        self.add_log(f"一時音声ファイル削除: {temp_audio_path}")
            else:
                result["txt_out"] = self.transcript_txt_path(
                    result["output_dir"],
                    self.app_settings,
                    result.get("transcription_pattern"),
                )
                all_rows = self.transcribe_pattern_job(result)
            memo_path = result.get("memo_path")
            memo_text = ""
            if memo_path and Path(memo_path).exists():
                memo_text = Path(memo_path).read_text(encoding="utf-8")

            metadata = {
                "transcription_pattern": result.get("transcription_pattern"),
                "transcription_pattern_label": self.transcription_pattern_label(result.get("transcription_pattern")),
                "output_dir": str(result.get("output_dir", "")),
            }
            self.transcriber.export_transcript(all_rows, result["txt_out"], memo_text=memo_text, metadata=metadata)
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

    def transcribe_pattern_job(self, result):
        pattern = result.get("transcription_pattern") or DEFAULT_TRANSCRIPTION_PATTERN
        if pattern == "manual":
            return self.transcribe_manual_job(result)
        routes = PATTERN_ROUTES.get(pattern)
        if not routes:
            raise RuntimeError(f"未対応の文字起こしパターンです: {pattern}")

        self.add_log(f"文字起こしパターン: {self.transcription_pattern_label(pattern)}")
        self.validate_pattern_routes(result, routes)
        if any(route["model"] in API_TRANSCRIBE_MODELS for route in routes):
            api_key = self.transcriber.resolve_api_key(self.transcriber.openai_api_key)
            if not api_key:
                raise RuntimeError(
                    "この文字起こしパターンはOpenAI APIを使います。"
                    "OpenAI APIキーを設定してから再実行してください。"
                )
        if pattern in PARALLEL_TRANSCRIPTION_PATTERNS:
            return self.transcribe_pattern_routes_parallel(result, routes)

        all_rows = []
        for route in routes:
            all_rows.extend(self.transcribe_pattern_route(result, route))
        return all_rows

    def validate_pattern_routes(self, result, routes):
        for route in routes:
            audio_path = Path(result[route["audio_key"]])
            if not audio_path.exists():
                raise RuntimeError(f"文字起こし対象音声が見つかりません: {audio_path}")

    def transcribe_pattern_routes_parallel(self, result, routes):
        route_transcribers = [
            self.new_api_route_transcriber(route["model"])
            if route["model"] in API_TRANSCRIBE_MODELS
            else self.transcriber
            for route in routes
        ]

        all_rows = []
        with ThreadPoolExecutor(max_workers=len(routes)) as executor:
            futures = [
                executor.submit(self.transcribe_pattern_route, result, route, transcriber)
                for route, transcriber in zip(routes, route_transcribers)
            ]
            for future in futures:
                all_rows.extend(future.result())
        return all_rows

    def new_api_route_transcriber(self, model_size):
        route_settings = self.app_settings.copy()
        route_settings["model_size"] = model_size
        transcriber = Transcriber(self.add_log)
        transcriber.set_settings(route_settings)
        return transcriber

    def transcribe_pattern_route(self, result, route, transcriber=None):
        audio_path = Path(result[route["audio_key"]])
        if not audio_path.exists():
            raise RuntimeError(f"文字起こし対象音声が見つかりません: {audio_path}")
        self.add_log(
            f"文字起こし経路: {route['source']} / model={route['model']} / speaker={route['speaker']}"
        )
        transcriber = transcriber or self.transcriber
        return transcriber.transcribe_file(
            audio_path,
            route["speaker"],
            source=route["source"],
            model_size=route["model"],
            diarized_prefix=route.get("diarized_prefix"),
        )

    def transcribe_manual_job(self, result):
        model_size = self.app_settings.get("model_size", DEFAULT_SETTINGS["model_size"])
        self.transcriber.set_settings(self.app_settings)
        self.add_log(f"文字起こしパターン: {self.transcription_pattern_label('manual')}")
        self.add_log(f"マニュアル文字起こしモデル: {model_size}")

        if model_size in API_TRANSCRIBE_MODELS:
            api_key = self.transcriber.resolve_api_key(self.transcriber.openai_api_key)
            if not api_key:
                raise RuntimeError(
                    "マニュアル文字起こしで選択したモデルはOpenAI APIを使います。"
                    "OpenAI APIキーを設定してから再実行してください。"
                )
            mixed_wav = result.get("mixed_wav")
            mixed_path = Path(mixed_wav) if mixed_wav else None
            if mixed_path and mixed_path.exists():
                self.add_log(f"文字起こし経路: mixed / model={model_size} / speaker=MIX")
                return self.transcriber.transcribe_file(mixed_path, "MIX", source="mixed")

        all_rows = []
        for audio_key, speaker, source in (
            ("system_wav", "相手", "system"),
            ("mic_wav", "自分", "mic"),
        ):
            audio_path = Path(result[audio_key])
            if not audio_path.exists():
                raise RuntimeError(f"マニュアル文字起こし対象音声が見つかりません: {audio_path}")
            self.add_log(f"文字起こし経路: {source} / model={model_size} / speaker={speaker}")
            all_rows.extend(self.transcriber.transcribe_file(audio_path, speaker, source=source))
        return all_rows

    def enqueue_transcription_job(self, result):
        if not result.get("video_path") and not result.get("transcription_pattern"):
            pattern = self.ask_transcription_pattern()
            if not pattern:
                self.add_log(f"文字起こしキュー追加を中止: {result.get('output_dir')}")
                return False
            result["transcription_pattern"] = pattern
        self.transcription_queue.append(result)
        self.refresh_queue_listbox()
        pattern_label = self.transcription_pattern_label(result.get("transcription_pattern"))
        suffix = f" ({pattern_label})" if pattern_label else ""
        self.add_log(f"文字起こしキュー追加: {result['output_dir']}{suffix}")
        return True

    def refresh_queue_listbox(self):
        self.queue_listbox.delete(0, "end")
        for idx, item in enumerate(self.transcription_queue, start=1):
            label = item.get("video_path") or item.get("output_dir")
            pattern_label = self.transcription_pattern_label(item.get("transcription_pattern"))
            suffix = f" / {pattern_label}" if pattern_label else ""
            self.queue_listbox.insert("end", f"{idx}. {label}{suffix}")
        if hasattr(self, "queue_count_label"):
            self.queue_count_label.configure(text=f"追加済みファイル（{len(self.transcription_queue)} 件）")
        if hasattr(self, "queue_placeholder"):
            if self.transcription_queue:
                self.queue_placeholder.pack_forget()
                self.queue_listbox.pack(fill="both", expand=True, padx=12, pady=12)
            else:
                self.queue_listbox.pack_forget()
                self.queue_placeholder.pack(fill="both", expand=True, padx=12, pady=12)

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
            "mixed_wav": folder / "mixed.m4a",
            "txt_out": self.transcript_txt_path(folder),
            "memo_path": folder / "memo.txt",
        }
        has_mixed = job["mixed_wav"].exists()
        has_split_audio = job["system_wav"].exists() or job["mic_wav"].exists()
        if not has_mixed and not has_split_audio:
            safe_messagebox_error("エラー", "mixed.m4a、system.wav、mic.wav のいずれも見つかりません。")
            return
        self.enqueue_transcription_job(job)

    def transcription_pattern_label(self, pattern):
        return TRANSCRIPTION_PATTERNS.get(pattern, "")

    def ask_transcription_pattern(self):
        selected_pattern = {"value": None}
        dialog = ctk.CTkToplevel(self.root)
        dialog.title("文字起こしパターン")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.configure(fg_color=self.bg_color)

        frame = ctk.CTkFrame(dialog, fg_color="transparent")
        frame.pack(fill="both", expand=True, padx=16, pady=16)
        ctk.CTkLabel(frame, text="この録音の文字起こしパターンを選択してください。",
                     font=self.font_body, text_color=self.text_color).pack(anchor="w", pady=(0, 10))

        label_to_pattern = {label: pattern for pattern, label in TRANSCRIPTION_PATTERNS.items()}
        pattern_var = tk.StringVar(value=TRANSCRIPTION_PATTERNS[DEFAULT_TRANSCRIPTION_PATTERN])
        self._option_menu(frame, pattern_var, list(label_to_pattern.keys()), None).pack(fill="x", pady=(0, 12))

        button_frame = ctk.CTkFrame(frame, fg_color="transparent")
        button_frame.pack(fill="x")

        def accept():
            selected_pattern["value"] = label_to_pattern.get(pattern_var.get())
            dialog.destroy()

        def cancel():
            dialog.destroy()

        self._primary_button(button_frame, "追加", accept, font=self.font_small).pack(side="left")
        self._ghost_button(button_frame, "キャンセル", cancel, border=self.border_color,
                          font=self.font_small).pack(side="left", padx=(8, 0))
        dialog.protocol("WM_DELETE_WINDOW", cancel)
        dialog.bind("<Return>", lambda _event: accept())
        dialog.bind("<Escape>", lambda _event: cancel())
        # CTkToplevel はまれに親の背後に出るため、前面化してから grab する。
        dialog.after(150, lambda: (dialog.lift(), dialog.focus(), dialog.grab_set()))
        self.root.wait_window(dialog)
        return selected_pattern["value"]

    def add_video_queue_from_dialog(self):
        video_path = filedialog.askopenfilename(
            title="文字起こし対象動画を選択",
            filetypes=[
                ("動画ファイル", "*.mp4 *.mov *.mkv *.avi *.m4v *.wmv *.flv *.webm"),
                ("すべてのファイル", "*.*"),
            ],
        )
        if not video_path:
            return
        video_path = Path(video_path)
        job = {
            "output_dir": video_path.parent,
            "video_path": video_path,
            "txt_out": video_path.with_suffix('.txt'),
            "memo_path": None,
        }
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
        self.start_btn.configure(state="normal")
        self.recording_stop_btn.configure(state="normal")
        self.reload_btn.configure(state="normal")
        self.system_combo.configure(state="normal")
        self.mic_combo.configure(state="normal")

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

        system_pos = self._option_index(self.system_device_var.get(), self._system_values)
        mic_pos = self._option_index(self.mic_device_var.get(), self._mic_values)
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
        self.root.configure(fg_color=self.bg_color)
        self.root.title(APP_TITLE)
        self.refresh_status_banner()
        self._flash_taskbar_once()

    def refresh_status_banner(self):
        if not hasattr(self, "status_banner"):
            return

        status = self.status_var.get()
        if "録音中" in status:
            title_fg = self.accent_color
            detail_fg = self.text_color
            self.status_title_var.set("● 録音中")
            self.status_detail_var.set(self.timer_var.get())
        elif "文字起こし中" in status or self.transcription_running:
            title_fg = self.accent_color
            detail_fg = self.muted_color
            self.status_title_var.set("文字起こし中")
            self.status_detail_var.set("録音データを txt に変換しています")
        elif "エラー" in status:
            title_fg = "#fb7185"
            detail_fg = "#fb7185"
            self.status_title_var.set("エラー")
            self.status_detail_var.set("詳細はログを確認してください")
        elif "デバイス" in status:
            title_fg = self.text_color
            detail_fg = self.muted_color
            self.status_title_var.set(status)
            self.status_detail_var.set("録音デバイスの状態を確認しています")
        else:
            title_fg = self.text_color
            detail_fg = self.muted_color
            self.status_title_var.set("停止中")
            self.status_detail_var.set("録音は開始されていません")

        self.status_title_label.configure(text_color=title_fg)
        self.status_detail_label.configure(text_color=detail_fg)
        self.current_transcription_label.configure(
            text_color=(self.accent_color if "待機" not in self.current_transcription_var.get() else self.muted_color))

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
            subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )

    def _get_lid_action_value(self, power_mode):
        result = subprocess.run(
            ["powercfg", f"/GET{power_mode}VALUEINDEX", "SCHEME_CURRENT", "SUB_BUTTONS", LIDCLOSE_GUID],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
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

    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")

    root = ctk.CTk()

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
