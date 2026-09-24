#!/usr/bin/env python3
"""
launcher.pyw — Account Watcher GUI 启动器

双击运行，无终端黑窗。自动启动 watchdog.py / kline_watcher.py，
窗口实时显示日志，关闭时自动终止子进程。
"""

import os
import sys
import subprocess
import threading
import signal
import time
import queue
import socket

import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WATCHDOG_PATH = os.path.join(SCRIPT_DIR, "watchdog.py")
KLINE_WATCHER_PATH = os.path.join(SCRIPT_DIR, "kline_watcher.py")
LOG_DIR = os.path.join(SCRIPT_DIR, "logs")

LOCK_PORT = 18081


def _acquire_lock():
    """绑定本地 TCP 端口实现单实例锁（跨进程可靠，进程退出自动释放）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(1)
        return s
    except OSError:
        return None


class LauncherApp:
    """Account Watcher GUI 启动器"""

    def __init__(self, root):
        self.root = root
        self.root.title("Account Watcher — GUI 启动器")
        self.root.geometry("780x520")
        self.root.minsize(600, 400)

        # 居中窗口
        self._center_window()

        # 进程 & 线程管理
        self.process = None
        self.log_queue = queue.Queue()
        self.running = False
        self.poll_thread = None

        # 构建 UI
        self._build_ui()

        # 定时检查日志队列
        self.root.after(100, self._poll_log_queue)

        # 关闭事件拦截
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── 窗口居中 ────────────────────────────────────────────
    def _center_window(self):
        self.root.update_idletasks()
        w, h = 780, 520
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = (sw - w) // 2
        y = (sh - h) // 2
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    # ── UI 构建 ─────────────────────────────────────────────
    def _build_ui(self):
        # 顶部控制区
        control_frame = ttk.Frame(self.root, padding=8)
        control_frame.pack(fill=tk.X)

        # 启动模式选择
        ttk.Label(control_frame, text="启动模式:").pack(side=tk.LEFT, padx=(0, 4))
        self.mode_var = tk.StringVar(value="watchdog")
        mode_combo = ttk.Combobox(
            control_frame, textvariable=self.mode_var,
            values=["watchdog", "kline_watcher"],
            state="readonly", width=16,
        )
        mode_combo.pack(side=tk.LEFT, padx=(0, 12))

        # 启动 / 停止按钮
        self.start_btn = ttk.Button(
            control_frame, text="▶ 启动", command=self._start_process
        )
        self.start_btn.pack(side=tk.LEFT, padx=(0, 6))

        self.stop_btn = ttk.Button(
            control_frame, text="■ 停止", command=self._stop_process, state=tk.DISABLED
        )
        self.stop_btn.pack(side=tk.LEFT, padx=(0, 6))

        # 清空日志按钮
        ttk.Button(
            control_frame, text="清空日志", command=self._clear_log
        ).pack(side=tk.LEFT, padx=(0, 6))

        # 打开日志目录按钮
        ttk.Button(
            control_frame, text="打开日志目录", command=self._open_log_dir
        ).pack(side=tk.LEFT)

        # 日志显示区
        log_frame = ttk.Frame(self.root, padding=(8, 0, 8, 8))
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.log_text = scrolledtext.ScrolledText(
            log_frame, wrap=tk.WORD, font=("Consolas", 10),
            bg="#1e1e1e", fg="#d4d4d4", insertbackground="#d4d4d4",
            state=tk.DISABLED,
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)

        # 标签颜色配置
        self.log_text.tag_config("info", foreground="#6a9955")
        self.log_text.tag_config("warn", foreground="#dcdcaa")
        self.log_text.tag_config("error", foreground="#f44747")
        self.log_text.tag_config("time", foreground="#569cd6")

        # 状态栏
        self.status_var = tk.StringVar(value="就绪 — 选择模式后点击「启动」")
        status_bar = ttk.Label(
            self.root, textvariable=self.status_var,
            relief=tk.SUNKEN, anchor=tk.W, padding=(6, 2),
        )
        status_bar.pack(fill=tk.X)

    # ── 子进程管理 ──────────────────────────────────────────
    def _get_script_path(self):
        mode = self.mode_var.get()
        if mode == "watchdog":
            return "watchdog.py"
        else:
            return "kline_watcher.py"

    def _start_process(self):
        if self.running:
            return

        script = self._get_script_path()
        script_path = os.path.join(SCRIPT_DIR, script)

        if not os.path.exists(script_path):
            self._log(f"[错误] 找不到脚本: {script_path}", "error")
            return

        try:
            # 启动子进程
            self.process = subprocess.Popen(
                [sys.executable, script_path],
                cwd=SCRIPT_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        except Exception as e:
            self._log(f"[错误] 启动失败: {e}", "error")
            return

        self.running = True
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        # 禁用模式选择
        for child in self.root.winfo_children():
            if isinstance(child, ttk.Frame):
                for c in child.winfo_children():
                    if isinstance(c, ttk.Combobox):
                        c.config(state=tk.DISABLED)

        self.status_var.set(f"● 运行中 — {script}")
        self._log(f"[启动] {script} (PID: {self.process.pid})", "info")

        # 启动读取线程
        self.poll_thread = threading.Thread(
            target=self._read_output, daemon=True
        )
        self.poll_thread.start()

    def _read_output(self):
        """读取子进程输出到队列（线程安全）"""
        try:
            for line in iter(self.process.stdout.readline, ""):
                if not line:
                    break
                self.log_queue.put(line)
        except (ValueError, OSError):
            pass
        finally:
            self.log_queue.put(None)  # 结束信号

    def _stop_process(self):
        if not self.running or not self.process:
            return

        pid = self.process.pid
        script = self._get_script_path()
        self._log(f"[停止] 正在终止 {script} (PID: {pid})...", "warn")

        try:
            if sys.platform == "win32":
                # Windows 下用 taskkill 杀进程树
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True, timeout=5,
                )
            else:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
        except Exception as e:
            self._log(f"[停止] 终止失败: {e}", "error")

        self.process = None
        self.running = False
        self._reset_ui()
        self._log(f"[停止] {script} 已终止", "info")
        self.status_var.set("已停止")

    def _reset_ui(self):
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        for child in self.root.winfo_children():
            if isinstance(child, ttk.Frame):
                for c in child.winfo_children():
                    if isinstance(c, ttk.Combobox):
                        c.config(state="readonly")

    # ── 日志显示 ────────────────────────────────────────────
    def _log(self, msg, tag=None):
        """向日志框追加一行"""
        timestamp = time.strftime("%H:%M:%S")
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, f"[{timestamp}] ", "time")
        if tag:
            self.log_text.insert(tk.END, f"{msg}\n", tag)
        else:
            self.log_text.insert(tk.END, f"{msg}\n")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _clear_log(self):
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete(1.0, tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _poll_log_queue(self):
        """定时从队列取日志并显示"""
        try:
            while True:
                line = self.log_queue.get_nowait()
                if line is None:
                    # 子进程结束，自动清理
                    if self.running:
                        self._log("[信息] 子进程已退出", "warn")
                        self._stop_process()
                    break
                line = line.rstrip("\n\r")
                if not line:
                    continue
                # 根据内容自动标记颜色
                low = line.lower()
                if "error" in low or "失败" in line or "异常" in line:
                    self._log(line, "error")
                elif "warn" in low or "warning" in low:
                    self._log(line, "warn")
                else:
                    self._log(line)
                # 更新状态栏最后一行
                self.status_var.set(f"● 运行中 — {line[-80:]}")
        except queue.Empty:
            pass
        self.root.after(100, self._poll_log_queue)

    # ── 关闭处理 ────────────────────────────────────────────
    def _on_close(self):
        if self.running:
            self._stop_process()
        self.root.destroy()

    # ── 辅助功能 ────────────────────────────────────────────
    def _open_log_dir(self):
        """打开日志目录"""
        if not os.path.exists(LOG_DIR):
            self._log("[信息] 日志目录不存在", "warn")
            return
        try:
            os.startfile(LOG_DIR)
        except AttributeError:
            subprocess.run(["explorer", LOG_DIR])


def main():
    # 单实例锁：绑定本地端口，进程退出自动释放
    lock_socket = _acquire_lock()
    if lock_socket is None:
        root = tk.Tk()
        root.withdraw()
        messagebox.showwarning("警告", "Account Watcher 已在运行中，请勿重复启动。")
        sys.exit(0)

    root = tk.Tk()
    app = LauncherApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()