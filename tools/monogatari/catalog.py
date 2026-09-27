"""Semantic book recognition and conservative TOC-to-card matching."""
from __future__ import annotations
import json
from pathlib import Path
import re
import unicodedata

CARDS = Path(__file__).resolve().parents[2] / "data/arc_cards.json"
# Stable identity order preserves the reference corpus, independent of filenames.
BOOK_ORDER = [("伤物语", ""), ("伪物语", "上"), ("伪物语", "下"), ("佰物语", ""),
              ("倾物语", ""), ("凭物语", ""), ("化物语", "上"), ("化物语", "下"),
              ("历物语", ""), ("囮物语", ""), ("恋物语", ""), ("猫物语", "黑"),
              ("猫物语", "白"), ("终物语", "上"), ("终物语", "下"), ("终物语", "中"),
              ("花物语", ""), ("鬼物语", "")]
TRANSLATE = str.maketrans("語傷偽傾憑曆歷戀貓終囮鬼話儀蟹駿猿撫繩貳參", "语伤伪倾凭历历恋猫终囮鬼话仪蟹骏猿抚绳二三")


def normalized(text):
    text = unicodedata.normalize("NFKC", text).translate(TRANSLATE)
    return "".join(c for c in text if c.isalnum())


def arc_name(text):
    text = normalized(text)
    return re.sub(r"^(?:第[零一二三四五六七八九十百终閑闲0-9]+话|最终话)", "", text)


def cards_for(book, volume, cards=None):
    cards = cards if cards is not None else json.loads(CARDS.read_text(encoding="utf-8"))["cards"]
    return sorted([c for c in cards.values() if (c["book"], c["volume"]) == (book, volume)], key=lambda c: c["arc_no"])


def identify(epub, explicit=None, cards=None):
    cards = cards if cards is not None else json.loads(CARDS.read_text(encoding="utf-8"))["cards"]
    identities = {(c["book"], c["volume"]) for c in cards.values()}
    if explicit is not None:
        if not isinstance(explicit, list) or len(explicit) != 2 or tuple(explicit) not in identities:
            raise ValueError("local identity must be [known book, volume]")
        epub.book, epub.volume = explicit
        return epub
    title_candidates = set()
    for title in epub.titles:
        value = normalized(title)
        for book, volume in identities:
            # Reject embedded longer titles (e.g. 续终物语 is not 终物语).
            source = unicodedata.normalize("NFKC", title).translate(TRANSLATE)
            pattern = r"(?<![\u4e00-\u9fff])" + re.escape(book)
            if re.search(pattern, source):
                suffix = value.split(book, 1)[1]
                volumes = {v for b, v in identities if b == book and v}
                stated = next((v for v in volumes if suffix.startswith(v)), None)
                if stated is None or stated == volume:
                    title_candidates.add((book, volume))
    toc_candidates = set()
    for entry in epub.toc:
        label = arc_name(entry["title"])
        hits = {(c["book"], c["volume"]) for c in cards.values() if c["arc"] and arc_name(c["arc"]) == label}
        if hits:
            if toc_candidates and not toc_candidates & hits:
                raise ValueError(f"{epub.filename}: conflicting TOC identities; add identities local override")
            toc_candidates = toc_candidates & hits if toc_candidates else hits
    candidates = title_candidates
    if toc_candidates:
        candidates = candidates & toc_candidates if candidates else toc_candidates
        if title_candidates and not candidates:
            raise ValueError(f"{epub.filename}: metadata/TOC conflict; add identities local override")
    if len(candidates) != 1:
        raise ValueError(f"{epub.filename}: unknown or ambiguous book/volume; filename is only a hint. Add identities local override")
    epub.book, epub.volume = next(iter(candidates))
    return epub


