#!/usr/bin/env python3
"""
notebook_to_pdf.py
-------------------
Convert a Jupyter notebook (.ipynb) into a single, clean PDF that looks like
a standard notebook export: In[]/Out[] prompts, monospace syntax-highlighted
code, wrapped lines (no cropping), embedded images/plots, and rendered
markdown -- on one A4-sized PDF.

USAGE
    python notebook_to_pdf.py path/to/notebook.ipynb
    python notebook_to_pdf.py path/to/notebook.ipynb -o output.pdf
    python notebook_to_pdf.py path/to/notebook.ipynb -o output.pdf --title "My Notebook"

One command does the whole job: if any markdown cell has an externally
linked image (e.g. <img src="https://..."> pointing at Google Drive or a
CDN), it's fetched and baked directly into the notebook as embedded base64
data first -- the same way an image looks when pasted straight into a
Jupyter cell -- and an updated, fully self-contained "<name>_embedded.ipynb"
is saved alongside the PDF. Anything that can't be fetched (dead link, or a
link that needs sign-in) is left untouched in that file and shown as a
clearly labelled placeholder in the PDF, never silently dropped. Pass
--no-embed-images to skip this and convert the notebook as-is.

Symbols, math and emoji are handled automatically: LaTeX such as $\\rightarrow$,
$\\alpha$, $x_i^2$, $\\frac{a}{b}$ becomes readable symbols with real super/subscripts;
Greek letters, arrows, math signs, check marks and box-drawing characters use a
font already installed on your computer; emoji are drawn as colour pictures
(needs internet; offline they fall back to plain symbols). Options:
--symbol-font PATH (force a .ttf font) and --no-emoji-images (never download).

DEPENDENCIES (install once -- pure Python, no system libraries needed)
    pip install pygments markdown xhtml2pdf

This script only needs the standard library to read the .ipynb file itself
(it's plain JSON), and uses xhtml2pdf (a pure-Python, reportlab-based PDF
writer) instead of WeasyPrint, so it works the same way on Windows, macOS,
and Linux -- no GTK/Pango/Cairo system libraries required.
"""

import argparse
import base64
import glob
import html
import json
import logging
import math
import mimetypes
import os
import re
import sys
import tempfile
import textwrap
import unicodedata
import urllib.error
import urllib.request
from html.parser import HTMLParser

# Max characters per code/output line before we manually wrap it. This is
# enforced in Python (not left to the PDF engine's CSS support), so long
# unbroken strings/URLs/hashes can never run off the page.
WRAP_WIDTH = 92

# The PDF engine (xhtml2pdf/reportlab) silently drops a numbered list item's
# own marker whenever that item starts with inline-formatted text (e.g. code
# spans) followed by a nested sub-list -- while still counting it for later
# items, so numbering looks skipped/wrong (e.g. "4, [blank], 6" instead of
# "4, 5, 6"). To make numbering always correct regardless of that engine
# quirk, ordered/unordered lists are re-rendered below as manually indented,
# manually numbered <div> blocks instead of native <ol>/<li> markup.
_VOID_TAGS = {"br", "img", "hr"}


class _HtmlTreeBuilder(HTMLParser):
    """Build a minimal node tree ({'tag','attrs','children'}) from an HTML
    fragment, so lists can be located and re-rendered regardless of nesting."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = {"tag": None, "attrs": {}, "children": []}
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = {"tag": tag, "attrs": dict(attrs), "children": []}
        self.stack[-1]["children"].append(node)
        if tag not in _VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1]["children"].append({"tag": tag, "attrs": dict(attrs), "children": []})

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i]["tag"] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1]["children"].append(data)


def _esc_attr(v):
    return html.escape(str(v), quote=True)


class _Raw(str):
    """Text that is already valid HTML and must not be escaped again."""


def _serialize(node_or_children):
    if isinstance(node_or_children, str):
        # The parser un-escapes text, so escape it again here; otherwise a
        # plain "x<y and y>z" in a note is mistaken for a tag and swallowed.
        if isinstance(node_or_children, _Raw):
            return node_or_children
        return html.escape(node_or_children, quote=False)
    if isinstance(node_or_children, list):
        return "".join(_serialize(c) for c in node_or_children)

    node = node_or_children
    tag = node["tag"]
    if tag in ("ol", "ul"):
        return _render_list_as_divs(node, depth=0)

    attrs = "".join(f' {k}="{_esc_attr(v)}"' for k, v in node["attrs"].items())
    inner = "".join(_serialize(c) for c in node["children"])
    if tag in _VOID_TAGS:
        return f"<{tag}{attrs}/>"
    return f"<{tag}{attrs}>{inner}</{tag}>"


def _render_list_as_divs(list_node, depth):
    is_ordered = list_node["tag"] == "ol"
    counter = int(list_node["attrs"].get("start", 1))
    indent = 14 + depth * 16
    out = []
    for child in list_node["children"]:
        if not isinstance(child, dict) or child["tag"] != "li":
            continue
        inline_parts, nested_html = [], []
        for sub in child["children"]:
            if isinstance(sub, dict) and sub["tag"] in ("ol", "ul"):
                nested_html.append(_render_list_as_divs(sub, depth + 1))
            else:
                inline_parts.append(_serialize(sub))
        content = "".join(inline_parts).strip()
        marker = f"{counter}." if is_ordered else "&bull;"
        out.append(
            f'<div class="list-item" style="margin-left:{indent}pt; text-indent:-14pt;">'
            f'<span class="list-marker">{marker}</span> {content}</div>'
        )
        out.extend(nested_html)
        if is_ordered:
            counter += 1
    return "".join(out)


def fix_lists(html_fragment):
    """Re-render any ol/ul lists in an HTML fragment as manually numbered
    divs, working around a PDF-engine bug that drops list markers."""
    builder = _HtmlTreeBuilder()
    builder.feed(html_fragment)
    return _serialize(builder.root["children"])


# ---------------------------------------------------------------------------
# Image embedding for markdown cells. Markdown cells can reference images
# three ways: (1) already embedded as a data: URI -- nothing to do; (2) a
# remote http(s) URL (pasted as an <img src="https://..."> tag, or markdown
# image syntax) -- fetched here and embedded, instead of relying on the PDF
# engine's own built-in fetcher, which uses no User-Agent header and fails
# against image hosts with hotlink protection; (3) a local file path
# relative to the notebook -- read directly from disk. Any image that still
# can't be resolved gets a clearly visible placeholder in the PDF (with the
# URL and the reason) instead of silently disappearing.
# ---------------------------------------------------------------------------

_IMAGE_FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "image/*,*/*;q=0.8",
}


def _fetch_remote_image(url, timeout=15):
    """Download an external image. Returns (mime, base64_str) on success,
    or (None, reason_string) on failure. Never raises."""
    try:
        req = urllib.request.Request(url, headers=_IMAGE_FETCH_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
            data = resp.read()
    except urllib.error.HTTPError as e:
        return None, f"server returned HTTP {e.code}"
    except urllib.error.URLError as e:
        return None, f"could not connect ({e.reason})"
    except Exception as e:
        return None, str(e)

    if content_type and not content_type.startswith("image/"):
        # Most often this means the link needs sign-in/isn't actually public
        # (e.g. a Google Drive link that isn't shared as "Anyone with the
        # link") and the server returned an HTML page instead of the image.
        return None, f"URL did not return image data (got '{content_type}' -- link may require sign-in or isn't public)"
    if not data:
        return None, "server returned an empty response"

    mime = content_type or mimetypes.guess_type(url)[0] or "image/png"
    return mime, base64.b64encode(data).decode("ascii")


def _load_local_image(path, base_dir):
    """Read a local image file (path may be relative to the notebook's own
    directory). Returns (mime, base64_str) on success, or (None, reason)."""
    candidate = path if os.path.isabs(path) else os.path.join(base_dir, path)
    if not os.path.isfile(candidate):
        return None, f"local file not found: {candidate}"
    mime = mimetypes.guess_type(candidate)[0] or "image/png"
    try:
        with open(candidate, "rb") as f:
            data = f.read()
    except Exception as e:
        return None, f"could not read local file ({e})"
    return mime, base64.b64encode(data).decode("ascii")


def _resolve_images(node, base_dir, cache):
    """Walk the tree in place, turning any <img> node's src into an embedded
    data: URI (fetched remotely or read locally as needed) or, on failure,
    rewriting that node into a visible placeholder explaining why."""
    if not isinstance(node, dict):
        return
    tag = node.get("tag")

    if tag == "img":
        src = node.get("attrs", {}).get("src", "")
        if not src or src.startswith("data:"):
            pass  # already embedded, or nothing to resolve
        else:
            if src in cache:
                mime, payload = cache[src]
            elif src.startswith("http://") or src.startswith("https://"):
                mime, payload = _fetch_remote_image(src)
                cache[src] = (mime, payload)
            else:
                mime, payload = _load_local_image(src, base_dir)
                cache[src] = (mime, payload)

            if mime:
                node["attrs"] = {"src": f"data:{mime};base64,{payload}", "class": "md-image"}
            else:
                reason = payload
                node.clear()
                node.update({
                    "tag": "div",
                    "attrs": {"class": "missing-image"},
                    "children": [
                        _Raw('<span class="missing-image-title">[!] Image not embedded</span><br/>'),
                        _Raw(f"Source: {esc(src)}<br/>"),
                        _Raw(f"Reason: {esc(reason)}"),
                    ],
                })
        return  # img has no children to recurse into

    for child in node.get("children", []):
        _resolve_images(child, base_dir, cache)


def render_markdown_html(html_fragment, base_dir, image_cache):
    """Full markdown-cell post-processing: fix list numbering, then resolve
    every image (remote fetch / local read / already-embedded) in place."""
    builder = _HtmlTreeBuilder()
    builder.feed(html_fragment)
    builder.close()
    _resolve_images(builder.root, base_dir, image_cache)
    return _serialize(builder.root["children"])


class _ImgSrcCollector(HTMLParser):
    """Collect every http(s) <img src=...> value from rendered HTML, however
    the attribute was written (single-quoted, double-quoted, unquoted, with
    or without spaces around '='). A plain regex can't reliably cover all of
    that -- a real parser handles every variant by construction."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.srcs = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "img":
            src = dict(attrs).get("src", "")
            if src.startswith("http://") or src.startswith("https://"):
                self.srcs.append(src)


