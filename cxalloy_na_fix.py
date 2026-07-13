#!/usr/bin/env python3
"""
cxalloy_na_fix.py — Post-process a CxAlloy TQ test-report PDF so that
Text Input lines answered "NA" render with the grey N/A badge instead of
a green tick, and the header pass/fail/NA percentages + progress bars are
recalculated accordingly. Photo/file link annotations are untouched.

Usage:
    python3 cxalloy_na_fix.py input.pdf [-o output.pdf] [--dry-run]

How it works (CxAlloy TQ / Prince 14 export anatomy):
  * Each scored line has a 12x12pt rounded square (vector) at x~55:
      green (0.004,0.765,0.055) = Passed, grey (0.502,...) = N/A.
  * The tick/X/NA glyphs are white glyphs in a shared 192x128 sprite image
    (3 cols x 2 rows); the placement x-offset selects the visible cell.
  * Header shows "NN% Passed | NN% Failed | NN% N/A" (ArialMT 8.25, #666)
    plus pill progress bars: grey track + green 're' + dark-grey 're'.
The script overlays corrections (no content is rebuilt), so signatures,
photos and link annotations survive intact. Only the stale percentage
text is redacted (text-only redaction) before the new text is written.

Conservative rule: only GREEN (Passed) lines whose recorded value is
exactly NA / N/A are converted. Failed lines are never altered.
"""

import argparse
import io
import re
import sys

import fitz  # PyMuPDF
from PIL import Image

# ---- CxAlloy style constants (sampled from a genuine export) ----------
GREEN = (0.004, 0.765, 0.055)          # tick square fill
GREY_NA = (0.502, 0.502, 0.502)        # N/A square fill
BAR_GREEN = (0.345, 0.788, 0.275)      # progress-bar passed segment
BAR_DARK = (0.4, 0.4, 0.4)             # progress-bar N/A segment
BAR_RED = (0.863, 0.275, 0.275)        # failed segment (only if failed>0)
BAR_TRACK = (0.847, 0.847, 0.855)      # pill track fill
HDR_COLOR = (0x66 / 255,) * 3          # #666666 header text
HDR_SIZE = 8.25
CORNER_R = 1.5 / 12                    # badge corner radius (fraction)
NA_TOKENS = {"na", "n/a", "n.a.", "n.a"}

PCT_RE = re.compile(r"^\d+% Passed \| \d+% Failed \| \d+% N/A$")


def is_close(a, b, tol=0.02):
    return a is not None and b is not None and all(abs(x - y) <= tol for x, y in zip(a, b))


def badge_squares(page):
    """All 12x12 rounded status squares on a page -> [(rect, fill)]."""
    out = []
    for d in page.get_drawings():
        r, f = d["rect"], d.get("fill")
        if f is None:
            continue
        if abs(r.width - 12) < 0.6 and abs(r.height - 12) < 0.6 and 40 <= r.x0 <= 70:
            if is_close(f, GREEN) or is_close(f, GREY_NA) or (f[0] > 0.6 and f[1] < 0.4 and f[2] < 0.4):
                out.append((r, tuple(f)))
    out.sort(key=lambda t: t[0].y0)
    return out


def section_bar_tops(page):
    """y0 of dark full-width section-header bars (block boundaries)."""
    tops = []
    for d in page.get_drawings():
        r, f = d["rect"], d.get("fill")
        if f and is_close(f, (0.333, 0.333, 0.333)) and r.width > 400:
            tops.append(r.y0)
    return sorted(tops)


def text_lines(page):
    """[(y0, x0, joined_text)] for every text line on the page."""
    lines = []
    for b in page.get_text("dict")["blocks"]:
        for ln in b.get("lines", []):
            txt = "".join(s["text"] for s in ln["spans"]).strip()
            if txt:
                x0, y0 = ln["bbox"][0], ln["bbox"][1]
                lines.append((y0, x0, txt))
    lines.sort()
    return lines


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
    return None  # caller falls back to drawn text


def collect_targets(doc):
    """Find green lines whose recorded value is NA. Also census all badges.

    Items are assembled in a single document-order stream so that a value
    printed at the top of the following page (row split across a page
    break) is still attributed to its item.

    Returns (targets, counts) where targets = [(page_no, rect, line_no)]
    and counts = dict(passed=, failed=, na=) BEFORE conversion.
    """
    HEADER_Y, FOOTER_Y = 48, 744  # exclude running page header/footer text
    stream = []  # (page, y, kind, payload) in document order
    for pno in range(doc.page_count):
        page = doc[pno]
        for rect, fill in badge_squares(page):
            stream.append((pno, rect.y0, "sq", (rect, fill)))
        for y in section_bar_tops(page):
            stream.append((pno, y, "bar", None))
        for (y, x, t) in text_lines(page):
            if HEADER_Y <= y <= FOOTER_Y:
                stream.append((pno, y, "txt", (x, t)))
    stream.sort(key=lambda e: (e[0], e[1]))

    targets, counts = [], {"passed": 0, "failed": 0, "na": 0}
    idx = [i for i, e in enumerate(stream) if e[2] == "sq"]
    for k, i in enumerate(idx):
        pno, y0, _, (rect, fill) = stream[i]
        if is_close(fill, GREEN):
            counts["passed"] += 1
        elif is_close(fill, GREY_NA):
            counts["na"] += 1
            continue
        else:
            counts["failed"] += 1
            continue                           # never touch failed lines
        # band = everything until the next badge or section bar
        end = len(stream)
        for j in range(i + 1, len(stream)):
            if stream[j][2] in ("sq", "bar"):
                end = j
                break
        band = [(e[0], e[1], *e[3]) for e in stream[i + 1:end] if e[2] == "txt"]
        line_no = next((t for (_, _, x, t) in band if t.isdigit() and x < 95), "?")
        is_na = any(t.lower() in NA_TOKENS and 90 <= x <= 200
                    for (_, _, x, t) in band)
        if is_na:
            targets.append((pno, rect, line_no))
    return targets, counts


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
        raw[j] = floors[j]  # don't pick the same bucket twice unfairly
    p, f, n = floors
    return f"{p}% Passed | {f}% Failed | {n}% N/A", (p, f, n)


