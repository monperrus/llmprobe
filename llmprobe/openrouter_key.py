"""Read the OpenRouter API key from environment or system keyring."""

import os
import subprocess
import sys
from pathlib import Path


def ensure_api_key() -> str:
    """Return an OpenRouter API key, reading from env var or system keyring."""
    # 1. Environment variable
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key

    # 2. Keyring
    try:
        result = subprocess.run(
            ["keyring", "get", "login2", "OPENROUTER_API_KEY"],
            capture_output=True, text=True, check=True,
        )
        token = result.stdout.strip()
        if token:
            return token
    except Exception:
        pass

    # 3. Config file
    config_path = Path.home() / ".config" / "openrouter" / "api_key"
    if config_path.exists():
        return config_path.read_text().strip()

    # 4. Fallback: check common env var names
    for var in ("OPENAI_API_KEY", "OPENROUTER_KEY"):
        val = os.environ.get(var)
        if val:
            return val

    raise RuntimeError(
        "No OpenRouter API key found. Set OPENROUTER_API_KEY environment variable "
        "or store it in the system keyring: "
        "keyring set login2 OPENROUTER_API_KEY"
    )
