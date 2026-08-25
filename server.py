#!/usr/bin/env python3
"""
Church Media Player - Web Edition
-----------------------------------
Runs on the church media PC and serves a browser-based control page so
the playlist can be operated from any device on the same LAN (laptop,
tablet, phone) while audio plays through this machine's PA output.

This process is the single source of truth for playback -- it's the
only thing that ever touches VLC directly. Both the browser control
page AND the desktop app (church_player.py, running in client mode)
talk to this same server over HTTP, so there's never a risk of two
separate players fighting over the audio/video output: whichever
control surface you use, you're controlling the exact same live
session. Keep this running permanently (see the systemd setup notes
below) so mobile control works even when nobody's at the church PC.

Two separate libraries are supported: a Music folder (background
worship music, which may include mp4 files played as audio-only by
default) and a Video folder (standalone video content, e.g. course
sessions, shown with video on the church PC's screen by default). A
"Show video" toggle lets you switch either kind of file between
audio-only and video-visible. When video is shown, it automatically
opens fullscreen on your second display (the projector) if one is
detected via xrandr -- see "Choose Video Display" in either control
surface if auto-detection picks the wrong screen.

Note: showing video requires this process to have access to the
church PC's actual display (the same way it needed access to its
audio session -- see the audio troubleshooting notes in the README).
If the video toggle doesn't produce a visible window, that's the
first thing to check.

If the process is stopped or restarted (e.g. "systemctl restart") while
a Stop fade-out is still in progress, the volume is restored to normal
before the process exits, so it can never be left faded down.

Dependencies:
  sudo apt install vlc
  pip install Flask python-vlc

Run with:
  python3 server.py
"""

import atexit
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time

from flask import Flask, jsonify, render_template, request

try:
    import vlc
except ImportError:
    print("python-vlc is required. Install it with: pip install python-vlc")
    sys.exit(1)


CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "church_player")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

MEDIA_EXTENSIONS = {
    ".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".wma",
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm",
}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm"}

# The two libraries this app manages.
LIBRARIES = ("music", "video")

# Matches a trailing "(...)" group at the end of a filename, e.g.
# "Amazing Grace (John Newton)" -> song="Amazing Grace", artist="John Newton"
ARTIST_PATTERN = re.compile(r"^(.*?)\s*\(([^()]+)\)\s*$")

FADE_OUT_SECONDS = 3.0
FADE_TICK_SECONDS = 0.05

PORT = 5000


def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


def parse_song_and_artist(filename_no_ext):
    match = ARTIST_PATTERN.match(filename_no_ext)
    if match:
        song = match.group(1).strip()
        artist = match.group(2).strip()
        if song:
            return song, artist
    return filename_no_ext, ""


def is_video_file(path):
    if not path:
        return False
    ext = os.path.splitext(path)[1].lower()
    return ext in VIDEO_EXTENSIONS


def scan_media_folder(folder):
    """Scan a folder for media files, including one level of
    subfolders. Returns a list of (subfolder_name, filename, full_path)
    tuples -- subfolder_name is "" for files directly in the root
    folder. Root-level files come first, then each subfolder's files
    (grouped together, subfolders in alphabetical order). Only goes one
    level deep -- subfolders inside subfolders are not scanned."""
    entries = []

    def is_media(name):
        return os.path.splitext(name)[1].lower() in MEDIA_EXTENSIONS

    try:
        top_level = sorted(os.listdir(folder))
    except OSError:
        return entries

    for name in top_level:
        full = os.path.join(folder, name)
        if os.path.isfile(full) and is_media(name):
            entries.append(("", name, full))

    for name in top_level:
        full = os.path.join(folder, name)
        if not os.path.isdir(full):
            continue
        try:
            sub_names = sorted(os.listdir(full))
        except OSError:
            continue
        for sub_name in sub_names:
            sub_full = os.path.join(full, sub_name)
            if os.path.isfile(sub_full) and is_media(sub_name):
                entries.append((name, sub_name, sub_full))

    return entries


SCREEN_LINE_PATTERN = re.compile(
    r"^(\S+) connected (primary )?(\d+)x(\d+)\+(\d+)\+(\d+)"
)


