#!/usr/bin/env python3
"""
app.py — Web front-end for cxalloy_na_fix.py

Upload a CxAlloy TQ test-report PDF, get back the corrected version
(NA-valued ticks converted to N/A badges, header stats recalculated).

Environment:
    APP_PASSWORD   optional — if set, uploads require this password
    MAX_MB         optional — upload size cap in MB (default 25)

Run locally:      python3 app.py            (http://localhost:8000)
Run in prod:      gunicorn -b 0.0.0.0:8000 -w 2 --timeout 120 app:app

Privacy: files are processed entirely in memory. The corrected PDF is
held for a single download (15-minute expiry) and nothing is written
to disk or logged.
"""

import io
import os
import secrets
import time

import fitz
from flask import Flask, abort, render_template_string, request, send_file
from werkzeug.utils import secure_filename

from cxalloy_na_fix import (collect_targets, fix_bars, fix_header_text,
                            flip_badges, na_glyph_png, pct_string)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_MB", 25)) * 1024 * 1024
PASSWORD = os.environ.get("APP_PASSWORD", "")
STORE = {}          # token -> (pdf_bytes, download_name, created_ts)
STORE_TTL = 900     # seconds


def purge_store():
    now = time.time()
    for k in [k for k, v in STORE.items() if now - v[2] > STORE_TTL]:
        STORE.pop(k, None)


def process_pdf(data: bytes):
    """Run the correction. Returns (out_bytes_or_None, summary_dict)."""
    doc = fitz.open(stream=data, filetype="pdf")
    targets, counts = collect_targets(doc)
    before = dict(counts)
    if not targets:
        doc.close()
        return None, {"before": before, "lines": [], "header": None}
    new_p = counts["passed"] - len(targets)
    new_f = counts["failed"]
    new_n = counts["na"] + len(targets)
    header, _ = pct_string(new_p, new_f, new_n)
    total = new_p + new_f + new_n
    flip_badges(doc, targets, na_glyph_png(doc))
    fix_header_text(doc, header)
    fix_bars(doc, new_p / total, new_f / total)
    out = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    return out, {
        "before": before,
        "after": {"passed": new_p, "failed": new_f, "na": new_n},
        "lines": [t[2] for t in targets],
        "header": header,
    }


PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CxAlloy N/A Fixer</title>
<style>
  :root{--green:#01c30e;--grey:#808080;--ink:#333;--mut:#666;--rule:#d9d9d9;--bg:#f2f2f2}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
       font:15px/1.5 "Segoe UI",system-ui,-apple-system,Arial,sans-serif}
  main{max-width:620px;margin:48px auto;padding:0 20px}
  .card{background:#fff;border:1px solid var(--rule);border-top:4px solid var(--green);
        padding:28px 32px}
  h1{font-size:19px;margin:0 0 4px;letter-spacing:.2px}
  .sub{color:var(--mut);font-size:13px;margin:0 0 22px}
  .badge{display:inline-block;width:18px;height:18px;border-radius:3px;color:#fff;
         font:700 10px/18px Arial;text-align:center;vertical-align:-4px}
  .b-g{background:var(--green)} .b-n{background:var(--grey)}
  .arrow{color:var(--mut);margin:0 4px}
  label{display:block;font-size:12px;color:var(--mut);text-transform:uppercase;
        letter-spacing:.6px;margin:16px 0 6px}
  input[type=file],input[type=password]{width:100%;padding:9px;border:1px solid var(--rule);
        background:#fafafa;font:inherit}
  button{margin-top:20px;width:100%;padding:11px;border:0;background:var(--green);
         color:#fff;font:600 15px inherit;cursor:pointer}
  button:hover{filter:brightness(.94)}
  button:focus-visible,a:focus-visible{outline:3px solid #333;outline-offset:2px}
  .msg{border:1px solid var(--rule);border-left:4px solid var(--grey);background:#fafafa;
       padding:12px 16px;margin-bottom:20px;font-size:14px}
  .msg.ok{border-left-color:var(--green)}
  .stats{font-family:Consolas,Menlo,monospace;font-size:13px;background:#fafafa;
         border:1px solid var(--rule);padding:12px 16px;margin:14px 0;white-space:pre-wrap}
  a.dl{display:block;text-align:center;margin-top:18px;padding:11px;background:var(--green);
       color:#fff;text-decoration:none;font-weight:600}
  .foot{color:var(--mut);font-size:12px;margin-top:16px}
</style></head><body><main>
<div class="card">
  <h1><span class="badge b-g">&check;</span><span class="arrow">&rarr;</span><span
      class="badge b-n">NA</span>&nbsp; CxAlloy N/A Fixer</h1>
  <p class="sub">Converts Passed lines answered &ldquo;NA&rdquo; into N/A badges and
     recalculates the report header. Photo links and signatures are preserved.</p>

  {% if error %}<div class="msg">{{ error }}</div>{% endif %}

  {% if summary %}
    {% if summary.header %}
      <div class="msg ok"><strong>{{ summary.lines|length }} line(s) converted:</strong>
        {{ summary.lines|join(', ') }}</div>
      <div class="stats">Before: {{ summary.before.passed }} passed / {{ summary.before.failed }} failed / {{ summary.before.na }} N/A
After:  {{ summary.after.passed }} passed / {{ summary.after.failed }} failed / {{ summary.after.na }} N/A
Header: {{ summary.header }}</div>
      <a class="dl" href="/download/{{ token }}">Download corrected PDF</a>
      <p class="foot">Single-use link, expires in 15 minutes. Verify the converted
         line numbers against CxAlloy before issuing.</p>
    {% else %}
      <div class="msg ok">No NA-valued Passed lines found &mdash; this report needs no
        correction. ({{ summary.before.passed }} passed / {{ summary.before.failed }}
        failed / {{ summary.before.na }} N/A)</div>
    {% endif %}
    <hr style="border:0;border-top:1px solid var(--rule);margin:24px 0">
  {% endif %}

  <form method="post" enctype="multipart/form-data" action="/">
    <label for="file">CxAlloy test report (.pdf)</label>
    <input id="file" type="file" name="file" accept="application/pdf" required>
    {% if needs_password %}
      <label for="pw">Password</label>
      <input id="pw" type="password" name="password" required>
    {% endif %}
    <button type="submit">Fix report</button>
  </form>
  <p class="foot">Files are processed in memory and never stored. Original PDFs are
     not modified &mdash; keep them for your audit trail.</p>
</div>
</main></body></html>"""


@app.route("/", methods=["GET", "POST"])
def index():
    purge_store()
    ctx = {"needs_password": bool(PASSWORD), "error": None,
           "summary": None, "token": None}
    if request.method == "GET":
        return render_template_string(PAGE, **ctx)

    if PASSWORD and request.form.get("password", "") != PASSWORD:
        ctx["error"] = "Incorrect password."
        return render_template_string(PAGE, **ctx), 403

    f = request.files.get("file")
    if not f or not f.filename.lower().endswith(".pdf"):
        ctx["error"] = "Choose a .pdf file to upload."
        return render_template_string(PAGE, **ctx), 400

    try:
        out, summary = process_pdf(f.read())
    except Exception:
        ctx["error"] = ("That file could not be read as a CxAlloy report PDF. "
                        "Upload the unmodified export from CxAlloy TQ.")
        return render_template_string(PAGE, **ctx), 400

    ctx["summary"] = summary
    if out is not None:
        token = secrets.token_urlsafe(16)
        stem = secure_filename(f.filename)[:-4] or "report"
        STORE[token] = (out, f"{stem}_corrected.pdf", time.time())
        ctx["token"] = token
    return render_template_string(PAGE, **ctx)


@app.route("/download/<token>")
def download(token):
    purge_store()
    item = STORE.pop(token, None)   # single use
    if item is None:
        abort(410, "Link expired or already used — upload the file again.")
    data, name, _ = item
    return send_file(io.BytesIO(data), as_attachment=True,
                     download_name=name, mimetype="application/pdf")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
