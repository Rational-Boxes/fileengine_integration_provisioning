# Copyright (C) 2026 James Hickman
# AGPL-3.0-or-later — see package header.
"""Locate + import the reused FileEngine Python client (``fileengine``).

Prefers an installed package; else falls back to the sibling ``python_interface``
checkout (override with FILEENGINE_PYTHON_CLIENT). Same bootstrap as folder_actions /
CSAI. Imported lazily by ``core`` so config/auth/blueprint import without the gRPC
stack present."""
import os
import sys


def _ensure_on_path() -> None:
    try:
        import fileengine  # noqa: F401
        return
    except ImportError:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.environ.get("FILEENGINE_PYTHON_CLIENT", ""),
        # commercial_provisioning/src/provisioning_service/ -> ../../../python_interface
        os.path.join(here, "..", "..", "..", "python_interface"),
        os.path.join(here, "..", "..", "..", "..", "python_interface"),
    ]
    for c in candidates:
        if c and os.path.isfile(os.path.join(c, "fileengine", "__init__.py")):
            sys.path.insert(0, os.path.abspath(c))
            return
    raise ImportError(
        "Could not import 'fileengine'. Install ../python_interface "
        "(`pip install ../python_interface`) or set FILEENGINE_PYTHON_CLIENT.")


def load():
    """Return the ManagedFiles class + common exceptions, importing lazily."""
    _ensure_on_path()
    from fileengine import (  # noqa: E402
        ManagedFiles, FileEngineError, NotFoundError,
    )
    return ManagedFiles, FileEngineError, NotFoundError
