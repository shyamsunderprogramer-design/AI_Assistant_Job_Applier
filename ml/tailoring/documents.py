"""Write the draft as DOCX and PDF, render every page, check them, fix, and check again.

The DOCX is rendered by Microsoft Word when it is installed -- the document a
recruiter opens is the one inspected -- and the PDF by its own writer; both
are turned into page images for the preview. Each page is checked for:

  clipped text        a line running past the page edge or right margin
  overlapping text    two lines drawn over each other
  broken bullets      a bullet with nothing after it, or a glyph that did not render
  spacing             a large empty band in the middle of a page
  page breaks         a heading left alone at the foot of a page; a nearly empty last page
  missing sections    a heading of the draft that is not in the document

and for content: every line of the draft is in the document, and every
employer and date of the original resume survives.

A page break that can be fixed is fixed -- the draft is fitted to one page
fewer and rendered again, up to twice -- and only then are the downloads
offered. Anything left is reported, never hidden.
"""

from __future__ import annotations

import re
import shutil
import statistics
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

WORD_DIR = Path.home() / "Library" / "Containers" / "com.microsoft.Word" / "Data" / "tailoring"
SHARED_DIR = Path(__file__).resolve().parents[2] / "data" / "tailoring" / "word"
PAUSE = SHARED_DIR.parent / "word-paused"
BULLETS = "•·▪◦-–"


@dataclass
class Check:
    file: str            # docx | pdf
    page: int | None
    kind: str
    detail: str
    severity: str = "warning"     # warning | problem


@dataclass
class Rendered:
    docx: Path
    pdf: Path
    docx_pdf: Path | None                  # the DOCX as Word laid it out
    pages: dict[str, list[str]] = field(default_factory=dict)   # file -> page image paths
    checks: list[Check] = field(default_factory=list)
    attempts: int = 1
    word: bool = False

    @property
    def ok(self) -> bool:
        return not any(c.severity == "problem" for c in self.checks)

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("docx", "pdf", "docx_pdf"):
            d[k] = str(d[k]) if d[k] else None
        return d


# -- rendering -----------------------------------------------------------------------

def word_to_pdf(docx: Path, out: Path) -> bool:
    """Lay the DOCX out with Microsoft Word and save it as PDF. False if Word cannot.

    Word is sandboxed. It works freely in its own folder, which only a
    terminal may write to; a background service (the web app) uses
    data/tailoring/word, which Word may open once the person has granted it
    access to that folder (Word asks once, with its own "Grant Access" box).
    Until then Word waits on that box, so a timeout pauses Word for an hour
    rather than holding every run.

    Off unless `tailoring.use_word` is true: without that one grant, Word
    bounced in the Dock after every tailoring asking for file access, and the
    PDF page checks already cover the layout.
    """
    if not use_word() or not Path("/Applications/Microsoft Word.app").exists():
        return False
    for folder in (WORD_DIR, SHARED_DIR):
        if folder == SHARED_DIR and _paused():
            return False
        stem = uuid.uuid4().hex[:10]
        src, dst = folder / f"{stem}.docx", folder / f"{stem}.pdf"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            shutil.copy2(docx, src)
        except OSError:
            continue
        try:
            done = _word_save_pdf(src, dst)
            if done is None and folder == SHARED_DIR:
                PAUSE.parent.mkdir(parents=True, exist_ok=True)
                PAUSE.write_text(str(time.time()))
            if dst.exists():
                shutil.move(str(dst), str(out))
                return True
        finally:
            for f in (src, dst):
                try:
                    f.unlink(missing_ok=True)
                except OSError:
                    pass
    return False


def use_word() -> bool:
    """Whether the person has switched Word's page checks on (config: tailoring.use_word)."""
    try:
        from backend.config.loader import load_config
        return bool(load_config().get("tailoring.use_word", False))
    except Exception:
        return False


def _word_save_pdf(src: Path, dst: Path) -> bool | None:
    """Word opens src and saves it as dst. None when Word did not answer in time."""
    script = f'''
tell application "Microsoft Word"
  with timeout of 60 seconds
    set doc to open file name (POSIX file "{src}" as text)
    save as doc file name (POSIX file "{dst}" as text) file format format PDF
    close doc saving no
  end timeout
end tell'''
    try:
        done = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        return None
    if "-1712" in done.stderr:            # AppleEvent timed out: Word is waiting on a box
        return None
    return dst.exists()


