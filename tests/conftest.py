"""Test bootstrap.

custom_components/ha_docker_compose/__init__.py imports `homeassistant`,
which isn't installed in a pure-logic test environment. To unit test the
filesystem-discovery and compose-CLI-wrapper layers in isolation (no HA
runtime required), register a stub package pointing at the component
directory *before* anything imports `ha_docker_compose.*`, so Python
resolves submodules without ever executing the real `__init__.py`.

Tests that need `config_flow.py` or `__init__.py` themselves (entity-level
tests, from build-order step 2 onward) should use
pytest-homeassistant-custom-component instead of relying on this stub.
"""
import sys
import types
from pathlib import Path

_COMPONENT_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "ha_docker_compose"

if "ha_docker_compose" not in sys.modules:
    _pkg = types.ModuleType("ha_docker_compose")
    _pkg.__path__ = [str(_COMPONENT_DIR)]
    sys.modules["ha_docker_compose"] = _pkg
