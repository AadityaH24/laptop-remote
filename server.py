#!/usr/bin/env python3
"""
Laptop Remote — control this Windows PC from a phone or tablet.

Transport model
---------------
By default the server binds to 127.0.0.1 only. The only way in is through
`tailscale serve`: either the https flavour (real TLS on 443) or the tcp flavour
(`serve --bg --tcp 8765 8765`), which forwards raw TCP and therefore speaks plain
http — still inside WireGuard, still tailnet-only. Nothing is ever exposed on the
local network, so there is no firewall hole to open and no URL that works from a
coffee-shop Wi-Fi.

Pairing model
-------------
A PIN is printed on startup. Entering it once per device mints a long-lived
token (the device stores it in localStorage; the server stores only a SHA-256
hash). Tokens are listed on the settings screen and can be revoked at any time,
which is the right response to a lost phone.
"""
from __future__ import annotations

import argparse
import asyncio
import atexit
import base64
import ctypes
import hashlib
import json
import logging
import os
import queue
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
STATE_FILE = ROOT / "state.json"
AUDIT_FILE = ROOT / "audit.log"
CONFIG_FILE = ROOT / "config.toml"
LOG_FILE = ROOT / "server.log"
LOG = logging.getLogger("remote")

# Windows virtual-key codes we care about.
VK_MEDIA_NEXT = 0xB0
VK_MEDIA_PREV = 0xB1
VK_MEDIA_STOP = 0xB2
VK_MEDIA_PLAY_PAUSE = 0xB3
VK_RETURN = 0x0D
KEYEVENTF_KEYUP = 0x0002

POWER_ACTIONS = ("lock", "sleep", "displayoff", "signout")

# -- named shortcuts -------------------------------------------------------
# The phone can only ask for a shortcut *by name*; it can never send a key
# sequence of its own. That keeps a compromised page from inventing Win+R.
VK_KEYS = {
    "ctrl": 0x11,
    "alt": 0x12,
    "shift": 0x10,
    "win": 0x5B,
    "enter": 0x0D,
    "esc": 0x1B,
    "tab": 0x09,
    "space": 0x20,
    "backspace": 0x08,
    "delete": 0x2E,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "f5": 0x74,
    "f11": 0x7A,
}
for _ch in "abcdefghijklmnopqrstuvwxyz0123456789":
    VK_KEYS.setdefault(_ch, ord(_ch.upper()))

SHORTCUT_MODS = ("ctrl", "alt", "shift", "win")

SHORTCUTS: dict[str, tuple[str, ...]] = {
    "copy": ("ctrl", "c"),
    "paste": ("ctrl", "v"),
    "cut": ("ctrl", "x"),
    "undo": ("ctrl", "z"),
    "redo": ("ctrl", "y"),
    "selectall": ("ctrl", "a"),
    "save": ("ctrl", "s"),
    "find": ("ctrl", "f"),
    "newtab": ("ctrl", "t"),
    "closetab": ("ctrl", "w"),
    "reopentab": ("ctrl", "shift", "t"),
    "refresh": ("f5",),
    "back": ("alt", "left"),
    "forward": ("alt", "right"),
    "alttab": ("alt", "tab"),
    "showdesktop": ("win", "d"),
    "enter": ("enter",),
    "esc": ("esc",),
    "tab": ("tab",),
    "backspace": ("backspace",),
    "up": ("up",),
    "down": ("down",),
    "left": ("left",),
    "right": ("right",),
}

# Widget types the front end knows how to draw. Config may reference any of
# these; anything else is skipped and reported at startup.
WIDGET_TYPES = (
    "nowplaying",
    "volume",
    "brightness",
    "lock",
    "audio_out",
    "apps",
    "windows",
    "touchpad",
    "clickbar",
    "shortcuts",
    "typebar",
)

DEFAULT_DECK: list[dict[str, Any]] = [
    {"id": "media", "label": "Media", "widgets": ["nowplaying", "volume"]},
    {
        "id": "mouse",
        "label": "Mouse",
        "widgets": ["touchpad", "clickbar", "shortcuts", "typebar"],
    },
    {"id": "system", "label": "System", "widgets": ["volume", "brightness", "lock", "audio_out"]},
    {"id": "apps", "label": "Apps", "widgets": ["apps", "windows"]},
]


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

DEFAULTS: dict[str, Any] = {
    "server": {"port": 8765, "base_path": ""},
    "security": {
        "pin_enabled": True,
        "pairing_window_minutes": 10,
        "allowed_ssids": [],
        "allowed_logins": [],
    },
    "apps": {},
    "widgets": {},
    "vlc": {"enabled": True, "http_port": 8080, "http_password": ""},
}


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def normalise_base(value: str | None) -> str:
    """'' for the root, otherwise '/some/prefix' with no trailing slash."""
    value = (value or "").strip()
    if value in ("", "/"):
        return ""
    return "/" + value.strip("/")


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return json.loads(json.dumps(DEFAULTS))
    with CONFIG_FILE.open("rb") as fh:
        return _merge(DEFAULTS, tomllib.load(fh))


def resolve_layout(config: dict) -> dict:
    """Turn the `[[deck]]` tables into the layout the phone renders.

    A tile is `{id, label, widgets:[{type, options}]}`. Unknown widget types are
    dropped (never rendered as errors), duplicate ids are dropped, and an
    unusable deck falls back to the built-in one, so a config typo cannot leave
    the remote with a blank screen.
    """
    warnings: list[str] = []
    options_by_type = config.get("widgets") or {}
    raw = config.get("deck")

    def build(items: list, source: str) -> list[dict]:
        tiles: list[dict] = []
        seen: set[str] = set()
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                warnings.append(f"{source}[{index}] is not a table — skipped")
                continue
            tile_id = str(item.get("id") or "").strip()
            if not tile_id:
                tile_id = f"tile{index + 1}"
                warnings.append(f"{source}[{index}] has no id — using {tile_id!r}")
            if tile_id in seen:
                warnings.append(f"duplicate tile id {tile_id!r} — skipped")
                continue
            seen.add(tile_id)
            label = str(item.get("label") or tile_id)
            wanted = item.get("widgets")
            if not isinstance(wanted, list):
                warnings.append(f"tile {tile_id!r}: 'widgets' is not a list — skipped")
                continue
            widgets: list[dict] = []
            for name in wanted:
                name = str(name)
                if name not in WIDGET_TYPES:
                    warnings.append(f"tile {tile_id!r}: unknown widget {name!r} — skipped")
                    continue
                options = options_by_type.get(name)
                widgets.append({"type": name, "options": options if isinstance(options, dict) else {}})
            if not widgets:
                warnings.append(f"tile {tile_id!r}: no usable widgets — dropped")
                continue
            tiles.append({"id": tile_id, "label": label, "widgets": widgets})
        return tiles

    tiles: list[dict] = []
    if isinstance(raw, list) and raw:
        tiles = build(raw, "deck")
    if not tiles:
        if raw:
            warnings.append("no usable deck tiles — falling back to the built-in deck")
        tiles = build(json.loads(json.dumps(DEFAULT_DECK)), "deck")

    return {"tiles": tiles, "widgets": list(WIDGET_TYPES), "warnings": warnings}


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------

def _read_json(path: Path, fallback: dict) -> dict:
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return fallback


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), "utf-8")
    tmp.replace(path)


AUDIT_QUEUE: "queue.Queue[tuple[str, str, str] | None]" = queue.Queue(maxsize=20000)
AUDIT_THREAD: threading.Thread | None = None


def _audit_writer() -> None:
    """Drains audit lines onto disk and the console, off the event loop.

    A console write on Windows costs 10–20 ms. Doing that inline meant every
    tap stalled the pointer stream behind it, so taps felt like a hitch while
    dragging. The writer thread keeps the order and nobody waits for it.
    """
    while True:
        item = AUDIT_QUEUE.get()
        try:
            if item is None:
                return
            line, summary, who = item
            try:
                with AUDIT_FILE.open("a", encoding="utf-8") as fh:
                    fh.write(line)
            except Exception:
                LOG.debug("audit write failed", exc_info=True)
            try:
                LOG.info("%s  (%s)", summary, who)
            except Exception:
                pass
        finally:
            AUDIT_QUEUE.task_done()


def _audit_shutdown() -> None:
    if AUDIT_THREAD is None or not AUDIT_THREAD.is_alive():
        return
    try:
        AUDIT_QUEUE.put_nowait(None)
    except Exception:
        return
    AUDIT_THREAD.join(timeout=3)


atexit.register(_audit_shutdown)


