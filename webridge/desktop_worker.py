"""Run WeChat desktop operations in a separate process."""

import json
import os
from pathlib import Path
import sys


def runtime_directory():
    root = Path(os.environ.get("WEBRIDGE_STATE_DIR") or Path(os.environ["LOCALAPPDATA"]) / "WeBridge")
    directory = root / "desktop"
    directory.mkdir(parents=True, exist_ok=True)
    return str(directory)


def main():
    output = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8")
    sys.stdout = sys.stderr
    try:
        request = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        if request.get("operation", "forward") == "send":
            from .sender import DesktopSender

            DesktopSender()._send_native(request["contact"], request["kind"], request["value"])
            result = {"sent": True}
        else:
            from .api import forward_messages

            result = forward_messages(request["source_session"], request["message_ids"], request["target"])
    except Exception as exc:
        result = {"error": str(exc)}
    output.write(json.dumps(result, ensure_ascii=False) + "\n")
    output.flush()


if __name__ == "__main__":
    main()
