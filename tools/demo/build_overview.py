"""The technical overview as a page of the demonstration site, in the Blum design, so it can be read without the
repository.

Usage: .venv/bin/python3 tools/demo/build_overview.py docs/technical-overview.md docs/demo/overview.html
"""

import re
import sys
from pathlib import Path

import markdown

TEMPLATE = Path(__file__).with_name("overview_template.html")


def main(md_path, out_path):
    text = Path(md_path).read_text()
    title, body = text.split("\n", 1)
    if title != "# Technical overview":
        raise SystemExit(f"not building: {md_path} starts with {title!r}, not the overview's title")
    # The overview starts lists right under a paragraph line, which Python-Markdown reads as part of the paragraph
    # unless a blank line separates them; it indents nested lists by two spaces.
    lines, in_list = [], False
    for line in body.split("\n"):
        item = re.match(r"\s*- ", line) is not None
        if item and not in_list and lines and lines[-1].strip():
            lines.append("")
        in_list = (in_list or item) and bool(line.strip())
        lines.append(line)
    html = markdown.markdown("\n".join(lines), extensions=["tables"], tab_length=2)
    html = html.replace("<h2>", '<h2 class="bs-h2">').replace("<h3>", '<h3 class="bs-h3">')
    html = re.sub(r"<table>(.*?)</table>", r'<div class="ov-box"><table class="bs-ledger">\1</table></div>', html,
                  flags=re.S)
    foot = f"<p>Built from <code>{Path(md_path).as_posix()}</code> in the repository by <code>tools/demo/build_overview.py</code>.</p>"
    page = TEMPLATE.read_text().replace("<!--__BODY__-->", html).replace("<!--__FOOT__-->", foot)
    Path(out_path).write_text(page)
    print(f"wrote {out_path}: {html.count('<h2')} sections, {html.count('<table')} tables")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    main(*sys.argv[1:])
