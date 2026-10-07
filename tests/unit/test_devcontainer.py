import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG = json.loads((ROOT / ".devcontainer" / "devcontainer.json").read_text(encoding="utf-8"))


def test_the_container_installs_from_the_lockfile() -> None:
    command = CONFIG["postCreateCommand"]
    assert "uv sync --locked" in command
    assert (ROOT / "uv.lock").exists()


def test_the_key_is_a_declared_secret_and_never_a_value() -> None:
    assert "NVIDIA_API_KEY" in CONFIG["secrets"]
    assert "NVIDIA_API_KEY_JUDGE" not in CONFIG["secrets"]
    text = (ROOT / ".devcontainer" / "devcontainer.json").read_text(encoding="utf-8")
    assert "nvapi-" not in text.replace("starts with nvapi-", "")
    for section in ("containerEnv", "remoteEnv"):
        assert "NVIDIA_API_KEY" not in CONFIG.get(section, {})


def test_the_python_version_matches_the_project() -> None:
    assert "3.12" in CONFIG["image"]
    assert 'requires-python = ">=3.11"' in (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def test_private_folders_stay_out_of_git() -> None:
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for entry in ("/runs/", "/memory/", ".env"):
        assert entry in ignored
