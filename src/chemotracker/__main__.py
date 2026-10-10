"""`python -m chemotracker` / `chemotracker` entry point."""

from __future__ import annotations

import logging
import os

import uvicorn


def main() -> None:
    logging.basicConfig(level=os.environ.get("CHEMO_LOG_LEVEL", "INFO"), format="%(asctime)s %(name)s %(levelname)s %(message)s")
    uvicorn.run(
        "chemotracker.web:create_app",
        factory=True,
        host=os.environ.get("CHEMO_HOST", "127.0.0.1"),
        port=int(os.environ.get("CHEMO_PORT", "8000")),
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("CHEMO_FORWARDED_ALLOW_IPS", "127.0.0.1"),
    )


if __name__ == "__main__":
    main()
