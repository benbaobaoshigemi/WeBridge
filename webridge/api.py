"""WeBridge 数据与发送 API。

读取走 WeFlow 本地 HTTP API（127.0.0.1:5031），发送通过微信原生无障碍控件。
只在被调用时工作，不自动回复。
"""

import asyncio
import json
import os
import re
import tempfile
import time
from datetime import datetime
from html import unescape

import httpx
import requests

WEFLOW_CONFIG = os.environ.get("WEFLOW_CONFIG") or os.path.join(os.environ["APPDATA"], "WeFlow", "WeFlow-config.json")
STATE_DIR = os.environ.get("WEBRIDGE_STATE_DIR") or os.path.join(os.environ["LOCALAPPDATA"], "WeBridge")

def _cfg() -> dict:
    with open(WEFLOW_CONFIG, encoding="utf-8") as f:
        return json.load(f)


def _base() -> str:
    c = _cfg()
    return f"http://{c.get('httpApiHost') or '127.0.0.1'}:{c.get('httpApiPort') or 5031}"


def _get(path: str, timeout: float = 60, **params) -> dict:
    c = _cfg()
    params = {k: v for k, v in params.items() if v not in (None, "")}
    r = requests.get(_base() + path, params=params, timeout=timeout,
                     headers={"Authorization": "Bearer " + c.get("httpApiToken", "")})
    with r:
        if not r.ok:
            raise RuntimeError(f"WeFlow {path}: HTTP {r.status_code}: {r.text[:500]}")
        return r.json()


def _account_dir() -> str:
    c = _cfg()
    wxid = c.get("myWxid", "")
    return os.path.join(c.get("dbPath", ""), wxid)


_names: dict[str, tuple[float, str]] = {}
_groups: dict[str, tuple[float, dict[str, str]]] = {}


def _name(username: str, session_id: str = "") -> str:
    if not username:
        return ""
    now = time.monotonic()
    if session_id.endswith("@chatroom"):
        cached = _groups.get(session_id)
        if not cached or now - cached[0] >= 300:
            members = get_group_members(session_id)["members"]
            names = {m["wxid"]: (m.get("remark") or m.get("groupNickname") or
                     m.get("nickname") or m.get("displayName") or m["wxid"]) for m in members}
            _groups[session_id] = (now, names)
        member_name = _groups[session_id][1].get(username)
        if member_name:
            return member_name
    cached = _names.get(username)
    if not cached or now - cached[0] >= 300:
        cs = _get("/api/v1/contacts", keyword=username, limit=5).get("contacts", [])
        hit = next((x for x in cs if x.get("username") == username), None)
        _names[username] = (now, (hit.get("remark") or hit.get("displayName") or
                                hit.get("nickname") or username) if hit else username)
    return _names[username][1]


def _tag(xml: str, tag: str) -> str:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", xml or "", re.S)
    if not m:
        return ""
    v = m.group(1).strip()
    if v.startswith("<![CDATA[") and v.endswith("]]>"):
        v = v[9:-3]
    return unescape(v)


def _file_path(title: str, create_time: int) -> str | None:
    """文件消息的本地文件：<账号>/msg/file/<YYYY-MM>/<文件名>，未下载则返回 None。"""
    base = os.path.join(_account_dir(), "msg", "file")
    month = datetime.fromtimestamp(create_time).strftime("%Y-%m")
    p = os.path.join(base, month, os.path.basename(title))
    if os.path.isfile(p):
        return p
    return None


