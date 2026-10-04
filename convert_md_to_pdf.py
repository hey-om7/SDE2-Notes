#!/usr/bin/env python3
"""
convert_md_to_pdf.py
====================

Convert a Markdown document into a modern, professional-looking PDF.

Features
--------
* Full Markdown support: headings, tables, fenced code, lists, blockquotes.
* Syntax highlighting for code blocks (Java, YAML, JSON, bash, HCL, SQL, ...)
  via Pygments.
* A custom, AWS-inspired theme: colored headings, zebra-striped tables,
  styled code blocks, a cover page, running header + page numbers, and a
  page break before every top-level ("# N.") section.
* Colored callout boxes for lines like
  `> **Important:** ...`, `> **Warning:** ...`,
  `> **Best Practice:** ...`, `> **Interview Tip:** ...`.
* Optional Mermaid diagram rendering: if the Mermaid CLI (`mmdc`) is on PATH,
  ```mermaid blocks are rendered to SVG and embedded; otherwise they are shown
  as a nicely-styled "diagram source" figure.

Dependencies
------------
    pip install markdown pygments weasyprint

WeasyPrint needs native libraries (pango, cairo, gdk-pixbuf). On macOS:
    brew install pango gdk-pixbuf libffi
(or simply:  brew install weasyprint)

For rendering Mermaid diagrams to real images (enabled by default):
    npm install -g @mermaid-js/mermaid-cli      # provides the `mmdc` command

Usage
-----
    python convert_md_to_pdf.py                         # uses the default guide file
    python convert_md_to_pdf.py INPUT.md OUTPUT.pdf
    python convert_md_to_pdf.py INPUT.md --no-render-mermaid   # force source fallback
    python convert_md_to_pdf.py --help
"""

from __future__ import annotations

import argparse
import base64
import html
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #
DEFAULT_INPUT = "AWS-SDE2-Java-Backend-Complete-Guide.md"

# These are only *fallbacks*. The real title/subtitle are derived from the
# Markdown itself (the first "# " heading and the first "> " blockquote line),
# so the same script produces a correct cover for any document. You can still
# override them explicitly with --title / --subtitle.
FALLBACK_TITLE = "Document"
FALLBACK_SUBTITLE = ""

# Pygments code-highlighting theme (light, print-friendly). Try e.g. "friendly",
# "default", "tango", "manni". Change CODE_BG below to match if you pick a dark one.
PYGMENTS_STYLE = "friendly"
CODE_BG = "#f6f8fa"  # background behind code blocks

# AWS-inspired palette
PALETTE = {
    "ink": "#16212E",        # near-black "squid ink"
    "ink_soft": "#232F3E",
    "orange": "#FF9900",     # AWS orange accent
    "orange_dark": "#EC7211",
    "blue": "#0972D3",
    "blue_soft": "#E7F0FB",
    "red": "#D13212",
    "red_soft": "#FDECEA",
    "green": "#037F0C",
    "green_soft": "#EAF6EB",
    "purple": "#7D2BC4",
    "purple_soft": "#F2E9FB",
    "border": "#D5DBDB",
    "row_alt": "#F4F6F7",
    "text": "#1B2733",
    "muted": "#5F6B7A",
}