def find_remote_image_urls(markdown_source, markdown_mod):
    """Render markdown to HTML (so both ![]() syntax and raw <img> tags are
    normalized the same way) and return every externally-linked image URL
    found in it, in order, duplicates included."""
    try:
        html_out = markdown_mod.markdown(markdown_source, extensions=["fenced_code", "tables"])
    except Exception:
        return []
    collector = _ImgSrcCollector()
    collector.feed(html_out)
    return collector.srcs


def bake_images_into_notebook(nb, base_dir, image_cache, markdown_mod, log=None):
    """Permanently rewrite every externally-linked image in the notebook's
    markdown cells (e.g. <img src="https://..."> pointing at Google Drive or
    a CDN, in any attribute-quoting style, or markdown's ![]() syntax) into
    embedded base64 data, in place in `nb["cells"]` -- the same durable form
    a pasted-in image already takes. Images that can't be fetched are left
    exactly as they were (untouched, still a live link) so nothing is lost;
    the PDF step still shows a clear placeholder for those. Uses (and
    populates) the same `image_cache` the PDF-rendering step uses, so a
    successfully baked image is never fetched a second time.
    Returns a list of (cell_index, url, "embedded"|"failed", detail).
    """
    results = []
    for i, cell in enumerate(nb.get("cells", [])):
        if cell.get("cell_type") != "markdown":
            continue
        src = as_text(cell.get("source"))
        urls = find_remote_image_urls(src, markdown_mod)
        if not urls:
            continue

        new_src = src
        for url in urls:
            if url not in image_cache:
                if log:
                    log(f"  Fetching cell {i}: {url}")
                image_cache[url] = _fetch_remote_image(url)
            mime, payload = image_cache[url]
            if mime:
                if url in new_src:  # guard: a url markdown rendered but the raw source doesn't literally contain is left alone
                    new_src = new_src.replace(url, f"data:{mime};base64,{payload}")
                    results.append((i, url, "embedded", f"{len(payload)} base64 chars"))
            else:
                results.append((i, url, "failed", payload))

        if new_src != src:
            lines = new_src.splitlines(keepends=True)
            cell["source"] = lines if lines else [new_src]

    return results


def _require(module_name, pip_name=None):
    """Import a dependency, or exit with a clear install instruction."""
    pip_name = pip_name or module_name
    try:
        return __import__(module_name)
    except ImportError:
        sys.stderr.write(
            f"\nMissing dependency: '{module_name}'.\n"
            f"Install it with:\n    pip install {pip_name}\n\n"
        )
        sys.exit(1)


def load_notebook(path):
    """Read a .ipynb file (plain JSON) and return the full parsed notebook
    (not just its cells), so it can be rewritten later with images baked
    in while preserving metadata/nbformat untouched."""
    if not os.path.isfile(path):
        sys.stderr.write(f"Error: notebook file not found: {path}\n")
        sys.exit(1)
    try:
        with open(path, "r", encoding="utf-8") as f:
            nb = json.load(f)
    except json.JSONDecodeError as e:
        sys.stderr.write(f"Error: '{path}' is not valid .ipynb/JSON ({e}).\n")
        sys.exit(1)
    if not nb.get("cells"):
        sys.stderr.write("Warning: notebook has no cells; PDF will be title-only.\n")
    return nb


def as_text(source):
    """Notebook 'source' fields may be a list of lines or a single string."""
    if source is None:
        return ""
    if isinstance(source, list):
        return "".join(source)
    return str(source)


def strip_ansi(text):
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def esc(text):
    return html.escape(text, quote=False)


def wrap_text_block(text, width=WRAP_WIDTH):
    """
    Hard-wrap every line of a text block to `width` characters, so nothing
    can overflow the page regardless of the PDF engine's own line-wrapping
    support. Preserves blank lines and existing indentation.
    """
    out_lines = []
    for line in text.split("\n"):
        if len(line) <= width:
            out_lines.append(line)
            continue
        wrapped = textwrap.wrap(
            line,
            width=width,
            break_long_words=True,
            break_on_hyphens=False,
            replace_whitespace=False,
            drop_whitespace=False,
        )
        out_lines.extend(wrapped if wrapped else [""])
    return "\n".join(out_lines)


# ---------------------------------------------------------------------------
# Math ($...$, $$...$$, \(...\), \[...\], \begin{align}...) -> readable text.
# Jupyter draws these with MathJax; a PDF built from plain HTML can't, so each
# expression is converted to Unicode symbols with real <sup>/<sub> scripts.
# ---------------------------------------------------------------------------
class _QuietEngineNoise(logging.Filter):
    """Drops three harmless, repetitive messages the PDF engine prints (one per
    character for symbols it can't draw, plus a CSS note). Our own summary
    already reports any character that really can't be drawn; every other
    engine message is left alone."""

    _DROP = ("No font this document has can draw", "None of the font families",
             "Ignoring CSS properties")

    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            return True
        return not any(k in msg for k in self._DROP)


