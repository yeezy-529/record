import queue
import threading
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

APP_TITLE = "文字起こしレコーダー"
BASE_DIR = Path.cwd() / "mtg_records"
BASE_DIR.mkdir(parents=True, exist_ok=True)


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("900x620")

        self.main_thread = threading.get_ident()
        self.is_recording = False
        self.record_started_at = None

        self.name_var = tk.StringVar(value="")
        self.status_var = tk.StringVar(value="待機中")

        self.task_queue = queue.Queue()
        self.total_jobs = 0
        self.done_jobs = 0
        self.active_job = None
        self.transcribing = False

        self._build_ui()

    def _build_ui(self):
        header = ttk.Frame(self.root, padding=12)
        header.pack(fill="x")

        ttk.Label(header, text="任意名:").pack(side="left")
        self.name_entry = ttk.Entry(header, textvariable=self.name_var, width=40)
        self.name_entry.pack(side="left", padx=8)

        ttk.Button(header, text="録音開始", command=self.start_recording).pack(side="left", padx=4)
        ttk.Button(header, text="録音停止", command=self.stop_recording).pack(side="left", padx=4)
        ttk.Button(header, text="ダミー音声をキュー追加", command=self.enqueue_dummy).pack(side="left", padx=4)

        status_frame = ttk.LabelFrame(self.root, text="ステータス", padding=12)
        status_frame.pack(fill="x", padx=12, pady=6)
        ttk.Label(status_frame, textvariable=self.status_var).pack(anchor="w")

        queue_frame = ttk.LabelFrame(self.root, text="文字起こしキュー", padding=12)
        queue_frame.pack(fill="both", expand=True, padx=12, pady=6)
        self.queue_list = tk.Listbox(queue_frame, height=16)
        self.queue_list.pack(fill="both", expand=True)

        log_frame = ttk.LabelFrame(self.root, text="ログ", padding=12)
        log_frame.pack(fill="both", expand=True, padx=12, pady=6)
        self.log = tk.Text(log_frame, height=10)
        self.log.pack(fill="both", expand=True)

    def add_log(self, text: str):
        if threading.get_ident() != self.main_thread:
            self.root.after(0, lambda: self.add_log(text))
            return
        now = datetime.now().strftime("%H:%M:%S")
        self.log.insert("end", f"[{now}] {text}\n")
        self.log.see("end")

    def update_status(self, text: str):
        if threading.get_ident() != self.main_thread:
            self.root.after(0, lambda: self.update_status(text))
            return
        self.status_var.set(text)

    def start_recording(self):
        if self.is_recording:
            return
        self.is_recording = True
        self.record_started_at = datetime.now()
        self.update_status("録音中")
        self.add_log("録音を開始しました。任意名は録音中に変更可能です。")

    def stop_recording(self):
        if not self.is_recording:
            return
        self.is_recording = False
        final_name = self.build_base_name(self.record_started_at, self.name_var.get())
        self.update_status("録音停止（文字起こし待機中）")
        self.add_log(f"録音を停止しました。確定名: {final_name}")
        self.enqueue_job(final_name)

    def build_base_name(self, dt: datetime, optional_name: str):
        suffix = (optional_name or "no_name").strip()
        return f"{dt.strftime('%Y年%m月%d日%H:%M')}-{suffix}"

    def enqueue_job(self, base_name: str):
        self.total_jobs += 1
        self.task_queue.put(base_name)
        self.queue_list.insert("end", f"待機: {base_name}")
        self.add_log(f"キュー追加: {base_name}")
        self.kick_worker()

    def enqueue_dummy(self):
        now = datetime.now()
        name = self.build_base_name(now, self.name_var.get() or "dummy")
        self.enqueue_job(name)

    def kick_worker(self):
        if self.transcribing:
            return
        if self.task_queue.empty():
            return
        self.transcribing = True
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        while not self.task_queue.empty():
            base_name = self.task_queue.get()
            self.active_job = base_name
            self.update_status(f"文字起こし中 ({self.done_jobs + 1}/{self.total_jobs}): {base_name}")
            self.add_log(f"文字起こし開始: {base_name}")
            try:
                out_dir = BASE_DIR / base_name
                out_dir.mkdir(parents=True, exist_ok=True)
                txt_path = out_dir / f"{base_name}.txt"
                txt_path.write_text("ここに文字起こし結果が入ります。\n", encoding="utf-8")
            except Exception as e:
                self.add_log(f"文字起こし失敗: {base_name} ({e})")
            self.done_jobs += 1
            self.add_log(f"文字起こし完了: {base_name}")

        self.transcribing = False
        self.active_job = None
        self.update_status("待機中")
        self.root.after(0, lambda: messagebox.showinfo("文字起こし完了", f"{self.done_jobs}件の文字起こしが完了しました。"))


def main():
    root = tk.Tk()
    app = App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
