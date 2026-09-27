"""Image input: attach screenshots and image files to a message (OpenAI-style image_url parts).

Images reach the model two ways: `@path/to/shot.png` in a message, or pasting one from the
clipboard (Ctrl+V / Alt+V in the prompt), which shows as [Image #N] until the message is sent.
The model must support vision; one that does not usually answers with an HTTP 400.
"""

import base64
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
               ".webp": "image/webp", ".bmp": "image/bmp"}
MAX_IMAGE_BYTES = 8 * 1024 * 1024
IMAGE_TOKEN_ESTIMATE = 1500  # rough per-image cost, for the context meter only


class ImageError(Exception):
    pass


def is_image(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_TYPES


def data_url(path: Path) -> str:
    size = path.stat().st_size
    if size > MAX_IMAGE_BYTES:
        raise ImageError(f"{path.name} is {size / 1e6:.1f} MB; the limit is {MAX_IMAGE_BYTES // 1_000_000} MB")
    mime = IMAGE_TYPES.get(path.suffix.lower(), "image/png")
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def user_content(text: str, images: Optional[List[str]]) -> Union[str, List[Dict[str, Any]]]:
    if not images:
        return text
    return [{"type": "text", "text": text}] + [{"type": "image_url", "image_url": {"url": u}} for u in images]


def content_text(content: Any) -> str:
    """Text of a message's content, whether a plain string or a list of parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict) and p.get("type") == "text":
                parts.append(p.get("text", ""))
            elif isinstance(p, dict) and p.get("type") == "image_url":
                parts.append("[image]")
        return "\n".join(parts)
    return ""


def image_count(content: Any) -> int:
    return sum(1 for p in content if isinstance(p, dict) and p.get("type") == "image_url") \
        if isinstance(content, list) else 0


def grab_clipboard_image() -> Optional[Path]:
    """Save the clipboard's image (if it holds one) to a temp PNG and return its path."""
    fd, name = tempfile.mkstemp(prefix="hubble-clip-", suffix=".png")
    os.close(fd)
    out = Path(name)
    try:
        if sys.platform == "win32":
            script = ("Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing; "
                      "$i = [System.Windows.Forms.Clipboard]::GetImage(); "
                      f"if ($i) {{ $i.Save('{out}', [System.Drawing.Imaging.ImageFormat]::Png); 'ok' }}")
            proc = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", script],
                                  capture_output=True, text=True, timeout=15)
            ok = "ok" in (proc.stdout or "")
        elif sys.platform == "darwin":
            script = ["-e", "try", "-e", "set d to (the clipboard as «class PNGf»)",
                      "-e", f'set f to open for access POSIX file "{out}" with write permission',
                      "-e", "write d to f", "-e", "close access f", "-e", "return \"ok\"",
                      "-e", "end try"]
            proc = subprocess.run(["osascript"] + script, capture_output=True, text=True, timeout=15)
            ok = "ok" in (proc.stdout or "")
        else:
            ok = False
            for cmd in (["wl-paste", "--type", "image/png"], ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"]):
                try:
                    proc = subprocess.run(cmd, capture_output=True, timeout=15)
                except (OSError, subprocess.TimeoutExpired):
                    continue
                if proc.returncode == 0 and proc.stdout.startswith(b"\x89PNG"):
                    out.write_bytes(proc.stdout)
                    ok = True
                    break
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    if ok and out.is_file() and out.stat().st_size > 0:
        return out
    out.unlink(missing_ok=True)
    return None
