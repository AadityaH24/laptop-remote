# Laptop Remote

Control this Windows laptop from a phone or tablet. A configurable deck of tiles —
media, a touchpad with left/right buttons and shortcut keys, system controls, app
launchers and a window switcher — over Tailscale, so nothing is ever exposed to the
local network and no certificates need installing.

```
laptop-remote\
  server.py          FastAPI server + Windows capability layer
  smtc.py            optional Windows media-session bridge
  config.toml        deck tiles, apps, port, security settings
  static\            the web app (no build step)
  docs\PLANNED-FEATURES.md   plan and decision log
  run.bat            double-click to start
  state.json         paired devices (generated, secrets — do not share)
  audit.log          every action, with device name and timestamp
```
## Features

| Media Controls | Mouse Controls |
|:---:|:---:|
| <img width="280" src="https://github.com/user-attachments/assets/09853697-97bf-4648-beaa-57c9d98b259e" /> | <img width="280" src="https://github.com/user-attachments/assets/8047c47b-5f25-416d-9fb0-44113507d7d5" /> |
| **System Controls** | **App Switcher** |
| <img width="280" src="https://github.com/user-attachments/assets/a6d33be0-aa9b-4c7d-87be-b4ac3f1ced34" /> | <img width="280" src="https://github.com/user-attachments/assets/6ad9f40f-9d5b-41e7-b4c3-9a98eca288bc" /> |


---

## 1. Start it

Double-click `launch.ps1` (right-click → *Run with PowerShell* if Windows refuses).
It waits for the Tailscale address, makes sure `tailscale serve` forwards the port
into your tailnet, then runs the server in that window. Closing the window stops
the server. It never opens a browser.

```
  URL  http://<IP>/laptop-remote/
  Starting Laptop Remote - logs below. Ctrl+C or closing this window stops it.
  ----------------------------------------------------------
  Laptop Remote
  ----------------------------------------------------------
  listening   127.0.0.1:8765
  actions     live

  Open this on your phone / tablet:
    http://<IP>/laptop-remote/
  Tailnet only — Tailscale must be on for those devices.

  Pairing PIN  <PIN | config.toml>     (enter it once per device)
  ----------------------------------------------------------
```

`run.bat` starts the bare server without touching `tailscale serve`; use it for
local work. The PIN comes from `[security] pin` in `config.toml` , so it is the same on every launch and every device. Delete that line to
go back to a random PIN printed at startup.

Open `http://127.0.0.1:8765/laptop-remote/` in Chrome **on the laptop** to try it
immediately. Nothing is reachable on your network — the server only listens on
loopback, and Tailscale is the only way in.

Flags for testing:

| Flag | Effect |
|---|---|
| `--simulate` | Logs every action, changes nothing. Safe to click everything. |
| `--dev` | Loopback + no PIN. Fastest local iteration. |
| `--tailscale` | Binds only to the Tailscale interface (`100.x.x.x`), skipping Serve. WireGuard still encrypts it, but you lose the installable PWA. `run-tailscale.bat` does this. |
| `--lan` | Binds to `0.0.0.0` (only if the current SSID is in `allowed_ssids`). You don't need this with Tailscale. |
| `--verbose` | Debug logging, including why a capability failed to load. |

`launch.ps1` runs `tailscale serve --bg --tcp 8765 8765` once and then leaves it
alone. The mapping persists across reboots and server restarts, and the banner
detects it and prints the real URL. Inspect with `tailscale serve status`, remove
with `tailscale serve --tcp=8765 off`.

### Serving under a path

`base_path` in `config.toml` (or `--base-path /laptop-remote`) mounts the app at
a prefix, e.g. `http://<ip>:8765/laptop-remote/`. It works with both serve
flavours. The TLS flavour can additionally route several services behind one
hostname:

```
tailscale serve --bg --set-path /laptop-remote 8765
tailscale serve --bg --set-path /something-else 9000
```