def _simplify(m: dict, session_id: str = "") -> dict:
    t = int(m.get("localType", 0))
    base, sub = t & 0xFFFFFFFF, t >> 32
    ts = int(m.get("createTime", 0))
    sender = m.get("senderUsername", "")
    out = {
        "id": str(m.get("serverId") if str(m.get("serverId") or "0") != "0" else m.get("localId")),
        "time": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S"),
        "from": "我" if str(m.get("isSend")) == "1" else _name(sender, session_id),
        "sender_id": sender,
        "is_send": str(m.get("isSend")) == "1",
    }
    raw = m.get("rawContent") or m.get("content") or ""
    text = m.get("parsedContent") or m.get("content") or ""
    if base == 49:
        appmsg_type = int(_tag(raw, "type") or sub or 0)
        title = _tag(raw, "title")
        if appmsg_type == 6:
            out["type"] = "file"
            out["file_name"] = title
            size = _tag(raw, "totallen")
            if size:
                out["file_size"] = int(size)
            out["file_path"] = _file_path(title, ts)
            if out["file_path"] is None:
                out["note"] = "文件未下载到本地，需在微信里点开下载后才能读取"
            text = f"[文件] {title}"
        elif appmsg_type == 5:
            out["type"] = "link"
            out["url"] = _tag(raw, "url")
            text = f"[链接] {title}"
        elif appmsg_type == 57:
            out["type"] = "quote"
            text = title or text
        else:
            out["type"] = f"appmsg:{appmsg_type}"
            text = title or text
    else:
        out["type"] = {1: "text", 3: "image", 34: "voice", 43: "video", 47: "emoji",
                       10000: "system", 42: "card", 48: "location", 50: "call"}.get(base, str(base))
        if text.lstrip().startswith("<"):
            text = {3: "[图片]", 34: "[语音]", 43: "[视频]", 47: "[表情]"}.get(base, text[:200])
    if m.get("quote"):
        q = m["quote"]
        out["quote"] = f"{q.get('accountName') or q.get('sender', '')}: {q.get('content', '')}"
    for k in ("mediaLocalPath", "mediaUrl"):
        if m.get(k):
            out[k] = m[k]
    out["text"] = text
    return out


def list_sessions(limit: int = 30, keyword: str = "") -> list[dict]:
    """列出最近的微信会话（私聊/群聊），按最后消息时间倒序。keyword 按名称或 ID 过滤。"""
    ss = _get("/api/v1/sessions", limit=limit, keyword=keyword).get("sessions", [])
    out = []
    for s in ss:
        name = _session_name(s)
        out.append({
            "session_id": s["username"],
            "name": name,
            "type": s.get("sessionType"),
            "last_time": datetime.fromtimestamp(int(s.get("lastTimestamp", 0))).strftime("%Y-%m-%d %H:%M:%S"),
            "unread": s.get("unreadCount", 0),
        })
        if len(out) >= limit:
            break
    return out


def search_contacts(keyword: str, limit: int = 20) -> list[dict]:
    """按备注、昵称、微信号或 wxid 搜索联系人和群，返回 session_id 供其他工具使用。"""
    cs = _get("/api/v1/contacts", keyword=keyword, limit=limit).get("contacts", [])
    return [{"session_id": c["username"], "name": c.get("remark") or c.get("displayName"),
             "nickname": c.get("nickname"), "alias": c.get("alias"), "type": c.get("type")} for c in cs]


def _session_name(session: dict) -> str:
    name = session.get("displayName")
    return name if name and name != session["username"] else _name(session["username"])


def get_messages(session_id: str, limit: int = 50, offset: int = 0, start: str = "", end: str = "",
                 keyword: str = "", with_media: bool = False) -> dict:
    """读取某个会话的消息（新的在前）。

    session_id: 私聊为对方 wxid，群聊为 xxx@chatroom（用 list_sessions / search_contacts 获取）。
    start/end: YYYYMMDD 或秒级时间戳。keyword: 按消息文本过滤。
    with_media: 为 true 时导出图片/语音/视频/表情并返回本地路径 mediaLocalPath。
    文件消息直接返回 file_path（微信已下载的本地文件）。
    """
    params = dict(talker=session_id, limit=limit, offset=offset, start=start, end=end, keyword=keyword)
    if with_media:
        params.update(media=1, image=1, voice=1, video=1, emoji=1)
    r = _get("/api/v1/messages", timeout=180, **params)
    return {"session_id": session_id, "name": _name(session_id), "has_more": r.get("hasMore"),
            "messages": [_simplify(m, session_id) for m in r.get("messages", [])]}


