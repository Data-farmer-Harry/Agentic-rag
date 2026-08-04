from __future__ import annotations

from importlib.metadata import version
from pathlib import Path

HERMES_AGENT_VERSION = "0.19.0"
TARGET = Path("/usr/local/lib/python3.13/site-packages/agent/error_classifier.py")
MARKER = "# HERMESGRAPH_PATCH: retry wrapped upstream transport failures"
ANCHOR = """    if status_code in {500, 502}:
        # Some OpenAI-compatible gateways return request-validation errors
"""
REPLACEMENT = f"""    if status_code in {{500, 502}}:
        {MARKER}
        # Some compatible gateways wrap an upstream transport failure in an
        # invalid_request_error envelope. The HTTP status and inner transport
        # signal take precedence so Hermes can retry before any tool executes.
        if (
            error_code.lower() == "upstream_error"
            or any(
                pattern in error_msg
                for pattern in (
                    "upstream_error",
                    "bad gateway",
                    "connection reset",
                    "connection closed",
                    "unexpected eof",
                    ": eof",
                )
            )
        ):
            return result_fn(FailoverReason.server_error, retryable=True)

        # Some OpenAI-compatible gateways return request-validation errors
"""


def patch_source(source: str) -> str:
    if MARKER in source:
        return source
    occurrences = source.count(ANCHOR)
    if occurrences != 1:
        raise RuntimeError(
            f"Expected exactly one Hermes error-classifier anchor, found {occurrences}"
        )
    return source.replace(ANCHOR, REPLACEMENT, 1)


def main() -> None:
    installed_version = version("hermes-agent")
    if installed_version != HERMES_AGENT_VERSION:
        raise RuntimeError(
            "HermesGraph error-classifier patch is pinned to "
            f"hermes-agent {HERMES_AGENT_VERSION}, found {installed_version}"
        )
    source = TARGET.read_text(encoding="utf-8")
    patched = patch_source(source)
    TARGET.write_text(patched, encoding="utf-8")


if __name__ == "__main__":
    main()
