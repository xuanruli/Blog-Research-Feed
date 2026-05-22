"""One-shot: upload the container .env to the Files API and print its id.

Run this manually whenever the secrets change (e.g. you rotate a key).
Persist the printed id as the ``ENV_FILE_ID`` GitHub Actions variable so
the daily cron runner can mount it directly instead of re-uploading on
every run.

Usage:

    # Read keys from host environment (PASSTHROUGH_KEYS in cron.daily):
    ANTHROPIC_API_KEY=... FIRECRAWL_API_KEY=... X_BEARER_TOKEN=... \\
    OPENAI_API_KEY=... SLACK_WEBHOOK_URL=... python -m scripts.upload_env

    # Or load them from a local dotenv file:
    python -m scripts.upload_env --from-file .env.production
"""
from __future__ import annotations

import argparse
import os
import sys

from cron.daily import (
    FILES_BETAS,
    PASSTHROUGH_KEYS,
    _build_env_payload,
    _upload_env_file,
)


def _load_dotenv(path: str) -> None:
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if (value.startswith('"') and value.endswith('"')) or (
                value.startswith("'") and value.endswith("'")
            ):
                value = value[1:-1]
            os.environ.setdefault(key, value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--from-file",
        help="Path to a local .env file. Keys not already in os.environ are loaded from it.",
    )
    args = parser.parse_args()

    if args.from_file:
        _load_dotenv(args.from_file)

    missing = [k for k in PASSTHROUGH_KEYS if not os.environ.get(k)]
    if missing:
        print(f"warning: missing keys (will be absent from upload): {missing}", file=sys.stderr)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("error: ANTHROPIC_API_KEY required to call the Files API", file=sys.stderr)
        return 2

    payload = _build_env_payload()
    print(f"payload: {len(payload)} bytes, {len(PASSTHROUGH_KEYS) - len(missing)} keys", file=sys.stderr)

    from anthropic import Anthropic

    client = Anthropic()
    uploaded = _upload_env_file(client, payload)
    print(uploaded.id)
    print(
        f"Persist this as the ENV_FILE_ID GitHub Actions variable. "
        f"Betas used: {FILES_BETAS}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
