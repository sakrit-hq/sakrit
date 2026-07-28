# SPDX-License-Identifier: Apache-2.0
"""Smoke tests: the package imports and its shape is intact."""

import subprocess
import sys

import sakrit
from sakrit.core.errors import SakritError
from sakrit.spec import v1


def test_version_is_exposed() -> None:
    assert isinstance(sakrit.__version__, str)
    assert sakrit.__version__


def test_core_error_base_is_importable() -> None:
    assert issubclass(SakritError, Exception)


def test_spec_version() -> None:
    assert v1.SED_MAJOR == 1


def test_stdlib_reexports_do_not_leak_at_top_level() -> None:
    # G-8: Callable/TypeVar are construction helpers for `safe`, not public surface — they must
    # not be tab-completable as `sakrit.Callable` / `sakrit.TypeVar` (STABILITY promise 3).
    assert not hasattr(sakrit, "Callable")
    assert not hasattr(sakrit, "TypeVar")
    assert hasattr(sakrit, "safe")  # the reason they existed is still exported


def test_python_m_sakrit_runs() -> None:
    # G-8: `python -m sakrit` (a common reflex) works via __main__.py, not just the console script.
    out = subprocess.run(
        [sys.executable, "-m", "sakrit", "--version"], capture_output=True, text=True
    )
    assert out.returncode == 0
    assert "sakrit" in out.stdout
