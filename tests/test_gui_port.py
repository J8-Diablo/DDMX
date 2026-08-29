"""The desktop shell must not die because another program holds its port.

Port 5000 is crowded on Windows -- Logitech G HUB's CS:GO Arx applet listens
there, among others -- and binding over an exclusive socket fails with
"access forbidden" rather than "already in use", so the failure did not even
read as a port conflict. The shell now takes the first free port.

gui.py imports PySide6 and the whole app, so the port helpers are lifted out
and exercised on their own.
"""

from __future__ import annotations

import os
import socket
import sys

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _load_port_helpers():
    with open(os.path.join(_REPO_ROOT, "gui.py"), "r", encoding="utf-8") as fh:
        src = fh.read()
    start = src.index('HOST = "127.0.0.1"')
    end = src.index("_popup_views")
    namespace = {"socket": socket, "os": os}
    exec(src[start:end], namespace)  # noqa: S102 - our own source, on purpose
    assert "choose_port" in namespace and "_port_is_free" in namespace
    return namespace


@pytest.fixture()
def gui():
    return _load_port_helpers()


@pytest.fixture()
def taken_port():
    """A port held by somebody else for the duration of the test."""
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    yield port
    holder.close()


def test_a_free_port_reads_as_free(gui):
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    assert gui["_port_is_free"](port) is True


def test_a_held_port_reads_as_taken(gui, taken_port):
    assert gui["_port_is_free"](taken_port) is False


def test_the_shell_moves_to_the_next_free_port(gui, taken_port):
    gui["PREFERRED_PORT"] = taken_port

    chosen = gui["choose_port"]()

    assert chosen != taken_port, "it must not insist on a port somebody holds"
    assert taken_port < chosen <= taken_port + 20
    assert gui["URL"] == f"http://127.0.0.1:{chosen}/"
    assert gui["META_URL"] == f"http://127.0.0.1:{chosen}/api/meta"


def test_the_preferred_port_wins_when_it_is_free(gui):
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    gui["PREFERRED_PORT"] = port

    assert gui["choose_port"]() == port


def test_ddmx_port_forces_one(gui, monkeypatch, taken_port):
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    forced = probe.getsockname()[1]
    probe.close()

    gui["PREFERRED_PORT"] = taken_port
    monkeypatch.setenv("DDMX_PORT", str(forced))

    assert gui["choose_port"]() == forced, "an explicit port must be honoured"


def test_a_forced_port_that_is_taken_is_still_used(gui, monkeypatch, taken_port):
    """Asking for a busy port must surface the real bind error, not wander off."""
    gui["PREFERRED_PORT"] = 5000
    monkeypatch.setenv("DDMX_PORT", str(taken_port))

    assert gui["choose_port"]() == taken_port
