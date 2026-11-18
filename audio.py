"""Switch the Windows default playback device.

Windows has no documented API for changing the default endpoint. The supported
path used by every "sound switcher" utility is the undocumented ``IPolicyConfig``
COM object; pycaw wraps it (with a Vista-era fallback) as
``AudioUtilities.SetDefaultDevice``.

Everything here is best-effort. If the interface cannot be created, the System
tile hides the widget instead of drawing buttons that would always fail.

Only *active* render endpoints are listed: the Sound panel also shows unplugged
and disabled devices, but switching to one of those either fails or produces
silence, which is worse than not offering it.
"""
from __future__ import annotations

import time


class AudioOutput:
    #: Device list cache. Enumeration walks a COM property store per device, so
    #: it is not free, and the state snapshot runs every second or so.
    CACHE_SECONDS = 2.5

    def __init__(self, simulate: bool = False) -> None:
        self.simulate = simulate
        self.ok = False
        self.error: str | None = None
        self._cache: tuple[float, list[dict]] = (0.0, [])
        self._utils = None
        if simulate:
            self.ok = True
            return
        try:
            from pycaw.constants import DEVICE_STATE, EDataFlow
            from pycaw.utils import AudioUtilities

            self._utils = AudioUtilities
            self._flow = EDataFlow.eRender.value
            self._active = DEVICE_STATE.ACTIVE.value
            self._probe()
        except Exception as exc:  # pragma: no cover - depends on the machine
            self.error = repr(exc)
            self.ok = False

    # -- probe --------------------------------------------------------------
    def _probe(self) -> None:
        """A device list is not proof: policy-managed machines expose devices
        but refuse the switch. Creating the policy object is the real test."""
        if not self.devices(force=True):
            self.error = "no active playback devices"
            return
        try:
            import comtypes
            from pycaw.api.policyconfig import IPolicyConfig
            from pycaw.constants import CLSID_CPolicyConfigClient

            comtypes.CoCreateInstance(
                CLSID_CPolicyConfigClient, IPolicyConfig, comtypes.CLSCTX_ALL
            )
        except Exception as exc:  # pragma: no cover - depends on the machine
            self.error = f"IPolicyConfig unavailable: {exc!r}"
            return
        self.ok = True

    # -- reading ------------------------------------------------------------
    def devices(self, force: bool = False) -> list[dict]:
        if self.simulate:
            return [
                {"id": "sim-speakers", "name": "Speakers (simulated)", "is_default": True},
                {"id": "sim-headphones", "name": "Headphones (simulated)", "is_default": False},
            ]
        stamp, cached = self._cache
        if not force and time.monotonic() - stamp < self.CACHE_SECONDS:
            return cached
        rows: list[dict] = []
        try:
            current = self._utils.GetSpeakers()
            current_id = current.id if current is not None else None
        except Exception:
            current_id = None
        try:
            for dev in self._utils.GetAllDevices(self._flow, self._active):
                name = (dev.FriendlyName or "").strip() or "Playback device"
                rows.append(
                    {
                        "id": dev.id,
                        "name": name,
                        "is_default": dev.id == current_id,
                    }
                )
        except Exception:  # pragma: no cover - COM failure
            return cached
        rows.sort(key=lambda d: (not d["is_default"], d["name"].lower()))
        self._cache = (time.monotonic(), rows)
        return rows

    # -- writing ------------------------------------------------------------
    def set_default(self, device_id: str) -> bool:
        """Make ``device_id`` the default for console *and* multimedia.

        Both roles matter: the Sound panel's "default device" is eConsole, while
        most media apps open a stream on eMultimedia. Setting only eConsole
        leaves playback on the old device - the classic half-switch bug.
        """
        if not self.ok:
            return False
        if self.simulate:
            return True
        ids = {d["id"] for d in self.devices(force=True)}
        if device_id not in ids:
            return False
        try:
            from pycaw.constants import ERole

            self._utils.SetDefaultDevice(
                device_id, [ERole.eConsole, ERole.eMultimedia]
            )
        except Exception as exc:  # pragma: no cover - COM failure
            self.error = repr(exc)
            return False
        self._cache = (0.0, [])
        # Give the audio service a moment, then read back what Windows thinks.
        for _ in range(2):
            rows = self.devices(force=True)
            if any(d["id"] == device_id and d["is_default"] for d in rows):
                return True
            time.sleep(0.15)
        self.error = "Windows did not switch the default endpoint"
        return False
