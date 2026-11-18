"""VLC 3 control, which needs a different door than everything else.

VLC 3 publishes **no** Windows media session (SMTC), so the system flyout and
``smtc.py`` cannot see it, and global media keys do not reach it. It does accept
the same window messages Explorer would send:

    WM_APPCOMMAND  APPCOMMAND_MEDIA_PLAY_PAUSE   pause / resume
    WM_APPCOMMAND  APPCOMMAND_MEDIA_NEXTTRACK    playlist next
    WM_APPCOMMAND  APPCOMMAND_MEDIA_PREVIOUSTRACK
    WM_APPCOMMAND  APPCOMMAND_MEDIA_STOP
    WM_KEYDOWN     VK_LEFT / VK_RIGHT            short jump (-10s / +10s)

Measured on VLC 3.0.17 (2026-11-18): posting these to the main window works
without giving VLC focus, which is what makes remote control viable. There is no
message that reports the playhead, though, so a seek bar needs VLC's Lua HTTP
interface, enabled once in the preferences or on the command line:

    vlc --extraintf http --http-port 8080 --http-password <password>

When that endpoint answers, ``state()`` carries position/duration and seeks are
absolute; otherwise the row is marked ``seek: False`` and only the relative
±10s keys are offered. Everything degrades to "detected, transport works".

Playing/paused comes from VLC's WASAPI session: Active means sound is running,
Inactive means paused (or the file has no audio track, in which case the state
is simply unknown and we show it as paused).
"""
from __future__ import annotations

import base64
import ctypes
import json
import time
import urllib.request

WM_APPCOMMAND = 0x0319
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
APPCOMMAND = {"toggle": 14, "next": 11, "prev": 12, "stop": 13}
VK_BACK, VK_FORWARD = 0x25, 0x27
TITLE_SUFFIX = " - VLC media player"

_user32 = ctypes.windll.user32


def _pids() -> list[int]:
    try:
        import psutil
    except Exception:
        return []
    found: list[int] = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if (proc.info.get("name") or "").lower() == "vlc.exe":
                found.append(int(proc.info["pid"]))
        except Exception:
            continue
    return found


def _windows() -> list[tuple[int, str, int]]:
    """(hwnd, title, pid) for every visible top-level window."""
    rows: list[tuple[int, str, int]] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def collect(hwnd, _):
        if not _user32.IsWindowVisible(hwnd):
            return True
        length = _user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buf, length + 1)
        pid = ctypes.c_ulong()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        rows.append((int(hwnd), buf.value, int(pid.value)))
        return True

    _user32.EnumWindows(collect, 0)
    return rows


def _audio_state(pid: int) -> int | None:
    """WASAPI session state: 1 = Active (playing), 0 = Inactive (paused)."""
    try:
        from pycaw.pycaw import AudioUtilities

        for session in AudioUtilities.GetAllSessions():
            proc = session.Process
            if proc is not None and proc.pid == pid:
                return int(session.State)
    except Exception:
        return None
    return None


def _post(hwnd: int, message: int, wparam: int, lparam: int) -> bool:
    try:
        return bool(_user32.PostMessageW(hwnd, message, wparam, lparam))
    except Exception:
        return False


