import os
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "opensearch-skills"
    / "cloud"
    / "instaclustr-setup"
    / "scripts"
)
sys.path.insert(0, str(_SCRIPTS_DIR))

pytestmark = pytest.mark.instaclustr_live

_HAS_CREDS = bool(
    os.environ.get("INSTACLUSTR_API_USERNAME") and os.environ.get("INSTACLUSTR_API_KEY")
)


@pytest.mark.skipif(not _HAS_CREDS, reason="Instaclustr credentials not set")
def test_live_list_versions():
    from instaclustr_ops import client_from_env

    client = client_from_env()
    data = client.list_versions()
    assert data is not None


@pytest.mark.skipif(not _HAS_CREDS, reason="Instaclustr credentials not set")
def test_live_compatible_sizes():
    from instaclustr_ops import client_from_env

    client = client_from_env()
    data = client.compatible_sizes()
    assert data is not None


@pytest.mark.skipif(
    True,
    reason="Live create is manual: uv run python ... create --yes with a skill-test- name prefix",
)
def test_live_create_manual_only():
    pass