def _quiet_engine_noise():
    for name in ("xhtml2pdf", "xhtml2pdf.context", "xhtml2pdf.parser", "xhtml2pdf.tags", "xhtml2pdf.util"):
        lg = logging.getLogger(name)
        if not any(isinstance(f, _QuietEngineNoise) for f in lg.filters):
            lg.addFilter(_QuietEngineNoise())


_LATEX_SYMBOLS = {
    # greek
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε", "varepsilon": "ε",
    "zeta": "ζ", "eta": "η", "theta": "θ", "vartheta": "ϑ", "iota": "ι", "kappa": "κ",
    "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ", "omicron": "ο", "pi": "π", "varpi": "ϖ",
    "rho": "ρ", "varrho": "ϱ", "sigma": "σ", "varsigma": "ς", "tau": "τ", "upsilon": "υ",
    "phi": "ϕ", "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π",
    "Sigma": "Σ", "Upsilon": "Υ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
    # arrows
    "rightarrow": "→", "to": "→", "leftarrow": "←", "gets": "←", "leftrightarrow": "↔",
    "Rightarrow": "⇒", "Leftarrow": "⇐", "Leftrightarrow": "⇔", "implies": "⟹",
    "impliedby": "⟸", "iff": "⟺", "uparrow": "↑", "downarrow": "↓", "updownarrow": "↕",
    "Uparrow": "⇑", "Downarrow": "⇓", "mapsto": "↦", "longmapsto": "⟼",
    "longrightarrow": "⟶", "longleftarrow": "⟵", "Longrightarrow": "⟹",
    "Longleftarrow": "⟸", "longleftrightarrow": "⟷", "Longleftrightarrow": "⟺",
    "hookrightarrow": "↪", "hookleftarrow": "↩", "nearrow": "↗", "searrow": "↘",
    "swarrow": "↙", "nwarrow": "↖", "rightharpoonup": "⇀", "leftharpoonup": "↼",
    "rightleftharpoons": "⇌", "leadsto": "⇝",
    # relations
    "le": "≤", "leq": "≤", "ge": "≥", "geq": "≥", "ne": "≠", "neq": "≠", "approx": "≈",
    "sim": "∼", "simeq": "≃", "cong": "≅", "equiv": "≡", "propto": "∝", "ll": "≪",
    "gg": "≫", "prec": "≺", "succ": "≻", "subset": "⊂", "supset": "⊃", "subseteq": "⊆",
    "supseteq": "⊇", "nsubseteq": "⊈", "in": "∈", "notin": "∉", "ni": "∋", "perp": "⊥",
    "parallel": "∥", "mid": "∣", "nmid": "∤", "asymp": "≍", "doteq": "≐", "models": "⊨",
    "vdash": "⊢", "dashv": "⊣", "triangleq": "≜",
    # operators
    "times": "×", "div": "÷", "cdot": "·", "pm": "±", "mp": "∓", "ast": "∗", "star": "⋆",
    "circ": "∘", "bullet": "•", "oplus": "⊕", "ominus": "⊖", "otimes": "⊗", "oslash": "⊘",
    "odot": "⊙", "cup": "∪", "cap": "∩", "wedge": "∧", "land": "∧", "vee": "∨", "lor": "∨",
    "neg": "¬", "lnot": "¬", "setminus": "∖", "sqcup": "⊔", "sqcap": "⊓", "uplus": "⊎",
    "dagger": "†", "ddagger": "‡", "dag": "†", "ddag": "‡",
    # big operators
    "sum": "∑", "prod": "∏", "coprod": "∐", "int": "∫", "iint": "∬", "iiint": "∭",
    "oint": "∮", "bigcup": "⋃", "bigcap": "⋂", "bigvee": "⋁", "bigwedge": "⋀",
    "bigoplus": "⨁", "bigotimes": "⨂",
    # misc
    "infty": "∞", "partial": "∂", "nabla": "∇", "forall": "∀", "exists": "∃",
    "nexists": "∄", "emptyset": "∅", "varnothing": "∅", "ldots": "…", "dots": "…",
    "cdots": "⋯", "vdots": "⋮", "ddots": "⋱", "prime": "′", "angle": "∠", "degree": "°",
    "ell": "ℓ", "hbar": "ℏ", "Re": "ℜ", "Im": "ℑ", "aleph": "ℵ", "therefore": "∴",
    "because": "∵", "square": "□", "Box": "□", "blacksquare": "■", "triangle": "△",
    "checkmark": "✓", "surd": "√", "top": "⊤", "bot": "⊥", "imath": "ı", "jmath": "ȷ",
    "wp": "℘", "flat": "♭", "natural": "♮", "sharp": "♯", "clubsuit": "♣",
    "diamondsuit": "♢", "heartsuit": "♡", "spadesuit": "♠", "lozenge": "◊", "diamond": "⋄",
    "S": "§", "P": "¶", "copyright": "©", "pounds": "£", "euro": "€",
    "lbrace": "{", "rbrace": "}", "langle": "⟨", "rangle": "⟩", "lfloor": "⌊",
    "rfloor": "⌋", "lceil": "⌈", "rceil": "⌉", "vert": "|", "Vert": "‖", "lvert": "|",
    "rvert": "|", "lVert": "‖", "rVert": "‖", "backslash": "\\", "textbackslash": "\\",
    "lt": "&lt;", "gt": "&gt;",
}
_LATEX_BB = {"R": "ℝ", "N": "ℕ", "Z": "ℤ", "Q": "ℚ", "C": "ℂ", "H": "ℍ", "P": "ℙ"}
_LATEX_CAL = {"B": "ℬ", "E": "ℰ", "F": "ℱ", "H": "ℋ", "I": "ℐ", "L": "ℒ", "M": "ℳ", "R": "ℛ"}
_LATEX_ACCENTS = {
    "hat": "\u0302", "widehat": "\u0302", "tilde": "\u0303", "widetilde": "\u0303",
    "bar": "\u0304", "overline": "\u0305", "vec": "\u20d7", "dot": "\u0307",
    "ddot": "\u0308", "check": "\u030c", "breve": "\u0306", "acute": "\u0301",
    "grave": "\u0300", "mathring": "\u030a", "underline": "\u0332",
}
_LATEX_TEXT_CMDS = {"text", "textrm", "textnormal", "mbox", "hbox", "textsf", "texttt"}
_LATEX_NOOPS = {"limits", "nolimits", "displaystyle", "textstyle", "scriptstyle",
                "scriptscriptstyle", "nonumber", "notag", "mathstrut", "strut"}
_LATEX_SWALLOW1 = {"label", "tag", "hspace", "vspace", "phantom", "vphantom", "hphantom"}
_LATEX_PASS1 = {"mathrm", "mathsf", "mathtt", "mathnormal", "operatorname", "mathbin",
                "mathrel", "mathop", "mathord", "mathpunct", "mathopen", "mathclose",
                "underbrace", "overbrace", "cancel", "bcancel", "xcancel", "boxed", "fbox",
                "mathfrak", "mathscr", "mathbbm"}
_LATEX_BOLD1 = {"mathbf", "boldsymbol", "bm", "textbf", "pmb"}
_LATEX_ITAL1 = {"mathit", "textit", "emph"}


