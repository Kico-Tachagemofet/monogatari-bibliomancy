"""Read EPUB spine order without third-party dependencies or source mutation.

Coordinates are zero-based Unicode character offsets in decoded paragraphs,
not byte offsets into XHTML. Paragraphs include headings (even when omitted
from pagination). Whitespace inside text is preserved; whitespace-only nodes
between blocks are not paragraphs. End coordinates are exclusive.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from html.parser import HTMLParser
from pathlib import Path
import io
import posixpath
import re
from urllib.parse import unquote, urldefrag
import xml.etree.ElementTree as ET
import zipfile


ARC_RE = re.compile(r"^(?:第[^\s话]{1,8}话|最终话)(?:\s|$)")
NUMBER_RE = re.compile(r"^[0-9０-９]+(?:\s*[-－–—~～]\s*[0-9０-９]+)?$")
NONBODY = ("封面", "制作信息", "翻译信息", "录入信息", "目录", "简介",
           "插画", "插图", "后记", "作者介绍", "封底")
BLOCKS = {"p", "div", "section", "article", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "tr"}


def char_count(text: str) -> int:
    return sum(not c.isspace() for c in text)


def compact(text: str) -> str:
    return "".join(text.split())


def is_arc(text: str) -> bool:
    return len(text.strip()) <= 80 and bool(ARC_RE.match(text.strip()))


class ParagraphParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.paragraphs: list[str] = []
        self.anchors: dict[str, tuple[int, int]] = {}
        self.buffer: list[str] = []
        self.body = False
        self.ignored = 0

    def flush(self):
        value = "".join(self.buffer)
        if value.strip():
            self.paragraphs.append(value)
        self.buffer = []

    def handle_starttag(self, tag, attrs):
        if tag == "body":
            self.body = True
        if not self.body:
            return
        if tag in {"script", "style"}:
            self.ignored += 1
        if self.ignored:
            return
        if tag in BLOCKS or tag == "br":
            self.flush()
        attrs = dict(attrs)
        anchor = attrs.get("id") or attrs.get("name")
        if anchor:
            self.anchors[anchor] = (len(self.paragraphs), len("".join(self.buffer)))

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.ignored:
            self.ignored -= 1
            return
        if self.ignored:
            return
        if tag in BLOCKS or tag == "body":
            self.flush()
        if tag == "body":
            self.body = False

    def handle_data(self, data):
        if self.body and not self.ignored:
            # Ignore pretty-print whitespace outside paragraphs, but preserve
            # all whitespace occurring with text, including ideographic spaces.
            if self.buffer or data.strip():
                self.buffer.append(data)


@dataclass
class SpineItem:
    path: str
    paragraphs: list[str]
    anchors: dict[str, tuple[int, int]] = field(default_factory=dict)
    labels: list[str] = field(default_factory=list)


@dataclass
class Epub:
    filename: str
    digest: str
    book: str
    volume: str
    spine: list[SpineItem]
    toc: list[dict]
    titles: list[str] = field(default_factory=list)


def local_tag(node):
    return node.tag.rsplit("}", 1)[-1]


def resolve_ref(base: str, ref: str) -> tuple[str, str]:
    path, fragment = urldefrag(ref)
    path = posixpath.normpath(posixpath.join(posixpath.dirname(base), unquote(path))) if path else base
    if path.startswith(("../", "/")) or ":" in path:
        raise ValueError("EPUB reference escapes archive")
    return path, unquote(fragment)


def read_epub(path: Path) -> Epub:
    raw = path.read_bytes()
    book, volume = "", ""
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        container = ET.fromstring(archive.read("META-INF/container.xml"))
        rootfiles = [n.get("full-path") for n in container.iter() if local_tag(n) == "rootfile"]
        if len(rootfiles) != 1:
            raise ValueError(f"{path.name}: expected one package root")
        opf = rootfiles[0]
        package = ET.fromstring(archive.read(opf))
        titles = ["".join(n.itertext()).strip() for n in package.iter() if local_tag(n) == "title"]
        # 猫白 has `mlns` rather than `xmlns`; match local names deliberately.
        items = {n.get("id"): n for n in package.iter() if local_tag(n) == "item"}
        spine_node = next(n for n in package.iter() if local_tag(n) == "spine")
        refs = [n.get("idref") for n in spine_node if local_tag(n) == "itemref"]
        if not refs or len(refs) != len(set(refs)):
            raise ValueError(f"{path.name}: empty or repeated spine")
        toc = []
        nav_items = [n for n in items.values() if "nav" in n.get("properties", "").split()]
        if nav_items:
            if len(nav_items) != 1:
                raise ValueError("ambiguous EPUB3 navigation documents")
            nav_path, _ = resolve_ref(opf, nav_items[0].get("href"))
            tree = ET.fromstring(archive.read(nav_path))
            navs = [n for n in tree.iter() if local_tag(n) == "nav" and
                    "toc" in n.get("{http://www.idpf.org/2007/ops}type", "").split()]
            if len(navs) != 1:
                raise ValueError("EPUB3 requires one toc nav")
            for link in navs[0].iter():
                if local_tag(link) == "a" and link.get("href"):
                    source, fragment = resolve_ref(nav_path, link.get("href"))
                    toc.append({"title": "".join(link.itertext()).strip(), "spine": source, "fragment": fragment})
        else:
            ncx = items.get(spine_node.get("toc"))
            if ncx is None:
                matches = [n for n in items.values() if n.get("media-type") == "application/x-dtbncx+xml"]
                if len(matches) != 1:
                    raise ValueError("EPUB requires one NCX or EPUB3 toc nav; add local override after inspection")
                ncx = matches[0]
            ncx_path, _ = resolve_ref(opf, ncx.get("href"))
            toc_xml = ET.fromstring(archive.read(ncx_path))
            for nav in toc_xml.iter():
                if local_tag(nav) != "navPoint":
                    continue
                label = next(n for n in nav if local_tag(n) == "navLabel")
                content = next(n for n in nav if local_tag(n) == "content")
                source, fragment = resolve_ref(ncx_path, content.get("src"))
                toc.append({"title": "".join(label.itertext()).strip(), "spine": source, "fragment": fragment})
        spine = []
        for ref in refs:
            source, _ = resolve_ref(opf, items[ref].get("href"))
            parser = ParagraphParser()
            parser.feed(archive.read(source).decode("utf-8-sig"))
            parser.close()
            parser.flush()
            spine.append(SpineItem(source, parser.paragraphs, parser.anchors,
                                   [e["title"] for e in toc if e["spine"] == source]))
    return Epub(path.name, sha256(raw).hexdigest(), book, volume, spine, toc, titles)


def nonbody_reason(item: SpineItem, book: str) -> str | None:
    for label in item.labels:
        if any(key in label for key in NONBODY):
            return label
    first = item.paragraphs[0].strip() if item.paragraphs else ""
    if first in NONBODY:
        return first
    stem = Path(item.path).stem.lower()
    known = {"cover": "封面", "coverpage": "封面", "title": "书名页", "logo": "标志页",
             "message": "制作信息", "contents": "目录", "summary": "简介",
             "backcover": "封底", "bottom": "封底", "intro": "作者介绍", "postscript": "后记"}
    if stem in known:
        return known[stem]
    if not any(char_count(p) for p in item.paragraphs):
        return "插图/空项"
    return None


def heading_reason(text: str, arc_titles: set[str], fragment_title: str = "") -> str | None:
    text = text.strip()
    if NUMBER_RE.fullmatch(text):
        return "小节号"
    if compact(text) in {compact(t) for t in arc_titles} or is_arc(text):
        return "话标题"
    if fragment_title and compact(text) == compact(fragment_title):
        return "片段标题"
    return None
