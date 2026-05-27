"""Pytest wrapper around the script-style phase tests so pytest collects them too."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

PHASE_TESTS = [
    "test_smoke.py",
    "test_phase2.py",
    "test_phase3.py",
    "test_phase4.py",
    "test_phase5.py",
    "test_phase6.py",
    "test_phase7.py",
    "test_phase8.py",
]


@pytest.mark.parametrize("script", PHASE_TESTS)
def test_phase_script(script):
    here = Path(__file__).parent
    spec = importlib.util.spec_from_file_location(script.replace(".", "_"), here / script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rc = mod.main()
    assert rc == 0, f"{script} reported {rc} failures"
