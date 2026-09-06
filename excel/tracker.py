"""Excel tracking sheet — DB is the source of truth, the sheet is the view.

Append-only by design: the user edits Status by hand in the workbook, so a
re-export must never clobber those edits or duplicate a row. Jobs are matched
back to their sheet row by the hidden Job Key column.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.formatting.rule import CellIsRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.worksheet import Worksheet

from config.loader import PROJECT_ROOT
from db.models import Job
from db.session import get_session

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
    ("Required Skills", 60),
    ("Date Found", 14),
    ("Application Status", 20),
    ("ATS Match Score", 16),
    ("Job Key", 30),
]

STATUS_VALUES = [
    "Not Applied",
    "Manual Review",
    "Applied",
    "Interviewing",
    "Rejected",
    "Offer",
    "Skipped",
]

# Statuses the pipeline sets on its own. An export may update a row still in
# one of these, because there is no user decision recorded there yet. Every
# other value is a human judgement and is never overwritten.
SYSTEM_STATUSES = {"", "Not Applied", "Manual Review"}

# Excel's hard per-cell limit is 32767; stay well under it.
JD_CELL_LIMIT = 20000

COL = {name: idx for idx, (name, _) in enumerate(COLUMNS, start=1)}
KEY_COL = COL["Job Key"]


def job_key(job: Job) -> str:
    """Stable identity for a posting, mirroring the DB unique constraint."""
    return f"{job.source}:{job.company_slug}:{job.external_id}"


def _truncate(text: str | None, limit: int = JD_CELL_LIMIT) -> str:
    if not text:
        return ""
    return text if len(text) <= limit else text[: limit - 20] + "\n… [truncated]"


def _fmt_date(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d") if value else ""


def _row_values(job: Job) -> dict[str, object]:
    return {
        "Company": job.company,
        "Job Title": job.title,
        "Application Link": job.application_url,
        "JD": _truncate(job.description),
        "Location": job.location or "",
        "Posting Date": _fmt_date(job.posted_at),
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

    def _existing_rows(self, ws: Worksheet) -> dict[str, int]:
        """Map Job Key -> row number for every row already in the sheet."""
        found: dict[str, int] = {}
        for row in range(2, ws.max_row + 1):
            key = ws.cell(row=row, column=KEY_COL).value
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
        existing = self._existing_rows(ws)

        appended = 0
        updated = 0

        for job in jobs:
            key = job_key(job)
            values = _row_values(job)
            row_idx = existing.get(key)

            if row_idx is None:
                row_idx = ws.max_row + 1
                self._write_row(ws, row_idx, values)
                ws.cell(row=row_idx, column=COL["Application Status"]).value = job.status
                existing[key] = row_idx
                appended += 1
            else:
                self._write_row(ws, row_idx, values)
                self._sync_status(ws, row_idx, job.status)
                updated += 1

        self._format(ws)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        wb.save(self.path)
        return appended, updated

    @staticmethod
    def _sync_status(ws: Worksheet, row_idx: int, db_status: str) -> None:
        """Let system-set statuses through only while the row is untouched.

        The Status column belongs to the user — once they've moved a row to
        "Applied" or "Rejected", nothing here may overwrite that. But a row
        still in a system-owned state records no user decision, so the pipeline
        may update it in both directions: flagging a low scorer "Manual
        Review", and clearing that flag when the job later clears the
        threshold.
        """
        cell = ws.cell(row=row_idx, column=COL["Application Status"])
        current = (cell.value or "").strip()
        if current in SYSTEM_STATUSES and db_status and db_status != current:
            cell.value = db_status

    @staticmethod
    def _write_row(ws: Worksheet, row_idx: int, values: dict[str, object]) -> None:
        for name, value in values.items():
            cell = ws.cell(row=row_idx, column=COL[name])
            cell.value = value
            if name == "Application Link" and value:
                cell.hyperlink = str(value)
                cell.style = "Hyperlink"
            if name in ("JD", "Required Skills"):
                cell.alignment = Alignment(vertical="top", wrap_text=False)

    # -- formatting --------------------------------------------------------
    def _format(self, ws: Worksheet) -> None:
        header_fill = PatternFill("solid", start_color="FF1F3864")
        for idx, (name, width) in enumerate(COLUMNS, start=1):
            cell = ws.cell(row=1, column=idx)
            cell.font = Font(bold=True, color="FFFFFFFF")
            cell.fill = header_fill
            cell.alignment = Alignment(vertical="center")
            ws.column_dimensions[get_column_letter(idx)].width = width

        ws.freeze_panes = "A2"
        ws.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}{max(ws.max_row, 1)}"
        # The join key is machine data, not something to read.
        ws.column_dimensions[get_column_letter(KEY_COL)].hidden = True

        last_row = max(ws.max_row, 2)
        status_letter = get_column_letter(COL["Application Status"])
        status_range = f"{status_letter}2:{status_letter}{last_row}"

        # Replace rules each save so the range keeps up with appended rows.
        ws.conditional_formatting = type(ws.conditional_formatting)()
        colours = {
            "Not Applied": "FFF2F2F2",
            "Manual Review": "FFFFF2CC",
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