class _Latex:
    """Tiny recursive converter from a LaTeX math string to an HTML fragment."""

    def __init__(self, src):
        self.s = src
        self.n = len(src)
        self.i = 0

    def _peek(self):
        return self.s[self.i] if self.i < self.n else ""

    def _skip_ws(self):
        while self.i < self.n and self.s[self.i].isspace():
            self.i += 1

    def _raw_group(self):
        """Return the raw text of the next {group} (or the next single token)."""
        self._skip_ws()
        if self._peek() == "{":
            depth, j = 0, self.i
            while j < self.n:
                c = self.s[j]
                if c == "\\":
                    j += 2
                    continue
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            inner = self.s[self.i + 1:j]
            self.i = min(j + 1, self.n)
            return inner
        if self._peek() == "\\":
            m = re.match(r"\\[A-Za-z]+|\\.", self.s[self.i:])
            tok = m.group(0) if m else "\\"
            self.i += len(tok)
            return tok
        tok = self._peek()
        if tok:
            self.i += 1
        return tok

    def _arg(self):
        return _Latex(self._raw_group()).parse()

    @staticmethod
    def _simple(h):
        plain = re.sub(r"<[^>]+>|&[a-z]+;", "", h)
        return bool(plain) and re.fullmatch(r"[\w.,°′∞]+", plain) is not None

    def parse(self):
        out = []
        while self.i < self.n:
            c = self.s[self.i]
            if c == "\\":
                out.append(self._command())
            elif c == "{":
                out.append(_Latex(self._raw_group()).parse())
            elif c == "}":
                self.i += 1
            elif c in "^_":
                self.i += 1
                tag = "sup" if c == "^" else "sub"
                arg = self._arg()
                arg = re.sub(r"<sup>(.*?)</sup>", r"^\1", arg)   # no nested <sup> in the PDF engine
                arg = re.sub(r"<sub>(.*?)</sub>", r"_\1", arg)
                out.append(f"<{tag}>{arg}</{tag}>")
            elif c in "~&":
                out.append(" ")
                self.i += 1
            elif c.isspace():
                while self.i < self.n and self.s[self.i].isspace():
                    self.i += 1
                out.append(" ")
            else:
                out.append(html.escape(c, quote=False))
                self.i += 1
        return "".join(out)

    def _control_symbol(self, ch):
        if ch == "\\":
            return "<br/>"
        if ch in ",;:> ":
            return " "
        if ch in "!-/'\"^`.=":
            return ""
        if ch == "|":
            return "‖"
        return html.escape(ch, quote=False)

    def _command(self):
        m = re.match(r"\\([A-Za-z]+)", self.s[self.i:])
        if not m:
            self.i += 1
            ch = self._peek()
            if ch:
                self.i += 1
            return self._control_symbol(ch)
        name = m.group(1)
        self.i += len(m.group(0))

        if name in ("frac", "dfrac", "tfrac", "cfrac"):
            a, b = self._arg(), self._arg()
            if not self._simple(a):
                a = f"({a})"
            if not self._simple(b):
                b = f"({b})"
            return f"{a}/{b}"
        if name == "binom":
            a, b = self._arg(), self._arg()
            return f"C({a}, {b})"
        if name == "sqrt":
            self._skip_ws()
            idx = ""
            if self._peek() == "[":
                j = self.s.find("]", self.i)
                if j > 0:
                    idx = self.s[self.i + 1:j]
                    self.i = j + 1
            a = self._arg()
            core = "√" + (a if self._simple(a) else f"({a})")
            return (f"<sup>{_Latex(idx).parse()}</sup>" + core) if idx else core
        if name in _LATEX_TEXT_CMDS:
            return html.escape(self._raw_group(), quote=False)
        if name in _LATEX_BOLD1:
            return f"<b>{self._arg()}</b>"
        if name in _LATEX_ITAL1:
            return f"<i>{self._arg()}</i>"
        if name in _LATEX_PASS1:
            return self._arg()
        if name in ("mathbb", "Bbb"):
            return "".join(_LATEX_BB.get(ch, html.escape(ch)) for ch in self._raw_group())
        if name in ("mathcal", "cal"):
            return "".join(_LATEX_CAL.get(ch, html.escape(ch)) for ch in self._raw_group())
        if name in _LATEX_ACCENTS:
            a = self._arg()
            if "<" in a or "&" in a or len(a) > 8:
                return a
            mark = _LATEX_ACCENTS[name]
            return "".join(c if c.isspace() else unicodedata.normalize("NFC", c + mark) for c in a)
        if name in ("left", "right") or name.startswith(("big", "Big")) and name.rstrip("lrm") in ("big", "Big", "bigg", "Bigg"):
            if self._peek() == ".":
                self.i += 1
            return ""
        if name in _LATEX_NOOPS:
            return ""
        if name in _LATEX_SWALLOW1:
            self._raw_group()
            return ""
        if name in ("quad", "enspace"):
            return "&nbsp;&nbsp;"
        if name == "qquad":
            return "&nbsp;&nbsp;&nbsp;&nbsp;"
        if name == "pmod":
            return f" (mod {self._arg()})"
        if name in ("bmod", "mod"):
            return " mod "
        if name == "begin":
            env = self._raw_group().strip()
            end = re.compile(r"\\end\{" + re.escape(env) + r"\}").search(self.s, self.i)
            body = self.s[self.i:end.start()] if end else self.s[self.i:]
            self.i = end.end() if end else self.n
            return self._environment(env.rstrip("*"), body)
        if name == "end":
            self._raw_group()
            return ""
        if name in _LATEX_SYMBOLS:
            return _LATEX_SYMBOLS[name]
        # unknown command / function name (sin, log, argmax, ...): show its name
        rest = self.s[self.i:self.i + 1]
        return name + (" " if rest and (rest.isalnum() or rest == "\\") else "")

    def _environment(self, base, body):
        body = re.sub(r"^\s*\{[^}]*\}", "", body) if base == "array" else body
        rows = [r.strip() for r in re.split(r"\\\\(?:\[[^\]]*\])?", body)]
        while rows and not rows[-1]:
            rows.pop()

        def cells(r):
            return [_Latex(c.strip()).parse() for c in re.split(r"(?<!\\)&", r)]

        brackets = {"pmatrix": ("(", ")"), "bmatrix": ("[", "]"), "Bmatrix": ("{", "}"),
                    "vmatrix": ("|", "|"), "Vmatrix": ("‖", "‖")}
        if base in brackets or base in ("matrix", "smallmatrix", "array"):
            left, right = brackets.get(base, ("", ""))
            return left + "; ".join(", ".join(cells(r)) for r in rows) + right
        if base == "cases":
            return "{ " + "; ".join(", ".join(cells(r)) for r in rows) + " }"
        return "<br/>".join(" ".join(cells(r)) for r in rows)


def latex_to_html(expr):
    """LaTeX math string -> HTML fragment (never raises)."""
    try:
        return _Latex(expr.strip()).parse().strip()
    except Exception:
        return html.escape(expr, quote=False)


_CODE_SPAN_RE = re.compile(r"```.*?```|~~~.*?~~~|``[^\n]+?``|`[^`\n]+`", re.S)
_MATH_DISPLAY_RE = re.compile(
    r"\$\$(.+?)\$\$|\\\[(.+?)\\\]|"
    r"\\begin\{(equation|align|gather|multline|eqnarray|displaymath)(\*?)\}.+?\\end\{\3\4\}",
    re.S)
_MATH_INLINE_RE = re.compile(
    r"\\\((.+?)\\\)|(?<![\\$])\$(?!\s)((?:\\.|[^$\\\n])+?)(?<!\s)\$(?!\d)")


