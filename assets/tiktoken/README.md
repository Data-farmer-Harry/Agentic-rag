# Vendored tiktoken cache

HermesGraph containers must not download tokenizer data during CLI or service startup. This directory contains the
cache object used by `tiktoken.get_encoding("o200k_base")`.

| Field | Value |
| --- | --- |
| Upstream URL | `https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken` |
| tiktoken cache key | `fb374d419588a4632f3f557e76b4b70aebbca790` |
| SHA-256 | `446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d` |
| Size | `3613922` bytes |

The filename is the SHA-1 of the upstream URL, as required by tiktoken's cache loader. The SHA-256 is the expected
hash embedded in the installed tiktoken encoding registry. Docker sets `TIKTOKEN_CACHE_DIR` to this directory and
calls `get_encoding("o200k_base")` during the image build, so a missing or corrupt asset fails the build instead of
making a runtime network request.

Do not replace this file without updating the pinned tiktoken dependency, verifying the registry's expected hash,
and running the offline Docker check.
