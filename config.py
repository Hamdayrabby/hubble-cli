"""
Configuration for AIHub API testing and CLI.
Loads sensitive credentials securely from .env file or environment variables.
"""
import os
import sys
from pathlib import Path


def load_env_file(env_path: Path = None):
    """
    Lightweight zero-dependency .env loader.
    Reads key-value pairs and sets them into os.environ if not already present.
    """
    if env_path is None:
        env_path = Path(__file__).resolve().parent / ".env"

    if env_path.exists() and env_path.is_file():
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    # Skip empty lines and comments
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        key, val = line.split("=", 1)
                        key = key.strip()
                        val = val.strip().strip("'\"")
                        if key and key not in os.environ:
                            os.environ[key] = val
        except Exception as e:
            print(f"[Warning] Could not parse .env file: {e}", file=sys.stderr)


# Automatically load from .env
load_env_file()

# Base URL (non-sensitive endpoint default)
DEFAULT_BASE_URL = "https://aihub.071129.xyz/v1"
BASE_URL = os.environ.get("HUBBLE_BASE_URL", DEFAULT_BASE_URL).rstrip("/")

# API Key (loaded strictly from environment or .env, never hardcoded)
API_KEY = os.environ.get("HUBBLE_API_KEY", "")

# Model registry lives in the hubble package so both CLIs share one list
from hubble.models import DEFAULT_MODEL, MODEL_CATEGORIES, RELIABLE_MODELS  # noqa: E402,F401

DEFAULT_TIMEOUT = 15.0
DEFAULT_MAX_TOKENS = 2048
DEFAULT_TEMPERATURE = 0.7


def ensure_api_key(api_key: str = None) -> str:
    """Ensure that an API key is available; otherwise raise error with instructions."""
    key = api_key or API_KEY
    if not key:
        print(
            "\n[Error] Missing HUBBLE_API_KEY!\n"
            "Please provide your API key in the .env file:\n"
            "  HUBBLE_API_KEY=your_key_here\n"
            "Or export it as an environment variable:\n"
            "  set HUBBLE_API_KEY=your_key_here (Windows cmd)\n"
            "  $env:HUBBLE_API_KEY=\"your_key_here\" (Powershell)\n"
            "  export HUBBLE_API_KEY=\"your_key_here\" (Linux/macOS)\n",
            file=sys.stderr
        )
        sys.exit(1)
    return key
