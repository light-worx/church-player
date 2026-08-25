#!/usr/bin/env python3
"""
Church Media Player - Desktop Client
--------------------------------------
A native PyQt5 control surface for the Church Media Player web server
(server.py). This app does NOT run its own VLC instance -- it talks to
the same server the browser control page uses, over the same small
HTTP API. That means you can have this open on the church PC AND
control things from a phone/tablet at the same time with zero risk of
conflict: there is only ever one real player (the server), and both
control surfaces are just windows onto the same live session.

Because of this, the server (server.py) must already be running
before you start this app -- normally as the permanent systemd --user
service described in the web edition's README. This app is meant to
be something you open on the church PC (or any machine on the LAN)
whenever you want a native window for controlling it, not something
that itself needs to be kept running.

Features (all driven through the server's live state):
  - Two separate libraries: Music and Video, one tab each
  - Playlist view (Song / Artist / Folder columns) with Play / Pause /
    Stop controls
  - Drag and drop to reorder each playlist
  - Double-click a song to move it to the top of its list and play it
    immediately
  - "Show video" toggle and Fullscreen control for whatever's playing
  - Progress bar showing elapsed / remaining time, with click-to-seek
  - Stop fades the volume out smoothly (handled server-side)
  - Auto-advance to the next song (handled server-side)

Dependencies:
  pip install PyQt5

Run with:
  python3 church_player.py
"""

import json
import os
import sys
import urllib.error
import urllib.request

from PyQt5.QtCore import Qt, QSize, QTimer
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QPushButton,
    QCheckBox,
    QLabel,
    QFileDialog,
    QMessageBox,
    QSlider,
    QAction,
    QTabWidget,
    QAbstractItemView,
    QInputDialog,
)


CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "church_player")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
DEFAULT_SERVER_URL = "http://localhost:5000"

LIBRARIES = ("music", "video")
LIBRARY_LABELS = {"music": "Music", "video": "Videos"}

# Table columns
COL_SONG = 0
COL_ARTIST = 1
COL_FOLDER = 2
ID_ROLE = Qt.UserRole  # stores the server-assigned track id, not a path


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


def format_time(ms):
    if ms is None or ms < 0:
        ms = 0
    total_seconds = ms // 1000
    minutes = total_seconds // 60
    seconds = total_seconds % 60
    return f"{minutes:02d}:{seconds:02d}"


class ApiClient:
    """Tiny HTTP client for the Church Media Player server's REST API.
    Deliberately uses only the standard library (urllib) so this app
    has no extra dependencies beyond PyQt5."""

    def __init__(self, base_url, timeout=3):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def get(self, path):
        with urllib.request.urlopen(self.base_url + path, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def post(self, path, payload=None):
        data = json.dumps(payload or {}).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + path, data=data,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))


