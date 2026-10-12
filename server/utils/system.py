"""
系统工具函数模块
提供系统相关的工具函数，如重启管理等
"""
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

# 重启管理相关的全局变量
_restart_lock = threading.Lock()
_restart_scheduled = False


def _spawn_windows_restart_watcher(*, parent_pid: int, python: str) -> None:
    """Start a detached watcher that relaunches Flask after this PID exits."""
    project_root = str(Path(__file__).resolve().parents[2])
    watcher = r"""
import os
import socket
import subprocess
import sys
import time

parent_pid = int(sys.argv[1])
project_root = sys.argv[2]
python = sys.argv[3]

deadline = time.time() + 30.0
while time.time() < deadline:
    try:
        os.kill(parent_pid, 0)
    except OSError:
        break
    time.sleep(0.2)

while time.time() < deadline:
    probe = socket.socket()
    try:
        if probe.connect_ex(("127.0.0.1", 5000)) != 0:
            break
    finally:
        probe.close()
    time.sleep(0.2)

flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
child_env = dict(os.environ)
child_env["PYTHONIOENCODING"] = "utf-8"
subprocess.Popen(
    [python, "-m", "server.app"],
    cwd=project_root,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    close_fds=True,
    creationflags=flags,
    env=child_env,
)
"""
    flags = getattr(subprocess, "DETACHED_PROCESS", 0)
    flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(
        [python, "-c", watcher, str(parent_pid), project_root, python],
        cwd=project_root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=flags,
    )


def _schedule_restart(delay: float = 1.0) -> None:
    """
    在单独的线程里延迟执行 os.execl，实现平滑重启。
    """
    global _restart_scheduled
    with _restart_lock:
        if _restart_scheduled:
            return
        _restart_scheduled = True

    def _restart():
        time.sleep(delay)
        python = sys.executable
        if os.name == "nt":
            _spawn_windows_restart_watcher(
                parent_pid=os.getpid(),
                python=python,
            )
            os._exit(0)
            return
        os.execl(python, python, *sys.argv)

    threading.Thread(target=_restart, daemon=True).start()

