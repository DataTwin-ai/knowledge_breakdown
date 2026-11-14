"""The prompt-caching marker on its own (no SDK imports), so engines and tests can use it without the LLM clients.
Text before it is the fixed part of a prompt (instructions + taxonomy), text after it the unit's own input."""
CACHE_BREAK = "\n<<<CACHE_BREAK>>>\n"
