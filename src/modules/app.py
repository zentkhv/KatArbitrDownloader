import datetime
import json
import logging
import os
import threading
import time
from collections import deque

from flask import Flask, jsonify, render_template, request, send_file

from . import converter
from . import scraper
from . import report

# Werkzeug logs every request at INFO level by default, which drowns the
# console in "GET /status 200" lines since the page polls every 2s. We
# still want our own log() lines (actual scraping/conversion progress) to
# print, so only the HTTP access log is silenced.
logging.getLogger("werkzeug").setLevel(logging.WARNING)

# templates/ lives at src/templates (next to the main.py entry point, one
# level up from this modules/ package), not relative to this file — so the
# default template_folder (which Flask would otherwise resolve to
# src/modules/templates) is pointed there explicitly.
_TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")
app = Flask(__name__, template_folder=_TEMPLATES_DIR)

_lock = threading.Lock()
_worker_thread = None
_stop_event = threading.Event()
_log_file = None
_run_started_monotonic = None

_STATE_DEFAULTS = {
    "case_url": "",
    "output_dir": "",
    "headless": False,
    "convert_to_md": True,
    "rate_per_min": scraper.MAX_RATE_PER_MIN,
    "from_page": None,
    "to_page": None,
    "discovered_total_pages": None,
    "missing_pages": [],
}

STATE_FILENAME = "state.json"


def _state_path(output_dir):
    return os.path.join(output_dir, STATE_FILENAME)


def _load_state(output_dir):
    """state.json lives inside the chosen working folder itself, not one
    global file for the whole app — so a case's remembered settings travel
    with its folder, and picking a different folder starts from defaults
    rather than leaking the previous case's values into it."""
    if not output_dir:
        return dict(_STATE_DEFAULTS)
    try:
        with open(_state_path(output_dir), "r", encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, json.JSONDecodeError):
        saved = {}
    merged = dict(_STATE_DEFAULTS)
    merged.update({k: v for k, v in saved.items() if k in _STATE_DEFAULTS})
    merged["output_dir"] = output_dir
    return merged


