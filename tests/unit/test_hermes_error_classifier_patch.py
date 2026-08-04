from pathlib import Path
from runpy import run_path


def _patch_module() -> dict[str, object]:
    path = (
        Path(__file__).parents[2]
        / "deploy"
        / "hermes"
        / "patch_error_classifier.py"
    )
    return run_path(str(path))


def test_patch_prioritizes_wrapped_upstream_failure_before_validation() -> None:
    module = _patch_module()
    patch_source = module["patch_source"]
    anchor = module["ANCHOR"]
    marker = module["MARKER"]
    source = f"before\n{anchor}        pass\nafter\n"

    patched = patch_source(source)

    assert marker in patched
    assert patched.index(marker) < patched.index(
        "Some OpenAI-compatible gateways return request-validation errors"
    )
    assert '"upstream_error"' in patched
    assert '": eof"' in patched
    assert patch_source(patched) == patched
