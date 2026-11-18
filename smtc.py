"""Best-effort wrapper around the Windows media session API (SMTC).

Everything here is optional. If the WinRT projection is missing, or a call
throws, the caller falls back to blind media-key presses and the UI simply
hides the "now playing" card.

Verified behaviour on this machine (2026-11-16):

* Opera GX / YouTube publishes a session: app `opera.exe`, the video title,
  play/pause and stop enabled, next/prev disabled for a lone video.
* Stremio 4 publishes **no** session at all (Qt + in-process mpv), ignores the
  global media keys, and ignores a focused spacebar. Windows routes a media key
  to the SMTC session, so pressing pause while Stremio plays pauses YouTube
  instead. That is why blind keys are only used when there are zero sessions.
"""
from __future__ import annotations

import base64
import datetime as _dt


class SMTC:
    def __init__(self) -> None:
        self.available = False
        self.error: str | None = None
        self._manager = None
        self._art: dict[tuple, str | None] = {}
        try:
            from winrt.windows.media.control import (
                GlobalSystemMediaTransportControlsSessionManager as Manager,
                GlobalSystemMediaTransportControlsSessionPlaybackStatus as Status,
            )

            self._Manager = Manager
            self._Status = Status
            self.available = True
        except Exception as exc:  # pragma: no cover
            self.error = repr(exc)

    async def _manager_async(self):
        if self._manager is None:
            self._manager = await self._Manager.request_async()
        return self._manager

    async def _art_for(self, info) -> str | None:
        key = (info.title, info.artist)
        if key in self._art:
            return self._art[key]
        data_url: str | None = None
        try:
            thumb = info.thumbnail
            if thumb is not None:
                stream = await thumb.open_read_async()
                size = int(stream.size)
                if 0 < size <= 1_500_000:
                    from winrt.windows.storage.streams import DataReader

                    reader = DataReader(stream)
                    await reader.load_async(size)
                    buffer = reader.read_buffer(size)
                    raw = bytes(memoryview(buffer))
                    data_url = "data:image/jpeg;base64," + base64.b64encode(raw).decode()
        except Exception:
            data_url = None
        if len(self._art) > 24:
            self._art.clear()
        self._art[key] = data_url
        return data_url

    async def _describe(self, session, with_art: bool, current: bool) -> dict | None:
        try:
            info = await session.try_get_media_properties_async()
        except Exception:
            return None
        if info is None:
            return None

        playing = False
        controls = None
        try:
            playback = session.get_playback_info()
            if playback is not None:
                playing = playback.playback_status == self._Status.PLAYING
                controls = playback.controls
        except Exception:
            pass

        def can(name: str) -> bool:
            if controls is None:
                return False
            try:
                return bool(getattr(controls, name))
            except Exception:
                return False

        position = duration = None
        min_seek = max_seek = None
        try:
            timeline = session.get_timeline_properties()
            if timeline is not None:
                end = timeline.end_time
                duration = max(0.0, end.total_seconds()) if end else None
                pos = timeline.position
                if pos is not None:
                    seconds = pos.total_seconds()
                    stamp = timeline.last_updated_time
                    if playing and stamp is not None:
                        now = _dt.datetime.now(_dt.timezone.utc)
                        seconds += max(0.0, (now - stamp).total_seconds())
                    position = max(0.0, seconds)
                try:
                    min_seek = max(0.0, timeline.min_seek_time.total_seconds())
                except Exception:
                    min_seek = 0.0
                try:
                    max_seek = timeline.max_seek_time.total_seconds()
                    if max_seek <= 0 and duration:
                        max_seek = duration
                except Exception:
                    max_seek = duration
        except Exception:
            pass

        title = (info.title or "").strip() or None
        artist = (info.artist or "").strip() or None
        app = getattr(session, "source_app_user_model_id", None) or "?"
        return {
            "id": f"{app}|{title or ''}",
            "app": app,
            "title": title,
            "artist": artist,
            "album": (info.album_title or "").strip() or None,
            "playing": playing,
            "position": round(position, 1) if position is not None else None,
            "duration": round(duration, 1) if duration else None,
            "can": {
                "play_pause": can("is_play_pause_toggle_enabled"),
                "next": can("is_next_enabled"),
                "prev": can("is_previous_enabled"),
                "stop": can("is_stop_enabled"),
                "seek": can("is_playback_position_enabled"),
                "skip": can("is_playback_position_enabled"),
            },
            "current": current,
            "art": await self._art_for(info) if with_art else None,
            "min_seek": round(min_seek, 1) if min_seek is not None else None,
            "max_seek": round(max_seek, 1) if max_seek else None,
        }

    async def state(self) -> dict:
        """One manager round trip for both the current session and the list."""
        empty = {"current": None, "sessions": []}
        if not self.available:
            return empty
        try:
            manager = await self._manager_async()
            sessions = list(manager.get_sessions())
        except Exception:
            return empty

        current = manager.get_current_session()
        current_key = None
        if current is not None:
            current_key = getattr(current, "source_app_user_model_id", None)

        rows: list[dict] = []
        for index, session in enumerate(sessions):
            # Decode art for every session: the card stacks them all now, and
            # the server turns each image into a cached /api/art/<hash> URL, so
            # the extra work happens once per track, not once per poll.
            is_current = index == 0 and current_key is None
            if current_key is not None:
                try:
                    is_current = getattr(session, "source_app_user_model_id", None) == current_key
                except Exception:
                    is_current = False
            row = await self._describe(session, with_art=True, current=is_current)
            if row is not None:
                rows.append(row)

        # Exactly one row carries current=True; if the AUMIDs collided, keep the first.
        seen_current = False
        for row in rows:
            if row["current"] and seen_current:
                row["current"] = False
            seen_current = seen_current or row["current"]

        return {"current": next((r for r in rows if r["current"]), rows[0] if rows else None),
                "sessions": rows}

    async def snapshot(self) -> dict | None:
        return (await self.state())["current"]

    async def _title_of(self, session) -> str:
        try:
            info = await session.try_get_media_properties_async()
        except Exception:
            return ""
        return ((info.title or "") if info else "").strip()

    async def _find(self, manager, target: dict | None):
        """Return ``(session, title)`` for an exact app + title match."""
        if not target:
            return None, ""
        want_app = str(target.get("app") or "")
        want_title = str(target.get("title") or "").strip()
        for session in manager.get_sessions():
            try:
                if getattr(session, "source_app_user_model_id", None) != want_app:
                    continue
            except Exception:
                continue
            title = await self._title_of(session)
            if title == want_title:
                return session, title
        return None, ""

    async def command(self, action: str, target: dict | None = None):
        """Run a transport action. Returns ``(status, resolved)``.

        ``status`` is 'ok', 'none' (no sessions at all) or 'error'. ``resolved``
        is ``{'app', 'title'}`` for the session that actually ran it, or None.
        A vanished target falls back to the current session, and reporting the
        resolved identity keeps the audit log honest about what was controlled.
        """
        if not self.available:
            return "none", None
        try:
            manager = await self._manager_async()
            if not list(manager.get_sessions()):
                return "none", None
            session, title = await self._find(manager, target)
            if session is None:
                session = manager.get_current_session()
                if session is None:
                    return "none", None
                title = await self._title_of(session)
            try:
                app = getattr(session, "source_app_user_model_id", None) or "?"
            except Exception:
                app = "?"
            resolved = {"app": app, "title": title}
            if action == "toggle":
                return ("ok" if await session.try_toggle_play_pause_async() else "error"), resolved
            if action == "next":
                return ("ok" if await session.try_skip_next_async() else "error"), resolved
            if action == "prev":
                return ("ok" if await session.try_skip_previous_async() else "error"), resolved
            if action == "stop":
                fn = getattr(session, "try_stop_async", None)
                if fn is None:
                    return "error", resolved
                return ("ok" if await fn() else "error"), resolved
        except Exception:
            return "error", None
        return "error", None

    async def seek(self, position: float, target: dict | None = None):
        """Seek to ``position`` seconds. Returns ``(status, resolved)``."""
        if not self.available:
            return "none", None
        try:
            manager = await self._manager_async()
            if not list(manager.get_sessions()):
                return "none", None
            session, title = await self._find(manager, target)
            if session is None:
                session = manager.get_current_session()
                if session is None:
                    return "none", None
                title = await self._title_of(session)
            try:
                app = getattr(session, "source_app_user_model_id", None) or "?"
            except Exception:
                app = "?"
            resolved = {"app": app, "title": title}
            ticks = int(max(0.0, position) * 10_000_000)
            try:
                ok = await session.try_change_playback_position_async(ticks)
            except Exception:
                return "error", resolved
            return ("ok" if ok else "error"), resolved
        except Exception:
            return "error", None

    async def skip(self, delta: float, target: dict | None = None):
        """Jump ``delta`` seconds from the current playhead.

        SMTC has no relative seek, so read the timeline (extrapolating while
        playing) and seek to the sum. Returns ``(status, resolved)``.
        """
        if not self.available:
            return "none", None
        try:
            manager = await self._manager_async()
            if not list(manager.get_sessions()):
                return "none", None
            session, title = await self._find(manager, target)
            if session is None:
                session = manager.get_current_session()
                if session is None:
                    return "none", None
                title = await self._title_of(session)
            try:
                app = getattr(session, "source_app_user_model_id", None) or "?"
            except Exception:
                app = "?"
            resolved = {"app": app, "title": title}
            try:
                timeline = session.get_timeline_properties()
                position = timeline.position.total_seconds()
                end = timeline.end_time.total_seconds()
                try:
                    playing = (
                        session.get_playback_info().playback_status
                        == self._Status.PLAYING
                    )
                except Exception:
                    playing = False
                stamp = timeline.last_updated_time
                if playing and stamp is not None:
                    now = _dt.datetime.now(_dt.timezone.utc)
                    position += max(0.0, (now - stamp).total_seconds())
                wanted = max(0.0, position + float(delta))
                if end > 0:
                    wanted = min(end, wanted)
            except Exception:
                return "error", resolved
            try:
                ok = await session.try_change_playback_position_async(
                    int(wanted * 10_000_000)
                )
            except Exception:
                return "error", resolved
            return ("ok" if ok else "error"), resolved
        except Exception:
            return "error", None
