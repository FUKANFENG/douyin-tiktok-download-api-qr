import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "init_qr", Path(__file__).parents[2] / "scripts" / "init_qr.py"
)
assert SPEC and SPEC.loader
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)


def test_setup_preserves_existing_configuration(tmp_path):
    target = tmp_path / ".env.qr"
    assert setup.initialize(target)
    before = target.read_bytes()
    assert setup.initialize(target, port=18080) is False
    assert target.read_bytes() == before


def test_each_install_has_distinct_strong_secrets(tmp_path):
    values = []
    for name in ["first", "second"]:
        target = tmp_path / name
        setup.initialize(target)
        fields = dict(line.split("=", 1) for line in target.read_text().splitlines() if "=" in line)
        values.extend(
            fields[key]
            for key in [
                "DTK_SECRET_KEY",
                "POSTGRES_PASSWORD",
                "REDIS_PASSWORD",
                "DTK_QR_LOGIN_TOKEN",
                "DTK_DOWNLOADER_TOKEN",
            ]
        )
    assert len(set(values)) == 10
    assert all(len(value) == 64 for value in values)


@pytest.mark.parametrize("port,project", [(80, "dtk"), (70000, "dtk"), (8000, "bad/project")])
def test_invalid_setup_does_not_create_a_config(tmp_path, port, project):
    target = tmp_path / ".env.qr"
    with pytest.raises(ValueError):
        setup.initialize(target, port=port, project=project)
    assert not target.exists()
