#!/usr/bin/env python3
"""Start the local Agent Runtime HTTP/SSE server."""

from __future__ import annotations

import argparse
import inspect
import os
import sys
from pathlib import Path

from env_loader import load_dotenv


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--runtime-dir", default=".runtime")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    load_dotenv(repo_root)
    sys.path.insert(0, str(repo_root / "src"))
    os.environ.setdefault("FORGE_RUNTIME_DIR", args.runtime_dir)
    os.environ.setdefault("FORGE_SKILLS_DIR", str(repo_root / "skills"))

    from code_forge.transport.server import create_server

    server = create_server(args.host, args.port)
    print(f"Agent Runtime listening on http://{args.host}:{args.port}")
    print(f"Harness: {type(server.runtime.harness).__name__}")
    print(f"Harness file: {inspect.getsourcefile(type(server.runtime.harness))}")
    model_adapter = getattr(server.runtime.harness, "model_adapter", None)
    print(f"Streaming model: {bool(model_adapter and hasattr(model_adapter, 'complete_stream'))}")
    print("Health endpoint: http://127.0.0.1:8000/health")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
