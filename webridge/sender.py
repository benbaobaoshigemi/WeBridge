"""微信桌面发送器：窗口定位、剪贴板与键盘操作。"""

import ctypes
from ctypes import wintypes
import io
import json
import logging
import os
import struct
import subprocess
import sys
import time

import win32api
import win32clipboard as clipboard
import win32con
import win32gui
import win32process

log = logging.getLogger(__name__)
user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.GetDpiForWindow.argtypes = [wintypes.HWND]
user32.GetDpiForWindow.restype = wintypes.UINT
user32.SetProcessDPIAware()


def wechat_hwnd() -> int:
    windows = []

    def collect(hwnd, _):
        if win32gui.GetWindowText(hwnd) not in ("微信", "WeChat", "Weixin"):
            return
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        process = win32api.OpenProcess(0x410, False, pid)
        try:
            name = win32process.GetModuleFileNameEx(process, 0)
        finally:
            process.Close()
        if name.lower().endswith(("weixin.exe", "wechat.exe")):
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            windows.append(((win32gui.IsWindowVisible(hwnd), (right - left) * (bottom - top)), hwnd))

    win32gui.EnumWindows(collect, None)
    if not windows:
        raise RuntimeError("未找到微信主窗口，请先登录微信")
    return max(windows)[1]


class SendLock:
    """沿用旧服务的跨进程锁，防止迁移期间两个服务同时操作微信。"""

    def __enter__(self):
        import msvcrt
        folder = os.path.join(os.environ["LOCALAPPDATA"], "wechat_mcp")
        os.makedirs(folder, exist_ok=True)
        self.file = open(os.path.join(folder, "send.lock"), "a+b")
        deadline = time.monotonic() + 120
        while True:
            try:
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    self.file.close()
                    raise TimeoutError("等待其他窗口完成微信发送超时")
                time.sleep(0.3)

    def __exit__(self, *args):
        import msvcrt
        try:
            self.file.seek(0)
            msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self.file.close()


def _dropfiles(paths) -> bytes:
    return struct.pack("<IiiII", 20, 0, 0, 0, 1) + ("\0".join(paths) + "\0\0").encode("utf-16-le")


class ClipboardSnapshot:
    """复制剪贴板实际数据，保留文字、DIB 图片、文件列表及 HTML/RTF 等格式。"""

    def __init__(self):
        self.items = []
        clipboard.OpenClipboard()
        try:
            # Windows 可以从位图生成 DIB；先请求 DIB，之后保存独立的字节数据。
            if clipboard.IsClipboardFormatAvailable(win32con.CF_BITMAP):
                self.items.append((win32con.CF_DIB, clipboard.GetClipboardData(win32con.CF_DIB)))
            fmt = 0
            while True:
                fmt = clipboard.EnumClipboardFormats(fmt)
                if not fmt:
                    break
                if fmt in (win32con.CF_BITMAP, win32con.CF_PALETTE, win32con.CF_METAFILEPICT,
                           win32con.CF_ENHMETAFILE, win32con.CF_DSPBITMAP,
                           win32con.CF_DSPMETAFILEPICT, win32con.CF_DSPENHMETAFILE):
                    continue
                # Indexed OLE file streams/descriptors are not Win32 global-memory
                # clipboard values. Existing files are preserved through CF_HDROP.
                if fmt >= 0xC000 and clipboard.GetClipboardFormatName(fmt) in (
                    "DataObject", "Ole Private Data", "FileContents",
                    "FileGroupDescriptor", "FileGroupDescriptorW",
                ):
                    continue
                if fmt == win32con.CF_DIB and any(f == fmt for f, _ in self.items):
                    continue
                data = clipboard.GetClipboardData(fmt)
                if fmt == win32con.CF_HDROP:
                    data = _dropfiles(data)
                self.items.append((fmt, data))
        finally:
            clipboard.CloseClipboard()

    def restore(self):
        clipboard.OpenClipboard()
        try:
            clipboard.EmptyClipboard()
            for fmt, data in self.items:
                clipboard.SetClipboardData(fmt, data)
        finally:
            clipboard.CloseClipboard()