def default_overrides(epub):
    result = {"arc_mode": "ncx", "item_exclusions": {}, "paragraph_exclusions": [], "notes": []}
    key = (epub.book, epub.volume)
    if key == ("猫物语", "白"):
        result.update(arc_mode="single", arc_title="", title_status="deferred", title_deferred=True)
    if key == ("佰物语", ""):
        result.update(arc_mode="fragments", arc_title="佰物语", title_status="confirmed")
    # Book-scoped structural front matter rules. No archive member names or hashes.
    if key in {("佰物语", ""), ("化物语", "下"), ("终物语", "上"), ("终物语", "中"), ("终物语", "下")}:
        result["front_matter_before_arc"] = True
    if key == ("终物语", "中"):
        for item in epub.spine:
            for number, text in enumerate(item.paragraphs):
                value = normalized(text)
                if len(value) < 35 and (re.fullmatch(r"终物语中忍盔甲[A-Z]卷", value) or
                                      re.fullmatch(r"第[一二三四五六七八九十百0-9]+章[0-9]+", value)):
                    result["paragraph_exclusions"].append({"spine": item.path, "paragraph": number, "reason": "重复卷/小节标题"})
    return result


def arc_boundaries(epub, overrides):
    expected = cards_for(epub.book, epub.volume)
    mode = overrides["arc_mode"]
    paths = {s.path: i for i, s in enumerate(epub.spine)}
    if "boundaries" in overrides:
        boundaries = overrides["boundaries"]
        if not isinstance(boundaries, list) or len(boundaries) != len(expected):
            raise ValueError("local boundaries must match all expected arcs")
        result = []
        for c, b in zip(expected, boundaries):
            if set(b) != {"arc_no", "spine", "paragraph"} or b["arc_no"] != c["arc_no"] or b["spine"] not in paths:
                raise ValueError("invalid local boundary; use arc_no/spine/paragraph")
            item = epub.spine[paths[b["spine"]]]
            if type(b["paragraph"]) is not int or not 0 <= b["paragraph"] <= len(item.paragraphs):
                raise ValueError("invalid local boundary paragraph")
            result.append({"title": c["arc"], "spine": b["spine"], "paragraph": b["paragraph"]})
    elif mode != "ncx":
        if len(expected) != 1:
            raise ValueError("single/fragments requires exactly one card")
        result = [{"title": expected[0]["arc"], "spine": epub.spine[0].path, "paragraph": 0}]
    else:
        result = []
        aliases = overrides.get("arc_aliases", {})
        for c in expected:
            names = {arc_name(c["arc"])} | {arc_name(n) for n in aliases.get(str(c["arc_no"]), [])}
            hits = [e for e in epub.toc if arc_name(e["title"]) in names]
            if len(hits) != 1:
                raise ValueError(f"{epub.book}/{epub.volume or '-'}/{c['arc_no']}: missing/ambiguous TOC arc; add books local arc_aliases or boundaries override")
            e = hits[0]
            if e["spine"] not in paths:
                raise ValueError("arc target outside spine")
            item = epub.spine[paths[e["spine"]]]
            if e["fragment"] and e["fragment"] not in item.anchors:
                raise ValueError("missing TOC anchor; add local boundaries override")
            p, offset = item.anchors[e["fragment"]] if e["fragment"] else (0, 0)
            if offset:
                raise ValueError("arc anchor inside paragraph; explicit local review required")
            result.append({"title": c["arc"], "spine": item.path, "paragraph": p})
        matched = {arc_name(c["arc"]) for c in expected} | {arc_name(n) for names in aliases.values() for n in names}
        for e in epub.toc:
            if re.match(r"^(?:第.+话|最终话)", e["title"].strip()) and arc_name(e["title"]) not in matched:
                raise ValueError("unmatched arc-like TOC entry; review local boundaries")
    coords = [(paths[b["spine"]], b["paragraph"]) for b in result]
    if coords != sorted(coords) or len(coords) != len(set(coords)):
        raise ValueError("arc order conflicts with cards or duplicate boundary")
    return result