def list_files(session_id: str, limit: int = 20) -> list[dict]:
    """列出某个会话里收发过的文件，带本地路径（未下载的 file_path 为 null）。"""
    out, offset = [], 0
    while len(out) < limit:
        r = _get("/api/v1/messages", timeout=180, talker=session_id, limit=500, offset=offset)
        ms = r.get("messages", [])
        out += [x for x in (_simplify(m, session_id) for m in ms) if x.get("type") == "file"]
        if not r.get("hasMore") or not ms:
            break
        offset += len(ms)
    return out[:limit]


def _all_messages(session_id: str, start: str = "", end: str = "", keyword: str = "", cap: int = 50000) -> list[dict]:
    """分页取完一个会话在时间范围内的全部消息（新的在前），最多 cap 条。"""
    out, offset = [], 0
    while len(out) < cap:
        r = _get("/api/v1/messages", timeout=300, talker=session_id, limit=min(2000, cap - len(out)), offset=offset,
                 start=start, end=end, keyword=keyword)
        ms = r.get("messages", [])
        out += [_simplify(m, session_id) for m in ms]
        if not r.get("hasMore") or not ms:
            break
        offset += len(ms)
    return out[:cap]


def search_all_messages(keyword: str, start: str = "", end: str = "", sessions: int = 100,
                        per_session: int = 20) -> dict:
    """跨会话搜索：在最近 sessions 个会话里找包含 keyword 的消息，按会话分组，每个会话最多 per_session 条。

    start/end: YYYYMMDD 或秒级时间戳。结果是精简文本行 "MM-DD HH:MM 名字: 内容"。
    """
    hits, total, errors = {}, 0, {}
    for s in _get("/api/v1/sessions", limit=sessions).get("sessions", []):
        try:
            ms = _get("/api/v1/messages", timeout=120, talker=s["username"], limit=per_session,
                      start=start, end=end, keyword=keyword).get("messages", [])
            if ms:
                lines = [f"{m['time'][5:16]} {m['from']}: {_clip(m['text'], 200)}"
                         for m in (_simplify(x, s["username"]) for x in ms)]
                hits[f"{_name(s['username'])} ({s['username']})"] = lines
                total += len(lines)
        except (requests.RequestException, RuntimeError) as exc:
            errors[s["username"]] = str(exc)
    return {"keyword": keyword, "sessions_matched": len(hits), "messages": total, "hits": hits, "errors": errors}


def export_chat(session_id: str, start: str = "", end: str = "", keyword: str = "") -> dict:
    """把一个会话在时间范围内的全部聊天记录导出成 UTF-8 文本文件（按时间正序，每行一条），返回文件路径。

    适合整段总结、长时间跨度分析：读文件比把几千条消息塞进对话省 token。
    start/end: YYYYMMDD 或秒级时间戳；都不填则读取全部历史范围。
    单次最多导出最新 50000 条，has_more=true 表示需缩小时间范围继续导出。
    """
    ms = list(reversed(_all_messages(session_id, start, end, keyword, cap=50001)))
    truncated = len(ms) > 50000
    ms = ms[-50000:]
    name = _name(session_id)
    d = os.path.join(STATE_DIR, "exports")
    os.makedirs(d, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip(". ")[:60]
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix=f"chat_{safe}_", suffix=".txt", dir=d, delete=False) as f:
        path = f.name
        f.write(f"# {name} ({session_id})  共 {len(ms)} 条\n")
        for m in ms:
            extra = f" [文件:{m['file_path']}]" if m.get("file_path") else ""
            f.write(f"{m['time']} {m['from']}: {' '.join((m['text'] or '').split())}{extra}\n")
    return {"path": path, "count": len(ms),
            "range": [ms[0]["time"], ms[-1]["time"]] if ms else None, "has_more": truncated}


