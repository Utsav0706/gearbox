"""Run the API locally from any working directory: http://127.0.0.1:8002.

Changes into the repo root first, so config.yaml, .env and the SQLite request log are
found. Run `py -m alembic upgrade head` once before the first start.
"""

import os
from pathlib import Path

os.chdir(Path(__file__).resolve().parents[1])

from gearbox.service.app import main  # noqa: E402

main()
