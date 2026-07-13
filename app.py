#!/usr/bin/env python3
"""
app.py — Web front-end for cxalloy_na_fix.py

Upload CxAlloy TQ test-report PDFs, get back corrected versions
(NA-valued ticks converted to N/A badges, header stats recalculated).

Stateless by design: the corrected PDF is returned in the same request
(base64 in JSON for the in-page flow, or directly as a file for the
no-JavaScript fallback). Nothing is stored server-side, so it works
correctly across multiple gunicorn workers, instances, and restarts.

Environment:
    APP_PASSWORD   optional — if set, uploads require this password
    MAX_MB         optional — upload size cap in MB (default 25)

Run locally:      python3 app.py            (http://localhost:8000)
Run in prod:      gunicorn -b 0.0.0.0:$PORT -w 2 --timeout 120 app:app
"""

import base64
import io
import os

import fitz
from flask import Flask, jsonify, render_template_string, request, send_file
from werkzeug.utils import secure_filename

from cxalloy_na_fix import (collect_targets, fix_bars, fix_header_text,
                            flip_badges, na_glyph_png, pct_string)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_MB", 25)) * 1024 * 1024
PASSWORD = os.environ.get("APP_PASSWORD", "")


def process_pdf(data: bytes):
    """Run the correction. Returns (out_bytes_or_None, summary_dict)."""
    doc = fitz.open(stream=data, filetype="pdf")
    targets, counts = collect_targets(doc)
    before = dict(counts)
    if not targets:
        doc.close()
        return None, {"before": before, "after": before, "lines": [], "header": None}
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


def handle_upload():
    """Shared validation + processing. Returns (payload, http_status)."""
    if PASSWORD and request.form.get("password", "") != PASSWORD:
        return {"ok": False, "error": "Incorrect password."}, 403
    f = request.files.get("file")
    if not f or not (f.filename or "").lower().endswith(".pdf"):
        return {"ok": False, "error": "Not a .pdf file."}, 400
    stem = secure_filename(f.filename)[:-4] or "report"
    try:
        out, summary = process_pdf(f.read())
    except Exception:
        return {"ok": False,
                "error": "Could not read this file as a CxAlloy report PDF."}, 400
    payload = {"ok": True, "changed": out is not None,
               "filename": f"{stem}_corrected.pdf", **summary}
    if out is not None:
        payload["pdf"] = base64.b64encode(out).decode("ascii")
    return payload, 200


@app.post("/api/convert")
def api_convert():
    payload, status = handle_upload()
    return jsonify(payload), status


@app.post("/direct")
def direct():
    """No-JavaScript fallback: returns the corrected file straight back."""
    payload, status = handle_upload()
    if not payload["ok"]:
        return payload["error"], status
    if not payload["changed"]:
        return ("No NA-valued Passed lines found — this report needs no "
                "correction. Go back and choose another file."), 200
    data = base64.b64decode(payload["pdf"])
    return send_file(io.BytesIO(data), as_attachment=True,
                     download_name=payload["filename"],
                     mimetype="application/pdf")


@app.get("/")
def index():
    return render_template_string(PAGE, needs_password=bool(PASSWORD))


PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CxAlloy N/A Fixer</title>
<style>
  :root{--green:#01c30e;--green-dk:#00990b;--grey:#808080;--dark:#4d4d4d;--red:#dc4646;
        --ink:#2e2e2e;--mut:#6e6e6e;--rule:#dcdcdc;--bg:#f0f1f0;--card:#fff;
        --mono:Consolas,Menlo,"Liberation Mono",monospace}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
       font:15px/1.5 "Segoe UI",system-ui,-apple-system,Arial,sans-serif}
  main{max-width:660px;margin:0 auto;padding:40px 20px 64px}

  /* header */
  .brand{display:flex;align-items:center;gap:10px;margin-bottom:6px}
  .badge{display:inline-flex;align-items:center;justify-content:center;width:26px;height:26px;
         border-radius:4px;color:#fff;font:700 12px/1 Arial;box-shadow:inset 0 -2px rgba(0,0,0,.12)}
  .b-g{background:var(--green)} .b-n{background:var(--grey)}
  .arrow{color:var(--mut);font-size:18px}
  h1{font-size:21px;margin:0;letter-spacing:.2px}
  .sub{color:var(--mut);font-size:13.5px;margin:0 0 26px;max-width:52ch}

  /* dropzone */
  .drop{display:block;border:2px dashed #b9bdb9;border-radius:6px;background:var(--card);
        padding:34px 24px;text-align:center;cursor:pointer;
        transition:border-color .15s,background .15s}
  .drop:hover,.drop.over{border-color:var(--green);background:#f6fef7}
  .drop:focus-within{outline:3px solid var(--ink);outline-offset:2px}
  .drop strong{display:block;font-size:16px;margin-bottom:4px}
  .drop span{color:var(--mut);font-size:13px}
  .sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
  .pwrow{margin-top:14px}
  .pwrow label{display:block;font-size:11.5px;color:var(--mut);text-transform:uppercase;
               letter-spacing:.7px;margin-bottom:5px}
  .pwrow input{width:100%;padding:10px;border:1px solid var(--rule);border-radius:4px;
               background:#fff;font:inherit}
  .pwrow input:focus-visible{outline:3px solid var(--ink);outline-offset:1px}

  /* results */
  #results{margin-top:22px;display:flex;flex-direction:column;gap:12px}
  .row{background:var(--card);border:1px solid var(--rule);border-radius:6px;padding:16px 18px}
  .row-top{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
  .fname{font-weight:600;font-size:14px;word-break:break-all;flex:1;min-width:40%}
  .chip{font:700 10.5px/1 Arial;letter-spacing:.6px;color:#fff;padding:5px 9px;border-radius:3px}
  .c-fix{background:var(--green)} .c-none{background:var(--grey)}
  .c-err{background:var(--red)} .c-busy{background:#9aa09a}
  .bar{display:flex;height:10px;border-radius:5px;overflow:hidden;background:var(--dark);
       margin:13px 0 7px}
  .bar i{display:block;height:100%}
  .bar .p{background:var(--green)} .bar .f{background:var(--red)}
  @media (prefers-reduced-motion:no-preference){.bar i{transition:width .8s ease}}
  .stats{font-family:var(--mono);font-size:12.5px;color:var(--mut);margin:0}
  .stats b{color:var(--ink);font-weight:600}
  .lines{margin:9px 0 0;font-size:12.5px;color:var(--mut)}
  .lines .ln{display:inline-block;background:var(--grey);color:#fff;border-radius:3px;
             font:700 10px/1 Arial;padding:3px 5px;margin:2px 3px 0 0}
  .dl{display:inline-block;margin-top:13px;padding:9px 22px;background:var(--green);color:#fff;
      border-radius:4px;text-decoration:none;font-weight:600;font-size:14px}
  .dl:hover{background:var(--green-dk)}
  .dl:focus-visible{outline:3px solid var(--ink);outline-offset:2px}
  .err-msg{color:var(--red);font-size:13.5px;margin:10px 0 0}
  .clear{margin-top:18px;background:none;border:0;color:var(--mut);font:inherit;font-size:13px;
         text-decoration:underline;cursor:pointer;padding:0}
  .foot{color:var(--mut);font-size:12px;margin-top:30px;border-top:1px solid var(--rule);
        padding-top:14px}
</style></head><body><main>

<div class="brand">
  <span class="badge b-g">&check;</span><span class="arrow">&rarr;</span>
  <span class="badge b-n">NA</span><h1>CxAlloy N/A Fixer</h1>
</div>
<p class="sub">Drop in exported test reports. Passed lines answered &ldquo;NA&rdquo; become
N/A badges, header percentages and progress bars are recalculated, and photo links and
signatures are preserved.</p>

<form id="form">
  <label class="drop" id="drop">
    <input class="sr" type="file" id="file" accept="application/pdf" multiple>
    <strong>Drop report PDFs here</strong>
    <span>or click to browse &mdash; multiple files welcome, up to 25&nbsp;MB each</span>
  </label>
  {% if needs_password %}
  <div class="pwrow">
    <label for="pw">Password</label>
    <input type="password" id="pw" autocomplete="current-password">
  </div>
  {% endif %}
</form>

<noscript>
  <form method="post" action="/direct" enctype="multipart/form-data"
        style="margin-top:14px;background:#fff;border:1px solid #dcdcdc;padding:16px">
    <p style="margin:0 0 8px;font-size:13px">JavaScript is off &mdash; basic mode:
       one file, corrected PDF is returned directly.</p>
    <input type="file" name="file" accept="application/pdf" required>
    {% if needs_password %}<input type="password" name="password" placeholder="Password" required>{% endif %}
    <button type="submit">Fix report</button>
  </form>
</noscript>

<div id="results" aria-live="polite"></div>
<button class="clear" id="clear" hidden>Clear results</button>

<p class="foot">Reports are processed in memory and returned immediately &mdash; nothing is
stored on the server. Originals are never modified; keep them for your audit trail, and
verify converted line numbers against CxAlloy before issuing.</p>

<script>
(function(){
  var drop = document.getElementById('drop');
  var input = document.getElementById('file');
  var results = document.getElementById('results');
  var clearBtn = document.getElementById('clear');
  var pw = document.getElementById('pw');
  var queue = Promise.resolve();

  function pct(n, total){ return total ? Math.round(n/total*100) : 0; }

  function barHTML(st){
    var t = st.passed + st.failed + st.na;
    return '<div class="bar" aria-hidden="true">'
         + '<i class="p" style="width:' + pct(st.passed,t) + '%"></i>'
         + '<i class="f" style="width:' + pct(st.failed,t) + '%"></i></div>';
  }
  function statLine(label, st){
    return label + ' <b>' + st.passed + '</b> passed / <b>' + st.failed
         + '</b> failed / <b>' + st.na + '</b> N/A';
  }

  function makeRow(name){
    var row = document.createElement('div');
    row.className = 'row';
    row.innerHTML = '<div class="row-top"><span class="fname"></span>'
                  + '<span class="chip c-busy">PROCESSING&hellip;</span></div>';
    row.querySelector('.fname').textContent = name;
    results.appendChild(row);
    clearBtn.hidden = false;
    return row;
  }

  function renderDone(row, d){
    var top = row.querySelector('.row-top');
    var chip = row.querySelector('.chip');
    if (!d.ok){
      chip.className = 'chip c-err'; chip.textContent = 'ERROR';
      var e = document.createElement('p'); e.className = 'err-msg';
      e.textContent = d.error || 'Something went wrong.';
      row.appendChild(e); return;
    }
    if (!d.changed){
      chip.className = 'chip c-none'; chip.textContent = 'NO CHANGE';
      var p = document.createElement('p'); p.className = 'stats';
      p.innerHTML = statLine('Already correct:', d.before);
      row.appendChild(p); return;
    }
    chip.className = 'chip c-fix';
    chip.textContent = d.lines.length + ' LINE' + (d.lines.length>1?'S':'') + ' FIXED';

    row.insertAdjacentHTML('beforeend', barHTML(d.before));
    var s = document.createElement('p'); s.className = 'stats';
    s.innerHTML = statLine('Before:', d.before) + '<br>'
                + statLine('After:&nbsp;', d.after) + ' &mdash; <b>' + d.header + '</b>';
    row.appendChild(s);

    var ln = document.createElement('p'); ln.className = 'lines';
    ln.innerHTML = 'Converted lines: ' + d.lines.map(function(n){
      return '<span class="ln">' + n + '</span>'; }).join('');
    row.appendChild(ln);

    var bytes = atob(d.pdf), arr = new Uint8Array(bytes.length);
    for (var i=0;i<bytes.length;i++) arr[i] = bytes.charCodeAt(i);
    var url = URL.createObjectURL(new Blob([arr], {type:'application/pdf'}));
    var a = document.createElement('a');
    a.className = 'dl'; a.href = url; a.download = d.filename;
    a.textContent = 'Download ' + d.filename;
    row.appendChild(a);

    // animate the bar from the old split to the new one
    requestAnimationFrame(function(){ requestAnimationFrame(function(){
      var t = d.after.passed + d.after.failed + d.after.na;
      var segs = row.querySelectorAll('.bar i');
      segs[0].style.width = pct(d.after.passed,t) + '%';
      segs[1].style.width = pct(d.after.failed,t) + '%';
    });});
  }

  function processFile(file){
    var row = makeRow(file.name);
    var fd = new FormData();
    fd.append('file', file);
    if (pw) fd.append('password', pw.value);
    return fetch('/api/convert', {method:'POST', body:fd})
      .then(function(r){ return r.json(); })
      .catch(function(){ return {ok:false, error:'Network error — try again.'}; })
      .then(function(d){ renderDone(row, d); });
  }

  function accept(files){
    Array.prototype.forEach.call(files, function(f){
      if (!/\.pdf$/i.test(f.name)){
        renderDone(makeRow(f.name), {ok:false, error:'Not a .pdf file.'});
        return;
      }
      queue = queue.then(function(){ return processFile(f); });
    });
  }

  input.addEventListener('change', function(){ accept(input.files); input.value=''; });
  ['dragover','dragenter'].forEach(function(ev){
    drop.addEventListener(ev, function(e){ e.preventDefault(); drop.classList.add('over'); });
  });
  ['dragleave','drop'].forEach(function(ev){
    drop.addEventListener(ev, function(e){ e.preventDefault(); drop.classList.remove('over'); });
  });
  drop.addEventListener('drop', function(e){ accept(e.dataTransfer.files); });
  clearBtn.addEventListener('click', function(){
    results.innerHTML=''; clearBtn.hidden = true;
  });
})();
</script>
</main></body></html>"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