def get_group_members(chatroom_id: str, force_refresh: bool = False) -> dict:
    """获取群成员列表（chatroom_id 形如 xxx@chatroom）。"""
    result = _get("/api/v1/group-members", chatroomId=chatroom_id, forceRefresh=int(force_refresh))
    if force_refresh:
        _groups.pop(chatroom_id, None)
    return result


def _clip(text: str, length: int = 120) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= length else text[:length] + "…"


async def wait_new_messages(timeout_seconds: int = 60, session_id: str = "", after_event_id: str = "", batch_seconds: float = 3) -> list[dict]:
    """等待新消息；首条到达后最多再收集 3 秒。忽略 SSE 握手和历史重放。

    session_id 可限定会话。传上次返回的 event_id 可接续短暂断线期间的消息（受 WeFlow 重放窗口限制）。
    连接失败、推送未启用时返回工具错误，正常超时返回空列表。
    """
    if timeout_seconds <= 0:
        return []
    c = _cfg()
    headers = {"Authorization": "Bearer " + c.get("httpApiToken", "")}
    if after_event_id:
        headers["Last-Event-ID"] = after_event_id
    got, started = [], int(time.time())
    loop = asyncio.get_running_loop()
    try:
        async with asyncio.timeout(timeout_seconds) as timer:
            async with httpx.AsyncClient(timeout=None, trust_env=False) as client:
                async with client.stream("GET", _base() + "/api/v1/push/messages", headers=headers) as resp:
                    if resp.status_code != 200:
                        body = (await resp.aread()).decode("utf-8", errors="replace")
                        raise RuntimeError(f"WeFlow 消息推送: HTTP {resp.status_code}: {body[:500]}")
                    data, event_id = [], ""
                    async for line in resp.aiter_lines():
                        if line.startswith("data:"):
                            data.append(line[5:].lstrip(" "))
                        elif line.startswith("id:"):
                            event_id = line[3:].strip()
                        elif not line and data:
                            ev = json.loads("\n".join(data))
                            data = []
                            if ev.get("event") != "message.new":
                                continue
                            if session_id and ev.get("sessionId") != session_id:
                                continue
                            if not after_event_id and int(ev.get("timestamp", 0)) < started:
                                continue
                            got.append({"event": ev["event"], "event_id": event_id,
                                        "id": str(ev.get("rawid", "")), "session_id": ev.get("sessionId"),
                                        "group": ev.get("groupName"), "from": ev.get("sourceName"),
                                        "text": ev.get("content"),
                                        "time": datetime.fromtimestamp(int(ev.get("timestamp", 0))).strftime("%Y-%m-%d %H:%M:%S")})
                            if len(got) == 1:
                                if batch_seconds <= 0:
                                    return got
                                timer.reschedule(min(timer.when(), loop.time() + batch_seconds))
                    raise ConnectionError("WeFlow 消息推送连接已断开")
    except TimeoutError:
        return got


def _target(contact_name: str) -> tuple[str, str]:
    sessions = _get("/api/v1/sessions", keyword=contact_name, limit=10000).get("sessions", [])
    matches = [(s["username"], _session_name(s)) for s in sessions
               if s["username"] == contact_name or _session_name(s) == contact_name]
    if not matches:
        contacts = search_contacts(contact_name, limit=10000)
        matches = [(c["session_id"], c["name"]) for c in contacts
                   if c["session_id"] == contact_name or c["name"] == contact_name]
    if len(matches) != 1:
        raise ValueError("找不到唯一的联系人或群，请用 list_sessions/search_contacts 确认名称或 session_id")
    return matches[0]


