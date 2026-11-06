from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent
APP_FILE = PROJECT_DIR / "app.py"


def main() -> int:
    parser = argparse.ArgumentParser(description="Launch the local eMAG AutoBuy dashboard.")
    parser.add_argument("--port", type=int, default=8501, help="Local dashboard port.")
    args = parser.parse_args()
    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(APP_FILE),
        "--server.address=127.0.0.1",
        f"--server.port={args.port}",
        "--server.headless=false",
        "--browser.gatherUsageStats=false",
    ]
    try:
        return subprocess.call(command, cwd=PROJECT_DIR)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
