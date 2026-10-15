# Notebook to PDF

A Python command-line tool for converting Jupyter Notebook (`.ipynb`) files, including notebooks downloaded from Google Colab, into a readable, paginated A4 PDF.

This project was created as a practical workaround for long notebooks where browser-based printing (for example, printing from Google Colab) may omit the final pages or outputs. I tested the converter on a long notebook and verified that the generated PDF included the notebook content through the end.

> **Important:** This is an independent workaround, not a fix to Google Colab's internal print implementation. PDF rendering can vary by notebook content and the PDF engine, so review the generated document before relying on it for a submission.

## Features

- Converts notebook code, Markdown, and raw cells.
- Includes **saved cell outputs**, including text streams, plain-text results, error tracebacks, and PNG/JPEG images.
- Syntax-highlights Python code.
- Renders Markdown headings, emphasis, inline code, fenced code blocks, tables, lists, links, and blockquotes.
- Wraps long code and text-output lines to reduce horizontal clipping.
- Converts many common LaTeX commands into readable mathematical notation and symbols.
- Supports Greek letters, arrows, math symbols, and emoji (font coverage varies; see [Limitations](#important-limitations)).
- Embeds publicly accessible remote images referenced by Markdown and saves a self-contained copy of the notebook (`<name>_embedded.ipynb`) with the images baked in.
- Supports a custom PDF title, code/output font size, symbol font, and output path.
- Can process one notebook, multiple notebooks, a folder, or a wildcard pattern.

## Requirements

- Python 3.9 or newer.
- Dependencies: `Pygments`, `Markdown`, and `xhtml2pdf` (installed from `requirements.txt`).
- Internet access is needed to fetch remote Markdown images and to download colour emoji images when they are not cached. Resources requiring authentication cannot be fetched automatically.

## Privacy and network use

By default, the tool makes network requests in two situations:

1. **Remote images:** images referenced by URL in your Markdown cells (for example, Google Drive or CDN links) are downloaded so they can be shown in the PDF.
2. **Colour emoji:** emoji images are downloaded when they are not already cached.

Your notebook file itself is never uploaded anywhere.

- Use `--no-emoji-images` to stop the emoji downloads.
- Use `--no-embed-images` to stop the tool from saving the self-contained `<name>_embedded.ipynb` copy. **Note:** remote images are still fetched while the PDF is rendered, so this option does not make the tool fully offline.
- If your notebook contains no remote image URLs, `--no-emoji-images` is enough to avoid all network use. If it does and you must stay offline, remove or replace those images before converting.

```bash
python ipynb-notebook_to_pdf.py path/to/notebook.ipynb --no-emoji-images
```

## Installation

1. Install Python from [python.org](https://www.python.org/downloads/) if it is not already installed.
2. Clone the repository and move into it:

   ```bash
   git clone https://github.com/theShubhmGupta/ipynb-notebook_to_pdf.git
   cd ipynb-notebook_to_pdf
   ```

   Alternatively, download the repository as a ZIP from GitHub and extract it.

3. *(Optional but recommended)* Create and activate a virtual environment:

   ```bash
   python -m venv .venv
   ```

   - macOS / Linux: `source .venv/bin/activate`
   - Windows (PowerShell): `.venv\Scripts\Activate.ps1`

4. Install the dependencies:

   ```bash
   python -m pip install -r requirements.txt
   ```

## Getting your notebook from Google Colab

If your notebook lives in Google Colab:

1. Run all cells first (**Runtime → Run all**) so the outputs you want in the PDF exist.
2. Download the notebook with its outputs: **File → Download → Download .ipynb**.
3. Convert the downloaded `.ipynb` file using the commands below.

The converter only uses outputs already saved in the `.ipynb` file. It does not run your code.

## Usage

Convert one notebook to a PDF:

```bash
python ipynb-notebook_to_pdf.py path/to/notebook.ipynb
```

Choose the output path:

```bash
python ipynb-notebook_to_pdf.py path/to/notebook.ipynb -o output.pdf
```

Set the title shown in the PDF:

```bash
python ipynb-notebook_to_pdf.py path/to/notebook.ipynb -o output.pdf --title "My Notebook"
```

Adjust code and output font size:

```bash
python ipynb-notebook_to_pdf.py path/to/notebook.ipynb --code-font-size 8
```

Skip saving the self-contained `_embedded.ipynb` copy (remote images are still fetched for the PDF):

```bash
python ipynb-notebook_to_pdf.py path/to/notebook.ipynb --no-embed-images
```

Convert all notebooks in the current folder:

```bash
python ipynb-notebook_to_pdf.py
```

Convert notebooks matching a wildcard:

```bash
python ipynb-notebook_to_pdf.py "examples/*.ipynb"
```

On Windows, use `py` instead of `python` if that is how Python is configured on your machine.

## Command-line options

| Option | Purpose | Default |
|---|---|---|
| `notebook` | Optional notebook file(s), folder(s), or wildcard pattern(s). With no argument, scans the current folder (not recursive). | Current folder |
| `-o`, `--output` | Output PDF path. Single notebook only. | Notebook filename with a `.pdf` extension |
| `--title` | Title printed at the top of the PDF. Single notebook only. | Notebook filename (including `.ipynb`) |
| `--code-font-size` | Font size in points for code and output text. | `8.5` |
| `--no-embed-images` | Do not save a self-contained notebook copy with remote images baked in. Remote images are still fetched to render the PDF. | Self-contained copy is saved when remote images are found |
| `--embedded-output` | Path for the self-contained notebook copy created when remote images are found. Single notebook only. | `<name>_embedded.ipynb`, next to the notebook |
| `--symbol-font` | Path to a `.ttf` font to try first for symbols. | Found automatically from installed fonts |
| `--no-emoji-images` | Do not download colour emoji images; draw emoji as plain symbols instead. | Emoji images are downloaded |

**Notes**

- Using `-o`, `--title`, or `--embedded-output` with more than one notebook stops with an error. Convert notebooks one at a time when you need these options.
- When scanning a folder or wildcard, the tool skips `.ipynb_checkpoints` folders and any `*_embedded.ipynb` files it created earlier.
- When converting several notebooks, a failure on one does not stop the rest. A summary is printed at the end and the exit code is non-zero if any failed.

## Important limitations

- The converter uses outputs already saved in the `.ipynb` file. It **does not execute notebook code**. Run the notebook and save it with outputs before converting if you want those outputs included.
- Rich output is format-dependent. The converter supports representations such as `text/plain`, PNG, and JPEG; HTML-only outputs, SVG, interactive widgets, and other MIME types may not render as they do in a browser.
- Remote images requiring sign-in or otherwise inaccessible URLs cannot be embedded. The PDF displays a labelled placeholder rather than silently omitting the image.
- Mathematical notation is converted to readable HTML/text for common commands. This is not a full LaTeX typesetting engine, so complex equations may differ from MathJax.
- Font coverage varies by operating system. Some unusual symbols may require an installed font or `--symbol-font`.
- Review the PDF for layout issues, especially with very large images, complex HTML, interactive visualizations, or unusual output formats.

## Troubleshooting

**A dependency is missing**

```bash
python -m pip install -r requirements.txt
```

**Outputs are missing from the PDF**

Open the notebook in Jupyter or Colab, run the relevant cells, save the notebook with outputs (in Colab, use **File → Download → Download .ipynb** after running), and try again.

**A Markdown image is missing**

Check that the URL is publicly accessible. Links requiring Google sign-in or other authentication may not be downloadable by the script.

**A symbol is missing or appears as a box**

Install a font containing the character or pass a suitable `.ttf` file with `--symbol-font`.

## Example

A sample notebook and its generated PDF are available in [`examples/`](examples/).


## Feedback, issues, and contributions

If you try this tool and encounter a problem, or find a case where it does not preserve or render your notebook content as expected, please open an issue in this repository:

**[Report a problem or request an improvement](https://github.com/theShubhmGupta/ipynb-notebook_to_pdf/issues)**

To help reproduce the problem, include your operating system, Python version, the command you ran, and (if possible) a small notebook that demonstrates the issue. Remove confidential or personal information before sharing files.

Suggestions, bug reports, and contributions are welcome.

---

## Author

**Shubham Gupta** 

[![GitHub](https://img.shields.io/badge/GitHub-theShubhmGupta-black?logo=github)](https://github.com/theShubhmGupta)
[![LinkedIn](https://img.shields.io/badge/LinkedIn-theshubhamguptaa-blue?logo=linkedin)](https://linkedin.com/in/theshubhamguptaa)
[![Tableau](https://img.shields.io/badge/Tableau-Public-blue?logo=tableau)](https://public.tableau.com/app/profile/shubham.gupta2025)
[![Streamlit](https://img.shields.io/badge/Streamlit-FF4B4B?style=for-the-badge&logo=streamlit&logoColor=white)](https://genai-sql-assistant.streamlit.app/)
---

## License

This project is distributed under the [MIT License](LICENSE).
