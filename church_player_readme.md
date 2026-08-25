# Church Media Player — Desktop Client

A native PyQt5 window for controlling the Church Media Player. This
app does **not** run its own copy of VLC — it's a control surface for
the [web server](../church_player_web) (`server.py`), talking to the
exact same small HTTP API that the browser control page uses.

## Why this matters

Because this app and the browser page both just *talk to* the same
server rather than each running their own player, you can have this
open on the church PC **and** control things from a phone or tablet
at the same time, with zero risk of conflict — there's only ever one
real VLC instance (the server's), and every control surface is just a
window onto that same live session. Pressing Play here and pressing
Stop from someone's phone a second later does exactly what you'd
expect, because they're both talking to the same thing.

## Requirements

- The web server (`server.py` in the `church_player_web` folder) must
  already be running before you open this app — normally as the
  permanent background service described in that folder's README.
  This app has nothing to control if the server isn't up.
- `pip install PyQt5` (that's it — no VLC or python-vlc needed here,
  since this app never touches VLC directly)

## Running it

```bash
python3 church_player.py
```

By default it connects to `http://localhost:5000`, which is correct
if you're running this on the same church PC as the server (the usual
setup). If you ever need to point it at a server on a different
machine, use **File > Server Address...**.

## Features

- Two tabs: **Music** and **Videos**, each showing that library's
  playlist (Song / Artist / Folder columns) live from the server
- **Play**, **Pause**, **Stop** (fades out over 3 seconds, handled by
  the server)
- **Drag and drop** to reorder each playlist — changes are sent to the
  server immediately, so any other connected device sees the new order
  too
- **Double-click** a song to move it to the top of its list and start
  playing it immediately
- **Show video** checkbox — appears whenever the current track is a
  video file. Toggling it tells the server to switch between
  audio-only and showing video (the video itself appears on the
  server machine's screen/projector, not in this app's window)
- **Fullscreen** button, and **Video > Choose Video Display...** to
  pick which physical display (e.g. the projector) shows video, if
  auto-detection picks the wrong one
- Progress bar with elapsed/remaining time and click-to-seek
- Volume slider
- **File > Music Folder...** / **Video Folder...** — pick folders
  using a normal native file browser (this assumes the app is running
  on the same machine as the server, since it's browsing the local
  filesystem)
- **File > Refresh Playlists** — re-scan both folders for new files
- Status line at the top shows whether it's currently connected to the
  server

## Usage

- Switch between the **Music** and **Videos** tabs to choose which
  library you're working with.
- Select a song and click **Play** to start it.
- Drag any song up or down to reorder that playlist — this updates the
  server (and therefore anyone else connected) right away.
- **Double-click** a song to instantly bump it to the top of its list
  and start playing it — handy for cueing up the next song on the fly.
- When the current track is a video file, a **Show video** checkbox
  appears. It automatically opens fullscreen on the projector (see
  below) — use the **Fullscreen** button if you need to temporarily
  step out of fullscreen.
- The window shows what's happening live, however it happens — whether
  you pressed Play here, or someone else did from their phone.

## Video display

Video actually renders on the **server machine's** screen (the church
PC), not inside this app's window — that's what lets it show up
properly on the projector no matter which device you're using to
control it. The server auto-detects your projector as the
non-primary display and opens video there in fullscreen automatically.
If it guesses the wrong screen, use **Video > Choose Video Display...**
in this app (or the equivalent on the web page) to pick the right one.

## Desktop shortcut

To add "Church Media Player" to your applications menu, make sure
`install_shortcut.sh` is in the **same folder** as `church_player.py`,
then run:

```bash
chmod +x install_shortcut.sh
./install_shortcut.sh
```

This automatically detects the correct path to `church_player.py` and
creates the shortcut for you — no manual editing needed. It may take a
moment (or a logout/login) to show up in the menu, under Sound &
Video.

If you ever move the `church_player.py` file to a different folder,
just re-run `install_shortcut.sh` to update the shortcut.
