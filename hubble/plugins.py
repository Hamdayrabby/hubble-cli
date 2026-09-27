"""Plugins: one installable folder that bundles commands, skills, agents, hooks and MCP servers.

Layout (everything optional except plugin.json):

    my-plugin/
      plugin.json        {"name": "my-plugin", "version": "1.0.0", "description": "...",
                          "hooks": {...same shape as settings.hooks...},
                          "mcp_servers": {...same shape as settings.mcp_servers...}}
      commands/*.md      slash commands, like .hubble/commands
      skills/<n>/SKILL.md or skills/*.md
      agents/*.md        custom sub-agents, like .hubble/agents

`${PLUGIN_DIR}` in a hook command or MCP command/env is replaced by the plugin's folder, so a
plugin can ship its own scripts.

Installed under ~/.hubble/plugins/<name>/ (user, every project) or <project>/.hubble/plugins/
(project). Hooks and MCP servers from a project plugin only load in a trusted folder, like the
same keys in .hubble/settings.json. `disabled_plugins` in settings turns one off without removing it.
"""

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hubble.settings import HOME_DIR

NAME_RX = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
MANIFEST = "plugin.json"


class PluginError(Exception):
    pass


def _bases(root: Path) -> List[Tuple[Path, str]]:
    return [(HOME_DIR / "plugins", "user"), (root / ".hubble" / "plugins", "project")]


def read_manifest(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads((path / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise PluginError(f"{path / MANIFEST}: {e}")
    if not isinstance(data, dict):
        raise PluginError(f"{path / MANIFEST} must contain a JSON object")
    name = str(data.get("name") or path.name).lower()
    if not NAME_RX.match(name):
        raise PluginError(f"invalid plugin name '{name}' (lowercase letters, digits, - or _)")
    data["name"] = name
    return data


def _disabled() -> set:
    try:
        data = json.loads((HOME_DIR / "settings.json").read_text(encoding="utf-8"))
        return set(data.get("disabled_plugins") or [])
    except (OSError, ValueError):
        return set()


def installed_plugins(root: Path, include_disabled: bool = False) -> List[Dict[str, Any]]:
    """[{name, version, description, path, scope, enabled, manifest}], project after user."""
    disabled = _disabled()
    out: Dict[str, Dict[str, Any]] = {}
    for base, scope in _bases(root):
        if not base.is_dir():
            continue
        for d in sorted(p for p in base.iterdir() if (p / MANIFEST).is_file()):
            try:
                m = read_manifest(d)
            except PluginError:
                continue
            enabled = m["name"] not in disabled
            if enabled or include_disabled:
                out[m["name"]] = {"name": m["name"], "version": str(m.get("version", "")),
                                  "description": str(m.get("description", "")), "path": d, "scope": scope,
                                  "enabled": enabled, "manifest": m}
    return list(out.values())


def plugin_dirs(root: Path) -> List[Tuple[str, Path]]:
    return [(p["name"], p["path"]) for p in installed_plugins(root)]


def _expand(value: Any, plugin_dir: Path) -> Any:
    if isinstance(value, str):
        return value.replace("${PLUGIN_DIR}", str(plugin_dir))
    if isinstance(value, list):
        return [_expand(v, plugin_dir) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v, plugin_dir) for k, v in value.items()}
    return value


def plugin_settings(root: Path, trusted: bool) -> Tuple[Dict[str, list], Dict[str, dict]]:
    """(hooks, mcp_servers) contributed by enabled plugins. MCP server names are prefixed
    with the plugin name so two plugins cannot collide."""
    hooks: Dict[str, list] = {}
    servers: Dict[str, dict] = {}
    for p in installed_plugins(root):
        if p["scope"] == "project" and not trusted:
            continue
        m = p["manifest"]
        for event, entries in (m.get("hooks") or {}).items():
            if isinstance(entries, list):
                hooks.setdefault(event, []).extend(_expand(e, p["path"]) for e in entries if isinstance(e, dict))
        for name, entry in (m.get("mcp_servers") or {}).items():
            if isinstance(entry, dict):
                servers[f"{p['name']}-{name}"] = _expand(entry, p["path"])
    return hooks, servers


def install(source: str, root: Path, scope: str = "user") -> Dict[str, Any]:
    """Install from a local folder or a git URL. Replaces an existing plugin of the same name."""
    base = (HOME_DIR if scope == "user" else root / ".hubble") / "plugins"
    tmp: Optional[str] = None
    src = Path(source).expanduser()
    try:
        if not src.is_dir():
            if not re.match(r"^(https?://|git@|ssh://)", source):
                raise PluginError(f"not a folder or git URL: {source}")
            tmp = tempfile.mkdtemp(prefix="hubble-plugin-")
            proc = subprocess.run(["git", "clone", "--depth", "1", source, tmp], capture_output=True, text=True)
            if proc.returncode != 0:
                raise PluginError(f"git clone failed: {(proc.stderr or proc.stdout).strip()[:300]}")
            src = Path(tmp)
        manifest = read_manifest(src)
        dest = base / manifest["name"]
        if dest.resolve() == src.resolve():
            raise PluginError("source is already the installed copy")
        if dest.exists():
            shutil.rmtree(dest)
        base.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".git"))
        return {"name": manifest["name"], "path": dest, "version": str(manifest.get("version", "")),
                "hooks": sorted((manifest.get("hooks") or {}).keys()),
                "mcp_servers": sorted((manifest.get("mcp_servers") or {}).keys())}
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def remove(name: str, root: Path) -> bool:
    for base, _ in reversed(_bases(root)):
        d = base / name
        if (d / MANIFEST).is_file():
            shutil.rmtree(d)
            return True
    return False


def set_enabled(name: str, enabled: bool):
    from hubble.settings import save_user_setting
    disabled = _disabled()
    (disabled.discard if enabled else disabled.add)(name)
    save_user_setting("disabled_plugins", sorted(disabled))
