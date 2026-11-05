"""Export a canonical tree as a Netscape bookmarks HTML file (importable by
every browser)."""

from __future__ import annotations

from html import escape

from .engine.model import Node, Tree

ROLE_TITLES = {
    "toolbar": "Bookmarks Toolbar",
    "other": "Other Bookmarks",
    "mobile": "Mobile Bookmarks",
    "menu": "Bookmarks Menu",
}


def to_html(K: Tree, title: str = "Bookmarks") -> str:
    lines = [
        "<!DOCTYPE NETSCAPE-Bookmark-file-1>",
        '<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">',
        f"<TITLE>{escape(title)}</TITLE>",
        f"<H1>{escape(title)}</H1>",
        "<DL><p>",
    ]

    def emit(n: Node, depth: int) -> None:
        pad = "    " * depth
        if n.kind == "separator":
            lines.append(f"{pad}<HR>")
        elif n.kind == "bookmark":
            lines.append(f'{pad}<DT><A HREF="{escape(n.url or "", quote=True)}">{escape(n.title)}</A>')
        else:
            lines.append(f"{pad}<DT><H3>{escape(n.title)}</H3>")
            lines.append(f"{pad}<DL><p>")
            for c in n.children:
                emit(c, depth + 1)
            lines.append(f"{pad}</DL><p>")

    for role in ("toolbar", "menu", "other", "mobile"):
        root = K.get(role)
        if root is None:
            continue
        attr = ' PERSONAL_TOOLBAR_FOLDER="true"' if role == "toolbar" else ""
        lines.append(f"    <DT><H3{attr}>{ROLE_TITLES[role]}</H3>")
        lines.append("    <DL><p>")
        for c in root.children:
            emit(c, 2)
        lines.append("    </DL><p>")
    lines.append("</DL><p>")
    return "\n".join(lines) + "\n"
