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
        # guesses wrong for your setup. Identified by NAME (e.g.
        # "HDMI-1"), not list position -- a numeric index recomputed
        # fresh from xrandr's current output order isn't stable enough
        # once there are 3+ displays involved, since the order it's
        # reported in can shift between the moment you pick one and the
        # moment we actually need it.
        self.video_screen_name = self.config.get("video_screen_name")

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
        that case VLC just opens wherever it opens by default.

        Always re-detects fresh rather than trusting a cache, and
        matches by display NAME rather than list position -- see the
        comment on video_screen_name for why."""
        screens = detect_screens()
        if not screens:
            return None

        if self.video_screen_name is not None:
            for s in screens:
                if s["name"] == self.video_screen_name:
                    return s
            print(f"[church-player] selected display {self.video_screen_name!r} "
                  f"not found among currently detected displays "
                  f"({[s['name'] for s in screens]}) -- falling back to auto")

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
        else:
            # Force software decoding. Hardware-accelerated decode/output
            # has shown real instability on some machines (failed VA-API
            # drivers, decode errors, and in the worst case a hung X
            # connection) -- software decoding is slower but far more
            # reliable for something that has to run unattended.
            media.add_option(":avcodec-hw=none")
        return media

    # ------------------------------------------------------------------
    # Safe wrappers around direct libvlc calls
    #
    # None of these ever run while self.lock is held, and the riskiest
    # ones (play/stop/fullscreen -- the calls that actually talk to the
    # X server) go through _call_with_timeout so that even a genuinely
    # hung call can't block the request that triggered it forever. This
    # matters because things outside our control can put VLC's video
    # output into a bad state -- e.g. force-closing its window with
    # Alt+F4, or a flaky graphics driver -- and when that happens we'd
    # rather have one action fail/time out than freeze the whole server
    # for every connected device.
    # ------------------------------------------------------------------
    def _call_with_timeout(self, func, timeout=3.0, name="vlc call"):
        result = {}

        def runner():
            try:
                result["value"] = func()
            except Exception as e:
                result["error"] = e

        t = threading.Thread(target=runner, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            print(f"[church-player] {name} did not return within {timeout}s -- "
                  f"giving up waiting on it (it may finish in the background, "
                  f"but we're not blocking on it any further)")
            return None
        if "error" in result:
            print(f"[church-player] {name} raised: {result['error']}")
            return None
        return result.get("value")

    def _safe_play(self):
        self._call_with_timeout(self.player.play, name="player.play()")

    def _safe_stop(self):
        self._call_with_timeout(self.player.stop, name="player.stop()")

    def _safe_set_fullscreen(self, value):
        self._call_with_timeout(
            lambda: self.player.set_fullscreen(bool(value)),
            name="player.set_fullscreen()",
        )

    def _safe_set_media(self, media):
        self._call_with_timeout(lambda: self.player.set_media(media), name="player.set_media()")

    def _safe_audio_set_volume(self, value):
        try:
            self.player.audio_set_volume(value)
        except Exception as e:
            print(f"[church-player] audio_set_volume failed: {e}")

    def _safe_get_time(self):
        try:
            return self.player.get_time()
        except Exception as e:
            print(f"[church-player] get_time failed: {e}")
            return None

    def _safe_set_time(self, value):
        try:
            self.player.set_time(value)
        except Exception as e:
            print(f"[church-player] set_time failed: {e}")

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

    def _reassert_volume_soon(self):
        """Push the desired volume again a moment after playback starts.
        Some audio servers (PulseAudio/PipeWire's stream-restore
        behavior) automatically apply a remembered volume to a brand
        new audio stream the instant it's created -- which can silently
        override whatever we set beforehand, especially right after a
        fade-out left the last stream at (or near) zero. Re-asserting
        once the new stream actually exists makes sure our value wins."""
        threading.Timer(0.3, lambda: self._safe_audio_set_volume(self.desired_volume)).start()

    def play(self, track_id, move_to_top=False, video_enabled=None):
        self.cancel_fade()

        with self.lock:
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
            path = item["path"]
            song = item["song"]

        # The actual VLC calls happen outside the lock, so a hang here
        # (e.g. a broken video output) can't block other clients.
        media = self._make_media(path, video_enabled)
        self._safe_set_media(media)
        self._safe_audio_set_volume(self.desired_volume)
        self._safe_play()
        self._reassert_volume_soon()

        with self.lock:
            self.current_id = track_id
            self.current_list = kind
            self.video_enabled = video_enabled

        if video_enabled:
            # Give the video output a moment to actually exist before
            # asking it to go fullscreen.
            threading.Timer(0.3, self._apply_video_placement).start()

        print(f"[church-player] play requested: {song!r} "
              f"list={kind} video={video_enabled} path={path!r}")
        return True

    def toggle_pause(self):
        self.cancel_fade()
        try:
            self.player.pause()
        except Exception as e:
            print(f"[church-player] pause failed: {e}")

    def set_volume(self, value):
        value = max(0, min(100, int(value)))
        with self.lock:
            self.desired_volume = value
            fading = self.fading
        if not fading:
            self._safe_audio_set_volume(value)

    def seek(self, fraction):
        try:
            length = self.player.get_length()
        except Exception as e:
            print(f"[church-player] get_length failed: {e}")
            return
        if length and length > 0:
            fraction = max(0.0, min(1.0, fraction))
            self._safe_set_time(int(fraction * length))

    def set_video_enabled(self, value):
        """Toggle video on/off for whatever's currently playing. Only
        applies to actual video files; hot-swaps without losing your
        place if something is already playing."""
        self.cancel_fade()  # in case this is toggled mid-fade

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
            path = item["path"]

        current_time = self._safe_get_time()
        # Restore volume BEFORE stopping, not after: PulseAudio/PipeWire
        # remember a stream's volume at the moment it closes, so setting
        # it back up only after stop() is too late to matter for what
        # gets "remembered" for next time.
        self._safe_audio_set_volume(self.desired_volume)
        self._safe_stop()
        media = self._make_media(path, want)
        self._safe_set_media(media)
        self._safe_audio_set_volume(self.desired_volume)
        self._safe_play()
        self._reassert_volume_soon()

        with self.lock:
            self.video_enabled = want

        print(f"[church-player] video toggled: now {want}")
        if want:
            threading.Timer(0.3, self._apply_video_placement).start()
        if current_time and current_time > 0:
            threading.Timer(0.3, lambda: self._safe_set_time(current_time)).start()

    def set_fullscreen(self, value):
        self._safe_set_fullscreen(value)

    def refresh_screens(self):
        return detect_screens()

    def set_video_screen(self, name):
        """Change which display video shows on (by name, e.g. 'HDMI-1',
        or None for auto). If something's currently showing video,
        reposition it immediately."""
        self.cancel_fade()

        with self.lock:
            self.video_screen_name = name
            self.config["video_screen_name"] = name
            save_config(self.config)
            reposition = self.current_id is not None and self.video_enabled
            path = None
            if reposition:
                found = self._find_track(self.current_id)
                if found is not None:
                    path = found[2]["path"]
                else:
                    reposition = False

        if reposition and path:
            current_time = self._safe_get_time()
            self._safe_audio_set_volume(self.desired_volume)
            self._safe_stop()
            media = self._make_media(path, True)
            self._safe_set_media(media)
            self._safe_audio_set_volume(self.desired_volume)
            self._safe_play()
            self._reassert_volume_soon()
            threading.Timer(0.3, self._apply_video_placement).start()
            if current_time and current_time > 0:
                threading.Timer(0.3, lambda: self._safe_set_time(current_time)).start()

    def play_next(self):
        """Advance to the next track in the current library's order, if
        there is one. Called when a track finishes naturally."""
        print("[church-player] play_next() invoked")
        next_id = None
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
                next_id = playlist[next_idx]["id"]
            else:
                print("[church-player] play_next: reached end of playlist")
                self.current_id = None
                self.current_list = None
                self.video_enabled = False

        if next_id is not None:
            self.play(next_id)  # outside the lock -- play() takes its own

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
        try:
            state = self.player.get_state()
        except Exception as e:
            print(f"[church-player] get_state failed: {e}")
            state = None

        if state in (vlc.State.Playing, vlc.State.Paused):
            self._start_fade_out()
        else:
            self._safe_stop()
            with self.lock:
                self.current_id = None
                self.current_list = None
                self.video_enabled = False

    def _start_fade_out(self):
        try:
            current_volume = self.player.audio_get_volume()
        except Exception as e:
            print(f"[church-player] audio_get_volume failed: {e}")
            current_volume = None

        if current_volume is None or current_volume <= 0:
            self._finish_stop()
            return

        with self.lock:
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
                done = elapsed >= FADE_OUT_SECONDS
                if not done:
                    fraction_remaining = 1.0 - (elapsed / FADE_OUT_SECONDS)
                    new_volume = max(0, int(round(start_volume * fraction_remaining)))
            if done:
                self._finish_stop()
                return
            self._safe_audio_set_volume(new_volume)

    def _reset_system_output_volume(self):
        """Directly reset every detected OS-level audio sink's volume
        AND mute state via pactl, as a mechanism-agnostic safety net
        alongside our own libvlc volume calls.

        This exists because fading THIS app's stream volume down can,
        on some systems, bleed through into the shared system output
        level (flat-volumes / stream-restore-style behavior) -- and
        once that's happened, calling libvlc's own audio_set_volume()
        afterward isn't reliably enough to fix it, since there may be
        no live stream left for that call to act on (e.g. right after
        stop(), or at process exit). Directly resetting the shared
        sink(s) sidesteps whatever the exact underlying cause is, and
        matters beyond just this app -- a stuck-low or muted sink
        affects everything that plays audio on the machine, not just
        this player.

        Resets volume AND mute (a separate flag from volume -- 80%
        volume on a muted sink is still silent), and targets every
        sink pactl reports rather than just "@DEFAULT_SINK@", in case
        there's more than one audio device and the wrong one ends up
        being the actual default. Uses pactl since that works against
        both real PulseAudio and PipeWire's PulseAudio-compatible
        layer.
        """
        try:
            result = subprocess.run(
                ["pactl", "list", "short", "sinks"],
                capture_output=True, text=True, timeout=2,
            )
            sink_names = [
                line.split("\t")[1]
                for line in result.stdout.strip().splitlines()
                if line.strip() and "\t" in line
            ]
        except FileNotFoundError:
            print("[church-player] pactl not installed -- can't reset system "
                  "volume/mute. Install with: sudo apt install pulseaudio-utils")
            return
        except Exception as e:
            print(f"[church-player] pactl list sinks failed: {e}")
            sink_names = []

        targets = sink_names or ["@DEFAULT_SINK@"]
        for sink in targets:
            try:
                subprocess.run(
                    ["pactl", "set-sink-volume", sink, f"{self.desired_volume}%"],
                    capture_output=True, timeout=2,
                )
                subprocess.run(
                    ["pactl", "set-sink-mute", sink, "0"],
                    capture_output=True, timeout=2,
                )
            except Exception as e:
                print(f"[church-player] pactl reset failed for sink {sink!r}: {e}")
        print(f"[church-player] reset volume/mute on sink(s): {targets}")

    def _finish_stop(self):
        with self.lock:
            self.fading = False
        # Restore volume BEFORE stopping, not after: PulseAudio/PipeWire
        # remember a stream's volume at the moment it closes (separately
        # from the shared sink's own volume), so setting it back up only
        # after stop() is too late to matter for what gets "remembered"
        # for the next stream. A brief pause gives the volume change a
        # moment to actually register before the stream disappears.
        self._safe_audio_set_volume(self.desired_volume)
        time.sleep(0.1)
        self._safe_stop()
        self._reset_system_output_volume()
        with self.lock:
            self.current_id = None
            self.current_list = None
            self.video_enabled = False

    def cancel_fade(self):
        with self.lock:
            if not self.fading:
                return
            self.fading = False
        self._safe_audio_set_volume(self.desired_volume)
        self._reset_system_output_volume()

    def restore_volume_now(self):
        """Unconditionally push the volume back to its normal level,
        regardless of fade state. Used as a shutdown safety net so the
        process can never exit (whether stopped normally, restarted by
        systemd, or killed) while a fade-out has left things quiet --
        on many systems an app's own volume changes affect the shared
        system volume, so this matters beyond just this app."""
        with self.lock:
            self.fading = False
        self._safe_audio_set_volume(self.desired_volume)
        self._reset_system_output_volume()

    # ------------------------------------------------------------------
    # Status snapshot for the frontend
    # ------------------------------------------------------------------
    def snapshot(self):
        # Query the player itself OUTSIDE the shared lock. These calls
        # can in principle hang if the underlying graphics driver/X
        # connection wedges (see the note on _safe_set_fullscreen) --
        # if that ever happens, it should only affect the one request
        # doing the querying, not freeze the shared lock that every
        # other API call (from every other client) also needs.
        try:
            vlc_state = self.player.get_state()
        except Exception:
            vlc_state = None
        try:
            length = self.player.get_length()
        except Exception:
            length = 0
        try:
            current = self.player.get_time()
        except Exception:
            current = 0
        try:
            fullscreen = bool(self.player.get_fullscreen())
        except Exception:
            fullscreen = False

        is_playing = vlc_state == vlc.State.Playing
        is_paused = vlc_state == vlc.State.Paused
        if length is None or length < 0:
            length = 0
        if current is None or current < 0:
            current = 0

        with self.lock:
            current_item = None
            if self.current_id is not None:
                found = self._find_track(self.current_id)
                if found is not None:
                    current_item = found[2]

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
                "video_screen_name": self.video_screen_name,
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
    return jsonify({"screens": screens, "selected_name": state.video_screen_name})


@app.route("/api/video_screen", methods=["POST"])
def api_video_screen():
    data = request.get_json(force=True) or {}
    name = data.get("name", None)  # null/None means "auto"
    state.set_video_screen(name if name else None)
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