def audit(
    event: str,
    detail: str = "",
    device: str = "-",
    login: str = "-",
    ip: str = "-",
) -> None:
    """Record what happened, who did it, and from which device.

    Writes one tab-separated row to audit.log and mirrors it to the console (and
    therefore server.log) so it shows up in the terminal window too. The actual
    I/O happens on a background thread: this call must never block the caller.
    """
    global AUDIT_THREAD

    def clean(value: str) -> str:
        return str(value).replace("\t", " ").replace("\n", " ").replace("\r", " ")

    device, login, ip, detail = clean(device), clean(login), clean(ip), clean(detail)
    device = device or "-"
    login = login or "-"
    ip = ip or "-"
    line = (
        f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{device}\t{ip}\t{login}\t"
        f"{event}\t{detail}\n"
    )

    who = [f"device={device or '-'}"]
    if ip and ip != "-":
        who.append(f"ip={ip}")
    if login and login != "-":
        who.append(f"user={login}")
    summary = f"{event} {detail}".strip()

    if AUDIT_THREAD is None or not AUDIT_THREAD.is_alive():
        AUDIT_THREAD = threading.Thread(
            target=_audit_writer, name="audit-writer", daemon=True
        )
        AUDIT_THREAD.start()
    try:
        AUDIT_QUEUE.put_nowait((line, summary, ", ".join(who)))
    except queue.Full:
        # Dropping a log line beats blocking a live input stream.
        LOG.warning("audit queue full, dropped: %s", summary)


def session_where(used: dict | None, target: dict | None) -> str:
    """Audit suffix: which session actually ran it, and whether it was the one
    the phone asked for. A vanished target falls back to the current session,
    so say that instead of logging the app unmatched."""
    if not used:
        return ""
    where = f" {used.get('app') or '?'}"
    if used.get("title"):
        where += f" / {used['title']}"
    if target:
        same = (
            str(target.get("app") or "") == str(used.get("app") or "")
            and str(target.get("title") or "").strip() == str(used.get("title") or "")
        )
        if not same:
            where += " (requested session gone, used the current one)"
    return where


# ---------------------------------------------------------------------------
# Media artwork cache
# ---------------------------------------------------------------------------
# Windows hands us thumbnails as base64 data URLs. The live state is pushed
# once a second while something plays, so inlining them in every frame would
# burn a phone's battery. Instead each image gets a content-addressed URL and
# the browser caches it forever (the hash changes when the art changes).

ART_CACHE_MAX = 48
_ART_BYTES: dict[str, tuple[bytes, str]] = {}
_ART_URLS: dict[str, str] = {}
_ART_ORDER: list[str] = []


def stash_art(data_url: str | None, base: str) -> str | None:
    """Store a data URL and return a stable ``/api/art/<hash>`` path."""
    if not data_url or not data_url.startswith("data:"):
        return None
    cached = _ART_URLS.get(data_url)
    if cached:
        return cached
    head, _, payload = data_url.partition(",")
    try:
        raw = base64.b64decode(payload)
    except Exception:
        return None
    if not raw or len(raw) > 2_000_000:
        return None
    mime = head[5:].split(";")[0] or "image/jpeg"
    key = hashlib.sha1(raw).hexdigest()[:20]
    if key not in _ART_BYTES:
        _ART_BYTES[key] = (raw, mime)
        _ART_ORDER.append(key)
        while len(_ART_ORDER) > ART_CACHE_MAX:
            dead = _ART_ORDER.pop(0)
            _ART_BYTES.pop(dead, None)
    url = f"{base}/api/art/{key}"
    _ART_URLS[data_url] = url
    while len(_ART_URLS) > ART_CACHE_MAX * 2:
        _ART_URLS.pop(next(iter(_ART_URLS)), None)
    return url


def art_bytes(key: str) -> tuple[bytes, str] | None:
    return _ART_BYTES.get(key)


# ---------------------------------------------------------------------------
# Win32 synthetic input (mouse + unicode keyboard)
# ---------------------------------------------------------------------------

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
KEYEVENTF_UNICODE = 0x0004

_ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_ulong),
        ("wParamL", ctypes.c_ushort),
        ("wParamH", ctypes.c_ushort),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTUNION)]


CLICK_FLAGS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
}


def _mouse_event(dx: int, dy: int, data: int, flags: int) -> INPUT:
    return INPUT(
        type=INPUT_MOUSE,
        u=_INPUTUNION(
            mi=_MOUSEINPUT(
                dx=dx,
                dy=dy,
                mouseData=data & 0xFFFFFFFF,
                dwFlags=flags,
                time=0,
                dwExtraInfo=0,
            )
        ),
    )


def _key_event(vk: int = 0, scan: int = 0, flags: int = 0) -> INPUT:
    return INPUT(
        type=INPUT_KEYBOARD,
        u=_INPUTUNION(
            ki=_KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
        ),
    )


def send_inputs(batch: list[INPUT]) -> None:
    """One SendInput call for a whole batch: keeps ordering and is fast."""
    if not batch:
        return
    array = (INPUT * len(batch))(*batch)
    sent = ctypes.windll.user32.SendInput(len(batch), array, ctypes.sizeof(INPUT))
    if sent != len(batch):
        # Most often UIPI: the focused window runs elevated and refuses input
        # from a normal-integrity process.
        err = ctypes.windll.kernel32.GetLastError()
        raise OSError(f"SendInput accepted {sent}/{len(batch)} events (win32 error {err})")


# ---------------------------------------------------------------------------
# Windows capability layer
# ---------------------------------------------------------------------------

