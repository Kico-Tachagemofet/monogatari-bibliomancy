"""Build/check a deterministic, text-free index of local EPUBs."""
from __future__ import annotations

import argparse
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from hashlib import sha1, sha256
import json
from pathlib import Path
import re
import statistics
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.monogatari.catalog import BOOK_ORDER, identify, default_overrides, arc_boundaries
from tools.monogatari.epub_text import (Epub, char_count, compact, heading_reason,
                                      is_arc, nonbody_reason, read_epub)

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
CONFIG = ROOT / "config.local.json"
MANIFEST = ROOT / ".local/manifest.json"
INDEX = ROOT / ".local/page_index.json"
VERSION = 1
END_CHARS = "。！？…"
CLOSERS = "」』）”’\"')】〕〉》］｝]}〗〙〛"
SENTENCE = re.compile("[" + re.escape(END_CHARS) + "]+[" + re.escape(CLOSERS) + "]*")


def utf8():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def encode_json(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def load_config(path=CONFIG):
    path = Path(path).resolve()
    config = json.loads(path.read_text(encoding="utf-8"))
    required = {"epub_dir", "target_chars", "merge_tail_below", "fallback_threshold", "state_dir", "log_path"}
    if not required <= set(config) or set(config) - required - {"overrides", "include_books", "epub_files"}:
        raise ValueError("config: unexpected or missing keys; see config.example.json")
    for key in ("epub_dir", "state_dir", "log_path", "overrides"):
        if key in config:
            value = Path(config[key]).expanduser()
            config[key] = str((path.parent / value).resolve() if not value.is_absolute() else value.resolve())
    for key in ("target_chars", "merge_tail_below", "fallback_threshold"):
        if type(config[key]) is not int or config[key] <= 0:
            raise ValueError(f"{key} must be a positive integer")
    if not config["merge_tail_below"] < config["target_chars"] < config["fallback_threshold"]:
        raise ValueError("require merge_tail_below < target_chars < fallback_threshold")
    if "include_books" in config:
        included = config["include_books"]
        if not isinstance(included, list) or not included or any(not isinstance(k, str) or k not in {b + "/" + (v or "-") for b, v in BOOK_ORDER} for k in included) or len(included) != len(set(included)):
            raise ValueError("include_books requires unique supported book/volume keys")
    if "epub_files" in config:
        names = config["epub_files"]
        if not isinstance(names, list) or not names or any(not isinstance(n, str) or not n.lower().endswith(".epub") or any(c in n for c in "/\\:") for n in names) or len({n.casefold() for n in names}) != len(names):
            raise ValueError("epub_files must contain unique leaf .epub filenames; selection never determines identity")
    return config


def state_paths(config):
    root = Path(config["state_dir"])
    return root / "manifest.json", root / "page_index.json"


def local_overrides(config):
    if "overrides" not in config:
        return {"identities": {}, "books": {}}
    value = json.loads(Path(config["overrides"]).read_text(encoding="utf-8"))
    if set(value) != {"identities", "books"} or not all(isinstance(v, dict) for v in value.values()):
        raise ValueError("overrides require identities and books objects")
    allowed = {"arc_aliases", "boundaries", "item_exclusions", "paragraph_exclusions", "front_matter_before_arc", "arc_mode", "arc_title"}
    for key, rules in value["books"].items():
        if key not in {b + "/" + (v or "-") for b, v in BOOK_ORDER} or not isinstance(rules, dict) or set(rules) - allowed:
            raise ValueError("unknown book or rule in local overrides")
    return value


@dataclass
class Paragraph:
    spine: str
    number: int
    text: str
    fragment: int | None = None


def coordinate(p: Paragraph, offset: int):
    return {"spine": p.spine, "paragraph": p.number, "offset": offset}


def prepare_book(epub: Epub, overrides: dict):
    paths = {item.path: i for i, item in enumerate(epub.spine)}
    for path in overrides["item_exclusions"]:
        if path not in paths:
            raise ValueError(f"stale item override: {epub.filename}/{path}")
    exclusions = {(e["spine"], e["paragraph"]): e["reason"] for e in overrides["paragraph_exclusions"]}
    for path, p in exclusions:
        if path not in paths or not 0 <= p < len(epub.spine[paths[path]].paragraphs):
            raise ValueError("stale paragraph override")
    mode = overrides["arc_mode"]
    if mode not in {"ncx", "single", "fragments"}:
        raise ValueError("unknown arc_mode")
    boundaries = arc_boundaries(epub, overrides)
    fragments = {}
    for entry in epub.toc:
        if mode == "fragments":
            match = re.fullmatch(r"([0-9]{1,3})\s+.+", entry["title"])
            if match:
                if entry["spine"] not in paths or entry["fragment"]:
                    raise ValueError("fragment layout needs local review; use one spine item per fragment")
                if entry["spine"] in fragments:
                    raise ValueError("duplicate fragment target")
                fragments[entry["spine"]] = (int(match[1]), entry["title"])
    if mode == "fragments" and not fragments:
        raise ValueError("no fragment boundaries; add local override")
    keys = [(paths[b["spine"]], b["paragraph"]) for b in boundaries]
    front_end = min(paths[p] for p in fragments) if mode == "fragments" else keys[0][0]
    arc_titles = {b["title"] for b in boundaries if b["title"]}
    arc_titles.update(e["title"] for e in epub.toc if is_arc(e["title"]))
    arc_titles.update(n for names in overrides.get("arc_aliases", {}).values() for n in names)
    arcs = [[] for _ in boundaries]
    body_items, excluded_items = [], []
    for item in epub.spine:
        reason = overrides["item_exclusions"].get(item.path) or nonbody_reason(item, epub.book)
        if not reason and overrides.get("front_matter_before_arc") and paths[item.path] < front_end:
            reason = "书级规则：首话前置材料"
        if reason:
            excluded_items.append({"spine": item.path, "title": " / ".join(item.labels) or reason, "reason": reason})
            continue
        skipped, kept = [], []
        for pno, text in enumerate(item.paragraphs):
            fragment, fragment_title = fragments.get(item.path, (None, ""))
            reason = exclusions.get((item.path, pno)) or heading_reason(text, arc_titles, fragment_title)
            if reason:
                skipped.append({"paragraph": pno, "length": len(text), "sha256": sha256(text.encode()).hexdigest(), "reason": reason})
                continue
            arc = bisect_right(keys, (paths[item.path], pno)) - 1
            if arc < 0 or mode == "fragments" and fragment is None:
                raise ValueError(f"LOCAL-REVIEW: unclassified content {epub.filename}/{item.path}:{pno}")
            arcs[arc].append(Paragraph(item.path, pno, text, fragment))
            kept.append(pno)
        if kept:
            body_items.append({"spine": item.path, "paragraph_count": len(item.paragraphs), "skipped": skipped})
        else:
            excluded_items.append({"spine": item.path, "title": " / ".join(item.labels) or "标题页", "reason": "仅话标题/小节号"})
    if any(not arc for arc in arcs):
        raise ValueError(f"LOCAL-REVIEW: empty arc in {epub.filename}")
    meta = {"epub": epub.filename, "sha256": epub.digest, "book": epub.book, "volume": epub.volume,
            "manual_overrides": overrides, "body_spine": body_items, "excluded_spine": excluded_items,
            "arcs": [{"id": i + 1, "title": b["title"], "ncx_start": {"spine": b["spine"], "paragraph": b["paragraph"]},
                      "start": coordinate(arcs[i][0], 0), "end": coordinate(arcs[i][-1], len(arcs[i][-1].text))}
                     for i, b in enumerate(boundaries)]}
    return meta, arcs


def sentence_ends(text: str) -> list[int]:
    ends = [m.end() for m in SENTENCE.finditer(text)]
    # Trailing whitespace stays on the preceding side; no whitespace-only page.
    if ends and not text[ends[-1]:].strip():
        ends[-1] = len(text)
    elif not ends or ends[-1] != len(text):
        ends.append(len(text))
    return ends


def units_for(paragraphs, config):
    units = []
    for p in paragraphs:
        start = 0
        for end in sentence_ends(p.text):
            count = char_count(p.text[start:end])
            if count > config["fallback_threshold"]:
                # A long unpunctuated unit ending at a paragraph boundary is
                # kept intact (the first fallback prescribed in the handoff).
                # Otherwise use comma stops closest to the target, never an
                # arbitrary character offset. Lack of any safe stop fails closed.
                if end == len(p.text):
                    units.append((p, start, end, "fallback-paragraph"))
                else:
                    cursor = start
                    commas = [m.end() + start for m in re.finditer(r"[，,]", p.text[start:end])]
                    while char_count(p.text[cursor:end]) > config["fallback_threshold"]:
                        candidates = [c for c in commas if c > cursor]
                        if not candidates:
                            raise ValueError(f"no safe fallback stop at {p.spine}:{p.number}")
                        cut = min(candidates, key=lambda c: (abs(char_count(p.text[cursor:c]) - config["target_chars"]), c))
                        units.append((p, cursor, cut, "fallback-comma"))
                        cursor = cut
                    if cursor < end:
                        units.append((p, cursor, end, "fallback-comma"))
            else:
                units.append((p, start, end, ""))
            start = end
    return units


def render_units(units):
    chunks = []
    last = None
    for p, lo, hi, _ in units:
        key = (p.spine, p.number)
        if key == last:
            chunks[-1] += p.text[lo:hi]
        else:
            chunks.append(p.text[lo:hi])
        last = key
    return "\n\n".join(chunks)


def paginate_arc(paragraphs, config):
    units = units_for(paragraphs, config)
    cumulative = [0]
    for p, lo, hi, _ in units:
        cumulative.append(cumulative[-1] + char_count(p.text[lo:hi]))
    spans = []
    start = 0
    while start < len(units):
        near = bisect_left(cumulative, cumulative[start] + config["target_chars"], lo=start + 1)
        options = [i for i in (near - 1, near) if start < i <= len(units)]
        end = min(options, key=lambda i: (abs(cumulative[i] - cumulative[start] - config["target_chars"]), i))
        spans.append([start, end, []])
        start = end
    if len(spans) > 1 and cumulative[spans[-1][1]] - cumulative[spans[-1][0]] < config["merge_tail_below"]:
        tail = spans.pop()
        spans[-1][1] = tail[1]
        spans[-1][2].append("tail-merged")
    pages = []
    for start, end, flags in spans:
        selected = units[start:end]
        flags += sorted({u[3] for u in selected if u[3]})
        text = render_units(selected)
        p0, lo, _, _ = selected[0]
        p1, _, hi, _ = selected[-1]
        count = char_count(text)
        if count < 800 and end < len(units) and units[end][3]:
            flags.append("fallback-adjacent")
        if count < 800 and end == len(units):
            flags.append("short-arc-tail")
        pages.append({"start": coordinate(p0, lo), "end": coordinate(p1, hi), "chars": count,
                      "sha1": sha1(text.encode("utf-8")).hexdigest(),
                      "fragments": sorted({u[0].fragment for u in selected if u[0].fragment is not None}),
                      "exceptions": flags})
    return pages


def build(config, previous=None, *, scan_all=False):
    """Rebuild the registered corpus; discovery requires explicit scan_all.

    Verification never enumerates or opens unregistered EPUBs. Even an explicit
    discovery run must not silently drop or replace a previously registered book.
    """
    root = Path(config["epub_dir"])
    previous_books = {}
    identities = set()
    for book in (previous or {}).get("books", []):
        name = book["epub"]
        if not isinstance(name, str) or not name.lower().endswith(".epub") or any(c in name for c in "/\\:"):
            raise ValueError("registered EPUB must be a filename within the configured directory")
        if name.casefold() in identities:
            raise ValueError(f"duplicate registered EPUB: {name}")
        identities.add(name.casefold())
        previous_books[name] = book
    if not previous_books and not scan_all:
        raise ValueError("registered manifest required; initial build requires explicit --build")
    # Check every registered file before parsing anything, including corrupt ZIPs.
    # read_epub's digest is checked again below in case a file changes mid-build.
    for name, old in previous_books.items():
        path = root / name
        if not path.is_file():
            raise ValueError(f"registered EPUB missing: {name}")
        if sha256(path.read_bytes()).hexdigest() != old["sha256"]:
            raise ValueError(f"EPUB changed; review overrides before reindexing: {name}")
    books, pages = [], []
    overrides_doc = local_overrides(config)
    selected = [root / n for n in config["epub_files"]] if "epub_files" in config else list(root.glob("*.epub")) if scan_all else []
    if scan_all and previous_books and not set(previous_books) <= {p.name for p in selected}:
        raise ValueError("cannot drop registered files; use a separate state_dir")
    files = selected if scan_all else [root / name for name in previous_books]
    if not files:
        raise ValueError("no EPUB files at configured directory")
    parsed = []
    seen = set()
    include = set(config.get("include_books", [b + "/" + (v or "-") for b, v in BOOK_ORDER]))
    for path in files:
        old = previous_books.get(path.name)
        epub = read_epub(path)
        if old and old["sha256"] != epub.digest:
            raise ValueError(f"EPUB changed during build: {path.name}")
        explicit = overrides_doc["identities"].get(path.name)
        identify(epub, explicit)
        key = (epub.book, epub.volume)
        if epub.book + "/" + (epub.volume or "-") not in include:
            if old:
                raise ValueError("cannot drop a registered book; create a separate state directory")
            continue
        if key in seen:
            raise ValueError(f"duplicate edition of {key}; keep one edition in the input directory")
        seen.add(key)
        overrides = default_overrides(epub)
        overrides.update(overrides_doc["books"].get(epub.book + "/" + (epub.volume or "-"), {}))
        parsed.append((epub, overrides))
    if not parsed:
        raise ValueError("no selected supported books")
    if scan_all:
        missing = include - {b + "/" + (v or "-") for b, v in seen}
        if missing:
            raise ValueError(f"selected books missing: {sorted(missing)}; set include_books for a subset")
    for epub, overrides in sorted(parsed, key=lambda pair: BOOK_ORDER.index((pair[0].book, pair[0].volume))):
        book, arcs = prepare_book(epub, overrides)
        book_page = 0
        for arc_meta, paragraphs in zip(book["arcs"], arcs):
            arc_pages = paginate_arc(paragraphs, config)
            arc_meta["global_pages"] = [len(pages) + 1, len(pages) + len(arc_pages)]
            arc_meta["book_pages"] = [book_page + 1, book_page + len(arc_pages)]
            for n, page in enumerate(arc_pages, 1):
                book_page += 1
                pages.append({"global_page": len(pages) + 1, "epub": epub.filename,
                              "book": epub.book, "volume": epub.volume, "book_page": book_page,
                              "arc_id": arc_meta["id"], "arc": arc_meta["title"], "arc_page": n,
                              "arc_total": len(arc_pages), **page})
        book["page_count"] = book_page
        books.append(book)
    decisions = (previous or {}).get("review_decisions", {"short_arc_tails": "confirmed"})
    manifest = {"schema_version": VERSION, "sort_order": "semantic-catalog-v1",
                "review_decisions": decisions, "books": books}
    index = {"schema_version": VERSION, "rules": {k: config[k] for k in ("target_chars", "merge_tail_below", "fallback_threshold")},
             "manifest_sha256": sha256(encode_json(manifest)).hexdigest(), "pages": pages}
    return manifest, index


def validate(manifest, index):
    errors = []
    if not index["pages"]:
        errors.append("empty index")
    for n, p in enumerate(index["pages"], 1):
        if p["global_page"] != n:
            errors.append("nonconsecutive global page")
        if not 800 <= p["chars"] <= 1500 and not p["exceptions"]:
            errors.append(f"page {n}: length outside 800–1500 without exception")
        if p["chars"] <= 0:
            errors.append(f"page {n}: empty page")
    def strings(value):
        if isinstance(value, dict):
            return all(strings(v) for v in value.values())
        if isinstance(value, list):
            return all(strings(v) for v in value)
        return not isinstance(value, str) or len(value) <= 80
    if not strings(index) or not strings(manifest):
        errors.append("string longer than 80 characters in metadata")
    if errors:
        raise ValueError("; ".join(errors))


def stats(manifest, index):
    rows = []
    for b in manifest["books"]:
        lengths = [p["chars"] for p in index["pages"] if p["epub"] == b["epub"]]
        rows.append((b["book"] + (f'（{b["volume"]}）' if b["volume"] else ""),
                     len(lengths), len(b["arcs"]), min(lengths), statistics.median(lengths), max(lengths)))
    return rows


def main(argv=None):
    utf8()
    parser = argparse.ArgumentParser(description="显式建库或只读校验；不抽取。歧义请填写本地覆盖配置。")
    parser.add_argument("--config", type=Path, default=CONFIG)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="只读核验登记书、索引及当前规则；未登记文件不读取")
    mode.add_argument("--build", action="store_true", help="显式扫描配置目录，识别并登记所选书籍；已登记源不允许变更")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        manifest_path, index_path = state_paths(config)
        if manifest_path.exists() != index_path.exists():
            raise ValueError("partial local build; preserve files and use a fresh state_dir after review")
        previous = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else None
        manifest, index = build(config, previous, scan_all=args.build)
        validate(manifest, index)
        outputs = {manifest_path: encode_json(manifest), index_path: encode_json(index)}
        for path, data in outputs.items():
            if args.check:
                if not path.exists() or path.read_bytes() != data:
                    raise ValueError("stale/missing local index; review and run explicit --build")
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                temp = path.with_suffix(".tmp")
                temp.write_bytes(data)
                temp.replace(path)
        for row in stats(manifest, index):
            print(f"{row[0]}: {row[1]} 页 / {row[2]} 话")
        print(f"{'CHECK OK' if args.check else 'BUILT'}: {len(index['pages'])} 页")
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
