import sys
import os
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, date, time, timedelta
from pathlib import Path

from PySide6.QtCore import Qt, QDate, QDateTime, QSize, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QStatusBar,
    QTimeEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

APP_NAME = "Maintenance Calendar"
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "maintenance_calendar.db"
PROMPT_FILE = BASE_DIR / "prompt list.txt"

# A 30-day logarithmic warning window. A deadline farther away than this
# gets zero stars. The final part of the window becomes increasingly urgent.
WARNING_WINDOW_DAYS = 30.0

SAFE_BG = "#EAF4EC"
APPROACHING_BG = "#FFF4CC"
URGENT_BG = "#FFE2B8"
CRITICAL_BG = "#FFD0D0"
OVERDUE_BG = "#F3B7B7"
COMPLETED_BG = "#E3EEE5"
NEUTRAL_BG = "#F2F1ED"
INK = "#292929"
MUTED = "#6A6A64"
GRID = "#C9C7C0"
PAPER = "#FCFBF7"
HEADER = "#EEECE5"


def dt_to_str(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat(sep=" ")


def str_to_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def qdate_to_date(qd: QDate) -> date:
    return date(qd.year(), qd.month(), qd.day())


def date_to_qdate(d: date) -> QDate:
    return QDate(d.year, d.month, d.day)


def qtime_to_time(qt) -> time:
    return time(qt.hour(), qt.minute())


def time_to_qtime(t: time):
    from PySide6.QtCore import QTime
    return QTime(t.hour, t.minute)


def now_local() -> datetime:
    return datetime.now().replace(second=0, microsecond=0)


def day_of_year(d: date) -> int:
    return d.timetuple().tm_yday


def date_from_day_of_year(year: int, doy: int) -> date:
    max_day = 366 if (date(year, 12, 31).timetuple().tm_yday == 366) else 365
    doy = max(1, min(doy, max_day))
    return date(year, 1, 1) + timedelta(days=doy - 1)


def format_relative_time(delta: timedelta) -> str:
    seconds = int(abs(delta.total_seconds()))
    if seconds < 60:
        value, unit = seconds, "second"
    elif seconds < 3600:
        value, unit = seconds // 60, "minute"
    elif seconds < 86400:
        value, unit = seconds // 3600, "hour"
    else:
        value, unit = seconds // 86400, "day"
    suffix = "" if value == 1 else "s"
    return f"{value} {unit}{suffix}"


def format_due_delta(deadline: datetime, reference: datetime | None = None) -> str:
    reference = reference or now_local()
    delta = deadline - reference
    if delta.total_seconds() > 0:
        return f"due in {format_relative_time(delta)}"
    if delta.total_seconds() == 0:
        return "due now"
    return f"overdue by {format_relative_time(delta)}"


def urgency_stars(deadline: datetime | None, reference: datetime | None = None) -> int:
    if deadline is None:
        return 0
    reference = reference or now_local()
    remaining_days = (deadline - reference).total_seconds() / 86400.0
    if remaining_days <= 0:
        return 5
    if remaining_days >= WARNING_WINDOW_DAYS:
        return 0
    # Logarithmic urgency: the warning accelerates as remaining time shrinks.
    closeness = 1.0 - (math.log1p(remaining_days) / math.log1p(WARNING_WINDOW_DAYS))
    return max(1, min(5, math.ceil(closeness * 5)))


def urgency_palette(deadline: datetime | None, completed: bool, reference: datetime | None = None):
    if completed:
        return COMPLETED_BG, "#315B3A"
    if deadline is None:
        return NEUTRAL_BG, INK
    reference = reference or now_local()
    delta = (deadline - reference).total_seconds()
    stars = urgency_stars(deadline, reference)
    if delta <= 0:
        return OVERDUE_BG, "#7E2222"
    if stars >= 4:
        return CRITICAL_BG, "#8B2929"
    if stars == 3:
        return URGENT_BG, "#8A4F00"
    if stars == 2:
        return APPROACHING_BG, "#786000"
    return SAFE_BG, "#376443"


def stars_text(stars: int) -> str:
    if stars <= 0:
        return ""
    return "★" * stars + "☆" * (5 - stars)


@dataclass
class Occurrence:
    id: int
    task_id: int
    scheduled_at: datetime
    deadline: datetime | None
    cycle_number: int | None
    visit_number: int | None
    completed: bool
    completed_at: datetime | None
    prompt_generated: bool


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.create_schema()

    def create_schema(self):
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                schedule_type TEXT NOT NULL,
                start_datetimes_json TEXT NOT NULL DEFAULT '[]',
                cycle_period_value INTEGER,
                cycle_period_unit TEXT,
                maximum_cycles INTEGER,
                first_deadline TEXT,
                distributed_start TEXT,
                distributed_end TEXT,
                visit_count INTEGER,
                created_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS occurrences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                scheduled_at TEXT NOT NULL,
                deadline TEXT,
                cycle_number INTEGER,
                visit_number INTEGER,
                completed INTEGER NOT NULL DEFAULT 0,
                completed_at TEXT,
                prompt_generated INTEGER NOT NULL DEFAULT 0,
                UNIQUE(task_id, scheduled_at),
                FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        self.conn.commit()
        self.set_default_setting("calendar_year", str(date.today().year))
        self.set_default_setting("planning_day", str(366 if date(date.today().year, 12, 31).timetuple().tm_yday == 366 else 365))

    def set_default_setting(self, key: str, value: str):
        self.conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value))
        self.conn.commit()

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str):
        self.conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (key, value))
        self.conn.commit()

    def create_task(self, data: dict) -> int:
        # JSON cannot serialize datetime objects directly. Store starting
        # datetimes as ISO strings so they can be reconstructed later.
        start_datetimes_json = json.dumps([
            dt_to_str(value) if isinstance(value, datetime) else str(value)
            for value in data.get("start_datetimes", [])
        ])
        cur = self.conn.execute(
            """
            INSERT INTO tasks(
                name, description, schedule_type, start_datetimes_json,
                cycle_period_value, cycle_period_unit, maximum_cycles,
                first_deadline, distributed_start, distributed_end,
                visit_count, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data["name"],
                data["description"],
                data["schedule_type"],
                start_datetimes_json,
                data.get("cycle_period_value"),
                data.get("cycle_period_unit"),
                data.get("maximum_cycles"),
                dt_to_str(data["first_deadline"]) if data.get("first_deadline") else None,
                dt_to_str(data["distributed_start"]) if data.get("distributed_start") else None,
                dt_to_str(data["distributed_end"]) if data.get("distributed_end") else None,
                data.get("visit_count"),
                dt_to_str(now_local()),
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_task(self, task_id: int):
        return self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()

    def get_tasks(self):
        return self.conn.execute("SELECT * FROM tasks WHERE active = 1 ORDER BY name COLLATE NOCASE").fetchall()

    def delete_task(self, task_id: int):
        self.conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        self.conn.commit()

    def occurrence_exists(self, task_id: int, scheduled_at: datetime) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM occurrences WHERE task_id = ? AND scheduled_at = ?",
            (task_id, dt_to_str(scheduled_at)),
        ).fetchone()
        return row is not None

    def add_occurrence(
        self,
        task_id: int,
        scheduled_at: datetime,
        deadline: datetime | None,
        cycle_number: int | None = None,
        visit_number: int | None = None,
    ):
        if self.occurrence_exists(task_id, scheduled_at):
            return
        self.conn.execute(
            """
            INSERT INTO occurrences(
                task_id, scheduled_at, deadline, cycle_number, visit_number
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                task_id,
                dt_to_str(scheduled_at),
                dt_to_str(deadline) if deadline else None,
                cycle_number,
                visit_number,
            ),
        )
        self.conn.commit()

    def get_occurrence(self, occurrence_id: int):
        # The occurrence dialog needs task-level fields such as name and description,
        # so return the occurrence joined to its parent task.
        return self.conn.execute(
            """
            SELECT o.*, t.name, t.description
            FROM occurrences o
            JOIN tasks t ON t.id = o.task_id
            WHERE o.id = ?
            """,
            (occurrence_id,),
        ).fetchone()

    def get_occurrences_between(self, start_dt: datetime, end_dt: datetime):
        return self.conn.execute(
            """
            SELECT o.*, t.name, t.description
            FROM occurrences o
            JOIN tasks t ON t.id = o.task_id
            WHERE o.scheduled_at >= ? AND o.scheduled_at < ?
            ORDER BY o.scheduled_at, t.name COLLATE NOCASE
            """,
            (dt_to_str(start_dt), dt_to_str(end_dt)),
        ).fetchall()

    def get_occurrences_for_day(self, d: date):
        start = datetime.combine(d, time.min)
        end = start + timedelta(days=1)
        return self.get_occurrences_between(start, end)

    def get_occurrence_object(self, row):
        return Occurrence(
            id=row["id"],
            task_id=row["task_id"],
            scheduled_at=str_to_dt(row["scheduled_at"]),
            deadline=str_to_dt(row["deadline"]) if row["deadline"] else None,
            cycle_number=row["cycle_number"],
            visit_number=row["visit_number"],
            completed=bool(row["completed"]),
            completed_at=str_to_dt(row["completed_at"]) if row["completed_at"] else None,
            prompt_generated=bool(row["prompt_generated"]),
        )

    def mark_complete(self, occurrence_id: int):
        self.conn.execute(
            "UPDATE occurrences SET completed = 1, completed_at = ? WHERE id = ?",
            (dt_to_str(now_local()), occurrence_id),
        )
        self.conn.commit()

    def mark_prompt_generated(self, occurrence_id: int):
        self.conn.execute(
            "UPDATE occurrences SET prompt_generated = 1 WHERE id = ?",
            (occurrence_id,),
        )
        self.conn.commit()

    def overdue_unprompted(self):
        now = dt_to_str(now_local())
        return self.conn.execute(
            """
            SELECT o.*, t.name, t.description
            FROM occurrences o
            JOIN tasks t ON t.id = o.task_id
            WHERE o.completed = 0
              AND o.prompt_generated = 0
              AND o.deadline IS NOT NULL
              AND o.deadline < ?
            ORDER BY o.deadline
            """,
            (now,),
        ).fetchall()

    def all_open_upcoming(self, limit=100):
        return self.conn.execute(
            """
            SELECT o.*, t.name, t.description
            FROM occurrences o
            JOIN tasks t ON t.id = o.task_id
            WHERE o.completed = 0
            ORDER BY o.scheduled_at
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    def close(self):
        self.conn.close()


class PromptManager:
    def __init__(self, db: Database, path: Path):
        self.db = db
        self.path = path
        self.path.touch(exist_ok=True)

    def _delay_phrase(self, deadline: datetime, reference: datetime) -> str:
        seconds = max(0, int((reference - deadline).total_seconds()))
        days = seconds // 86400
        remainder = seconds % 86400
        if days > 0:
            return f"{days} day" + ("" if days == 1 else "s") + " ago"
        hours = remainder // 3600
        if hours > 0:
            return f"{hours} hour" + ("" if hours == 1 else "s") + " ago"
        minutes = max(1, remainder // 60)
        return f"{minutes} minute" + ("" if minutes == 1 else "s") + " ago"

    def append_for_row(self, row) -> bool:
        if not row["deadline"]:
            return False
        deadline = str_to_dt(row["deadline"])
        reference = now_local()
        if deadline >= reference:
            return False
        text = (
            f"Dangers of missing deadline for maintenance task ({row['name']}) "
            f"[{row['description']}] which was due {self._delay_phrase(deadline, reference)} "
            f"and methods to resolve the problems.\n"
        )
        with self.path.open("a", encoding="utf-8") as f:
            f.write(text)
        self.db.mark_prompt_generated(row["id"])
        return True

    def scan_overdue(self) -> int:
        count = 0
        for row in self.db.overdue_unprompted():
            if self.append_for_row(row):
                count += 1
        return count


class TaskChip(QPushButton):
    clicked_occurrence = Signal(int)

    def __init__(self, occurrence_row, parent=None):
        super().__init__(parent)
        self.occurrence_id = occurrence_row["id"]
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumHeight(28)
        self.setMaximumHeight(34)
        self.setFont(QFont("Segoe UI", 9))

        deadline = str_to_dt(occurrence_row["deadline"]) if occurrence_row["deadline"] else None
        completed = bool(occurrence_row["completed"])
        bg, fg = urgency_palette(deadline, completed)
        stars = urgency_stars(deadline) if deadline else 0
        prefix = "✓ " if completed else ""
        time_text = str_to_dt(occurrence_row["scheduled_at"]).strftime("%H:%M")
        star_part = f"  {stars_text(stars)}" if stars else ""
        self.setText(f"{prefix}{time_text}  {occurrence_row['name']}{star_part}")
        self.setToolTip(
            f"{occurrence_row['name']}\n"
            f"{occurrence_row['description']}\n\n"
            f"Scheduled: {str_to_dt(occurrence_row['scheduled_at']).strftime('%Y-%m-%d %H:%M')}\n"
            f"{format_due_delta(deadline) if deadline else 'No deadline'}"
        )
        border = "#9AA99D" if completed else "#B7B2A7"
        self.setStyleSheet(
            f"""
            QPushButton {{
                text-align: left;
                padding: 3px 7px;
                border: 1px solid {border};
                border-radius: 6px;
                background: {bg};
                color: {fg};
            }}
            QPushButton:hover {{
                border: 2px solid #6C6A62;
            }}
            """
        )
        self.clicked.connect(lambda: self.clicked_occurrence.emit(self.occurrence_id))


class DayCell(QFrame):
    clicked_day = Signal(object)
    clicked_occurrence = Signal(int)

    def __init__(self, cell_date: date, is_current_month: bool, parent=None):
        super().__init__(parent)
        self.cell_date = cell_date
        self.is_current_month = is_current_month
        self.setObjectName("dayCell")
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(
            f"""
            QFrame#dayCell {{
                background: {PAPER if is_current_month else '#F2F0EA'};
                border: 1px solid {GRID};
            }}
            """
        )
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(7, 5, 7, 5)
        self.layout.setSpacing(4)

        header = QHBoxLayout()
        self.date_label = QLabel(str(cell_date.day))
        self.date_label.setFont(QFont("Segoe UI", 11, QFont.Bold))
        self.date_label.setStyleSheet(f"color: {INK if is_current_month else '#9B9991'}; border: none;")
        header.addWidget(self.date_label)
        header.addStretch()
        doy = QLabel(f"day {day_of_year(cell_date)}")
        doy.setFont(QFont("Segoe UI", 8))
        doy.setStyleSheet(f"color: {MUTED}; border: none;")
        header.addWidget(doy)
        self.layout.addLayout(header)

        self.task_layout = QVBoxLayout()
        self.task_layout.setSpacing(3)
        self.layout.addLayout(self.task_layout)
        self.layout.addStretch()
        self.setMinimumHeight(125)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Click this day to open the Day Board")

        today = date.today()
        if cell_date == today:
            self.setStyleSheet(
                f"""
                QFrame#dayCell {{
                    background: #FBF6DD;
                    border: 2px solid #8A7C45;
                }}
                """
            )

        self._make_header_clickable()

    def _make_header_clickable(self):
        self.date_label.mousePressEvent = self._day_click

    def _day_click(self, event):
        self.clicked_day.emit(self.cell_date)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked_day.emit(self.cell_date)
        super().mousePressEvent(event)

    def clear_occurrences(self):
        while self.task_layout.count():
            item = self.task_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

    def add_occurrence(self, row):
        chip = TaskChip(row, self)
        chip.clicked_occurrence.connect(self.clicked_occurrence.emit)
        self.task_layout.addWidget(chip)


class MonthCalendar(QWidget):
    clicked_occurrence = Signal(int)
    clicked_day = Signal(object)

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self.current_month = date.today().replace(day=1)
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(8, 8, 8, 8)
        self.grid.setSpacing(0)
        self.day_cells: list[DayCell] = []

        weekdays = ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"]
        for col, name in enumerate(weekdays):
            label = QLabel(name)
            label.setAlignment(Qt.AlignCenter)
            label.setFont(QFont("Segoe UI", 9, QFont.Bold))
            label.setStyleSheet(
                f"background: {HEADER}; color: {INK}; border: 1px solid {GRID}; padding: 8px;"
            )
            self.grid.addWidget(label, 0, col)

        for row in range(1, 7):
            self.grid.setRowStretch(row, 1)
        for col in range(7):
            self.grid.setColumnStretch(col, 1)

        self.refresh()

    def set_month(self, year: int, month: int):
        self.current_month = date(year, month, 1)
        self.refresh()

    def shift_month(self, amount: int):
        year = self.current_month.year + ((self.current_month.month - 1 + amount) // 12)
        month = (self.current_month.month - 1 + amount) % 12 + 1
        self.set_month(year, month)

    def refresh(self):
        for cell in self.day_cells:
            self.grid.removeWidget(cell)
            cell.deleteLater()
        self.day_cells.clear()

        first_weekday = (self.current_month.weekday() + 1) % 7  # Sunday = 0
        next_month = (
            date(self.current_month.year + 1, 1, 1)
            if self.current_month.month == 12
            else date(self.current_month.year, self.current_month.month + 1, 1)
        )
        days_in_month = (next_month - self.current_month).days
        prev_month_last = self.current_month - timedelta(days=1)

        cells: list[tuple[date, bool]] = []
        for i in range(first_weekday):
            d = prev_month_last - timedelta(days=first_weekday - 1 - i)
            cells.append((d, False))
        for day_num in range(1, days_in_month + 1):
            cells.append((date(self.current_month.year, self.current_month.month, day_num), True))
        while len(cells) < 42:
            d = cells[-1][0] + timedelta(days=1)
            cells.append((d, False))

        start = datetime.combine(cells[0][0], time.min)
        end = datetime.combine(cells[-1][0] + timedelta(days=1), time.min)
        rows = self.db.get_occurrences_between(start, end)
        by_day: dict[date, list] = {}
        for row in rows:
            d = str_to_dt(row["scheduled_at"]).date()
            by_day.setdefault(d, []).append(row)

        for index, (cell_date, is_current) in enumerate(cells):
            row = 1 + index // 7
            col = index % 7
            cell = DayCell(cell_date, is_current, self)
            cell.clicked_occurrence.connect(self.clicked_occurrence.emit)
            cell.clicked_day.connect(self.clicked_day.emit)
            for occurrence in by_day.get(cell_date, []):
                cell.add_occurrence(occurrence)
            self.grid.addWidget(cell, row, col)
            self.day_cells.append(cell)


class OccurrenceDialog(QDialog):
    request_refresh = Signal()

    def __init__(self, db: Database, prompt_manager: PromptManager, occurrence_id: int, parent=None):
        super().__init__(parent)
        self.db = db
        self.prompt_manager = prompt_manager
        self.occurrence_id = occurrence_id
        self.setWindowTitle("Maintenance Occurrence")
        self.setMinimumWidth(460)

        row = db.get_occurrence(occurrence_id)
        if not row:
            self.reject()
            return

        scheduled = str_to_dt(row["scheduled_at"])
        deadline = str_to_dt(row["deadline"]) if row["deadline"] else None
        completed = bool(row["completed"])

        layout = QVBoxLayout(self)
        title = QLabel(row["name"])
        title.setFont(QFont("Segoe UI", 16, QFont.Bold))
        layout.addWidget(title)

        description = QLabel(row["description"] or "No description provided.")
        description.setWordWrap(True)
        description.setStyleSheet(f"color: {MUTED}; padding-bottom: 8px;")
        layout.addWidget(description)

        info = QFormLayout()
        info.addRow("Scheduled:", QLabel(scheduled.strftime("%A, %d %B %Y at %H:%M")))
        if row["cycle_number"]:
            info.addRow("Cycle:", QLabel(str(row["cycle_number"])))
        if row["visit_number"]:
            info.addRow("Visit:", QLabel(str(row["visit_number"])))
        if deadline:
            info.addRow("Deadline:", QLabel(deadline.strftime("%A, %d %B %Y at %H:%M")))
            info.addRow("Status:", QLabel(format_due_delta(deadline)))
            stars = urgency_stars(deadline)
            info.addRow("Urgency:", QLabel(stars_text(stars) or "None"))
        else:
            info.addRow("Deadline:", QLabel("None"))
        if completed:
            completed_at = str_to_dt(row["completed_at"]) if row["completed_at"] else None
            completed_label = "Completed"
            if completed_at:
                completed_label += " on " + completed_at.strftime("%d %b %Y at %H:%M")
            info.addRow("Completion:", QLabel(completed_label))
        else:
            info.addRow("Completion:", QLabel("Not completed"))
        layout.addLayout(info)

        buttons = QDialogButtonBox()
        if not completed:
            complete = buttons.addButton("Mark Complete", QDialogButtonBox.AcceptRole)
            complete.clicked.connect(self.complete_occurrence)
        close_btn = buttons.addButton("Close", QDialogButtonBox.RejectRole)
        close_btn.clicked.connect(self.reject)
        layout.addWidget(buttons)

    def complete_occurrence(self):
        row = self.db.get_occurrence(self.occurrence_id)
        if not row:
            return
        self.db.mark_complete(self.occurrence_id)
        # A completion after the deadline creates the prompt entry.
        row_after = self.db.get_occurrence(self.occurrence_id)
        if row_after and row_after["deadline"]:
            if str_to_dt(row_after["deadline"]) < now_local():
                prompt_row = self.db.conn.execute(
                    """
                    SELECT o.*, t.name, t.description
                    FROM occurrences o
                    JOIN tasks t ON t.id = o.task_id
                    WHERE o.id = ?
                    """,
                    (self.occurrence_id,),
                ).fetchone()
                if prompt_row:
                    self.prompt_manager.append_for_row(prompt_row)
        self.request_refresh.emit()
        QMessageBox.information(self, "Completed", "This maintenance occurrence has been marked complete.")
        self.accept()


class DayDetailsDialog(QDialog):
    """Large, scrollable board for everything scheduled on one day."""

    def __init__(self, db: Database, prompt_manager: PromptManager, selected_date: date, parent=None):
        super().__init__(parent)
        self.db = db
        self.prompt_manager = prompt_manager
        self.selected_date = selected_date
        self.refresh_callback = None
        self.setWindowTitle(f"Day Board — {selected_date.strftime('%A, %d %B %Y')}")
        self.setMinimumSize(1050, 720)
        self.resize(1250, 850)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)

        header = QFrame()
        header.setStyleSheet(
            f"QFrame {{ background: {HEADER}; border: 1px solid {GRID}; border-radius: 8px; }}"
        )
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(16, 12, 16, 12)
        header_layout.setSpacing(8)

        title_row = QHBoxLayout()
        self.title_label = QLabel()
        self.title_label.setFont(QFont("Segoe UI", 20, QFont.Bold))
        title_row.addWidget(self.title_label)
        title_row.addStretch()
        self.summary_label = QLabel()
        self.summary_label.setStyleSheet(f"color: {MUTED};")
        title_row.addWidget(self.summary_label)
        header_layout.addLayout(title_row)

        nav = QHBoxLayout()
        prev_btn = QPushButton("‹  Previous day")
        prev_btn.clicked.connect(lambda: self.change_day(-1))
        next_btn = QPushButton("Next day  ›")
        next_btn.clicked.connect(lambda: self.change_day(1))
        today_btn = QPushButton("Today")
        today_btn.clicked.connect(self.go_today)
        nav.addWidget(prev_btn)
        nav.addWidget(today_btn)
        nav.addWidget(next_btn)
        nav.addStretch()
        hint = QLabel("Scroll to see every maintenance item scheduled for this day.")
        hint.setStyleSheet(f"color: {MUTED};")
        nav.addWidget(hint)
        header_layout.addLayout(nav)
        root.addWidget(header)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self.content = QWidget()
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(4, 4, 12, 16)
        self.content_layout.setSpacing(10)
        self.scroll.setWidget(self.content)
        root.addWidget(self.scroll, 1)

        close_btn = QPushButton("Close Day Board")
        close_btn.clicked.connect(self.accept)
        root.addWidget(close_btn, 0, Qt.AlignRight)

        self.refresh()

    def change_day(self, amount: int):
        self.selected_date += timedelta(days=amount)
        self.setWindowTitle(f"Day Board — {self.selected_date.strftime('%A, %d %B %Y')}")
        self.refresh()
        self.scroll.verticalScrollBar().setValue(0)

    def go_today(self):
        self.selected_date = date.today()
        self.setWindowTitle(f"Day Board — {self.selected_date.strftime('%A, %d %B %Y')}")
        self.refresh()
        self.scroll.verticalScrollBar().setValue(0)

    def _clear_content(self):
        while self.content_layout.count():
            item = self.content_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

    def _complete_occurrence(self, occurrence_id: int):
        row = self.db.get_occurrence(occurrence_id)
        if not row or bool(row["completed"]):
            return

        self.db.mark_complete(occurrence_id)
        row_after = self.db.get_occurrence(occurrence_id)
        if row_after and row_after["deadline"]:
            deadline = str_to_dt(row_after["deadline"])
            if deadline < now_local():
                self.prompt_manager.append_for_row(row_after)

        if self.refresh_callback:
            self.refresh_callback()
        self.refresh()

    def _open_occurrence(self, occurrence_id: int):
        dialog = OccurrenceDialog(self.db, self.prompt_manager, occurrence_id, self)
        dialog.request_refresh.connect(self.refresh)
        if self.refresh_callback:
            dialog.request_refresh.connect(self.refresh_callback)
        dialog.exec()
        self.refresh()

    def _build_event_card(self, row):
        scheduled = str_to_dt(row["scheduled_at"])
        deadline = str_to_dt(row["deadline"]) if row["deadline"] else None
        completed = bool(row["completed"])
        bg, fg = urgency_palette(deadline, completed)
        stars = urgency_stars(deadline) if deadline else 0

        frame = QFrame()
        frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        frame.setStyleSheet(
            f"QFrame {{ background: {bg}; border: 1px solid {GRID}; border-left: 6px solid {fg}; border-radius: 9px; }}"
        )
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(16)

        # Large time column makes this read like a physical day planner.
        time_box = QVBoxLayout()
        time_label = QLabel(scheduled.strftime("%H:%M"))
        time_label.setFont(QFont("Segoe UI", 18, QFont.Bold))
        time_label.setStyleSheet(f"color: {fg}; border: none;")
        time_box.addWidget(time_label)
        if completed:
            state = QLabel("✓ DONE")
            state.setFont(QFont("Segoe UI", 8, QFont.Bold))
        else:
            state = QLabel("SCHEDULED")
            state.setFont(QFont("Segoe UI", 8, QFont.Bold))
        state.setStyleSheet(f"color: {fg}; border: none;")
        time_box.addWidget(state)
        time_box.addStretch()
        layout.addLayout(time_box, 0)

        details = QVBoxLayout()
        name = QLabel(row["name"])
        name.setFont(QFont("Segoe UI", 13, QFont.Bold))
        name.setWordWrap(True)
        name.setStyleSheet(f"color: {fg}; border: none;")
        details.addWidget(name)

        description_text = row["description"] or "No description provided."
        description = QLabel(description_text)
        description.setWordWrap(True)
        description.setTextInteractionFlags(Qt.TextSelectableByMouse)
        description.setStyleSheet(f"color: {INK}; border: none; padding-top: 2px;")
        details.addWidget(description)

        meta_parts = []
        if row["cycle_number"]:
            meta_parts.append(f"Cycle {row['cycle_number']}")
        if row["visit_number"]:
            meta_parts.append(f"Visit {row['visit_number']}")
        meta_parts.append(scheduled.strftime("%A, %d %B %Y at %H:%M"))
        meta = QLabel("  •  ".join(meta_parts))
        meta.setWordWrap(True)
        meta.setStyleSheet(f"color: {MUTED}; border: none; padding-top: 4px;")
        details.addWidget(meta)

        if deadline:
            due_text = deadline.strftime("Deadline: %A, %d %B %Y at %H:%M")
            due_text += f"   •   {format_due_delta(deadline)}"
            due = QLabel(due_text)
            due.setWordWrap(True)
            due.setFont(QFont("Segoe UI", 9, QFont.Bold))
            due.setStyleSheet(f"color: {fg}; border: none; padding-top: 3px;")
            details.addWidget(due)

            if stars:
                urgency = QLabel(stars_text(stars))
                urgency.setFont(QFont("Segoe UI", 13))
                urgency.setStyleSheet(f"color: {fg}; border: none;")
                details.addWidget(urgency)
        else:
            due = QLabel("No deadline")
            due.setStyleSheet(f"color: {MUTED}; border: none; padding-top: 3px;")
            details.addWidget(due)

        if completed and row["completed_at"]:
            completed_at = str_to_dt(row["completed_at"])
            completed_label = QLabel(
                f"Completed: {completed_at.strftime('%A, %d %B %Y at %H:%M')}"
            )
            completed_label.setStyleSheet("color: #315B3A; border: none; padding-top: 3px;")
            details.addWidget(completed_label)

        layout.addLayout(details, 1)

        actions = QVBoxLayout()
        open_btn = QPushButton("Open details")
        open_btn.clicked.connect(lambda _=False, oid=row["id"]: self._open_occurrence(oid))
        actions.addWidget(open_btn)

        if not completed:
            complete_btn = QPushButton("✓  Mark Complete")
            complete_btn.setFont(QFont("Segoe UI", 9, QFont.Bold))
            complete_btn.setStyleSheet(
                "QPushButton { background: #E3EEE5; color: #315B3A; border: 1px solid #9AA99D; }"
                "QPushButton:hover { background: #D5E6D8; }"
            )
            complete_btn.clicked.connect(lambda _=False, oid=row["id"]: self._complete_occurrence(oid))
            actions.addWidget(complete_btn)

        actions.addStretch()
        layout.addLayout(actions, 0)
        return frame

    def refresh(self):
        self._clear_content()
        rows = self.db.get_occurrences_for_day(self.selected_date)

        total = len(rows)
        completed = sum(1 for row in rows if bool(row["completed"]))
        overdue = 0
        for row in rows:
            if not row["completed"] and row["deadline"] and str_to_dt(row["deadline"]) < now_local():
                overdue += 1

        self.title_label.setText(self.selected_date.strftime("%A, %d %B %Y"))
        summary_parts = [f"{total} task" + ("" if total == 1 else "s")]
        summary_parts.append(f"{completed} completed")
        if overdue:
            summary_parts.append(f"{overdue} overdue")
        self.summary_label.setText("  •  ".join(summary_parts))

        if not rows:
            empty = QFrame()
            empty.setStyleSheet(
                f"QFrame {{ background: {PAPER}; border: 1px solid {GRID}; border-radius: 9px; }}"
            )
            empty_layout = QVBoxLayout(empty)
            empty_layout.setContentsMargins(30, 40, 30, 40)
            label = QLabel("Nothing is scheduled for this day.")
            label.setAlignment(Qt.AlignCenter)
            label.setFont(QFont("Segoe UI", 14))
            label.setStyleSheet(f"color: {MUTED}; border: none;")
            empty_layout.addWidget(label)
            self.content_layout.addWidget(empty)
            self.content_layout.addStretch()
            return

        for row in rows:
            self.content_layout.addWidget(self._build_event_card(row))
        self.content_layout.addStretch()

    def open_occurrence(self, occurrence_id: int):
        # Backwards-compatible signal target for older callers.
        self._open_occurrence(occurrence_id)


class AddTaskDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Maintenance")
        self.setMinimumWidth(620)
        main = QVBoxLayout(self)

        form = QFormLayout()
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("e.g. Inspect cooling system")
        self.desc_edit = QLineEdit()
        self.desc_edit.setPlaceholderText("Describe what needs to be done")
        self.type_combo = QComboBox()
        self.type_combo.addItems(["Repeating cycle", "Distributed visits"])
        form.addRow("Task name:", self.name_edit)
        form.addRow("Description:", self.desc_edit)
        form.addRow("Schedule:", self.type_combo)
        main.addLayout(form)

        self.stack = QStackedWidget()
        main.addWidget(self.stack)
        self.build_cycle_page()
        self.build_distributed_page()
        self.type_combo.currentIndexChanged.connect(self.stack.setCurrentIndex)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.validate_and_accept)
        buttons.rejected.connect(self.reject)
        main.addWidget(buttons)

    def build_cycle_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)

        group = QGroupBox("Starting dates")
        gl = QVBoxLayout(group)
        row = QHBoxLayout()
        from PySide6.QtCore import QTime
        self.start_date_edit = QDateEdit(QDate.currentDate())
        self.start_date_edit.setCalendarPopup(True)
        self.start_time_edit = QTimeEdit(QTime.currentTime())
        self.start_time_edit.setDisplayFormat("HH:mm")
        add_btn = QPushButton("Add starting date")
        add_btn.clicked.connect(self.add_start_date)
        row.addWidget(QLabel("Date:"))
        row.addWidget(self.start_date_edit)
        row.addWidget(QLabel("Time:"))
        row.addWidget(self.start_time_edit)
        row.addWidget(add_btn)
        gl.addLayout(row)
        self.start_list = QListWidget()
        self.start_list.setMaximumHeight(115)
        gl.addWidget(self.start_list)
        remove_btn = QPushButton("Remove selected")
        remove_btn.clicked.connect(lambda: self.start_list.takeItem(self.start_list.currentRow()))
        gl.addWidget(remove_btn)
        layout.addWidget(group)

        cycle_form = QFormLayout()
        self.cycle_value = QSpinBox()
        self.cycle_value.setRange(1, 100000)
        self.cycle_value.setValue(30)
        self.cycle_unit = QComboBox()
        self.cycle_unit.addItems(["days", "hours"])
        unit_wrap = QHBoxLayout()
        unit_wrap.addWidget(self.cycle_value)
        unit_wrap.addWidget(self.cycle_unit)
        self.max_cycles = QSpinBox()
        self.max_cycles.setRange(0, 100000)
        self.max_cycles.setSpecialValueText("Unlimited")
        self.max_cycles.setValue(12)
        cycle_form.addRow("Cycle period:", unit_wrap)
        cycle_form.addRow("Maximum cycles:", self.max_cycles)
        layout.addLayout(cycle_form)

        deadline_group = QGroupBox("Deadline")
        dl = QHBoxLayout(deadline_group)
        self.cycle_deadline_enabled = QCheckBox("Use deadline for each occurrence")
        self.cycle_deadline_enabled.setChecked(True)
        self.cycle_deadline_date = QDateEdit(QDate.currentDate().addDays(2))
        self.cycle_deadline_date.setCalendarPopup(True)
        self.cycle_deadline_time = QTimeEdit(QTime(17, 0))
        self.cycle_deadline_time.setDisplayFormat("HH:mm")
        dl.addWidget(self.cycle_deadline_enabled)
        dl.addWidget(self.cycle_deadline_date)
        dl.addWidget(self.cycle_deadline_time)
        layout.addWidget(deadline_group)
        self.stack.addWidget(page)

    def build_distributed_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        from PySide6.QtCore import QTime
        form = QFormLayout()
        self.dist_start_date = QDateEdit(QDate.currentDate())
        self.dist_start_date.setCalendarPopup(True)
        self.dist_start_time = QTimeEdit(QTime.currentTime())
        self.dist_start_time.setDisplayFormat("HH:mm")
        self.dist_end_date = QDateEdit(QDate.currentDate().addDays(14))
        self.dist_end_date.setCalendarPopup(True)
        self.dist_end_time = QTimeEdit(QTime(17, 0))
        self.dist_end_time.setDisplayFormat("HH:mm")
        self.visit_count = QSpinBox()
        self.visit_count.setRange(1, 100000)
        self.visit_count.setValue(5)
        form.addRow("Start date:", self.dist_start_date)
        form.addRow("Start time:", self.dist_start_time)
        form.addRow("End date / deadline:", self.dist_end_date)
        form.addRow("End time:", self.dist_end_time)
        form.addRow("Number of visits:", self.visit_count)
        layout.addLayout(form)
        info = QLabel(
            "The program will distribute the visits evenly between the start and end timestamps. "
            "Hours and minutes are preserved. The end timestamp is the final visit's deadline."
        )
        info.setWordWrap(True)
        info.setStyleSheet(f"color: {MUTED}; padding: 8px;")
        layout.addWidget(info)
        layout.addStretch()
        self.stack.addWidget(page)

        # Seed one starting date by default for cycle mode.
        self.add_start_date()

    def add_start_date(self):
        d = qdate_to_date(self.start_date_edit.date())
        t = qtime_to_time(self.start_time_edit.time())
        dt = datetime.combine(d, t)
        item = QListWidgetItem(dt.strftime("%Y-%m-%d %H:%M"))
        item.setData(Qt.UserRole, dt_to_str(dt))
        self.start_list.addItem(item)

    def validate_and_accept(self):
        if not self.name_edit.text().strip():
            QMessageBox.warning(self, "Missing name", "Please enter a maintenance task name.")
            return
        if self.stack.currentIndex() == 0:
            self.validate_cycle()
        else:
            self.validate_distributed()

    def validate_cycle(self):
        if self.start_list.count() == 0:
            QMessageBox.warning(self, "Starting date required", "Add at least one starting date.")
            return
        starts = [str_to_dt(self.start_list.item(i).data(Qt.UserRole)) for i in range(self.start_list.count())]
        if len({dt_to_str(x) for x in starts}) != len(starts):
            QMessageBox.warning(self, "Duplicate starting date", "Remove duplicate starting dates.")
            return
        if self.cycle_deadline_enabled.isChecked():
            deadline = datetime.combine(qdate_to_date(self.cycle_deadline_date.date()), qtime_to_time(self.cycle_deadline_time.time()))
            if deadline < min(starts):
                QMessageBox.warning(self, "Invalid deadline", "The first deadline cannot be earlier than the starting date.")
                return
        self.accept()

    def validate_distributed(self):
        start = datetime.combine(qdate_to_date(self.dist_start_date.date()), qtime_to_time(self.dist_start_time.time()))
        end = datetime.combine(qdate_to_date(self.dist_end_date.date()), qtime_to_time(self.dist_end_time.time()))
        if end < start:
            QMessageBox.warning(self, "Invalid range", "The end date/time must be after the start date/time.")
            return
        if self.visit_count.value() > 1 and end == start:
            QMessageBox.warning(self, "Invalid range", "Multiple visits need a time interval.")
            return
        self.accept()

    def get_data(self) -> dict:
        common = {
            "name": self.name_edit.text().strip(),
            "description": self.desc_edit.text().strip(),
        }
        if self.stack.currentIndex() == 0:
            starts = [str_to_dt(self.start_list.item(i).data(Qt.UserRole)) for i in range(self.start_list.count())]
            first_deadline = None
            if self.cycle_deadline_enabled.isChecked():
                first_deadline = datetime.combine(
                    qdate_to_date(self.cycle_deadline_date.date()),
                    qtime_to_time(self.cycle_deadline_time.time()),
                )
            common.update(
                {
                    "schedule_type": "cycle",
                    "start_datetimes": starts,
                    "cycle_period_value": self.cycle_value.value(),
                    "cycle_period_unit": self.cycle_unit.currentText(),
                    "maximum_cycles": self.max_cycles.value() if self.max_cycles.value() > 0 else None,
                    "first_deadline": first_deadline,
                }
            )
        else:
            start = datetime.combine(qdate_to_date(self.dist_start_date.date()), qtime_to_time(self.dist_start_time.time()))
            end = datetime.combine(qdate_to_date(self.dist_end_date.date()), qtime_to_time(self.dist_end_time.time()))
            common.update(
                {
                    "schedule_type": "distributed",
                    "distributed_start": start,
                    "distributed_end": end,
                    "visit_count": self.visit_count.value(),
                }
            )
        return common


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.db = Database(DB_PATH)
        self.prompt_manager = PromptManager(self.db, PROMPT_FILE)
        self.setWindowTitle(APP_NAME)
        self.resize(1500, 950)
        self.setMinimumSize(1100, 700)
        self.apply_styles()

        self.calendar = MonthCalendar(self.db)
        self.calendar.clicked_occurrence.connect(self.open_occurrence)
        self.calendar.clicked_day.connect(self.open_day)
        self.setCentralWidget(self.calendar)

        self.build_toolbar()
        self.build_status_bar()

        self.current_year = int(self.db.get_setting("calendar_year", str(date.today().year)))
        self.planning_day = int(self.db.get_setting("planning_day", "365"))
        self.year_spin.setValue(self.current_year)
        self.horizon_spin.setValue(self.planning_day)

        # Start at the current month if the selected year is current year;
        # otherwise start at January.
        if self.current_year == date.today().year:
            self.calendar.set_month(self.current_year, date.today().month)
        else:
            self.calendar.set_month(self.current_year, 1)

        self.refresh_all(generate=True)

    def apply_styles(self):
        self.setStyleSheet(
            f"""
            QMainWindow {{ background: {PAPER}; color: {INK}; }}
            QLabel {{ color: {INK}; }}
            QToolBar {{
                background: {HEADER};
                border-bottom: 1px solid {GRID};
                spacing: 6px;
                padding: 7px;
            }}
            QPushButton, QToolButton {{
                background: #F8F7F2;
                color: {INK};
                border: 1px solid #BBB8AF;
                border-radius: 5px;
                padding: 6px 10px;
            }}
            QPushButton:hover, QToolButton:hover {{ background: #EDEBE3; }}
            QLineEdit, QComboBox, QSpinBox, QDateEdit, QTimeEdit {{
                background: white;
                color: {INK};
                border: 1px solid #BBB8AF;
                border-radius: 4px;
                padding: 5px;
            }}
            QStatusBar {{ background: {HEADER}; color: {MUTED}; }}
            QGroupBox {{
                border: 1px solid {GRID};
                border-radius: 6px;
                margin-top: 10px;
                padding-top: 10px;
            }}
            QGroupBox::title {{ left: 10px; padding: 0 4px; color: {INK}; }}
            """
        )

    def build_toolbar(self):
        toolbar = self.addToolBar("Calendar")
        toolbar.setMovable(False)
        toolbar.setIconSize(QSize(18, 18))

        title = QLabel("MAINTENANCE CALENDAR")
        title.setFont(QFont("Segoe UI", 14, QFont.Bold))
        title.setStyleSheet(f"padding: 2px 8px; color: {INK};")
        toolbar.addWidget(title)
        toolbar.addSeparator()

        prev_btn = QToolButton()
        prev_btn.setText("‹")
        prev_btn.setToolTip("Previous month")
        prev_btn.clicked.connect(lambda: self.change_month(-1))
        toolbar.addWidget(prev_btn)

        self.month_label = QLabel()
        self.month_label.setMinimumWidth(190)
        self.month_label.setAlignment(Qt.AlignCenter)
        self.month_label.setFont(QFont("Segoe UI", 13, QFont.Bold))
        toolbar.addWidget(self.month_label)

        next_btn = QToolButton()
        next_btn.setText("›")
        next_btn.setToolTip("Next month")
        next_btn.clicked.connect(lambda: self.change_month(1))
        toolbar.addWidget(next_btn)

        today_btn = QPushButton("Today")
        today_btn.clicked.connect(self.go_today)
        toolbar.addWidget(today_btn)

        add_btn = QPushButton("+ Add Maintenance")
        add_btn.setFont(QFont("Segoe UI", 9, QFont.Bold))
        add_btn.clicked.connect(self.add_task)
        toolbar.addWidget(add_btn)

        toolbar.addSeparator()
        toolbar.addWidget(QLabel("Year:"))
        self.year_spin = QSpinBox()
        self.year_spin.setRange(2000, 2200)
        self.year_spin.valueChanged.connect(self.year_changed)
        toolbar.addWidget(self.year_spin)

        toolbar.addWidget(QLabel("Generate through day:"))
        self.horizon_spin = QSpinBox()
        self.horizon_spin.setRange(1, 366)
        self.horizon_spin.valueChanged.connect(self.horizon_changed)
        toolbar.addWidget(self.horizon_spin)

        legend_btn = QPushButton("Legend")
        legend_btn.clicked.connect(self.show_legend)
        toolbar.addWidget(legend_btn)

        open_file_btn = QPushButton("Prompt List")
        open_file_btn.clicked.connect(self.show_prompt_file_info)
        toolbar.addWidget(open_file_btn)

        self.update_month_label()

    def build_status_bar(self):
        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status.showMessage("Ready")

    def update_month_label(self):
        self.month_label.setText(self.calendar.current_month.strftime("%B %Y"))

    def change_month(self, amount):
        self.calendar.shift_month(amount)
        self.update_month_label()
        self.status.showMessage(self.calendar.current_month.strftime("%B %Y"))

    def go_today(self):
        today = date.today()
        self.current_year = today.year
        self.year_spin.blockSignals(True)
        self.year_spin.setValue(today.year)
        self.year_spin.blockSignals(False)
        self.db.set_setting("calendar_year", str(today.year))
        self.calendar.set_month(today.year, today.month)
        self.update_month_label()
        self.refresh_all(generate=True)

    def year_changed(self, value):
        self.current_year = value
        self.db.set_setting("calendar_year", str(value))
        self.calendar.set_month(value, self.calendar.current_month.month)
        self.update_month_label()
        self.refresh_all(generate=True)

    def horizon_changed(self, value):
        self.planning_day = value
        self.db.set_setting("planning_day", str(value))
        self.refresh_all(generate=True)

    def refresh_all(self, generate=True):
        if generate:
            self.generate_all_occurrences()
        missed = self.prompt_manager.scan_overdue()
        self.calendar.refresh()
        self.update_month_label()
        if missed:
            self.status.showMessage(f"Generated {missed} new overdue prompt(s) in prompt list.txt")
        else:
            self.status.showMessage("Calendar refreshed")

    def generate_all_occurrences(self):
        horizon_end = datetime.combine(
            date_from_day_of_year(self.current_year, self.planning_day) + timedelta(days=1),
            time.min,
        )
        for task in self.db.get_tasks():
            if task["schedule_type"] == "cycle":
                self.generate_cycle_task(task, horizon_end)
            elif task["schedule_type"] == "distributed":
                self.generate_distributed_task(task, horizon_end)

    def generate_cycle_task(self, task, horizon_end: datetime):
        starts = [str_to_dt(x) for x in json.loads(task["start_datetimes_json"])]
        if not starts:
            return
        period_value = int(task["cycle_period_value"] or 1)
        unit = task["cycle_period_unit"] or "days"
        period = timedelta(days=period_value) if unit == "days" else timedelta(hours=period_value)
        max_cycles = task["maximum_cycles"]

        first_deadline = str_to_dt(task["first_deadline"]) if task["first_deadline"] else None
        base_offset = None
        if first_deadline:
            base_offset = first_deadline - min(starts)

        for start_dt in starts:
            cycle = 1
            while True:
                scheduled = start_dt + period * (cycle - 1)
                if scheduled >= horizon_end:
                    break
                if max_cycles is not None and cycle > int(max_cycles):
                    break
                deadline = scheduled + base_offset if base_offset is not None else None
                self.db.add_occurrence(task["id"], scheduled, deadline, cycle_number=cycle)
                cycle += 1

    def generate_distributed_task(self, task, horizon_end: datetime):
        start = str_to_dt(task["distributed_start"])
        end = str_to_dt(task["distributed_end"])
        count = int(task["visit_count"] or 1)
        if start >= horizon_end:
            return
        if count <= 1:
            if start < horizon_end:
                self.db.add_occurrence(task["id"], start, end, visit_number=1)
            return
        interval_seconds = (end - start).total_seconds() / (count - 1)
        for i in range(count):
            scheduled = start + timedelta(seconds=interval_seconds * i)
            if scheduled >= horizon_end:
                break
            # Each visit is expected at its scheduled timestamp; the final visit
            # coincides with the task's stated deadline.
            deadline = scheduled
            self.db.add_occurrence(task["id"], scheduled, deadline, visit_number=i + 1)

    def add_task(self):
        dialog = AddTaskDialog(self)
        if dialog.exec() != QDialog.Accepted:
            return
        data = dialog.get_data()
        task_id = self.db.create_task(data)
        self.generate_all_occurrences()
        self.calendar.refresh()
        self.status.showMessage(f"Added maintenance task: {data['name']}")
        # If task is in a different month, do not force navigation; the physical
        # calendar remains where the user was working.

    def open_occurrence(self, occurrence_id: int):
        dialog = OccurrenceDialog(self.db, self.prompt_manager, occurrence_id, self)
        dialog.request_refresh.connect(lambda: self.refresh_all(generate=False))
        dialog.exec()
        self.refresh_all(generate=True)

    def open_day(self, selected_date: date):
        dialog = DayDetailsDialog(self.db, self.prompt_manager, selected_date, self)
        dialog.refresh_callback = lambda: self.refresh_all(generate=False)
        dialog.exec()
        self.refresh_all(generate=True)

    def show_legend(self):
        msg = (
            "DEADLINE URGENCY\n\n"
            "☆  No active urgency\n"
            "★  Low awareness\n"
            "★★  Approaching\n"
            "★★★  High\n"
            "★★★★  Very high\n"
            "★★★★★  Critical or overdue\n\n"
            "Colors move from green to yellow, amber/orange, red, and dark red as the deadline approaches.\n\n"
            "The star scale uses a logarithmic-style curve so the warning accelerates near the deadline."
        )
        QMessageBox.information(self, "Calendar Legend", msg)

    def show_prompt_file_info(self):
        msg = (
            f"Prompt file:\n{PROMPT_FILE}\n\n"
            "Overdue maintenance occurrences are added automatically. "
            "Late completions also generate a prompt. Duplicate prompts are prevented per occurrence."
        )
        QMessageBox.information(self, "Prompt List", msg)

    def closeEvent(self, event):
        self.db.close()
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
