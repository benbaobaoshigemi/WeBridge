"""WeBridge stdio MCP 入口。"""

from functools import wraps
import argparse
import inspect
import os
import subprocess

import httpx
import requests
import pywintypes
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import api

def _expose(mcp, function):
    errors = (RuntimeError, ValueError, OSError, requests.RequestException, httpx.HTTPError,
              pywintypes.error, subprocess.SubprocessError)
    if inspect.iscoroutinefunction(function):
        @wraps(function)
        async def call(**kwargs):
            try:
                return await function(**kwargs)
            except errors as exc:
                raise ToolError(str(exc)) from exc
    else:
        @wraps(function)
        def call(**kwargs):
            try:
                return function(**kwargs)
            except errors as exc:
                raise ToolError(str(exc)) from exc
    mcp.tool()(call)


READ_TOOLS = (
    api.list_sessions,
    api.search_contacts,
    api.get_messages,
    api.list_files,
    api.search_all_messages,
    api.export_chat,
    api.get_group_members,
    api.wait_new_messages,
)
WRITE_TOOLS = (api.send_text, api.send_file, api.send_image, api.forward_messages)


def create_server(read_only=False):
    mcp = MCPServer("WeBridge")
    for tool in READ_TOOLS + (() if read_only else WRITE_TOOLS):
        _expose(mcp, tool)
    return mcp


def main():
    parser = argparse.ArgumentParser(description="WeBridge 微信本地 MCP 服务")
    parser.add_argument("--version", action="version", version="WeBridge 0.1.0")
    parser.add_argument("--config", help="WeFlow 配置文件路径，默认读取 WEFLOW_CONFIG 或系统位置")
    parser.add_argument("--state-dir", help="导出与桌面运行文件目录")
    parser.add_argument("--read-only", action="store_true", help="仅提供读取工具，不注册发送与转发工具")
    args = parser.parse_args()
    if args.config:
        api.WEFLOW_CONFIG = os.path.abspath(args.config)
    if args.state_dir:
        api.STATE_DIR = os.path.abspath(args.state_dir)
        os.environ["WEBRIDGE_STATE_DIR"] = api.STATE_DIR
    create_server(args.read_only).run()
