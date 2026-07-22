"""Command-line entrypoint."""

from __future__ import annotations

import os

import uvicorn

from .main import application_factory


def main() -> None:
    uvicorn.run(
        application_factory(),
        host=os.getenv("BAMBU_SERVER_HOST", "127.0.0.1"),
        port=int(os.getenv("BAMBU_SERVER_PORT", "8012")),
        server_header=False,
    )


if __name__ == "__main__":
    main()
