import os
import sys
import wave
import queue
import threading
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

import pyaudiowpatch as pyaudio
from faster_whisper import WhisperModel

APP_TITLE = "文字起こしレコーダー"
BASE_DIR = Path.cwd() / "mtg_records"
BASE_DIR.mkdir(parents=True, exist_ok=True)
ERROR_LOG = BASE_DIR / "error_log.txt"

FORMAT = pyaudio.paInt16
CHANNELS = 1
RATE = 16000
CHUNK = 2048
LANGUAGE = "ja"
MODEL_SIZE = "medium"
COMPUTE_TYPE = "int8"


def ensure_error_log():
    if not ERROR_LOG.exists():
        ERROR_LOG.write_text("error log initialized\n", encoding="utf-8")


def write_error_log(title, error):
    with open(ERROR_LOG, "a", encoding="utf-8") as f:
        f.write("\n" + "=" * 80 + "\n")
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} - {title}\n")
        f.write("=" * 80 + "\n")
        f.write(f"{repr(error)}\n")
        f.write(traceback.format_exc() + "\n")


@dataclass
class RecordResult:
    folder: Path
    audio_file: Path
    transcript_file: Path


class AudioRecorder:
    def __init__(self, log_func):
        self.log = log_func
        self.p = None
        self.stream = None
        self.thread = None
        self.is_recording = False
        self.frames = []
        self.output_dir = None
        self.audio_file = None
        self.pending_name = ""
        self.current_folder_stamp = ""

    def _folder_name(self, custom_name: str) -> str:
        suffix = custom_name.strip() or "無題"
        return f"{self.current_folder_stamp}-{suffix}"

    def start(self, custom_name: str):
        if self.is_recording:
            return
        self.current_folder_stamp = datetime.now().strftime("%Y年%m月%d日%H:%M")
        self.pending_name = custom_name
        folder_name = self._folder_name(custom_name)

        self.output_dir = BASE_DIR / folder_name
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.audio_file = self.output_dir / f"{folder_name}.wav"

        self.p = pyaudio.PyAudio()
        self.stream = self.p.open(
            format=FORMAT,
            channels=CHANNELS,
            rate=RATE,
            input=True,
            frames_per_buffer=CHUNK,
        )

        self.frames = []
        self.is_recording = True
        self.thread = threading.Thread(target=self._record_loop, daemon=True)
        self.thread.start()
        self.log(f"録音開始: {self.audio_file.name}")

    def update_name(self, custom_name: str):
        if self.is_recording:
            self.pending_name = custom_name

    def _record_loop(self):
        while self.is_recording:
            try:
                self.frames.append(self.stream.read(CHUNK, exception_on_overflow=False))
            except Exception as e:
                write_error_log("record loop", e)
                break

    def stop(self) -> RecordResult | None:
        if not self.is_recording:
            return None

        self.is_recording = False
        if self.thread:
            self.thread.join(timeout=2)

        self.stream.stop_stream()
        self.stream.close()
        self.p.terminate()

        final_folder_name = self._folder_name(self.pending_name)
        final_dir = BASE_DIR / final_folder_name
        if final_dir != self.output_dir:
            if final_dir.exists():
                final_dir = BASE_DIR / f"{final_folder_name}_{datetime.now():%H%M%S}"
            self.output_dir.rename(final_dir)
            self.output_dir = final_dir

        self.audio_file = self.output_dir / f"{self.output_dir.name}.wav"
        with wave.open(str(self.audio_file), "wb") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(pyaudio.get_sample_size(FORMAT))
            wf.setframerate(RATE)
            wf.writeframes(b"".join(self.frames))

        transcript_file = self.output_dir / f"{self.output_dir.name}.txt"
        self.log(f"録音停止: {self.audio_file.name}")
        return RecordResult(self.output_dir, self.audio_file, transcript_file)


