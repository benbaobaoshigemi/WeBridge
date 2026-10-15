"""Native message forwarding performed by the Bot's desktop transport."""

from collections import Counter
import time
from types import SimpleNamespace

import win32api
import win32gui

from .sender import ClipboardSnapshot, _wait_idle, wechat_hwnd


def _bubble_rectangle(row, viewport):
    import numpy as np
    from PIL import ImageGrab

    left, top = max(row.left, viewport.left), max(row.top, viewport.top)
    right, bottom = min(row.right, viewport.right), min(row.bottom, viewport.bottom)
    image = ImageGrab.grab(bbox=(left, top, right, bottom)).convert("RGB")
    pixels = np.asarray(image)
    colors = Counter(image.getdata()).most_common(8)
    for color, count in colors[1:]:
        ys, xs = np.nonzero(np.all(pixels == color, axis=2))
        x0, x1, y0, y1 = int(xs.min()), int(xs.max()) + 1, int(ys.min()), int(ys.max()) + 1
        width, height = x1 - x0, y1 - y0
        if width >= 40 and height >= 20 and count / (width * height) >= 0.45:
            return SimpleNamespace(left=left + x0, top=top + y0, right=left + x1, bottom=top + y1)
    raise RuntimeError("无法从消息行定位文字气泡")


def forward_texts(source_name, target_name, messages, confirm, recent):
    from wechatauto.forward import ForwardOps
    from wechatauto.uia_driver import WeChatUIA

    occurrences = Counter(row["text"].strip() for row in recent if row["type"] == "text")
    positions = {row["text"].strip(): index for index, row in enumerate(recent)
                 if row["type"] == "text" and occurrences[row["text"].strip()] == 1}

    class ForwardingControls(WeChatUIA):
        expected_text = ""

        def _mouse_wheel(self, delta):
            x, y = win32gui.GetCursorPos()
            saved = []

            def prepare(hwnd, _):
                if win32gui.GetClassName(hwnd).startswith("MMUIRenderSubWindow"):
                    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                    if left <= x < right and top <= y < bottom:
                        style = win32gui.GetWindowLong(hwnd, self.GWL_EXSTYLE)
                        if style & self.WS_EX_TRANSPARENT:
                            win32gui.SetWindowLong(hwnd, self.GWL_EXSTYLE, style & ~self.WS_EX_TRANSPARENT)
                            saved.append((hwnd, style))

            win32gui.EnumChildWindows(wechat_hwnd(), prepare, None)
            try:
                win32api.mouse_event(0x0800, 0, 0, delta, 0)
            finally:
                self._restore_transparent(saved)

        def find_in_message_list(self, predicate, match_last=False, max_scrolls=30):
            expected = self.expected_text.strip()
            message_list = self._message_list()
            viewport = message_list.BoundingRectangle

            def reveal(hit):
                self._set_cursor((viewport.left + viewport.right) // 2, (viewport.top + viewport.bottom) // 2)
                for _ in range(max_scrolls):
                    rect = hit[2]
                    if rect.top >= viewport.top and rect.bottom <= viewport.bottom:
                        return (hit[0], hit[1], _bubble_rectangle(rect, viewport))
                    self._mouse_wheel(120 if rect.top < viewport.top else -120)
                    time.sleep(0.2)
                    row = next((row for row in message_list.GetChildren()
                                if row.ClassName == "mmui::ChatTextItemView" and (row.Name or "").strip() == expected), None)
                    if row is None:
                        raise RuntimeError("滚动后目标消息离开可见区域")
                    hit = (row.ClassName, row.Name, row.BoundingRectangle)
                raise RuntimeError("无法将目标消息完整移入可见区域")

            self._set_cursor((viewport.left + viewport.right) // 2, (viewport.top + viewport.bottom) // 2)

            def visible():
                return [(row.ClassName, row.Name or "", row.BoundingRectangle)
                        for row in message_list.GetChildren()]

            def signature(rows):
                return tuple((cls, name, rect.top, rect.bottom) for cls, name, rect in rows)

            for _ in range(len(recent) + 1):
                rows = visible()
                hit = next((row for row in rows if row[0] == "mmui::ChatTextItemView"
                            and row[1].strip() == expected), None)
                if hit:
                    return reveal(hit)
                known = [positions[name.strip()] for cls, name, rect in rows
                         if cls == "mmui::ChatTextItemView" and name.strip() in positions
                         and rect.bottom > viewport.top and rect.top < viewport.bottom]
                target = positions[expected]
                if known and min(known) <= target <= max(known):
                    return None
                delta = 240 if known and max(known) < target else -240
                before = signature(rows)
                self._mouse_wheel(delta)
                deadline = time.monotonic() + 0.8
                while signature(visible()) == before:
                    if time.monotonic() >= deadline:
                        return None
                    time.sleep(0.05)
            return None

    _wait_idle()
    previous, cursor = win32gui.GetForegroundWindow(), win32gui.GetCursorPos()
    clipboard = ClipboardSnapshot()
    driver = ForwardingControls(timeout=5, search_timeout=1)
    operations = ForwardOps(uia=driver)
    results = []
    try:
        if not driver.ensure_window():
            raise RuntimeError("微信原生聊天控件未能开启，转发未开始")
        for message in messages:
            driver.expected_text = message["text"]
            opened = operations.open_dialog(chat=source_name, match=message["text"], max_scrolls=30)
            if not opened["ok"]:
                results.append({"message_id": message["id"], "sent": False, "error": opened["reason"]})
                break
            try:
                selected = operations.select([target_name])
                if selected["picked"] != [target_name] or operations.selected() != [target_name]:
                    raise RuntimeError("未唯一选中指定收件人，未点击发送")
                sent = operations.send(expect=1)
                if not sent["ok"]:
                    raise RuntimeError(sent["reason"])
                confirmed = confirm(message)
                results.append({"message_id": message["id"], "sent": True, "confirmed": confirmed})
                if not confirmed:
                    break
            finally:
                operations.close_dialog()
        return results
    finally:
        clipboard.restore()
        win32api.SetCursorPos(cursor)
        if previous and win32gui.IsWindow(previous):
            win32gui.SetForegroundWindow(previous)
