#!/usr/bin/env python3
"""
cxalloy_na_fix.py — Post-process CxAlloy TQ test-report PDFs so that
Text Input lines answered "NA" render with the grey N/A badge instead of
a green tick, with pass/fail/NA percentages and progress bars
recalculated. Photo/file link annotations are untouched.

Concatenated exports (many tests in one PDF) are fully supported: each
test is detected and processed independently — its own line
conversions, its own header percentages, its own progress bars. Tests
that need no change are left untouched apart from the footer note.

Because the report is a formal record, whenever anything is changed a
note is added to the footer of every page, right-aligned on the
existing footer baseline in the footer's own font, size and colour so
it reads as part of the template:

    "This CxAlloy report has been modified to meet CTA reporting
     requirements."

Usage:
    python3 cxalloy_na_fix.py input.pdf [-o output.pdf]
                              [--dry-run] [--workers N]

Architecture — a strict two-phase design, which is also what makes
parallelism safe:

  1. ANALYSE (read-only, parallelisable). Each test segment is scanned
     for status squares, NA-valued lines, header percentage spans and
     progress-bar tracks. Segments are independent. PyMuPDF documents
     are NOT thread-safe, so parallelism uses worker *processes*, each
     opening its own copy of the document and returning plain picklable
     findings.
  2. MUTATE (serial, main process). All edits are cheap overlays
     applied to the one authoritative document, guaranteeing
     link/annotation fidelity. Analysis dominates runtime, so this is
     where the speed-up lives.

Parallel analysis engages automatically for large concatenated exports
(>= PARALLEL_MIN_SEGMENTS tests and >= PARALLEL_MIN_PAGES pages) or
whenever --workers > 1 is given explicitly; for small files serial is
faster because process start-up outweighs the work.

Conservative rule: only GREEN (Passed) lines whose recorded value is
exactly NA / N/A are converted. Failed lines are never altered.
"""

import argparse
import io
import os
import re
import sys

import fitz  # PyMuPDF
from PIL import Image

# ---- CxAlloy style constants (sampled from genuine exports) ------------
GREEN = (0.004, 0.765, 0.055)          # tick square fill
GREY_NA = (0.502, 0.502, 0.502)        # N/A square fill
BAR_GREEN = (0.345, 0.788, 0.275)      # progress-bar passed segment
BAR_DARK = (0.4, 0.4, 0.4)             # progress-bar N/A segment
BAR_RED = (0.863, 0.275, 0.275)        # failed segment (only if failed>0)
BAR_TRACK = (0.847, 0.847, 0.855)      # pill track fill
SECTION_BAR = (0.333, 0.333, 0.333)    # dark section-header bars
CORNER_R = 1.5 / 12                    # badge corner radius (fraction)
NA_TOKENS = {"na", "n/a", "n.a.", "n.a"}
HEADER_Y, FOOTER_Y = 48, 744           # running header/footer exclusion

FOOTER_NOTE = ("This CxAlloy report has been modified to meet "
               "CTA reporting requirements.")

PCT_RE = re.compile(r"^\d+% Passed \| \d+% Failed \| \d+% N/A$")
LABEL_RE = re.compile(r"^#\d+$")

# Auto-parallelism thresholds (see module docstring)
PARALLEL_MIN_SEGMENTS = 3
PARALLEL_MIN_PAGES = 40


# ======================================================================
# Small utilities
# ======================================================================

def is_close(a, b, tol=0.02):
    return a is not None and b is not None and all(
        abs(x - y) <= tol for x, y in zip(a, b))


def _int_to_rgb(color_int):
    return tuple(((color_int >> s) & 255) / 255 for s in (16, 8, 0))


def page_text(page):
    """One text extraction serving everything: (spans, lines).

    spans: raw span dicts.  lines: [(y0, x0, joined_text)] sorted by y.
    """
    spans, lines = [], []
    for b in page.get_text("dict")["blocks"]:
        for ln in b.get("lines", []):
            spans.extend(ln["spans"])
            txt = "".join(s["text"] for s in ln["spans"]).strip()
            if txt:
                lines.append((ln["bbox"][1], ln["bbox"][0], txt))
    lines.sort()
    return spans, lines