def fix_header_text(doc, new_string):
    """Redact the old percentage line (text only) and write the new one."""
    replaced = 0
    for pno in range(doc.page_count):
        page = doc[pno]
        jobs = []
        for b in page.get_text("dict")["blocks"]:
            for ln in b.get("lines", []):
                for s in ln["spans"]:
                    if PCT_RE.match(s["text"].strip()):
                        jobs.append((fitz.Rect(s["bbox"]), s["origin"]))
        if not jobs:
            continue
        for rect, _ in jobs:
            page.add_redact_annot(rect + (-1, -1, 1, 1))
        page.apply_redactions(
            images=fitz.PDF_REDACT_IMAGE_NONE,
            graphics=fitz.PDF_REDACT_LINE_ART_NONE,
        )
        for _, origin in jobs:
            page.insert_text(origin, new_string, fontname="helv",
                             fontsize=HDR_SIZE, color=HDR_COLOR)
            replaced += 1
    return replaced


def fix_bars(doc, p_frac, f_frac):
    """Repaint every pill progress bar with the new segment split."""
    fixed = 0
    for pno in range(doc.page_count):
        page = doc[pno]
        tracks = [
            d["rect"] for d in page.get_drawings()
            if d.get("fill") and is_close(d["fill"], BAR_TRACK, 0.03)
            and 7 <= d["rect"].height <= 11 and d["rect"].width > 40
        ]
        for t in tracks:
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
            fixed += 1
    return fixed


def flip_badges(doc, targets, glyph_png):
    for pno, rect, _ in targets:
        page = doc[pno]
        shape = page.new_shape()
        # white underlay wipes the green square incl. antialiased edge
        shape.draw_rect(rect + (-0.7, -0.7, 0.7, 0.7))
        shape.finish(fill=(1, 1, 1), color=None)
        # genuine-geometry grey badge on top
        shape.draw_rect(rect, radius=CORNER_R)
        shape.finish(fill=GREY_NA, color=None)
        shape.commit()
        if glyph_png:
            page.insert_image(rect, stream=glyph_png, keep_proportion=True)
        else:  # fallback if the sprite could not be located
            page.insert_textbox(rect + (0, 2.5, 0, 0), "NA", fontname="hebo",
                                fontsize=6, color=(1, 1, 1), align=1)


def main():
    ap = argparse.ArgumentParser(description="Convert NA-valued ticks to N/A badges in a CxAlloy report PDF.")
    ap.add_argument("input")
    ap.add_argument("-o", "--output", default=None)
    ap.add_argument("--dry-run", action="store_true", help="report what would change, write nothing")
    args = ap.parse_args()
    out = args.output or re.sub(r"\.pdf$", "", args.input, flags=re.I) + "_corrected.pdf"

    doc = fitz.open(args.input)
    links_before = sum(len(doc[p].get_links()) for p in range(doc.page_count))

    targets, counts = collect_targets(doc)
    new_p = counts["passed"] - len(targets)
    new_n = counts["na"] + len(targets)
    new_f = counts["failed"]
    s, (pi, fi, ni) = pct_string(new_p, new_f, new_n)

    print(f"Scored lines: {sum(counts.values())}  "
          f"(before: {counts['passed']} passed / {counts['failed']} failed / {counts['na']} N/A)")
    print(f"Lines to convert to N/A ({len(targets)}): "
          + ", ".join(t[2] for t in targets))
    print(f"After:  {new_p} passed / {new_f} failed / {new_n} N/A  ->  \"{s}\"")

    if args.dry_run:
        return
    if not targets:
        print("Nothing to change — output not written.")
        return

    total = new_p + new_f + new_n
    glyph = na_glyph_png(doc)
    if glyph is None:
        print("WARNING: icon sprite not found — using drawn 'NA' fallback glyph.")
    flip_badges(doc, targets, glyph)
    hdr = fix_header_text(doc, s)
    bars = fix_bars(doc, new_p / total, new_f / total)

    doc.save(out, garbage=3, deflate=True)
    links_after = sum(len(fitz.open(out)[p].get_links()) for p in range(doc.page_count))
    print(f"Header strings replaced: {hdr}, bars repainted: {bars}")
    print(f"Link annotations: {links_before} before -> {links_after} after "
          + ("(OK)" if links_before == links_after else "(MISMATCH — check!)"))
    print(f"Written: {out}")


if __name__ == "__main__":
    main()
