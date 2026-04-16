"""gspread-backed Google Sheets client.

Deliberately thin: provides worksheet create-or-fetch, header-auto-detect
row reading, idempotent row writes, and data-validation dropdowns. Mirrors
the bhiksha GoogleSheetTableClient pattern so operators see a consistent
UX across the two systems.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

logger = logging.getLogger("vol_crush.integrations.google_sheets")

_SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_SHEET_ID_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9_-]+)")


def extract_spreadsheet_id(raw: str) -> str:
    """Accept either a raw spreadsheet ID or a full Google Sheets URL."""
    if not raw:
        return ""
    match = _SHEET_ID_RE.search(raw)
    return match.group(1) if match else raw.strip()


def resolve_credentials_path(
    config: Mapping[str, Any], *, fallback_env: str = "GOOGLE_API_CREDENTIALS_PATH"
) -> Path:
    raw = (
        (config.get("google_sheets") or {}).get("credentials_file")
        or os.environ.get(fallback_env)
        or ""
    )
    if not raw:
        raise RuntimeError(
            "Google Sheets credentials path not configured. Set "
            "google_sheets.credentials_file in config.yaml or "
            f"{fallback_env} in .env."
        )
    path = Path(raw).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"credentials file not found: {path}")
    return path


def resolve_spreadsheet_id(config: Mapping[str, Any]) -> str:
    raw = (config.get("google_sheets") or {}).get("spreadsheet_id") or ""
    if not raw:
        raise RuntimeError(
            "Google Sheets spreadsheet_id not configured. Set KAMANDAL_SHEET_ID "
            "in .env or google_sheets.spreadsheet_id in config.yaml."
        )
    return extract_spreadsheet_id(str(raw))


@dataclass
class WorksheetHandle:
    """Lightweight wrapper around a gspread worksheet with extras."""

    worksheet: Any
    title: str

    def all_rows(self) -> list[list[str]]:
        return self.worksheet.get_all_values() or []

    def header(self) -> list[str]:
        rows = self.all_rows()
        for row in rows:
            nonempty = [cell for cell in row if (cell or "").strip()]
            if len(nonempty) >= 2:
                return [cell.strip() for cell in row]
        return []

    def data_rows(self) -> list[dict[str, str]]:
        """Return all data rows keyed by the detected header. Empty rows skipped."""
        all_rows = self.all_rows()
        header = self.header()
        if not header:
            return []
        try:
            header_index = all_rows.index(
                [h if h in header else "" for h in all_rows[0]]
            )
        except ValueError:
            header_index = 0
        for idx, row in enumerate(all_rows):
            if [cell.strip() for cell in row] == header:
                header_index = idx
                break
        data: list[dict[str, str]] = []
        for raw in all_rows[header_index + 1 :]:
            if not any((cell or "").strip() for cell in raw):
                continue
            padded = list(raw) + [""] * (len(header) - len(raw))
            data.append({header[i]: padded[i].strip() for i in range(len(header))})
        return data

    def replace_contents(
        self, header: Sequence[str], rows: Sequence[Sequence[Any]]
    ) -> None:
        """Atomically rewrite the worksheet with the given header + rows."""
        self.worksheet.clear()
        payload = [list(header)]
        for row in rows:
            padded = list(row) + [""] * (len(header) - len(row))
            payload.append([_to_cell_value(cell) for cell in padded])
        if payload:
            self.worksheet.update(
                range_name=f"A1:{_col_letter(len(header))}{len(payload)}",
                values=payload,
                value_input_option="USER_ENTERED",
            )

    def ensure_header(self, header: Sequence[str]) -> None:
        current = self.header()
        if current == list(header):
            return
        # Preserve any existing data rows that match the expected header.
        data = self.data_rows() if current else []
        rows = [[row.get(col, "") for col in header] for row in data]
        self.replace_contents(header, rows)

    def set_enum_validation(self, column: str, options: Sequence[str]) -> None:
        """Apply a data-validation dropdown to all data cells under ``column``.

        Silently no-ops if the column is not present in the current header.
        """
        header = self.header()
        if column not in header:
            return
        col_idx = header.index(column) + 1
        rule = {
            "setDataValidation": {
                "range": {
                    "sheetId": self.worksheet.id,
                    "startRowIndex": 1,
                    "endRowIndex": 1000,
                    "startColumnIndex": col_idx - 1,
                    "endColumnIndex": col_idx,
                },
                "rule": {
                    "condition": {
                        "type": "ONE_OF_LIST",
                        "values": [{"userEnteredValue": opt} for opt in options],
                    },
                    "strict": False,
                    "showCustomUi": True,
                },
            }
        }
        self.worksheet.spreadsheet.batch_update({"requests": [rule]})


def _to_cell_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return value
    return str(value)


def _col_letter(n: int) -> str:
    letters = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


class GoogleSheetClient:
    """Operator-facing sheet client.

    Constructed once per process. `get_worksheet` is idempotent — it creates
    the worksheet if missing, so bootstrap + pull + push can all use the
    same code path safely.
    """

    def __init__(
        self,
        *,
        credentials_path: Path,
        spreadsheet_id: str,
        scopes: Sequence[str] = tuple(_SHEETS_SCOPES),
    ):
        try:
            import gspread  # type: ignore
            from google.oauth2.service_account import Credentials
        except ImportError as exc:
            raise RuntimeError(
                "gspread / google-auth not installed. Run `pip install -r requirements.txt`."
            ) from exc

        credentials = Credentials.from_service_account_file(
            str(credentials_path), scopes=list(scopes)
        )
        self._client = gspread.authorize(credentials)
        self._spreadsheet = self._client.open_by_key(spreadsheet_id)
        self.spreadsheet_id = spreadsheet_id
        self.title = self._spreadsheet.title
        logger.info(
            "opened Google Sheet %r (id=%s)", self._spreadsheet.title, spreadsheet_id
        )

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "GoogleSheetClient":
        return cls(
            credentials_path=resolve_credentials_path(config),
            spreadsheet_id=resolve_spreadsheet_id(config),
        )

    def get_worksheet(
        self, title: str, *, rows: int = 200, cols: int = 26
    ) -> WorksheetHandle:
        try:
            ws = self._spreadsheet.worksheet(title)
        except Exception:  # gspread.WorksheetNotFound on a miss
            ws = self._spreadsheet.add_worksheet(title=title, rows=rows, cols=cols)
            logger.info("created worksheet %r", title)
        return WorksheetHandle(worksheet=ws, title=title)

    def worksheet_titles(self) -> list[str]:
        return [ws.title for ws in self._spreadsheet.worksheets()]

    def delete_worksheet(self, title: str) -> None:
        try:
            ws = self._spreadsheet.worksheet(title)
        except Exception:
            return
        self._spreadsheet.del_worksheet(ws)

    def ensure_no_default_sheet1(self) -> None:
        """Remove the default 'Sheet1' if it exists *and* other tabs are present."""
        titles = self.worksheet_titles()
        if "Sheet1" in titles and len(titles) > 1:
            self.delete_worksheet("Sheet1")


def coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "yes", "y", "1"}:
        return True
    if text in {"false", "no", "n", "0", ""}:
        return False
    return default


def as_bool_cell(value: bool) -> str:
    return "TRUE" if value else "FALSE"


def as_list_cell(values: Iterable[Any]) -> str:
    return ", ".join(str(v) for v in values if v is not None and str(v).strip())
