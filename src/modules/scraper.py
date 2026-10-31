import datetime
import glob
import json
import os
import re
import sys
import time
import types
import urllib.parse

# undetected-chromedriver's patcher.py does `from distutils.version import
# LooseVersion` at import time. distutils was removed from the standard
# library in Python 3.12, so on 3.12+ (this project targets 3.14) that
# import crashes before undetected_chromedriver can even be loaded, with
# "ModuleNotFoundError: No module named 'distutils'". Rather than patch the
# third-party package in site-packages (reinstalling/upgrading it would wipe
# that out), a minimal stand-in for distutils.version.LooseVersion is
# registered in sys.modules here, before undetected_chromedriver is
# imported — it only needs to support what patcher.py actually uses:
# constructing from a version string, `.version` (parsed components, used
# as `release.version[0]` for the major version number) and `.vstring`
# (the original string, used verbatim in a download URL).
if "distutils" not in sys.modules:
    try:
        import distutils.version  # noqa: F401
    except ModuleNotFoundError:
        _component_re = re.compile(r"\d+|[a-zA-Z]+|\.")

        class _LooseVersion:
            def __init__(self, vstring):
                self.vstring = vstring
                self.version = [
                    int(part) if part.isdigit() else part
                    for part in _component_re.findall(vstring)
                    if part != "."
                ]

            def __str__(self):
                return self.vstring

            def __repr__(self):
                return f"LooseVersion ('{self.vstring}')"

        _distutils_version = types.ModuleType("distutils.version")
        _distutils_version.LooseVersion = _LooseVersion
        _distutils = types.ModuleType("distutils")
        _distutils.version = _distutils_version
        sys.modules["distutils"] = _distutils
        sys.modules["distutils.version"] = _distutils_version

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By

# kad.arbitr.ru routes every PDF link through a brief JS-only relay page
# (markers like id="salto"/id="hash") that the browser solves on its own in
# a second or two before the real download fires. Each PDF is opened in its
# own background tab (not the main tab) so the "Электронное дело" list and
# its pagination in the main tab never get navigated away from.

# Backoff schedule (seconds) used when the site stops returning document
# content at all (a soft, session-level cooldown rather than a per-request
# limit — see README/chat notes). Tried in order; stop_event is checked
# throughout so a user Stop is still responsive during a wait.
BLOCK_BACKOFF_SCHEDULE = [5 * 60, 10 * 60, 20 * 60]

PER_DOC_ATTEMPT_WAIT = 40
PER_DOC_MAX_ATTEMPTS = 3

# Pacing ceiling for the rate slider. An earlier run sustained ~13/min
# (median ~4.5s/doc) for 615 documents without trouble, but the site later
# served an explicit hard block ("с вашего IP поступило необычно много
# запросов") — likely caused by an unthrottled quick-check burst (48 page
# clicks back-to-back, now fixed to respect this same rate) rather than the
# paced downloading itself. Raised back up partway, below the original 13.
MIN_RATE_PER_MIN = 1
MAX_RATE_PER_MIN = 10

# Fixed pace for page-to-page navigation when no PDF is being fetched on a
# given page (plain checking, or an actualize pass over pages that turn out
# to have nothing missing) — kept independent of the rate slider, and never
# skipped, so a long run of "nothing missing" pages can't turn into the kind
# of unthrottled back-to-back page-click burst that got an earlier session
# hard-blocked.
CHECK_PAGE_DELAY = 1.0


def _inter_doc_delay(rate_per_min):
    rate = min(max(rate_per_min, MIN_RATE_PER_MIN), MAX_RATE_PER_MIN)
    return 60.0 / rate


class BlockedError(Exception):
    """Raised when the case page loads but the document list never does."""


class Stopped(Exception):
    """Raised internally to unwind cleanly when the user hits Stop."""


def _sleep_checking_stop(seconds, stop_event):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if stop_event.is_set():
            raise Stopped()
        time.sleep(min(2, end - time.monotonic()))


def make_driver(headless: bool) -> uc.Chrome:
    options = uc.ChromeOptions()
    options.add_argument("--disable-popup-blocking")
    # Without these, Chrome throttles JS timers on a window that isn't
    # focused (occluded/backgrounded) — the site's own AJAX pagination and
    # the download relay page rely on that JS, so our fixed waits expire
    # before it gets a chance to run and this looks like random failures
    # whenever the user alt-tabs away from the automation window.
    options.add_argument("--disable-backgrounding-occluded-windows")
    options.add_argument("--disable-renderer-backgrounding")
    options.add_argument("--disable-background-timer-throttling")
    prefs = {
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "plugins.always_open_pdf_externally": True,
    }
    options.add_experimental_option("prefs", prefs)
    return uc.Chrome(options=options, headless=headless)