def protect_math(text):
    """Swap every math span for a placeholder so the markdown library can't
    mangle it (it would turn x_i + x_j into italics). Code spans/blocks are
    left alone. Returns (text, store) for restore_math()."""
    if not any(k in text for k in ("$", "\\(", "\\[", "\\begin")):
        return text, []
    held, store = [], []

    def hold(m):
        held.append(m.group(0))
        return f"zzcodezz{len(held) - 1}zz"

    def put(expr, display):
        store.append((latex_to_html(expr), display))
        return f"zzmathzz{len(store) - 1}zz"

    t = _CODE_SPAN_RE.sub(hold, text)
    t = _MATH_DISPLAY_RE.sub(
        lambda m: put(m.group(1) if m.group(1) is not None else
                      (m.group(2) if m.group(2) is not None else m.group(0)), True), t)
    def inline(m):
        expr = m.group(1) if m.group(1) is not None else m.group(2)
        # "$10, or "$HOME"" has plain words between the dollar signs: leave it as text
        if m.group(1) is None and re.search(r"\s", expr.strip()) and not re.search(r"[\\^_{}=+*/<>|()\[\]-]", expr):
            return m.group(0)
        return put(expr, False)

    t = _MATH_INLINE_RE.sub(inline, t)
    t = re.sub(r"zzcodezz(\d+)zz", lambda m: held[int(m.group(1))], t)
    return t, store


def restore_math(rendered_html, store):
    if not store:
        return rendered_html
    rendered_html = re.sub(
        r"<p>\s*zzmathzz(\d+)zz\s*</p>",
        lambda m: (f'<p class="math-display">{store[int(m.group(1))][0]}</p>'
                   if store[int(m.group(1))][1] else m.group(0)),
        rendered_html)
    return re.sub(r"zzmathzz(\d+)zz",
                  lambda m: f'<span class="math">{store[int(m.group(1))][0]}</span>',
                  rendered_html)


# ---------------------------------------------------------------------------
# Symbols, fonts and emoji.
# The PDF's built-in Helvetica/Courier only contain basic Western characters,
# so anything else (Greek, arrows, math signs, check marks, box-drawing,
# emoji ...) would print as a blank or a black box. Fix: find a font that is
# already installed on this computer and covers each such character, and
# draw emoji as small colour pictures (Twemoji), with a font fallback offline.
# ---------------------------------------------------------------------------
_PROP_FONTS = [  # proportional text: first match that covers a character wins
    "arial.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf", "seguisym.ttf",
    "Arial Unicode.ttf", "arialuni.ttf", "Apple Symbols.ttf", "segoeui.ttf",
    "NotoSansSymbols2-Regular.ttf", "NotoSansSymbols-Regular.ttf",
    "NotoSansMath-Regular.ttf", "FreeSans.ttf", "Symbola.ttf", "NotoEmoji-Regular.ttf",
]
_MONO_FONTS = [  # code/output: keep columns aligned where a mono font has the glyph
    "consola.ttf", "DejaVuSansMono.ttf", "lucon.ttf", "cour.ttf", "Courier New.ttf",
    "Menlo.ttc", "LiberationMono-Regular.ttf", "FreeMono.ttf", "NotoSansMono-Regular.ttf",
]
_MONO_TAGS = ("pre", "code", "tt", "kbd", "samp")

_INVISIBLE_RE = re.compile("[\u00ad\u200b-\u200f\u2060-\u2064\ufeff\ufe00-\ufe0f]")
_SPACE_RE = re.compile("[\u2000-\u200a\u202f\u205f\u3000]")

_EMOJI_BMP_DEFAULT = ("\u231a\u231b\u23e9-\u23ec\u23f0\u23f3\u25fd\u25fe\u2614\u2615\u2648-\u2653"
                      "\u267f\u2693\u26a1\u26aa\u26ab\u26bd\u26be\u26c4\u26c5\u26ce\u26d4\u26ea"
                      "\u26f2\u26f3\u26f5\u26fa\u26fd\u2705\u270a\u270b\u2728\u274c\u274e"
                      "\u2753-\u2755\u2757\u2795-\u2797\u27b0\u27bf\u2b1b\u2b1c\u2b50\u2b55")
# characters that are emoji only when followed by the emoji selector U+FE0F
_EMOJI_BMP_VS = ("\u00a9\u00ae\u203c\u2049\u2122\u2139\u2194-\u2199\u21a9\u21aa\u2328\u23cf"
                 "\u23ed-\u23ef\u23f1\u23f2\u23f8-\u23fa\u24c2\u25aa\u25ab\u25b6\u25c0"
                 "\u25fb-\u25fc\u2600-\u2604\u260e\u2611\u2618\u261d\u2620\u2622\u2623"
                 "\u2626\u262a\u262e\u262f\u2638-\u263a\u2640\u2642\u265f\u2660\u2663"
                 "\u2665\u2666\u2668\u267b\u267e\u2692\u2694-\u2697\u2699\u269b\u269c"
                 "\u26a0\u26a7\u26b0\u26b1\u26c8\u26cf\u26d1\u26d3\u26e9\u26f0\u26f1"
                 "\u26f4\u26f7-\u26f9\u2702\u2708\u2709\u270c\u270d\u270f\u2712\u2714"
                 "\u2716\u271d\u2721\u2733\u2734\u2744\u2747\u2763\u2764\u27a1\u2934\u2935"
                 "\u2b05-\u2b07\u3030\u303d\u3297\u3299")
_EMOJI_ELEMENT = (f"(?:[\U0001F000-\U0001FAFF]|[{_EMOJI_BMP_DEFAULT}]|[{_EMOJI_BMP_VS}](?=\ufe0f))")
_EMOJI_RE = re.compile(
    "[0-9#*]\ufe0f?\u20e3"                                    # keycaps 1️⃣
    "|[\U0001F1E6-\U0001F1FF]{2}"                             # flags 🇮🇳
    f"|{_EMOJI_ELEMENT}[\U0001F3FB-\U0001F3FF]?\ufe0f?"       # one emoji (+skin tone)
    f"(?:\u200d{_EMOJI_ELEMENT}[\U0001F3FB-\U0001F3FF]?\ufe0f?)*"  # ...joined (family etc.)
    "[\U000E0020-\U000E007F]*")

_TWEMOJI_URLS = (
    "https://raw.githubusercontent.com/jdecked/twemoji/main/assets/72x72/{n}.png",
    "https://raw.githubusercontent.com/twitter/twemoji/master/assets/72x72/{n}.png",
    "https://cdn.jsdelivr.net/gh/jdecked/twemoji@latest/assets/72x72/{n}.png",
)


def _is_cp1252(ch):
    try:
        ch.encode("cp1252")
        return True
    except UnicodeEncodeError:
        return False


def _discover_font_files():
    dirs = []
    if os.name == "nt":
        dirs.append(os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))
        if os.environ.get("LOCALAPPDATA"):
            dirs.append(os.path.join(os.environ["LOCALAPPDATA"], "Microsoft", "Windows", "Fonts"))
    else:
        dirs += ["/System/Library/Fonts", "/System/Library/Fonts/Supplemental", "/Library/Fonts",
                 os.path.expanduser("~/Library/Fonts"), "/usr/share/fonts", "/usr/local/share/fonts",
                 os.path.expanduser("~/.fonts"), os.path.expanduser("~/.local/share/fonts")]
    found = {}
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for root, _, files in os.walk(d):
            for f in files:
                if f.lower().endswith((".ttf", ".ttc")):
                    found.setdefault(f.lower(), os.path.join(root, f))
    return found


