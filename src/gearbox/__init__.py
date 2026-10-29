"""Gearbox: an LLM auto-router that picks the cheapest model tier that can do the job."""

import os

# Set before any module imports LiteLLM: use the cost map bundled with the package
# instead of downloading one from GitHub at import. Gearbox prices calls from
# config.yaml anyway, and startup shouldn't need the network.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
# In its default "DEV" mode LiteLLM copies every line of .env into os.environ at import,
# so every API key would leak into the environment of any subprocess. Gearbox reads
# secrets itself (config.Secrets) and passes each key only to the call that needs it.
os.environ.setdefault("LITELLM_MODE", "PRODUCTION")