def _set_download_dir(driver, download_dir):
    # Chrome DevTools Protocol call so we can point an already-created
    # driver at the right folder (prefs are baked in at creation time).
    driver.execute_cdp_cmd(
        "Page.setDownloadBehavior",
        {"behavior": "allow", "downloadPath": download_dir},
    )


def _wait_for_case_page(driver, case_url, log, stop_event, timeout_sec=20 * 60, renav_every=20):
    """Wait for the case page's nav to actually be there.

    If it's missing entirely (as opposed to present but the AJAX content
    not loaded yet — that's handled separately), it's usually a block or
    login page, not a transient glitch. That needs a human to act in the
    visible window, so we wait generously and keep re-navigating in case a
    manual login redirected elsewhere, instead of failing fast and closing
    the browser before anyone notices.
    """
    deadline = time.monotonic() + timeout_sec
    last_renav = time.monotonic()
    last_reminder = 0.0
    reminded = False
    while not driver.find_elements(By.CSS_SELECTOR, ".js-case-chrono-button--ed"):
        if stop_event.is_set():
            raise Stopped()
        now = time.monotonic()
        if now > deadline:
            raise RuntimeError(
                "Не найдена вкладка 'Электронное дело' — похоже на страницу блокировки "
                "или входа, и за отведённое время её не решили. Проверь окно Chrome."
            )
        if not reminded or now - last_reminder > 20:
            log("Не вижу карточку дела — похоже на страницу блокировки/входа. Если нужно "
                "авторизоваться или подтвердить что-то — сделай это в открытом окне Chrome, жду...")
            last_reminder = now
            reminded = True
        if now - last_renav > renav_every:
            driver.get(case_url)
            last_renav = now
        time.sleep(3)


def open_electronic_case(driver, case_url, log, stop_event):
    driver.get(case_url)
    time.sleep(5)
    _wait_for_case_page(driver, case_url, log, stop_event)

    for attempt in range(1, 6):
        if stop_event.is_set():
            raise Stopped()

        btns = driver.find_elements(By.CSS_SELECTOR, ".js-case-chrono-button--ed")
        if not btns:
            _wait_for_case_page(driver, case_url, log, stop_event)
            btns = driver.find_elements(By.CSS_SELECTOR, ".js-case-chrono-button--ed")
        btn = btns[0]
        if "active" not in btn.get_attribute("class"):
            btn.click()
        time.sleep(3)

        loaded = driver.find_elements(
            By.CSS_SELECTOR,
            "#chrono_ed_content a.b-case-chrono-ed-item-link, "
            "#chrono_ed_content .b-chrono-pagination-pager-item",
        )
        not_hidden = driver.find_elements(By.CSS_SELECTOR, "#chrono_ed_content:not(.g-hidden)")
        if loaded or not_hidden:
            return

        log(f"'Электронное дело' не загрузилось, попытка {attempt}/5, повтор...")
        driver.get(case_url)
        time.sleep(5)

    raise BlockedError("Список документов не загрузился после нескольких попыток")


def get_total_pages(driver) -> int:
    items = driver.find_elements(
        By.CSS_SELECTOR,
        "#chrono_ed_content .b-chrono-pagination-pager-item[data-page_num]",
    )
    pages = [int(el.get_attribute("data-page_num")) for el in items if el.get_attribute("data-page_num").isdigit()]
    return max(pages) if pages else 1


def get_current_page(driver) -> int:
    active = driver.find_elements(
        By.CSS_SELECTOR,
        "#chrono_ed_content .b-chrono-pagination-pager-item--active[data-page_num]",
    )
    if not active:
        return 1
    return int(active[0].get_attribute("data-page_num"))


def collect_links(driver):
    elements = driver.find_elements(
        By.CSS_SELECTOR, "#chrono_ed_content a.b-case-chrono-ed-item-link"
    )
    return [el.get_attribute("href") for el in elements if el.get_attribute("href")]


def doc_id_and_name(pdf_url: str):
    parts = pdf_url.rstrip("/").split("/")
    doc_id = parts[-2]
    filename = urllib.parse.unquote(parts[-1])
    filename = re.sub(r'[\\/:*?"<>|]', "_", filename)
    return doc_id, filename


# --- Filenames and the manifest ------------------------------------------
#
# Every document on the site can share its exact displayed name with other
# documents in the same case (several "Opredelenie.pdf" filed the same day
# is common), so some disambiguator tied to the document's own id is
# unavoidable. The very first version prefixed the *full* 36-character
# doc_id in front of every filename to guarantee uniqueness, which made
# every name in the folder start with an unreadable UUID. New downloads now
# get a short (8-char) suffix *after* the readable part instead — enough to
# make a collision practically impossible across a case this size, without
# burying the case/date/type that actually matters for a human scanning the
# folder. Files from earlier runs keep their old prefixed names as-is
# (nothing here renames existing files) — `_existing_path` recognizes both
# schemes so they aren't redownloaded under the new name.
OLD_DOC_ID_PREFIX_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}_"
)
NEW_DOC_ID_SUFFIX_RE = re.compile(r"__[0-9a-f]{8}$")