With `serve --tcp` there is one mapping per port, so use `base_path` to keep
paths distinct. Either way the app works whether the proxy strips the prefix or
forwards it, and the root still works too, so you can't lock yourself out with a
bad path. Set `base_path = ""` to go back to the root.

---

## 2. Tailscale, once

1. **Laptop** — `tailscale up` in a terminal, sign in. Then give the machine a
   short name in the admin console, e.g. `laptop`.
2. **Admin console** → <https://login.tailscale.com/admin/dns> → enable
   **HTTPS Certificates**. MagicDNS should already be on.
3. **Each Android device** — install Tailscale from the Play Store, sign in with
   the *same* account, toggle it on.
4. **Laptop** — `launch.ps1` already ran this once for you:

   ```
   tailscale serve --bg --tcp 8765 8765
   ```

   Your URL is `http://100.x.x.x:8765/laptop-remote/` (the banner prints the exact
   address); the MagicDNS name works too when its DNS resolves.

TCP forwarding keeps the app tailnet-only without needing certificates, and it
needs **no Windows Firewall rule** — `tailscaled` accepts the connection and dials
`127.0.0.1` itself, so the python process is never exposed. Plain http is fine
inside WireGuard.

Optional: for a real certificate and an installable PWA, enable **HTTPS
Certificates** in the admin console and use the TLS flavour instead —
`tailscale serve --bg 8765` gives `https://laptop.your-tailnet.ts.net/`. Undo
either with `tailscale serve --https=443 off` or `tailscale serve --tcp=8765 off`.

> **Never use `tailscale funnel`.** Funnel publishes the port to the public
> internet. `serve` keeps it inside your tailnet. They differ by one word, so
> it's worth re-reading the command before you press enter.

---

## 3. Pair each device

