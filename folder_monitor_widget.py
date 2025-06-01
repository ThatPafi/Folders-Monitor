import os
import sys

def setup_qt_platform() -> str:
    """Detect and configure the correct Qt platform plugin. Returns the selected platform name."""
    force_wayland = "--wayland" in sys.argv
    force_xcb = "--xcb" in sys.argv

    sys.argv[:] = [arg for arg in sys.argv if arg not in ("--wayland", "--xcb")]

    if force_wayland:
        os.environ["QT_QPA_PLATFORM"] = "wayland"
        platform = "wayland (forced)"
    elif force_xcb:
        os.environ["QT_QPA_PLATFORM"] = "xcb"
        platform = "xcb (forced)"
    elif os.environ.get("WAYLAND_DISPLAY"):
        os.environ["QT_QPA_PLATFORM"] = "xcb"
        platform = "xcb (auto fallback from Wayland)"
    else:
        platform = os.environ.get("QT_QPA_PLATFORM", "default")

    print(f"[Info] Qt platform selected: {platform}")
    return platform

selected_platform = setup_qt_platform()

import json
import time
import re
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QIcon, QPixmap, QPainter, QColor
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QComboBox, QListWidget,
    QListWidgetItem, QFileDialog, QMessageBox, QMenu, QInputDialog,
    QDialog,
)

STATE_DIR = Path.home() / ".local/state/folder_monitor"
LOG_FILE = STATE_DIR / "log.txt"
SNAPSHOT_FILE = STATE_DIR / "snapshots.json"
FOLDER_LIST_FILE = STATE_DIR / "folders.json"
LAST_CHECK_FILE = STATE_DIR / "last_check.json"
WINDOW_STATE_FILE = STATE_DIR / "window_state.json"


STATE_DIR.mkdir(parents=True, exist_ok=True)

def log(msg: str):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a") as f:
        f.write(f"[{timestamp}] {msg}\n")

def get_metadata(folder: Path):
    metadata = {}
    for root, dirs, files in os.walk(folder):
        for fname in files:
            fpath = Path(root) / fname
            try:
                stat = fpath.stat()
                metadata[str(fpath)] = (stat.st_mtime, stat.st_size)
            except Exception as e:
                log(f"Error accessing {fpath}: {e}")
    return metadata

def colored_icon(symbol: str, color: str) -> QIcon:
    pixmap = QPixmap(32, 32)
    pixmap.fill(Qt.transparent)

    painter = QPainter(pixmap)
    painter.setPen(QColor(color))
    painter.setFont(painter.font())
    painter.drawText(pixmap.rect(), Qt.AlignCenter, symbol)
    painter.end()

    return QIcon(pixmap)

class IntervalInputDialog(QDialog):
    def __init__(self, folder: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Update Interval")
        self.setModal(True)
        self.setMinimumWidth(300)

        self.valid = False
        self.result = None

        layout = QVBoxLayout()
        layout.addWidget(QLabel(f"Enter new interval for:\n{folder}\n(e.g. 5m, 2h, 1d) or (1d2h, 6h2m):"))

        self.input_line = QLineEdit()
        self.input_line.setPlaceholderText("e.g. 10m, 5h, 30s")
        self.input_line.textChanged.connect(self.validate_input)
        layout.addWidget(self.input_line)

        # Buttons
        button_row = QHBoxLayout()
        self.ok_btn = QPushButton("OK")
        self.ok_btn.setEnabled(False)
        self.ok_btn.clicked.connect(self.accept)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)

        button_row.addWidget(self.ok_btn)
        button_row.addWidget(cancel_btn)
        layout.addLayout(button_row)

        self.setLayout(layout)

    def validate_input(self, text):
        pattern = r"^(\d+[smhd])+$"
        self.valid = re.fullmatch(pattern, text.strip().lower()) is not None
        self.ok_btn.setEnabled(self.valid)


    def get_interval(self):
        return self.input_line.text().strip().lower() if self.valid else None


