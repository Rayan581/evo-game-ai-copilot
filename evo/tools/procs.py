"""Cross-platform process helpers for background training runs."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return bool(ok) and code.value == STILL_ACTIVE
    try:
        os.kill(int(pid), 0)
    except OSError:
        return False
    # reap zombies of our own children so they don't look alive
    try:
        done, _ = os.waitpid(int(pid), os.WNOHANG)
        if done == int(pid):
            return False
    except ChildProcessError:
        pass
    return True


def spawn_detached(args: list[str], cwd: Path, stderr_path: Path) -> subprocess.Popen:
    err = open(stderr_path, "ab")
    kwargs: dict = dict(cwd=str(cwd), stdout=subprocess.DEVNULL, stderr=err, stdin=subprocess.DEVNULL)
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008  # DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    env = dict(os.environ)
    env.setdefault("PYTHONUNBUFFERED", "1")
    kwargs["env"] = env
    return subprocess.Popen([sys.executable, *args], **kwargs)
