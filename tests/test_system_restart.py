from __future__ import annotations

from unittest.mock import Mock

from server.utils import system


def test_windows_restart_watcher_relaunches_server_module(monkeypatch):
    popen = Mock()
    monkeypatch.setattr(system.subprocess, "Popen", popen)
    monkeypatch.setattr(system.subprocess, "DETACHED_PROCESS", 8, raising=False)
    monkeypatch.setattr(
        system.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, raising=False
    )
    monkeypatch.setattr(system.subprocess, "CREATE_NO_WINDOW", 1024, raising=False)

    system._spawn_windows_restart_watcher(parent_pid=123, python="python.exe")

    args, kwargs = popen.call_args
    command = args[0]
    assert command[:3] == ["python.exe", "-c", command[2]]
    assert command[3] == "123"
    assert command[-1] == "python.exe"
    assert '"-m", "server.app"' in command[2]
    assert 'child_env["PYTHONIOENCODING"] = "utf-8"' in command[2]
    assert kwargs["creationflags"] == 8 | 512 | 1024
    assert kwargs["close_fds"] is True


def test_schedule_restart_uses_windows_watcher(monkeypatch):
    watcher = Mock()
    exit_process = Mock()
    monkeypatch.setattr(system.os, "name", "nt")
    monkeypatch.setattr(system.os, "getpid", lambda: 456)
    monkeypatch.setattr(system.os, "_exit", exit_process)
    monkeypatch.setattr(system, "_spawn_windows_restart_watcher", watcher)
    monkeypatch.setattr(system.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(system.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(system, "_restart_scheduled", False)

    system._schedule_restart(delay=0)

    watcher.assert_called_once_with(parent_pid=456, python=system.sys.executable)
    exit_process.assert_called_once_with(0)


class ImmediateThread:
    def __init__(self, *, target, daemon):
        self.target = target
        self.daemon = daemon

    def start(self):
        self.target()