# --------------------------------------------------------------------------- #
# Native library path (WeasyPrint needs pango/cairo/glib)
# --------------------------------------------------------------------------- #
def ensure_native_libs() -> None:
    """
    On macOS, WeasyPrint loads native libs (libgobject/pango/cairo) via the
    dynamic loader, which reads DYLD_FALLBACK_LIBRARY_PATH only at process
    launch. If those libs live under a Homebrew prefix that isn't on the path,
    re-exec this process once with the path added so the import succeeds.
    """
    if sys.platform != "darwin":
        return
    if os.environ.get("_MD2PDF_REEXEC") == "1":
        return  # already re-execed; avoid an infinite loop

    candidates = ["/opt/homebrew/lib", "/usr/local/lib"]
    lib_dirs = [
        d for d in candidates
        if os.path.isdir(d)
        and any(f.startswith("libgobject-2.0") for f in os.listdir(d))
    ]
    if not lib_dirs:
        return  # nothing to add; let the normal import error guide the user

    current = os.environ.get("DYLD_FALLBACK_LIBRARY_PATH", "")
    parts = [p for p in current.split(os.pathsep) if p]
    missing = [d for d in lib_dirs if d not in parts]
    if not missing:
        return  # already present

    new_path = os.pathsep.join(missing + parts) if parts else os.pathsep.join(missing)
    new_env = dict(os.environ)
    new_env["DYLD_FALLBACK_LIBRARY_PATH"] = new_path
    new_env["_MD2PDF_REEXEC"] = "1"
    # Re-launch the exact same command with the augmented environment.
    os.execve(sys.executable, [sys.executable] + sys.argv, new_env)


# --------------------------------------------------------------------------- #
# Dependency checks
# --------------------------------------------------------------------------- #
def _require(module: str, pip_name: str | None = None):
    try:
        return __import__(module)
    except ImportError:
        pip_name = pip_name or module
        sys.exit(
            f"\nMissing dependency '{module}'.\n"
            f"Install the requirements with:\n"
            f"    pip install markdown pygments weasyprint\n"
            f"(this one needs: pip install {pip_name})\n"
        )


# --------------------------------------------------------------------------- #
# Document metadata (derive title/subtitle/section count from the Markdown)
# --------------------------------------------------------------------------- #
# First top-level ATX heading, e.g. "# Spring Boot SDE2 — ... Guide"
_FIRST_H1 = re.compile(r"^\#[ \t]+(?P<title>\S.*?)[ \t]*$", re.MULTILINE)
# A numbered top-level section heading, e.g. "# 12. Spring Data JPA"
_NUMBERED_H1 = re.compile(r"^\#[ \t]+\d+\.[ \t]+\S", re.MULTILINE)


def _strip_md_inline(text: str) -> str:
    """Strip common inline Markdown (emphasis, code, links) for plain display."""
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)  # [label](url) -> label
    text = re.sub(r"[*_`]+", "", text)                     # */_/` emphasis & code
    return text.strip()


def extract_title(md_text: str, fallback: str) -> str:
    """The document title = text of the first '# ' heading."""
    m = _FIRST_H1.search(md_text)
    return _strip_md_inline(m.group("title")) if m else fallback


def extract_subtitle(md_text: str, fallback: str) -> str:
    """
    The subtitle = the first blockquote line ('> ...') that appears after the
    first H1. This matches the convention of a one-line description under the
    title. Returns the fallback if none is found.
    """
    m = _FIRST_H1.search(md_text)
    start = m.end() if m else 0
    for line in md_text[start:].splitlines():
        stripped = line.strip()
        if stripped.startswith(">"):
            quote = stripped.lstrip(">").strip()
            if quote:
                return _strip_md_inline(quote)
        elif stripped.startswith("#"):
            break  # reached the next heading before any blockquote
    return fallback


def count_sections(md_text: str) -> int:
    """Number of top-level numbered sections ('# N. Title')."""
    return len(_NUMBERED_H1.findall(md_text))


def short_footer_label(title: str) -> str:
    """
    A compact footer label derived from the title: the part before the first
    em/en dash, hyphen, or colon, capped in length.
    e.g. "Spring Boot SDE2 — Java Backend ..." -> "Spring Boot SDE2 Guide".
    """
    head = re.split(r"\s*[—–\-:]\s*", title, maxsplit=1)[0].strip()
    if len(head) > 40:
        head = head[:40].rstrip() + "…"
    return f"{head} Guide" if head and "guide" not in head.lower() else (head or "Guide")