def derive_title(stem):
    """Human-readable title for a filename stem, regardless of which
    naming scheme (old prefix or new suffix) it was saved under."""
    title = OLD_DOC_ID_PREFIX_RE.sub("", stem)
    title = NEW_DOC_ID_SUFFIX_RE.sub("", title)
    return title


def build_filename(doc_id, filename):
    stem, ext = os.path.splitext(filename)
    return f"{stem}__{doc_id[:8]}{ext}"


def _existing_path(download_dir, doc_id, filename):
    """Where this document already sits on disk, if anywhere — checking
    both the new and the old (legacy) naming scheme. Pattern-matching only,
    no manifest involved — the fallback for documents the manifest doesn't
    know about yet (see _verify_or_locate, which is what callers should
    normally use)."""
    new_path = os.path.join(download_dir, build_filename(doc_id, filename))
    if os.path.exists(new_path):
        return new_path
    old_path = os.path.join(download_dir, f"{doc_id}_{filename}")
    if os.path.exists(old_path):
        return old_path
    return None


def _verify_or_locate(download_dir, doc_id, filename, manifest):
    """The careful version of 'is this document already here': look it up
    by doc_id in the manifest first (the authoritative record of what was
    actually collected under what name), and only fall back to a plain
    filename-pattern guess for documents the manifest doesn't know about
    yet at all (e.g. a folder from before manifest.json existed, on its
    very first load).

    Returns (path_or_None, manifest_mismatch). manifest_mismatch is True
    only when the manifest says this doc_id was downloaded under a given
    filename but that exact file isn't on disk any more (deleted by hand,
    moved out, etc.) — distinct from "never downloaded at all", worth
    calling out separately rather than silently treating it the same as a
    brand-new document.
    """
    entry = manifest.get(doc_id) if manifest is not None else None
    if entry and entry.get("filename"):
        path = os.path.join(download_dir, entry["filename"])
        if os.path.exists(path):
            return path, False
        return None, True
    return _existing_path(download_dir, doc_id, filename), False


MANIFEST_FILENAME = "manifest.json"

_OLD_NAME_RE = re.compile(
    r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})_(.+)$"
)


def _now_iso():
    return datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _mtime_iso(path):
    try:
        return datetime.datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%dT%H:%M:%S")
    except OSError:
        return None


def manifest_path_for(output_dir):
    return os.path.join(output_dir, MANIFEST_FILENAME)


def load_manifest(output_dir, download_dir, md_dir):
    """Load manifest.json, and backfill entries for any PDF already sitting
    in download_dir under the old naming scheme that isn't recorded yet
    (e.g. the whole collection from before this file existed). Backfilled
    entries get their downloaded_at estimated from the file's own mtime
    (flagged as such) since the real moment isn't known."""
    path = manifest_path_for(output_dir)
    try:
        with open(path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, json.JSONDecodeError):
        manifest = {}

    if os.path.isdir(download_dir):
        md_stems = set()
        if os.path.isdir(md_dir):
            md_stems = {os.path.splitext(f)[0] for f in os.listdir(md_dir) if f.lower().endswith(".md")}
        for fname in os.listdir(download_dir):
            if not fname.lower().endswith(".pdf"):
                continue
            m = _OLD_NAME_RE.match(fname)
            if not m:
                continue
            doc_id = m.group(1)
            if doc_id in manifest:
                continue
            stem = os.path.splitext(fname)[0]
            converted_at = None
            if stem in md_stems:
                converted_at = _mtime_iso(os.path.join(md_dir, stem + ".md"))
            manifest[doc_id] = {
                "title": derive_title(stem),
                "filename": fname,
                "url": None,
                "page_last_seen": None,
                "downloaded_at": _mtime_iso(os.path.join(download_dir, fname)),
                "downloaded_at_estimated": True,
                "converted_at": converted_at,
            }
    return manifest