class SymbolSupport:
    """Rewrites the document body so every character has a glyph to draw it."""

    def __init__(self, extra_font=None, emoji_images=True, log=None):
        self.extra_font = extra_font
        self.emoji_images = emoji_images
        self.log = log or (lambda msg: None)
        self._files = None            # lowercase filename -> path (lazy)
        self._fonts = {}              # path -> (alias, cmap-set) or None
        self._ordered = {True: None, False: None}
        self.missing = set()          # characters no installed font could draw
        self.stats = {"emoji_images": 0, "emoji_text": 0, "symbols": 0}
        self._emoji_cache = {}
        self._emoji_net_failures = 0

    # ---- fonts -----------------------------------------------------------
    def _candidate_paths(self, mono):
        if self._ordered[mono] is None:
            if self._files is None:
                self._files = _discover_font_files()
            names = (_MONO_FONTS + _PROP_FONTS) if mono else (_PROP_FONTS + _MONO_FONTS)
            paths = [self.extra_font] if self.extra_font and os.path.isfile(self.extra_font) else []
            for n in names:
                p = self._files.get(n.lower())
                if p and p not in paths:
                    paths.append(p)
            self._ordered[mono] = paths
        return self._ordered[mono]

    def _load(self, path):
        if path not in self._fonts:
            try:
                from reportlab.pdfbase import pdfmetrics
                from reportlab.pdfbase.ttfonts import TTFont
                alias = f"nbsym{len(self._fonts)}"
                font = TTFont(alias, path, subfontIndex=0)
                cmap = set(font.face.charToGlyph.keys())
                pdfmetrics.registerFont(font)
                pdfmetrics.registerFontFamily(alias, normal=alias, bold=alias, italic=alias, boldItalic=alias)
                try:  # tell xhtml2pdf the family exists (it keeps its own list of known names)
                    import xhtml2pdf.default as _x2p_default
                    _x2p_default.DEFAULT_FONT[alias.lower()] = alias
                except Exception:
                    pass
                self._fonts[path] = (alias, cmap)
            except Exception:
                self._fonts[path] = None
        return self._fonts[path]

    def _font_for(self, cluster, mono):
        for path in self._candidate_paths(mono):
            f = self._load(path)
            if f and all(ord(c) in f[1] for c in cluster if not c.isspace()):
                return f[0]
        return None

    # ---- emoji -----------------------------------------------------------
    def _emoji_uri(self, seq):
        if seq in self._emoji_cache:
            return self._emoji_cache[seq]
        cps = [f"{ord(c):x}" for c in seq]
        if "200d" not in cps:
            cps = [c for c in cps if c != "fe0f"]
        name = "-".join(cps)
        uri = None
        cache_dir = os.path.join(tempfile.gettempdir(), "notebook_to_pdf_emoji")
        cache_file = os.path.join(cache_dir, name + ".png")
        data = None
        try:
            if os.path.isfile(cache_file):
                with open(cache_file, "rb") as fh:
                    data = fh.read()
        except OSError:
            data = None
        if data is None and self._emoji_net_failures < 3:
            for tmpl in _TWEMOJI_URLS:
                try:
                    req = urllib.request.Request(tmpl.format(n=name), headers=_IMAGE_FETCH_HEADERS)
                    with urllib.request.urlopen(req, timeout=6) as resp:
                        data = resp.read()
                    if data[:4] == b"\x89PNG":
                        try:  # palette+transparency PNG -> plain RGBA so the PDF engine draws it cleanly
                            import io
                            from PIL import Image
                            buf = io.BytesIO()
                            Image.open(io.BytesIO(data)).convert("RGBA").save(buf, "PNG")
                            data = buf.getvalue()
                        except Exception:
                            pass
                        try:
                            os.makedirs(cache_dir, exist_ok=True)
                            with open(cache_file, "wb") as fh:
                                fh.write(data)
                        except OSError:
                            pass
                        break
                    data = None
                except urllib.error.HTTPError:
                    data = None          # this emoji isn't in the set; try the next host
                except Exception:
                    data = None
                    self._emoji_net_failures += 1
                    break
        if data:
            uri = "data:image/png;base64," + base64.b64encode(data).decode("ascii")
        self._emoji_cache[seq] = uri
        return uri

    def _emoji_html(self, seq, mono):
        uri = self._emoji_uri(seq) if self.emoji_images else None
        if uri:
            self.stats["emoji_images"] += 1
            return f'<img class="emoji" src="{uri}" width="11" height="11" align="middle"/>'
        self.stats["emoji_text"] += 1
        return self._symbolize(seq, mono)

    # ---- text ------------------------------------------------------------
    def _symbolize(self, text, mono):
        text = _SPACE_RE.sub(" ", _INVISIBLE_RE.sub("", text))
        if text.isascii():
            return text
        # group each base character with the combining marks that follow it
        clusters, cur = [], ""
        for ch in text:
            if cur and unicodedata.category(ch) in ("Mn", "Me"):
                cur += ch
            else:
                if cur:
                    clusters.append(cur)
                cur = ch
        if cur:
            clusters.append(cur)
        out, run_font, run = [], None, ""

        def flush():
            nonlocal run, run_font
            if run:
                out.append(f'<span style="font-family:{run_font}">{run}</span>' if run_font else run)
            run, run_font = "", None

        for cl in clusters:
            need = any(not _is_cp1252(c) for c in cl)
            font = self._font_for(cl, mono) if need else None
            if need and font is None:
                self.missing.update(c for c in cl if not _is_cp1252(c))
            elif need:
                self.stats["symbols"] += 1
            if font != run_font:
                flush()
                run_font = font
            run += cl
        flush()
        return "".join(out)

    def process(self, body_html):
        """Rewrite the text parts of an HTML body string (tags are untouched)."""
        parts = re.split(r"(<[^>]*>)", body_html)
        out, mono_depth = [], 0
        for part in parts:
            if part.startswith("<"):
                m = re.match(r"<(/?)([a-zA-Z0-9]+)", part)
                if m and m.group(2).lower() in _MONO_TAGS and not part.endswith("/>"):
                    mono_depth = max(0, mono_depth + (-1 if m.group(1) else 1))
                out.append(part)
                continue
            if part.isascii():
                out.append(part)
                continue
            mono, pos, buf = mono_depth > 0, 0, []
            for m in _EMOJI_RE.finditer(part):
                buf.append(self._symbolize(part[pos:m.start()], mono))
                buf.append(self._emoji_html(m.group(0), mono))
                pos = m.end()
            buf.append(self._symbolize(part[pos:], mono))
            out.append("".join(buf))
        return "".join(out)


_LIST_ITEM_RE = re.compile(r'^( *)([-*+]|\d+\.)( +)(.*)$')


def normalize_list_indentation(text):
    """Jupyter/Colab's markdown renderer recognizes a nested bullet indented
    by as little as 2 spaces; the 'markdown' library's default list parser
    only recognizes nesting on 4-space boundaries, so a source written with
    a 2-space (or 3-space) indent convention renders as one flat list with
    no nesting at all. Detect whatever indentation step the cell actually
    uses (via the GCD of the indentation widths seen on list-item lines) and
    rescale every list-item line to a clean 4-space-per-level convention, so
    the nesting the author intended survives regardless of which step size
    they used. Lines that aren't list items are left untouched."""
    lines = text.split("\n")
    widths = set()
    for line in lines:
        m = _LIST_ITEM_RE.match(line)
        if m and len(m.group(1)) > 0:
            widths.add(len(m.group(1)))
    if not widths:
        return text
    step = 0
    for w in widths:
        step = math.gcd(step, w) if step else w
    if step <= 0:
        return text
    new_lines = []
    for line in lines:
        m = _LIST_ITEM_RE.match(line)
        if m and len(m.group(1)) > 0:
            level = round(len(m.group(1)) / step)
            line = (" " * (level * 4)) + m.group(2) + m.group(3) + m.group(4)
        new_lines.append(line)
    return "\n".join(new_lines)


def strip_backslash_breaks(text):
    """A line containing only a backslash is how some notebook authors (and
    Jupyter/Colab's own renderer) write a forced paragraph break; the
    'markdown' library doesn't treat it as a break and instead renders the
    backslash itself as a stray leading character on the next line. Drop
    any such line so the intended break survives without the literal '\\'
    showing up in the PDF."""
    return re.sub(r"(?m)^\\[ \t]*$\n?", "", text)


