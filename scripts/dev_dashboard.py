"""Run the dashboard locally from any working directory: http://127.0.0.1:8502.

Changes into the repo root first, so .env, the SQLite request log and reports/ are found.
"""

import os
import sys
from pathlib import Path

os.chdir(Path(__file__).resolve().parents[1])

from streamlit.web import cli  # noqa: E402

sys.argv = [
    "streamlit",
    "run",
    "src/gearbox/dashboard/app.py",
    "--server.port",
    "8502",
    "--server.address",
    "127.0.0.1",
    "--server.headless",
    "true",
]
sys.exit(cli.main())