class App:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_TITLE)
        self.model = None

        self.recorder = AudioRecorder(self.add_log)
        self.transcribe_queue = queue.Queue()
        self.transcribe_thread = threading.Thread(target=self._transcribe_worker, daemon=True)
        self.transcribe_thread.start()
        self.pending_count = 0
        self.completed_batch = []

        self.status_var = tk.StringVar(value="待機中")
        self.timer_var = tk.StringVar(value="録音時間: 00:00")
        self.name_var = tk.StringVar(value="会議")

        self.start_time = None
        self.timer_job = None

        self._build_ui()
        self._set_status("待機中")

    def _build_ui(self):
        frm = ttk.Frame(self.root, padding=10)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="任意名（録音中も変更可）:").pack(anchor="w")
        ent = ttk.Entry(frm, textvariable=self.name_var, width=40)
        ent.pack(fill="x", pady=(0, 8))
        self.name_var.trace_add("write", self._on_name_change)

        btnf = ttk.Frame(frm)
        btnf.pack(fill="x", pady=(0, 8))
        self.start_btn = ttk.Button(btnf, text="録音開始", command=self.start_recording)
        self.stop_btn = ttk.Button(btnf, text="録音停止", command=self.stop_recording, state="disabled")
        self.start_btn.pack(side="left", padx=(0, 8))
        self.stop_btn.pack(side="left")

        ttk.Label(frm, textvariable=self.timer_var).pack(anchor="w")
        ttk.Label(frm, text="現在ステータス:").pack(anchor="w", pady=(8, 0))
        ttk.Label(frm, textvariable=self.status_var, foreground="blue").pack(anchor="w")

        self.log_text = tk.Text(frm, height=12)
        self.log_text.pack(fill="both", expand=True, pady=(8, 0))

    def _on_name_change(self, *_):
        self.recorder.update_name(self.name_var.get())

    def _set_status(self, status: str):
        self.status_var.set(status)

    def start_recording(self):
        try:
            self.recorder.start(self.name_var.get())
            self.start_btn.config(state="disabled")
            self.stop_btn.config(state="normal")
            self.start_time = datetime.now()
            self._tick_timer()
            self._set_status("録音中")
        except Exception as e:
            write_error_log("start recording", e)
            messagebox.showerror("エラー", str(e))

    def stop_recording(self):
        result = self.recorder.stop()
        if not result:
            return

        if self.timer_job:
            self.root.after_cancel(self.timer_job)
            self.timer_job = None
        self.timer_var.set("録音時間: 00:00")

        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")

        self.pending_count += 1
        self.transcribe_queue.put(result)
        self._set_status(f"文字起こし待ち: {self.pending_count}件")

    def _tick_timer(self):
        if not self.recorder.is_recording:
            return
        elapsed = int((datetime.now() - self.start_time).total_seconds())
        self.timer_var.set(f"録音時間: {elapsed//60:02d}:{elapsed%60:02d}")
        self.timer_job = self.root.after(1000, self._tick_timer)

    def _load_model(self):
        if self.model is None:
            self._set_status("文字起こしモデル読込中")
            self.model = WhisperModel(MODEL_SIZE, compute_type=COMPUTE_TYPE)

    def _transcribe_worker(self):
        while True:
            item: RecordResult = self.transcribe_queue.get()
            try:
                self.root.after(0, lambda: self._set_status(f"文字起こし中: {item.audio_file.name}"))
                self._load_model()
                segments, _ = self.model.transcribe(str(item.audio_file), language=LANGUAGE)
                text = "\n".join(seg.text.strip() for seg in segments if seg.text.strip())
                item.transcript_file.write_text(text, encoding="utf-8")
                self.completed_batch.append(item)
                self.root.after(0, lambda: self.add_log(f"文字起こし完了: {item.transcript_file.name}"))
            except Exception as e:
                write_error_log("transcribe", e)
                self.root.after(0, lambda: self.add_log(f"文字起こし失敗: {e}"))
            finally:
                self.pending_count = max(0, self.pending_count - 1)
                if self.pending_count == 0:
                    done_items = list(self.completed_batch)
                    self.completed_batch.clear()
                    self.root.after(0, lambda: self._on_batch_completed(done_items))
                else:
                    self.root.after(0, lambda: self._set_status(f"文字起こし待ち: {self.pending_count}件"))
                self.transcribe_queue.task_done()

    def _on_batch_completed(self, done_items):
        self._set_status("待機中")
        if done_items:
            messagebox.showinfo("文字起こし完了", f"{len(done_items)}件の文字起こしが完了しました。")

    def add_log(self, text):
        now = datetime.now().strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{now}] {text}\n")
        self.log_text.see("end")


def global_exception_handler(exc_type, exc_value, exc_traceback):
    ensure_error_log()
    with open(ERROR_LOG, "a", encoding="utf-8") as f:
        f.write("\n" + "=" * 80 + "\n")
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} - GLOBAL ERROR\n")
        f.write("=" * 80 + "\n")
        traceback.print_exception(exc_type, exc_value, exc_traceback, file=f)
        f.write("\n")


def main():
    ensure_error_log()
    sys.excepthook = global_exception_handler
    root = tk.Tk()
    app = App(root)
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()


if __name__ == "__main__":
    main()
