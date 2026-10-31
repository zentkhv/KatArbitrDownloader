import os

from pypdf import PdfReader

from . import scraper


def _pdf_to_markdown(path):
    reader = PdfReader(path)
    stem = os.path.splitext(os.path.basename(path))[0]
    title = scraper.derive_title(stem)
    parts = [f"# {title}", ""]
    for page in reader.pages:
        text = (page.extract_text() or "").strip()
        if text:
            parts.append(text)
            parts.append("")
    return "\n".join(parts)


def run(download_dir, md_dir, output_dir, log, stop_event, status):
    """Convert every PDF in download_dir to a .md file in md_dir.

    Already-converted files (matched by stem) are skipped, so this is safe
    to re-run after an interruption. Also records a converted_at timestamp
    in manifest.json for whichever document each PDF turns out to be (by
    matching its on-disk filename), so the manifest reflects conversion
    status too, not just downloads. Returns "done" or "stopped".
    """
    os.makedirs(md_dir, exist_ok=True)

    manifest = scraper.load_manifest(output_dir, download_dir, md_dir)
    doc_id_by_filename = {entry.get("filename"): doc_id for doc_id, entry in manifest.items()}

    pdf_files = sorted(f for f in os.listdir(download_dir) if f.lower().endswith(".pdf"))
    status["md_total"] = len(pdf_files)
    log(f"Конвертация PDF -> MD: всего файлов {len(pdf_files)}")

    for i, filename in enumerate(pdf_files, start=1):
        if stop_event.is_set():
            scraper.save_manifest(output_dir, manifest)
            log("Конвертация остановлена пользователем.")
            return "stopped"

        status["md_processed"] = i
        stem = os.path.splitext(filename)[0]
        target_path = os.path.join(md_dir, stem + ".md")
        if os.path.exists(target_path):
            status["md_skipped"] = status.get("md_skipped", 0) + 1
            continue

        src_path = os.path.join(download_dir, filename)
        try:
            text = _pdf_to_markdown(src_path)
        except Exception as e:
            status["md_failed"] = status.get("md_failed", 0) + 1
            log(f"  [ошибка] {filename}: {e}")
            continue

        with open(target_path, "w", encoding="utf-8") as f:
            f.write(text)
        status["md_converted"] = status.get("md_converted", 0) + 1
        log(f"  [{i}/{len(pdf_files)}] сконвертирован: {filename}")

        doc_id = doc_id_by_filename.get(filename)
        if doc_id:
            scraper.record_converted(manifest, doc_id)

    scraper.save_manifest(output_dir, manifest)
    log("Конвертация завершена.")
    return "done"
