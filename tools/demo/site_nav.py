"""The site's menu and the bar under it, the same on every page of blum.caiborg.ai: every page is in the menu, and
every page loads ask-ai.js, which adds "Have your own AI explain how it works" under the menu.

Pages generated from one file each (source views, the auditor review) get the menu through `chrome`, which also
links the design system (the page's own styles follow it and keep the page's look), lets the page's header scroll
under the sticky menu and, on a page whose body was the centred column, moves the column into <main>.
"""
import re
from html import escape

# (href from the site root, label); the operator screen is served by nginx at /operator-screen.
PAGES = [("demo-day/", "Demo Day"), ("deck.html", "Deck"), ("video.html", "Video"), ("index.html", "Parties"), ("stages.html", "Stages"),
         ("timeline.html", "Timeline"), ("flow.html", "Flow"), ("program.html", "Program"), ("overview.html", "Overview"),
         ("operator-screen", "Operator"), ("audit/review.html", "Review")]
CURRENT = ' aria-current="true"'
COLUMN = "body{margin:24px auto;padding:0 16px;max-width:1100px;"


def nav(title, current, root=""):
    """The menu: `title` after "Blum · ", `current` the label of this page's item, `root` the path to the site root."""
    links = "".join(f'<a href="{root}{href}"{CURRENT if label == current else ""}>{label}</a>' for href, label in PAGES)
    return f'<nav class="bs-index"><div class="in"><b>Blum · {escape(title)}</b>{links}</div></nav>'


def chrome(page, title, current, root):
    """Add the menu, the design system and ask-ai.js to a page generated with its own <style> and no menu."""
    column = COLUMN in page
    page = page.replace("<style>", f'<link rel="stylesheet" href="{root}blum/styles.css"><style>', 1)
    page = page.replace("header{padding:20px;position:sticky;top:0;", "header{padding:20px;", 1)
    if column:
        page = page.replace(COLUMN, "body{margin:0;", 1).replace("</style>", "main{display:block;margin:24px auto;padding:0 16px;max-width:1100px}main p{margin:0 0 1em}</style>", 1)
    page = page.replace("</style>", "</style>" + nav(title, current, root) + ("<main>" if column else ""), 1)
    return re.sub(r"</html>\s*$", ("</main>" if column else "") + f'<script src="{root}ask-ai.js" defer></script></html>', page, count=1)