def save_manifest(output_dir, manifest):
    path = manifest_path_for(output_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _record_downloaded(manifest, doc_id, filename, pdf_url, page):
    prev = manifest.get(doc_id, {})
    manifest[doc_id] = {
        "title": derive_title(os.path.splitext(filename)[0]),
        "filename": filename,
        "url": pdf_url,
        "page_last_seen": page,
        "downloaded_at": _now_iso(),
        "downloaded_at_estimated": False,
        "converted_at": prev.get("converted_at"),
        "last_verified_at": _now_iso(),
    }


def _record_seen(manifest, doc_id, existing_path, pdf_url, page):
    """An already-downloaded document was encountered again while walking
    the site (first-time discovery during manifest backfill, or a repeat
    sighting on a later/earlier page as the live case shifts documents
    between pages) — keep the manifest in sync without touching the file."""
    if doc_id not in manifest:
        stem = os.path.splitext(os.path.basename(existing_path))[0]
        manifest[doc_id] = {
            "title": derive_title(stem),
            "filename": os.path.basename(existing_path),
            "url": pdf_url,
            "page_last_seen": page,
            "downloaded_at": _mtime_iso(existing_path),
            "downloaded_at_estimated": True,
            "converted_at": None,
            "last_verified_at": _now_iso(),
        }
    else:
        manifest[doc_id]["page_last_seen"] = page
        manifest[doc_id]["last_verified_at"] = _now_iso()
        manifest[doc_id].pop("vanished_at", None)


def record_converted(manifest, doc_id):
    if doc_id in manifest:
        manifest[doc_id]["converted_at"] = _now_iso()


def move_to_vanished(output_dir, download_dir, md_dir, manifest, seen_doc_ids, log):
    """Any manifest entry that was downloaded but whose document didn't
    turn up anywhere in this pass's full walk of the site is, as far as we
    can tell, no longer listed there. Its files move out of download/ and
    md/ into a sibling vanished/ folder — out of the way of normal use,
    but not deleted — and the manifest entry is kept and stamped with
    vanished_at instead of removed, so there's a record that the document
    existed and when it stopped showing up. If it reappears later,
    _record_seen/_record_downloaded clear vanished_at again.

    Only ever called after a genuinely complete page walk (run_actualize) —
    never after a partial/ranged download, where "not seen" would just
    mean "outside the requested range", not "gone"."""
    vanished_dir = os.path.join(output_dir, "vanished")
    moved = []
    for doc_id, entry in manifest.items():
        if entry.get("vanished_at") or doc_id in seen_doc_ids:
            continue
        filename = entry.get("filename")
        if not filename:
            continue
        moved_any = False
        src_pdf = os.path.join(download_dir, filename)
        if os.path.exists(src_pdf):
            os.makedirs(vanished_dir, exist_ok=True)
            os.replace(src_pdf, os.path.join(vanished_dir, filename))
            moved_any = True
        stem = os.path.splitext(filename)[0]
        src_md = os.path.join(md_dir, stem + ".md")
        if os.path.exists(src_md):
            os.makedirs(vanished_dir, exist_ok=True)
            os.replace(src_md, os.path.join(vanished_dir, stem + ".md"))
            moved_any = True
        if moved_any:
            entry["vanished_at"] = _now_iso()
            moved.append((doc_id, entry.get("title") or filename))

    if moved:
        log(f"Пропавшие документы (не найдены на сайте при полном обходе): {len(moved)}. "
            f"Перенесены в vanished/, запись в manifest.json сохранена:")
        for doc_id, title in moved:
            log(f"  [vanished] {_tag(doc_id)} {title}")
    return moved


def _close_extra_tab(driver, main_handle, extra_handle):
    if extra_handle and extra_handle in driver.window_handles:
        try:
            driver.switch_to.window(extra_handle)
            driver.close()
        except Exception:
            pass
    driver.switch_to.window(main_handle)


def _attempt_download(driver, main_handle, pdf_url, filename, download_dir, log, stop_event):
    before_files = set(glob.glob(os.path.join(download_dir, "*")))
    before_handles = set(driver.window_handles)

    driver.execute_script("window.open(arguments[0], '_blank');", pdf_url)

    extra_handle = None
    for _ in range(10):
        time.sleep(0.3)
        diff = set(driver.window_handles) - before_handles
        if diff:
            extra_handle = diff.pop()
            break

    new_file = None
    waited = 0
    while waited < PER_DOC_ATTEMPT_WAIT:
        if stop_event.is_set():
            _close_extra_tab(driver, main_handle, extra_handle)
            raise Stopped()
        time.sleep(1)
        waited += 1

        after_files = set(glob.glob(os.path.join(download_dir, "*")))
        candidates = [
            f
            for f in (after_files - before_files)
            if not f.endswith(".crdownload") and not f.endswith(".tmp")
        ]
        if candidates:
            candidate = candidates[0]
            try:
                size1 = os.path.getsize(candidate)
                time.sleep(1)
                size2 = os.path.getsize(candidate)
            except FileNotFoundError:
                continue
            if size1 == size2 and size1 > 0:
                new_file = candidate
                break

        if waited % 15 == 0:
            log(f"...жду скачивание {filename} ({waited}с)")

    _close_extra_tab(driver, main_handle, extra_handle)
    return new_file


def _tag(doc_id):
    """Short, visually distinct id tag for log lines — the first 8 hex
    characters of the site's own doc_id (the GUID segment of the PDF's
    URL, e.g. .../PdfDocument/<case_id>/THIS_PART/filename.pdf), not
    anything derived or invented here."""
    return f"#{doc_id[:8]}"


def download_one(driver, main_handle, pdf_url, download_dir, log, stop_event, manifest=None, page=None) -> str:
    doc_id, filename = doc_id_and_name(pdf_url)
    if manifest is not None:
        existing, mismatch = _verify_or_locate(download_dir, doc_id, filename, manifest)
        if mismatch:
            log(f"  [манифест] {_tag(doc_id)} записан как скачанный, но файла нет на диске — перекачиваю: {filename}")
    else:
        existing, mismatch = _existing_path(download_dir, doc_id, filename), False
    if existing:
        if manifest is not None:
            _record_seen(manifest, doc_id, existing, pdf_url, page)
        log(f"  [skipped] {_tag(doc_id)} {pdf_url}")
        return "skipped"

    target_name = build_filename(doc_id, filename)
    target_path = os.path.join(download_dir, target_name)

    new_file = None
    for attempt in range(1, PER_DOC_MAX_ATTEMPTS + 1):
        new_file = _attempt_download(driver, main_handle, pdf_url, filename, download_dir, log, stop_event)
        if new_file:
            break
        log(f"Попытка {attempt}/{PER_DOC_MAX_ATTEMPTS} не удалась, пробую снова: {_tag(doc_id)} {filename}")
        _sleep_checking_stop(3, stop_event)

    if not new_file:
        log(f"  [failed] {_tag(doc_id)} Не удалось скачать после {PER_DOC_MAX_ATTEMPTS} попыток: {pdf_url}")
        return "failed"

    os.replace(new_file, target_path)
    if manifest is not None:
        _record_downloaded(manifest, doc_id, target_name, pdf_url, page)
    log(f"  [downloaded] {_tag(doc_id)} {pdf_url}")
    return "downloaded"


def click_next_page(driver, expected_page, log, stop_event):
    next_btn = driver.find_element(
        By.CSS_SELECTOR,
        "#chrono_ed_content .js-chrono-pagination-pager-item--arrow.next",
    )
    next_btn.click()

    waited = 0
    max_wait = 60
    while waited < max_wait:
        if stop_event.is_set():
            raise Stopped()
        time.sleep(1)
        waited += 1
        if get_current_page(driver) == expected_page:
            return
        if waited % 20 == 0:
            log(f"...жду переход на страницу {expected_page} ({waited}с)")
    raise BlockedError(f"Не удалось перейти на страницу {expected_page}")


CASE_INFO_FILENAME = "info.txt"

# Column order/classes are a stable structural thing on kad.arbitr.ru (the
# generic "who's on which side" slots), but the header label text itself
# depends on the case type — "Кредиторы (заявители)"/"Должники" for a
# bankruptcy case, "Истец"/"Ответчик" for others — so the label is read
# from the page's own <thead> rather than hardcoded, and this works for
# any case type, not just bankruptcy ones.
_PARTICIPANT_COLUMN_CLASSES = ["plaintiffs", "defendants", "third", "others"]


def _text_content(el):
    return (el.get_attribute("textContent") or "").strip()


def get_case_info(driver):
    """Scrape the case header/overview/participants block from the main
    case page (the default "Card" view — this must be called before
    open_electronic_case() switches the tab over to 'Электронное дело',
    which replaces this content). Uses textContent rather than Selenium's
    .text, since some of this (e.g. the case category heading) is a
    CSS-hidden element whose .text would otherwise come back empty."""
    case_numbers = [
        _text_content(el) for el in driver.find_elements(By.CSS_SELECTOR, "#b-case-header .js-case-header-case_num")
    ]

    category_els = driver.find_elements(By.CSS_SELECTOR, "#case-category")
    case_category = _text_content(category_els[0]) if category_els else ""

    duration_els = driver.find_elements(By.CSS_SELECTOR, ".b-case-overview .case-dur")
    case_duration = _text_content(duration_els[0]) if duration_els else ""

    date_els = driver.find_elements(By.CSS_SELECTOR, ".b-case-overview .case-date a")
    case_start_date = _text_content(date_els[0]) if date_els else ""

    header_label_by_class = {}
    for td in driver.find_elements(By.CSS_SELECTOR, "#gr_case_partps thead td"):
        classes = (td.get_attribute("class") or "").split()
        for cls in _PARTICIPANT_COLUMN_CLASSES:
            if cls in classes:
                header_label_by_class[cls] = _text_content(td)

    participants = []
    for cls in _PARTICIPANT_COLUMN_CLASSES:
        label = header_label_by_class.get(cls, cls)
        items = []
        for li in driver.find_elements(By.CSS_SELECTOR, f"#gr_case_partps tbody td.{cls} ul li"):
            links = li.find_elements(By.CSS_SELECTOR, "a")
            name = _text_content(links[0]) if links else _text_content(li)
            addr_els = li.find_elements(By.CSS_SELECTOR, ".js-rolloverHtml")
            address = _text_content(addr_els[0]) if addr_els else ""
            items.append({"name": name, "address": address})
        participants.append((label, items))

    return {
        "case_numbers": case_numbers,
        "case_category": case_category,
        "case_duration": case_duration,
        "case_start_date": case_start_date,
        "participants": participants,
    }


def format_case_info_text(case_url, info):
    lines = []
    if info.get("case_category"):
        lines.append(info["case_category"])
        lines.append("")
    if info.get("case_numbers"):
        lines.extend(info["case_numbers"])
        lines.append("")

    lines.append(f"Ссылка: {case_url}")
    if info.get("case_start_date"):
        lines.append(f"Начато: {info['case_start_date']}")
    if info.get("case_duration"):
        lines.append(f"Длится (на момент сбора данных): {info['case_duration']}")
    lines.append("")

    for label, items in info.get("participants", []):
        lines.append(f"{label} ({len(items)}):")
        if not items:
            lines.append("  —")
        for item in items:
            if item.get("address"):
                lines.append(f"  - {item['name']} ({item['address']})")
            else:
                lines.append(f"  - {item['name']}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write_case_info_file(output_dir, case_url, info):
    path = os.path.join(output_dir, CASE_INFO_FILENAME)
    with open(path, "w", encoding="utf-8") as f:
        f.write(format_case_info_text(case_url, info))


def run_discover(case_url, headless, output_dir, log, stop_event, status):
    """Find out how many pages 'Электронное дело' has, and — if output_dir
    is given — also scrape the case header/participants info from the
    default case page and save it to CASE_INFO_FILENAME right in that
    folder. get_case_info() runs before open_electronic_case() switches
    the page over to the document-list tab, since that's a different view
    that replaces the participants block in the DOM."""
    driver = make_driver(headless)
    case_info = None
    try:
        driver.get(case_url)
        time.sleep(5)
        _wait_for_case_page(driver, case_url, log, stop_event)
        case_info = get_case_info(driver)
        open_electronic_case(driver, case_url, log, stop_event)
        total_pages = get_total_pages(driver)
    except Stopped:
        log("Разведка остановлена пользователем.")
        return "stopped"
    finally:
        driver.quit()

    status["total_pages"] = total_pages
    status["discovered_total_pages"] = total_pages
    log(f"Разведка: всего страниц в 'Электронное дело': {total_pages}")

    if output_dir and case_info is not None:
        try:
            os.makedirs(output_dir, exist_ok=True)
            write_case_info_file(output_dir, case_url, case_info)
            log(f"Информация о деле сохранена: {CASE_INFO_FILENAME}")
        except OSError as e:
            log(f"Не удалось сохранить информацию о деле: {e}")
    return "done"


def _recheck_first_page(driver, case_url, download_dir, log, stop_event, inter_doc_delay, manifest, status, seen_doc_ids=None):
    """The case is live and keeps receiving new filings, which always land
    on page 1 — a long walk through every page can easily take long enough
    for a new document to appear there after it was already passed. One
    extra pass over page 1 at the very end catches that.

    If seen_doc_ids is passed (the running set from a full actualize walk),
    every doc_id found here is added to it too, so a document that only
    reappeared on page 1 during this very last pass still counts as "seen"
    for vanished-detection and doesn't get wrongly moved to vanished/.

    Page 1 was already walked once as part of the main crawl, so almost
    every link here comes back "skipped" (already have it) — that's just
    this pass re-confirming what the main walk already counted, not new
    information. Folding it into the shared downloaded/skipped/failed
    counters would double-count page 1 and make "пропущено" look inflated
    for no real reason, so only genuinely new outcomes (downloaded, or a
    newly-found document that failed) are added to status here."""
    log("Финальная проверка: возвращаюсь на страницу 1 — вдруг за время обхода появились новые документы.")
    open_electronic_case(driver, case_url, log, stop_event)
    main_handle = driver.current_window_handle
    links = collect_links(driver)
    new_count = 0
    for pdf_url in links:
        if seen_doc_ids is not None:
            doc_id, _ = doc_id_and_name(pdf_url)
            seen_doc_ids.add(doc_id)
        result = download_one(driver, main_handle, pdf_url, download_dir, log, stop_event, manifest=manifest, page=1)
        if result != "skipped":
            status[result] = status.get(result, 0) + 1
        if result == "downloaded":
            new_count += 1
        _sleep_checking_stop(inter_doc_delay, stop_event)
    log("Финальная проверка страницы 1: новых документов нет." if new_count == 0
        else f"Финальная проверка страницы 1: нашёл и скачал {new_count} новых документов.")


def _crawl(driver, case_url, download_dir, log, stop_event, status, inter_doc_delay, start_page, end_page, manifest):
    open_electronic_case(driver, case_url, log, stop_event)
    main_handle = driver.current_window_handle
    total_pages = get_total_pages(driver)
    status["total_pages"] = total_pages

    start_page = max(1, start_page or 1)
    end_page = min(end_page or total_pages, total_pages)
    if start_page > end_page:
        start_page, end_page = end_page, start_page
    log(f"Всего страниц в 'Электронное дело': {total_pages}. Скачиваю страницы {start_page}-{end_page}.")

    page = 1
    if start_page > 1:
        log(f"Пропускаю страницы 1-{start_page - 1}, перехожу к {start_page}...")
        while page < start_page:
            _sleep_checking_stop(inter_doc_delay, stop_event)
            click_next_page(driver, page + 1, log, stop_event)
            page += 1

    while True:
        status["page"] = page
        log(f"--- Страница {page}/{total_pages} ---")
        links = collect_links(driver)
        if not links and page == 1:
            raise BlockedError("На первой странице не найдено ни одного документа")
        log(f"  Найдено документов: {len(links)}")

        for pdf_url in links:
            result = download_one(driver, main_handle, pdf_url, download_dir, log, stop_event, manifest=manifest, page=page)
            status[result] = status.get(result, 0) + 1
            _sleep_checking_stop(inter_doc_delay, stop_event)

        if page >= end_page:
            break
        click_next_page(driver, page + 1, log, stop_event)
        page += 1

    return total_pages, end_page


def run(case_url, download_dir, output_dir, headless, log, stop_event, status, rate_per_min=MAX_RATE_PER_MIN,
        start_page=1, end_page=None):
    """Download documents from a case's 'Электронное дело' tab.

    rate_per_min caps how many PDF requests are issued per minute (clamped
    to [MIN_RATE_PER_MIN, MAX_RATE_PER_MIN]) — a manual dial on top of the
    automatic block-cooldown backoff below. start_page/end_page restrict the
    crawl to a page range (both inclusive); end_page=None means "to the
    last page". Setting start_page == end_page downloads just that page.

    When the crawl reaches the last page (a genuinely full sweep, not a
    narrow custom range), a final pass re-checks page 1 for documents that
    were filed after the walk had already moved past it.

    Returns "done" or "stopped". Raises on an unrecoverable error (after
    exhausting the block-cooldown backoff schedule).
    """
    os.makedirs(download_dir, exist_ok=True)
    md_dir = os.path.join(output_dir, "md")
    inter_doc_delay = _inter_doc_delay(rate_per_min)
    log(f"Темп: {min(max(rate_per_min, MIN_RATE_PER_MIN), MAX_RATE_PER_MIN):.1f} док/мин "
        f"(пауза между документами ~{inter_doc_delay:.1f}с).")

    attempts = [0] + BLOCK_BACKOFF_SCHEDULE
    last_error = None
    for i, wait_before in enumerate(attempts):
        if wait_before:
            log(f"Похоже на блокировку/cooldown сайта. Жду {wait_before // 60} мин перед повтором...")
            _sleep_checking_stop(wait_before, stop_event)

        manifest = load_manifest(output_dir, download_dir, md_dir)
        driver = make_driver(headless)
        try:
            _set_download_dir(driver, download_dir)
            total_pages, reached_end_page = _crawl(
                driver, case_url, download_dir, log, stop_event, status, inter_doc_delay, start_page, end_page, manifest
            )
            save_manifest(output_dir, manifest)

            if reached_end_page >= total_pages:
                try:
                    _recheck_first_page(driver, case_url, download_dir, log, stop_event, inter_doc_delay, manifest, status)
                except Stopped:
                    save_manifest(output_dir, manifest)
                    raise
                except Exception as e:
                    log(f"Финальная проверка страницы 1 не удалась (не критично): {e}")
                save_manifest(output_dir, manifest)

            log("Скачивание завершено.")
            return "done"
        except Stopped:
            save_manifest(output_dir, manifest)
            log("Остановлено пользователем.")
            return "stopped"
        except BlockedError as e:
            save_manifest(output_dir, manifest)
            last_error = e
            log(f"Блокировка: {e}")
            continue
        finally:
            driver.quit()

    raise RuntimeError(f"Не удалось обойти блокировку сайта после всех попыток: {last_error}")


def run_actualize(case_url, output_dir, headless, log, stop_event, status, download_missing, rate_per_min=MAX_RATE_PER_MIN):
    """Walk every page once, comparing the site's live document list against
    what's already collected. Each link is checked against the manifest by
    doc_id first (the authoritative record of what was actually downloaded
    and under what name), falling back to a plain filename-pattern guess
    only for documents the manifest doesn't know about at all yet — see
    _verify_or_locate. If download_missing is set, missing documents are
    fetched right away as they're found, in the same pass, instead of a
    separate second pass.

    Page-to-page navigation is always paced at CHECK_PAGE_DELAY at minimum
    (never skipped, even while actually downloading) — walking through many
    consecutive "nothing missing" pages with no delay at all is exactly the
    kind of unthrottled burst that got an earlier session hard-blocked.

    Ends with the same final page-1 re-check as a full download, but only
    when download_missing is set (a check-only pass has nothing to catch up
    on page 1 with anyway), followed unconditionally by vanished-detection:
    any manifest entry whose doc_id never turned up anywhere in this full
    walk gets its files moved to vanished/ (manifest entry kept, stamped
    with vanished_at, never deleted — see move_to_vanished).

    Returns "done" or "stopped". Raises on an unrecoverable error.
    """
    download_dir = os.path.join(output_dir, "download")
    md_dir = os.path.join(output_dir, "md")
    os.makedirs(download_dir, exist_ok=True)
    manifest = load_manifest(output_dir, download_dir, md_dir)

    doc_delay = _inter_doc_delay(rate_per_min)
    log(f"Актуализация{' с докачкой' if download_missing else ' (только проверка)'}. "
        f"Темп обхода страниц: 1/{CHECK_PAGE_DELAY:.0f}с."
        + (f" Темп докачки: {doc_delay:.1f}с/документ." if download_missing else ""))

    driver = make_driver(headless)
    try:
        if download_missing:
            _set_download_dir(driver, download_dir)
        open_electronic_case(driver, case_url, log, stop_event)
        main_handle = driver.current_window_handle
        total_pages = get_total_pages(driver)
        status["total_pages"] = total_pages

        seen_doc_ids = set()
        missing_pages = []
        page = 1
        while True:
            status["page"] = page
            log(f"--- Страница {page}/{total_pages} ---")
            links = collect_links(driver)

            missing_on_page = 0
            for pdf_url in links:
                doc_id, filename = doc_id_and_name(pdf_url)
                seen_doc_ids.add(doc_id)
                existing, mismatch = _verify_or_locate(download_dir, doc_id, filename, manifest)
                if mismatch:
                    log(f"  [манифест] {_tag(doc_id)} записан как скачанный, но файла нет на диске: {filename}")

                if existing:
                    _record_seen(manifest, doc_id, existing, pdf_url, page)
                    continue

                if not download_missing:
                    missing_on_page += 1
                    log(f"  [missing] {_tag(doc_id)} {pdf_url}")
                    continue

                result = download_one(driver, main_handle, pdf_url, download_dir, log, stop_event, manifest=manifest, page=page)
                status[result] = status.get(result, 0) + 1
                if result != "downloaded":
                    missing_on_page += 1
                _sleep_checking_stop(doc_delay, stop_event)

            if missing_on_page:
                missing_pages.append({"page": page, "missing": missing_on_page})
            save_manifest(output_dir, manifest)

            if page >= total_pages:
                break
            _sleep_checking_stop(CHECK_PAGE_DELAY, stop_event)
            click_next_page(driver, page + 1, log, stop_event)
            page += 1

        if download_missing:
            try:
                _recheck_first_page(driver, case_url, download_dir, log, stop_event, doc_delay, manifest, status, seen_doc_ids)
            except Exception as e:
                log(f"Финальная проверка страницы 1 не удалась (не критично): {e}")

        move_to_vanished(output_dir, download_dir, md_dir, manifest, seen_doc_ids, log)
        save_manifest(output_dir, manifest)
    except Stopped:
        save_manifest(output_dir, manifest)
        log("Актуализация остановлена пользователем.")
        return "stopped"
    finally:
        driver.quit()

    diff = sum(m["missing"] for m in missing_pages)
    expected = len(seen_doc_ids)
    actual = expected - diff
    status["expected_docs"] = expected
    status["actual_docs"] = actual
    status["missing_pages"] = missing_pages
    if diff == 0:
        log(f"Актуализация завершена: на сайте {expected} документов, в папке {actual} — совпадает.")
    else:
        pages_str = ", ".join(f"{m['page']} (-{m['missing']})" for m in missing_pages)
        log(f"Актуализация завершена: на сайте {expected} документов, в папке {actual} — не хватает {diff}. "
            f"Страницы с пропусками: {pages_str or 'не определены'}.")
    return "done"
