"""Open Computer Use Agent: a fallback desktop control extension for Biorouter.

A small standard library MCP server (the wrapper) that sits between Biorouter and the Open
Computer Use helper. It pins the helper's tool contract, scrubs the helper's environment,
refuses what Biorouter Copilot refuses, asks the person once per chat, and owns the helper's
lifetime. It runs on Python 3.9 and later and has no runtime dependencies.
"""

__version__ = "0.1.0"

DISPLAY_NAME = "Open Computer Use Agent"
APP_NAME = "Open Computer Use Agent"
BUNDLE_IDENTIFIER = "edu.ucsf.baranzinilab.opencomputeruseagent"
SERVER_NAME = "opencomputeruseagent"

# The engine this wrapper is written and tested against. A payload that names anything else is
# refused before it runs (scripts/version.py keeps these in step with the vendored pin).
UPSTREAM_VERSION = "1.1.5"
UPSTREAM_COMMIT = "8ec5cdbce8e76d20dd59bc867343079098ef6613"
PATCH_REVISION = "ocua-2"
MINIMUM_BIOROUTER = "1.94.6"