def detect_screens():
    """Detect connected displays via xrandr (X11 only). Returns a list
    of dicts: {index, name, width, height, x, y, primary}. Returns an
    empty list if xrandr isn't available or nothing could be parsed
    (e.g. on Wayland) -- callers should treat that as "can't reposition,
    just show video wherever VLC opens it by default"."""
    try:
        result = subprocess.run(
            ["xrandr", "--query"], capture_output=True, text=True, timeout=3
        )
        output = result.stdout
    except Exception as e:
        print(f"[church-player] xrandr detection failed: {e}")
        return []

    screens = []
    for line in output.splitlines():
        m = SCREEN_LINE_PATTERN.match(line)
        if not m:
            continue
        name, is_primary, w, h, x, y = m.groups()
        screens.append({
            "index": len(screens),
            "name": name,
            "width": int(w),
            "height": int(h),
            "x": int(x),
            "y": int(y),
            "primary": bool(is_primary),
        })
    return screens


class PlayerState:
    """Holds both playlists and wraps the VLC player. All access goes
    through `lock` (an RLock, so helper methods can call each other
    safely while already holding it)."""

    def __init__(self):
        self.lock = threading.RLock()
        self.config = load_config()
        self.folders = {
            "music": self.config.get("music_folder", ""),
            "video": self.config.get("video_folder", ""),
        }

        # --aout=pulse: force PulseAudio explicitly rather than letting
        # VLC auto-detect, since auto-detection can pick plain ALSA in
        # this systemd-service context and silently miss the PA system.
        # No --no-video here (unlike before) -- video is now controlled
        # per-track via a ":no-video" media option, so the instance
        # itself stays capable of showing video when asked to.
        # (No --quiet here on purpose, so audio-device errors show up
        # in the terminal instead of being suppressed.)
        self.vlc_instance = vlc.Instance(["--aout=pulse"])
        self.player = self.vlc_instance.media_player_new()

        self.playlists = {"music": [], "video": []}  # each: {id, song, artist, path}
        self.next_id = 1
        self.current_id = None
        self.current_list = None
        self.video_enabled = False
        self.desired_volume = 80
        self.player.audio_set_volume(self.desired_volume)

        # Which physical display to show video on. None means
        # "auto-detect" (prefer whichever screen isn't the primary
        # one). Set explicitly via /api/video_screen if auto-detection
        # guesses wrong for your setup.
        self.video_screen_index = self.config.get("video_screen_index")
        self.screens_cache = detect_screens()

        self.fading = False

        # Auto-advance to the next song when the current one finishes
        # naturally (not on manual Stop, which is a different event).
        # VLC fires this on its own internal thread, so we hand off to a
        # fresh Python thread rather than doing playback work directly
        # inside the callback.
        self.player.event_manager().event_attach(
            vlc.EventType.MediaPlayerEndReached, self._on_end_reached
        )

        for kind in LIBRARIES:
            if self.folders[kind] and os.path.isdir(self.folders[kind]):
                self.rescan(kind)

    # ------------------------------------------------------------------
    # Playlist scanning
    # ------------------------------------------------------------------
    def rescan(self, kind=None):
        with self.lock:
            kinds = LIBRARIES if kind is None else (kind,)
            for k in kinds:
                folder = self.folders.get(k, "")
                self.playlists[k] = []
                if not folder or not os.path.isdir(folder):
                    continue
                for subfolder, name, full_path in scan_media_folder(folder):
                    base_name = os.path.splitext(name)[0]
                    song, artist = parse_song_and_artist(base_name)
                    self.playlists[k].append({
                        "id": self.next_id,
                        "song": song,
                        "artist": artist,
                        "folder": subfolder,
                        "path": full_path,
                    })
                    self.next_id += 1

    def set_folder(self, kind, folder):
        with self.lock:
            self.folders[kind] = folder
            self.config[f"{kind}_folder"] = folder
            save_config(self.config)
            self.rescan(kind)

    def reorder(self, kind, id_order):
        with self.lock:
            playlist = self.playlists[kind]
            by_id = {item["id"]: item for item in playlist}
            new_list = [by_id[i] for i in id_order if i in by_id]
            missing = [item for item in playlist if item["id"] not in id_order]
            self.playlists[kind] = new_list + missing

    def _find_track(self, track_id):
        """Search both libraries for a track id. Returns
        (kind, index, item) or None."""
        for kind in LIBRARIES:
            for idx, item in enumerate(self.playlists[kind]):
                if item["id"] == track_id:
                    return kind, idx, item
        return None

    def move_to_top(self, kind, track_id):
        with self.lock:
            playlist = self.playlists[kind]
            item = next((t for t in playlist if t["id"] == track_id), None)
            if item is None:
                return
            playlist.remove(item)
            playlist.insert(0, item)

    # ------------------------------------------------------------------
    # Playback
    # ------------------------------------------------------------------
    def _target_screen(self):
        """Figure out which physical display should show video, or
        None if we can't tell (e.g. xrandr unavailable/Wayland) -- in
        that case VLC just opens wherever it opens by default."""
        screens = self.screens_cache or detect_screens()
        if not screens:
            return None
        if self.video_screen_index is not None:
            for s in screens:
                if s["index"] == self.video_screen_index:
                    return s
        # Auto: prefer any screen other than the primary one, since the
        # projector is normally the secondary/extended display.
        for s in screens:
            if not s.get("primary"):
                return s
        return screens[0]

    def _make_media(self, path, want_video):
        media = self.vlc_instance.media_new(path)
        if not want_video:
            media.add_option(":no-video")
        return media

    def _safe_set_fullscreen(self, value):
        with self.lock:
            try:
                self.player.set_fullscreen(bool(value))
            except Exception as e:
                print(f"[church-player] set_fullscreen failed: {e}")

    def _find_vlc_window_id(self, retries=15, delay=0.15):
        """Poll for the actual X11 window VLC's video output creates.
        The window doesn't exist the instant play() returns -- it takes
        a moment for VLC to open it -- so this retries briefly rather
        than giving up after one attempt."""
        for _ in range(retries):
            try:
                result = subprocess.run(
                    ["xdotool", "search", "--class", "vlc"],
                    capture_output=True, text=True, timeout=2,
                )
                ids = [i for i in result.stdout.split() if i]
                if ids:
                    return ids[-1]  # most recently matched
            except FileNotFoundError:
                print("[church-player] xdotool not installed -- "
                      "can't reposition the video window. "
                      "Install it with: sudo apt install xdotool")
                return None
            except Exception as e:
                print(f"[church-player] xdotool search failed: {e}")
                return None
            time.sleep(delay)
        print("[church-player] gave up looking for VLC's video window")
        return None

    def _apply_video_placement(self):
        """Move VLC's actual video window onto the target screen, then
        fullscreen it there. This is the part that makes the display
        choice actually take effect -- libvlc's own --video-x/-y media
        options aren't reliably honored by modern video output modules
        for window placement, so we instead find the real window with
        xdotool and move it ourselves before requesting fullscreen."""
        screen = self._target_screen()
        if screen is not None:
            win_id = self._find_vlc_window_id()
            if win_id:
                try:
                    subprocess.run(
                        ["xdotool", "windowmove", win_id, str(screen["x"]), str(screen["y"])],
                        timeout=2,
                    )
                    subprocess.run(
                        ["xdotool", "windowsize", win_id, str(screen["width"]), str(screen["height"])],
                        timeout=2,
                    )
                    print(f"[church-player] moved VLC window {win_id} to "
                          f"{screen['name']} ({screen['x']},{screen['y']})")
                except Exception as e:
                    print(f"[church-player] failed to move VLC window: {e}")
        self._safe_set_fullscreen(True)

    def play(self, track_id, move_to_top=False, video_enabled=None):
        with self.lock:
            self.cancel_fade()

            found = self._find_track(track_id)
            if found is None:
                return False
            kind, idx, item = found

            if move_to_top:
                self.move_to_top(kind, track_id)

            # Video-folder items show video by default; music-folder
            # items (even mp4s) default to audio-only, unless the
            # caller explicitly requested a state.
            if video_enabled is None:
                video_enabled = (kind == "video")
            video_enabled = bool(video_enabled) and is_video_file(item["path"])

            media = self._make_media(item["path"], video_enabled)
            self.player.set_media(media)
            self.player.audio_set_volume(self.desired_volume)
            self.player.play()
            self.current_id = track_id
            self.current_list = kind
            self.video_enabled = video_enabled
            if video_enabled:
                # Give the video output a moment to actually exist
                # before asking it to go fullscreen.
                threading.Timer(0.3, self._apply_video_placement).start()
            # Diagnostic: confirm VLC's own view of state and volume after
            # play() is called. If state prints "Error" or volume prints
            # -1, VLC failed to open the audio device -- check the
            # terminal for accompanying error text above this line.
            print(f"[church-player] play requested: {item['song']!r} "
                  f"list={kind} video={video_enabled} "
                  f"path={item['path']!r} state={self.player.get_state()} "
                  f"volume={self.player.audio_get_volume()}")
            return True

    def toggle_pause(self):
        with self.lock:
            self.cancel_fade()
            self.player.pause()

    def set_volume(self, value):
        with self.lock:
            value = max(0, min(100, int(value)))
            self.desired_volume = value
            if not self.fading:
                self.player.audio_set_volume(value)

    def seek(self, fraction):
        with self.lock:
            length = self.player.get_length()
            if length and length > 0:
                fraction = max(0.0, min(1.0, fraction))
                self.player.set_time(int(fraction * length))

    def set_video_enabled(self, value):
        """Toggle video on/off for whatever's currently playing. Only
        applies to actual video files; hot-swaps without losing your
        place if something is already playing."""
        with self.lock:
            if self.current_id is None:
                return
            found = self._find_track(self.current_id)
            if found is None:
                return
            _, _, item = found
            if not is_video_file(item["path"]):
                return

            want = bool(value)
            if want == self.video_enabled:
                return

            current_time = self.player.get_time()
            self.player.stop()
            media = self._make_media(item["path"], want)
            self.player.set_media(media)
            self.video_enabled = want
            self.player.play()
            print(f"[church-player] video toggled: now {want}")

            if want:
                threading.Timer(0.3, self._apply_video_placement).start()
            if current_time and current_time > 0:
                threading.Timer(
                    0.3, lambda: self.player.set_time(current_time)
                ).start()

    def set_fullscreen(self, value):
        self._safe_set_fullscreen(value)

    def refresh_screens(self):
        with self.lock:
            self.screens_cache = detect_screens()
            return self.screens_cache

    def set_video_screen(self, index):
        """Change which display video shows on. If something's
        currently showing video, reposition it immediately."""
        with self.lock:
            self.video_screen_index = index
            self.config["video_screen_index"] = index
            save_config(self.config)

            if self.current_id is not None and self.video_enabled:
                found = self._find_track(self.current_id)
                if found is not None:
                    _, _, item = found
                    current_time = self.player.get_time()
                    self.player.stop()
                    media = self._make_media(item["path"], True)
                    self.player.set_media(media)
                    self.player.play()
                    threading.Timer(0.3, self._apply_video_placement).start()
                    if current_time and current_time > 0:
                        threading.Timer(
                            0.3, lambda: self.player.set_time(current_time)
                        ).start()

    def play_next(self):
        """Advance to the next track in the current library's order, if
        there is one. Called when a track finishes naturally."""
        print("[church-player] play_next() invoked")
        with self.lock:
            if self.current_id is None or self.current_list is None:
                print("[church-player] play_next: nothing currently playing")
                return
            playlist = self.playlists[self.current_list]
            idx = next(
                (i for i, t in enumerate(playlist) if t["id"] == self.current_id),
                None,
            )
            if idx is None:
                print(f"[church-player] play_next: current_id {self.current_id} "
                      f"not found in {self.current_list} list, aborting")
                return
            next_idx = idx + 1
            print(f"[church-player] play_next: list={self.current_list} "
                  f"idx={idx}, next_idx={next_idx}, len={len(playlist)}")
            if next_idx < len(playlist):
                self.play(playlist[next_idx]["id"])
            else:
                print("[church-player] play_next: reached end of playlist")
                self.current_id = None
                self.current_list = None
                self.video_enabled = False

    def _on_end_reached(self, event):
        # Runs on VLC's internal event thread. We deliberately wait a
        # short moment before calling back into libvlc: calling play()
        # again immediately from inside (or right at the tail end of)
        # this callback can silently no-op, because libvlc is often
        # still finishing its own internal teardown of the track that
        # just ended at the exact moment this fires.
        print("[church-player] MediaPlayerEndReached event received")
        threading.Timer(0.3, self.play_next).start()

    # ------------------------------------------------------------------
    # Fade-out on stop
    # ------------------------------------------------------------------
    def stop(self):
        with self.lock:
            if self.fading:
                return  # already fading out, let it finish
            state = self.player.get_state()
            if state in (vlc.State.Playing, vlc.State.Paused):
                self._start_fade_out()
            else:
                self.player.stop()
                self.current_id = None
                self.current_list = None
                self.video_enabled = False

    def _start_fade_out(self):
        current_volume = self.player.audio_get_volume()
        if current_volume is None or current_volume <= 0:
            self._finish_stop()
            return
        self.fading = True
        threading.Thread(
            target=self._fade_worker, args=(current_volume,), daemon=True
        ).start()

    def _fade_worker(self, start_volume):
        start_time = time.monotonic()
        while True:
            time.sleep(FADE_TICK_SECONDS)
            with self.lock:
                if not self.fading:
                    return  # cancelled
                elapsed = time.monotonic() - start_time
                if elapsed >= FADE_OUT_SECONDS:
                    self._finish_stop()
                    return
                fraction_remaining = 1.0 - (elapsed / FADE_OUT_SECONDS)
                new_volume = int(round(start_volume * fraction_remaining))
                self.player.audio_set_volume(max(0, new_volume))

    def _finish_stop(self):
        self.fading = False
        self.player.stop()
        self.player.audio_set_volume(self.desired_volume)
        self.current_id = None
        self.current_list = None
        self.video_enabled = False

    def cancel_fade(self):
        with self.lock:
            if self.fading:
                self.fading = False
                self.player.audio_set_volume(self.desired_volume)

    def restore_volume_now(self):
        """Unconditionally push the volume back to its normal level,
        regardless of fade state. Used as a shutdown safety net so the
        process can never exit (whether stopped normally, restarted by
        systemd, or killed) while a fade-out has left things quiet --
        on many systems an app's own volume changes affect the shared
        system volume, so this matters beyond just this app."""
        with self.lock:
            self.fading = False
            try:
                self.player.audio_set_volume(self.desired_volume)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Status snapshot for the frontend
    # ------------------------------------------------------------------
    def snapshot(self):
        with self.lock:
            state = self.player.get_state()
            is_playing = state == vlc.State.Playing
            is_paused = state == vlc.State.Paused
            length = self.player.get_length()
            current = self.player.get_time()
            if length is None or length < 0:
                length = 0
            if current is None or current < 0:
                current = 0

            current_item = None
            if self.current_id is not None:
                found = self._find_track(self.current_id)
                if found is not None:
                    current_item = found[2]

            try:
                fullscreen = bool(self.player.get_fullscreen())
            except Exception:
                fullscreen = False

            return {
                "playlists": {
                    kind: [
                        {"id": t["id"], "song": t["song"], "artist": t["artist"], "folder": t.get("folder", "")}
                        for t in self.playlists[kind]
                    ]
                    for kind in LIBRARIES
                },
                "current_id": self.current_id,
                "current_list": self.current_list,
                "current_song": current_item["song"] if current_item else None,
                "current_artist": current_item["artist"] if current_item else None,
                "current_is_video": is_video_file(current_item["path"]) if current_item else False,
                "video_enabled": self.video_enabled,
                "fullscreen": fullscreen,
                "playing": is_playing,
                "paused": is_paused,
                "fading": self.fading,
                "elapsed_ms": current,
                "length_ms": length,
                "volume": self.desired_volume,
                "folders": dict(self.folders),
                "video_screen_index": self.video_screen_index,
            }


