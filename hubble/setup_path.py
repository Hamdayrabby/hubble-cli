"""`python -m hubble --add-to-path`: put the folder pip installed `hubble` into on the user's PATH.

pip prints "The script hubble.exe is installed in '...Scripts' which is not on PATH" and moves
on, which leaves new users with "hubble is not recognized". This fixes it once, for the user
only (no admin rights): the user PATH in the registry on Windows, the shell profile elsewhere.
"""

import os
import shutil
import sys
import sysconfig
from pathlib import Path
from typing import List, Optional


def scripts_dirs() -> List[Path]:
    """Folders where pip may have put the hubble launcher, most likely first."""
    out: List[Path] = []
    for scheme in (None, f"{os.name}_user", "nt_user", "posix_user", "osx_framework_user"):
        try:
            p = sysconfig.get_path("scripts", scheme) if scheme else sysconfig.get_path("scripts")
        except KeyError:
            continue
        if p and Path(p) not in out:
            out.append(Path(p))
    exe_dir = Path(sys.executable).parent / ("Scripts" if os.name == "nt" else "")
    if exe_dir not in out:
        out.append(exe_dir)
    return out


def launcher_dir() -> Optional[Path]:
    name = "hubble.exe" if os.name == "nt" else "hubble"
    return next((d for d in scripts_dirs() if (d / name).is_file()), None)


def _on_path(folder: Path) -> bool:
    norm = lambda p: os.path.normcase(os.path.normpath(p))  # noqa: E731
    return any(norm(p) == norm(str(folder)) for p in os.environ.get("PATH", "").split(os.pathsep) if p)


def _add_windows(folder: Path) -> str:
    import ctypes
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
        try:
            current, kind = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            current, kind = "", winreg.REG_EXPAND_SZ
        parts = [p for p in current.split(";") if p]
        if any(os.path.normcase(p.rstrip("\\")) == os.path.normcase(str(folder)) for p in parts):
            return "already on your user PATH"
        winreg.SetValueEx(key, "Path", 0, kind if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ)
                          else winreg.REG_EXPAND_SZ, ";".join(parts + [str(folder)]))
    # Tell Explorer (and terminals started from it) that the environment changed.
    HWND_BROADCAST, WM_SETTINGCHANGE, SMTO_ABORTIFHUNG = 0xFFFF, 0x001A, 0x0002
    ctypes.windll.user32.SendMessageTimeoutW(HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment",
                                             SMTO_ABORTIFHUNG, 5000, ctypes.byref(ctypes.c_ulong()))
    return "added to your user PATH"


def _add_posix(folder: Path) -> str:
    shell = os.path.basename(os.environ.get("SHELL", ""))
    rc = Path.home() / {"zsh": ".zshrc", "fish": ".config/fish/config.fish"}.get(shell, ".bashrc")
    line = (f"fish_add_path {folder}" if shell == "fish" else f'export PATH="{folder}:$PATH"')
    existing = rc.read_text(encoding="utf-8") if rc.exists() else ""
    if str(folder) in existing:
        return f"already in {rc}"
    rc.parent.mkdir(parents=True, exist_ok=True)
    with open(rc, "a", encoding="utf-8") as f:
        f.write(f"\n# Added by hubble --add-to-path\n{line}\n")
    return f"added to {rc}"


def add_to_path() -> int:
    if shutil.which("hubble") and not os.environ.get("HUBBLE_FORCE_PATH_SETUP"):
        print(f"hubble is already on PATH: {shutil.which('hubble')}")
        return 0
    folder = launcher_dir()
    if folder is None:
        print("Could not find the hubble launcher. Reinstall with: python -m pip install --user hubble-cli")
        return 1
    try:
        result = _add_windows(folder) if os.name == "nt" else _add_posix(folder)
    except OSError as e:
        print(f"Could not update PATH automatically ({e}). Add this folder to PATH yourself:\n  {folder}")
        return 1
    print(f"{folder}\n  {result}.")
    print("Open a NEW terminal window, then run: hubble" if os.name == "nt" else
          "Open a new terminal (or run: source your shell profile), then run: hubble")
    return 0


def path_hint() -> Optional[str]:
    """A one-line tip when hubble was started some other way and its launcher is not on PATH."""
    folder = launcher_dir()
    if folder and not _on_path(folder) and not shutil.which("hubble"):
        return "Tip: `hubble` is not on your PATH. Run `python -m hubble --add-to-path` once to fix that."
    return None