def classify_drawings(drawings):
    """One vector-graphics pass serving everything.

    Returns (badge_squares, section_bar_tops, bar_tracks):
      badge_squares : [(Rect, fill, is_slice)] status squares sorted by
                      y. Rows split across a page break render their
                      badge as two clipped fragments (e.g. 10.9pt at the
                      bottom of one page + 1.1pt at the top of the
                      next), so heights from 1pt up are accepted and
                      fragments are flagged for merging downstream.
      section_bar_tops : [y0] of dark full-width section headers
      bar_tracks    : [Rect] light-grey pill progress-bar tracks
    """
    squares, bars, tracks = [], [], []
    for d in drawings:
        r, f = d["rect"], d.get("fill")
        if f is None:
            continue
        if (abs(r.width - 12) < 0.6 and 1.0 <= r.height <= 12.6
                and 40 <= r.x0 <= 70
                and (is_close(f, GREEN) or is_close(f, GREY_NA)
                     or (f[0] > 0.6 and f[1] < 0.4 and f[2] < 0.4))):
            squares.append((r, tuple(f), r.height < 11.4))
        elif is_close(f, SECTION_BAR) and r.width > 400:
            bars.append(r.y0)
        elif is_close(f, BAR_TRACK, 0.03) and 7 <= r.height <= 11 and r.width > 40:
            tracks.append(r)
    squares.sort(key=lambda t: t[0].y0)
    return squares, sorted(bars), tracks


def pct_string(passed, failed, na):
    """Integer percentages via largest-remainder so they sum to 100."""
    total = passed + failed + na
    if total == 0:
        return "0% Passed | 0% Failed | 0% N/A", (0, 0, 0)
    raw = [passed * 100 / total, failed * 100 / total, na * 100 / total]
    floors = [int(v) for v in raw]
    for _ in range(100 - sum(floors)):
        j = max(range(3), key=lambda k: raw[k] - floors[k])
        floors[j] += 1
        raw[j] = floors[j]
    p, f, n = floors
    return f"{p}% Passed | {f}% Failed | {n}% N/A", (p, f, n)


def na_glyph_png(doc):
    """Crop the white 'NA' glyph cell out of the shared icon sprite."""
    for pno in range(doc.page_count):
        for info in doc[pno].get_image_info(xrefs=True):
            if info["width"] == 192 and info["height"] == 128:
                img = doc.extract_image(info["xref"])
                base = Image.open(io.BytesIO(img["image"])).convert("RGB")
                if img.get("smask"):
                    m = doc.extract_image(img["smask"])
                    mask = Image.open(io.BytesIO(m["image"])).convert("L")
                    if mask.size != base.size:
                        mask = mask.resize(base.size)
                    base.putalpha(mask)
                else:
                    base = base.convert("RGBA")
                cell = base.crop((128, 0, 192, 64))  # row 1, col 3 = "NA"
                buf = io.BytesIO()
                cell.save(buf, format="PNG")
                return buf.getvalue()
    return None  # caller falls back to a drawn glyph


# ======================================================================
# Phase 1 — ANALYSE (read-only; safe to run in worker processes)
# ======================================================================

def find_test_segments(doc):
    """Split a (possibly concatenated) export into per-test page ranges.

    A test begins on any page carrying the summary percentage line
    ("NN% Passed | NN% Failed | NN% N/A"), which appears exactly once
    per test. Returns [(start, end)]; whole document if none found.
    """
    starts = []
    for pno in range(doc.page_count):
        spans, _ = page_text(doc[pno])
        if any(PCT_RE.match(s["text"].strip()) for s in spans):
            starts.append(pno)
    if not starts:
        return [(0, doc.page_count)]
    starts[0] = 0  # fold any preamble pages into the first test
    return [(s, starts[i + 1] if i + 1 < len(starts) else doc.page_count)
            for i, s in enumerate(starts)]