class FolderMonitorWidget(QWidget):
    def __init__(self, qt_platform):
        super().__init__()
        self.setWindowTitle("Folder Monitor")
        self.load_window_state()
        #self.resize(500, 400)  # Optional: set initial size
        #self.setMinimumWidth(150)

        self.snapshots = self.load_json(SNAPSHOT_FILE)
        self.folder_intervals = self.load_json(FOLDER_LIST_FILE)
        self.last_check_times = self.load_json(LAST_CHECK_FILE)
        self.folder_statuses = {}
        self.executor = ThreadPoolExecutor()

        self.setup_ui(qt_platform)
        self.refresh_folder_list()

        self.timer = QTimer(self)
        # self.timer.timeout.connect(self.check_due_folders)
        self.timer.start(60000)

    def setup_ui(self, qt_platform):
        layout = QVBoxLayout()

        # Platform label
        platform_label = QLabel(f"Qt Platform: {qt_platform}")
        platform_label.setStyleSheet("font-size: 10pt; color: gray;")
        layout.addWidget(platform_label)

        # Inputs
        input_row = QHBoxLayout()
        self.folder_input = QLineEdit()
        self.folder_input.setPlaceholderText("Folder to monitor...")
        #browse_btn = QPushButton("📂")
        browse_btn = QPushButton("Browse")
        browse_btn.clicked.connect(self.browse_folder)

        self.time_input = QLineEdit()
        self.time_input.setPlaceholderText("Interval (e.g., 1h30m, 45m, 2d)")
        self.time_input.textChanged.connect(self.validate_interval_input)

        self.add_btn = QPushButton("Add")
        self.add_btn.setEnabled(False)  # Disabled by default
        self.add_btn.clicked.connect(self.add_folder)

        input_row.addWidget(self.folder_input)
        input_row.addWidget(browse_btn)
        input_row.addWidget(self.time_input)
        input_row.addWidget(self.add_btn)
        layout.addLayout(input_row)

        # Buttons
        button_row = QHBoxLayout()
        self.open_log_btn = QPushButton("Open Log")
        self.check_now_btn = QPushButton("Check Now")
        self.update_btn = QPushButton("Update Snapshots")

        self.open_log_btn.clicked.connect(self.open_log)
        self.check_now_btn.clicked.connect(self.run_check_all)
        self.update_btn.clicked.connect(self.update_snapshots)

        button_row.addWidget(self.open_log_btn)
        button_row.addWidget(self.check_now_btn)
        button_row.addWidget(self.update_btn)
        layout.addLayout(button_row)

        # Filter and Sort Controls
        control_row = QHBoxLayout()

        self.filter_input = QLineEdit()
        self.filter_input.setPlaceholderText("Filter by folder, interval, or last check...")
        self.filter_input.textChanged.connect(self.refresh_folder_list)

        self.sort_dropdown = QComboBox()
        self.sort_dropdown.addItems(["Folder", "Interval", "Last Checked", "Status"])
        self.sort_dropdown.currentIndexChanged.connect(self.refresh_folder_list)

        control_row.addWidget(QLabel("Filter:"))
        control_row.addWidget(self.filter_input)
        control_row.addWidget(QLabel("Sort by:"))
        control_row.addWidget(self.sort_dropdown)

        layout.addLayout(control_row)

        # Folder list
        self.folder_list = QListWidget()
        self.folder_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.folder_list.customContextMenuRequested.connect(self.show_context_menu)
        layout.addWidget(self.folder_list)

        self.setLayout(layout)

    def save_window_state(self):
        state = {
            "size": [self.size().width(), self.size().height()],
            "pos": [self.pos().x(), self.pos().y()]
        }
        with open(WINDOW_STATE_FILE, "w") as f:
            json.dump(state, f)

    def load_window_state(self):
        if WINDOW_STATE_FILE.exists():
            try:
                with open(WINDOW_STATE_FILE, "r") as f:
                    state = json.load(f)
                self.resize(*state.get("size", [500, 400]))
                self.move(*state.get("pos", [100, 100]))
            except Exception as e:
                log(f"Failed to load window state: {e}")
        else:
            self.resize(500, 400)  # default size


    def browse_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Folder")
        if folder:
            self.folder_input.setText(folder)


    def parse_multiunit_interval(self, s: str) -> int:
        """Parses strings like '1h30m', '2d4h' into seconds."""
        unit_multipliers = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}
        total = 0
        for number, unit in re.findall(r'(\d+)([smhd])', s):
            total += int(number) * unit_multipliers[unit]
        return total

    def validate_interval_input(self):
        text = self.time_input.text().strip().lower()
        is_valid = bool(re.fullmatch(r'(\d+[smhd])+', text))
        self.add_btn.setEnabled(is_valid)



    def show_context_menu(self, pos):
        item = self.folder_list.itemAt(pos)
        if not item:
            return

        folder_line = item.text().split('\n')[0].strip()
        menu = QMenu()

        update_action = menu.addAction("Update Interval")
        remove_action = menu.addAction("Remove Folder")
        check_action = menu.addAction("Check Now")
        open_action = menu.addAction("Open Folder")
        view_logs_action = menu.addAction("View Logs for Folder")

        action = menu.exec_(self.folder_list.mapToGlobal(pos))
        if action == update_action:
            self.update_folder_interval(folder_line)
        elif action == remove_action:
            self.remove_folder(folder_line)
        elif action == check_action:
            self.check_single_folder(folder_line)
        elif action == open_action:
            self.open_folder(folder_line)
        elif action == view_logs_action:
            self.view_logs_for_folder(folder_line)

    def interval_label(self, seconds: int) -> str:
        if seconds % 86400 == 0:
            return f"{seconds // 86400} days"
        elif seconds % 3600 == 0:
            return f"{seconds // 3600} hours"
        elif seconds % 60 == 0:
            return f"{seconds // 60} minutes"
        return f"{seconds} sec"

    def add_folder(self):
        folder = self.folder_input.text().strip()
        interval_str = self.time_input.text().strip().lower()

        if not folder or not re.fullmatch(r'(\d+[smhd])+', interval_str):
            return

        folder_path = Path(folder).resolve()
        if not folder_path.is_dir():
            return

        folder_str = str(folder_path)
        if folder_str in self.folder_intervals:
            QMessageBox.warning(self, "Duplicate", "Folder is already being monitored.")
            return

        interval = self.parse_multiunit_interval(interval_str)
        self.folder_intervals[folder_str] = interval
        self.last_check_times[folder_str] = 0

        self.take_snapshot(folder_path)
        self.save_json(FOLDER_LIST_FILE, self.folder_intervals)
        self.refresh_folder_list()


    def refresh_folder_list(self):
        self.folder_list.clear()
        filter_text = self.filter_input.text().strip().lower()
        sort_by = self.sort_dropdown.currentText()

        def sort_key(item):
            folder, interval = item
            status = self.folder_statuses.get(folder, "ok")
            last_checked = self.last_check_times.get(folder, 0)
            if sort_by == "Folder":
                return folder.lower()
            elif sort_by == "Interval":
                return interval
            elif sort_by == "Last Checked":
                return last_checked
            elif sort_by == "Status":
                return 0 if status == "ok" else 1
            return folder.lower()

        sorted_folders = sorted(self.folder_intervals.items(), key=sort_key)

        for folder, interval in sorted_folders:
            last_checked_ts = self.last_check_times.get(folder, 0)
            last_checked_str = datetime.fromtimestamp(last_checked_ts).strftime("%Y-%m-%d %H:%M:%S") if last_checked_ts else "never"
            interval_str = self.interval_label(interval)

            combined_text = f"{folder} {interval_str} {last_checked_str}".lower()
            if filter_text and filter_text not in combined_text:
                continue

            status = self.folder_statuses.get(folder, "ok")
            symbol = "✔" if status == "ok" else "❌"
            color = "green" if status == "ok" else "red"

            text = f"{folder}\n  Interval: {interval_str} | Last check: {last_checked_str}"
            item = QListWidgetItem(text)
            item.setIcon(colored_icon(symbol, color))
            self.folder_list.addItem(item)



    def update_folder_interval(self, folder):
        dialog = IntervalInputDialog(folder, self)
        if dialog.exec_() == QDialog.Accepted:
            new_input = dialog.get_interval()
            if new_input:
                try:
                    value = self.parse_interval_input(new_input)
                    self.folder_intervals[folder] = value
                    self.save_json(FOLDER_LIST_FILE, self.folder_intervals)
                    self.refresh_folder_list()
                except ValueError as e:
                    QMessageBox.warning(self, "Invalid Format", str(e))

    def parse_interval_input(self, input_str):
        input_str = input_str.strip().lower()
        pattern = r"(\d+)([smhd])"
        matches = re.findall(pattern, input_str)

        if not matches:
            raise ValueError("Invalid interval format. Use combinations like '1h30m', '2d4h', etc.")

        unit_seconds = {"s": 1, "m": 60, "h": 3600, "d": 86400}
        total_seconds = 0
        for value, unit in matches:
            total_seconds += int(value) * unit_seconds[unit]

        return total_seconds


    def open_folder(self, folder):
        os.system(f'xdg-open "{folder}"')

    def remove_folder(self, folder):
        confirm = QMessageBox.question(self, "Confirm Delete",
                                    f"Are you sure you want to stop monitoring this folder?\n\n{folder}")
        if confirm != QMessageBox.Yes:
            return

        self.folder_intervals.pop(folder, None)
        self.snapshots.pop(folder, None)
        self.last_check_times.pop(folder, None)
        self.folder_statuses.pop(folder, None)

        self.save_json(FOLDER_LIST_FILE, self.folder_intervals)
        self.save_json(SNAPSHOT_FILE, self.snapshots)
        self.save_json(LAST_CHECK_FILE, self.last_check_times)

        self.refresh_folder_list()

    def check_single_folder(self, folder):
        now = time.time()
        self.last_check_times[folder] = now
        self.save_json(LAST_CHECK_FILE, self.last_check_times)
        self.executor.submit(self.check_folder, folder)
        self.refresh_folder_list()

    def view_logs_for_folder(self, folder):
        if not LOG_FILE.exists():
            QMessageBox.information(self, "No Log", "Log file does not exist.")
            return

        try:
            with open(LOG_FILE, "r") as f:
                lines = f.readlines()

            filtered = [line for line in lines if folder in line]

            if not filtered:
                QMessageBox.information(self, "No Entries", f"No log entries found for:\n{folder}")
                return

            temp_path = STATE_DIR / "filtered_log.txt"
            with open(temp_path, "w") as out:
                out.writelines(filtered)

            os.system(f'xdg-open "{temp_path}"')
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to read log file:\n{e}")


    def load_json(self, path):
        if path.exists():
            try:
                with open(path, "r") as f:
                    return json.load(f)
            except Exception as e:
                log(f"Error loading {path.name}: {e}")
        return {}

    def save_json(self, path, data):
        try:
            with open(path, "w") as f:
                json.dump(data, f)
        except Exception as e:
            log(f"Error saving {path.name}: {e}")

    def take_snapshot(self, folder_path: Path):
        self.executor.submit(self._snapshot_worker, folder_path)

    def _snapshot_worker(self, folder_path: Path):
        metadata = get_metadata(folder_path)
        self.snapshots[str(folder_path)] = metadata
        self.save_json(SNAPSHOT_FILE, self.snapshots)
        log(f"Snapshot updated for {folder_path}")

    def run_check_all(self):
        now = time.time()
        for folder in self.folder_intervals:
            self.last_check_times[folder] = now
            self.executor.submit(self.check_folder, folder)
        self.save_json(LAST_CHECK_FILE, self.last_check_times)  # ✅ Save
        self.refresh_folder_list()

    def check_due_folders(self):
        now = time.time()
        updated = False
        for folder, interval in self.folder_intervals.items():
            last_time = self.last_check_times.get(folder, 0)
            if now - last_time > interval:
                self.last_check_times[folder] = now
                updated = True
                self.executor.submit(self.check_folder, folder)
        if updated:
            self.save_json(LAST_CHECK_FILE, self.last_check_times)
            self.refresh_folder_list()

    def check_folder(self, folder: str):
        log("")  # blank line between runs
        folder_path = Path(folder)
        current = get_metadata(folder_path)
        previous = self.snapshots.get(folder, {})

        changed_files = []
        for path, (mtime, size) in current.items():
            if path not in previous:
                changed_files.append(f"NEW: {path}")
            elif previous[path] != (mtime, size):
                changed_files.append(f"MODIFIED: {path}")

        deleted = set(previous.keys()) - set(current.keys())
        for path in deleted:
            changed_files.append(f"DELETED: {path}")

        if changed_files:
            self.folder_statuses[folder] = "changed"
            log(f"Changes in {folder}:")
            for line in changed_files:
                log(f"  {line}")
        else:
            self.folder_statuses[folder] = "ok"
            log(f"No changes in {folder}")

        self.refresh_folder_list()

    def update_snapshots(self):
        for folder in self.folder_intervals:
            self.take_snapshot(Path(folder))

    def open_log(self):
        os.system(f"xdg-open '{LOG_FILE}'")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = FolderMonitorWidget(selected_platform)
    win.show()
    app.aboutToQuit.connect(win.save_window_state)
    sys.exit(app.exec_())