def _put_clipboard(kind: str, value: str):
    if kind == "file":
        # 微信使用 Windows 标准文件剪贴板中的 FileNameW 等关联格式。
        # 单独写入 CF_HDROP 虽能枚举出路径，在当前微信中不会粘贴文件。
        subprocess.run(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command",
             "Set-Clipboard -LiteralPath $env:WEBRIDGE_FILE_PATH"],
            env={**os.environ, "WEBRIDGE_FILE_PATH": value},
            check=True, capture_output=True, timeout=10,
        )
        return
    if kind == "image":
        from PIL import Image
        with Image.open(value) as image:
            with io.BytesIO() as buffer:
                image.convert("RGB").save(buffer, "BMP")
                data = buffer.getvalue()[14:]
        fmt = win32con.CF_DIB
    else:
        fmt, data = win32con.CF_UNICODETEXT, value
    clipboard.OpenClipboard()
    try:
        clipboard.EmptyClipboard()
        clipboard.SetClipboardData(fmt, data)
    finally:
        clipboard.CloseClipboard()


def _wait_idle():
    class LastInput(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetTickCount.restype = wintypes.DWORD
    deadline = time.monotonic() + 90
    while True:
        info = LastInput(ctypes.sizeof(LastInput), 0)
        if not user32.GetLastInputInfo(ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        elapsed = (kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF
        if elapsed >= 3000:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError("键鼠持续忙碌，发送未开始")
        time.sleep(0.25)


def _activate(hwnd):
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    elif not win32gui.IsWindowVisible(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
    win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
    win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
    win32gui.SetForegroundWindow(hwnd)
    time.sleep(0.3)
    if win32gui.GetForegroundWindow() != hwnd:
        raise RuntimeError("无法激活微信主窗口")


class DesktopSender:
    def send(self, contact: str, kind: str, value: str):
        from .desktop_worker import runtime_directory

        request = {"operation": "send", "contact": contact, "kind": kind, "value": value}
        process = subprocess.run(
            [sys.executable, "-m", "webridge.desktop_worker"],
            input=json.dumps(request, ensure_ascii=False).encode("utf-8"),
            capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW,
            cwd=runtime_directory(),
        )
        if process.returncode:
            raise RuntimeError("微信发送进程失败：" + process.stderr.decode("utf-8", errors="replace")[-2000:])
        result = json.loads(process.stdout.decode("utf-8"))
        if "error" in result:
            raise RuntimeError(result["error"])

    def _send_native(self, contact: str, kind: str, value: str):
        import uiautomation as auto
        from wechatauto.uia_driver import WeChatUIA

        if kind != "text" and not os.path.isfile(value):
            raise FileNotFoundError(value)
        _wait_idle()
        hwnd = wechat_hwnd()
        previous = win32gui.GetForegroundWindow()
        cursor = win32gui.GetCursorPos()
        saved = ClipboardSnapshot()
        try:
            _activate(hwnd)
            driver = WeChatUIA(timeout=5, search_timeout=1)
            if not driver.ensure_window():
                raise RuntimeError("无法打开微信原生聊天控件")
            if driver.current_chat() != contact and not driver.open_chat(contact):
                raise RuntimeError("无法打开指定会话：" + contact)
            if driver.current_chat() != contact:
                raise RuntimeError("当前会话与目标会话不符")
            field = driver._chat_input()
            if field is None:
                raise RuntimeError("找不到目标会话的聊天输入框")
            driver._click_ctrl(field)
            field.SetFocus()
            if kind == "text":
                field.SendKeys("{Ctrl}a{Delete}", waitTime=0.05)
            _put_clipboard(kind, value)
            field.SendKeys("{Ctrl}v", waitTime=0.05)
            time.sleep(min(len(value) * 0.02, 2) + 0.5 if kind == "text" else 1.5)
            if win32gui.GetForegroundWindow() != hwnd:
                raise RuntimeError("粘贴后微信失去焦点，未按下发送键；请检查聊天输入框")
            if kind == "text":
                field.SendKeys("{Enter}", waitTime=0.05)
            else:
                auto.SendKeys("{Enter}")
            time.sleep(0.3)
        finally:
            try:
                saved.restore()
            except Exception:
                log.exception("发送后恢复剪贴板失败")
            try:
                win32api.SetCursorPos(cursor)
            finally:
                if previous and win32gui.IsWindow(previous):
                    try:
                        win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
                        win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
                        win32gui.SetForegroundWindow(previous)
                    except Exception:
                        log.exception("发送后恢复前台窗口失败")