class ReorderableTableWidget(QTableWidget):
    """A QTableWidget whose built-in drag-and-drop reordering actually
    works correctly for multi-column rows.

    Qt's default InternalMove drag-drop for QTableWidget was really
    designed around single-column lists; with multiple columns it can
    leave a row's cells behind as blanks instead of moving the whole
    row cleanly. This subclass handles the drop itself: it lifts every
    cell out of the source row as a single unit and re-inserts them at
    the drop location, so nothing gets left behind.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDropIndicatorShown(True)
        self.on_row_moved = None  # callback(order: list[int]) set by owner

    def dropEvent(self, event):
        if event.source() is not self:
            event.ignore()
            return

        selected_rows = sorted(set(idx.row() for idx in self.selectedIndexes()))
        if len(selected_rows) != 1:
            event.ignore()
            return
        source_row = selected_rows[0]

        target_index = self.indexAt(event.pos())
        if target_index.isValid():
            target_row = target_index.row()
            row_rect = self.visualRect(target_index)
            if event.pos().y() > row_rect.center().y():
                target_row += 1
        else:
            target_row = self.rowCount()  # dropped below the last row

        if target_row == source_row or target_row == source_row + 1:
            event.ignore()
            return

        row_items = [self.takeItem(source_row, col) for col in range(self.columnCount())]
        self.removeRow(source_row)

        if target_row > source_row:
            target_row -= 1

        self.insertRow(target_row)
        for col, item in enumerate(row_items):
            self.setItem(target_row, col, item)

        self.selectRow(target_row)
        event.accept()

        if callable(self.on_row_moved):
            order = [self.item(r, COL_SONG).data(ID_ROLE) for r in range(self.rowCount())]
            self.on_row_moved(order)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Church Media Player")
        self.resize(600, 660)

        self.config = load_config()
        self.api = ApiClient(self.config.get("server_url", DEFAULT_SERVER_URL))

        self.selected_ids = {"music": None, "video": None}
        self._last_order = {"music": None, "video": None}
        self._seek_slider_dragging = False
        self._volume_slider_dragging = False
        self._last_state = None

        self._build_ui()
        self._build_menu()

        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(500)
        self.poll_timer.timeout.connect(self.poll_state)
        self.poll_timer.start()
        self.poll_state()  # immediate first fetch rather than waiting 500ms

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        self.status_label = QLabel("Connecting...")
        self.status_label.setStyleSheet("color: gray;")
        layout.addWidget(self.status_label)

        self.tab_widget = QTabWidget()
        self.tables = {}
        for kind in LIBRARIES:
            tab = QWidget()
            tab_layout = QVBoxLayout(tab)
            tab_layout.setContentsMargins(0, 6, 0, 0)
            table = self._build_table()
            table.cellDoubleClicked.connect(
                lambda row, col, k=kind: self.on_cell_double_clicked(row, col, k)
            )
            table.itemSelectionChanged.connect(
                lambda k=kind: self._on_selection_changed(k)
            )
            table.on_row_moved = lambda order, k=kind: self.on_row_moved(k, order)
            tab_layout.addWidget(table)
            self.tables[kind] = table
            self.tab_widget.addTab(tab, LIBRARY_LABELS[kind])
        layout.addWidget(self.tab_widget)

        self.now_playing_label = QLabel("Nothing playing")
        self.now_playing_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(self.now_playing_label)

        video_controls = QHBoxLayout()
        self.show_video_checkbox = QCheckBox("Show video")
        self.show_video_checkbox.setEnabled(False)
        self.show_video_checkbox.stateChanged.connect(self.on_show_video_toggled)
        video_controls.addWidget(self.show_video_checkbox)

        self.fullscreen_btn = QPushButton("Fullscreen")
        self.fullscreen_btn.setEnabled(False)
        self.fullscreen_btn.clicked.connect(self.toggle_fullscreen_video)
        video_controls.addWidget(self.fullscreen_btn)
        video_controls.addStretch()
        layout.addLayout(video_controls)

        progress_layout = QHBoxLayout()
        self.elapsed_label = QLabel("00:00")
        self.elapsed_label.setFixedWidth(45)
        progress_layout.addWidget(self.elapsed_label)

        self.seek_slider = QSlider(Qt.Horizontal)
        self.seek_slider.setRange(0, 1000)
        self.seek_slider.sliderPressed.connect(lambda: setattr(self, "_seek_slider_dragging", True))
        self.seek_slider.sliderReleased.connect(self._on_seek_slider_released)
        progress_layout.addWidget(self.seek_slider)

        self.remaining_label = QLabel("-00:00")
        self.remaining_label.setFixedWidth(50)
        self.remaining_label.setAlignment(Qt.AlignRight)
        progress_layout.addWidget(self.remaining_label)

        layout.addLayout(progress_layout)

        controls = QHBoxLayout()
        controls.addStretch()

        icon_size = QSize(28, 28)
        button_size = QSize(48, 48)

        self.play_btn = QPushButton()
        self._set_button_icon(self.play_btn, ["media-playback-start"], "Play")
        self.play_btn.setIconSize(icon_size)
        self.play_btn.setFixedSize(button_size)
        self.play_btn.setToolTip("Play")
        self.play_btn.clicked.connect(self.play_selected)
        controls.addWidget(self.play_btn)

        self.pause_btn = QPushButton()
        self._set_button_icon(self.pause_btn, ["media-playback-pause"], "Pause")
        self.pause_btn.setIconSize(icon_size)
        self.pause_btn.setFixedSize(button_size)
        self.pause_btn.setToolTip("Pause")
        self.pause_btn.clicked.connect(lambda: self._api_post_safe("/api/pause"))
        controls.addWidget(self.pause_btn)

        self.stop_btn = QPushButton()
        self._set_button_icon(self.stop_btn, ["media-playback-stop"], "Stop")
        self.stop_btn.setIconSize(icon_size)
        self.stop_btn.setFixedSize(button_size)
        self.stop_btn.setToolTip("Stop")
        self.stop_btn.clicked.connect(lambda: self._api_post_safe("/api/stop"))
        controls.addWidget(self.stop_btn)

        controls.addStretch()
        layout.addLayout(controls)

        vol_layout = QHBoxLayout()
        vol_layout.addWidget(QLabel("Volume"))
        self.volume_slider = QSlider(Qt.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(80)
        self.volume_slider.sliderPressed.connect(lambda: setattr(self, "_volume_slider_dragging", True))
        self.volume_slider.sliderReleased.connect(lambda: setattr(self, "_volume_slider_dragging", False))
        self.volume_slider.valueChanged.connect(
            lambda v: self._api_post_safe("/api/volume", {"value": v})
        )
        vol_layout.addWidget(self.volume_slider)
        layout.addLayout(vol_layout)

    def _build_table(self):
        table = ReorderableTableWidget(0, 3)
        table.setHorizontalHeaderLabels(["Song", "Artist", "Folder"])
        table.verticalHeader().setVisible(False)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setDragDropOverwriteMode(False)
        table.setShowGrid(False)
        header = table.horizontalHeader()
        header.setSectionResizeMode(COL_SONG, QHeaderView.Stretch)
        header.setSectionResizeMode(COL_ARTIST, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(COL_FOLDER, QHeaderView.ResizeToContents)
        return table

    @staticmethod
    def _set_button_icon(button, theme_names, fallback_text):
        for name in theme_names:
            icon = QIcon.fromTheme(name)
            if not icon.isNull():
                button.setIcon(icon)
                return
        button.setText(fallback_text)

    def _build_menu(self):
        menu = self.menuBar()

        file_menu = menu.addMenu("File")

        music_settings_action = QAction("Music Folder...", self)
        music_settings_action.triggered.connect(lambda: self.open_settings("music"))
        file_menu.addAction(music_settings_action)

        video_settings_action = QAction("Video Folder...", self)
        video_settings_action.triggered.connect(lambda: self.open_settings("video"))
        file_menu.addAction(video_settings_action)

        refresh_action = QAction("Refresh Playlists", self)
        refresh_action.triggered.connect(lambda: self._api_post_safe("/api/refresh"))
        file_menu.addAction(refresh_action)

        server_action = QAction("Server Address...", self)
        server_action.triggered.connect(self.change_server_address)
        file_menu.addAction(server_action)

        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        video_menu = menu.addMenu("Video")
        choose_display_action = QAction("Choose Video Display...", self)
        choose_display_action.triggered.connect(self.choose_video_display)
        video_menu.addAction(choose_display_action)

    # ------------------------------------------------------------------
    # Networking helpers
    # ------------------------------------------------------------------
    def _api_post_safe(self, path, payload=None):
        """Fire-and-forget POST -- errors just surface in the status
        line via the next poll rather than popping up dialogs, since
        transient network hiccups shouldn't interrupt the person."""
        try:
            self.api.post(path, payload)
        except Exception as e:
            self.status_label.setText(f"Couldn't reach server: {e}")

    def poll_state(self):
        try:
            data = self.api.get("/api/state")
        except Exception:
            self.status_label.setText(
                f"Disconnected from {self.api.base_url} — retrying..."
            )
            return
        self._last_state = data
        self.render(data)

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    def open_settings(self, kind):
        label = LIBRARY_LABELS[kind]
        start_dir = os.path.expanduser("~")
        if self._last_state:
            existing = self._last_state.get("folders", {}).get(kind)
            if existing:
                start_dir = existing
        folder = QFileDialog.getExistingDirectory(self, f"Select {label} Folder", start_dir)
        if folder:
            self._api_post_safe("/api/settings", {"list": kind, "folder": folder})

    def change_server_address(self):
        current = self.api.base_url
        text, ok = QInputDialog.getText(
            self, "Server Address",
            "Church Media Player server URL (this PC is usually 'http://localhost:5000'):",
            text=current,
        )
        if ok and text.strip():
            self.api.base_url = text.strip().rstrip("/")
            self.config["server_url"] = self.api.base_url
            save_config(self.config)
            self.status_label.setText("Connecting...")
            self.poll_state()

    # ------------------------------------------------------------------
    # Rendering server state into the UI
    # ------------------------------------------------------------------
    def render(self, data):
        self.status_label.setText(f"Connected to {self.api.base_url}")

        if data.get("current_song"):
            artist = data.get("current_artist")
            self.now_playing_label.setText(
                f"Now playing: {data['current_song']} — {artist}" if artist
                else f"Now playing: {data['current_song']}"
            )
        else:
            self.now_playing_label.setText("Nothing playing")

        if not self._seek_slider_dragging:
            length = data.get("length_ms", 0)
            elapsed = data.get("elapsed_ms", 0)
            fraction = (elapsed / length) if length > 0 else 0
            self.seek_slider.setValue(int(max(0, min(1, fraction)) * 1000))
        self.elapsed_label.setText(format_time(data.get("elapsed_ms", 0)))
        remaining = max(0, data.get("length_ms", 0) - data.get("elapsed_ms", 0))
        self.remaining_label.setText(f"-{format_time(remaining)}")

        if not self._volume_slider_dragging:
            self.volume_slider.blockSignals(True)
            self.volume_slider.setValue(data.get("volume", 80))
            self.volume_slider.blockSignals(False)

        is_video = data.get("current_is_video", False)
        self.show_video_checkbox.blockSignals(True)
        self.show_video_checkbox.setEnabled(is_video)
        self.show_video_checkbox.setChecked(bool(data.get("video_enabled")) if is_video else False)
        self.show_video_checkbox.blockSignals(False)
        self.fullscreen_btn.setEnabled(is_video and data.get("video_enabled", False))
        self.fullscreen_btn.setText("Exit Fullscreen" if data.get("fullscreen") else "Fullscreen")

        playlists = data.get("playlists", {})
        for kind in LIBRARIES:
            self.render_playlist(kind, playlists.get(kind, []), data.get("current_id"))

    def render_playlist(self, kind, items, current_id):
        table = self.tables[kind]

        order = ",".join(str(i["id"]) for i in items)
        if order != self._last_order[kind]:
            self._last_order[kind] = order
            table.setRowCount(0)
            for item in items:
                self._add_row(table, item["song"], item.get("artist", ""),
                              item.get("folder", ""), item["id"])

        self._apply_current_highlight(table, current_id)

    def _apply_current_highlight(self, table, current_id):
        """Bold the currently-playing row's song title. Deliberately
        avoids touching text color, since a hardcoded color can end up
        invisible against the system's actual light/dark theme -- bold
        stays readable either way."""
        for row in range(table.rowCount()):
            song_item = table.item(row, COL_SONG)
            if song_item is None:
                continue
            track_id = song_item.data(ID_ROLE)
            font = song_item.font()
            font.setBold(track_id == current_id)
            song_item.setFont(font)

    def _add_row(self, table, song, artist, folder_name, track_id, row=None):
        if row is None:
            row = table.rowCount()
        table.insertRow(row)

        song_item = QTableWidgetItem(song)
        song_item.setData(ID_ROLE, track_id)
        table.setItem(row, COL_SONG, song_item)

        artist_item = QTableWidgetItem(artist)
        artist_item.setForeground(Qt.gray)
        table.setItem(row, COL_ARTIST, artist_item)

        folder_item = QTableWidgetItem(folder_name)
        folder_item.setForeground(Qt.gray)
        table.setItem(row, COL_FOLDER, folder_item)

        return row

    def _current_row_data(self, table, row):
        song_item = table.item(row, COL_SONG)
        artist_item = table.item(row, COL_ARTIST)
        folder_item = table.item(row, COL_FOLDER)
        track_id = song_item.data(ID_ROLE)
        song = song_item.text()
        artist = artist_item.text() if artist_item else ""
        folder_name = folder_item.text() if folder_item else ""
        return song, artist, folder_name, track_id

    def _current_tab_kind(self):
        return LIBRARIES[self.tab_widget.currentIndex()]

    # ------------------------------------------------------------------
    # Playback controls
    # ------------------------------------------------------------------
    def play_selected(self):
        kind = self._current_tab_kind()
        table = self.tables[kind]
        row = table.currentRow()
        if row < 0:
            if table.rowCount() > 0:
                row = 0
                table.selectRow(0)
            else:
                QMessageBox.information(self, "No songs", "That playlist is empty.")
                return

        _, _, _, track_id = self._current_row_data(table, row)
        self._api_post_safe("/api/play", {"id": track_id})

    def on_cell_double_clicked(self, row, column, kind):
        table = self.tables[kind]
        song, artist, folder_name, track_id = self._current_row_data(table, row)

        # Optimistically move the row to the top locally for instant
        # feedback; the next poll will confirm the same order.
        table.removeRow(row)
        self._add_row(table, song, artist, folder_name, track_id, row=0)
        table.selectRow(0)
        self.selected_ids[kind] = track_id
        self._last_order[kind] = None  # force a re-sync on next poll

        self._api_post_safe("/api/play", {"id": track_id, "move_to_top": True})

    def _on_selection_changed(self, kind):
        table = self.tables[kind]
        row = table.currentRow()
        if row < 0:
            return
        song_item = table.item(row, COL_SONG)
        if song_item is None:
            return
        self.selected_ids[kind] = song_item.data(ID_ROLE)

    def on_row_moved(self, kind, order):
        self._last_order[kind] = ",".join(str(i) for i in order)
        self._api_post_safe("/api/reorder", {"list": kind, "order": order})

    # ------------------------------------------------------------------
    # Video display
    # ------------------------------------------------------------------
    def on_show_video_toggled(self, state):
        # render() blocks signals when syncing from polled state, so if
        # this fires it's a genuine user click, not a programmatic update.
        self._api_post_safe("/api/video", {"value": bool(state)})

    def toggle_fullscreen_video(self):
        currently_fullscreen = bool(self._last_state and self._last_state.get("fullscreen"))
        self._api_post_safe("/api/fullscreen", {"value": not currently_fullscreen})

    def choose_video_display(self):
        try:
            data = self.api.get("/api/screens")
        except Exception as e:
            QMessageBox.warning(self, "Couldn't reach server", str(e))
            return

        screens = data.get("screens", [])
        selected_index = data.get("selected_index")

        labels = ["Auto (prefer the non-primary display)"]
        for s in screens:
            labels.append(
                f"{s['index']}: {s['name']} ({s['width']}x{s['height']} at {s['x']},{s['y']})"
            )

        if not screens:
            QMessageBox.information(
                self, "No displays detected",
                "The server couldn't detect any displays via xrandr. "
                "Video will open wherever VLC opens it by default.",
            )

        current_row = 0
        if selected_index is not None:
            current_row = selected_index + 1  # offset for the "Auto" entry

        choice, ok = QInputDialog.getItem(
            self, "Choose Video Display",
            "Show video on:", labels, current_row, False,
        )
        if not ok or not choice:
            return

        if choice.startswith("Auto"):
            self._api_post_safe("/api/video_screen", {"index": None})
        else:
            idx = int(choice.split(":")[0])
            self._api_post_safe("/api/video_screen", {"index": idx})

    # ------------------------------------------------------------------
    # Progress bar
    # ------------------------------------------------------------------
    def _on_seek_slider_released(self):
        fraction = self.seek_slider.value() / 1000.0
        self._api_post_safe("/api/seek", {"fraction": fraction})
        self._seek_slider_dragging = False


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Church Media Player")
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()