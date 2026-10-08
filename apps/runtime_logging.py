"""UTC diagnostics on stderr; third-party HTTP request logging stays disabled by default."""

import logging
import time


def configureLogging():
    handler = logging.StreamHandler()
    formatter = logging.Formatter(
        "%(asctime)sZ %(levelname)s %(name)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"
    )
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    # Entry points own their process logging. Replace ambient basicConfig state
    # so UTC formatting and stdout/stderr separation do not depend on imports.
    logging.basicConfig(level=logging.WARNING, handlers=[handler], force=True)
    # Do not enable HTTPX/Web3 INFO/DEBUG logs, which may contain credential-bearing URLs.
    for namespace in ("apps", "modules"):
        logging.getLogger(namespace).setLevel(logging.INFO)
