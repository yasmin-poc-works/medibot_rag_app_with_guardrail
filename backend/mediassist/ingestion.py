"""Document ingestion using Docling's structural parser and HybridChunker.

The production path uses the assignment's required Docling stack. A small
plain-text fallback keeps API tests independent from model downloads.
"""

import json
import re
from pathlib import Path

SOURCE_SUFFIXES = {".pdf", ".md"}
CACHE_VERSION = 1
# Top-level section headings in the source documents are enumerated
# ("B. Hypertension - Stage 2", "3. Exclusions Reference", "SOP 6 - ...",
# "Q5. ..."); their subsections ("Diagnostic criteria", "Monitoring") are not.
MAJOR_HEADING = re.compile(r"^(?:[A-Z]|\d+|Q\d+)\.\s|^SOP\s+\d+\b")


def ingest_documents(data_dir: Path, cache_file: Path | None = None) -> list[dict]:
    """Parse source files into hierarchical chunks carrying complete metadata.

    Docling's layout and table models take roughly a minute per PDF on CPU, so
    chunks are cached per file and only re-parsed when the file changes.
    """

    try:
        from docling.chunking import HybridChunker
    # Docling pulls in transformers at import time.  Some Windows/Python
    # environments can have a broken optional transformers installation even
    # though the package itself is present; keep tests and local fallback mode
    # independent of that optional production dependency.
    except (ImportError, OSError):
        return _fallback_chunks(data_dir)

    cache = _load_cache(cache_file)
    converter = None
    chunker = None
    chunks: list[dict] = []
    for path in sorted(data_dir.rglob("*")):
        if path.suffix.lower() not in SOURCE_SUFFIXES:
            continue
        key = path.relative_to(data_dir).as_posix()
        fingerprint = _fingerprint(path)
        cached = cache.get(key)
        if cached and cached.get("fingerprint") == fingerprint:
            chunks.extend(_attach_parent_sections(cached["chunks"]))
            continue
        try:
            if converter is None:
                converter = _build_converter()
                chunker = HybridChunker()
        except (ImportError, OSError):
            return _fallback_chunks(data_dir)
        print(f"[ingestion] Parsing {key} with Docling...", flush=True)
        document = converter.convert(str(path)).document
        file_chunks = [
            _metadata(path, index, chunker.serialize(chunk), getattr(chunk, "meta", None))
            for index, chunk in enumerate(chunker.chunk(document))
        ]
        # The cache keeps Docling's raw output; parent sections are attached on
        # every load so changing that rule never requires re-parsing.
        cache[key] = {"fingerprint": fingerprint, "chunks": file_chunks}
        chunks.extend(_attach_parent_sections(file_chunks))
        # Save after every file so an interrupted first run keeps its progress.
        _save_cache(cache_file, cache)
    return chunks


def _build_converter() -> object:
    """Create a Docling converter tuned for the digitally generated source PDFs."""

    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    # The source PDFs already contain a text layer, so OCR adds minutes of CPU
    # time without recovering any extra text.
    pdf_options = PdfPipelineOptions(do_ocr=False)
    return DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pdf_options)}
    )


def _attach_parent_sections(file_chunks: list[dict]) -> list[dict]:
    """Prefix subsection chunks with their enumerated parent section.

    Docling reports every PDF heading at the same level, so a chunk under
    "Diagnostic criteria" would not mention the condition it belongs to and
    could not be retrieved for "diagnostic criteria of Hypertension".
    """

    parent: str | None = None
    result: list[dict] = []
    for chunk in file_chunks:
        title = chunk["section_title"]
        if MAJOR_HEADING.match(title):
            parent = title
        elif parent and title != "Document":
            chunk = {
                **chunk,
                "text": f"{parent}\n{chunk['text']}",
                "section_title": f"{parent} > {title}",
            }
        result.append(chunk)
    return result


def _fingerprint(path: Path) -> list[int]:
    """Identify a file version by size and modification time."""

    stat = path.stat()
    return [stat.st_size, stat.st_mtime_ns]


def _load_cache(cache_file: Path | None) -> dict:
    """Read cached chunks, treating a missing, stale, or corrupt cache as empty."""

    if cache_file is None or not cache_file.exists():
        return {}
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if data.get("version") != CACHE_VERSION:
        return {}
    return data.get("files", {})


def _save_cache(cache_file: Path | None, cache: dict) -> None:
    """Persist cached chunks; a write failure only costs re-parsing next time."""

    if cache_file is None:
        return
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": CACHE_VERSION, "files": cache}
        cache_file.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        pass


def _metadata(path: Path, index: int, text: str, meta: object = None) -> dict:
    """Build the metadata schema required by the retrieval layer."""

    section = getattr(meta, "headings", None) or ["Document"]
    return {
        "text": text,
        "source_document": path.name,
        "section_title": str(section[-1]),
        "collection": path.parent.name,
        "chunk_type": "text",
        "chunk_index": index,
    }


def _fallback_chunks(data_dir: Path) -> list[dict]:
    """Create deterministic chunks for tests or local runs when Docling is unavailable."""

    chunks: list[dict] = []
    for path in sorted(data_dir.rglob("*")):
        if path.suffix.lower() == ".md":
            chunks.append(_metadata(path, 0, path.read_text(encoding="utf-8")))
        elif path.suffix.lower() == ".pdf":
            try:
                from pypdf import PdfReader

                reader = PdfReader(str(path))
                for index, page in enumerate(reader.pages):
                    text = page.extract_text() or ""
                    if text.strip():
                        chunks.append(_metadata(path, index, text))
            except (ImportError, OSError):
                # Markdown remains available even when optional PDF parsing is
                # not installed or a PDF cannot be decoded.
                continue
    return chunks
