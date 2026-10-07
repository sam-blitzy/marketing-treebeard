"""Ads Action Engine.

Syncs LinkedIn Ads, Google Ads (through the n8n webhook) and HubSpot, and computes ranked,
evidence-backed recommendations. Runs as the `api` and `worker` processes and the `ads-engine` CLI.
"""

# Importing this package must stay free of side effects: no environment reads, no logging
# configuration and no imports of `ads_engine.*` submodules. The sibling module is named
# `logging.py`, so an eager import here would entangle import order with the standard library's
# `logging`. Entry points, `migrations/env.py` and the tests import the submodules they need
# explicitly, for example `ads_engine.settings`.

from typing import Final

#: Package version. Must equal `[project] version` in `backend/pyproject.toml`.
__version__: Final = "0.1.0"

__all__ = ["__version__"]