def preprocess_markdown_source(text):
    text = strip_backslash_breaks(text)
    text = normalize_list_indentation(text)
    return text


def build_converter(pygments_mod, markdown_mod, notebook_dir, image_cache=None):
    from pygments import highlight
    from pygments.lexers import PythonLexer
    from pygments.formatters import HtmlFormatter

    formatter = HtmlFormatter(style="default", nowrap=False)
    pygments_css = formatter.get_style_defs(".highlight")
    if image_cache is None:
        image_cache = {}  # URL/path -> (mime, base64) or (None, reason); avoids re-fetching duplicates

    def render_code(source):
        source = wrap_text_block(as_text(source))
        if not source.strip():
            return ""
        try:
            return highlight(source, PythonLexer(), formatter)
        except Exception:
            return f"<pre>{esc(source)}</pre>"

    def render_markdown(source):
        source = as_text(source)
        original = source
        if not source.strip():
            return ""
        try:
            source, math_store = protect_math(source)
            source = preprocess_markdown_source(source)
            raw_html = markdown_mod.markdown(source, extensions=["fenced_code", "tables"])
            raw_html = restore_math(raw_html, math_store)
            return render_markdown_html(raw_html, notebook_dir, image_cache)
        except Exception:
            return f"<pre>{esc(original)}</pre>"

    return render_code, render_markdown, pygments_css


def cell_row(prompt_html, content_html, prompt_class=""):
    """One notebook input/output row, laid out as a two-column table so the
    layout works reliably across PDF engines (no CSS flexbox dependency)."""
    return (
        '<table class="cellrow"><tr>'
        f'<td class="prompt {prompt_class}">{prompt_html}</td>'
        f'<td class="content">{content_html}</td>'
        "</tr></table>"
    )


def render_outputs(outputs):
    parts = []
    for output in outputs or []:
        otype = output.get("output_type")

        if otype == "stream":
            text = wrap_text_block(as_text(output.get("text")))
            parts.append(cell_row("", f'<pre class="plain-out">{esc(text)}</pre>'))

        elif otype == "execute_result":
            data = output.get("data", {})
            if "text/plain" in data:
                text = wrap_text_block(as_text(data["text/plain"]))
                execn = output.get("execution_count", "")
                parts.append(
                    cell_row(f"Out[{execn}]:", f'<pre class="plain-out">{esc(text)}</pre>', "out-prompt")
                )

        elif otype == "display_data":
            data = output.get("data", {})
            shown = False
            for mime in ("image/png", "image/jpeg"):
                if mime in data:
                    imgdata = as_text(data[mime])
                    parts.append(
                        cell_row("", f'<img class="output-image" src="data:{mime};base64,{imgdata}"/>')
                    )
                    shown = True
            if not shown and "text/plain" in data:
                text = wrap_text_block(as_text(data["text/plain"]))
                parts.append(cell_row("", f'<pre class="plain-out">{esc(text)}</pre>'))

        elif otype == "error":
            ename = output.get("ename", "")
            evalue = output.get("evalue", "")
            traceback_lines = output.get("traceback", [])
            if traceback_lines:
                tb = "\n".join(strip_ansi(l) for l in traceback_lines)
            else:
                tb = f"{ename}: {evalue}"
            tb = wrap_text_block(tb)
            parts.append(cell_row("", f'<pre class="plain-out error-text">{esc(tb)}</pre>'))

    return "".join(parts)


def build_cell_html(cell, render_code, render_markdown):
    ctype = cell.get("cell_type")

    if ctype == "code":
        execn = cell.get("execution_count")
        label = f"In [{execn if execn is not None else ' '}]:"
        src = as_text(cell.get("source"))
        block = []
        if src.strip():
            block.append(cell_row(label, f'<div class="input-code">{render_code(src)}</div>', "in-prompt"))
        outputs = cell.get("outputs", [])
        if outputs:
            block.append(render_outputs(outputs))
        return f'<div class="cell">{"".join(block)}</div>'

    elif ctype == "markdown":
        src = as_text(cell.get("source"))
        if not src.strip():
            return ""
        return f'<div class="cell markdown-cell">{render_markdown(src)}</div>'

    else:  # raw or unknown cell types
        src = as_text(cell.get("source"))
        if not src.strip():
            return ""
        return f'<div class="cell raw-cell"><pre>{esc(wrap_text_block(src))}</pre></div>'


PAGE_CSS_TEMPLATE = """
@page {{
    size: A4;
    margin: 2cm 1.8cm 2cm 1.8cm;
}}
body {{
    font-family: Helvetica;
    font-size: 10.5pt;
    color: #000000;
}}
h1.nb-title {{
    font-size: 16pt;
    font-weight: normal;
    border-bottom: 1px solid #cccccc;
    padding-bottom: 8pt;
    margin-bottom: 12pt;
}}
.cell {{
    margin-bottom: 6pt;
    page-break-inside: avoid;
}}
table.cellrow {{
    width: 100%;
    border-collapse: collapse;
    page-break-inside: avoid;
}}
td.prompt {{
    width: 60pt;
    font-family: Courier;
    font-size: {code_font_size}pt;
    font-weight: bold;
    vertical-align: top;
    padding: 3pt 4pt 3pt 0;
}}
td.in-prompt {{ color: #303f9f; }}
td.out-prompt {{ color: #d84315; }}
td.content {{
    vertical-align: top;
}}
.input-code {{
    background-color: #f7f7f7;
    border: 1px solid #cfcfcf;
    padding: 4pt 6pt;
}}
.input-code pre, .input-code .highlight pre {{
    margin: 0;
    font-family: Courier;
    font-size: {code_font_size}pt;
    line-height: 1.3;
}}
pre.plain-out {{
    margin: 2pt 0;
    font-family: Courier;
    font-size: {code_font_size}pt;
    line-height: 1.3;
    padding: 2pt 4pt;
}}
.error-text {{ color: #aa0000; }}
img.output-image {{
    max-width: 100%;
    margin: 4pt 0;
}}
.markdown-cell {{
    font-size: 10.5pt;
    margin: 6pt 0 10pt 0;
}}
.markdown-cell h1 {{ font-size: 13pt; margin: 8pt 0 4pt 0; }}
.markdown-cell h2 {{ font-size: 12pt; margin: 8pt 0 4pt 0; }}
.markdown-cell h3 {{ font-size: 11.5pt; margin: 8pt 0 4pt 0; }}
.markdown-cell p {{ margin: 3pt 0; }}
.markdown-cell code {{
    font-family: Courier;
    font-size: 9pt;
    background-color: #f0f0f0;
}}
.markdown-cell .list-item {{
    margin: 2pt 0;
}}
.markdown-cell .list-marker {{
    display: inline-block;
    width: 14pt;
}}
.markdown-cell img, img.md-image {{
    max-width: 100%;
    height: auto;
    margin: 4pt 0;
}}
.missing-image {{
    border: 1px dashed #aa0000;
    background-color: #fff5f5;
    color: #aa0000;
    padding: 6pt 8pt;
    margin: 4pt 0;
    font-size: 9pt;
    font-family: Helvetica;
}}
.missing-image-title {{
    font-weight: bold;
}}
.math-display {{
    text-align: center;
    margin: 6pt 0;
}}
img.emoji {{
    margin: 0 1pt;
}}
{pygments_css}
"""


