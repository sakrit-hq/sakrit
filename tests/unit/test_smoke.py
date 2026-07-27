# SPDX-License-Identifier: Apache-2.0
"""Smoke tests: the package imports and its shape is intact."""

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
