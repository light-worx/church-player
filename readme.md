# Church Media Player — Web Edition

This is the always-on server for the Church Media Player. It runs on
the church PC, owns the one real VLC instance, and plays audio/video
out of that machine's PA connection and screen. It also serves a
browser-based control page so playback can be **controlled from any
device on the same network** — laptop, tablet, or phone.

> **This server is the single source of truth for playback.** The
> desktop app (`church_player.py`, in the `church_player` folder) no
> longer runs its own VLC instance — it's just another control surface
> that talks to this same server, exactly like the browser page does.
> That means you can run this server **and** have the desktop app open
> **and** control things from a phone, all at the same time, with zero
> conflict — there's only ever one real player, and every control
> surface is a window onto the same live session. Keep this server
> running permanently (see the systemd setup below) so mobile/desktop
> control both work any time, whether or not anyone's physically at
> the church PC.

## Features

- **Two libraries, each its own tab and folder setting:**
  - **Music** — for background worship music. mp4s here play as
    audio-only by default.
  - **Videos** — for standalone video content (e.g. course sessions).
    Files here show video by default.
- Play / Pause / Stop with a 3-second fade-out on Stop
- Drag-and-drop playlist reordering per library (works with touch too,
  so you can reorder from a phone or tablet)
- Double-tap/double-click a song to move it to the top and play it
- Song / Artist columns, with the artist auto-detected from a trailing
  `(Artist Name)` in the filename
- Progress bar with elapsed/remaining time and click/drag-to-seek
- Volume control
- **Show video toggle** — appears whenever the current track is a
  video file, letting you switch it between audio-only and
  video-visible from your phone
- **Fullscreen toggle** — once video is showing, remotely control
  whether it's fullscreen on the church PC's screen
- Auto-advances to the next song in the same library when one finishes
- Volume is safely restored if the app is stopped/restarted mid fade-out
- Control from any device on the LAN at once (e.g. run playback from a
  tablet at the sound desk while it plays through the church PC)

## Setup (one-time)

