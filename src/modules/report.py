import io
import os

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from . import scraper

COLUMNS = [
    ("category", "Категория"),
    ("title", "Документ"),
    ("doc_id", "id документа на сайте"),
    ("url", "Ссылка на PDF"),
    ("md_status", "Статус MD"),
    ("converted_at", "Дата конвертации"),
    ("last_verified_at", "Дата последней сверки"),
    ("downloaded_at", "Дата скачивания"),
    ("vanished_at", "Дата исчезновения"),
]


def build_rows(output_dir):
    """One row per document in manifest.json, ready for both the HTML
    report page and the Excel export — same data, same column order."""
    download_dir = os.path.join(output_dir, "download")
    md_dir = os.path.join(output_dir, "md")
    manifest = scraper.load_manifest(output_dir, download_dir, md_dir)

    rows = []
    for doc_id, entry in manifest.items():
        vanished = bool(entry.get("vanished_at"))
        downloaded_at = entry.get("downloaded_at") or ""
        if downloaded_at and entry.get("downloaded_at_estimated"):
            downloaded_at += " (оценка по дате файла)"
        rows.append({
            "doc_id": doc_id,
            "title": entry.get("title") or entry.get("filename") or doc_id,
            "url": entry.get("url") or "",
            "category": "Пропал" if vanished else "Собран",
            "md_status": "Сконвертирован" if entry.get("converted_at") else "Не сконвертирован",
            "converted_at": entry.get("converted_at") or "",
            "last_verified_at": entry.get("last_verified_at") or "",
            "downloaded_at": downloaded_at,
            "vanished_at": entry.get("vanished_at") or "",
        })

    # Собранные документы впереди, пропавшие — отдельным блоком в конце;
    # внутри каждой группы — по названию (там же дата в формате YYYYMMDD).
    rows.sort(key=lambda r: (r["category"] == "Пропал", r["title"]))
    return rows


def build_xlsx(rows):
    wb = Workbook()
    ws = wb.active
    ws.title = "Документы дела"

    header_font = Font(bold=True)
    for col_idx, (_, label) in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=1, column=col_idx, value=label)
        cell.font = header_font

    url_col = next((i for i, (key, _) in enumerate(COLUMNS, start=1) if key == "url"), None)
    for row_idx, row in enumerate(rows, start=2):
        for col_idx, (key, _) in enumerate(COLUMNS, start=1):
            value = row.get(key, "")
            cell = ws.cell(row=row_idx, column=col_idx, value=value)
            if col_idx == url_col and value:
                cell.hyperlink = value
                cell.style = "Hyperlink"

    widths = [12, 62, 40, 46, 18, 20, 20, 24, 20]
    for col_idx, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = width
    ws.freeze_panes = "A2"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf
