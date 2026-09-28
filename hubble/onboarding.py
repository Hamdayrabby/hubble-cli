"""Interactive wizard for adding a provider (base URL + API key), with verification."""

from typing import Iterable, List, Optional, Tuple
from urllib.parse import urlparse

from rich.markup import escape

from hubble.provider import normalize_base_url
from hubble.providers import KNOWN_EXAMPLES, NAME_RX, ProviderConfig, verify
from hubble.ui import console, pick


def _ask(message: str, default: str = "", password: bool = False) -> Optional[str]:
    from prompt_toolkit import prompt
    try:
        return prompt(message, default=default, is_password=password).strip()
    except (EOFError, KeyboardInterrupt):
        return None


def _confirm(message: str, default: bool = True) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    answer = _ask(f"{message} {hint} ")
    if answer is None:
        return False
    return default if not answer else answer.lower() in ("y", "yes")


def _suggest_name(url: str, taken: Iterable[str]) -> str:
    host = urlparse(url).hostname or "provider"
    parts = [p for p in host.split(".") if p not in ("www", "api", "com", "ai", "io", "xyz", "net", "org", "dev")]
    base = (parts[0] if parts else "provider").lower()
    base = "".join(ch for ch in base if ch.isalnum() or ch in "-_")[:24] or "provider"
    name, n = base, 2
    while name in taken:
        name, n = f"{base}{n}", n + 1
    return name


def add_provider_wizard(taken: Iterable[str], first_run: bool = False) -> Optional[Tuple[ProviderConfig, List[str]]]:
    """Returns (config, model_ids) after a successful check, or None if cancelled."""
    taken = set(taken)
    if first_run:
        console.print("[bold cyan]✦ Welcome to Hubble[/bold cyan]\n"
                      "  No API key is configured yet. Add a provider to start: Claude (Anthropic), or any\n"
                      "  OpenAI-compatible API.\n")
    else:
        console.print("[bold]Add a provider[/bold] [dim](Claude or any OpenAI-compatible API; Esc/Ctrl+C cancels)[/dim]")

    labels = {"anthropic": "Claude (Anthropic API)"}
    items = [((url, kind), labels.get(name, name), url + ("  · native Messages API" if kind == "anthropic" else ""))
             for name, url, kind in KNOWN_EXAMPLES]
    items += [(("custom", "openai"), "Custom URL (OpenAI-compatible)...", "any /v1/chat/completions endpoint"),
              (("custom", "anthropic"), "Custom URL (Anthropic-compatible)...", "a proxy or gateway speaking /v1/messages")]
    choice = pick("Provider", items)
    if choice is None:
        return None
    picked_url, kind = choice
    default_url = "" if picked_url == "custom" else picked_url

    while True:
        raw_url = _ask("Base URL: ", default=default_url)
        if not raw_url:
            return None
        url = normalize_base_url(raw_url)
        local = urlparse(url).hostname in ("localhost", "127.0.0.1", "::1")
        key = _ask("API key" + (" (optional for local servers)" if local else "") + ": ", password=True)
        if key is None:
            return None
        key = key or ("none" if local else "")
        if not key:
            console.print("[red]An API key is required.[/red]")
            continue

        with console.status(f"[dim]Checking {escape(url)} ...[/dim]"):
            ok, message, ids = verify(url, key, kind=kind)
        if ok:
            console.print(f"[green]✔ {escape(message)}[/green]")
            break
        console.print(f"[red]✘ {escape(message)}[/red]")
        if not _confirm("Try again?"):
            return None
        default_url = raw_url

    while True:
        name = _ask("Name for this provider: ", default=_suggest_name(url, taken))
        if name is None:
            return None
        name = name.lower()
        if not NAME_RX.match(name):
            console.print("[red]Use lowercase letters, digits, - or _ (max 31 chars).[/red]")
        elif name in taken:
            console.print(f"[red]'{escape(name)}' already exists.[/red]")
        else:
            break

    if kind == "anthropic":
        # The model list only contains models this key can use: nothing to probe or pay for.
        return ProviderConfig(name, url, key, check_models=False, kind="anthropic"), ids
    console.print(f"[dim]  Checking availability sends one tiny request to each of the {len(ids)} models. "
                  "That is free on most gateways but can cost a little on paid APIs.[/dim]")
    check = _confirm(f"Check which of the {len(ids)} models respond now (runs in the background)?")
    return ProviderConfig(name, url, key, check_models=check, kind=kind), ids