state = PlayerState()

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/state")
def api_state():
    return jsonify(state.snapshot())


@app.route("/api/play", methods=["POST"])
def api_play():
    data = request.get_json(force=True) or {}
    track_id = data.get("id")
    move_to_top = bool(data.get("move_to_top", False))
    if track_id is None:
        return jsonify({"error": "missing id"}), 400
    ok = state.play(int(track_id), move_to_top=move_to_top)
    if not ok:
        return jsonify({"error": "track not found"}), 404
    return jsonify(state.snapshot())


@app.route("/api/pause", methods=["POST"])
def api_pause():
    state.toggle_pause()
    return jsonify(state.snapshot())


@app.route("/api/stop", methods=["POST"])
def api_stop():
    state.stop()
    return jsonify(state.snapshot())


@app.route("/api/volume", methods=["POST"])
def api_volume():
    data = request.get_json(force=True) or {}
    value = data.get("value")
    if value is None:
        return jsonify({"error": "missing value"}), 400
    state.set_volume(value)
    return jsonify(state.snapshot())


@app.route("/api/seek", methods=["POST"])
def api_seek():
    data = request.get_json(force=True) or {}
    fraction = data.get("fraction")
    if fraction is None:
        return jsonify({"error": "missing fraction"}), 400
    state.seek(float(fraction))
    return jsonify(state.snapshot())