# --------------------------------------------------------------------------- #
# Mermaid handling
# --------------------------------------------------------------------------- #
_MERMAID_BLOCK = re.compile(r"```mermaid[ \t]*\n(.*?)```", re.DOTALL)

# Scale factor passed to mmdc (-s). Higher = sharper raster output in the PDF.
_MERMAID_SCALE = "3"

# mmdc puppeteer config: run headless Chromium with the no-sandbox flag so it
# works in restricted / CI environments without extra setup.
_PUPPETEER_CONFIG = '{"args": ["--no-sandbox", "--disable-setuid-sandbox"]}'

# Mermaid rendering config: a clean light theme that matches the document and
# keeps diagram text readable at print size.
_MERMAID_CONFIG = (
    '{'
    '"theme": "default",'
    '"themeVariables": {'
    '"fontFamily": "Helvetica Neue, Segoe UI, Arial, sans-serif",'
    '"fontSize": "16px"'
    '},'
    '"flowchart": {"htmlLabels": true, "curve": "basis", "useMaxWidth": true},'
    '"sequence": {"useMaxWidth": true}'
    '}'
)


def _mmdc_available() -> bool:
    return shutil.which("mmdc") is not None


def _render_mermaid_to_png(source: str) -> bytes | None:
    """
    Render a single Mermaid diagram to a high-resolution PNG using mmdc.

    PNG is used (rather than SVG) because WeasyPrint's SVG support does not
    fully handle the foreignObject / HTML labels Mermaid emits, which produced
    the vague, mangled diagrams. A rasterised PNG embeds exactly as drawn.
    """
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            in_path = tmp_dir / "d.mmd"
            out_path = tmp_dir / "d.png"
            cfg_path = tmp_dir / "config.json"
            pptr_path = tmp_dir / "puppeteer.json"
            in_path.write_text(source, encoding="utf-8")
            cfg_path.write_text(_MERMAID_CONFIG, encoding="utf-8")
            pptr_path.write_text(_PUPPETEER_CONFIG, encoding="utf-8")
            result = subprocess.run(
                ["mmdc",
                 "-i", str(in_path),
                 "-o", str(out_path),
                 "-b", "white",
                 "-s", _MERMAID_SCALE,
                 "-c", str(cfg_path),
                 "-p", str(pptr_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if result.returncode != 0 or not out_path.exists():
                err = (result.stderr or b"").decode("utf-8", "replace").strip()
                print(f"  warning: mermaid render failed: {err.splitlines()[-1] if err else 'unknown error'}",
                      file=sys.stderr)
                return None
            return out_path.read_bytes()
    except Exception as exc:  # pragma: no cover - defensive
        print(f"  warning: mermaid render raised: {exc}", file=sys.stderr)
        return None


def preprocess_mermaid(md_text: str, render: bool) -> str:
    """Replace ```mermaid blocks with HTML figures (PNG if possible, else source)."""
    use_cli = render and _mmdc_available()
    if render and not _mmdc_available():
        print("  note: Mermaid rendering requested but 'mmdc' not found on PATH.\n"
              "        Install it with:  npm install -g @mermaid-js/mermaid-cli\n"
              "        Falling back to styled diagram source for now.",
              file=sys.stderr)

    total = len(_MERMAID_BLOCK.findall(md_text))
    counter = {"i": 0, "ok": 0}

    def repl(match: re.Match) -> str:
        source = match.group(1).rstrip("\n")
        counter["i"] += 1
        if use_cli:
            png = _render_mermaid_to_png(source)
            if png:
                counter["ok"] += 1
                b64 = base64.b64encode(png).decode("ascii")
                return (
                    '\n<figure class="mermaid-figure">'
                    f'<img alt="diagram" src="data:image/png;base64,{b64}"/>'
                    '</figure>\n'
                )
        # Fallback: styled source box
        escaped = html.escape(source)
        return (
            '\n<div class="mermaid-fallback">'
            '<div class="mermaid-label">◆ Diagram (Mermaid source)</div>'
            f'<pre class="mermaid-src">{escaped}</pre></div>\n'
        )

    out = _MERMAID_BLOCK.sub(repl, md_text)
    if total:
        if use_cli:
            print(f"  rendered {counter['ok']}/{total} Mermaid diagram(s) to images.")
        else:
            print(f"  found {total} Mermaid diagram(s) (shown as source).")
    return out


# --------------------------------------------------------------------------- #
# Callout post-processing
# --------------------------------------------------------------------------- #
_BLOCKQUOTE = re.compile(r"<blockquote>(.*?)</blockquote>", re.DOTALL)


def _callout_class(inner_html: str) -> str:
    """Pick a callout style from the first bold label inside a blockquote."""
    lowered = inner_html.lower()
    if "warning" in lowered:
        return "callout warning"
    if "best practice" in lowered:
        return "callout best"
    if "interview" in lowered:        # "Interview Tip", "SDE2 Interview Tip", "Interview Q"
        return "callout tip"
    if "important" in lowered:
        return "callout important"
    if "note" in lowered:
        return "callout note"
    return "callout note"


def apply_callouts(html_text: str) -> str:
    def repl(match: re.Match) -> str:
        inner = match.group(1)
        cls = _callout_class(inner)
        return f'<blockquote class="{cls}">{inner}</blockquote>'

    return _BLOCKQUOTE.sub(repl, html_text)


# --------------------------------------------------------------------------- #
# Table of contents (auto-generated index with real PDF page numbers)
# --------------------------------------------------------------------------- #
# Matches the rendered top-level section headings, e.g.
#   <h1 id="1-aws-fundamentals">1. AWS Fundamentals</h1>
_H1_WITH_ID = re.compile(
    r'<h1[^>]*\bid="(?P<id>[^"]+)"[^>]*>(?P<text>.*?)</h1>',
    re.DOTALL | re.IGNORECASE,
)
# A top-level numbered section looks like "1. Title" / "12. Title".
_NUMBERED_SECTION = re.compile(r"^\s*\d+\.\s")
# The hand-written static ToC block in the Markdown, from the heading down to
# the first horizontal rule that follows it.
_STATIC_TOC = re.compile(
    r"<h2[^>]*>\s*Table of Contents\s*</h2>.*?(?=<hr\s*/?>|<h1)",
    re.DOTALL | re.IGNORECASE,
)


def _strip_tags(text: str) -> str:
    """Remove any inline HTML tags, returning plain text."""
    return re.sub(r"<[^>]+>", "", text).strip()


def build_toc_html(body_html: str) -> str:
    """
    Build a Table of Contents from the rendered H1 section headings.

    Each entry links to the section anchor; the printed page number is filled
    in by WeasyPrint via CSS `target-counter`, so the index always reflects the
    true rendered page.
    """
    entries = []
    for m in _H1_WITH_ID.finditer(body_html):
        text = _strip_tags(m.group("text"))
        if not _NUMBERED_SECTION.match(text):
            continue  # skip the cover-adjacent / non-numbered H1s
        # Drop the leading "N. " — the ToC re-numbers via a CSS counter so the
        # printed numbering always stays sequential and consistent.
        text_no_num = re.sub(r"^\s*\d+\.\s*", "", text).strip()
        entries.append((m.group("id"), text_no_num))

    if not entries:
        return ""

    items = "\n".join(
        f'    <li><a href="#{html.escape(anchor)}">{html.escape(text_no_num)}</a></li>'
        for anchor, text_no_num in entries
    )
    return (
        '<section class="toc">\n'
        '  <h2 class="toc-title">Table of Contents</h2>\n'
        f'  <ol class="toc-list">\n{items}\n  </ol>\n'
        '</section>'
    )


def replace_static_toc(body_html: str, toc_html: str) -> str:
    """Swap the hand-written ToC in the Markdown for the generated index."""
    if not toc_html:
        return body_html
    new_html, count = _STATIC_TOC.subn(toc_html, body_html, count=1)
    if count:
        return new_html
    # No static ToC found: insert the generated one right before the first H1.
    first_h1 = re.search(r"<h1", body_html)
    if first_h1:
        idx = first_h1.start()
        return body_html[:idx] + toc_html + "\n" + body_html[idx:]
    return toc_html + "\n" + body_html


# --------------------------------------------------------------------------- #
# CSS
# --------------------------------------------------------------------------- #
def build_css(pygments_css: str, doc_title: str, footer_label: str) -> str:
    p = PALETTE
    return f"""
/* ---------- Pygments syntax highlighting ---------- */
{pygments_css}

/* ---------- Page setup ---------- */
@page {{
    size: A4;
    margin: 20mm 16mm 18mm 16mm;
    @top-right {{
        content: "{doc_title}";
        font-family: 'Helvetica Neue', Arial, sans-serif;
        font-size: 7.5pt;
        color: {p['muted']};
    }}
    @bottom-right {{
        content: "Page " counter(page) " of " counter(pages);
        font-family: 'Helvetica Neue', Arial, sans-serif;
        font-size: 7.5pt;
        color: {p['muted']};
    }}
    @bottom-left {{
        content: "{footer_label}";
        font-family: 'Helvetica Neue', Arial, sans-serif;
        font-size: 7.5pt;
        color: {p['muted']};
    }}
}}
@page :first {{
    margin: 0;
    @top-right {{ content: ""; }}
    @bottom-right {{ content: ""; }}
    @bottom-left {{ content: ""; }}
}}

/* ---------- Base typography ---------- */
html {{ font-size: 10.3pt; }}
body {{
    font-family: 'Helvetica Neue', 'Segoe UI', Arial, sans-serif;
    color: {p['text']};
    line-height: 1.5;
    hyphens: none;
}}

/* ---------- Cover page ---------- */
.cover {{
    page-break-after: always;
    height: 100vh;
    background: #ffffff;
    color: {p['ink']};
    padding: 46mm 24mm 24mm 24mm;
    box-sizing: border-box;
    position: relative;
    border-top: 10mm solid {p['orange']};
}}
.cover .kicker {{
    font-size: 10pt;
    font-weight: 700;
    letter-spacing: 2.5px;
    text-transform: uppercase;
    color: {p['orange_dark']};
    margin-bottom: 7mm;
}}
.cover .bar {{
    width: 46mm; height: 4px; background: {p['orange']};
    border-radius: 2px; margin-bottom: 10mm;
}}
.cover h1 {{
    font-size: 30pt; line-height: 1.18; margin: 0 0 7mm 0;
    color: {p['ink']}; border: none; padding: 0; font-weight: 800;
    break-before: avoid;
}}
.cover .subtitle {{
    font-size: 13.5pt; color: {p['muted']}; font-weight: 400;
    line-height: 1.4; max-width: 150mm;
}}
.cover .meta {{
    position: absolute; bottom: 26mm; left: 24mm; right: 24mm;
    font-size: 9.5pt; color: {p['muted']};
    border-top: 1.5px solid {p['border']}; padding-top: 6mm;
}}
.cover .meta .accent {{ color: {p['orange_dark']}; font-weight: 700; }}
.cover .meta-row {{ margin-top: 2mm; }}

/* ---------- Headings ---------- */
h1, h2, h3, h4 {{
    font-weight: 700;
    color: {p['ink']};
    line-height: 1.25;
}}
h1 {{
    font-size: 20pt;
    margin: 0 0 10pt 0;
    padding: 10pt 0 8pt 0;
    border-bottom: 3px solid {p['orange']};
    break-before: page;
    break-after: avoid;
}}
/* first real H1 after the cover should not add another blank page */
.content > h1:first-child {{ break-before: avoid; }}
h2 {{
    font-size: 14.5pt;
    margin: 16pt 0 7pt 0;
    padding-left: 9pt;
    border-left: 5px solid {p['orange']};
    color: {p['ink_soft']};
    break-after: avoid;
}}
h3 {{
    font-size: 12pt;
    margin: 13pt 0 5pt 0;
    color: {p['blue']};
    break-after: avoid;
}}
h4 {{
    font-size: 10.6pt;
    margin: 10pt 0 4pt 0;
    color: {p['ink_soft']};
    break-after: avoid;
}}

/* ---------- Section end-blocks (Key Takeaways etc.) ---------- */
h3 {{ letter-spacing: 0.2px; }}

p {{ margin: 5pt 0; }}
a {{ color: {p['blue']}; text-decoration: none; }}
strong {{ color: {p['ink']}; }}

ul, ol {{ margin: 5pt 0 5pt 0; padding-left: 20pt; }}
li {{ margin: 2.5pt 0; }}
li > ul, li > ol {{ margin: 2pt 0; }}

hr {{
    border: none;
    border-top: 1px solid {p['border']};
    margin: 14pt 0;
}}

/* ---------- Inline code ---------- */
code {{
    font-family: 'SFMono-Regular', 'Consolas', 'Menlo', monospace;
    font-size: 8.8pt;
    background: {CODE_BG};
    color: #B3104A;
    padding: 1px 4px;
    border-radius: 3px;
    border: 1px solid {p['border']};
    word-break: break-word;
}}

/* ---------- Code blocks ---------- */
pre {{
    font-family: 'SFMono-Regular', 'Consolas', 'Menlo', monospace;
    font-size: 8.4pt;
    line-height: 1.42;
    background: {CODE_BG};
    border: 1px solid {p['border']};
    border-left: 4px solid {p['ink_soft']};
    border-radius: 6px;
    padding: 9pt 11pt;
    margin: 7pt 0;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    break-inside: avoid;
}}
pre code {{
    background: none; border: none; padding: 0; color: inherit;
    font-size: inherit; word-break: normal;
}}
.codehilite {{
    background: {CODE_BG};
    border: 1px solid {p['border']};
    border-left: 4px solid {p['ink_soft']};
    border-radius: 6px;
    margin: 7pt 0;
    break-inside: avoid;
}}
.codehilite pre {{
    border: none; border-radius: 0; margin: 0; background: none;
}}

/* ---------- Tables ---------- */
table {{
    border-collapse: collapse;
    width: 100%;
    margin: 8pt 0;
    font-size: 8.6pt;
    break-inside: avoid;
}}
th, td {{
    border: 1px solid {p['border']};
    padding: 4.5pt 6pt;
    text-align: left;
    vertical-align: top;
    overflow-wrap: anywhere;
}}
th {{
    background: {p['ink_soft']};
    color: #fff;
    font-weight: 600;
}}
tbody tr:nth-child(even) {{ background: {p['row_alt']}; }}

/* ---------- Blockquotes / callouts ---------- */
blockquote {{
    margin: 8pt 0;
    padding: 7pt 11pt 7pt 12pt;
    border-radius: 6px;
    break-inside: avoid;
}}
blockquote p {{ margin: 3pt 0; }}
blockquote p:first-child {{ margin-top: 0; }}
blockquote p:last-child {{ margin-bottom: 0; }}

.callout {{ border-left: 5px solid {p['muted']}; background: #F4F6F7; }}
.callout.important {{ border-left-color: {p['blue']};   background: {p['blue_soft']}; }}
.callout.warning   {{ border-left-color: {p['red']};    background: {p['red_soft']}; }}
.callout.best      {{ border-left-color: {p['green']};  background: {p['green_soft']}; }}
.callout.tip       {{ border-left-color: {p['purple']}; background: {p['purple_soft']}; }}
.callout.note      {{ border-left-color: {p['muted']};  background: #F4F6F7; }}
.callout.important strong:first-child {{ color: {p['blue']}; }}
.callout.warning strong:first-child   {{ color: {p['red']}; }}
.callout.best strong:first-child      {{ color: {p['green']}; }}
.callout.tip strong:first-child       {{ color: {p['purple']}; }}

/* ---------- Mermaid ---------- */
.mermaid-figure {{
    text-align: center; margin: 12pt 0; break-inside: avoid;
}}
.mermaid-figure img {{
    max-width: 100%;
    max-height: 220mm;
    height: auto;
}}
.mermaid-caption {{
    font-size: 8pt; color: {p['muted']}; font-style: italic; margin-top: 3pt;
}}
.mermaid-fallback {{
    border: 1px dashed {p['blue']};
    background: {p['blue_soft']};
    border-radius: 6px;
    padding: 8pt 11pt;
    margin: 8pt 0;
    break-inside: avoid;
}}
.mermaid-label {{
    font-style: normal; font-weight: 600; color: {p['blue']};
    margin-bottom: 4pt; font-size: 8.5pt;
}}
.mermaid-src {{
    background: #fff; border: 1px solid {p['border']}; border-left: none;
    font-size: 8pt; margin: 0; color: {p['ink_soft']};
}}

/* ---------- Table of contents list spacing ---------- */
.content > ol:first-of-type li {{ margin: 1.5pt 0; }}

/* ---------- Generated Table of Contents (index with page numbers) ---------- */
.toc {{
    break-after: page;
}}
.toc-title {{
    font-size: 18pt;
    color: {p['ink']};
    border-left: none;
    border-bottom: 3px solid {p['orange']};
    padding: 0 0 8pt 0;
    margin: 0 0 12pt 0;
}}
.toc-list {{
    list-style: none;
    margin: 0;
    padding: 0;
    counter-reset: toc-counter;
    font-size: 10pt;
}}
.toc-list li {{
    counter-increment: toc-counter;
    margin: 0;
    padding: 3.4pt 0;
    border-bottom: 1px dotted {p['border']};
    break-inside: avoid;
}}
.toc-list li a {{
    color: {p['text']};
    text-decoration: none;
}}
/* numbered prefix "1. 2. 3. ..." (re-numbered independently of heading text) */
.toc-list li a::before {{
    content: counter(toc-counter) ".\\00a0\\00a0";
    color: {p['orange_dark']};
    font-weight: 700;
}}
/* dotted leader stretching to the real, auto-resolved PDF page number */
.toc-list li a::after {{
    content: leader('. ') target-counter(attr(href url), page);
    color: {p['ink_soft']};
    font-weight: 600;
}}
"""


# --------------------------------------------------------------------------- #
# Cover page
# --------------------------------------------------------------------------- #
def build_cover(doc_title: str, doc_subtitle: str, section_count: int,
                accent_label: str = "") -> str:
    import datetime
    today = datetime.date.today().strftime("%B %Y")
    sections = f"{section_count} sections" if section_count else ""
    subtitle_html = (
        f'<div class="subtitle">{html.escape(doc_subtitle)}</div>'
        if doc_subtitle else ""
    )
    meta_bits = [b for b in ("Fundamentals → Production depth", sections, today) if b]
    meta_line = " &nbsp;·&nbsp; ".join(meta_bits)
    label = html.escape(accent_label) if accent_label else "Developer Guide"
    return f"""
<div class="cover">
  <div class="kicker">Technical Reference</div>
  <div class="bar"></div>
  <h1>{html.escape(doc_title)}</h1>
  {subtitle_html}
  <div class="meta">
    <div class="meta-row"><span class="accent">{label}</span></div>
    <div class="meta-row">{meta_line}</div>
  </div>
</div>
"""


# --------------------------------------------------------------------------- #
# Main conversion
# --------------------------------------------------------------------------- #
def convert(input_path: Path, output_path: Path, render_mermaid: bool,
            title_override: str | None = None,
            subtitle_override: str | None = None) -> None:
    markdown = _require("markdown")
    _require("pygments")
    weasyprint = _require("weasyprint")
    from pygments.formatters import HtmlFormatter

    print(f"Reading  : {input_path}")
    md_text = input_path.read_text(encoding="utf-8")

    # Derive cover/header metadata from the document itself (overridable).
    doc_title = title_override or extract_title(md_text, FALLBACK_TITLE)
    doc_subtitle = (subtitle_override
                    if subtitle_override is not None
                    else extract_subtitle(md_text, FALLBACK_SUBTITLE))
    section_count = count_sections(md_text)
    footer_label = short_footer_label(doc_title)
    print(f"Title    : {doc_title}")
    if doc_subtitle:
        print(f"Subtitle : {doc_subtitle}")
    print(f"Sections : {section_count}")

    print("Processing Mermaid diagrams ...")
    md_text = preprocess_mermaid(md_text, render_mermaid)

    print("Converting Markdown -> HTML ...")
    md = markdown.Markdown(
        extensions=[
            "extra",          # tables, fenced_code, attr_list, etc.
            "codehilite",     # Pygments syntax highlighting
            "sane_lists",
            "toc",
            "admonition",
        ],
        extension_configs={
            "codehilite": {
                "guess_lang": False,
                "noclasses": False,
                "pygments_style": PYGMENTS_STYLE,
            }
        },
        output_format="html5",
    )
    body_html = md.convert(md_text)

    print("Styling callouts ...")
    body_html = apply_callouts(body_html)

    print("Building table of contents (with page numbers) ...")
    toc_html = build_toc_html(body_html)
    body_html = replace_static_toc(body_html, toc_html)

    pygments_css = HtmlFormatter(style=PYGMENTS_STYLE).get_style_defs(".codehilite")
    css = build_css(pygments_css, doc_title, footer_label)

    full_html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>{html.escape(doc_title)}</title></head>
<body>
{build_cover(doc_title, doc_subtitle, section_count, footer_label)}
<div class="content">
{body_html}
</div>
</body>
</html>"""

    print(f"Rendering PDF (WeasyPrint) -> {output_path} ...")
    weasyprint.HTML(string=full_html, base_url=str(input_path.parent)).write_pdf(
        str(output_path), stylesheets=[weasyprint.CSS(string=css)]
    )

    size_kb = output_path.stat().st_size / 1024
    print(f"Done     : {output_path}  ({size_kb:.0f} KB)")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert a Markdown file to a professional PDF.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("input", nargs="?", default=DEFAULT_INPUT,
                        help="Input Markdown file.")
    parser.add_argument("output", nargs="?", default=None,
                        help="Output PDF file (default: <input>.pdf).")
    parser.add_argument("--render-mermaid", dest="render_mermaid",
                        action="store_true", default=True,
                        help="Render Mermaid diagrams to images using the "
                             "'mmdc' CLI (default: enabled).")
    parser.add_argument("--no-render-mermaid", dest="render_mermaid",
                        action="store_false",
                        help="Do not render Mermaid diagrams; show the diagram "
                             "source in a styled box instead.")
    parser.add_argument("--title", default=None,
                        help="Override the cover/header title (default: the "
                             "first '# ' heading in the Markdown).")
    parser.add_argument("--subtitle", default=None,
                        help="Override the cover subtitle (default: the first "
                             "'> ' blockquote line after the title).")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    ensure_native_libs()  # macOS: make Homebrew pango/cairo/glib loadable
    args = parse_args(argv)
    input_path = Path(args.input).expanduser().resolve()
    if not input_path.is_file():
        sys.exit(f"Input file not found: {input_path}")
    output_path = (
        Path(args.output).expanduser().resolve()
        if args.output
        else input_path.with_suffix(".pdf")
    )
    convert(input_path, output_path, args.render_mermaid,
            title_override=args.title, subtitle_override=args.subtitle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
