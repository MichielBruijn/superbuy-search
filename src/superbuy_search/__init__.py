"""Slimmer zoeken op Superbuy: Firefox-extensie + lokale bridge + MCP-server."""

import os
from pathlib import Path

HOST = "127.0.0.1"
PORT = int(os.environ.get("SUPERBUY_PORT", "8765"))
BRIDGE_URL = f"http://{HOST}:{PORT}"
DATA_DIR = Path(os.environ.get("SUPERBUY_DATA", Path.home() / ".local/share/superbuy-search"))