@app.route("/api/video", methods=["POST"])
def api_video():
    data = request.get_json(force=True) or {}
    value = data.get("value")
    if value is None:
        return jsonify({"error": "missing value"}), 400
    state.set_video_enabled(bool(value))
    return jsonify(state.snapshot())


@app.route("/api/fullscreen", methods=["POST"])
def api_fullscreen():
    data = request.get_json(force=True) or {}
    value = data.get("value")
    if value is None:
        return jsonify({"error": "missing value"}), 400
    state.set_fullscreen(bool(value))
    return jsonify(state.snapshot())


@app.route("/api/screens")
def api_screens():
    screens = state.refresh_screens()
    return jsonify({"screens": screens, "selected_index": state.video_screen_index})


@app.route("/api/video_screen", methods=["POST"])
def api_video_screen():
    data = request.get_json(force=True) or {}
    index = data.get("index", None)  # null/None means "auto"
    state.set_video_screen(int(index) if index is not None else None)
    return jsonify(state.snapshot())


@app.route("/api/reorder", methods=["POST"])
def api_reorder():
    data = request.get_json(force=True) or {}
    kind = data.get("list")
    order = data.get("order")
    if kind not in LIBRARIES:
        return jsonify({"error": "missing or invalid 'list'"}), 400
    if not isinstance(order, list):
        return jsonify({"error": "missing order"}), 400
    state.reorder(kind, [int(i) for i in order])
    return jsonify(state.snapshot())


