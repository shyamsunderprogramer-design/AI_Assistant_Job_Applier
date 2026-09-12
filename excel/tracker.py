"""Excel tracking sheet — DB is the source of truth, the sheet is the view.

Append-only by design: the user edits Status by hand in the workbook, so a
re-export must never clobber those edits or duplicate a row. Jobs are matched
back to their sheet row by the hidden Job Key column.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.formatting.rule import CellIsRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.worksheet import Worksheet

from config.loader import PROJECT_ROOT
from db.models import STATUS_VALUES, SYSTEM_STATUSES, Job
from db.session import get_session
from jobage import age_label

log = logging.getLogger(__name__)

SHEET_NAME = "Jobs"

# (header, width). Job Key is last and hidden — it's the join key back to the DB.
COLUMNS: list[tuple[str, int]] = [
    ("Company", 22),
    ("Job Title", 46),
    ("Application Link", 32),
    ("JD", 80),
    ("Location", 28),
    ("Posting Date", 14),
    # Age is text so it reads at a glance; that means it sorts alphabetically,
    # so Posting Date stays the column to sort by and --max-age does filtering.
    ("Age", 10),
    ("Required Skills", 60),
    ("Date Found", 14),
    ("Application Status", 20),
    ("ATS Match Score", 16),
    ("Job Key", 30),
]

# STATUS_VALUES and SYSTEM_STATUSES are defined in db.models and imported here.
# Three things write a status now — the scorer, the lifecycle reconciler, and
# this export — so the vocabulary lives in one place or they drift.
#
# SYSTEM_STATUSES are the ones an export may update, because they record no
# user decision. Every other value is a human judgement and is never overwritten.

# Excel's hard per-cell limit is 32767; stay well under it.
JD_CELL_LIMIT = 20000

# COL describes the layout we CREATE. A workbook already on disk describes
# itself, through its own header row — see ColumnMap. Nothing that touches a
# loaded workbook may index by these, or adding a column silently reindexes
# every existing sheet.
COL = {name: idx for idx, (name, _) in enumerate(COLUMNS, start=1)}
KEY_COL = COL["Job Key"]


@dataclass(frozen=True)
class ColumnMap:
    """Header name -> 1-based column, read from a live sheet's own row 1."""

    index: dict[str, int]
    width: int          # widest column present, including ones we did not write

    def get(self, name: str) -> int | None:
        return self.index.get(name)

    def __getitem__(self, name: str) -> int:
        return self.index[name]

    def has(self, name: str) -> bool:
        return name in self.index

    def letter(self, name: str) -> str | None:
        idx = self.index.get(name)
        return get_column_letter(idx) if idx else None


@dataclass(frozen=True)
class _RowDates:
    """The two date fields `jobage` reads, recovered from a sheet row.

    Lets a refresh reuse the age logic instead of reimplementing the scale.
    """

    posted_at: datetime | None
    found_at: datetime | None


def _normalise_header(value: object) -> str:
    text = re.sub(r"[^a-z0-9 ]", " ", str(value or "").lower())
    return re.sub(r"\s+", " ", text).strip()


def job_key(job: Job) -> str:
    """Stable identity for a posting, mirroring the DB unique constraint."""
    return f"{job.source}:{job.company_slug}:{job.external_id}"


def _truncate(text: str | None, limit: int = JD_CELL_LIMIT) -> str:
    if not text:
        return ""
    return text if len(text) <= limit else text[: limit - 20] + "\n… [truncated]"


