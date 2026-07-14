from __future__ import annotations

import argparse

from .server import serve


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the DeReel local studio")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()
    from .config import Settings, settings

    configured = Settings(host=args.host or settings.host, port=args.port or settings.port)
    serve(configured)