@app.route("/api/settings", methods=["POST"])
def api_settings():
    data = request.get_json(force=True) or {}
    kind = data.get("list")
    folder = (data.get("folder") or "").strip()
    if kind not in LIBRARIES:
        return jsonify({"error": "missing or invalid 'list'"}), 400
    if not folder:
        return jsonify({"error": "missing folder"}), 400
    if not os.path.isdir(folder):
        return jsonify({"error": f"'{folder}' is not a valid folder on the server machine"}), 400
    state.set_folder(kind, folder)
    return jsonify(state.snapshot())


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    data = request.get_json(silent=True) or {}
    kind = data.get("list")
    state.rescan(kind if kind in LIBRARIES else None)
    return jsonify(state.snapshot())


def _handle_shutdown_signal(signum, frame):
    print(f"[church-player] received signal {signum}, restoring volume before exit")
    state.restore_volume_now()
    sys.exit(0)


def main():
    # Safety net: whether we're stopped normally (Ctrl+C), restarted by
    # systemd, or killed outright, make sure the volume is never left
    # faded down from an in-progress Stop.
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    signal.signal(signal.SIGINT, _handle_shutdown_signal)
    atexit.register(state.restore_volume_now)

    print(f"Church Media Player web server starting on port {PORT}...")
    print("On this machine, open: http://localhost:%d" % PORT)
    print("From another device on the LAN, use this machine's LAN IP address, e.g.:")
    print("  http://<this-computer's-ip-address>:%d" % PORT)
    app.run(host="0.0.0.0", port=PORT, threaded=True)


if __name__ == "__main__":
    main()