def analyze_segment(doc, start, end, index=0):
    """Read-only scan of one test. Returns plain picklable findings:

    { index, label, pages:(start,end),
      counts: {passed, failed, na},
      targets: [{fragments:[(page, rect4), ...], line}],  # ticks to convert
      header_hits: [{page, bbox4, origin2, size, color}],
      tracks: [(page, rect4)] }                   # progress-bar tracks
    """
    stream, header_hits, tracks = [], [], []
    label = f"Test {index + 1}"
    for pno in range(start, end):
        page = doc[pno]
        squares, bar_tops, page_tracks = classify_drawings(page.get_drawings())
        spans, lines = page_text(page)
        for rect, fill, is_slice in squares:
            stream.append((pno, rect.y0, "sq", (tuple(rect), fill, is_slice)))
        for y in bar_tops:
            stream.append((pno, y, "bar", None))
        for r in page_tracks:
            tracks.append((pno, tuple(r)))
        for s in spans:
            txt = s["text"].strip()
            if PCT_RE.match(txt):
                header_hits.append({"page": pno, "bbox": tuple(s["bbox"]),
                                    "origin": tuple(s["origin"]),
                                    "size": s["size"], "color": s["color"]})
            elif pno == start and s["size"] > 15 and LABEL_RE.match(txt):
                label = txt
        for (y, x, t) in lines:
            if HEADER_Y <= y <= FOOTER_Y:
                stream.append((pno, y, "txt", (x, t)))
    stream.sort(key=lambda e: (e[0], e[1]))

    # A row split across a page break yields two badge fragments: one at
    # the bottom of page p, one at the top of page p+1. Merge each such
    # pair into a single logical badge anchored on the first fragment,
    # so the census stays exact and the row's band spans the page break.
    sq_idx = [i for i, e in enumerate(stream) if e[2] == "sq"]
    merged, extra = set(), {}
    for k in range(1, len(sq_idx)):
        i0, i1 = sq_idx[k - 1], sq_idx[k]
        p0, _, _, (r0, f0, s0) = stream[i0]
        p1, _, _, (r1, f1, s1) = stream[i1]
        if (s0 and s1 and p1 == p0 + 1 and f1 == f0
                and r0[3] > FOOTER_Y - 60 and r1[1] < HEADER_Y + 65):
            merged.add(i1)
            extra.setdefault(i0, []).append((p1, r1))

    targets, counts = [], {"passed": 0, "failed": 0, "na": 0}
    for i in sq_idx:
        if i in merged:
            continue
        pno, _, _, (rect4, fill, _) = stream[i]
        if is_close(fill, GREEN):
            counts["passed"] += 1
        elif is_close(fill, GREY_NA):
            counts["na"] += 1
            continue
        else:
            counts["failed"] += 1
            continue                             # never touch failed lines
        # band = everything until the next badge or section bar; bands
        # may cross page breaks within the segment but never leave it
        end_j = len(stream)
        for j in range(i + 1, len(stream)):
            if stream[j][2] == "bar" or (stream[j][2] == "sq"
                                         and j not in merged):
                end_j = j
                break
        band = [e[3] for e in stream[i + 1:end_j] if e[2] == "txt"]
        line_no = next((t for (x, t) in band if t.isdigit() and x < 95), "?")
        if any(t.lower() in NA_TOKENS and 90 <= x <= 200 for (x, t) in band):
            targets.append({"fragments": [(pno, rect4)] + extra.get(i, []),
                            "line": line_no})

    return {"index": index, "label": label, "pages": (start, end),
            "counts": counts, "targets": targets,
            "header_hits": header_hits, "tracks": tracks}


# --- process-pool plumbing (each worker owns a private Document) --------
_WORKER_DOC = None


def _worker_init(pdf_bytes):
    global _WORKER_DOC
    _WORKER_DOC = fitz.open(stream=pdf_bytes, filetype="pdf")


def _worker_analyze(index, start, end):
    return analyze_segment(_WORKER_DOC, start, end, index)


def _analyze_all(doc, segments, workers=None):
    """Analyse every segment, in parallel worker processes when it pays."""
    cpu = os.cpu_count() or 1
    if workers is None:
        use = cpu if (len(segments) >= PARALLEL_MIN_SEGMENTS
                      and doc.page_count >= PARALLEL_MIN_PAGES) else 1
    else:
        use = max(1, workers)
    use = min(use, len(segments))
    if use <= 1:
        return [analyze_segment(doc, s, e, i)
                for i, (s, e) in enumerate(segments)]
    try:
        import concurrent.futures as cf
        import multiprocessing as mp
        data = doc.tobytes()                     # pre-mutation snapshot
        ctx = mp.get_context("spawn")            # fork-unsafe hosts (gunicorn)
        with cf.ProcessPoolExecutor(max_workers=use, mp_context=ctx,
                                    initializer=_worker_init,
                                    initargs=(data,)) as ex:
            futs = [ex.submit(_worker_analyze, i, s, e)
                    for i, (s, e) in enumerate(segments)]
            return [f.result() for f in futs]
    except Exception as exc:                     # pragma: no cover
        print(f"[warn] parallel analysis unavailable ({exc}); running serial",
              file=sys.stderr)
        return [analyze_segment(doc, s, e, i)
                for i, (s, e) in enumerate(segments)]


