#!/usr/bin/env python3
import os
import sys

# Detect and configure the correct Qt Platform plugin. Returns the selected platform name. Defaults to X11 if no arguments passed.
def setup_qt_platform() -> str:
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
import subprocess
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from PyQt5.QtCore import Qt, QTimer, QReadWriteLock, pyqtSignal, QObject
from PyQt5.QtGui import QIcon, QPixmap, QPainter, QColor
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QComboBox, QListWidget,
    QListWidgetItem, QFileDialog, QMessageBox, QMenu, QInputDialog,
    QDialog,
)

# Constants
STATE_DIR = Path.home() / ".local/state/folder_monitor"
LOG_FILE = STATE_DIR / "log.txt"
SNAPSHOT_FILE = STATE_DIR / "snapshots.json"
FOLDER_LIST_FILE = STATE_DIR / "folders.json"
LAST_CHECK_FILE = STATE_DIR / "last_check.json"
WINDOW_STATE_FILE = STATE_DIR / "window_state.json"
BACKUP_TARGETS_FILE = STATE_DIR / "backup_targets.json"

STATE_DIR.mkdir(parents=True, exist_ok=True)


def log(msg: str, folder: str = None, operation_type: str = None):
    # Log a message while maintaining only the last snapshot and check per folder
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{timestamp}] {msg}\n"

    # Read existing log if it exists
    try:
        with open(LOG_FILE, "r") as f:
            lines = f.readlines()
    except FileNotFoundError:
        lines = []

    # Filter out old entries of the same type for this folder
    new_lines = []
    skip_next = False

    for line in lines:
        # Skip detail lines (those without timestamps)
        if not line.startswith("["):
            if skip_next:
                continue
            new_lines.append(line)
            continue

        # Check if this is an entry we might want to replace
        if folder and folder in line:
            if operation_type and any(op in line for op in ["Snapshot", "Changes", "No changes"]):
                # This is an operation we want to potentially replace
                current_op = "Snapshot" if "Snapshot" in line else "Check"
                if current_op == operation_type:
                    # Skip this line and its details (we'll add new one)
                    skip_next = True
                    continue

        skip_next = False
        new_lines.append(line)

    # Add our new entry
    new_lines.append(entry)

    # Write back to file
    with open(LOG_FILE, "w") as f:
        f.writelines(new_lines)

def clear_log():
    # Clear the log file while keeping one empty line
    with open(LOG_FILE, "w") as f:
        f.write("")  # Write empty file

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

class FolderSignals(QObject):
    operation_started = pyqtSignal(str)  # folder path
    operation_finished = pyqtSignal(str)  # folder path

