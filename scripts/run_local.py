#!/usr/bin/env python3
"""Run Runtime and Platform together for local verification."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from pathlib import Path

from env_loader import load_dotenv


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-port", type=int, default=8000)
    parser.add_argument("--platform-port", type=int, default=8001)
    parser.add_argument("--runtime-dir", default=".runtime")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root)
    runtime = subprocess.Popen(
        [
            sys.executable,
            str(root / "scripts" / "run_runtime.py"),
            "--port",
            str(args.runtime_port),
            "--runtime-dir",
            args.runtime_dir,
        ],
        env=os.environ.copy(),
    )
    platform = subprocess.Popen(
        [
            sys.executable,
            str(root / "scripts" / "run_platform.py"),
            "--port",
            str(args.platform_port),
        ],
        env=os.environ.copy(),
    )
    print(
        f"Agent Runtime: http://127.0.0.1:{args.runtime_port}\n"
        f"Platform page: http://127.0.0.1:{args.platform_port}"
    )
    try:
        runtime.wait()
    except KeyboardInterrupt:
        pass
    finally:
        for process in (runtime, platform):
            if process.poll() is None:
                process.terminate()
        runtime.wait(timeout=5)
        platform.wait(timeout=5)


if __name__ == "__main__":
    main()