def _fmt_date(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d") if value else ""


def _parse_date(value: object) -> datetime | None:
    """Read back a date this module wrote, however Excel hands it over."""
    if isinstance(value, datetime):
        return value
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def _row_values(job: Job) -> dict[str, object]:
    return {
        "Company": job.company,
        "Job Title": job.title,
        "Application Link": job.application_url,
        "JD": _truncate(job.description),
        "Location": job.location or "",
        "Posting Date": _fmt_date(job.posted_at),
        "Age": age_label(job),
        "Required Skills": _truncate(job.requirements, 4000),
        "Date Found": _fmt_date(job.found_at),
        "ATS Match Score": job.ats_match_score if job.ats_match_score is not None else "",
        "Job Key": job_key(job),
    }


class ExcelTracker:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if not self.path.is_absolute():
            self.path = PROJECT_ROOT / self.path

    # -- workbook lifecycle ------------------------------------------------
    def _open(self) -> Workbook:
        if self.path.exists():
            return load_workbook(self.path)
        wb = Workbook()
        ws = wb.active
        ws.title = SHEET_NAME
        ws.append([name for name, _ in COLUMNS])
        return wb

    def _sheet(self, wb: Workbook) -> Worksheet:
        if SHEET_NAME in wb.sheetnames:
            return wb[SHEET_NAME]
        ws = wb.create_sheet(SHEET_NAME)
        ws.append([name for name, _ in COLUMNS])
        return ws

    # -- column resolution -------------------------------------------------
    @staticmethod
    def _header_map(ws: Worksheet) -> ColumnMap:
        """Read row 1. Pure — never mutates the sheet."""
        wanted = {_normalise_header(name): name for name, _ in COLUMNS}
        index: dict[str, int] = {}
        for col in range(1, ws.max_column + 1):
            header = _normalise_header(ws.cell(row=1, column=col).value)
            canonical = wanted.get(header)
            if canonical and canonical not in index:   # a duplicate header loses
                index[canonical] = col
        return ColumnMap(index=index, width=ws.max_column)

    @staticmethod
    def _ensure_columns(ws: Worksheet) -> ColumnMap:
        """Add any header this sheet lacks, to the RIGHT of what is there.

        Appending rather than inserting is deliberate. `insert_cols` shifts
        every column after it, but openpyxl leaves `column_dimensions` keyed by
        the old letter — so the hidden flag would stay on whatever now sits in
        that position and the Job Key column would become visible, spilling the
        join key across every row.
        """
        columns = ExcelTracker._header_map(ws)
        missing = [name for name, _ in COLUMNS if not columns.has(name)]
        if not missing:
            return columns

        next_col = ws.max_column + 1
        for offset, name in enumerate(missing):
            ws.cell(row=1, column=next_col + offset).value = name
        log.info("Sheet has no %s column — added it", ", ".join(missing))
        return ExcelTracker._header_map(ws)

    def _existing_rows(self, ws: Worksheet, columns: ColumnMap) -> dict[str, int]:
        """Map Job Key -> row number for every row already in the sheet."""
        key_col = columns.get("Job Key")
        if key_col is None:
            return {}
        found: dict[str, int] = {}
        for row in range(2, ws.max_row + 1):
            key = ws.cell(row=row, column=key_col).value
            if key:
                found[str(key)] = row
        return found

    # -- writing -----------------------------------------------------------
    def export(self, jobs: list[Job]) -> tuple[int, int]:
        """Append new jobs, refresh changed ones. Returns (appended, updated).

        A row's Application Status is never overwritten — that column belongs to
        the user.
        """
        wb = self._open()
        ws = self._sheet(wb)
        # Must precede _existing_rows: every read and write below is keyed off
        # this map, and the Age header has to exist before a row can fill it.
        columns = self._ensure_columns(ws)
        existing = self._existing_rows(ws, columns)

        appended = 0
        updated = 0
        written: set[int] = set()

        for job in jobs:
            key = job_key(job)
            values = _row_values(job)
            row_idx = existing.get(key)

            if row_idx is None:
                row_idx = ws.max_row + 1
                self._write_row(ws, row_idx, values, columns)
                status_col = columns.get("Application Status")
                if status_col:
                    ws.cell(row=row_idx, column=status_col).value = job.status
                existing[key] = row_idx
                appended += 1
            else:
                self._write_row(ws, row_idx, values, columns)
                self._sync_status(ws, row_idx, job.status, columns)
                updated += 1
            written.add(row_idx)

        self._refresh_ages(ws, columns, skip=written)
        self._format(ws, columns)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        wb.save(self.path)
        return appended, updated

    def remove(self, keys: set[str]) -> int:
        """Delete rows whose jobs no longer exist. Returns how many went.

        The sheet is otherwise append-only, which is right while the DB only
        grows — but `prune` deletes jobs that no longer match the search, and
        without this their rows linger forever as dead entries the user would
        still click on.

        A row the user has acted on is never deleted, even when its job was
        pruned: "Applied" outlives the posting, and is the row they most need
        to keep. Deletion runs bottom-up so earlier deletions do not shift the
        indices of rows not yet visited.
        """
        if not keys or not self.path.exists():
            return 0

        wb = self._open()
        ws = self._sheet(wb)

        survivors: list[list[object]] = []
        removed = 0
        columns = self._ensure_columns(ws)
        # A sheet we cannot read is a sheet we must not rewrite: without these
        # two columns there is no way to tell a survivor from a victim.
        if not columns.has("Job Key") or not columns.has("Application Status"):
            log.warning(
                "%s has no Job Key/Application Status column — leaving it alone",
                self.path.name,
            )
            return 0
        key_col = columns["Job Key"]
        status_col = columns["Application Status"]
        last_col = max(ws.max_column, columns.width)
        body_rows = max(ws.max_row - 1, 0)

        blanks = 0
        for row in range(2, ws.max_row + 1):
            values = [ws.cell(row=row, column=col).value for col in range(1, last_col + 1)]
            key = values[key_col - 1]
            if not key:
                # A row with no key but with content is unreadable, not
                # disposable — someone may have typed it in by hand. Only a
                # wholly empty row is worth reclaiming.
                if all(v in (None, "") for v in values):
                    blanks += 1
                else:
                    survivors.append(values)
                continue
            status = (values[status_col - 1] or "").strip()
            if str(key) in keys and status in SYSTEM_STATUSES:
                removed += 1
                continue
            survivors.append(values)

        if not removed and not blanks:
            return 0

        # Tripwire: a body that produced no survivors means the layout was
        # misread. Refusing costs a prune; rewriting costs the sheet.
        if body_rows and not survivors and removed < body_rows:
            log.error(
                "%s: read %d rows but kept none — refusing to rewrite",
                self.path.name, body_rows,
            )
            return 0

        # Rewrite rather than delete_rows(): openpyxl leaves the row dimensions
        # and styling of a deleted row behind, so deleting 111 rows left 111
        # blank-but-formatted rows and max_row never shrank. Rebuilding the
        # body is the only way to actually reclaim them.
        if ws.max_row >= 2:
            ws.delete_rows(2, ws.max_row - 1)   # a COUNT of body rows, not the last row
        link_col = columns.get("Application Link")
        for offset, values in enumerate(survivors):
            row = 2 + offset
            for col, value in enumerate(values, start=1):
                cell = ws.cell(row=row, column=col)
                cell.value = value
                if col == link_col and value:
                    cell.hyperlink = str(value)
                    cell.style = "Hyperlink"

        self._format(ws, columns)
        wb.save(self.path)
        return removed

    @staticmethod
    def _refresh_ages(ws: Worksheet, columns: ColumnMap, skip: set[int]) -> int:
        """Re-derive Age on rows this export did not rewrite.

        `export_jobs(only_new=True)` skips jobs already exported, so without
        this a row's Age freezes at whatever it was the day it first landed and
        the column quietly starts lying. The dates are already in the sheet, so
        no database read is needed.
        """
        age_col = columns.get("Age")
        posted_col = columns.get("Posting Date")
        if age_col is None or posted_col is None:
            return 0
        found_col = columns.get("Date Found")

        refreshed = 0
        for row in range(2, ws.max_row + 1):
            if row in skip:
                continue
            # A shim rather than a second implementation: same fallback, same
            # "~" marker for an age inferred from when we first saw the row.
            shim = _RowDates(
                posted_at=_parse_date(ws.cell(row=row, column=posted_col).value),
                found_at=(
                    _parse_date(ws.cell(row=row, column=found_col).value)
                    if found_col else None
                ),
            )
            cell = ws.cell(row=row, column=age_col)
            label = age_label(shim)
            if cell.value != label:
                cell.value = label
                refreshed += 1
        return refreshed

    @staticmethod
    def _sync_status(ws: Worksheet, row_idx: int, db_status: str, columns: ColumnMap) -> None:
        """Let system-set statuses through only while the row is untouched.

        The Status column belongs to the user — once they've moved a row to
        "Applied" or "Rejected", nothing here may overwrite that. But a row
        still in a system-owned state records no user decision, so the pipeline
        may update it in both directions: flagging a low scorer "Manual
        Review", and clearing that flag when the job later clears the
        threshold.
        """
        status_col = columns.get("Application Status")
        if status_col is None:
            return
        cell = ws.cell(row=row_idx, column=status_col)
        current = (cell.value or "").strip()
        if current in SYSTEM_STATUSES and db_status and db_status != current:
            cell.value = db_status

    @staticmethod
    def _write_row(
        ws: Worksheet, row_idx: int, values: dict[str, object], columns: ColumnMap
    ) -> None:
        for name, value in values.items():
            col = columns.get(name)
            if col is None:      # a sheet we do not fully control; skip, never raise
                continue
            cell = ws.cell(row=row_idx, column=col)
            cell.value = value
            if name == "Application Link" and value:
                cell.hyperlink = str(value)
                cell.style = "Hyperlink"
            if name in ("JD", "Required Skills"):
                cell.alignment = Alignment(vertical="top", wrap_text=False)

    # -- formatting --------------------------------------------------------
    def _format(self, ws: Worksheet, columns: ColumnMap) -> None:
        header_fill = PatternFill("solid", start_color="FF1F3864")
        for name, width in COLUMNS:
            idx = columns.get(name)
            if idx is None:
                continue
            cell = ws.cell(row=1, column=idx)
            cell.font = Font(bold=True, color="FFFFFFFF")
            cell.fill = header_fill
            cell.alignment = Alignment(vertical="center")
            ws.column_dimensions[get_column_letter(idx)].width = width

        ws.freeze_panes = "A2"
        # Span what the sheet actually has, so a column the user added is
        # filterable too and nothing we did not write gets clipped.
        last_col = max(ws.max_column, columns.width)
        ws.auto_filter.ref = f"A1:{get_column_letter(last_col)}{max(ws.max_row, 1)}"
        # The join key is machine data, not something to read.
        key_letter = columns.letter("Job Key")
        if key_letter:
            ws.column_dimensions[key_letter].hidden = True

        last_row = max(ws.max_row, 2)
        status_letter = columns.letter("Application Status")
        if status_letter is None:
            return          # no status column: nothing to colour or validate
        status_range = f"{status_letter}2:{status_letter}{last_row}"

        # Replace rules each save so the range keeps up with appended rows.
        ws.conditional_formatting = type(ws.conditional_formatting)()
        colours = {
            "Not Applied": "FFF2F2F2",
            "Manual Review": "FFFFF2CC",
            "Closed": "FFE0E0E0",
            "Applied": "FFDDEBF7",
            "Interviewing": "FFD9EAD3",
            "Rejected": "FFF4CCCC",
            "Offer": "FFB6D7A8",
            "Skipped": "FFEFEFEF",
        }
        for status, colour in colours.items():
            ws.conditional_formatting.add(
                status_range,
                CellIsRule(
                    operator="equal",
                    formula=[f'"{status}"'],
                    fill=PatternFill("solid", start_color=colour),
                ),
            )

        # Re-add the dropdown against the current range.
        ws.data_validations.dataValidation = []
        validation = DataValidation(
            type="list",
            formula1='"' + ",".join(STATUS_VALUES) + '"',
            allow_blank=True,
            showDropDown=False,
        )
        ws.add_data_validation(validation)
        validation.add(status_range)


def export_jobs(cfg, only_new: bool = True) -> tuple[int, int, int]:
    """Export jobs from the DB to the workbook. Returns (appended, updated, total)."""
    path = cfg.get("excel.path", "data/job_tracker.xlsx")
    tracker = ExcelTracker(path)

    with get_session() as session:
        query = session.query(Job)
        if only_new:
            query = query.filter(Job.exported_to_excel.is_(False))
        jobs = query.order_by(Job.found_at.desc(), Job.id.desc()).all()

        appended, updated = tracker.export(jobs)

        for job in jobs:
            job.exported_to_excel = True

    log.info("Excel export: %d appended, %d updated -> %s", appended, updated, tracker.path)
    return appended, updated, len(jobs)
