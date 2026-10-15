import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

HOOK = Path(__file__).parents[2] / "scripts/statusline_limits_hook.py"


def module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("usage_hook", HOOK)
    assert spec and spec.loader
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def invoke(cache: Path, payload: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", str(HOOK), "--cache-path", str(cache)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=5,
    )


def test_hook_subprocess_whitelist_modes_and_identical_capture(tmp_path: Path) -> None:
    cache = tmp_path / "state/statusline-limits.json"
    payload = {
        "model": {"display_name": "model-a\n\x1b[31m"},
        "context_window": {"used_percentage": 21},
        "rate_limits": {"five_hour": {"used_percentage": 12.5, "resets_at": 1791676801}},
        "private": "private-marker",
    }
    result = invoke(cache, payload)
    assert result.returncode == 0 and not result.stderr
    assert len(result.stdout.strip()) <= 80 and "ctx 21%" in result.stdout
    assert "\x1b" not in result.stdout and "private-marker" not in result.stdout
    first = json.loads(cache.read_text())
    assert set(first) == {"captured_ms", "windows"}
    assert first["windows"] == {"five_hour": {"used_percentage": 12.5, "resets_at": 1791676801}}
    assert cache.stat().st_mode & 0o777 == 0o600
    assert cache.parent.stat().st_mode & 0o777 == 0o700
    assert not list(cache.parent.glob("*.tmp"))
    assert invoke(cache, payload).returncode == 0
    assert json.loads(cache.read_text()) == first
    assert "private-marker" not in "".join(p.read_text() for p in cache.parent.iterdir())


@pytest.mark.parametrize(
    "percent,reset", [(True, 1), (-1, 1), (101, 1), (float("inf"), 1), (1, True), (1, 1.1), (1, -1)]
)
def test_invalid_windows_never_persist(tmp_path: Path, percent: object, reset: object) -> None:
    cache = tmp_path / "state/cache.json"
    result = invoke(
        cache,
        {
            "rate_limits": {
                "five_hour": {
                    "used_percentage": percent,
                    "resets_at": reset,
                }
            }
        },
    )
    assert result.returncode == 0 and not result.stderr
    assert not cache.exists() and "ctx ?" in result.stdout


def test_older_writers_and_identical_payload_do_not_refresh(tmp_path: Path) -> None:
    hook = module()
    cache = tmp_path / "state/cache.json"
    a = {"five_hour": {"used_percentage": 10, "resets_at": 5000}}
    b = {"five_hour": {"used_percentage": 20, "resets_at": 5000}}
    assert hook.persist(cache, a, 2000)
    assert not hook.persist(cache, b, 1000)
    assert hook.persist(cache, a, 3000)
    assert json.loads(cache.read_text())["captured_ms"] == 2000
    assert not hook.persist(cache, b, 2500)  # later identical repaint fences older different data
    assert hook.persist(cache, b, 4000)
    assert json.loads(cache.read_text())["captured_ms"] == 4000


def test_concurrent_process_writers_keep_latest_receipt(tmp_path: Path) -> None:
    cache = tmp_path / "state/cache.json"
    script = (
        "import importlib.util,sys; from pathlib import Path; "
        "s=importlib.util.spec_from_file_location('h',sys.argv[1]); "
        "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
        "m.persist(Path(sys.argv[2]),{'five_hour':{'used_percentage':int(sys.argv[3]),"
        "'resets_at':5000}},int(sys.argv[3]))"
    )
    children = [
        subprocess.Popen([sys.executable, "-B", "-c", script, str(HOOK), str(cache), str(i)])
        for i in (20, 80, 10, 50)
    ]
    assert all(p.wait(timeout=5) == 0 for p in children)
    value = json.loads(cache.read_text())
    assert value["captured_ms"] == 80 and value["windows"]["five_hour"]["used_percentage"] == 80


def test_symlink_lock_timeout_and_bounded_stdin(tmp_path: Path) -> None:
    import fcntl

    hook = module()
    cache = tmp_path / "state/cache.json"
    hook.persist(cache, {"five_hour": {"used_percentage": 1, "resets_at": 5000}}, 1)
    with (cache.parent / ".statusline-limits.lock").open("r") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert not hook.persist(cache, {"five_hour": {"used_percentage": 2, "resets_at": 5000}}, 2)
    target = tmp_path / "untouched"
    target.write_text("private-marker")
    cache.unlink()
    cache.symlink_to(target)
    assert not hook.persist(cache, {"five_hour": {"used_percentage": 2, "resets_at": 5000}}, 2)
    assert target.read_text() == "private-marker"
    result = subprocess.run(
        [sys.executable, "-B", str(HOOK), "--cache-path", str(cache)],
        input="x" * 65537,
        text=True,
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 0 and result.stdout.strip().endswith("ctx ?")
    assert not result.stderr


def test_huge_invalid_context_percentage_is_silent(tmp_path: Path) -> None:
    result = invoke(tmp_path / "cache.json", {"context_window": {"used_percentage": 10**1000}})
    assert result.returncode == 0 and result.stdout.strip().endswith("ctx ?")
    assert not result.stderr
