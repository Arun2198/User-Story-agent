import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "setup_keys.sh"
MAIN = "nvapi-" + "m" * 30
JUDGE = "nvapi-" + "j" * 30


def run(tmp: Path, stdin: str) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp),
        "NVIDIA_ENV_FILE": str(tmp / ".nvidia_env"),
        "NVIDIA_RC_FILE": str(tmp / ".bashrc"),
    }
    probe = (
        f'source "{SCRIPT}" && echo "main=${{NVIDIA_API_KEY:-}}" '
        '&& echo "judge=${NVIDIA_API_KEY_JUDGE:-}"'
    )
    return subprocess.run(  # noqa: S603  (a fixed local script)
        ["bash", "-c", probe],  # noqa: S607
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_two_keys_are_saved_privately_and_loaded(tmp_path: Path) -> None:
    result = run(tmp_path, f"{MAIN}\n{JUDGE}\n")
    assert result.returncode == 0, result.stderr
    assert f"main={MAIN}" in result.stdout
    assert f"judge={JUDGE}" in result.stdout
    saved = tmp_path / ".nvidia_env"
    assert (saved.stat().st_mode & 0o777) == 0o600
    assert saved.read_text(encoding="utf-8").count("export ") == 2
    assert (tmp_path / ".bashrc").read_text(encoding="utf-8").count("nvidia_env") >= 1


def test_the_keys_never_appear_in_the_output(tmp_path: Path) -> None:
    result = run(tmp_path, f"{MAIN}\n{JUDGE}\n")
    for text in (
        result.stdout.replace(f"main={MAIN}", "").replace(f"judge={JUDGE}", ""),
        result.stderr,
    ):
        assert MAIN not in text
        assert JUDGE not in text


def test_a_blank_judge_key_means_the_main_key_is_reused(tmp_path: Path) -> None:
    result = run(tmp_path, f"{MAIN}\n\n")
    assert result.returncode == 0, result.stderr
    assert "judge=\n" in result.stdout or result.stdout.endswith("judge=\n")
    assert (tmp_path / ".nvidia_env").read_text(encoding="utf-8").count("export ") == 1
    assert "the judge will use NVIDIA_API_KEY" in result.stdout


def test_running_twice_does_not_duplicate_the_startup_line(tmp_path: Path) -> None:
    run(tmp_path, f"{MAIN}\n\n")
    run(tmp_path, f"{MAIN}\n{JUDGE}\n")
    assert (tmp_path / ".bashrc").read_text(encoding="utf-8").count("nvidia_env") == 2  # one line


@pytest.mark.parametrize("bad", ["", "short", "has space nvapi-" + "x" * 30, "quote'" + "x" * 30])
def test_a_bad_key_is_refused_and_nothing_is_saved(tmp_path: Path, bad: str) -> None:
    result = run(tmp_path, f"{bad}\n")
    assert result.returncode != 0
    assert "Nothing was saved" in result.stderr
    assert not (tmp_path / ".nvidia_env").exists()


def test_a_bad_judge_key_is_refused_and_nothing_is_saved(tmp_path: Path) -> None:
    result = run(tmp_path, f"{MAIN}\nbad key\n")
    assert result.returncode != 0
    assert not (tmp_path / ".nvidia_env").exists()


def test_the_script_holds_no_key_and_is_executable() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "nvapi-" not in text.replace("start with nvapi-", "").replace("nvapi-*", "")
    assert os.access(SCRIPT, os.X_OK)
