import sys
import types
from pathlib import Path

import hubble.setup_path as sp


def test_already_on_path_does_nothing(monkeypatch, capsys):
    monkeypatch.setattr(sp.shutil, "which", lambda name: "/usr/bin/hubble")
    monkeypatch.delenv("HUBBLE_FORCE_PATH_SETUP", raising=False)
    assert sp.add_to_path() == 0
    assert "already on PATH" in capsys.readouterr().out


def test_missing_launcher_explains(monkeypatch, capsys):
    monkeypatch.setattr(sp.shutil, "which", lambda name: None)
    monkeypatch.setattr(sp, "launcher_dir", lambda: None)
    assert sp.add_to_path() == 1
    assert "pip install --user hubble-cli" in capsys.readouterr().out


class FakeKey:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_winreg(store):
    m = types.SimpleNamespace(HKEY_CURRENT_USER=1, KEY_READ=1, KEY_WRITE=2, REG_SZ=1, REG_EXPAND_SZ=2)
    m.OpenKey = lambda *a: FakeKey()

    def query(key, name):
        if name not in store:
            raise FileNotFoundError
        return store[name], 2
    m.QueryValueEx = query
    m.SetValueEx = lambda key, name, _r, kind, value: store.__setitem__(name, value)
    return m


def test_windows_user_path_appended_once(monkeypatch, tmp_path):
    store = {"Path": r"C:\Windows;C:\Tools"}
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg(store))
    fake_ctypes = types.SimpleNamespace(
        windll=types.SimpleNamespace(user32=types.SimpleNamespace(SendMessageTimeoutW=lambda *a: 1)),
        byref=lambda x: x, c_ulong=lambda: 0)
    monkeypatch.setitem(sys.modules, "ctypes", fake_ctypes)
    folder = tmp_path / "Scripts"
    assert sp._add_windows(folder) == "added to your user PATH"
    assert store["Path"].endswith(";" + str(folder)) and store["Path"].startswith(r"C:\Windows;C:\Tools")
    assert sp._add_windows(folder) == "already on your user PATH"
    assert store["Path"].count(str(folder)) == 1


def test_posix_profile_line(monkeypatch, tmp_path):
    monkeypatch.setattr(sp.Path, "home", lambda: tmp_path)
    monkeypatch.setenv("SHELL", "/bin/zsh")
    folder = Path("/home/me/.local/bin")
    assert "added to" in sp._add_posix(folder)
    rc = (tmp_path / ".zshrc").read_text()
    assert f'export PATH="{folder}:$PATH"' in rc
    assert "already in" in sp._add_posix(folder)


def test_launcher_dir_finds_the_script(monkeypatch, tmp_path):
    name = "hubble.exe" if sp.os.name == "nt" else "hubble"
    (tmp_path / name).write_text("", encoding="utf-8")
    monkeypatch.setattr(sp, "scripts_dirs", lambda: [tmp_path / "nope", tmp_path])
    assert sp.launcher_dir() == tmp_path


def test_path_hint_only_when_needed(monkeypatch, tmp_path):
    monkeypatch.setattr(sp, "launcher_dir", lambda: tmp_path)
    monkeypatch.setattr(sp.shutil, "which", lambda name: None)
    monkeypatch.setenv("PATH", "/usr/bin")
    assert "--add-to-path" in sp.path_hint()
    monkeypatch.setenv("PATH", str(tmp_path))
    assert sp.path_hint() is None