def convert(notebook_path, output_pdf_path, title=None, code_font_size=8.5,
            embed_images=True, embedded_output_path=None,
            symbol_font=None, emoji_images=True):
    pygments_mod = _require("pygments")
    markdown_mod = _require("markdown")
    xhtml2pdf_mod = _require("xhtml2pdf", "xhtml2pdf")
    from xhtml2pdf import pisa
    _quiet_engine_noise()

    nb = load_notebook(notebook_path)
    cells = nb.get("cells", [])
    notebook_dir = os.path.dirname(os.path.abspath(notebook_path))
    image_cache = {}

    embed_results = []
    embedded_notebook_written = None
    if embed_images:
        embed_results = bake_images_into_notebook(nb, notebook_dir, image_cache, markdown_mod, log=print)
        if embed_results:
            target_path = embedded_output_path or (
                os.path.splitext(notebook_path)[0] + "_embedded.ipynb"
            )
            try:
                with open(target_path, "w", encoding="utf-8") as f:
                    json.dump(nb, f, indent=1, ensure_ascii=False)
                embedded_notebook_written = target_path
            except OSError as e:
                sys.stderr.write(
                    f"Warning: could not write self-contained notebook to '{target_path}' ({e}).\n"
                    f"Continuing with the PDF using the images resolved in memory; "
                    f"re-run with --embedded-output pointing at a writable location to save it.\n"
                )

    render_code, render_markdown, pygments_css = build_converter(
        pygments_mod, markdown_mod, notebook_dir, image_cache=image_cache
    )

    body_parts = [build_cell_html(c, render_code, render_markdown) for c in cells]

    doc_title = title or os.path.basename(notebook_path)
    body_html = f'<h1 class="nb-title">{esc(doc_title)}</h1>\n' + "".join(body_parts)
    symbols = SymbolSupport(extra_font=symbol_font, emoji_images=emoji_images)
    body_html = symbols.process(body_html)
    css = PAGE_CSS_TEMPLATE.format(code_font_size=code_font_size, pygments_css=pygments_css)

    html_doc = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>{css}</style>
</head>
<body>
{body_html}
</body>
</html>
"""

    try:
        with open(output_pdf_path, "wb") as out_file:
            result = pisa.CreatePDF(html_doc, dest=out_file)
        if result.err:
            sys.stderr.write(f"Error while rendering PDF ({result.err} issue(s) reported).\n")
            sys.exit(1)
    except Exception as e:
        sys.stderr.write(f"Error while rendering PDF: {e}\n")
        sys.exit(1)

    code_cells = sum(1 for c in cells if c.get("cell_type") == "code")
    md_cells = sum(1 for c in cells if c.get("cell_type") == "markdown")
    return {
        "output": output_pdf_path,
        "total_cells": len(cells),
        "code_cells": code_cells,
        "markdown_cells": md_cells,
        "embed_results": embed_results,
        "embedded_notebook_written": embedded_notebook_written,
        "symbol_stats": symbols.stats,
        "symbols_missing": sorted(symbols.missing),
    }


def _collect_notebooks(items):
    """No items = every .ipynb in the current folder. A folder = its .ipynb
    files; a wildcard is expanded. Our own *_embedded.ipynb copies are skipped
    when notebooks are auto-discovered."""
    found, seen = [], set()

    def add(path, auto):
        path = os.path.abspath(path)
        if path in seen or ".ipynb_checkpoints" in path.split(os.sep):
            return
        if auto and (not path.lower().endswith(".ipynb") or path.lower().endswith("_embedded.ipynb")):
            return
        seen.add(path)
        found.append(path)

    for item in (items or ["."]):
        if os.path.isdir(item):
            for name in sorted(os.listdir(item)):
                add(os.path.join(item, name), True)
        elif any(ch in item for ch in "*?["):
            for name in sorted(glob.glob(item)):
                add(name, True)
        else:
            add(item, False)
    return found


def _print_report(result):
    embed_results = result["embed_results"]
    if embed_results:
        embedded = [r for r in embed_results if r[2] == "embedded"]
        failed = [r for r in embed_results if r[2] == "failed"]
        print(
            f"\nExternal images found: {len(embed_results)}  |  "
            f"embedded: {len(embedded)}  |  failed: {len(failed)}"
        )
        if result["embedded_notebook_written"]:
            print(f"Self-contained notebook written to: {result['embedded_notebook_written']}")
        if failed:
            print("Could not embed (left as external links, shown as a placeholder in the PDF):")
            for i, url, _, reason in failed:
                print(f"  - cell {i}: {url}\n      reason: {reason}")
        print()

    stats = result["symbol_stats"]
    if stats["emoji_images"] or stats["emoji_text"]:
        print(f"Emoji: {stats['emoji_images']} drawn as colour pictures"
              + (f", {stats['emoji_text']} as plain symbols (picture not available)" if stats["emoji_text"] else ""))
    if result["symbols_missing"]:
        shown = " ".join(result["symbols_missing"][:40])
        sys.stderr.write(
            f"Warning: no installed font could draw these characters, so they may show as boxes: {shown}\n"
            f"         Install a font that has them, or pass one with --symbol-font path\\to\\font.ttf\n")

    print(f"Done. PDF written to: {result['output']}")
    print(
        f"Cells: {result['total_cells']} "
        f"(code={result['code_cells']}, markdown={result['markdown_cells']})"
    )


def main():
    parser = argparse.ArgumentParser(
        description="Convert a Jupyter notebook (.ipynb) into a single, plain-style PDF."
    )
    parser.add_argument(
        "notebook", nargs="*",
        help="Notebook file(s), folder(s) or wildcard (e.g. *.ipynb). "
             "Leave empty to convert every .ipynb in the current folder.")
    parser.add_argument(
        "-o", "--output",
        help="Path to the output PDF (default: same name as notebook, .pdf extension)",
        default=None,
    )
    parser.add_argument(
        "--title",
        help="Title shown at the top of the PDF (default: notebook filename)",
        default=None,
    )
    parser.add_argument(
        "--code-font-size",
        type=float,
        default=8.5,
        help="Font size in points for code/output text (default: 8.5)",
    )
    parser.add_argument(
        "--no-embed-images",
        action="store_true",
        help="Skip baking externally-linked images into the notebook; convert it as-is",
    )
    parser.add_argument(
        "--embedded-output",
        help="Path for the self-contained notebook copy (default: <name>_embedded.ipynb)",
        default=None,
    )
    parser.add_argument(
        "--symbol-font",
        help="Path to a .ttf font to try first for symbols/Greek/math (normally found automatically)",
        default=None,
    )
    parser.add_argument(
        "--no-emoji-images",
        action="store_true",
        help="Don't download colour emoji pictures; draw emoji as plain symbols instead",
    )
    args = parser.parse_args()

    targets = _collect_notebooks(args.notebook)
    if not targets:
        sys.stderr.write("No .ipynb files found. Run this from the folder that contains your "
                         "notebooks, or pass a file/folder path.\n")
        sys.exit(1)
    if len(targets) > 1 and (args.output or args.embedded_output or args.title):
        sys.stderr.write("-o/--output, --embedded-output and --title work with a single notebook only.\n")
        sys.exit(1)

    done, failed = [], []
    for n, nb_path in enumerate(targets, 1):
        if len(targets) > 1:
            print(f"\n[{n}/{len(targets)}] {os.path.basename(nb_path)}")
        try:
            result = convert(
                nb_path,
                args.output or (os.path.splitext(nb_path)[0] + ".pdf"),
                title=args.title,
                code_font_size=args.code_font_size,
                embed_images=not args.no_embed_images,
                embedded_output_path=args.embedded_output,
                symbol_font=args.symbol_font,
                emoji_images=not args.no_emoji_images,
            )
        except SystemExit:
            failed.append(nb_path)
            continue
        except Exception as e:
            sys.stderr.write(f"Error converting '{nb_path}': {e}\n")
            failed.append(nb_path)
            continue
        _print_report(result)
        done.append(nb_path)

    if len(targets) > 1:
        print(f"\nFinished: {len(done)} converted, {len(failed)} failed.")
    for f in failed:
        print(f"  FAILED: {f}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
