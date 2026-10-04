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

Optional, for rendering Mermaid diagrams to real images:
    npm install -g @mermaid-js/mermaid-cli      # provides the `mmdc` command

Usage
-----
    python convert_md_to_pdf.py                         # uses the default guide file
    python convert_md_to_pdf.py INPUT.md OUTPUT.pdf
    python convert_md_to_pdf.py INPUT.md --render-mermaid
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
DOC_TITLE = "AWS SDE2 — Java Backend Developer Complete Guide"
DOC_SUBTITLE = "A production-oriented AWS reference for Java / Spring Boot engineers"

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
# Mermaid handling
# --------------------------------------------------------------------------- #
_MERMAID_BLOCK = re.compile(r"```mermaid[ \t]*\n(.*?)```", re.DOTALL)


def _mmdc_available() -> bool:
    return shutil.which("mmdc") is not None


def _render_mermaid_to_svg(source: str) -> str | None:
    """Render a single Mermaid diagram to an inline SVG string using mmdc."""
    try:
        with tempfile.TemporaryDirectory() as tmp:
            in_path = Path(tmp) / "d.mmd"
            out_path = Path(tmp) / "d.svg"
            in_path.write_text(source, encoding="utf-8")
            subprocess.run(
                ["mmdc", "-i", str(in_path), "-o", str(out_path),
                 "-b", "transparent"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            svg = out_path.read_text(encoding="utf-8")
            # strip XML prolog so it embeds cleanly inside HTML
            svg = re.sub(r"<\?xml.*?\?>", "", svg, flags=re.DOTALL).strip()
            return svg
    except Exception:
        return None


def preprocess_mermaid(md_text: str, render: bool) -> str:
    """Replace ```mermaid blocks with HTML figures (SVG if possible, else source)."""
    use_cli = render and _mmdc_available()
    if render and not use_cli:
        print("  note: --render-mermaid set but 'mmdc' not found on PATH; "
              "falling back to styled diagram source.", file=sys.stderr)

    def repl(match: re.Match) -> str:
        source = match.group(1).rstrip("\n")
        if use_cli:
            svg = _render_mermaid_to_svg(source)
            if svg:
                # Embed as a data URI <img> for reliable sizing in WeasyPrint.
                b64 = base64.b64encode(svg.encode("utf-8")).decode("ascii")
                return (
                    '\n<div class="mermaid-figure">'
                    f'<img alt="diagram" src="data:image/svg+xml;base64,{b64}"/>'
                    '<div class="mermaid-caption">Diagram</div></div>\n'
                )
        # Fallback: styled source box
        escaped = html.escape(source)
        return (
            '\n<div class="mermaid-fallback">'
            '<div class="mermaid-label">◆ Diagram (Mermaid)</div>'
            f'<pre class="mermaid-src">{escaped}</pre></div>\n'
        )

    return _MERMAID_BLOCK.sub(repl, md_text)


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
# CSS
# --------------------------------------------------------------------------- #
def build_css(pygments_css: str) -> str:
    p = PALETTE
    return f"""
/* ---------- Pygments syntax highlighting ---------- */
{pygments_css}

/* ---------- Page setup ---------- */
@page {{
    size: A4;
    margin: 20mm 16mm 18mm 16mm;
    @top-right {{
        content: "{DOC_TITLE}";
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
        content: "AWS SDE2 Guide";
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
    background: linear-gradient(150deg, {p['ink']} 0%, {p['ink_soft']} 55%, #2E4053 100%);
    color: #fff;
    padding: 48mm 22mm 20mm 22mm;
    box-sizing: border-box;
    position: relative;
}}
.cover .bar {{
    width: 70mm; height: 6px; background: {p['orange']};
    border-radius: 3px; margin-bottom: 14mm;
}}
.cover h1 {{
    font-size: 30pt; line-height: 1.15; margin: 0 0 8mm 0;
    color: #fff; border: none; padding: 0;
}}
.cover .subtitle {{
    font-size: 13pt; color: #D5DBDB; font-weight: 400; margin-bottom: 24mm;
}}
.cover .meta {{
    position: absolute; bottom: 22mm; left: 22mm; right: 22mm;
    font-size: 9.5pt; color: #AEB6BF;
    border-top: 1px solid rgba(255,255,255,0.25); padding-top: 6mm;
}}
.cover .meta .accent {{ color: {p['orange']}; font-weight: 600; }}

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
    text-align: center; margin: 10pt 0; break-inside: avoid;
}}
.mermaid-figure img {{ max-width: 100%; }}
.mermaid-caption, .mermaid-label {{
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
"""


# --------------------------------------------------------------------------- #
# Cover page
# --------------------------------------------------------------------------- #
def build_cover() -> str:
    import datetime
    today = datetime.date.today().strftime("%B %Y")
    return f"""
<div class="cover">
  <div class="bar"></div>
  <h1>{html.escape(DOC_TITLE)}</h1>
  <div class="subtitle">{html.escape(DOC_SUBTITLE)}</div>
  <div class="meta">
    <span class="accent">Technical Reference</span> &nbsp;·&nbsp;
    Fundamentals → Production depth &nbsp;·&nbsp;
    63 sections &nbsp;·&nbsp; {today}
  </div>
</div>
"""


# --------------------------------------------------------------------------- #
# Main conversion
# --------------------------------------------------------------------------- #
def convert(input_path: Path, output_path: Path, render_mermaid: bool) -> None:
    markdown = _require("markdown")
    _require("pygments")
    weasyprint = _require("weasyprint")
    from pygments.formatters import HtmlFormatter

    print(f"Reading  : {input_path}")
    md_text = input_path.read_text(encoding="utf-8")

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

    pygments_css = HtmlFormatter(style=PYGMENTS_STYLE).get_style_defs(".codehilite")
    css = build_css(pygments_css)

    full_html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>{html.escape(DOC_TITLE)}</title></head>
<body>
{build_cover()}
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
    parser.add_argument("--render-mermaid", action="store_true",
                        help="Render Mermaid diagrams to images using the "
                             "'mmdc' CLI if available.")
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
    convert(input_path, output_path, args.render_mermaid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