def _save_state(output_dir, partial):
    if not output_dir:
        return
    current = _load_state(output_dir)
    current.update(partial)
    try:
        os.makedirs(output_dir, exist_ok=True)
        with open(_state_path(output_dir), "w", encoding="utf-8") as f:
            json.dump(current, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _ensure_folder_state(output_dir):
    """Load output_dir's state.json, creating a fresh default one right
    there first if it doesn't exist yet — called whenever a folder is
    chosen (by dialog or typed in), so a brand-new folder gets its own
    state.json immediately instead of waiting for some later action to
    happen to save something into it."""
    if not os.path.exists(_state_path(output_dir)):
        _save_state(output_dir, {"output_dir": output_dir})
    return _load_state(output_dir)


status = {
    "phase": "idle",  # idle | downloading | converting | checking | done | stopped | error
    "message": "",
    "page": 0,
    "total_pages": 0,
    "downloaded": 0,
    "skipped": 0,
    "failed": 0,
    "md_total": 0,
    "md_processed": 0,
    "md_converted": 0,
    "md_skipped": 0,
    "md_failed": 0,
    "expected_docs": None,
    "actual_docs": None,
    "missing_pages": [],
    "discovered_total_pages": None,
    "log_tail": [],
    "elapsed_seconds": 0,
}

_LOG_TAIL_MAXLEN = 300


def _make_log():
    tail = deque(maxlen=_LOG_TAIL_MAXLEN)
    status["log_tail"] = tail

    def log(msg):
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        elapsed = time.monotonic() - _run_started_monotonic
        # Captures the elapsed time at each log line so that once the job
        # finishes (no more log() calls), /status has a frozen final value
        # to fall back on instead of recomputing from a now-stale start time.
        status["elapsed_seconds"] = round(elapsed)
        line = f"[{ts}] (+{elapsed:7.1f}s) {msg}"
        print(line, flush=True)
        if _log_file:
            with open(_log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        tail.append(line)

    return log


def _scan_existing(download_dir, md_dir):
    pdf_count = len([f for f in os.listdir(download_dir) if f.lower().endswith(".pdf")]) if os.path.isdir(download_dir) else 0
    md_count = len([f for f in os.listdir(md_dir) if f.lower().endswith(".md")]) if os.path.isdir(md_dir) else 0
    return pdf_count, md_count


def _run_discover_job(case_url, headless, output_dir):
    global _run_started_monotonic, _log_file
    _run_started_monotonic = time.monotonic()
    _log_file = None  # discovery doesn't need an output folder

    log = _make_log()
    status.update(phase="discovering", message="", discovered_total_pages=None)
    log(f"Разведка. Дело: {case_url}.")

    try:
        result = scraper.run_discover(case_url, headless, output_dir, log, _stop_event, status)
    except Exception as e:
        status["phase"] = "error"
        status["message"] = str(e)
        log(f"ОШИБКА разведки: {e}")
        return

    status["phase"] = "stopped" if result == "stopped" else "done"
    if output_dir and status.get("discovered_total_pages") is not None:
        _save_state(output_dir, {"discovered_total_pages": status["discovered_total_pages"]})


def _run_job(case_url, output_dir, headless, convert_to_md, rate_per_min, from_page, to_page):
    global _run_started_monotonic, _log_file
    _run_started_monotonic = time.monotonic()

    download_dir = os.path.join(output_dir, "download")
    md_dir = os.path.join(output_dir, "md")
    os.makedirs(output_dir, exist_ok=True)
    _log_file = os.path.join(output_dir, "run_log.txt")

    log = _make_log()

    pdf_count, md_count = _scan_existing(download_dir, md_dir)
    if pdf_count or md_count:
        log(f"Найдена существующая папка: {pdf_count} PDF, {md_count} MD — уже готовое будет пропущено.")

    status.update(
        phase="downloading",
        message="",
        page=0,
        total_pages=0,
        downloaded=0,
        skipped=0,
        failed=0,
        md_total=0,
        md_processed=0,
        md_converted=0,
        md_skipped=0,
        md_failed=0,
        expected_docs=None,
        actual_docs=None,
        missing_pages=[],
    )
    log(f"Старт. Дело: {case_url}. Папка: {output_dir}. Headless: {headless}. "
        f"Конвертация в MD: {convert_to_md}. Темп: {rate_per_min}/мин. "
        f"Страницы: {from_page or 1}-{to_page or 'конец'}.")

    try:
        result = scraper.run(
            case_url, download_dir, output_dir, headless, log, _stop_event, status, rate_per_min,
            start_page=from_page or 1, end_page=to_page,
        )
    except Exception as e:
        status["phase"] = "error"
        status["message"] = str(e)
        log(f"ОШИБКА: {e}")
        return

    if result == "stopped":
        status["phase"] = "stopped"
        log(f"Остановлено. Итог: downloaded={status['downloaded']}, skipped={status['skipped']}, failed={status['failed']}.")
        return

    log(f"Скачивание готово. downloaded={status['downloaded']}, skipped={status['skipped']}, failed={status['failed']}.")

    if not convert_to_md:
        status["phase"] = "done"
        log("Конвертация не запрошена. Готово.")
        return

    status["phase"] = "converting"
    try:
        conv_result = converter.run(download_dir, md_dir, output_dir, log, _stop_event, status)
    except Exception as e:
        status["phase"] = "error"
        status["message"] = str(e)
        log(f"ОШИБКА конвертации: {e}")
        return

    status["phase"] = "stopped" if conv_result == "stopped" else "done"
    log(f"Готово. Итог: PDF downloaded={status['downloaded']}, skipped={status['skipped']}, failed={status['failed']}; "
        f"MD converted={status['md_converted']}, skipped={status['md_skipped']}, failed={status['md_failed']}.")


def _run_actualize_job(case_url, output_dir, headless, download_missing, convert_to_md, rate_per_min):
    global _run_started_monotonic, _log_file
    _run_started_monotonic = time.monotonic()

    download_dir = os.path.join(output_dir, "download")
    md_dir = os.path.join(output_dir, "md")
    os.makedirs(output_dir, exist_ok=True)
    _log_file = os.path.join(output_dir, "run_log.txt")

    log = _make_log()
    status.update(
        phase="checking", message="", page=0, total_pages=0,
        downloaded=0, skipped=0, failed=0,
        md_total=0, md_processed=0, md_converted=0, md_skipped=0, md_failed=0,
        expected_docs=None, actual_docs=None, missing_pages=[],
    )
    log(f"Актуализация. Дело: {case_url}. Папка: {output_dir}. Докачка: {download_missing}.")

    try:
        result = scraper.run_actualize(
            case_url, output_dir, headless, log, _stop_event, status, download_missing, rate_per_min
        )
    except Exception as e:
        status["phase"] = "error"
        status["message"] = str(e)
        log(f"ОШИБКА актуализации: {e}")
        return

    _save_state(output_dir, {"missing_pages": status.get("missing_pages", [])})

    if result == "stopped":
        status["phase"] = "stopped"
        return

    if not download_missing or not convert_to_md:
        status["phase"] = "done"
        return

    status["phase"] = "converting"
    try:
        conv_result = converter.run(download_dir, md_dir, output_dir, log, _stop_event, status)
    except Exception as e:
        status["phase"] = "error"
        status["message"] = str(e)
        log(f"ОШИБКА конвертации: {e}")
        return
    status["phase"] = "stopped" if conv_result == "stopped" else "done"


def _run_convert_job(output_dir):
    global _run_started_monotonic, _log_file
    _run_started_monotonic = time.monotonic()

    download_dir = os.path.join(output_dir, "download")
    md_dir = os.path.join(output_dir, "md")
    os.makedirs(output_dir, exist_ok=True)
    _log_file = os.path.join(output_dir, "run_log.txt")

    log = _make_log()

    if not os.path.isdir(download_dir):
        status["phase"] = "error"
        status["message"] = f"Папка с PDF не найдена: {download_dir}"
        log(f"ОШИБКА: папка с PDF не найдена: {download_dir}")
        return

    status.update(
        phase="converting",
        message="",
        md_total=0,
        md_processed=0,
        md_converted=0,
        md_skipped=0,
        md_failed=0,
    )
    log(f"Только конвертация. Папка: {output_dir}.")

    try:
        result = converter.run(download_dir, md_dir, output_dir, log, _stop_event, status)
    except Exception as e:
        status["phase"] = "error"
        status["message"] = str(e)
        log(f"ОШИБКА конвертации: {e}")
        return

    status["phase"] = "stopped" if result == "stopped" else "done"
    log(f"Готово. MD converted={status['md_converted']}, skipped={status['md_skipped']}, failed={status['md_failed']}.")


def _start_background(target, args):
    global _worker_thread
    with _lock:
        if _worker_thread and _worker_thread.is_alive():
            return False
        _stop_event.clear()
        _worker_thread = threading.Thread(target=target, args=args, daemon=True)
        _worker_thread.start()
        return True


@app.route("/")
def index():
    return render_template(
        "index.html",
        min_rate=scraper.MIN_RATE_PER_MIN,
        max_rate=scraper.MAX_RATE_PER_MIN,
        state=dict(_STATE_DEFAULTS),
    )


@app.route("/start", methods=["POST"])
def start():
    data = request.get_json(force=True)
    case_url = (data.get("case_url") or "").strip()
    output_dir = (data.get("output_dir") or "").strip()
    headless = bool(data.get("headless"))
    convert_to_md = bool(data.get("convert_to_md"))
    rate_per_min = data.get("rate_per_min", scraper.MAX_RATE_PER_MIN)
    try:
        rate_per_min = float(rate_per_min)
    except (TypeError, ValueError):
        rate_per_min = scraper.MAX_RATE_PER_MIN

    def _to_int_or_none(v):
        try:
            return int(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    from_page = _to_int_or_none(data.get("from_page"))
    to_page = _to_int_or_none(data.get("to_page"))

    if not case_url:
        return jsonify({"ok": False, "error": "Укажи ссылку на дело"}), 400
    if not output_dir:
        return jsonify({"ok": False, "error": "Укажи папку для сохранения"}), 400

    _save_state(output_dir, {
        "case_url": case_url, "headless": headless,
        "convert_to_md": convert_to_md, "rate_per_min": rate_per_min,
        "from_page": from_page, "to_page": to_page,
    })
    os.makedirs(output_dir, exist_ok=True)

    if not _start_background(
        _run_job, (case_url, output_dir, headless, convert_to_md, rate_per_min, from_page, to_page)
    ):
        return jsonify({"ok": False, "error": "Задача уже выполняется"}), 409
    return jsonify({"ok": True})


@app.route("/discover", methods=["POST"])
def discover():
    data = request.get_json(force=True)
    case_url = (data.get("case_url") or "").strip()
    output_dir = (data.get("output_dir") or "").strip()
    headless = bool(data.get("headless"))

    if not case_url:
        return jsonify({"ok": False, "error": "Укажи ссылку на дело"}), 400

    if output_dir:
        _save_state(output_dir, {"case_url": case_url, "headless": headless})

    if not _start_background(_run_discover_job, (case_url, headless, output_dir)):
        return jsonify({"ok": False, "error": "Задача уже выполняется"}), 409
    return jsonify({"ok": True})


@app.route("/actualize", methods=["POST"])
def actualize():
    data = request.get_json(force=True)
    case_url = (data.get("case_url") or "").strip()
    output_dir = (data.get("output_dir") or "").strip()
    headless = bool(data.get("headless"))
    download_missing = bool(data.get("download_missing"))
    convert_to_md = bool(data.get("convert_to_md"))
    rate_per_min = data.get("rate_per_min", scraper.MAX_RATE_PER_MIN)
    try:
        rate_per_min = float(rate_per_min)
    except (TypeError, ValueError):
        rate_per_min = scraper.MAX_RATE_PER_MIN

    if not case_url:
        return jsonify({"ok": False, "error": "Укажи ссылку на дело"}), 400
    if not output_dir:
        return jsonify({"ok": False, "error": "Укажи папку для сохранения"}), 400

    _save_state(output_dir, {"case_url": case_url, "headless": headless})
    os.makedirs(output_dir, exist_ok=True)

    if not _start_background(
        _run_actualize_job, (case_url, output_dir, headless, download_missing, convert_to_md, rate_per_min)
    ):
        return jsonify({"ok": False, "error": "Задача уже выполняется"}), 409
    return jsonify({"ok": True})


@app.route("/convert", methods=["POST"])
def convert():
    data = request.get_json(force=True)
    output_dir = (data.get("output_dir") or "").strip()

    if not output_dir:
        return jsonify({"ok": False, "error": "Укажи папку для сохранения"}), 400

    if not _start_background(_run_convert_job, (output_dir,)):
        return jsonify({"ok": False, "error": "Задача уже выполняется"}), 409
    return jsonify({"ok": True})


@app.route("/report")
def report_page():
    output_dir = (request.args.get("output_dir") or "").strip()
    if not output_dir or not os.path.isdir(output_dir):
        return render_template("report.html", rows=None, output_dir=output_dir, columns=report.COLUMNS)
    rows = report.build_rows(output_dir)
    return render_template("report.html", rows=rows, output_dir=output_dir, columns=report.COLUMNS)


@app.route("/report/export.xlsx")
def report_export():
    output_dir = (request.args.get("output_dir") or "").strip()
    if not output_dir or not os.path.isdir(output_dir):
        return jsonify({"ok": False, "error": "Папка с результатами не найдена"}), 400
    rows = report.build_rows(output_dir)
    buf = report.build_xlsx(rows)
    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name="docskatarbitr_otchet.xlsx",
    )


@app.route("/stop", methods=["POST"])
def stop():
    _stop_event.set()
    return jsonify({"ok": True})


@app.route("/pick_folder")
def pick_folder():
    # The Flask dev server and the browser run on the same machine here,
    # so a native dialog opened server-side actually shows up for the user.
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        path = filedialog.askdirectory()
    finally:
        root.destroy()
    state = _ensure_folder_state(path) if path else dict(_STATE_DEFAULTS)
    return jsonify({"ok": True, "path": path or "", "state": state})


@app.route("/folder_state", methods=["POST"])
def folder_state():
    # For when the folder is typed/pasted in by hand instead of picked via
    # the native dialog — same effect: load that folder's state.json if it
    # has one already, or create a fresh one right there if it doesn't.
    data = request.get_json(force=True)
    output_dir = (data.get("output_dir") or "").strip()
    if not output_dir:
        return jsonify({"ok": False, "error": "Укажи папку"}), 400
    state = _ensure_folder_state(output_dir)
    return jsonify({"ok": True, "state": state})


@app.route("/reset_state", methods=["POST"])
def reset_state():
    data = request.get_json(force=True) or {}
    output_dir = (data.get("output_dir") or "").strip()
    if output_dir:
        try:
            path = _state_path(output_dir)
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass
    status.update(
        discovered_total_pages=None,
        expected_docs=None,
        actual_docs=None,
        missing_pages=[],
    )
    return jsonify({"ok": True})


@app.route("/status")
def get_status():
    out = dict(status)
    out["log_tail"] = list(status["log_tail"])[-50:]
    running = bool(_worker_thread and _worker_thread.is_alive())
    out["running"] = running
    # Computed fresh only while a job is actually running — a stretch with
    # no log lines (e.g. the quiet page-walk during a check) would otherwise
    # leave the counter frozen even though work is ongoing. Once the job
    # finishes, _run_started_monotonic itself goes stale (it isn't reset
    # until the *next* job starts), so recomputing unconditionally made the
    # elapsed time keep climbing forever after completion; once not running,
    # this falls through to status["elapsed_seconds"] as last written by
    # log() — the job's own final elapsed time, frozen for good.
    if running and _run_started_monotonic is not None:
        out["elapsed_seconds"] = round(time.monotonic() - _run_started_monotonic)
    return jsonify(out)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