def plan_document(doc, workers=None):
    """ANALYSE + decide. Pure (no mutation). Returns one plan per test:

    { label, pages, before, after, header, lines, changed, _analysis }
    """
    segments = find_test_segments(doc)
    plans = []
    for a in _analyze_all(doc, segments, workers):
        c, n = a["counts"], len(a["targets"])
        after = ({"passed": c["passed"] - n, "failed": c["failed"],
                  "na": c["na"] + n} if n else dict(c))
        plans.append({
            "label": a["label"],
            "pages": [a["pages"][0] + 1, a["pages"][1]],   # 1-based incl.
            "before": dict(c), "after": after,
            "header": pct_string(**after)[0] if n else None,
            "lines": [t["line"] for t in a["targets"]],
            "changed": n > 0,
            "_analysis": a,
        })
    return plans


# ======================================================================
# Phase 2 — MUTATE (serial, main process only)
# ======================================================================

def _flip_badges(doc, targets, glyph_png):
    for t in targets:
        for pno, rect4 in t["fragments"]:
            page, rect = doc[pno], fitz.Rect(rect4)
            full = rect.height >= 11.4
            shape = page.new_shape()
            # white underlay wipes the green square incl. antialiased edge
            shape.draw_rect(rect + (-0.7, -0.7, 0.7, 0.7))
            shape.finish(fill=(1, 1, 1), color=None)
            # genuine-geometry grey badge on top; page-break fragments
            # stay square-cut, exactly as CxAlloy's own clipping renders
            shape.draw_rect(rect, radius=CORNER_R if full else None)
            shape.finish(fill=GREY_NA, color=None)
            shape.commit()
            if glyph_png and rect.height >= 8:
                page.insert_image(rect, stream=glyph_png,
                                  keep_proportion=True)
            elif not glyph_png and full:  # sprite missing: drawn fallback
                page.insert_textbox(rect + (0, 2.5, 0, 0), "NA",
                                    fontname="hebo", fontsize=6,
                                    color=(1, 1, 1), align=1)


def _rewrite_headers(doc, hits, new_string):
    """Redact each stale percentage span (text only) and write the new one."""
    by_page = {}
    for h in hits:
        by_page.setdefault(h["page"], []).append(h)
    for pno, page_hits in by_page.items():
        page = doc[pno]
        for h in page_hits:
            page.add_redact_annot(fitz.Rect(h["bbox"]) + (-1, -1, 1, 1))
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                              graphics=fitz.PDF_REDACT_LINE_ART_NONE)
        for h in page_hits:
            page.insert_text(h["origin"], new_string, fontname="helv",
                             fontsize=h["size"], color=_int_to_rgb(h["color"]))


def _repaint_tracks(doc, tracks, p_frac, f_frac):
    """Overlay every progress bar in a test with its new segment split."""
    for pno, rect4 in tracks:
        page, t = doc[pno], fitz.Rect(rect4)
        gx = t.x0 + t.width * p_frac
        rx = gx + t.width * f_frac
        shape = page.new_shape()
        if p_frac > 0:
            shape.draw_rect(fitz.Rect(t.x0, t.y0, gx, t.y1))
            shape.finish(fill=BAR_GREEN, color=None)
        if f_frac > 0:
            shape.draw_rect(fitz.Rect(gx, t.y0, rx, t.y1))
            shape.finish(fill=BAR_RED, color=None)
        if rx < t.x1 - 0.1:
            shape.draw_rect(fitz.Rect(rx, t.y0, t.x1, t.y1))
            shape.finish(fill=BAR_DARK, color=None)
        shape.commit()


