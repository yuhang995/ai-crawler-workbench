"""Process-local coordination shared by every Codex CLI entry point."""
import threading


# Deterministic crawling may run concurrently, but only one local model process
# should compete for the machine at a time.
AI_SEMAPHORE = threading.Semaphore(1)