def _paused() -> bool:
    try:
        return time.time() - float(PAUSE.read_text()) < 3600
    except (OSError, ValueError):
        return False


def word_ready() -> bool:
    """Whether Word is installed and its last try did not time out."""
    return Path("/Applications/Microsoft Word.app").exists() and not _paused()


def page_images(pdf: Path, folder: Path, prefix: str) -> list[str]:
    import pypdfium2 as pdfium
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    doc = pdfium.PdfDocument(str(pdf))
    try:
        for i in range(len(doc)):
            path = folder / f"{prefix}-page{i + 1}.png"
            page = doc[i]
            page.render(scale=1.6).to_pil().save(path)
            page.close()
            paths.append(str(path))
    finally:
        doc.close()
    return paths


# -- checks ---------------------------------------------------------------------------

def _lines(pdf: Path) -> list[tuple[int, float, float, float, float, float, str]]:
    """(page, left, bottom, right, top, page width, text) for every text line."""
    import pypdfium2 as pdfium
    out = []
    doc = pdfium.PdfDocument(str(pdf))
    try:
        for n in range(len(doc)):
            page = doc[n]
            width, height = page.get_size()
            tp = page.get_textpage()
            for i in range(tp.count_rects()):
                l, b, r, t = tp.get_rect(i)
                text = tp.get_text_bounded(l, b, r, t).strip()
                if text:
                    out.append((n + 1, l, b, r, t, width, text))
            out.append((n + 1, 0, height, 0, height, width, ""))    # page marker: its height
            tp.close()
            page.close()
    finally:
        doc.close()
    return out


def visual_checks(pdf: Path, file: str, headings: list[str]) -> list[Check]:
    checks: list[Check] = []
    rows = _lines(pdf)
    pages = sorted({r[0] for r in rows})
    for p in pages:
        lines = [r for r in rows if r[0] == p and r[6]]
        height = next(r[2] for r in rows if r[0] == p and not r[6])
        width = lines[0][5] if lines else 612
        for _, l, b, r, t, _, text in lines:
            if l < 0 or r > width - 18 or b < 0 or t > height:
                checks.append(Check(file, p, "clipped text", f"“{text[:50]}” runs past the page margin.", "problem"))
        for i, a in enumerate(lines):
            for c in lines[i + 1:]:
                ox = min(a[3], c[3]) - max(a[1], c[1])
                oy = min(a[4], c[4]) - max(a[2], c[2])
                if ox > 5 and oy > 0.5 * min(a[4] - a[2], c[4] - c[2]) and a[6] != c[6]:
                    checks.append(Check(file, p, "overlapping text",
                                        f"“{a[6][:30]}” overlaps “{c[6][:30]}”.", "problem"))
        for _, l, b, r, t, _, text in lines:
            if "\x7f" in text or "�" in text:
                checks.append(Check(file, p, "broken bullet", f"A character did not render near "
                                                               f"“{text[:40]}”.", "problem"))
            elif text in BULLETS:
                # The bullet comes back as its own piece of text; it is broken
                # only if nothing follows it on the same line.
                follows = any(o[6] and o[6] not in BULLETS and o[1] >= r - 1
                              and min(o[4], t) - max(o[2], b) > 0.4 * (t - b) for o in lines)
                if not follows:
                    checks.append(Check(file, p, "broken bullet", "A bullet with no text after it.", "problem"))
        tops = sorted((r[4] for r in lines), reverse=True)
        gaps = [a - b for a, b in zip(tops, tops[1:])]
        if len(gaps) > 6:
            usual = statistics.median(gaps)
            for g, top in zip(gaps, tops):
                if g > max(8 * usual, 0.22 * height) and top > 0.15 * height:
                    checks.append(Check(file, p, "spacing", "A large empty band in the middle of the page."))
        if lines:
            lowest = min(lines, key=lambda r: r[2])
            if p != pages[-1] and lowest[6].strip().upper() in {h.upper() for h in headings}:
                checks.append(Check(file, p, "page break", f"The heading “{lowest[6]}” is left alone at the "
                                                           f"foot of the page.", "problem"))
        if p == pages[-1] and len(pages) > 1 and len(lines) < 4:
            checks.append(Check(file, p, "page break", f"The last page holds only {len(lines)} line(s).",
                                "problem"))
    all_text = _flat(" ".join(r[6] for r in rows))
    for h in headings:
        if _flat(h) not in all_text:
            checks.append(Check(file, None, "missing section", f"The section “{h}” is not in the document.",
                                "problem"))
    return checks


