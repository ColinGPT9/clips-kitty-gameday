"""The dashboard works under the app's Content-Security-Policy (docs/API.md).

That policy allows scripts, styles and media from the app itself only, so
index.html may hold no inline script or style, no style= attribute and no
event-handler attribute, and may load nothing from another site. Plain links
to another site (<a href="https://...">, opened in a new tab) load nothing
and are allowed.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "gameday" / "static"
INDEX = STATIC / "index.html"
EXTERNAL = re.compile(r"^\s*(?:[a-z][a-z0-9+.-]*:)?//", re.IGNORECASE)  # https://, http://, //host
URL_ATTRS = ("src", "href", "poster", "action", "formaction", "data", "srcset", "background", "xlink:href")


class Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str]]] = []
        self.inline_scripts: list[str] = []
        self._in_script = False

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, {k.lower(): v or "" for k, v in attrs}))
        if tag == "script":
            self._in_script = True

    def handle_startendtag(self, tag, attrs):
        self.tags.append((tag, {k.lower(): v or "" for k, v in attrs}))

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_script = False

    def handle_data(self, data):
        if self._in_script and data.strip():
            self.inline_scripts.append(data.strip()[:60])


def _page() -> Page:
    page = Page()
    page.feed(INDEX.read_text(encoding="utf-8"))
    return page


def test_no_inline_script():
    page = _page()
    assert not page.inline_scripts, f"inline script in index.html: {page.inline_scripts}"
    for tag, attrs in page.tags:
        if tag == "script":
            assert attrs.get("src", "").startswith("/static/"), f"a script that isn't the app's own: {attrs}"
        handlers = [name for name in attrs if name.startswith("on")]
        assert not handlers, f"<{tag}> has inline event handlers {handlers}"
        for name in URL_ATTRS:
            assert not attrs.get(name, "").strip().lower().startswith("javascript:"), f"<{tag} {name}> runs script"


def test_no_inline_style():
    page = _page()
    for tag, attrs in page.tags:
        assert tag != "style", "a <style> block in index.html"
        assert "style" not in attrs, f"<{tag}> has a style= attribute"
    assert not re.search(r"<style\b", INDEX.read_text(encoding="utf-8"), re.IGNORECASE)


def test_loads_nothing_from_other_sites():
    for tag, attrs in _page().tags:
        for name in URL_ATTRS:
            value = attrs.get(name, "")
            if not EXTERNAL.match(value):
                continue
            # A link someone clicks, opened in a new tab without access back to this page.
            is_link = tag == "a" and name == "href"
            assert is_link, f"<{tag} {name}={value!r}> loads from another site"
            assert value.startswith("https://"), f"link {value!r} isn't https"
            opens_apart = attrs.get("target") == "_blank" and "noopener" in attrs.get("rel", "")
            assert opens_apart, f"link {value!r} needs target=_blank and rel=noopener"
    for name in ("app.css", "app.js"):
        text = (STATIC / name).read_text(encoding="utf-8")
        hosts = re.findall(r"(?:https?:)?//[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", text)
        assert not hosts, f"{name} names another site: {hosts}"
        assert not re.search(r"@import\b", text), f"{name} imports another stylesheet"


def test_static_references_exist():
    page = _page()
    refs = [v for _, attrs in page.tags for k, v in attrs.items() if k in URL_ATTRS and v.startswith("/static/")]
    assert {"/static/app.js", "/static/app.css", "/static/icon.svg"} <= set(refs)
    for ref in refs:
        assert (STATIC / ref.removeprefix("/static/")).is_file(), f"{ref} is referenced but missing"


def test_script_stays_inside_the_policy():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert banned not in js, f"app.js uses {banned}"
    assert not re.search(r"setAttribute\(\s*['\"]style", js), "app.js sets a style attribute"
    assert "X-Gameday-Action" in js, "changes must send the X-Gameday-Action header"


def test_every_id_the_script_uses_is_in_the_page():
    ids = {attrs["id"] for _, attrs in _page().tags if "id" in attrs}
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    used = set(re.findall(r"(?:\$|show|setText|note)\('([A-Za-z][\w-]*)'\s*[,)]", js))
    missing = sorted(used - ids)
    assert not missing, f"app.js looks up ids that index.html lacks: {missing}"