1. Install VLC and xdotool on the church PC (xdotool is used to move
   VLC's video window onto the correct display -- see "Video
   playback" below):

   ```bash
   sudo apt update
   sudo apt install vlc xdotool
   ```

2. Install the Python packages:

   ```bash
   pip3 install -r requirements.txt
   ```

## Running it

From the `church_player_web` folder:

```bash
python3 server.py
```

You'll see something like:

```
Church Media Player web server starting on port 5000...
On this machine, open: http://localhost:5000
From another device on the LAN, use this machine's LAN IP address, e.g.:
  http://<this-computer's-ip-address>:5000
```

### Finding the church PC's IP address

Run this on the church PC:

```bash
hostname -I
```

It'll print something like `192.168.1.42`. From any other device on
the same Wi-Fi/network, open a browser to:

```
http://192.168.1.42:5000
```

### First-time setup in the browser

Scroll to the settings panel at the bottom of the page. There are two
folder boxes — **Music folder** and **Video folder** — each with its
own **Save** button. Type the folder path *as it exists on the church
PC* (e.g. `/home/yourname/Music`) into each, and save. These are
remembered for next time.

## Video playback

Showing video requires this process to have access to the church PC's
actual screen (the same way it needs access to its audio session).
When you toggle "Show video" on, VLC opens its own on-screen window on
the church PC and automatically goes fullscreen on your **second
display** (the projector) if one is detected. If nothing appears on
screen when you'd expect it to:

- Confirm the church PC is logged into its desktop session (not just
  showing a lock screen), since the video window needs a real display
  to draw into.
- Check the service logs (see below) for any error mentioning
  `DISPLAY` or similar — if the service can't see the desktop's
  display, this is the equivalent of the earlier PulseAudio access
  issue we solved for audio, and may need a similar fix.
- Try running `python3 server.py` directly in a terminal on the church
  PC's desktop first (rather than as a background service) to confirm
  it works there, exactly like we did when debugging the original
  audio issue.

**Which screen it picks:** the server detects connected displays via
`xrandr` and defaults to whichever one *isn't* the primary display
(since the projector is normally the extended/secondary one). If it
guesses wrong, use **Video > Choose Video Display...** in the desktop
app (or the equivalent dropdown in the web page's settings panel),
which lists the detected displays **by name** (e.g. `HDMI-1`) so you
can pick the right one — this is remembered for next time and stays
correct even on machines with 3+ displays. Moving the actual VLC
window onto that display requires `xdotool` (installed above) — if
it's missing, video still plays and still goes fullscreen, just
potentially on the wrong screen, and a note appears in the server logs
saying so.

If you set a display choice with an older version of this app (before
displays were identified by name), you'll need to re-pick it once —
the old setting isn't carried over automatically.

Use the **Fullscreen** button (web page or desktop app) any time you
need to temporarily step out of fullscreen — no need to walk over to
the PC.

**Important: don't close the video window with Alt+F4 (or Ctrl+F4).**
That's a window-manager-level shortcut that force-closes whatever
window has focus, bypassing the app entirely — since the video window
belongs to VLC itself, forcibly killing it that way can leave VLC in a
broken state (we've seen this cause an "X server failure" that
destabilized the video output). Always use the **Show video**
checkbox/toggle instead to turn video off — that does a controlled
shutdown rather than a forced window kill. The server is now written
to survive a stuck/hung video call without freezing the rest of the
app for other devices, but a truly broken video output may still need
a `systemctl --user restart church-player` to fully recover.

## The desktop app

There's also a native desktop control window (`church_player.py`, in
the `church_player` folder) that talks to this same server instead of
running its own VLC instance — see that folder's README. Since it's
just another client of this server, you can run it on the church PC
alongside this service running permanently in the background, and
control things from a phone at the same time, with no conflict.

## Firewall

If other devices can't reach the page, the church PC's firewall may be
blocking the connection. Allow port 5000:

```bash
sudo ufw allow 5000/tcp
```

(If you're not using `ufw`, check whatever firewall tool Linux Mint
has configured, or open port 5000 for local network traffic there.)

## Running it automatically on startup

Run it as a **user-level** systemd service (not a system-wide one) so
it has proper access to your desktop's audio and display sessions:

1. Create `~/.config/systemd/user/church-player.service`:

   ```ini
   [Unit]
   Description=Church Media Player Web Server
   After=default.target sound.target

   [Service]
   Type=simple
   WorkingDirectory=/path/to/church_player_web
   ExecStart=/usr/bin/python3 -u /path/to/church_player_web/server.py
   Restart=on-failure
   RestartSec=3

   [Install]
   WantedBy=default.target
   ```

   Replace the paths with wherever you saved these files.

2. Let it run even when logged out, and enable/start it:

   ```bash
   sudo loginctl enable-linger $USER
   systemctl --user daemon-reload
   systemctl --user enable church-player
   systemctl --user start church-player
   ```

   Note: no `sudo` on the `systemctl --user` commands.

3. Check it's running and watch its logs:

   ```bash
   systemctl --user status church-player
   journalctl --user -u church-player -f
   ```

Once set up, anyone on the LAN can open `http://<church-pc-ip>:5000`
whenever they need it — no need to open a terminal first, and it
survives reboots and logouts.

## Notes

- The "Song"/"Artist" parsing, drag-to-reorder, double-tap-to-play,
  fade-out-on-stop, and auto-advance all behave the same regardless of
  which control surface (browser or desktop app) triggers them — it's
  all the same server-side logic.
- Multiple devices (and the desktop app) can be connected at the same
  time; they'll all stay in sync (the page/app polls the server a
  couple of times a second).
- Video-folder items default to showing video; music-folder items
  (even mp4s) default to audio-only. Either can be toggled per-track
  from any control surface while it's playing.
- Each library folder can have **one level of subfolders** (e.g.
  `Music/Christmas/`, `Music/Easter/`) — files inside are shown with
  their subfolder name as a small badge next to the artist.
- The server exposes a small REST API (`/api/state`, `/api/play`,
  `/api/pause`, `/api/stop`, `/api/volume`, `/api/seek`, `/api/video`,
  `/api/fullscreen`, `/api/screens`, `/api/video_screen`,
  `/api/reorder`, `/api/settings`, `/api/refresh`) — this is what both
  the browser page and the desktop app talk to. Nothing else needs to
  be built on top of it, but it's there if you ever want to script
  something.