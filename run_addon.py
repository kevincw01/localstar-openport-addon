#!/usr/bin/env python3
"""Run the ingress certificate manager and supervise the Skyport process."""

import json
import sys

from aiohttp import web

from addon_config import command_from_options, ssl_file, validate_options
from management import create_application


def main(options_path="/data/options.json"):
    try:
        with open(options_path, encoding="utf-8") as stream:
            options = json.load(stream)
        validate_options(options)
    except (OSError, ValueError) as exc:
        print(f"localstar-openport configuration error: {exc}", file=sys.stderr, flush=True)
        return 1
    web.run_app(create_application(options), host="0.0.0.0", port=8099,
                access_log=None, print=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
