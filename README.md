# Notebook to PDF

A Python command-line tool for converting Jupyter Notebook (`.ipynb`) files into a single A4 PDF. It is designed to keep notebook content readable in long exports by wrapping long code and output lines and rendering notebook cells into a paginated document.

> **Status:** Initial public release. Rendering can vary by notebook content and PDF-engine support. Please test the output before relying on it for a final submission.

## Features

- Converts code, Markdown, and raw cells.
- Includes saved cell outputs: text streams, plain-text results, error tracebacks, and PNG/JPEG images (including many saved plots).
- Syntax-highlights Python code.
- Renders Markdown headings, emphasis, fenced code blocks, and tables.
- Wraps long code and text-output lines to reduce horizontal clipping.
- Handles many common LaTeX math commands, Greek letters, arrows, and other symbols.
- Embeds publicly accessible remote images used in Markdown and can save a copy of the notebook with successfully embedded images.
- Supports a custom PDF title, code font size, symbol font, and output path.
- Can convert one notebook, multiple notebook files, a folder, or a wildcard pattern.

## Requirements

- Python 3.9 or newer is recommended.
- Dependencies: `Pygments`, `Markdown`, and `xhtml2pdf`.
- Internet access is needed to fetch remote Markdown images and download colour emoji images when they are not cached. Remote resources that require authentication cannot be fetched automatically.

## Installation

1. Install Python from [python.org](https://www.python.org/downloads/) if it is not already installed.
2. Download or clone this repository.
3. Open a terminal in the repository folder and install the dependencies:

   ```bash
   python -m pip install -r requirements.txt
   ```

## Usage

Convert one notebook to a PDF beside the notebook file:

```bash
python notebook_to_pdf.py path/to/notebook.ipynb
```

Choose the output PDF path:

```bash
python notebook_to_pdf.py path/to/notebook.ipynb -o output.pdf
```

Set the title shown in the PDF:

```bash
python notebook_to_pdf.py path/to/notebook.ipynb -o output.pdf --title "My Notebook"
```

Adjust code and output font size:

```bash
python notebook_to_pdf.py path/to/notebook.ipynb --code-font-size 8
```

Skip embedding remote Markdown images:

```bash
python notebook_to_pdf.py path/to/notebook.ipynb --no-embed-images
```

Convert all notebooks in the current folder:

```bash
python notebook_to_pdf.py
```

Convert a wildcard selection:

```bash
python notebook_to_pdf.py "examples/*.ipynb"
```

On Windows, use `py` instead of `python` if that is how Python is configured on your machine.

## Command-line options

| Option | Purpose |
|---|---|
| `notebook` | Optional notebook file(s), folder(s), or wildcard pattern(s). With no argument, scans the current folder. |
| `-o`, `--output` | Output PDF path; for one notebook only. Defaults to the notebook filename with `.pdf`. |
| `--title` | Title printed at the top of the PDF; for one notebook only. |
| `--code-font-size` | Font size in points for code and output text (default: `8.5`). |
| `--no-embed-images` | Do not fetch and embed externally linked Markdown images. |
| `--embedded-output` | Set the path for the self-contained notebook copy created when external images are successfully embedded; for one notebook only. |
| `--symbol-font` | Path to a `.ttf` font to try first for symbols. |
| `--no-emoji-images` | Do not download colour emoji images; use text-symbol fallback instead. |

## Important limitations

- The converter uses saved outputs in the `.ipynb` file; it does **not** execute notebook code. Run the notebook and save its outputs before converting if you want those outputs included.
- For `execute_result` and `display_data`, the script currently renders supported output representations such as `text/plain`, PNG, and JPEG. Rich outputs available only as HTML, SVG, interactive widgets, or other MIME types may not appear as they do in a browser.
- Remote images requiring sign-in or otherwise inaccessible URLs cannot be embedded. The PDF reports an image placeholder instead of silently omitting the image.
- Mathematical notation is converted to readable HTML/text for common commands; this is not a full LaTeX typesetting engine, so complex equations may not match MathJax exactly.
- Font coverage differs between systems. Some unusual symbols may need an installed font or `--symbol-font`.
- The output should be reviewed for layout issues, especially for notebooks containing very large images, complex HTML, interactive visualizations, or unusual output formats.

## Troubleshooting

**A dependency is missing**

```bash
python -m pip install -r requirements.txt
```

**The notebook has no output in the PDF**

Open the notebook in Jupyter or Colab, run the relevant cells, save the notebook with outputs, and try again.

**A Markdown image is missing**

Check that the URL is publicly accessible. Links that require Google sign-in or other authentication may not be downloadable by the script.

**A symbol is missing or appears as a box**

Install a font containing the character or pass a suitable `.ttf` file with `--symbol-font`.

## Contributing

Bug reports, reproducible examples, and improvements are welcome. When reporting a PDF rendering issue, include your operating system, Python version, the command used, and a small notebook that demonstrates the issue. Remove private or sensitive data before sharing a notebook.

## License

This project is distributed under the MIT License. See [LICENSE](LICENSE).