def _flat(text: str) -> str:
    return re.sub(r"[^a-z0-9%+#]+", "", (text or "").lower())


def content_checks(pdf: Path, file: str, blocks: list[tuple[str, str]], model=None) -> list[Check]:
    """Every line of the draft is in the document; every employer and date of the original survives."""
    words = re.findall(r"[a-z0-9%+#]+", " ".join(r[6] for r in _lines(pdf)).lower())
    text, have = "".join(words), set(words)
    checks = []

    def present(line: str) -> bool:
        # By words, not characters: a PDF reader may split or repeat a glyph
        # ("Engineering: A Automated"), which is not a missing line.
        need = re.findall(r"[a-z0-9%+#]+", line.lower())
        return not need or sum(w in have for w in need) / len(need) >= 0.9

    missing = [t for k, t in blocks if k in ("bullet", "text", "skill", "role") and not present(t)]
    if missing:
        checks.append(Check(file, None, "content", f"{len(missing)} line(s) of the draft are not in the "
                                                   f"document, e.g. “{missing[0][:60]}”.", "problem"))
    if model is not None:
        for e in model.entries:
            for part in (e.employer, e.dates):
                if part and _flat(part) not in text:
                    checks.append(Check(file, None, "facts", f"“{part}” from the original resume is "
                                                             f"missing.", "problem"))
    return checks


# -- the whole step ---------------------------------------------------------------------

def produce(draft, folder: Path, name: str = "tailored-resume", model=None, jd_text: str = "",
            max_pages: int = 2, attempts: int = 3) -> Rendered:
    from ml.resume.pdf import write_pdf
    from ml.resume.writer import plan_document, write_tailored_resume

    folder.mkdir(parents=True, exist_ok=True)
    rendered = None
    pages = max_pages
    for attempt in range(1, attempts + 1):
        docx, pdf = folder / f"{name}.docx", folder / f"{name}.pdf"
        write_tailored_resume(draft.base, draft.result, docx, header_lines=draft.header)
        write_pdf(draft.base, draft.result, pdf, header_lines=draft.header)
        docx_pdf = folder / f"{name}.docx.pdf"
        word = word_to_pdf(docx, docx_pdf)
        blocks = plan_document(draft.base, draft.result, draft.header)
        headings = [t for k, t in blocks if k == "heading"]
        checks = visual_checks(pdf, "pdf", headings) + content_checks(pdf, "pdf", blocks, model)
        if word:
            checks += visual_checks(docx_pdf, "docx", headings) + content_checks(docx_pdf, "docx", blocks, model)
        else:
            checks.append(Check("docx", None, "render", "The DOCX was checked through its matching PDF, drawn "
                                                        "from the same content (Word's own page check is off "
                                                        "in Settings, or Word is not available).",
                                severity="note"))
        rendered = Rendered(docx, pdf, docx_pdf if word else None, {}, checks, attempt, word)
        fixable = [c for c in checks if c.kind == "page break"]
        if not fixable or attempt == attempts:
            break
        # Fix: fit one page fewer, so nothing is left dangling, and render again.
        from ml.resume.fit import fit_to_pages
        from ml.tailoring.draft import _pages
        current = max(1, len(__import__("pypdf").PdfReader(str(pdf)).pages))
        pages = max(1, min(pages, current) - (1 if any("last page" in c.detail for c in fixable) else 0))
        fit_to_pages(draft.base, draft.result, jd_text, max_pages=pages,
                     pages=lambda b, r: _pages(b, r, draft.header))
    rendered.pages["pdf"] = page_images(rendered.pdf, folder / "pages", "pdf")
    if rendered.docx_pdf:
        rendered.pages["docx"] = page_images(rendered.docx_pdf, folder / "pages", "docx")
    return rendered