class IntervalInputDialog(QDialog):
    def __init__(self, folder: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Update Interval")
        self.setModal(True)
        self.setMinimumWidth(300)

        self.valid = False
        self.result = None

        layout = QVBoxLayout()
        layout.addWidget(QLabel(f"Enter new interval for:\n{folder}"))

        self.input_line = QLineEdit()
        self.input_line.setPlaceholderText("1h30m, 45m, 2d)")
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
        #self.setMinimumWidth(150)  # Optionnal : set minimum size

        self.snapshots = self.load_json(SNAPSHOT_FILE)
        self.folder_intervals = self.load_json(FOLDER_LIST_FILE)
        self.last_check_times = self.load_json(LAST_CHECK_FILE)
        self.backup_targets = self.load_backup_targets()
        self.folder_statuses = {}
        self.active_operations = set()  # Track folders with ongoing operations
        self.signals = FolderSignals()
        self.executor = ThreadPoolExecutor()

        #Thread safety
        self.snapshots_lock = QReadWriteLock()
        self.folder_intervals_lock = QReadWriteLock()
        self.last_check_times_lock = QReadWriteLock()

        self.setup_ui(qt_platform)
        self.refresh_folder_list()

        # Connect signals
        self.signals.operation_started.connect(self.on_operation_started)
        self.signals.operation_finished.connect(self.on_operation_finished)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.check_due_folders)  # This used to be commented for some reason ?!
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

    # Saved backup destinations logic
    def load_backup_targets(self):
        if BACKUP_TARGETS_FILE.exists():
            try:
                with open(BACKUP_TARGETS_FILE, "r") as f:
                    return json.load(f)
            except Exception as e:
                log(f"Error loading backup_targets.json: {e}")
        return []

    def save_backup_targets(self, targets):
        try:
            with open(BACKUP_TARGETS_FILE, "w") as f:
                json.dump(targets, f)
        except Exception as e:
            log(f"Error saving backup_targets.json: {e}")

    # Persistent window size between launches
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
        return folder

    # Converts multi unit intervals to seconds
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

    # Context menu logic
    def show_context_menu(self, pos):
        self.backup_targets = self.load_backup_targets()  # ⬅️ Always refresh from disk
        item = self.folder_list.itemAt(pos)
        if not item:
            return

        folder_line = item.text().split('\n')[0].strip()
        menu = QMenu()

        # Create main menu
        update_action = menu.addAction("Update Interval")
        remove_action = menu.addAction("Remove Folder")
        check_action = menu.addAction("Check Now")
        open_action = menu.addAction("Open Folder")
        view_logs_action = menu.addAction("View Logs for Folder")

        # Create Backup submenu
        backup_menu = menu.addMenu("Backup to")
        backupMenu_browse = backup_menu.addAction("Browse ..")
        backupMenuAdd = backup_menu.addAction("Add destination ..")
        backupMenuManage = backup_menu.addAction("Manage destination ..")

        # Creates submenu based on backup_targets.json
        backup_actions = {}
        for path in self.backup_targets:
            action = backup_menu.addAction(path)
            backup_actions[action] = path

        # Main menu logic
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

        # Backup submenu logic
        elif action == backupMenu_browse:
            destination = self.browse_folder()
            self.backup(folder_line, destination)

        # Saves destinations for future use
        if action == backupMenuAdd:
            destination = self.browse_folder()
            if destination and destination not in self.backup_targets:
                self.backup_targets.append(destination)
                self.save_backup_targets(self.backup_targets)
            # Uncomment to run backup as well
            #self.backup(folder_line, destination)
        elif action in backup_actions:
            self.backup(folder_line, backup_actions[action])

        if action == backupMenuManage:
            # For Linux
            subprocess.run(['xdg-open', BACKUP_TARGETS_FILE])

    # Calls personnal script: run_rsync_backup_manager
    def backup(self, source, destination):
        full_destination = destination + source
        print(f"[INFO] source: {source} \n[INFO] Destination: {full_destination}")

        print("[INFO] Calling rsync_backup_manager")
        command = [
            "konsole",
            "--hold",
            "-e",
            "rsync_backup_manager.py", source, full_destination,
            "--dry-run",
        ]
        try:
            result = subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            print(f"[Success] Script return. \n{result.stdout}")
        except subprocess.CalledProcessError as e:
            print(f"[Error] Script failed: \n{e.stderr}")

    # Main logic
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

            # Determine which icon to show
            if folder in self.active_operations:
                symbol = "↻"  # Loading spinner (not animated)
                color = "blue"
            else:
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
        self.active_operations.discard(folder)

        self.save_json(FOLDER_LIST_FILE, self.folder_intervals)
        self.save_json(SNAPSHOT_FILE, self.snapshots)
        self.save_json(LAST_CHECK_FILE, self.last_check_times)

        self.refresh_folder_list()

    def check_single_folder(self, folder):
        now = time.time()
        self.last_check_times[folder] = now
        self.save_json(LAST_CHECK_FILE, self.last_check_times)
        self.signals.operation_started.emit(folder)
        self.executor.submit(self.check_folder, folder)
        self.refresh_folder_list()

    # View log with only specific folder information
    def view_logs_for_folder(self, folder):
        if not LOG_FILE.exists():
            QMessageBox.information(self, "No Log", "Log file does not exist.")
            return

        try:
            with open(LOG_FILE, "r") as f:
                lines = f.readlines()

            # Find the most recent snapshot and check entries
            snapshot_entry = None
            check_entry = None
            snapshot_details = []
            check_details = []

            i = 0
            while i < len(lines):
                line = lines[i]
                if folder in line:
                    if "Snapshot" in line:
                        snapshot_entry = line
                        # Collect subsequent non-timestamped lines as details
                        i += 1
                        while i < len(lines) and not lines[i].startswith("["):
                            snapshot_details.append(lines[i])
                            i += 1
                        continue
                    elif "Changes" in line or "No changes" in line:
                        check_entry = line
                        # Collect subsequent non-timestamped lines as details
                        i += 1
                        while i < len(lines) and not lines[i].startswith("["):
                            check_details.append(lines[i])
                            i += 1
                        continue
                i += 1

            if not snapshot_entry and not check_entry:
                QMessageBox.information(self, "No Entries", f"No log entries found for:\n{folder}")
                return

            temp_path = STATE_DIR / f"logs_{Path(folder).name}.txt"
            with open(temp_path, "w") as out:
                if snapshot_entry:
                    out.write(snapshot_entry)
                    out.writelines(snapshot_details)
                    out.write("\n")

                if check_entry:
                    out.write(check_entry)
                    out.writelines(check_details)

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
        self.signals.operation_started.emit(str(folder_path))
        self.executor.submit(self._snapshot_worker, folder_path)

    def _snapshot_worker(self, folder_path: Path):
        try:
            metadata = get_metadata(folder_path)
            try:
                self.snapshots_lock.lockForWrite()
                self.snapshots[str(folder_path)] = metadata
                self.save_json(SNAPSHOT_FILE, self.snapshots)
            finally:
                self.snapshots_lock.unlock()
            log(f"Snapshot updated for {folder_path}",
                folder=str(folder_path),
                operation_type="Snapshot")
        finally:
            self.signals.operation_finished.emit(str(folder_path))

    def run_check_all(self):
        now = time.time()
        for folder in self.folder_intervals:
            self.last_check_times[folder] = now
            self.signals.operation_started.emit(folder)
            self.executor.submit(self.check_folder, folder)
        self.save_json(LAST_CHECK_FILE, self.last_check_times)
        self.refresh_folder_list()

    def check_due_folders(self):
        now = time.time()
        updated = False
        for folder, interval in self.folder_intervals.items():
            last_time = self.last_check_times.get(folder, 0)
            if now - last_time > interval:
                self.last_check_times[folder] = now
                updated = True
                self.signals.operation_started.emit(folder)
                self.executor.submit(self.check_folder, folder)
        if updated:
            self.save_json(LAST_CHECK_FILE, self.last_check_times)
            self.refresh_folder_list()

    def check_folder(self, folder: str):
        try:
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
                log(f"Changes in {folder}:", folder=folder, operation_type="Check")
                for line in changed_files:
                    # Add details without timestamps (they'll be associated with the parent entry)
                    with open(LOG_FILE, "a") as f:
                        f.write(f"  {line}\n")
            else:
                self.folder_statuses[folder] = "ok"
                log(f"No changes in {folder}", folder=folder, operation_type="Check")
        finally:
            self.signals.operation_finished.emit(folder)

    def get_current_status(self):
        # Returns a string with current monitoring status
        status = []
        for folder in self.folder_intervals:
            last_check = self.last_check_times.get(folder, 0)
            last_check_str = datetime.fromtimestamp(last_check).strftime("%Y-%m-%d %H:%M:%S") if last_check else "Never"
            status_str = self.folder_statuses.get(folder, "unknown")
            status.append(f"{folder} - Last check: {last_check_str} - Status: {status_str}")
        return "\n".join(status)

    # Issue with icon being incorrecly set after update snapshot
    def update_snapshots(self):
        for folder in self.folder_intervals:
            self.signals.operation_started.emit(folder)
            self.take_snapshot(Path(folder))

    def open_log(self):
        os.system(f"xdg-open '{LOG_FILE}'")

    def on_operation_started(self, folder):
        self.active_operations.add(folder)
        self.refresh_folder_list()

    def on_operation_finished(self, folder):
        self.active_operations.discard(folder)
        self.refresh_folder_list()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = FolderMonitorWidget(selected_platform)
    win.show()
    app.aboutToQuit.connect(win.save_window_state)
    sys.exit(app.exec_())