class Caps:
    """Every Windows call lives here, on one worker thread, so COM stays sane."""

    def __init__(self, simulate: bool = False) -> None:
        self.simulate = simulate
        self.volume_ok = False
        self.brightness_ok = False
        self.audio_ok = False
        self._vol = None
        self._audio = None
        self._brightness_cache = (0.0, None)
        self._stremio_cache = (0.0, False)
        self._proc_names: dict[int, str] = {}
        self._init_volume()
        self._init_brightness()
        self._init_audio()

    # -- volume -------------------------------------------------------------
    def _init_volume(self) -> None:
        try:
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
            from comtypes import CLSCTX_ALL

            dev = AudioUtilities.GetSpeakers()
            vol = getattr(dev, "EndpointVolume", None)
            if vol is None:  # pycaw < 20240210
                iface = dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
                vol = ctypes.cast(iface, ctypes.POINTER(IAudioEndpointVolume))
            self._vol = vol
            self.volume_ok = True
            LOG.info("volume: ready")
        except Exception as exc:  # pragma: no cover
            LOG.warning("volume unavailable: %s", exc)

    def volume(self) -> int | None:
        if not self.volume_ok:
            return None
        try:
            return round(self._vol.GetMasterVolumeLevelScalar() * 100)
        except Exception:
            LOG.debug("volume read failed", exc_info=True)
            return None

    def muted(self) -> bool | None:
        if not self.volume_ok:
            return None
        try:
            return bool(self._vol.GetMute())
        except Exception:
            return None

    def set_volume(self, value: int) -> None:
        value = max(0, min(100, int(value)))
        if self.simulate or not self.volume_ok:
            return
        self._vol.SetMasterVolumeLevelScalar(value / 100.0, None)

    def set_mute(self, value: bool) -> None:
        if self.simulate or not self.volume_ok:
            return
        self._vol.SetMute(1 if value else 0, None)

    # -- brightness ---------------------------------------------------------
    def _init_brightness(self) -> None:
        try:
            import comtypes.client

            # Early binding is required here: pywin32's dynamic dispatch passes
            # plain ints, and WMI rejects WmiSetBrightness with "Invalid
            # parameter" because it wants a uint32 timeout and a uint8 level.
            comtypes.client.GetModule("C:/Windows/System32/wbem/wbemdisp.tlb")
            locator = comtypes.client.CreateObject("WbemScripting.SWbemLocator")
            self._wmi_root = locator.ConnectServer(".", "root\\wmi")
            if self.get_brightness() is None:
                raise RuntimeError("no brightness value returned")
            self.brightness_ok = True
            LOG.info("brightness: ready")
        except Exception as exc:  # pragma: no cover
            LOG.warning("brightness unavailable: %s", exc)

    def get_brightness(self) -> int | None:
        if not self.brightness_ok and self._brightness_cache[1] is not None:
            return self._brightness_cache[1]
        stamp, last = self._brightness_cache
        if time.monotonic() - stamp < 0.7:
            return last
        try:
            for item in self._wmi_root.ExecQuery(
                "SELECT CurrentBrightness FROM WmiMonitorBrightness"
            ):
                value = int(item.Properties_.Item("CurrentBrightness").Value)
                self._brightness_cache = (time.monotonic(), value)
                return value
        except Exception:
            LOG.debug("brightness read failed", exc_info=True)
        self._brightness_cache = (time.monotonic(), last)
        return last

    def set_brightness(self, value: int) -> None:
        value = max(1, min(100, int(value)))
        if self.simulate or not self.brightness_ok:
            return
        try:
            for method in self._wmi_root.ExecQuery(
                "SELECT * FROM WmiMonitorBrightnessMethods"
            ):
                params = method.Methods_("WmiSetBrightness").InParameters.SpawnInstance_()
                params.Properties_.Item("Timeout").Value = 1
                params.Properties_.Item("Brightness").Value = value
                method.ExecMethod_("WmiSetBrightness", params)
            self._brightness_cache = (time.monotonic(), value)
        except Exception:
            LOG.debug("brightness write failed", exc_info=True)

    # -- media keys (fallback when there is no media session) ---------------
    def tap_media(self, action: str) -> None:
        if self.simulate:
            return
        self.tap(
            {
                "toggle": VK_MEDIA_PLAY_PAUSE,
                "next": VK_MEDIA_NEXT,
                "prev": VK_MEDIA_PREV,
                "stop": VK_MEDIA_STOP,
            }[action]
        )

    # -- audio output -------------------------------------------------------
    def _init_audio(self) -> None:
        """Switching the default endpoint needs the undocumented IPolicyConfig;
        without it the System tile hides the picker entirely."""
        try:
            from audio import AudioOutput  # local module

            out = AudioOutput(self.simulate)
            if not out.ok:
                LOG.info("audio output: unavailable (%s)", out.error or "unknown")
                return
            self._audio = out
            self.audio_ok = True
            LOG.info("audio output: ready (%d active device(s))", len(out.devices()))
        except Exception as exc:
            LOG.info("audio output: unavailable (%s)", exc)
            self._audio = None

    def audio_outputs(self) -> list[dict]:
        if not (self.audio_ok and self._audio):
            return []
        try:
            return self._audio.devices()
        except Exception:
            LOG.debug("audio device list failed", exc_info=True)
            return []

    def set_audio_output(self, device_id: str) -> bool:
        if not (self.audio_ok and self._audio):
            return False
        try:
            return self._audio.set_default(device_id)
        except Exception:
            LOG.debug("audio switch failed", exc_info=True)
            return False

    # -- "is Stremio running?" ----------------------------------------------
    def stremio_running(self) -> bool:
        """Stremio publishes no SMTC session and ignores media keys, so the UI
        needs to know it is playing to explain why there are no controls."""
        stamp, cached = self._stremio_cache
        if time.monotonic() - stamp < 5.0:
            return cached
        running = False
        try:
            import psutil

            for proc in psutil.process_iter(["name"]):
                name = (proc.info.get("name") or "").lower()
                if name.startswith("stremio"):
                    running = True
                    break
        except Exception:
            LOG.debug("stremio probe failed", exc_info=True)
        self._stremio_cache = (time.monotonic(), running)
        return running

    # -- keys ---------------------------------------------------------------
    @staticmethod
    def tap(vk: int) -> None:
        user32 = ctypes.windll.user32
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)

    # -- power --------------------------------------------------------------
    def power(self, action: str) -> None:
        if self.simulate:
            return
        if action == "lock":
            ctypes.windll.user32.LockWorkStation()
        elif action == "sleep":
            ctypes.windll.powrprof.SetSuspendState(0, 1, 0)
        elif action == "displayoff":
            HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER = 0xFFFF, 0x0112, 0xF170
            ctypes.windll.user32.SendMessageW(
                HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER, 2
            )
        elif action == "signout":
            ctypes.windll.user32.ExitWindowsEx(0, 0)

    # -- apps ---------------------------------------------------------------
    def launch(self, command: str) -> None:
        if self.simulate:
            return
        subprocess.Popen(
            command,
            shell=True,
            cwd=str(Path.home()),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) | 0x00000008,
        )

    # -- open windows -------------------------------------------------------
    def _process_name(self, pid: int) -> str:
        """Executable name for a pid, cached: EnumWindows is called per poll."""
        cached = self._proc_names.get(pid)
        if cached is not None:
            return cached
        name = ""
        if pid:
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if handle:
                try:
                    buf = ctypes.create_unicode_buffer(260)
                    size = ctypes.c_ulong(len(buf))
                    if ctypes.windll.kernel32.QueryFullProcessImageNameW(
                        handle, 0, buf, ctypes.byref(size)
                    ):
                        name = Path(buf.value).stem
                except Exception:
                    LOG.debug("process name lookup failed", exc_info=True)
                finally:
                    ctypes.windll.kernel32.CloseHandle(handle)
        if len(self._proc_names) > 400:
            self._proc_names.clear()
        self._proc_names[pid] = name or "?"
        return self._proc_names[pid]

    def windows(self) -> list[dict]:
        """Visible, titled top-level windows in z-order, frontmost first."""
        if self.simulate:
            return [
                {
                    "hwnd": 1,
                    "app": "sim",
                    "title": "Simulated window",
                    "active": True,
                }
            ]
        user32 = ctypes.windll.user32
        dwmapi = ctypes.windll.dwmapi
        GWL_EXSTYLE = -20
        WS_EX_TOOLWINDOW = 0x00000080
        DWMWA_CLOAKED = 14
        foreground = user32.GetForegroundWindow()
        found: list[dict] = []

        def visit(hwnd, _lparam):
            if len(found) >= 30:
                return False
            if not hwnd or not user32.IsWindowVisible(hwnd):
                return True
            if user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = " ".join(buf.value.split())
            if not title:
                return True
            # UWP apps keep hidden "cloaked" windows around with real titles.
            cloaked = ctypes.c_ulong(0)
            try:
                dwmapi.DwmGetWindowAttribute(
                    hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
                )
            except Exception:
                pass
            if cloaked.value:
                return True
            pid = ctypes.c_ulong(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            found.append(
                {
                    "hwnd": int(hwnd),
                    "app": self._process_name(int(pid.value)),
                    "title": title[:140],
                    "active": hwnd == foreground,
                }
            )
            return True

        try:
            user32.EnumWindows(
                ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(visit),
                0,
            )
        except Exception:
            LOG.debug("window enumeration failed", exc_info=True)
        return found

    def window_title(self, hwnd: int) -> str:
        if self.simulate:
            return "simulated window"
        user32 = ctypes.windll.user32
        if not hwnd or not user32.IsWindow(hwnd):
            return ""
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        return " ".join(buf.value.split())[:140]

    def focus_window(self, hwnd: int) -> str | None:
        """Bring a window to the front. Returns its title, or None on failure.

        Windows refuses SetForegroundWindow from a process that does not own the
        focus; tapping ALT first is the standard way around that lock, and
        AttachThreadInput is the fallback when even that is refused.
        """
        if self.simulate:
            return "simulated window"
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        if not hwnd or not user32.IsWindow(hwnd):
            return None
        title = self.window_title(hwnd)
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.keybd_event(0x12, 0, 0, 0)  # VK_MENU down
        user32.SetForegroundWindow(hwnd)
        user32.keybd_event(0x12, 0, KEYEVENTF_KEYUP, 0)
        if user32.GetForegroundWindow() != hwnd:
            fg_thread = 0
            try:
                fg = user32.GetForegroundWindow()
                if fg:
                    fg_thread = user32.GetWindowThreadProcessId(fg, None)
                current = kernel32.GetCurrentThreadId()
                if fg_thread and fg_thread != current:
                    user32.AttachThreadInput(fg_thread, current, True)
                user32.BringWindowToTop(hwnd)
                user32.SetForegroundWindow(hwnd)
                if fg_thread and fg_thread != current:
                    user32.AttachThreadInput(fg_thread, current, False)
            except Exception:
                LOG.debug("focus fallback failed", exc_info=True)
        return title if user32.GetForegroundWindow() == hwnd else None

    # -- named keyboard shortcuts -------------------------------------------
    def hotkey(self, keys: tuple[str, ...]) -> None:
        """One SendInput batch for a whole chord: modifiers, key, release."""
        if self.simulate:
            return
        mods = [name for name in keys if name in SHORTCUT_MODS]
        main = next((name for name in reversed(keys) if name not in SHORTCUT_MODS), None)
        if main is None or main not in VK_KEYS:
            return
        batch: list[INPUT] = []
        for name in mods:
            batch.append(_key_event(vk=VK_KEYS[name]))
        batch.append(_key_event(vk=VK_KEYS[main]))
        batch.append(_key_event(vk=VK_KEYS[main], flags=KEYEVENTF_KEYUP))
        for name in reversed(mods):
            batch.append(_key_event(vk=VK_KEYS[name], flags=KEYEVENTF_KEYUP))
        send_inputs(batch)

    # -- pointer (relative mouse) -------------------------------------------
    def pointer(self, events: list[dict]) -> None:
        """Apply a batch of validated pointer events in one SendInput call."""
        if self.simulate:
            return
        batch: list[INPUT] = []
        for ev in events:
            kind = ev["t"]
            if kind == "move":
                batch.append(_mouse_event(ev["dx"], ev["dy"], 0, MOUSEEVENTF_MOVE))
            elif kind == "click":
                down, up = CLICK_FLAGS[ev["button"]]
                batch.append(_mouse_event(0, 0, 0, down))
                batch.append(_mouse_event(0, 0, 0, up))
            elif kind in ("down", "up"):
                down, up = CLICK_FLAGS[ev["button"]]
                batch.append(_mouse_event(0, 0, 0, down if kind == "down" else up))
            elif kind == "scroll":
                if ev.get("dy"):
                    batch.append(_mouse_event(0, 0, ev["dy"], MOUSEEVENTF_WHEEL))
                if ev.get("dx"):
                    batch.append(_mouse_event(0, 0, ev["dx"], MOUSEEVENTF_HWHEEL))
        send_inputs(batch)

    # -- typing (unicode keyboard) ------------------------------------------
    def type_text(self, text: str, submit: bool = False) -> None:
        if self.simulate:
            return
        batch: list[INPUT] = []
        for ch in text:
            code = ord(ch)
            if code > 0xFFFF:  # outside the BMP: send the surrogate pair
                code -= 0x10000
                for unit in (0xD800 + (code >> 10), 0xDC00 + (code & 0x3FF)):
                    batch.append(_key_event(scan=unit, flags=KEYEVENTF_UNICODE))
                    batch.append(
                        _key_event(scan=unit, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)
                    )
            else:
                batch.append(_key_event(scan=code, flags=KEYEVENTF_UNICODE))
                batch.append(
                    _key_event(scan=code, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)
                )
        if submit:
            batch.append(_key_event(vk=VK_RETURN))
            batch.append(_key_event(vk=VK_RETURN, flags=KEYEVENTF_KEYUP))
        send_inputs(batch)


class MediaCaps:
    """Windows media sessions (SMTC) plus VLC, which needs its own driver.

    Lives on its own MTA thread: WinRT and the Core Audio COM objects disagree
    about apartment threading, and VLC's playing/paused probe is Core Audio.
    """

    def __init__(self, simulate: bool = False, vlc_cfg: dict | None = None) -> None:
        self.simulate = simulate
        self.ok = False
        self.vlc = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._smtc = None

        cfg = vlc_cfg if isinstance(vlc_cfg, dict) else {}
        if cfg.get("enabled", True):
            try:
                from vlc import VLC  # local module

                self.vlc = VLC(
                    int(cfg.get("http_port") or 8080),
                    str(cfg.get("http_password") or ""),
                )
                LOG.info("vlc: watching (http port %s)", self.vlc.port)
            except Exception:
                LOG.debug("vlc driver unavailable", exc_info=True)

        try:
            from smtc import SMTC  # local module

            smtc = SMTC()
            if not smtc.available:
                LOG.info("media session: unavailable (%s)", smtc.error)
                return
            self._loop = asyncio.new_event_loop()
            self._smtc = smtc
            self._loop.run_until_complete(smtc.state())
            self.ok = True
            LOG.info("media session: ready")
        except Exception as exc:
            LOG.info("media session: unavailable (%s)", exc)
            self._smtc = None
            self._loop = None

    def state(self) -> dict:
        result: dict = {"current": None, "sessions": []}
        if self.ok and self._smtc and self._loop:
            try:
                result = self._loop.run_until_complete(self._smtc.state())
            except Exception:
                LOG.debug("media state failed", exc_info=True)

        row = None
        if self.vlc is not None:
            try:
                row = self.vlc.state()
            except Exception:
                LOG.debug("vlc state failed", exc_info=True)
        if row is not None:
            sessions = list(result.get("sessions") or [])
            sessions.append(row)
            result = dict(result, sessions=sessions)
            if not result.get("current"):
                row["current"] = True
                result["current"] = row
        return result

    def snapshot(self) -> dict | None:
        return self.state()["current"]

    @staticmethod
    def _is_vlc(target: dict | None) -> bool:
        app = str((target or {}).get("app") or "").lower()
        return "vlc" in app

    def command(self, action: str, target: dict | None = None):
        """Returns ``(status, resolved)``; see ``smtc.SMTC.command``."""
        if self.simulate:
            return "ok", None
        if self._is_vlc(target) and self.vlc is not None:
            try:
                return self.vlc.command(action, target)
            except Exception:
                LOG.debug("vlc command failed", exc_info=True)
                return "error", None
        if self.ok and self._smtc and self._loop:
            try:
                status, used = self._loop.run_until_complete(
                    self._smtc.command(action, target)
                )
            except Exception:
                LOG.debug("media command failed", exc_info=True)
                status, used = "error", None
            if status != "none":
                return status, used
        if self.vlc is not None:
            try:
                return self.vlc.command(action, target)
            except Exception:
                LOG.debug("vlc command failed", exc_info=True)
        return "none", None

    def seek(self, position: float, target: dict | None = None):
        if self.simulate:
            return "ok", None
        if self._is_vlc(target) and self.vlc is not None:
            try:
                return self.vlc.seek(position, target)
            except Exception:
                LOG.debug("vlc seek failed", exc_info=True)
                return "error", None
        if self.ok and self._smtc and self._loop:
            try:
                status, used = self._loop.run_until_complete(
                    self._smtc.seek(position, target)
                )
            except Exception:
                LOG.debug("media seek failed", exc_info=True)
                status, used = "error", None
            if status != "none":
                return status, used
        if self.vlc is not None:
            try:
                return self.vlc.seek(position, target)
            except Exception:
                LOG.debug("vlc seek failed", exc_info=True)
        return "none", None

    def skip(self, delta: float, target: dict | None = None):
        if self.simulate:
            return "ok", None
        if self._is_vlc(target) and self.vlc is not None:
            try:
                return self.vlc.skip(delta, target)
            except Exception:
                LOG.debug("vlc skip failed", exc_info=True)
                return "error", None
        if self.ok and self._smtc and self._loop:
            try:
                status, used = self._loop.run_until_complete(
                    self._smtc.skip(delta, target)
                )
            except Exception:
                LOG.debug("media skip failed", exc_info=True)
                status, used = "error", None
            if status != "none":
                return status, used
        if self.vlc is not None:
            try:
                return self.vlc.skip(delta, target)
            except Exception:
                LOG.debug("vlc skip failed", exc_info=True)
        return "none", None


class WindowsWorker:
    """Serialises every COM/SendInput call, on one thread per apartment."""

    def __init__(self, simulate: bool, vlc_cfg: dict | None = None) -> None:
        self._sys = ThreadPoolExecutor(max_workers=1, thread_name_prefix="win-sys")
        self._media = ThreadPoolExecutor(max_workers=1, thread_name_prefix="win-media")
        # Input lives on its own thread: the system executor also runs the
        # once-a-second volume/brightness snapshot, and a slow WMI read there
        # must never sit in front of a cursor move.
        self._input = ThreadPoolExecutor(max_workers=1, thread_name_prefix="win-input")
        self.caps: Caps = self._sys.submit(Caps, simulate).result()
        self.media: MediaCaps = self._media.submit(
            self._boot_media, simulate, vlc_cfg
        ).result()

    @staticmethod
    def _boot_media(simulate: bool, vlc_cfg: dict | None = None) -> MediaCaps:
        try:
            ctypes.windll.ole32.CoInitializeEx(None, 0x0000)  # MTA
        except Exception:
            pass
        return MediaCaps(simulate, vlc_cfg)

    async def run(self, fn, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._sys, lambda: fn(*args))

    async def run_media(self, fn, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._media, lambda: fn(*args))

    async def run_input(self, fn, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._input, lambda: fn(*args))


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------

class Auth:
    def __init__(self, config: dict) -> None:
        self.cfg = config["security"]
        self.state = _read_json(STATE_FILE, {"devices": {}, "pin": None})
        self.state.setdefault("devices", {})
        configured = str(self.cfg.get("pin") or "").strip()
        if self.cfg.get("pin_enabled", True):
            self.pin = configured if configured else self._new_pin()
        else:
            self.pin = None
        self._attempts: list[float] = []
        self._lock_until = 0.0
        # Last time each device's last_seen was flushed to disk. Persisting on
        # every request meant one temp-file rename per pointer frame.
        self._flushed: dict[str, int] = {}

    @staticmethod
    def _new_pin() -> str:
        return f"{secrets.randbelow(1_000_000):06d}"

    def rotate_pin(self) -> str:
        self.pin = self._new_pin()
        self._attempts.clear()
        self._lock_until = 0.0
        return self.pin

    @staticmethod
    def hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def record(self, device: str, login: str) -> None:
        entry = self.state["devices"].get(device)
        if not entry:
            return
        now = int(time.time())
        entry["last_seen"] = now
        entry["login"] = login
        # Memory is authoritative; the file is flushed at most every 10 s (and
        # on pair/revoke), which is plenty for "last seen" and keeps a 60 Hz
        # pointer stream from rewriting state.json 60 times a second.
        if now - self._flushed.get(device, 0) < 10:
            return
        self._flushed[device] = now
        _write_json(STATE_FILE, self.state)

    def check(self, token: str | None) -> dict | None:
        if not token:
            return None
        return self.state["devices"].get(self.hash_token(token))

    def pair(self, pin: str, name: str, login: str, ip: str = "-") -> str | None:
        now = time.monotonic()
        if now < self._lock_until:
            return None
        window = float(self.cfg.get("pairing_window_minutes", 10)) * 6.0
        self._attempts = [t for t in self._attempts if now - t < window]
        if len(self._attempts) >= 6:
            self._lock_until = now + 300
            audit("pair_lockout", f"{len(self._attempts)} attempts", name, login, ip)
            return None
        if not self.pin or not secrets.compare_digest(pin or "", self.pin):
            self._attempts.append(now)
            audit("pair_fail", "wrong PIN", name, login, ip)
            return None
        token = secrets.token_urlsafe(32)
        self.state["devices"][self.hash_token(token)] = {
            "name": (name or "device")[:40],
            "created": int(time.time()),
            "last_seen": int(time.time()),
            "login": login,
            "scope": "full",
        }
        _write_json(STATE_FILE, self.state)
        audit("pair_ok", "device paired", name, login, ip)
        return token

    def revoke(self, digest: str, ip: str = "-") -> bool:
        if digest in self.state["devices"]:
            name = self.state["devices"][digest].get("name", "?")
            del self.state["devices"][digest]
            _write_json(STATE_FILE, self.state)
            audit("revoke", f"revoked {digest[:12]}", name, ip=ip)
            return True
        return False

    def devices(self) -> list[dict]:
        out = []
        for digest, entry in self.state["devices"].items():
            out.append(
                {
                    "id": digest[:16],
                    "name": entry.get("name"),
                    "created": entry.get("created"),
                    "last_seen": entry.get("last_seen"),
                }
            )
        return sorted(out, key=lambda d: d.get("last_seen") or 0, reverse=True)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def current_ssid() -> str | None:
    try:
        out = subprocess.run(
            ["netsh", "wlan", "show", "interfaces"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
        for line in out.splitlines():
            if "SSID" in line and "BSSID" not in line and ":" in line:
                return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return None


def tailscale_exe() -> Path | None:
    for candidate in (
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tailscale" / "tailscale.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Tailscale" / "tailscale.exe",
    ):
        if candidate.exists():
            return candidate
    return None


def serve_url(port: int) -> str | None:
    """The URL `tailscale serve` is currently exposing for this port.

    Two shapes are in the wild:

      https://desktop.tailnet.ts.net       serve --bg <port>     (TLS on 443)
      tcp://desktop.tailnet.ts.net:8765    serve --bg --tcp ...  (raw forwarding)

    The TCP form is plain http, but it still travels inside WireGuard, so it is
    tailnet-only just like the https form. Prefer the IPv4 host there: MagicDNS
    is the piece that tends to break on Android, and the address always works.
    """
    exe = tailscale_exe()
    if not exe:
        return None
    try:
        out = subprocess.run(
            [str(exe), "serve", "status"], capture_output=True, text=True, timeout=8
        ).stdout
    except Exception:
        LOG.debug("serve status probe failed", exc_info=True)
        return None

    tcp_hosts: list[str] = []
    for line in out.splitlines():
        # `serve status` trees its entries, so every line but the first can
        # start with "|-- " or "|--> ". Match the URL itself, not the prefix.
        https = re.search(r"https://\S+", line)
        if https:
            return https.group(0).rstrip("/")
        match = re.search(r"tcp://(\[[^\]]+\]|[^\s:]+):(\d+)", line)
        if match and int(match.group(2)) == port:
            host = match.group(1).strip("[]")
            if not host.startswith("127.") and host != "localhost":
                tcp_hosts.append(host)
    if tcp_hosts:
        ipv4 = next((h for h in tcp_hosts if h.count(".") == 3 and h[:1].isdigit()), None)
        return f"http://{ipv4 or tcp_hosts[0]}:{port}"
    return None


def tailscale_info() -> tuple[str | None, str | None]:
    """Return (ipv4, magicdns_name) for this machine's tailnet address."""
    exe = tailscale_exe()
    if exe:
        try:
            out = subprocess.run(
                [str(exe), "status", "--json"],
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout
            data = json.loads(out)
            node = data.get("Self") or {}
            name = (node.get("DNSName") or "").rstrip(".")
            ips = node.get("TailscaleIPs") or []
            ip = next((value for value in ips if ":" not in value), None)
            if ip:
                return ip, (name or None)
        except Exception:
            LOG.debug("tailscale cli probe failed", exc_info=True)
    try:
        import psutil

        for iface, addrs in psutil.net_if_addrs().items():
            if "tailscale" not in iface.lower():
                continue
            for addr in addrs:
                if addr.family == socket.AF_INET and not addr.address.startswith("169.254"):
                    return addr.address, None
    except Exception:
        LOG.debug("psutil probe failed", exc_info=True)
    return None, None


def lan_ip() -> str | None:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("192.168.1.1", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except Exception:
        return None


def tailscale_login(request: Request | WebSocket) -> str:
    """Identity header injected by `tailscale serve`; empty when absent."""
    for key in ("x-tailscale-user-login", "tailscale-user-login"):
        value = request.headers.get(key)
        if value:
            return value
    return ""


def client_ip(request: Request | WebSocket) -> str:
    """The peer address, which over Tailscale is the device's 100.x address."""
    try:
        if request.client and request.client.host:
            return request.client.host
    except Exception:
        pass
    return "-"


# ---------------------------------------------------------------------------
# pointer / typing validation
# ---------------------------------------------------------------------------

class RateLimiter:
    """Batches-per-minute guard so a wedged client cannot saturate the server."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        window = self._hits.setdefault(key, [])
        window[:] = [t for t in window if now - t < 60.0]
        if len(window) >= self.per_minute:
            return False
        window.append(now)
        return True


def _int_in(value: Any, low: int, high: int, field: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be an integer") from None
    if not (low <= number <= high):
        raise ValueError(f"{field} must be between {low} and {high}")
    return number


def clean_pointer_events(raw: Any) -> list[dict]:
    """Validate a client batch. Raises ValueError with a human-readable reason."""
    if not isinstance(raw, list) or not raw:
        raise ValueError("events must be a non-empty list")
    if len(raw) > 200:
        raise ValueError("at most 200 events per batch")
    out: list[dict] = []
    for ev in raw:
        if not isinstance(ev, dict):
            raise ValueError("each event must be an object")
        kind = str(ev.get("t") or "")
        if kind == "move":
            out.append(
                {
                    "t": "move",
                    "dx": _int_in(ev.get("dx", 0), -500, 500, "dx"),
                    "dy": _int_in(ev.get("dy", 0), -500, 500, "dy"),
                }
            )
        elif kind in ("click", "down", "up"):
            button = str(ev.get("button") or "")
            if button not in CLICK_FLAGS:  # left / right only, no middle
                raise ValueError("button must be 'left' or 'right'")
            out.append({"t": kind, "button": button})
        elif kind == "scroll":
            dy = _int_in(ev.get("dy", 0), -1200, 1200, "dy")
            dx = _int_in(ev.get("dx", 0), -1200, 1200, "dx")
            if not dy and not dx:
                continue
            out.append({"t": "scroll", "dy": dy, "dx": dx})
        else:
            raise ValueError(f"unknown event type {kind!r}")
    return out


def clean_typed_text(raw: Any) -> str:
    if not isinstance(raw, str):
        raise ValueError("text must be a string")
    if len(raw) > 500:
        raise ValueError("at most 500 characters per send")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        raise ValueError("control characters are not allowed")
    return raw


# ---------------------------------------------------------------------------
# app
# ---------------------------------------------------------------------------

class BasePathMiddleware:
    """Serve the app under a prefix.

    Works whether or not the reverse proxy strips the prefix before forwarding:
    if the incoming path starts with the base we trim it, otherwise we pass the
    request through untouched. That makes `tailscale serve --set-path` and a
    direct visit behave identically.
    """

    def __init__(self, app, base: str) -> None:
        self.app = app
        self.base = base

    async def __call__(self, scope, receive, send):
        if self.base and scope.get("type") in ("http", "websocket"):
            path = scope.get("path", "")
            if path == self.base or path.startswith(self.base + "/"):
                scope = dict(scope)
                scope["path"] = path[len(self.base):] or "/"
                scope["root_path"] = self.base
        await self.app(scope, receive, send)


def build_app(config: dict, worker: WindowsWorker, auth: Auth, args, base_path: str = "") -> FastAPI:
    clients: set[WebSocket] = set()
    last_snapshot: dict[str, Any] = {}
    base = normalise_base(base_path)
    layout = resolve_layout(config)
    for warning in layout["warnings"]:
        LOG.warning("layout: %s", warning)
    # 120 Hz tablets produce 7200 batches/min; 6000 used to lock them out after
    # ~50 s of dragging. 12000 still bounds a wedged client to ~200 batches/s.
    pointer_limiter = RateLimiter(12000)
    # Rate-limit refusals are logged at most once per device per 10 s, so a
    # client stuck at 120 Hz cannot turn a 429 into an audit storm.
    pointer_deny_logged: dict[str, float] = {}

    def note_pointer_denied(device: str, login: str, ip: str) -> None:
        now = time.monotonic()
        if now - pointer_deny_logged.get(device, -1e9) < 10:
            return
        pointer_deny_logged[device] = now
        audit("action_fail", "pointer: rate limited", device, login, ip)

    def audit_pointer_clicks(events: list[dict], device: str, login: str, ip: str) -> None:
        """Clicks are audited; moves and scrolls are not (they would flood)."""
        clicks: dict[str, int] = {}
        for ev in events:
            if ev["t"] == "click":
                clicks[ev["button"]] = clicks.get(ev["button"], 0) + 1
        if clicks:
            audit(
                "pointer",
                ", ".join(f"{button} x{count}" for button, count in clicks.items()),
                device,
                login,
                ip,
            )

    type_limiter = RateLimiter(60)
    shortcut_limiter = RateLimiter(240)  # ~4 taps/s; nobody types faster on glass
    focus_limiter = RateLimiter(60)
    index_html = (STATIC / "index.html").read_text("utf-8").replace(
        '<base href="/">', f'<base href="{base}/">'
    )

    async def snapshot() -> dict[str, Any]:
        caps = worker.caps
        try:
            media_state = await asyncio.wait_for(worker.run_media(worker.media.state), timeout=5.0)
        except asyncio.TimeoutError:
            LOG.warning("media state timed out")
            media_state = {"current": None, "sessions": []}
        sessions = media_state.get("sessions") or []
        for row in sessions:
            row["art"] = stash_art(row.get("art"), base)
        if caps.audio_ok:
            try:
                audio_outputs = await asyncio.wait_for(worker.run(caps.audio_outputs), timeout=3.0)
            except asyncio.TimeoutError:
                LOG.warning("audio device list timed out")
                audio_outputs = []
        else:
            audio_outputs = []
        return {
            "host": os.environ.get("COMPUTERNAME", "windows"),
            "volume": await worker.run(caps.volume),
            "muted": await worker.run(caps.muted),
            "brightness": await worker.run(caps.get_brightness),
            "caps": {
                "volume": caps.volume_ok,
                "brightness": caps.brightness_ok,
                "media": worker.media.ok,
                "vlc": worker.media.vlc is not None,
                "audio_out": caps.audio_ok,
            },
            "audio_outputs": audio_outputs,            "media": media_state.get("current"),
            "media_sessions": sessions,
            "stremio": await worker.run(caps.stremio_running),
            "apps": [
                {"id": key, "label": meta.get("label", key)}
                for key, meta in config["apps"].items()
                if isinstance(meta, dict) and meta.get("command")
            ],
            "power": list(POWER_ACTIONS),
            "simulate": args.simulate,
        }

    async def broadcast() -> None:
        nonlocal last_snapshot
        try:
            state = await snapshot()
        except Exception:
            LOG.debug("snapshot failed", exc_info=True)
            return
        if state == last_snapshot:
            return
        last_snapshot = state
        payload = json.dumps({"type": "state", "state": state})
        dead = []
        for ws in clients:
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            clients.discard(ws)

    async def poller() -> None:
        while True:
            await broadcast()
            await asyncio.sleep(1.0)

    def guard(request: Request) -> dict:
        if auth.pin is None:  # dev mode: loopback, no auth
            return {"name": "local", "scope": "full"}
        token = request.headers.get("x-control-token") or request.query_params.get("t")
        entry = auth.check(token)
        if entry is None:
            audit(
                "denied",
                f"{request.method} {request.url.path}",
                ip=client_ip(request),
            )
            raise HTTPException(status_code=401, detail="not paired")
        login = tailscale_login(request)
        allowed = auth.cfg.get("allowed_logins") or []
        if allowed and login and login not in allowed:
            audit(
                "denied",
                f"{request.method} {request.url.path} (login {login} not allowed)",
                entry.get("name", "?"),
                login,
                client_ip(request),
            )
            raise HTTPException(status_code=403, detail="login not allowed")
        auth.record(auth.hash_token(token or ""), login)
        return entry

    def guard_ws(ws: WebSocket) -> dict | None:
        entry = auth.check(ws.query_params.get("token"))
        if entry is None:
            return None
        return entry

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        task = asyncio.create_task(poller())
        try:
            yield
        finally:
            task.cancel()

    app = FastAPI(
        title="Laptop Remote",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )

    @app.middleware("http")
    async def revalidate_static(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            # Always revalidate: ETag makes it a 304 when unchanged, and it
            # stops a stale app.js surviving a server restart.
            response.headers["cache-control"] = "no-cache"
        return response

    # -- static ------------------------------------------------------------
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    @app.get("/")
    async def index():
        return HTMLResponse(index_html, headers={"cache-control": "no-store"})

    @app.get("/manifest.webmanifest")
    async def manifest():
        return JSONResponse(
            {
                "name": "Laptop Remote",
                "short_name": "Remote",
                "start_url": f"{base}/",
                "scope": f"{base}/",
                "display": "standalone",
                "orientation": "any",
                "background_color": "#0a0c10",
                "theme_color": "#0a0c10",
                "description": "Control this Windows laptop from a phone or tablet.",
                "icons": [
                    {
                        "src": f"{base}/static/icon.svg",
                        "sizes": "any",
                        "type": "image/svg+xml",
                        "purpose": "any maskable",
                    }
                ],
            }
        )

    @app.get("/sw.js")
    async def service_worker():
        return FileResponse(
            STATIC / "sw.js", headers={"cache-control": "no-store"}
        )

    # -- auth --------------------------------------------------------------
    @app.get("/api/status")
    async def status(request: Request):
        login = tailscale_login(request)
        return {
            "paired": auth.check(request.headers.get("x-control-token")) is not None,
            "pin_enabled": auth.pin is not None,
            "host": os.environ.get("COMPUTERNAME", "windows"),
            "login": login,
            "simulate": args.simulate,
        }

    @app.post("/api/pair")
    async def pair(request: Request):
        body = await request.json()
        pin = str(body.get("pin", "")).strip()
        name = str(body.get("name", "")).strip() or "device"
        login = tailscale_login(request)
        token = auth.pair(pin, name, login, client_ip(request))
        if not token:
            raise HTTPException(status_code=403, detail="wrong PIN or locked out")
        return {"token": token, "name": name}

    @app.get("/api/state")
    async def get_state(request: Request):
        guard(request)
        return await snapshot()

    @app.get("/api/layout")
    async def get_layout(request: Request):
        guard(request)
        return layout

    # -- open windows -------------------------------------------------------
    @app.get("/api/windows")
    async def get_windows(request: Request):
        """Visible top-level windows, polled by the Apps tile while it is open.

        Deliberately not part of the 1 Hz state snapshot: window titles change
        constantly (a video timestamp, a document name) and would turn every
        state comparison into a broadcast.
        """
        guard(request)
        try:
            wins = await asyncio.wait_for(worker.run(worker.caps.windows), timeout=3.0)
        except asyncio.TimeoutError:
            LOG.warning("window enumeration timed out")
            wins = []
        return {"ok": True, "windows": wins}

    @app.get("/api/art/{key}")
    async def art(key: str, request: Request):
        guard(request)
        hit = art_bytes(key)
        if hit is None:
            raise HTTPException(status_code=404, detail="no artwork")
        raw, mime = hit
        # Content-addressed: the URL changes when the image does.
        return Response(
            raw,
            media_type=mime,
            headers={"cache-control": "public, max-age=604800, immutable"},
        )

    @app.get("/api/devices")
    async def devices(request: Request):
        guard(request)
        return {"devices": auth.devices()}

    @app.post("/api/devices/revoke")
    async def revoke(request: Request):
        guard(request)
        body = await request.json()
        wanted = str(body.get("id", ""))
        for digest in list(auth.state["devices"]):
            if digest.startswith(wanted):
                ok = auth.revoke(digest, client_ip(request))
                return {"revoked": ok}
        return {"revoked": False}

    # -- actions -----------------------------------------------------------
    @app.post("/api/action")
    async def action(request: Request):
        entry = guard(request)
        login = tailscale_login(request)
        device = entry.get("name", "?")
        ip = client_ip(request)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="bad json")
        kind = str(body.get("type", ""))
        caps = worker.caps
        detail = ""

        if kind == "volume":
            value = int(body.get("value", 50))
            await worker.run(caps.set_volume, value)
            detail = str(value)
        elif kind == "volume_step":
            current = await worker.run(caps.volume) or 0
            value = max(0, min(100, current + int(body.get("delta", 0))))
            await worker.run(caps.set_volume, value)
            detail = str(value)
        elif kind == "mute":
            value = bool(body.get("value", True))
            await worker.run(caps.set_mute, value)
            detail = str(value)
        elif kind == "brightness":
            value = int(body.get("value", 50))
            await worker.run(caps.set_brightness, value)
            detail = str(value)
        elif kind == "brightness_step":
            current = await worker.run(caps.get_brightness) or 0
            value = max(1, min(100, current + int(body.get("delta", 0))))
            await worker.run(caps.set_brightness, value)
            detail = str(value)
        elif kind == "media":
            name = str(body.get("action", "toggle"))
            target = body.get("session")
            if not isinstance(target, dict):
                target = None
            if name == "seek":
                try:
                    position = float(body.get("position"))
                except (TypeError, ValueError):
                    audit("action_fail", "media seek (bad position)", device, login, ip)
                    raise HTTPException(status_code=400, detail="bad position")
                if position < 0:
                    audit("action_fail", "media seek (negative position)", device, login, ip)
                    raise HTTPException(status_code=400, detail="bad position")
                status, used = await worker.run_media(worker.media.seek, position, target)
                if status == "none":
                    audit("action_fail", "media seek (no session)", device, login, ip)
                    raise HTTPException(status_code=409, detail="nothing is playing")
                if status == "error":
                    audit("action_fail", "media seek rejected by session", device, login, ip)
                    raise HTTPException(status_code=409, detail="that app refused the seek")
                where = session_where(used, target)
                detail = f"seek {position:.1f}s{where}"
                audit(kind, detail, device, login, ip)
                asyncio.create_task(broadcast())
                return {"ok": True, "simulate": args.simulate}
            if name == "skip":
                try:
                    delta = float(body.get("delta"))
                except (TypeError, ValueError):
                    audit("action_fail", "media skip (bad delta)", device, login, ip)
                    raise HTTPException(status_code=400, detail="bad delta")
                if not delta or abs(delta) > 60:
                    audit("action_fail", "media skip (out of range)", device, login, ip)
                    raise HTTPException(status_code=400, detail="bad delta")
                status, used = await worker.run_media(worker.media.skip, delta, target)
                if status == "none":
                    audit("action_fail", "media skip (no session)", device, login, ip)
                    raise HTTPException(status_code=409, detail="nothing is playing")
                if status == "error":
                    audit("action_fail", "media skip rejected by session", device, login, ip)
                    raise HTTPException(status_code=409, detail="that app refused the skip")
                detail = f"skip {delta:+.0f}s{session_where(used, target)}"
                audit(kind, detail, device, login, ip)
                asyncio.create_task(broadcast())
                return {"ok": True, "simulate": args.simulate}
            if name not in ("toggle", "next", "prev", "stop"):
                audit("action_fail", f"media {name!r}", device, login, ip)
                raise HTTPException(status_code=400, detail="bad media action")
            status, used = await worker.run_media(worker.media.command, name, target)
            if status == "none":
                # No SMTC session at all: this is the only case where a blind,
                # global media key is safe. With a session present it could
                # pause something else entirely (Stremio is deaf to them).
                await worker.run(caps.tap_media, name)
                detail = f"{name} (blind media key)"
            elif status == "error":
                audit("action_fail", f"media {name} rejected by session", device, login, ip)
                raise HTTPException(status_code=409, detail="that app refused the command")
            else:
                detail = f"{name}{session_where(used, target)}"
        elif kind == "power":
            name = str(body.get("action", ""))
            if name not in POWER_ACTIONS:
                audit("action_fail", f"power {name!r}", device, login, ip)
                raise HTTPException(status_code=400, detail="bad power action")
            await worker.run(caps.power, name)
            detail = name
        elif kind == "launch":
            app_id = str(body.get("id", ""))
            meta = config["apps"].get(app_id)
            if not isinstance(meta, dict) or not meta.get("command"):
                audit("action_fail", f"launch {app_id!r} (no such app)", device, login, ip)
                raise HTTPException(status_code=404, detail="unknown app")
            await worker.run(caps.launch, meta["command"])
            detail = f"{app_id}: {meta['command']}"
        elif kind == "shortcut":
            name = str(body.get("id", ""))
            keys = SHORTCUTS.get(name)
            if keys is None:
                audit("action_fail", f"shortcut {name!r}", device, login, ip)
                raise HTTPException(status_code=400, detail="unknown shortcut")
            if not shortcut_limiter.allow(device):
                audit("action_fail", f"shortcut {name}: rate limited", device, login, ip)
                raise HTTPException(status_code=429, detail="too many shortcuts")
            await worker.run_input(caps.hotkey, keys)
            detail = "+".join(keys)
        elif kind == "focus":
            try:
                hwnd = int(body.get("hwnd"))
            except (TypeError, ValueError):
                audit("action_fail", "focus (bad hwnd)", device, login, ip)
                raise HTTPException(status_code=400, detail="bad window")
            if not focus_limiter.allow(device):
                audit("action_fail", "focus: rate limited", device, login, ip)
                raise HTTPException(status_code=429, detail="too many window switches")
            title = await worker.run_input(caps.focus_window, hwnd)
            if not title:
                audit("action_fail", f"focus {hwnd} (window gone or refused)", device, login, ip)
                raise HTTPException(status_code=409, detail="that window is gone")
            detail = title
        else:
            audit("action_fail", f"unknown type {kind!r}", device, login, ip)
            raise HTTPException(status_code=400, detail="unknown action")

        audit(kind, detail, device, login, ip)
        asyncio.create_task(broadcast())
        return {"ok": True, "simulate": args.simulate}

    # -- pointer -----------------------------------------------------------
    @app.post("/api/pointer")
    async def pointer(request: Request):
        entry = guard(request)
        login = tailscale_login(request)
        device = entry.get("name", "?")
        ip = client_ip(request)
        try:
            body = await request.json()
            events = clean_pointer_events(body.get("events"))
        except ValueError as exc:
            audit("action_fail", f"pointer: {exc}", device, login, ip)
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception:
            audit("action_fail", "pointer: bad json", device, login, ip)
            raise HTTPException(status_code=400, detail="bad json")

        if not pointer_limiter.allow(device):
            note_pointer_denied(device, login, ip)
            raise HTTPException(status_code=429, detail="too many pointer batches")

        try:
            await worker.run_input(worker.caps.pointer, events)
        except Exception as exc:
            LOG.debug("pointer failed", exc_info=True)
            audit("action_fail", f"pointer: {exc}", device, login, ip)
            raise HTTPException(status_code=500, detail="input was blocked")

        # Clicks are audited; moves and scrolls are not (they would flood the log).
        audit_pointer_clicks(events, device, login, ip)
        return {"ok": True}

    # -- typing ------------------------------------------------------------
    @app.post("/api/type")
    async def type_text(request: Request):
        entry = guard(request)
        login = tailscale_login(request)
        device = entry.get("name", "?")
        ip = client_ip(request)
        try:
            body = await request.json()
            text = clean_typed_text(body.get("text", ""))
        except ValueError as exc:
            audit("action_fail", f"type: {exc}", device, login, ip)
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception:
            audit("action_fail", "type: bad json", device, login, ip)
            raise HTTPException(status_code=400, detail="bad json")

        submit = bool(body.get("submit"))
        if not text and not submit:
            raise HTTPException(status_code=400, detail="nothing to send")
        if not type_limiter.allow(device):
            audit("action_fail", "type: rate limited", device, login, ip)
            raise HTTPException(status_code=429, detail="too many sends")

        try:
            await worker.run_input(worker.caps.type_text, text, submit)
        except Exception as exc:
            LOG.debug("type failed", exc_info=True)
            audit("action_fail", f"type: {exc}", device, login, ip)
            raise HTTPException(status_code=500, detail="input was blocked")

        # Never log the text itself: it may be a password or a search query.
        audit("type", f"{len(text)} chars" + (" + enter" if submit else ""), device, login, ip)
        return {"ok": True, "chars": len(text)}

    # -- audio output ------------------------------------------------------
    @app.get("/api/audio/outputs")
    async def audio_outputs(request: Request):
        entry = guard(request)
        caps = worker.caps
        return {
            "supported": caps.audio_ok,
            "devices": await worker.run(caps.audio_outputs),
        }

    @app.post("/api/audio/output")
    async def audio_output(request: Request):
        entry = guard(request)
        login = tailscale_login(request)
        device = entry.get("name", "?")
        ip = client_ip(request)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="bad json")
        wanted = str(body.get("id") or "")
        caps = worker.caps
        if not caps.audio_ok:
            raise HTTPException(status_code=409, detail="this laptop cannot switch audio output")
        rows = await worker.run(caps.audio_outputs)
        match = next((r for r in rows if r["id"] == wanted), None)
        if match is None:
            audit("action_fail", f"audio output {wanted!r} (not an active device)", device, login, ip)
            raise HTTPException(status_code=404, detail="that device is not connected")
        if match["is_default"]:
            return {"ok": True, "unchanged": True}
        if not await worker.run(caps.set_audio_output, wanted):
            audit("action_fail", f"audio output {match['name']} (Windows refused)", device, login, ip)
            raise HTTPException(status_code=502, detail="Windows did not switch the output")
        audit("audio_out", match["name"], device, login, ip)
        await broadcast()
        return {"ok": True}

    # -- live state --------------------------------------------------------
    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        if auth.pin is not None and guard_ws(ws) is None:
            await ws.close(code=4401)
            return
        await ws.accept()
        clients.add(ws)
        try:
            await ws.send_text(json.dumps({"type": "state", "state": await snapshot()}))
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        except Exception:
            LOG.debug("ws error", exc_info=True)
        finally:
            clients.discard(ws)

    # -- touchpad input channel --------------------------------------------
    @app.websocket("/ws-input")
    async def ws_input(ws: WebSocket):
        """Pointer batches over a socket instead of one HTTP request per frame.

        The browser opens this only while the Mouse tile is mounted. Ordering is
        what matters: down/move/up on one TCP stream can never overtake each
        other, which HTTP could (several connections in flight), and a dropped
        or delayed frame costs no reconnection.
        """
        entry = guard_ws(ws) if auth.pin is not None else {"name": "local"}
        if entry is None:
            await ws.close(code=4401)
            return
        device = entry.get("name", "?")
        login = tailscale_login(ws)
        ip = client_ip(ws)
        await ws.accept()
        LOG.debug("input channel open (%s)", device)
        try:
            while True:
                raw = await ws.receive_text()
                if len(raw) > 65536:
                    continue
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                if not isinstance(msg, dict) or msg.get("t") != "pointer":
                    continue
                try:
                    events = clean_pointer_events(msg.get("events"))
                except ValueError as exc:
                    await ws.send_text(json.dumps({"type": "error", "detail": str(exc)}))
                    continue
                if not pointer_limiter.allow(device):
                    note_pointer_denied(device, login, ip)
                    continue
                try:
                    await worker.run_input(worker.caps.pointer, events)
                except Exception as exc:
                    LOG.debug("pointer failed", exc_info=True)
                    audit("action_fail", f"pointer: {exc}", device, login, ip)
                    await ws.send_text(
                        json.dumps({"type": "error", "detail": "input was blocked"})
                    )
                    continue
                audit_pointer_clicks(events, device, login, ip)
        except WebSocketDisconnect:
            pass
        except Exception:
            LOG.debug("input channel error", exc_info=True)
        finally:
            LOG.debug("input channel closed (%s)", device)

    return app


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def banner(config: dict, auth: Auth, args, bind: str, ts_name: str | None = None, base: str = "") -> None:
    port = config["server"]["port"]
    primary: str
    qr_target: str | None = None
    print()
    print("  Laptop Remote")
    print("  " + "-" * 58)
    print(f"  listening   {bind}:{port}")
    print(f"  path        {base or ''}/")
    print(f"  actions     {'SIMULATED (nothing will change)' if args.simulate else 'live'}")

    if bind.startswith("127."):
        served = serve_url(port)
        if served:
            primary = f"{served}{base}"
            qr_target = f"{primary}/"
            print()
            print("  Open this on your phone / tablet:")
            print(f"    {primary}/")
            print("  Tailnet only — Tailscale must be on for those devices.")
        elif ts_name:
            primary = f"http://{ts_name}:{port}{base}"
            print()
            print("  Local test URL")
            print(f"    http://127.0.0.1:{port}{base}/")
            print()
            print("  Not reachable from your devices yet. Run once:")
            print(f"    tailscale serve --bg --tcp {port} {port}")
            print("  or start with launch.ps1, which does it for you.")
        else:
            primary = f"http://127.0.0.1:{port}{base}"
            print()
            print("  Local test URL")
            print(f"    {primary}/")
            print()
            print(f"  From your devices, run once:  tailscale serve --bg --tcp {port} {port}")
    elif bind == "0.0.0.0":
        primary = f"http://{lan_ip() or '<your-ip>'}:{port}{base}"
        qr_target = f"{primary}/"
        print()
        print(f"  LAN URL  {primary}/   (see README for the firewall rule)")
    else:
        primary = f"http://{ts_name or bind}:{port}{base}"
        # The QR deliberately uses the raw IP: the ts.net name needs working DNS,
        # which is exactly what fails on some phones/tablets.
        qr_target = f"http://{bind}:{port}{base}/"
        print()
        print("  Open this on your phone / tablet:")
        if ts_name:
            print(f"    http://{ts_name}:{port}{base}/")
        print(f"    http://{bind}:{port}{base}/")
        print("  WireGuard-encrypted.")
        print("  Scan the QR for the IP link (works even if DNS does not).")

    if auth.pin:
        print()
        print(f"  Pairing PIN  {auth.pin}     (enter it once per device)")
        print("  Revoke devices anytime from the app's Settings screen.")
        if qr_target:
            _maybe_qr(qr_target)
    print("  " + "-" * 58)
    print()


def _maybe_qr(text: str) -> None:
    try:
        import qrcode  # type: ignore

        qr = qrcode.QRCode(border=1)
        qr.add_data(text)
        qr.make()
        qr.print_ascii(invert=True)
    except Exception:
        pass


_console_handler = None


def install_console_exit() -> None:
    """Make closing the console window stop the server.

    Windows can leave a child python process running after its console window is
    closed, which strands a server you can no longer see or Ctrl+C. Handling
    CTRL_CLOSE_EVENT ourselves guarantees the process goes away with its window.
    """
    global _console_handler
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

        def on_console(ctrl_type: int) -> bool:
            # 2 = close, 5 = logoff, 6 = shutdown
            if ctrl_type in (2, 5, 6):
                try:
                    LOG.info("console closed, stopping server")
                    logging.shutdown()
                finally:
                    os._exit(0)
            return False  # Ctrl+C / Ctrl+Break: let the normal handler run

        _console_handler = handler_type(on_console)
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_console_handler, True)
    except Exception:
        LOG.debug("could not install console handler", exc_info=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Laptop Remote server")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--base-path", default=None, help="serve under a path, e.g. /laptop-remote")
    parser.add_argument("--simulate", action="store_true", help="log actions, change nothing")
    parser.add_argument("--lan", action="store_true", help="bind to the LAN address instead of loopback")
    parser.add_argument("--tailscale", action="store_true", help="bind only to the Tailscale address (no serve needed)")
    parser.add_argument("--dev", action="store_true", help="loopback + no auth (local development only)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(message)s",
    )
    # Mirror the log to a file so it survives after the console scrolls away, and
    # so a second launch can show what an already-running server is doing.
    try:
        from logging.handlers import RotatingFileHandler

        filelog = RotatingFileHandler(
            LOG_FILE, maxBytes=512_000, backupCount=2, encoding="utf-8"
        )
        filelog.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
        )
        logging.getLogger().addHandler(filelog)
    except Exception:
        pass
    install_console_exit()
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    if os.name != "nt":
        print("This server only runs on Windows.", file=sys.stderr)
        return 2

    config = load_config()
    if args.port:
        config["server"]["port"] = args.port
    if args.base_path is not None:
        config["server"]["base_path"] = args.base_path
    base = normalise_base(config["server"].get("base_path"))

    if args.dev:
        config["security"]["pin_enabled"] = False

    bind = "127.0.0.1"
    ts_name: str | None = None
    if args.tailscale:
        ts_ip, ts_name = tailscale_info()
        if not ts_ip:
            print(
                "No Tailscale IPv4 address found. Is Tailscale running and logged in?",
                file=sys.stderr,
            )
            return 4
        bind = ts_ip
    elif args.lan:
        allowed = config["security"].get("allowed_ssids") or []
        ssid = current_ssid()
        if allowed and ssid not in allowed:
            print(
                f"Refusing to bind to the network: connected to {ssid!r}, "
                f"which is not in allowed_ssids.",
                file=sys.stderr,
            )
            return 3
        bind = "0.0.0.0"

    worker = WindowsWorker(simulate=args.simulate, vlc_cfg=config.get("vlc"))
    auth = Auth(config)
    app = build_app(config, worker, auth, args, base)
    banner(config, auth, args, bind, ts_name, base)

    asgi = BasePathMiddleware(app, base) if base else app
    uvicorn.run(
        asgi,
        host=bind,
        port=config["server"]["port"],
        root_path=base,
        log_level="debug" if args.verbose else "warning",
        access_log=False,
        ws_ping_interval=25,
        ws_ping_timeout=20,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