Open the `https://…ts.net` URL on the phone/tablet, type the 6-digit PIN (shown in
the laptop's terminal and set in `config.toml`), done. The token is stored in the
browser and survives restarts. Revoke any device from the ⚙️ screen in the app.

If the PIN is wrong six times the pairing endpoint locks for five minutes.

---

## 4. Make it convenient

- **Android home screen** — Chrome menu → *Add to Home screen*. Over the plain
  http TCP URL this is a shortcut (fullscreen, own icon); true PWA install needs
  the HTTPS flavour of `tailscale serve`.
- **Auto-start Tailscale** — in the Android app, enable *Always-on VPN* if your
  device offers it. Only one Android VPN can be active at a time.
- **Desktop / taskbar shortcut** — `launch.ps1` opens a terminal window that *is*
  the server: you see the banner, the PIN and every request live. It does **not**
  open a browser — the banner prints the URL, plus a QR of the IP link to scan
  with your phone. Closing the window (or Ctrl+C) stops the server; clicking it
  again while the server is up follows the log instead of starting a second copy.
  Point a shortcut at it:

  ```
  Target : C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe
  Args   : -NoProfile -ExecutionPolicy Bypass -File "D:\Random\Misc\laptop-remote\launch.ps1"
  Icon   : D:\Random\Misc\laptop-remote\static\app.ico
  ```

  Right-click the shortcut → *Pin to taskbar*. Windows blocks apps from pinning
  themselves, so this last step has to be done once by hand.

  It waits up to 60s for Tailscale to hand out its `100.x` address before starting,
  because binding too early is the one thing that fails on a cold boot. If
  Tailscale is signed out or stopped, the window says so instead of vanishing.
  Logs are also mirrored to `server.log`.
- **Auto-start at login** — put that same shortcut in `shell:startup`. The PIN is
  fixed in `config.toml`, so it stays the same across launches.
- **Stop it** — close the server window (or `stop-remote.bat`). Closing the
  browser does not stop it.
- **DHCP reservation** isn't needed here, unlike the mkcert route — the ts.net
  name is stable regardless of IP.

---

## Customising the buttons

Edit `config.toml`:

```toml
[apps.spotify]
label = "Spotify"
command = "explorer.exe shell:AppsFolder\\SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify"

[apps.notes]
label = "Notes"
command = "notepad.exe"
```

Add as many as you like, restart the server, and they appear in the launcher grid.
Use full paths for anything that isn't on `PATH`. Commands run via `cmd /c` with a
hidden window, in your home directory.

Find an app's `shell:AppsFolder` id with:

```powershell
Get-StartApps | Format-Table Name, AppID -AutoSize
```

---

## The deck (tiles and widgets)

The row of tiles at the top is the navigation. Each tile lists the widgets shown
underneath it, in order. Both come from `config.toml` and are sent to the phone by
`GET /api/layout`; edit the file and restart the server.

```toml
[[deck]]
id = "media"
label = "Media"
widgets = ["nowplaying", "volume"]

[[deck]]
id = "mouse"
label = "Mouse"
widgets = ["touchpad", "clickbar", "shortcuts", "typebar"]

[[deck]]
id = "system"
label = "System"
widgets = ["volume", "brightness", "lock", "audio_out"]

[[deck]]
id = "apps"
label = "Apps"
widgets = ["apps", "windows"]
```

Widget types:

| Widget | What it draws |
|---|---|
| `nowplaying` | media card: art, title, transport; session chips when several apps are playing |
| `volume` | slider + mute |
| `brightness` | brightness slider (hidden when the panel has no brightness control) |
| `lock` | power buttons: lock, sleep, screen off, sign out |
| `audio_out` | default playback device picker (hidden until the backend lands) |
| `apps` | one button per `[apps.*]` entry |
| `windows` | open windows on the laptop, frontmost first; tap to bring one to the front |
| `touchpad` | the pad: drag, tap, double-tap-drag, long-press, two-finger scroll |
| `clickbar` | left and right click buttons (press-and-hold) |
| `shortcuts` | common chords: copy/paste/undo, Enter/Esc/Tab, arrows, Alt+Tab, Win+D, tab controls |
| `typebar` | text field that types into the focused window |

Notes:

- Unknown widget names are skipped and reported in the console, never shown as an
  error card. An empty or broken deck falls back to the built-in four tiles.
- Delete the whole `[[deck]]` section to use the built-in deck.
- Reordering tiles or widgets is just reordering those lists.
- The tile you last used is remembered per device (`localStorage`), so the app
  reopens there.
- Widget options can go in `[widgets.<type>]` tables; they are passed to the widget
  as-is.

### Media tile

- Every session Windows reports is drawn as its own row, stacked top to bottom:
  artwork, title, an app badge and its own transport. Nothing is hidden behind a
  picker, so two apps playing at once are visible together, and each row's
  buttons act on that row only.
- Artwork for every row is fetched from `/api/art/<hash>` and cached by the
  browser, so a changing playhead does not resend thumbnails over Tailscale.
- Buttons follow that row's session capabilities: if the app says it cannot skip,
  the button is hidden instead of doing nothing.
- The seek bar is a full-height touch target. Press and hold to scrub: the bar and
  the elapsed time preview the target while your finger is down, and the seek is
  only sent on release. Desktop mouse users also get a hover bubble with the time
  the click would land on. Apps that report no seek support hide the bar.
- `-10s` / `+10s` jump relative to the playhead. SMTC sessions read the timeline and
  seek to `position ± 10`; VLC receives its short-jump key, which is 10 s by
  default. Both are disabled when the session cannot move its playhead at all.
- Stop is shown for sessions that advertise it (Opera/YouTube do; it ends playback).
- **VLC 3** publishes no media session at all, so the app finds its window and posts
  the same messages Explorer's media keys would: play/pause, next/prev, stop and
  ±10s work without giving VLC focus. VLC's title becomes the track name. There is no
  message that reports the playhead, so the seek bar needs VLC's web interface; run
  `vlc --extraintf http --http-port 8080 --http-password <password>` (or enable
  *Interface > Main interfaces > Web* in the preferences) and put the same values in
  `[vlc]` in `config.toml`. Until then the card shows transport only, with ±10s.
- **Multiple media:** Chromium publishes exactly **one** session per browser, no
  matter how many windows or audible tabs it has — two Opera windows (YouTube and
  Stremio-in-browser) appear as a single row whose title follows the audible tab.
  A different browser, Spotify, Media Player, or VLC each add their own row.
- Stremio publishes no media session and ignores both the global media keys and a
  focused spacebar, so nothing can control it remotely. When Stremio is running and
  no session exists, the card says so rather than showing dead buttons.
- Global media keys are only sent when Windows reports **zero** sessions; otherwise a
  key press could pause a different app than the one on screen.

### Mouse tile gestures

| Gesture | Action |
|---|---|
| one-finger drag | move the pointer |
| one-finger tap | left click |
| double-tap then drag | press and hold the left button (drag windows / select text) |
| long press (~0.5 s) | right click |
| two-finger drag | scroll (natural direction; toggle in the pad header) |
| two-finger tap | right click |

Scroll has momentum, so a flick keeps going briefly. Clicks are written to
`audit.log`; pointer moves and scrolls are not, to keep the log readable.

The pad is deliberately large (58% of the screen height, capped at 560 px) and spans
the full width once the deck goes multi-column. The **On / Off** chip in its header
starts **Off** on a new device: the pad consumes no touches and the input socket is
not even opened until you turn it on, so picking up the phone cannot move the cursor.
Turning it off releases a held left button before it stops listening. The choice is
remembered per device (`localStorage["lr.pad"]`). Left/right click buttons still work
while the pad is off — they fall back to HTTP.

Pointer batches travel over a **dedicated WebSocket** (`/ws-input`) that exists only
while the touchpad is switched **On**; HTTP `POST /api/pointer` stays as the fallback
when the socket is down. One ordered stream matters: with HTTP, a `down` and the moves
that follow it could land on different connections and overtake each other. It also
removes a request header and a round trip per frame, and it is what makes a 120 Hz
screen safe — the old 6000 batches/min limit would have locked out a 120 Hz tablet
after about 50 seconds of dragging.

### Shortcut keys

The Mouse tile carries a grid of chords for the things a touchpad cannot do on its
own. Buttons send a named shortcut, never a raw key sequence — the server owns the
table, so a page cannot ask for `Win+R` or anything else.

| Group | Chords |
|---|---|
| editing | Copy, Paste, Cut, Undo, Redo, Select all, Save, Find |
| keys | Enter, Tab, Esc, Backspace, ↑ ↓ ← → |
| windows | Alt+Tab, Desktop (`Win+D`) |
| browser | Close tab (`Ctrl+W`), New tab (`Ctrl+T`), Reopen (`Ctrl+Shift+T`), Refresh (`F5`), Back / Forward (`Alt+←` / `Alt+→`) |

Each tap is one `SendInput` batch (modifiers, key, release), so a chord can never
arrive as a stuck modifier, and every one is written to `audit.log` with the device
that sent it. A hold-to-repeat is deliberately not implemented: a stuck key on a
remote is worse than a slow tap.

The typing bar sends text as real keystrokes to whatever window is focused. It can't
type into an elevated (admin) window — Windows blocks that, and the app says so
instead of pretending. The text itself is never logged, only its length.

### Apps tile

- One button per `[apps.*]` entry launches that command detached.
- **Open windows** lists visible top-level windows on the laptop, frontmost first,
  with the process name and full title. Tap one to bring it to the front —
  `SetForegroundWindow` with the usual ALT-tap workaround, so a window on another
  desktop or behind a fullscreen app still comes forward. The current window is
  highlighted. The list is polled every 2.5 s while the tile is open (and never in
  the background), and a **Refresh** chip forces it.
- Hidden UWP shells and tool windows are filtered out, so the list is the windows a
  human would Alt-Tab through, not every message-only window in the session.
- Switching is audited with the window's title, so `audit.log` answers "what did the
  phone bring to the front, and when".


---

## Security notes

- **Nothing listens on your LAN.** Default bind is `127.0.0.1`; Tailscale is the
  only door, and it authenticates every device before traffic reaches Python.
- **No arbitrary command execution.** The only launchable programs are the ones
  in `config.toml`. A stolen token gets you "open Notepad", not a shell.
- **Tokens are stored hashed.** `state.json` contains SHA-256 digests, not
  tokens. Still treat the file as a secret.
- **`audit.log`** records every action with the device name, the device's IP and a
  timestamp, and the same lines are echoed into the server window. Columns are
  tab-separated:

  ```
  2026-11-16 12:16:28  Tablet  100.120.202.63  -  volume  33
  2026-11-16 12:16:28  Tablet  100.120.202.63  -  launch  spotify: explorer.exe shell:AppsFolder\...
  2026-11-16 12:19:02  -       100.80.60.40    -  denied  GET /laptop-remote/api/state
  ```

  `time`, `device`, `ip`, `tailnet login`, `event`, `detail`. Refused actions
  (`action_fail`) and unpaired requests (`denied`) are logged too.

  One caveat with `serve --tcp`: tailscaled dials `127.0.0.1` for every client, so
  the `ip` column always reads `127.0.0.1` and the identity header is absent. The
  `device` column (the paired token) is still accurate. The HTTPS flavour of
  `serve`, or `--tailscale`, preserves the real tailnet IP and login.
- **`allowed_logins`** in `config.toml` adds a second gate: if `tailscale serve`
  supplies an identity header, only these logins are accepted. That gate only
  applies to the HTTPS flavour; with `--tcp` no header arrives. Leave it empty
  and read `--verbose` output to see whether headers reach your setup.
- **Lost device** → revoke it in the app, or remove it from the tailnet in the
  admin console.

## Troubleshooting

| Symptom | Cause |
|---|---|
| UI loads, everything says `--` | Capability failed to import. Run with `--verbose`; check `requirements.txt` installed. |
| Volume works, no now-playing card | WinRT optional deps missing, or nothing is playing. Buttons still work. |
| Brightness reads `--` | Desktop with an external monitor only. `WmiMonitorBrightness` needs the internal panel. |
| Red dot, never connects | `tailscale serve` is not forwarding the port, or the device's Tailscale is off. Run `launch.ps1` (it sets up `serve --tcp`) and check `tailscale serve status`. |
| `ERR_EMPTY_RESPONSE` | `tailscaled` is listening but the python server is dead. Close the old window, run `launch.ps1` again; the new version probes the app instead of trusting the port. |
| `403 wrong PIN or locked out` | Six bad attempts — wait five minutes. |
| Media buttons do nothing | A media app isn't holding a session; try starting playback on the laptop first. Stremio is always like this - it exposes no session to Windows. |
| VLC shows up but has no seek bar | VLC's web interface is off. Add `--extraintf http --http-port 8080 --http-password <password>` (or enable it in Preferences) and match it under `[vlc]` in `config.toml`. |
| Touchpad doesn't move the cursor | The focused window is running elevated; Windows refuses synthetic input from a normal process (UIPI). |
| Touchpad feels laggy | Check the pad header shows **On**, then the network: `netsh wlan show interfaces` should report 5 GHz and/or 802.11ax. On 2.4 GHz expect 10–40 ms of jitter no code can fix. Clicks are now audited off the event loop and input has its own thread, so the remaining delay is the network. |
| Open windows list is empty | The laptop is locked or showing the sign-in screen — Windows cloaks every window there, so there is nothing visible to list. Unlock it and tap Refresh. |
| Tapping a window does nothing | The window closed between the poll and the tap (409 "that window is gone"), or Windows refused focus; tap Refresh and try again. |
| Typing bar says "input was blocked" | Same cause: the focused window is elevated. |
| Deck shows fewer tiles than `config.toml` | Check the server window: unknown widget names and dropped tiles are logged there. |