def _confirm_sent(sid: str, kind: str, value: str, previous_ids: set[str]) -> bool:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        for m in _get("/api/v1/messages", talker=sid, limit=20).get("messages", []):
            identity = str(m.get("serverId") if str(m.get("serverId") or "0") != "0" else m.get("localId"))
            if identity in previous_ids or str(m.get("isSend")) != "1":
                continue
            base = int(m.get("localType", 0)) & 0xFFFFFFFF
            if kind == "text" and base == 1 and (m.get("parsedContent") or m.get("content") or "").strip() == value.strip():
                return True
            raw = m.get("rawContent") or m.get("content") or ""
            if kind == "file" and base == 49 and _tag(raw, "title") == os.path.basename(value):
                return True
            if kind == "image" and base == 3:
                return True
        time.sleep(1)
    return False


def _send(contact_name: str, kind: str, value: str) -> dict:
    from .sender import DesktopSender, SendLock

    sid, name = _target(contact_name)
    with SendLock():
        before = _get("/api/v1/messages", talker=sid, limit=20).get("messages", [])
        previous_ids = {str(m.get("serverId") if str(m.get("serverId") or "0") != "0" else m.get("localId")) for m in before}
        DesktopSender().send(name, kind, value)
        try:
            confirmed = _confirm_sent(sid, kind, value, previous_ids)
        except (requests.RequestException, RuntimeError) as exc:
            return {"sent": True, "confirmed": False, "session_id": sid,
                    "detail": f"已执行发送，WeFlow 回读失败：{exc}；请先核对微信，避免重复发送"}
    return {"sent": True, "confirmed": confirmed, "session_id": sid,
            "detail": "已在目标会话中确认" if confirmed else "已执行发送，目标会话尚未回读到对应消息；请先核对微信，避免重复发送"}


def send_text(contact_name: str, text: str) -> dict:
    """给联系人或群发送文字。支持精确名称或 session_id；发送后回读目标会话确认。

    操作会短暂激活微信；等键鼠空闲后发送，并还原前台窗口和剪贴板。
    """
    if not text:
        raise ValueError("消息正文不能为空")
    return _send(contact_name, "text", text)


def send_file(contact_name: str, file_path: str) -> dict:
    """给联系人或群发送本地文件，返回发送动作与目标会话回读结果。"""
    return _send(contact_name, "file", os.path.abspath(file_path))


def send_image(contact_name: str, image_path: str) -> dict:
    """给联系人或群发送本地图片，返回发送动作与目标会话回读结果。"""
    return _send(contact_name, "image", os.path.abspath(image_path))


def forward_messages(source_session: str, message_ids: list[str], target: str) -> dict:
    """逐条原生转发来源会话最近200条内的文字消息到指定联系人或群，不生成合并记录。"""
    from .sender import SendLock
    from .forwarding import forward_texts

    if not message_ids or len(set(message_ids)) != len(message_ids):
        raise ValueError("请提供不重复的消息ID")
    source_id, source_name = _target(source_session)
    target_id, target_name = _target(target)
    recent = get_messages(source_id, 200)["messages"]
    indexed = {message["id"]: message for message in recent}
    messages = []
    for message_id in message_ids:
        message = indexed.get(message_id)
        if not message or message["type"] != "text":
            raise ValueError("消息必须是来源会话最近200条内的文字消息：" + message_id)
        if sum(row["text"].strip() == message["text"].strip() for row in recent) != 1:
            raise ValueError("消息正文重复，无法唯一定位，未进行转发：" + message_id)
        messages.append(message)
    with SendLock():
        before = get_messages(target_id, 20)["messages"]
        previous_ids = {message["id"] for message in before}

        def confirm(message):
            confirmed = _confirm_sent(target_id, "text", message["text"], previous_ids)
            previous_ids.update(row["id"] for row in get_messages(target_id, 20)["messages"])
            return confirmed

        results = forward_texts(source_name, target_name, messages, confirm, recent)
    return {"source_session": source_id, "target_session": target_id, "messages": results}