def add_footer_note(doc, note=FOOTER_NOTE):
    """Right-align the modification note on each page's footer baseline.

    Uses the footer's own font size and colour so the note reads as part
    of the template. If a page's footer is unusually wide the note
    shrinks (never below 6pt) and, as a last resort, drops one line.
    """
    for pno in range(doc.page_count):
        page = doc[pno]
        spans, _ = page_text(page)
        foot = [s for s in spans if "Printed on" in s["text"]
                and s["bbox"][1] > page.rect.height * 0.85]
        if foot:
            f = foot[0]
            size, color = f["size"], _int_to_rgb(f["color"])
            y, left = f["origin"][1], f["bbox"][0]
            occupied = max(s["bbox"][2] for s in spans
                           if abs(s["origin"][1] - y) < 1)
        else:  # footerless page: sit where the footer would be
            size, color = 8.25, _int_to_rgb(0x333333)
            y, left, occupied = page.rect.height - 46.4, 48.2, 0
        right = page.rect.width - left
        fs = size
        while (fs > 6.0 and
               fitz.get_text_length(note, fontname="helv", fontsize=fs)
               > right - occupied - 12):
            fs -= 0.25
        w = fitz.get_text_length(note, fontname="helv", fontsize=fs)
        if right - w < occupied + 12:            # still colliding: own line
            fs = size
            w = fitz.get_text_length(note, fontname="helv", fontsize=fs)
            y = min(y + size + 1.5, page.rect.height - 6)
        page.insert_text((right - w, y), note, fontname="helv",
                         fontsize=fs, color=color)


def apply_plans(doc, plans):
    """Apply every changed test's plan; add the footer note if anything
    changed. Returns True if the document was modified."""
    glyph, changed = None, False
    for p in plans:
        if not p["changed"]:
            continue
        if glyph is None:
            glyph = na_glyph_png(doc) or False
        a, after = p["_analysis"], p["after"]
        total = sum(after.values()) or 1
        _flip_badges(doc, a["targets"], glyph or None)
        _rewrite_headers(doc, a["header_hits"], p["header"])
        _repaint_tracks(doc, a["tracks"],
                        after["passed"] / total, after["failed"] / total)
        changed = True
    if changed:
        add_footer_note(doc)
    return changed


def fix_document(doc, workers=None):
    """One-call API: plan + apply. Mutates `doc`; returns per-test
    summaries (without the internal analysis payload)."""
    plans = plan_document(doc, workers)
    apply_plans(doc, plans)
    return [{k: p[k] for k in ("label", "pages", "before", "after",
                               "header", "lines", "changed")} for p in plans]


# ======================================================================
# CLI
# ======================================================================

def main():
    ap = argparse.ArgumentParser(
        description="Convert NA-valued ticks to N/A badges in CxAlloy "
                    "report PDFs (single or concatenated multi-test).")
    ap.add_argument("input")
    ap.add_argument("-o", "--output", default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change, write nothing")
    ap.add_argument("--workers", type=int, default=None,
                    help="analysis worker processes (default: auto — "
                         "parallel only when the file is large enough "
                         "to benefit; 1 forces serial)")
    args = ap.parse_args()
    out = args.output or re.sub(r"\.pdf$", "", args.input,
                                flags=re.I) + "_corrected.pdf"

    doc = fitz.open(args.input)
    links_before = sum(len(doc[p].get_links()) for p in range(doc.page_count))
    plans = plan_document(doc, args.workers)

    print(f"Detected {len(plans)} test(s) across {doc.page_count} pages.")
    for p in plans:
        span = f"pages {p['pages'][0]}-{p['pages'][1]}"
        b = p["before"]
        if p["changed"]:
            print(f"  {p['label']:<7} {span:<13} convert {len(p['lines'])} "
                  f"line(s) ({', '.join(p['lines'])})  ->  {p['header']}")
        else:
            print(f"  {p['label']:<7} {span:<13} no change "
                  f"({b['passed']} passed / {b['failed']} failed / "
                  f"{b['na']} N/A)")
    total = sum(len(p["lines"]) for p in plans)
    print(f"Total lines to convert: {total}")

    if args.dry_run:
        return
    if not apply_plans(doc, plans):
        print("Nothing to change — output not written.")
        return

    doc.save(out, garbage=3, deflate=True)
    check = fitz.open(out)
    links_after = sum(len(check[p].get_links())
                      for p in range(check.page_count))
    print(f"Footer note added to all {doc.page_count} pages.")
    print(f"Link annotations: {links_before} before -> {links_after} after "
          + ("(OK)" if links_before == links_after else "(MISMATCH — check!)"))
    print(f"Written: {out}")


if __name__ == "__main__":
    main()
