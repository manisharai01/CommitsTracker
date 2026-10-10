"""The script inside report.html, and the Content-Security-Policy that lets
only that script run.

Kept apart from htmlreport (which needs pandas) so the web server can serve
reports without loading pandas: on a 512 MB host that memory (about 40 MB)
is needed by the PDF step.
"""

from __future__ import annotations

import base64
import hashlib

SCRIPT = """
(function () {
  function setAll(open, selector) {
    document.querySelectorAll(selector || 'details.commitlist').forEach(function (d) { d.open = open; });
  }
  var btn = document.getElementById('toggle-commits');
  if (btn) {
    btn.addEventListener('click', function () {
      var open = btn.dataset.state !== 'open';
      setAll(open);
      btn.dataset.state = open ? 'open' : 'closed';
      btn.textContent = open ? 'Collapse all commit lists' : 'Expand all commit lists';
    });
  }
  // A PDF prints only one part of a long appendix at a time: its rows FROM
  // to TO-1, after the rest of the report (first part) or alone (later ones).
  function printPart(from, to) {
    var rows = document.querySelectorAll('#commit-timeline tbody > tr');
    for (var i = 0; i < rows.length; i++) {
      if (i < from || i >= to) rows[i].classList.add('pskip');
    }
    // A part that starts mid-month repeats that month's header.
    var first = rows[from];
    if (from > 0 && first && !first.classList.contains('month')) {
      for (var j = from - 1; j >= 0; j--) {
        if (rows[j].classList.contains('month')) {
          var head = rows[j].cloneNode(true);
          head.classList.remove('pskip');
          head.cells[0].textContent += ' (continued)';
          first.parentNode.insertBefore(head, first);
          break;
        }
      }
    }
    if (from > 0) document.body.classList.add('pcont');
    if (to < rows.length) document.body.classList.add('pmore');
  }
  // For PDF/printing only the chronological appendix is expanded; collapsed
  // per-repo lists are hidden by the print stylesheet so raw commit data
  // appears once, at the end, in time order ('#print', or '#print=FROM-TO'
  // for one part of it).
  var print = /^#print(?:=(\\d+)-(\\d+))?$/.exec(location.hash);
  if (print) {
    setAll(true, 'details.timeline');
    if (print[1]) printPart(+print[1], +print[2]);
  }
  // 'Save as PDF' in the web app opens the report with '#save-pdf': the
  // browser's print dialog, where 'Save as PDF' is a destination, makes the
  // PDF on the reader's own device - no server memory needed, however long
  // the report.
  if (location.hash === '#save-pdf') {
    setAll(true, 'details.timeline');
    window.addEventListener('load', function () { setTimeout(function () { window.print(); }, 250); });
  }
  window.addEventListener('beforeprint', function () { setAll(true, 'details.timeline'); });
})();
"""

#: The only script the report may run (anything injected would not match).
SCRIPT_HASH = "sha256-" + base64.b64encode(hashlib.sha256(SCRIPT.encode("utf-8")).digest()).decode("ascii")
#: Content-Security-Policy of report.html: no network access at all (charts
#: are data URIs), inline styles, and only the script above.
REPORT_CSP = (
    "default-src 'none'; img-src data:; style-src 'unsafe-inline'; "
    f"script-src '{SCRIPT_HASH}'; base-uri 'none'; form-action 'none'"
)
