"""Process-local coordination shared by every Codex CLI entry point."""
import os
import threading


def _positive_int(name, default):
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(1, value)


# Crawling may run four jobs concurrently. Two repair processes avoid a single
# slow site blocking the whole batch while still bounding local model load.
AI_SEMAPHORE = threading.Semaphore(_positive_int('AI_REPAIR_CONCURRENCY', 2))
