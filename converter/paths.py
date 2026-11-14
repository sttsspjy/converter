"""Where things live, both when running from source and from the installed exe."""
from __future__ import annotations

import os
import sys
from pathlib import Path

FROZEN = getattr(sys, 'frozen', False)

# Bundled read-only resources (ui/, assets/).
RES_DIR = Path(getattr(sys, '_MEIPASS', '')) if FROZEN else Path(__file__).resolve().parent.parent
# The folder the user sees: next to Converter.exe, or the source checkout.
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else RES_DIR

# Roaming settings (small) and local machine data (downloaded tools, logs).
DATA_DIR = Path(os.environ.get('APPDATA') or Path.home()) / 'Converter'
LOCAL_DIR = Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'Converter'
TOOLS_DIR = Path(os.environ.get('CONVERTER_TOOLS_DIR') or LOCAL_DIR / 'tools')
