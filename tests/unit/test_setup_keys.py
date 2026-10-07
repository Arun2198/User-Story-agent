import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "setup_keys.sh"
ANTHROPIC = "sk-ant-" + "a" * 30
NVIDIA = "nvapi-" + "n" * 30


def run(tmp: Path, stdin: str) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp),
        "NVIDIA_ENV_FILE": str(tmp / ".nvidia_env"),
        "NVIDIA_RC_FILE": str(tmp / ".bashrc"),
    }
    probe = (
        f'source "{SCRIPT}" && echo "anthropic=${{ANTHROPIC_API_KEY:-}}" '
        '&& echo "nvidia=${NVIDIA_API_KEY:-}"'
    )
    return subprocess.run(  # noqa: S603  (a fixed local script)
        ["bash", "-c", probe],  # noqa: S607
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_both_keys_are_saved_privately_and_loaded(tmp_path: Path) -> None:
    result = run(tmp_path, f"{ANTHROPIC}\n{NVIDIA}\n")
    assert result.returncode == 0, result.stderr
    assert f"anthropic={ANTHROPIC}" in result.stdout
    assert f"nvidia={NVIDIA}" in result.stdout
    saved = tmp_path / ".nvidia_env"
    assert (saved.stat().st_mode & 0o777) == 0o600
    assert saved.read_text(encoding="utf-8").count("export ") == 2
    assert (tmp_path / ".bashrc").read_text(encoding="utf-8").count("nvidia_env") >= 1


@pytest.mark.parametrize(
    ("stdin", "kept", "name"),
    [
        (f"{ANTHROPIC}\n\n", "anthropic", "ANTHROPIC_API_KEY"),
        (f"\n{NVIDIA}\n", "nvidia", "NVIDIA_API_KEY"),
    ],
)
def test_one_key_is_enough(tmp_path: Path, stdin: str, kept: str, name: str) -> None:
    result = run(tmp_path, stdin)
    assert result.returncode == 0, result.stderr
    saved = (tmp_path / ".nvidia_env").read_text(encoding="utf-8")
    assert saved.count("export ") == 1
    assert name in saved
    assert f"{name}: set" in result.stdout
    other = "NVIDIA_API_KEY" if name == "ANTHROPIC_API_KEY" else "ANTHROPIC_API_KEY"
    assert f"{other}: not set" in result.stdout
    assert f"{kept}=" in result.stdout


def test_the_keys_never_appear_in_the_summary(tmp_path: Path) -> None:
    result = run(tmp_path, f"{ANTHROPIC}\n{NVIDIA}\n")
    cleaned = result.stdout.replace(f"anthropic={ANTHROPIC}", "").replace(f"nvidia={NVIDIA}", "")
    for text in (cleaned, result.stderr):
        assert ANTHROPIC not in text
        assert NVIDIA not in text


def test_running_again_replaces_the_keys_and_keeps_one_startup_line(tmp_path: Path) -> None:
    run(tmp_path, f"{ANTHROPIC}\n{NVIDIA}\n")
    other = "nvapi-" + "z" * 30
    result = run(tmp_path, f"\n{other}\n")
    assert f"nvidia={other}" in result.stdout
    assert f"anthropic={ANTHROPIC}" not in result.stdout  # the old key is not kept
    assert (tmp_path / ".nvidia_env").read_text(encoding="utf-8").count("export ") == 1
    assert (tmp_path / ".bashrc").read_text(encoding="utf-8").count("nvidia_env") == 2  # one line


def test_no_key_at_all_saves_nothing(tmp_path: Path) -> None:
    result = run(tmp_path, "\n\n")
    assert result.returncode != 0
    assert "Nothing was saved" in result.stderr
    assert not (tmp_path / ".nvidia_env").exists()


@pytest.mark.parametrize("bad", ["short", "has space " + "x" * 30, "quote'" + "x" * 30])
@pytest.mark.parametrize("slot", [0, 1])
def test_a_bad_key_is_refused_and_nothing_is_saved(tmp_path: Path, bad: str, slot: int) -> None:
    lines = ["", ""]
    lines[slot] = bad
    result = run(tmp_path, "\n".join(lines) + "\n")
    assert result.returncode != 0
    assert "Nothing was saved" in result.stderr
    assert not (tmp_path / ".nvidia_env").exists()


def test_the_script_holds_no_key_and_is_executable() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "nvapi-" not in text.replace("start with nvapi-", "").replace("nvapi-*", "")
    assert os.access(SCRIPT, os.X_OK)