class VLC:
    """One best guess at "the VLC window the user means"."""

    def __init__(self, http_port: int = 8080, http_password: str = "") -> None:
        self.port = int(http_port or 8080)
        self.password = str(http_password or "")
        self._http_at = 0.0
        self._http_ok = False

    # -- discovery ------------------------------------------------------

    def running(self) -> bool:
        return bool(_pids())

    def _target(self) -> dict | None:
        pids = set(_pids())
        if not pids:
            return None
        windows = [w for w in _windows()
                   if w[2] in pids and w[1].endswith(TITLE_SUFFIX)]
        if not windows:
            return None
        best: tuple[int, dict] | None = None
        for hwnd, title, pid in windows:
            state = _audio_state(pid)
            score = 2 if state == 1 else 1 if state == 0 else 0
            if best is None or score > best[0]:
                best = (score, {
                    "hwnd": hwnd,
                    "pid": pid,
                    "title": title[:-len(TITLE_SUFFIX)].strip() or title,
                    "audio": state,
                })
        return best[1] if best else None

    # -- optional Lua HTTP interface ------------------------------------

    def _request(self, query: str = "", timeout: float = 1.5) -> dict | None:
        url = f"http://127.0.0.1:{self.port}/requests/status.json"
        if query:
            url += "?" + query
        req = urllib.request.Request(url)
        token = base64.b64encode(f":{self.password}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                raw = response.read(120_000)
        except Exception:
            return None
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        return data

    def _status(self) -> dict | None:
        """Probe the Lua interface; back off for 10s after a miss."""
        now = time.monotonic()
        if not self._http_ok and now - self._http_at < 10.0:
            return None
        self._http_at = now
        data = self._request()
        self._http_ok = data is not None and "state" in data and "time" in data
        return data if self._http_ok else None

    def _command(self, query: str) -> bool:
        return self._request(query, timeout=1.2) is not None

    # -- public API -----------------------------------------------------

    def state(self) -> dict | None:
        target = self._target()
        if target is None:
            return None
        http = self._status()

        playing = target["audio"] == 1
        title = target["title"]
        position = duration = None
        if http is not None:
            playing = str(http.get("state") or "") == "playing"
            try:
                position = max(0.0, float(http.get("time") or 0))
                length = float(http.get("length") or 0)
                duration = length if length > 0 else None
            except (TypeError, ValueError):
                position = duration = None
            title = _http_title(http) or title

        return {
            "id": f"vlc.exe|{title}",
            "app": "vlc.exe",
            "title": title,
            "artist": None,
            "album": None,
            "playing": playing,
            "position": round(position, 1) if position is not None else None,
            "duration": round(duration, 1) if duration else None,
            "can": {
                "play_pause": True,
                "next": True,
                "prev": True,
                "stop": True,
                "seek": http is not None,
                "skip": True,
            },
            "current": False,
            "art": None,
            "min_seek": 0.0 if duration else None,
            "max_seek": round(duration, 1) if duration else None,
            "source": "vlc",
        }

    def command(self, action: str, target: dict | None = None):
        row = self._target()
        if row is None:
            return "none", None
        resolved = {"app": "vlc.exe", "title": row["title"]}
        if action not in APPCOMMAND:
            return "error", resolved

        http = self._status()
        if http is not None:
            query = {"toggle": "pl_pause", "next": "pl_next",
                     "prev": "pl_previous", "stop": "pl_stop"}[action]
            if self._command("command=" + query):
                return "ok", resolved

        ok = _post(row["hwnd"], WM_APPCOMMAND, 0, APPCOMMAND[action] << 16)
        return ("ok" if ok else "error"), resolved

    def seek(self, position: float, target: dict | None = None):
        row = self._target()
        if row is None:
            return "none", None
        resolved = {"app": "vlc.exe", "title": row["title"]}
        if self._status() is None:
            return "error", resolved
        if self._command(f"command=seek&val={int(max(0.0, position))}"):
            return "ok", resolved
        return "error", resolved

    def skip(self, delta: float, target: dict | None = None):
        """Jump ±delta seconds. Window mode posts VLC's 10s short-jump key."""
        row = self._target()
        if row is None:
            return "none", None
        resolved = {"app": "vlc.exe", "title": row["title"]}

        http = self._status()
        if http is not None:
            try:
                now = float(http.get("time") or 0)
                length = float(http.get("length") or 0)
                wanted = max(0.0, now + float(delta))
                if length > 0:
                    wanted = min(length, wanted)
                if self._command(f"command=seek&val={int(wanted)}"):
                    return "ok", resolved
            except (TypeError, ValueError):
                pass

        presses = max(1, min(6, int(round(abs(float(delta)) / 10.0))))
        key = VK_FORWARD if delta > 0 else VK_BACK
        for _ in range(presses):
            _post(row["hwnd"], WM_KEYDOWN, key, 0x00000001)
            _post(row["hwnd"], WM_KEYUP, key, 0xC0000001)
            time.sleep(0.02)
        return "ok", resolved


def _http_title(data: dict) -> str | None:
    try:
        meta = data["information"]["category"]["meta"]
    except (KeyError, TypeError):
        return None
    for key in ("title", "filename", "now_playing"):
        value = meta.get(key)
        if value:
            return str(value).strip() or None
    return None
