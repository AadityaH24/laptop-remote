/* Laptop Remote — front end. Vanilla JS, no build step.
 *
 * The app is a shell: /api/layout describes the deck tiles and which widgets
 * each tile shows. Every widget is a small module with mount(node) → {update}.
 * Only the active tile's widgets exist, and they all read one shared store.
 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const KEY_TOKEN = "lr.token";
  const KEY_NAME = "lr.name";
  const KEY_TILE = "lr.tile";
  const KEY_NATURAL = "lr.natural";
  const KEY_PAD = "lr.pad";

  const store = {
    token: localStorage.getItem(KEY_TOKEN) || "",
    name: localStorage.getItem(KEY_NAME) || "",
    state: null,
    layout: null,
    tile: localStorage.getItem(KEY_TILE) || "",
    widgets: [],
    ws: null,
    retries: 0,
    pollTimer: null,
    dev: false,
    dragging: { volume: false, brightness: false },
  };

  /* ---------------------------------------------------------------- util */

  const activity = (ms = 8) => {
    if (navigator.vibrate) { try { navigator.vibrate(ms); } catch (_) {} }
  };

  let toastTimer = null;
  function toast(msg) {
    const el = $("toast");
    el.textContent = msg;
    el.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("show"), 1800);
  }

  const fmtTime = (s) => {
    if (s == null || !isFinite(s)) return "0:00";
    s = Math.max(0, Math.round(s));
    const m = Math.floor(s / 60);
    return `${m}:${String(s % 60).padStart(2, "0")}`;
  };

  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

  function guessName() {
    const ua = navigator.userAgent;
    if (/SM-X|Lenovo|Tab|Pad/i.test(ua) || Math.min(screen.width, screen.height) > 600) {
      return "Tablet";
    }
    if (/Pixel/i.test(ua)) return "Pixel";
    if (/Samsung/i.test(ua)) return "Galaxy";
    return "Phone";
  }

  /* ----------------------------------------------------------------- api */

  async function api(path, options = {}) {
    const headers = Object.assign(
      { "Content-Type": "application/json" },
      options.headers || {}
    );
    if (store.token) headers["X-Control-Token"] = store.token;
    const res = await fetch(path, Object.assign({}, options, { headers }));
    if (res.status === 401) { unpair(); throw new Error("unauthorized"); }
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch (_) {}
      throw new Error(detail);
    }
    return res.status === 204 ? null : res.json();
  }

  const act = (body) =>
    api("api/action", { method: "POST", body: JSON.stringify(body) }).catch((e) => {
      if (e.message !== "unauthorized") toast(e.message);
    });

  /* --------------------------------------------------------------- state */

  function setSliderFill(input) {
    const min = +input.min || 0, max = +input.max || 100;
    const pct = ((+input.value - min) / (max - min)) * 100;
    input.style.setProperty("--p", pct + "%");
  }

  function applyState(state) {
    store.state = state;
    $("host").textContent = state.host || "Laptop";
    store.widgets.forEach((w) => {
      if (!w.update) return;
      try { w.update(state); } catch (err) { console.error("widget", w.type, err); }
    });
  }

  /* -------------------------------------------------------------- layout */

  // Used only if /api/layout cannot be reached (older server, offline boot).
  const FALLBACK_LAYOUT = {
    tiles: [
      { id: "media", label: "Media", widgets: [{ type: "nowplaying" }, { type: "volume" }] },
      { id: "mouse", label: "Mouse", widgets: [{ type: "touchpad" }, { type: "clickbar" }, { type: "shortcuts" }, { type: "typebar" }] },
      { id: "system", label: "System", widgets: [{ type: "volume" }, { type: "brightness" }, { type: "lock" }, { type: "audio_out" }] },
      { id: "apps", label: "Apps", widgets: [{ type: "apps" }, { type: "windows" }] },
    ],
    widgets: [],
    warnings: [],
  };

  function renderDeck() {
    const nav = $("deck");
    const tiles = (store.layout && store.layout.tiles) || [];
    nav.innerHTML = "";
    tiles.forEach((tile) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "tile";
      btn.dataset.tile = tile.id;
      btn.textContent = tile.label;
      btn.addEventListener("click", () => { activity(6); selectTile(tile.id); });
      nav.appendChild(btn);
    });
    const known = tiles.some((t) => t.id === store.tile);
    selectTile(known ? store.tile : (tiles[0] && tiles[0].id), true);
  }

  function selectTile(id, force = false) {
    if (!id) return;
    if (!force && id === store.tile && store.widgets.length) return;
    store.tile = id;
    localStorage.setItem(KEY_TILE, id);
    document.querySelectorAll(".deck .tile").forEach((btn) => {
      const on = btn.dataset.tile === id;
      if (on) btn.setAttribute("aria-current", "true");
      else btn.removeAttribute("aria-current");
    });
    mountTile(id);
  }

  function clearWidgets() {
    store.widgets.forEach((w) => {
      if (!w.destroy) return;
      try { w.destroy(); } catch (err) { console.error("widget destroy", w.type, err); }
    });
    store.widgets = [];
    $("main").innerHTML = "";
  }

  function mountTile(id) {
    clearWidgets();
    const tile = ((store.layout && store.layout.tiles) || []).find((t) => t.id === id);
    if (!tile) return;
    const main = $("main");
    (tile.widgets || []).forEach(({ type, options }) => {
      const def = WIDGETS[type];
      const tpl = def && $(def.tpl);
      if (!tpl) {
        // Unknown widget: render nothing rather than an error card.
        console.warn("skipping widget", type);
        return;
      }
      const node = tpl.content.firstElementChild.cloneNode(true);
      let inst = {};
      try {
        inst = def.mount(node, options || {}) || {};
      } catch (err) {
        console.error("widget mount failed", type, err);
        return;
      }
      main.appendChild(node);
      store.widgets.push({ type, node, update: inst.update, destroy: inst.destroy });
    });
    if (store.state) applyState(store.state);
  }

  /* ------------------------------------------------------------- widgets */

  const WIDGETS = {};

  /* -- now playing ------------------------------------------------------ */

  const APP_LABELS = [
    [/opera_gx/i, "Opera GX"],
    [/opera/i, "Opera"],
    [/chrome/i, "Chrome"],
    [/msedge/i, "Edge"],
    [/firefox/i, "Firefox"],
    [/spotify/i, "Spotify"],
    [/stremio/i, "Stremio"],
    [/vlc/i, "VLC"],
    [/music\.ui|media player/i, "Media Player"],
    [/foobar/i, "foobar2000"],
  ];

  function appLabel(app) {
    if (!app || app === "?") return "Windows";
    for (const [re, name] of APP_LABELS) if (re.test(app)) return name;
    const base = app.split(/[\\/]/).pop().replace(/\.exe$/i, "");
    return base || "App";
  }

  const PLAY_ICON = '<path d="M8 5v14l11-7L8 5Z" fill="currentColor" stroke="none"/>';
  const PAUSE_ICON = '<path d="M7 4h4v16H7zM13 4h4v16h-4z" fill="currentColor" stroke="none"/>';

  // Artwork is served as /api/art/<hash>; CSS background requests cannot send
  // the control header, so carry the device token in the query string.
  function artSrc(url) {
    if (!url) return null;
    if (/^(data|https?):/i.test(url)) return url;
    const sep = url.includes("?") ? "&" : "?";
    return store.token ? `${url}${sep}t=${encodeURIComponent(store.token)}` : url;
  }

  const sessionKey = (s) => s.id || `${s.app}|${s.title}`;

  function paintRow(r) {
    if (!r.el.isConnected) return;
    const c = r.clock;
    if (!c.dur) {
      r.bar.style.width = "0%";
      r.knob.style.left = "0%";
      r.pos.textContent = "0:00";
      r.dur.textContent = "0:00";
      return;
    }
    let pos = r.seek
      ? r.seek.pos
      : c.pos + (c.playing ? (performance.now() - c.at) / 1000 : 0);
    pos = Math.max(0, Math.min(c.dur, pos));
    const pct = (pos / c.dur) * 100;
    r.bar.style.width = pct + "%";
    r.knob.style.left = pct + "%";
    r.pos.textContent = fmtTime(pos);
    r.dur.textContent = fmtTime(c.dur);
    r.progress.setAttribute("aria-valuemax", String(Math.round(c.dur)));
    r.progress.setAttribute("aria-valuenow", String(Math.round(pos)));
    r.progress.setAttribute("aria-valuetext", `${fmtTime(pos)} of ${fmtTime(c.dur)}`);
  }

  WIDGETS.nowplaying = {
    tpl: "tpl-nowplaying",
    mount(node) {
      const list = node.querySelector(".session-list");
      const empty = node.querySelector(".empty");
      const tpl = document.getElementById("tpl-media-session");
      const rows = new Map();

      if (!list || !tpl) {
        // The page and this script came from different builds (a half-updated
        // service-worker cache). Say so instead of leaving a blank card.
        console.warn("laptop-remote: media markup mismatch; reload the page");
        if (empty) {
          empty.hidden = false;
          empty.querySelector(".title").textContent = "Reload to update";
          empty.querySelector(".sub").textContent = "Close and reopen this app";
        } else {
          node.hidden = true;
        }
        return { update() {}, destroy() {} };
      }

      function createRow(session) {
        const el = tpl.content.firstElementChild.cloneNode(true);
        const r = {
          el,
          key: sessionKey(session),
          session,
          dragging: false,
          seek: null,
          clock: { at: performance.now(), pos: 0, dur: 0, playing: false },
          art: el.querySelector(".art"),
          title: el.querySelector(".title"),
          app: el.querySelector(".app"),
          sub: el.querySelector(".sub"),
          seekBox: el.querySelector(".seek"),
          progress: el.querySelector(".progress"),
          bar: el.querySelector(".bar"),
          knob: el.querySelector(".knob"),
          hint: el.querySelector(".seek-hint"),
          pos: el.querySelector(".pos"),
          dur: el.querySelector(".dur"),
          icon: el.querySelector(".playIcon"),
          buttons: Array.from(el.querySelectorAll("[data-media]")),
        };
        el.dataset.key = r.key;
        r.button = (name) => r.buttons.find((b) => b.dataset.media === name);
        r.sessionRef = () => ({ app: r.session.app, title: r.session.title });
        r.duration = () => r.session.duration || 0;
        r.canSeek = () => !!(r.session.can && r.session.can.seek && r.duration());

        r.buttons.forEach((btn) => {
          btn.addEventListener("click", () => {
            activity(8);
            const action = btn.dataset.media;
            if (action === "back10" || action === "fwd10") {
              act({
                type: "media",
                action: "skip",
                delta: action === "back10" ? -10 : 10,
                session: r.sessionRef(),
              });
            } else {
              act({ type: "media", action, session: r.sessionRef() });
            }
          });
        });

        function fracFrom(event) {
          const rect = r.progress.getBoundingClientRect();
          if (!rect.width) return 0;
          return Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
        }

        function showHint(frac, position) {
          const width = r.progress.clientWidth || 1;
          const half = (r.hint.offsetWidth || 46) / 2;
          const x = Math.max(half, Math.min(width - half, frac * width));
          r.hint.textContent = fmtTime(position);
          r.hint.style.left = x + "px";
          r.hint.hidden = false;
        }

        function previewSeek(frac) {
          const dur = r.duration();
          if (!dur) return;
          r.seek = { pos: Math.max(0, Math.min(dur, frac * dur)) };
          paintRow(r);
          showHint(frac, r.seek.pos);
        }

        function commitSeek(frac) {
          const dur = r.duration();
          if (!dur) return;
          const position = Math.max(0, Math.min(dur, frac * dur));
          activity(4);
          act({ type: "media", action: "seek", position, session: r.sessionRef() });
          r.seek = null;
          Object.assign(r.clock, { at: performance.now(), pos: position, dur });
          paintRow(r);
        }

        r.progress.addEventListener("pointerdown", (event) => {
          if (!r.canSeek()) return;
          r.dragging = true;
          try { r.progress.setPointerCapture(event.pointerId); } catch (_) {}
          r.progress.classList.add("seeking");
          previewSeek(fracFrom(event));
          event.preventDefault();
        });

        r.progress.addEventListener("pointermove", (event) => {
          if (r.dragging) {
            previewSeek(fracFrom(event));
            return;
          }
          // Desktop hover: show where a click would land, without moving anything.
          if (event.pointerType === "mouse" && r.canSeek()) {
            const frac = fracFrom(event);
            showHint(frac, frac * r.duration());
          }
        });

        r.progress.addEventListener("pointerleave", () => {
          if (!r.dragging) r.hint.hidden = true;
        });

        r.progress.addEventListener("pointerup", (event) => {
          if (!r.dragging) return;
          r.dragging = false;
          r.progress.classList.remove("seeking");
          try { r.progress.releasePointerCapture(event.pointerId); } catch (_) {}
          commitSeek(fracFrom(event));
          r.hint.hidden = true;
        });

        // A cancelled touch must not seek; just drop the preview.
        r.progress.addEventListener("pointercancel", () => {
          if (!r.dragging) return;
          r.dragging = false;
          r.progress.classList.remove("seeking");
          r.seek = null;
          paintRow(r);
          r.hint.hidden = true;
        });

        return r;
      }

      function applyRow(r, session) {
        const prev = r.session;
        r.session = session;
        r.key = sessionKey(session);
        r.el.dataset.key = r.key;

        // Re-render the visible parts, but never stomp a finger that is
        // currently scrubbing this row.
        if (r.dragging) return;

        const can = session.can || {};
        const playing = !!session.playing;
        const has = !!(session.title || session.artist || session.app);

        r.title.textContent = session.title || "Unknown track";
        r.title.title = session.title || "";
        r.app.textContent = appLabel(session.app);
        const bits = [session.artist, session.album].filter(Boolean);
        r.sub.textContent = bits.join(" · ") || (playing ? "Playing" : "Paused");

        const art = artSrc(session.art);
        if (art) {
          r.art.style.backgroundImage = `url("${art}")`;
          r.art.classList.add("has-art");
        } else {
          r.art.style.backgroundImage = "";
          r.art.classList.remove("has-art");
        }

        r.icon.innerHTML = playing ? PAUSE_ICON : PLAY_ICON;
        r.el.classList.toggle("playing", playing && has);
        r.el.setAttribute(
          "aria-label",
          `${appLabel(session.app)}: ${session.title || "unknown"}${playing ? ", playing" : ""}`
        );

        // Only show the buttons this session actually supports; a row full of
        // disabled buttons is noise when several sessions are stacked.
        r.button("prev").hidden = !can.prev;
        r.button("next").hidden = !can.next;
        r.button("stop").hidden = !can.stop;
        r.button("toggle").disabled = !has || !can.play_pause;
        const canSkip = !!(can.skip || can.seek);
        r.button("back10").disabled = !has || !canSkip;
        r.button("fwd10").disabled = !has || !canSkip;

        const seekable = !!can.seek && !!session.duration;
        r.seekBox.hidden = !seekable;
        r.el.classList.toggle("no-seek", !seekable);
        r.progress.style.pointerEvents = seekable ? "auto" : "none";

        const moved = !prev || prev.position !== session.position
          || prev.duration !== session.duration
          || prev.playing !== session.playing;
        // Re-anchor the local clock on every state push except when the
        // playhead did not move (a paused session would otherwise drift to
        // "now" for no reason).
        if (moved || !r.clock.dur) {
          Object.assign(r.clock, {
            at: performance.now(),
            pos: session.position || 0,
            dur: session.duration || 0,
            playing: playing && has,
          });
        }
        r.seek = null;
        paintRow(r);
      }

      function paintEmpty(state) {
        list.hidden = true;
        empty.hidden = false;
        const stremio = !!state.stremio;
        empty.querySelector(".title").textContent = stremio
          ? "Stremio is playing"
          : "Nothing playing";
        empty.querySelector(".sub").textContent = stremio
          ? "Windows exposes no media controls for Stremio"
          : "Start media on the laptop";
      }

      function update(state) {
        if (!state || !state.caps || (!state.caps.media && !state.caps.vlc)) {
          node.hidden = true;
          return;
        }
        node.hidden = false;

        const sessions = state.media_sessions || [];
        if (!sessions.length) {
          rows.forEach((r) => r.el.remove());
          rows.clear();
          paintEmpty(state);
          return;
        }

        empty.hidden = true;
        list.hidden = false;

        // Playing sessions first; otherwise keep the order Windows reports.
        const order = sessions
          .map((s, i) => ({ s, i }))
          .sort((a, b) => (b.s.playing ? 1 : 0) - (a.s.playing ? 1 : 0) || a.i - b.i)
          .map((x) => x.s);

        const wanted = new Set(order.map(sessionKey));
        rows.forEach((r, key) => {
          if (!wanted.has(key)) { r.el.remove(); rows.delete(key); }
        });

        const dragging = Array.from(rows.values()).some((r) => r.dragging);
        order.forEach((session) => {
          const key = sessionKey(session);
          let r = rows.get(key);
          if (!r) {
            r = createRow(session);
            rows.set(key, r);
            list.appendChild(r.el);
          }
          applyRow(r, session);
        });

        // Keep visual order in sync, but never yank a row out from under a
        // finger that is scrubbing it.
        if (!dragging) {
          const wantedRows = order.map((s) => rows.get(sessionKey(s))).filter(Boolean);
          const current = Array.from(list.children);
          if (wantedRows.some((r, i) => current[i] !== r.el)) {
            wantedRows.forEach((r) => list.appendChild(r.el));
          }
        }
      }

      const tick = setInterval(() => rows.forEach(paintRow), 500);

      return {
        update,
        destroy() {
          clearInterval(tick);
          rows.clear();
        },
      };
    },
  };

  /* -- sliders ---------------------------------------------------------- */

  function bindSlider(input, out, kind) {
    let pending = null;
    let last = 0;
    const send = (value) => { last = Date.now(); act({ type: kind, value }); };

    input.addEventListener("pointerdown", () => { store.dragging[kind] = true; });
    const release = () => {
      store.dragging[kind] = false;
      if (pending != null) { send(pending); pending = null; }
    };
    ["pointerup", "pointercancel", "blur"].forEach((ev) => input.addEventListener(ev, release));

    input.addEventListener("input", () => {
      const value = +input.value;
      setSliderFill(input);
      out.textContent = value;
      pending = value;
      if (Date.now() - last > 120) { send(value); pending = null; }
    });
  }

  WIDGETS.volume = {
    tpl: "tpl-volume",
    mount(node) {
      const slider = node.querySelector("input[type=range]");
      const out = node.querySelector("output");
      const mute = node.querySelector("[data-mute]");
      bindSlider(slider, out, "volume");
      mute.addEventListener("click", () => {
        const next = !(store.state && store.state.muted);
        activity(8);
        mute.setAttribute("aria-pressed", String(next));
        mute.textContent = next ? "Unmute" : "Mute";
        act({ type: "mute", value: next });
      });
      return {
        update(state) {
          const ok = !!(state.caps && state.caps.volume);
          node.hidden = false;
          if (!store.dragging.volume && state.volume != null) {
            slider.value = state.volume;
            setSliderFill(slider);
          }
          slider.disabled = !ok;
          out.textContent = state.volume == null ? "--" : state.volume;
          mute.disabled = !ok;
          mute.setAttribute("aria-pressed", state.muted ? "true" : "false");
          mute.textContent = state.muted ? "Unmute" : "Mute";
        },
      };
    },
  };

  WIDGETS.brightness = {
    tpl: "tpl-brightness",
    mount(node) {
      const slider = node.querySelector("input[type=range]");
      const out = node.querySelector("output");
      bindSlider(slider, out, "brightness");
      return {
        update(state) {
          const ok = !!(state.caps && state.caps.brightness) && state.brightness != null;
          node.hidden = !(state.caps && state.caps.brightness);
          if (!store.dragging.brightness && state.brightness != null) {
            slider.value = state.brightness;
            setSliderFill(slider);
          }
          slider.disabled = !ok;
          out.textContent = state.brightness == null ? "--" : state.brightness;
        },
      };
    },
  };

  /* -- power ------------------------------------------------------------ */

  const POWER_META = {
    lock: {
      label: "Lock",
      icon: '<rect x="4.5" y="10.5" width="15" height="10" rx="2.2"/><path d="M8 10.5V7.6A4 4 0 0 1 16 7.6v2.9"/>',
      confirm: false,
    },
    sleep: {
      label: "Sleep",
      icon: '<path d="M20 14.5A8.2 8.2 0 0 1 9.5 4 8.5 8.5 0 1 0 20 14.5Z"/>',
      confirm: true,
    },
    displayoff: {
      label: "Screen off",
      icon: '<rect x="2.5" y="4.5" width="19" height="12.5" rx="2"/><path d="M8 20.5h8M12 17v3.5"/>',
      confirm: false,
    },
    signout: {
      label: "Sign out",
      icon: '<path d="M14 4.5H6.5a2 2 0 0 0-2 2v11a2 2 0 0 0 2 2H14"/><path d="M17 8.5 20.5 12 17 15.5M10 12h10.5"/>',
      confirm: true,
    },
  };

  WIDGETS.lock = {
    tpl: "tpl-lock",
    mount(node) {
      const grid = node.querySelector("[data-power]");
      let built = "";
      return {
        update(state) {
          const actions = (state.power || []).filter((name) => POWER_META[name]);
          const key = actions.join(",");
          if (key !== built) {
            built = key;
            grid.innerHTML = "";
            actions.forEach((name) => {
              const meta = POWER_META[name];
              const btn = document.createElement("button");
              btn.className = "pbtn";
              btn.innerHTML = `<svg viewBox="0 0 24 24">${meta.icon}</svg><span>${meta.label}</span>`;
              btn.addEventListener("click", () => {
                activity(10);
                if (meta.confirm && !confirm(`${meta.label} the laptop?`)) return;
                act({ type: "power", action: name });
                toast(meta.label + " sent");
              });
              grid.appendChild(btn);
            });
          }
        },
      };
    },
  };

  /* -- apps ------------------------------------------------------------- */

  WIDGETS.apps = {
    tpl: "tpl-apps",
    mount(node) {
      const box = node.querySelector("[data-apps]");
      let built = "";
      return {
        update(state) {
          const apps = state.apps || [];
          const key = apps.map((a) => a.id).join(",");
          if (key === built) return;
          built = key;
          box.innerHTML = "";
          apps.forEach((app) => {
            const btn = document.createElement("button");
            btn.className = "abtn";
            btn.innerHTML =
              `<span class="avatar">${(app.label || app.id).trim().charAt(0).toUpperCase()}</span>` +
              `<span class="nm"></span>`;
            btn.querySelector(".nm").textContent = app.label || app.id;
            btn.addEventListener("click", () => {
              activity(10);
              act({ type: "launch", id: app.id });
              toast("Launching " + (app.label || app.id));
            });
            box.appendChild(btn);
          });
          if (!apps.length) {
            box.innerHTML = '<span class="empty">No apps configured.</span>';
          }
        },
      };
    },
  };

  /* -- audio output (read-only until the backend lands) ------------------ */

  WIDGETS.audio_out = {
    tpl: "tpl-audio",
    mount(node) {
      const list = node.querySelector("[data-outputs]");
      return {
        update(state) {
          const ok = !!(state.caps && state.caps.audio_out);
          node.hidden = !ok;
          if (!ok) return;
          const outputs = state.audio_outputs || [];
          list.innerHTML = "";
          outputs.forEach((device) => {
            const li = document.createElement("li");
            li.className = "audio-row" + (device.is_default ? " current" : "");
            li.textContent = device.name;
            if (!device.is_default) {
              li.addEventListener("click", () => {
                activity(8);
                api("api/audio/output", {
                  method: "POST",
                  body: JSON.stringify({ id: device.id }),
                }).catch((e) => { if (e.message !== "unauthorized") toast(e.message); });
              });
              li.setAttribute("role", "button");
              li.tabIndex = 0;
            }
            list.appendChild(li);
          });
        },
      };
    },
  };

  /* -- touchpad --------------------------------------------------------- */

  const POINTER_GAIN = 1.7;   // finger pixels → cursor pixels
  const SCROLL_GAIN = 2.0;    // finger pixels → wheel units

  WIDGETS.touchpad = {
    tpl: "tpl-touchpad",
    mount(node) {
      const pad = node.querySelector("[data-pad]");
      const dirBtn = node.querySelector("[data-scroll-dir]");
      const toggle = node.querySelector("[data-pad-toggle]");
      const hint = node.querySelector(".pad-hint");
      const hintIdle = hint ? hint.textContent : "";

      let natural = localStorage.getItem(KEY_NATURAL) !== "0";
      const paintDir = () => {
        dirBtn.textContent = natural ? "Natural" : "Classic";
        dirBtn.setAttribute("aria-pressed", String(natural));
      };
      paintDir();
      dirBtn.addEventListener("click", () => {
        natural = !natural;
        localStorage.setItem(KEY_NATURAL, natural ? "1" : "0");
        paintDir();
      });

      // Off unless this device has explicitly turned it on. A pad that starts
      // live is a pad that moves the cursor when you brush the screen; the
      // input socket is not opened either until it is on.
      let enabled = localStorage.getItem(KEY_PAD) === "1";
      let channel = false;
      const setChannel = (on) => {
        if (on === channel) return;
        channel = on;
        if (on) inputHold(); else inputRelease();
      };
      const paintPad = () => {
        toggle.textContent = enabled ? "On" : "Off";
        toggle.setAttribute("aria-pressed", String(enabled));
        pad.classList.toggle("paused", !enabled);
        if (hint) {
          hint.textContent = enabled
            ? hintIdle
            : "Touchpad is off — tap On above to control the laptop";
        }
      };

      const pointers = new Map();
      let accX = 0, accY = 0, wheelY = 0, wheelX = 0;
      let frame = 0, flushTimer = 0, momentum = 0;
      let gesture = null;
      let two = null;
      let longTimer = 0;
      let lastTap = 0;

      function flush() {
        if (frame) { cancelAnimationFrame(frame); frame = 0; }
        if (flushTimer) { clearTimeout(flushTimer); flushTimer = 0; }
        const events = [];
        // Truncate instead of round, and keep the fraction for the next batch:
        // rounding threw away up to half a pixel every frame, which made slow
        // drags feel like the cursor was dragging through mud.
        if (accX || accY) {
          const dx = clamp(Math.trunc(accX), -500, 500);
          const dy = clamp(Math.trunc(accY), -500, 500);
          accX = clamp(accX - dx, -2000, 2000);
          accY = clamp(accY - dy, -2000, 2000);
          if (dx || dy) events.push({ t: "move", dx, dy });
        }
        const wy = Math.trunc(wheelY);
        const wx = Math.trunc(wheelX);
        if (wy || wx) {
          wheelY -= wy;
          wheelX -= wx;
          events.push({
            t: "scroll",
            dy: clamp(wy, -1200, 1200),
            dx: clamp(wx, -1200, 1200),
          });
        }
        if (events.length) sendPointer(events);
      }

      // rAF alone waits up to a whole frame (16.7 ms on a 60 Hz screen) before
      // anything leaves the tablet. Race it with an 8 ms timer so input leaves
      // sooner when frames are slow; the socket makes the extra batches cheap.
      function schedule() {
        if (frame || flushTimer) return;
        frame = requestAnimationFrame(flush);
        flushTimer = setTimeout(flush, 8);
      }

      function centroid() {
        let x = 0, y = 0;
        pointers.forEach((p) => { x += p.x; y += p.y; });
        const n = pointers.size || 1;
        return { x: x / n, y: y / n };
      }

      function stopMomentum() {
        if (momentum) { cancelAnimationFrame(momentum); momentum = 0; }
      }

      function startMomentum(units) {
        stopMomentum();
        let v = clamp(units, -160, 160);
        if (Math.abs(v) < 0.8) return;
        const tick = () => {
          if (Math.abs(v) < 0.8) { momentum = 0; return; }
          wheelY += v;
          v *= 0.92;
          schedule();
          momentum = requestAnimationFrame(tick);
        };
        momentum = requestAnimationFrame(tick);
      }

      function reset() {
        clearTimeout(longTimer);
        stopMomentum();
        pointers.clear();
        gesture = null;
        two = null;
        accX = accY = wheelY = wheelX = 0;
      }

      toggle.addEventListener("click", () => {
        enabled = !enabled;
        localStorage.setItem(KEY_PAD, enabled ? "1" : "0");
        if (!enabled) {
          // Never leave the laptop believing a button is still held.
          if (gesture && gesture.drag) sendPointer([{ t: "up", button: "left" }]);
          reset();
        }
        setChannel(enabled);
        paintPad();
        activity(8);
      });
      paintPad();

      function onDown(e) {
        if (!enabled) return;
        if (e.pointerType === "mouse" && e.button !== 0) return;
        e.preventDefault();
        try { pad.setPointerCapture(e.pointerId); } catch (_) {}
        pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
        activity(4);

        if (pointers.size === 1) {
          stopMomentum();
          gesture = {
            t: performance.now(), moved: 0, long: false, drag: false,
          };
          if (performance.now() - lastTap < 340) {
            // Double-tap and hold: press the left button so you can drag.
            gesture.drag = true;
            sendPointer([{ t: "down", button: "left" }]);
          }
          clearTimeout(longTimer);
          longTimer = setTimeout(() => {
            if (!gesture || gesture.moved > 10 || pointers.size !== 1) return;
            gesture.long = true;
            sendPointer([{ t: "click", button: "right" }]);
            activity(14);
          }, 520);
        } else if (pointers.size === 2) {
          clearTimeout(longTimer);
          if (gesture && gesture.drag) sendPointer([{ t: "up", button: "left" }]);
          gesture = null;
          const c = centroid();
          two = { x: c.x, y: c.y, t: performance.now(), moved: 0, last: performance.now(), v: 0 };
        }
      }

      function onMove(e) {
        if (!enabled) return;
        const p = pointers.get(e.pointerId);
        if (!p) return;

        if (pointers.size === 1 && gesture) {
          // A 120 Hz stylus/touch stream delivers several samples per frame;
          // summing them all keeps fast swipes accurate instead of taking only
          // the last sample.
          let dx = 0, dy = 0, lx = p.x, ly = p.y;
          const coalesced = e.getCoalescedEvents ? e.getCoalescedEvents() : null;
          const samples = coalesced && coalesced.length ? coalesced : [e];
          for (const s of samples) {
            dx += s.clientX - lx;
            dy += s.clientY - ly;
            lx = s.clientX;
            ly = s.clientY;
          }
          p.x = lx;
          p.y = ly;
          if (!dx && !dy) return;
          gesture.moved += Math.abs(dx) + Math.abs(dy);
          accX += dx * POINTER_GAIN;
          accY += dy * POINTER_GAIN;
          schedule();
          return;
        }

        const dx = e.clientX - p.x;
        const dy = e.clientY - p.y;
        p.x = e.clientX;
        p.y = e.clientY;
        if (!dx && !dy) return;

        if (pointers.size >= 2 && two) {
          const c = centroid();
          const sdx = c.x - two.x;
          const sdy = c.y - two.y;
          two.x = c.x;
          two.y = c.y;
          two.moved += Math.abs(sdx) + Math.abs(sdy);
          if (!sdx && !sdy) return;
          const dir = natural ? 1 : -1;
          const units = sdy * SCROLL_GAIN * dir;
          wheelY += units;
          wheelX += -sdx * SCROLL_GAIN * dir;
          const now = performance.now();
          const dt = Math.max(8, now - two.last);
          two.last = now;
          two.v = (units * 16) / dt;   // wheel units per 60 Hz frame
          schedule();
        }
      }

      function onUp(e) {
        if (!enabled) return;
        pointers.delete(e.pointerId);
        if (pointers.size === 0) {
          clearTimeout(longTimer);
          if (gesture) {
            const g = gesture;
            gesture = null;
            if (g.drag) {
              sendPointer([{ t: "up", button: "left" }]);
            } else if (!g.long && performance.now() - g.t < 320 && g.moved < 12) {
              sendPointer([{ t: "click", button: "left" }]);
              lastTap = performance.now();
              activity(6);
            }
          }
          if (two) {
            const t = two;
            two = null;
            if (performance.now() - t.t < 300 && t.moved < 14) {
              sendPointer([{ t: "click", button: "right" }]);
              activity(12);
            } else {
              startMomentum(t.v);
            }
          }
          flush();
        } else if (pointers.size === 1) {
          // Dropped from two fingers to one: stop scrolling; don't click.
          two = null;
          gesture = null;
        }
      }

      pad.addEventListener("pointerdown", onDown);
      pad.addEventListener("pointermove", onMove);
      pad.addEventListener("pointerup", onUp);
      pad.addEventListener("pointercancel", onUp);
      pad.addEventListener("contextmenu", (e) => e.preventDefault());

      // The input socket exists only while the pad is switched on.
      setChannel(enabled);

      return {
        update() {},
        destroy() {
          setChannel(false);
          if (gesture && gesture.drag) sendPointer([{ t: "up", button: "left" }]);
          reset();
          if (frame) cancelAnimationFrame(frame);
          if (flushTimer) clearTimeout(flushTimer);
        },
      };
    },
  };

  /* -- click bar: left and right only ----------------------------------- */

  WIDGETS.clickbar = {
    tpl: "tpl-clickbar",
    mount(node) {
      node.querySelectorAll("[data-click]").forEach((btn) => {
        const button = btn.dataset.click;
        const down = (e) => {
          e.preventDefault();
          activity(6);
          try { btn.setPointerCapture(e.pointerId); } catch (_) {}
          btn.classList.add("held");
          sendPointer([{ t: "down", button }]);
        };
        const up = () => {
          if (!btn.classList.contains("held")) return;
          btn.classList.remove("held");
          sendPointer([{ t: "up", button }]);
        };
        btn.addEventListener("pointerdown", down);
        btn.addEventListener("pointerup", up);
        btn.addEventListener("pointercancel", up);
        btn.addEventListener("contextmenu", (e) => e.preventDefault());
      });
      return { update() {} };
    },
  };

  /* -- open windows (Apps tile) ----------------------------------------- */

  WIDGETS.windows = {
    tpl: "tpl-windows",
    mount(node) {
      const list = node.querySelector("[data-windows]");
      const count = node.querySelector("[data-win-count]");
      const refresh = node.querySelector("[data-win-refresh]");
      let timer = 0;
      let inflight = false;
      let sig = "";

      function render(wins) {
        list.innerHTML = "";
        if (!wins.length) {
          list.innerHTML = '<span class="empty">No open windows.</span>';
          return;
        }
        wins.forEach((w) => {
          const btn = document.createElement("button");
          btn.type = "button";
          btn.className = "win" + (w.active ? " current" : "");
          btn.dataset.hwnd = String(w.hwnd);

          const avatar = document.createElement("span");
          avatar.className = "avatar";
          avatar.textContent = (w.app || "?").trim().charAt(0).toUpperCase();

          const text = document.createElement("span");
          text.className = "win-text";
          const title = document.createElement("span");
          title.className = "win-title";
          title.textContent = w.title || "(untitled)";
          const app = document.createElement("span");
          app.className = "win-app";
          app.textContent = w.active ? `${w.app} · current` : w.app;
          text.append(title, app);
          btn.append(avatar, text);

          btn.addEventListener("click", async () => {
            activity(8);
            btn.classList.add("switching");
            try {
              await api("api/action", {
                method: "POST",
                body: JSON.stringify({ type: "focus", hwnd: w.hwnd }),
              });
              sig = "";
              setTimeout(load, 350);
            } catch (err) {
              if (err.message !== "unauthorized") toast(err.message);
              sig = "";
              setTimeout(load, 350);
            } finally {
              setTimeout(() => btn.classList.remove("switching"), 600);
            }
          });
          list.appendChild(btn);
        });
      }

      async function load() {
        if (inflight) return;
        inflight = true;
        try {
          const data = await api("api/windows");
          const wins = data.windows || [];
          const next = wins
            .map((w) => `${w.hwnd}:${w.active ? 1 : 0}:${w.title}`)
            .join("|");
          if (next !== sig) {
            sig = next;
            render(wins);
          }
          count.textContent = wins.length ? `${wins.length} open` : "";
        } catch (err) {
          if (err.message === "unauthorized") return;
          count.textContent = "";
          if (!list.children.length) {
            list.innerHTML = '<span class="empty">Window list unavailable.</span>';
          }
        } finally {
          inflight = false;
        }
      }

      const tick = () => { if (!document.hidden) load(); };
      const onVisible = () => { if (!document.hidden) load(); };
      refresh.addEventListener("click", () => { activity(6); sig = ""; load(); });
      load();
      timer = setInterval(tick, 2500);
      document.addEventListener("visibilitychange", onVisible);

      return {
        update() {},
        destroy() {
          clearInterval(timer);
          document.removeEventListener("visibilitychange", onVisible);
        },
      };
    },
  };

  /* -- common shortcuts (Mouse tile) ------------------------------------ */

  const SHORTCUT_KEYS = [
    ["copy", "Copy", "Ctrl+C"],
    ["paste", "Paste", "Ctrl+V"],
    ["cut", "Cut", "Ctrl+X"],
    ["undo", "Undo", "Ctrl+Z"],
    ["redo", "Redo", "Ctrl+Y"],
    ["selectall", "Select all", "Ctrl+A"],
    ["save", "Save", "Ctrl+S"],
    ["find", "Find", "Ctrl+F"],
    ["enter", "Enter", "Enter"],
    ["tab", "Tab", "Tab"],
    ["esc", "Esc", "Esc"],
    ["backspace", "⌫", "Backspace"],
    ["left", "←", "Left arrow"],
    ["up", "↑", "Up arrow"],
    ["down", "↓", "Down arrow"],
    ["right", "→", "Right arrow"],
    ["alttab", "Alt+Tab", "Alt+Tab"],
    ["showdesktop", "Desktop", "Win+D"],
    ["closetab", "Close tab", "Ctrl+W"],
    ["newtab", "New tab", "Ctrl+T"],
    ["reopentab", "Reopen", "Ctrl+Shift+T"],
    ["refresh", "Refresh", "F5"],
    ["back", "Back", "Alt+Left"],
    ["forward", "Forward", "Alt+Right"],
  ];

  WIDGETS.shortcuts = {
    tpl: "tpl-shortcuts",
    mount(node) {
      const box = node.querySelector("[data-keys]");
      SHORTCUT_KEYS.forEach(([id, label, chord]) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "key";
        btn.dataset.key = id;
        btn.textContent = label;
        btn.title = chord;
        btn.setAttribute("aria-label", chord);
        btn.addEventListener("click", () => {
          activity(6);
          act({ type: "shortcut", id });
        });
        box.appendChild(btn);
      });
      return { update() {} };
    },
  };

  /* -- typing bar ------------------------------------------------------- */

  let typeDraft = "";

  WIDGETS.typebar = {
    tpl: "tpl-typebar",
    mount(node) {
      const field = node.querySelector("[data-field]");
      const sendBtn = node.querySelector("[data-send]");
      const enterBtn = node.querySelector("[data-send-enter]");
      field.value = typeDraft;
      field.addEventListener("input", () => { typeDraft = field.value; });

      async function send(withEnter) {
        const text = field.value;
        if (!text && !withEnter) return;
        try {
          await api("api/type", {
            method: "POST",
            body: JSON.stringify({ text, submit: withEnter }),
          });
        } catch (err) {
          if (err.message !== "unauthorized") toast(err.message);
          return;
        }
        field.value = "";
        typeDraft = "";
        activity(8);
        toast(withEnter ? "Sent + Enter" : "Sent");
      }

      sendBtn.addEventListener("click", () => send(false));
      enterBtn.addEventListener("click", () => send(true));
      field.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); send(true); }
      });
      return { update() {} };
    },
  };

  /* ------------------------------------------------------------ pairing */

  function unpair() {
    localStorage.removeItem(KEY_TOKEN);
    store.token = "";
    if (store.ws) { try { store.ws.close(); } catch (_) {} store.ws = null; }
    if (store.pollTimer) { clearInterval(store.pollTimer); store.pollTimer = null; }
    $("dot").className = "dot off";
    if (store.dev) { toast("Dev mode: no pairing needed"); connect(); return; }
    clearWidgets();
    $("pairOverlay").hidden = false;
    $("pinInput").focus();
  }

  $("nameInput").value = store.name || guessName();

  $("pairBtn").addEventListener("click", async () => {
    const pin = $("pinInput").value.trim();
    const name = $("nameInput").value.trim() || guessName();
    const err = $("pairErr");
    err.textContent = "";
    if (!/^\d{6}$/.test(pin)) { err.textContent = "PIN is 6 digits."; return; }
    $("pairBtn").disabled = true;
    try {
      const res = await fetch("api/pair", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pin, name }),
      });
      if (!res.ok) {
        err.textContent = res.status === 403 ? "Wrong PIN, or temporarily locked out." : "Pairing failed.";
        return;
      }
      const data = await res.json();
      store.token = data.token;
      store.name = name;
      localStorage.setItem(KEY_TOKEN, data.token);
      localStorage.setItem(KEY_NAME, name);
      $("pairOverlay").hidden = true;
      $("pinInput").value = "";
      await loadLayout();
      connect();
    } catch (e) {
      err.textContent = "Can't reach the laptop.";
    } finally {
      $("pairBtn").disabled = false;
    }
  });

  $("pinInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter") $("pairBtn").click();
  });

  /* ------------------------------------------------------------- socket */

  function connect() {
    if (!store.token && !store.dev) { $("pairOverlay").hidden = false; return; }
    const url = new URL("ws", document.baseURI);
    url.protocol = location.protocol === "https:" ? "wss:" : "ws:";
    if (store.token) url.searchParams.set("token", store.token);
    let ws;
    try { ws = new WebSocket(url); } catch (e) { schedulePoll(); return; }
    store.ws = ws;

    ws.onopen = () => {
      store.retries = 0;
      $("dot").className = "dot on";
      if (store.pollTimer) { clearInterval(store.pollTimer); store.pollTimer = null; }
    };
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === "state") applyState(msg.state);
      } catch (_) {}
    };
    ws.onclose = (ev) => {
      store.ws = null;
      if (ev.code === 4401) { unpair(); return; }
      $("dot").className = "dot off";
      if (store.retries < 6) {
        store.retries += 1;
        setTimeout(connect, Math.min(8000, 500 * 2 ** store.retries));
      } else {
        $("dot").className = "dot off";
        schedulePoll();
      }
    };
    ws.onerror = () => { try { ws.close(); } catch (_) {} };
  }

  function schedulePoll() {
    if (store.pollTimer) return;
    store.pollTimer = setInterval(async () => {
      try {
        const state = await api("api/state");
        $("dot").className = "dot on";
        applyState(state);
      } catch (_) {
        $("dot").className = "dot off";
      }
    }, 2000);
  }

  /* ------------------------------------------------- pointer input channel

     The state socket carries once-a-second snapshots; this one carries pointer
     batches, and only exists while the Mouse tile is mounted. One ordered TCP
     stream instead of an HTTP request per animation frame. If it is down, the
     caller falls back to POST /api/pointer. */

  const input = {
    ws: null,
    open: false,
    want: 0,
    retry: 0,
    timer: 0,
  };

  let pointerWarnAt = 0;

  function inputConnect() {
    if (input.ws || input.want <= 0) return;
    if (!store.token && !store.dev) return;
    const url = new URL("ws-input", document.baseURI);
    url.protocol = location.protocol === "https:" ? "wss:" : "ws:";
    if (store.token) url.searchParams.set("token", store.token);
    let ws;
    try { ws = new WebSocket(url); } catch (_) { return; }
    input.ws = ws;
    ws.onopen = () => { input.open = true; input.retry = 0; };
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg && msg.type === "error" && msg.detail) toast(msg.detail);
      } catch (_) {}
    };
    ws.onclose = (ev) => {
      const was = input.ws;
      input.open = false;
      if (input.ws === was) input.ws = null;
      if (ev.code === 4401) { unpair(); return; }
      if (input.want > 0) {
        input.retry += 1;
        input.timer = setTimeout(inputConnect, Math.min(4000, 300 * 2 ** input.retry));
      }
    };
    ws.onerror = () => { try { ws.close(); } catch (_) {} };
  }

  function inputHold() {
    input.want += 1;
    inputConnect();
  }

  function inputRelease() {
    input.want = Math.max(0, input.want - 1);
    if (input.want > 0) return;
    clearTimeout(input.timer);
    input.timer = 0;
    input.retry = 0;
    const ws = input.ws;
    input.ws = null;
    input.open = false;
    if (ws) { try { ws.close(); } catch (_) {} }
  }

  // One entry point for every pointer batch (touchpad, momentum, click bar).
  function sendPointer(events) {
    if (!events || !events.length) return;
    const ws = input.ws;
    // Never drop a click or a button transition: those are what "sticky"
    // bugs are made of. Moves may be skipped when the socket is congested.
    const critical = events.some((ev) => ev.t !== "move" && ev.t !== "scroll");
    if (ws && input.open && (critical || ws.bufferedAmount < 32768)) {
      try { ws.send(JSON.stringify({ t: "pointer", events })); return; } catch (_) {}
    } else if (ws && input.open) {
      return;
    }
    api("api/pointer", { method: "POST", body: JSON.stringify({ events }) }).catch((err) => {
      if (err.message === "unauthorized") return;
      const now = Date.now();
      if (now - pointerWarnAt > 4000) { pointerWarnAt = now; toast(err.message); }
    });
  }

  async function loadLayout() {
    try {
      store.layout = await api("api/layout");
    } catch (err) {
      if (err.message === "unauthorized") return false;
      console.warn("layout unavailable, using built-in deck", err);
      store.layout = FALLBACK_LAYOUT;
    }
    renderDeck();
    return true;
  }

  /* ----------------------------------------------------------- settings */

  const settings = $("settingsOverlay");

  $("settingsBtn").addEventListener("click", async () => {
    settings.hidden = false;
    const stat = $("statline");
    stat.innerHTML = "loading…";
    try {
      const st = await api("api/status");
      const s = store.state || {};
      const caps = s.caps || {};
      const on = (v) => (v ? "<b>yes</b>" : "no");
      const tiles = ((store.layout && store.layout.tiles) || []).length;
      stat.innerHTML =
        `Host <b>${st.host}</b>${st.simulate ? " · <b>simulate mode</b>" : ""}<br>` +
        `Volume ${on(caps.volume)} · Brightness ${on(caps.brightness)} · Media ${on(caps.media)}<br>` +
        `Deck <b>${tiles}</b> tile${tiles === 1 ? "" : "s"}` +
        (st.login ? `<br>Tailnet login <b>${st.login}</b>` : "");
      await loadDevices();
    } catch (e) {
      stat.textContent = "Couldn't load status.";
    }
  });

  $("closeSettings").addEventListener("click", () => { settings.hidden = true; });
  settings.addEventListener("click", (e) => { if (e.target === settings) settings.hidden = true; });

  async function loadDevices() {
    const list = $("deviceList");
    list.innerHTML = "";
    try {
      const data = await api("api/devices");
      data.devices.forEach((d) => {
        const li = document.createElement("li");
        const when = d.last_seen ? new Date(d.last_seen * 1000).toLocaleString() : "never";
        li.innerHTML = `<div><div class="nm"></div><div class="when"></div></div>`;
        li.querySelector(".nm").textContent = d.name || "device";
        li.querySelector(".when").textContent = "last seen " + when;
        const btn = document.createElement("button");
        btn.textContent = "Revoke";
        btn.addEventListener("click", async () => {
          if (!confirm(`Revoke "${d.name}"? It will need the PIN again.`)) return;
          await api("api/devices/revoke", {
            method: "POST",
            body: JSON.stringify({ id: d.id }),
          });
          toast("Revoked");
          loadDevices();
        });
        li.appendChild(btn);
        list.appendChild(li);
      });
      if (!data.devices.length) {
        list.innerHTML = '<li><span class="when">No devices paired yet.</span></li>';
      }
    } catch (e) {
      list.innerHTML = '<li><span class="when">Could not load devices.</span></li>';
    }
  }

  $("forgetBtn").addEventListener("click", () => {
    if (!confirm("Forget this device? You'll need the PIN to pair again.")) return;
    settings.hidden = true;
    unpair();
  });

  /* ---------------------------------------------------------------- boot */

  async function boot() {
    if (store.name) $("nameInput").value = store.name;
    try {
      const res = await fetch("api/status", { headers: {} });
      const st = await res.json();
      if (st.pin_enabled === false) {
        // --dev mode: loopback only, no auth. Skip pairing entirely.
        store.dev = true;
        $("pairOverlay").hidden = true;
      }
    } catch (_) { /* fall through to the normal path */ }

    if (!store.dev && !store.token) {
      $("pairOverlay").hidden = false;
      return;
    }
    const ok = await loadLayout();
    if (ok !== false) connect();
  }

  boot();

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && !store.ws && (store.token || store.dev)) {
      store.retries = 0;
      connect();
    }
  });

  if ("serviceWorker" in navigator && location.protocol === "https:") {
    navigator.serviceWorker.register("sw.js").catch(() => {});
  }
})();
