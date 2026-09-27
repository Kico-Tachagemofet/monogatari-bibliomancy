"""Validate and deterministically render paraphrased arc cards, without source dumps."""
from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import defaultdict
from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from hashlib import sha256
import json
from pathlib import Path
import re
import sys
import unicodedata

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.monogatari.draw import retrieve
from tools.monogatari.epub_text import read_epub
from tools.monogatari.paginate import CONFIG, INDEX, MANIFEST, load_config, state_paths, utf8

HERE = Path(__file__).resolve().parent
CARDS = HERE.parents[1] / "data/arc_cards.json"
SAMPLE_KEYS = {"伤物语/-/1", "化物语/上/1", "化物语/上/2"}
CAST_BATCH_KEYS = set()
SCHEMA = {
    "key": "book/volume/arc_no; '-' denotes no volume; arc_no is one-based within the book volume",
    "progress": "Cumulative non-whitespace character fraction within the arc; punctuation counts; ROUND_HALF_UP to 0.001",
    "interval": "[from,to); first from=0; final to=1; shared boundaries belong to the following stage",
}
CAST_SCHEMA = {**SCHEMA, "stages": "Partial rollout: summary (one sentence) and cast (name/state, one to three sentences); unconverted cards retain desc"}
QUANTUM = Decimal("0.001")
# Explicit proper names only: ordinary words, single characters, and explanatory
# phrases must never become exemptions just because a validation failed.
PROPER_NOUNS = (
    "阿良良木历", "阿良良木火怜", "阿良良木月火", "战场原黑仪", "八九寺真宵", "神原骏河",
    "千石抚子", "羽川翼", "忍野咩咩", "忍野忍", "忍野扇", "贝木泥舟", "影缝余弦",
    "斧乃木余接", "斧乃木余切", "卧烟伊豆湖", "卧烟远江", "老仓育", "沼地蜡花",
    "手折正弦", "德拉曼兹路基", "艾比所特", "艾皮索德", "奇洛金卡达",
    "姬丝秀忒", "雅赛劳拉莉昂", "刃下心", "Kiss-shot", "KissShot",
    "黑仪", "真宵", "骏河", "抚子", "羽川", "火怜", "月火", "忍野", "贝木", "斧乃木",
    "老仓", "沼地", "卧烟", "余弦", "余接", "余切", "重蟹", "迷牛", "雨魔", "障猫",
    "苛虎", "蛇切绳", "围猎火蜂", "杜鹃", "白蛇神社", "北白蛇神社", "直江津高中",
)


def normalize(text):
    return "".join(c for c in text if not c.isspace() and not unicodedata.category(c).startswith("P"))


def card_key(book, volume, arc_no):
    if not book or "/" in book or "/" in volume or volume == "-":
        raise ValueError("book/volume cannot contain slash; '-' is reserved for absent volume")
    return f"{book}/{volume or '-'}/{arc_no}"


def page_key(page):
    return card_key(page["book"], page["volume"], page["arc_id"])


def grouped_pages(index):
    groups = defaultdict(list)
    for page in index["pages"]:
        groups[page_key(page)].append(page)
    for key, pages in groups.items():
        if len({p["epub"] for p in pages}) != 1:
            raise ValueError(f"{key}: more than one local edition; select a single edition index")
        if [p["arc_page"] for p in pages] != list(range(1, len(pages) + 1)):
            raise ValueError(f"{key}: nonconsecutive local arc pages")
        if any(p["global_page"] != pages[0]["global_page"] + i for i, p in enumerate(pages)):
            raise ValueError(f"{key}: nonconsecutive global pages")
    return groups


def cumulative_chars(pages):
    result = [0]
    for page in pages:
        if type(page["chars"]) is not int or page["chars"] <= 0:
            raise ValueError("page character counts must be positive integers")
        result.append(result[-1] + page["chars"])
    return result


def progress_at(offset, total):
    return (Decimal(offset) / Decimal(total)).quantize(QUANTUM, rounding=ROUND_HALF_UP)


def progress_boundary(value, cumulative):
    """Map a three-decimal fraction to a zero-based page-start boundary.

    Quantization comes BEFORE page location: find a page start whose exact
    cumulative-character ratio rounds HALF_UP to the supplied fraction. A
    unique match reconstructs the source page start, even if rounding moved
    the numeric point into the preceding page (109/224 original stage starts).
    Multiple matches fail closed; this precision cannot identify that boundary.
    For a different edition with no match, use the page CONTAINING value*total
    (half-open page intervals; exact page starts belong to the following page).
    0 and 1 are absolute beginning/end sentinels. Stage `to` is exclusive: its
    boundary's preceding page is the final included page. Mapping that creates
    an empty stage is rejected rather than silently moving another boundary.
    """
    fraction = Decimal(str(value))
    if not fraction.is_finite() or not 0 <= fraction <= 1 or fraction != fraction.quantize(QUANTUM):
        raise ValueError("progress must be a finite 0..1 number at 0.001 precision")
    if fraction == 0:
        return 0
    if fraction == 1:
        return len(cumulative) - 1
    matches = [i for i, n in enumerate(cumulative) if progress_at(n, cumulative[-1]) == fraction]
    if len(matches) > 1:
        raise ValueError("ambiguous rounded page boundary; local edition needs review")
    if matches:
        return matches[0]
    return bisect_right(cumulative, fraction * cumulative[-1]) - 1


def derive_pages(data, index):
    derived = {}
    for key, pages in grouped_pages(index).items():
        card = data["cards"][key]
        cumulative = cumulative_chars(pages)
        stages = []
        cursor = 0
        for stage in card["stages"]:
            lo = progress_boundary(stage["from"], cumulative)
            hi = progress_boundary(stage["to"], cumulative)
            if lo != cursor or not lo < hi <= len(pages):
                raise ValueError(f"{key}: rounded stages collapse or leave gaps; local review required")
            stages.append({"name": stage["name"], "global_from": pages[lo]["global_page"],
                           "global_to": pages[hi - 1]["global_page"], "arc_from": lo + 1, "arc_to": hi})
            cursor = hi
        if cursor != len(pages):
            raise ValueError(f"{key}: incomplete local stage coverage")
        derived[key] = {"global_range": [pages[0]["global_page"], pages[-1]["global_page"]], "stages": stages}
    return derived


def narrative_fields(card):
    for k, v in card["anomaly"].items():
        yield f"anomaly.{k}", v
    yield "protagonist", card["protagonist"]
    for i, theme in enumerate(card["themes"]):
        yield f"themes[{i}].title", theme["title"]
        yield f"themes[{i}].note", theme["note"]
    for i, stage in enumerate(card["stages"]):
        yield f"stages[{i}].name", stage["name"]
        if "desc" in stage:
            yield f"stages[{i}].desc", stage["desc"]
        else:
            yield f"stages[{i}].summary", stage["summary"]
            for j, person in enumerate(stage["cast"]):
                yield f"stages[{i}].cast[{j}].name", person["name"]
                yield f"stages[{i}].cast[{j}].state", person["state"]
        if "section_hint" in stage:
            yield f"stages[{i}].section_hint", stage["section_hint"]


def overlap_hits(fields, source_pages, proper_nouns=PROPER_NOUNS):
    """Return field/offset/page/hash for every forbidden 12-character window.

    Match across page boundaries as well. Diagnostics deliberately return no
    matched source text, so neither stdout nor redirected logs become excerpts.
    A whole proper name must fit inside a window to exempt that window.
    """
    names = tuple(normalize(n) for n in proper_nouns if len(normalize(n)) >= 2)
    candidates = defaultdict(list)
    for field, text in fields:
        cleaned = normalize(text)
        for offset in range(len(cleaned) - 11):
            window = cleaned[offset:offset + 12]
            if not any(name in window for name in names):
                candidates[window].append((field, offset))
    hits = []
    tail = ""
    tail_pages = []
    matched = set()
    for page_number, text in source_pages:
        current = normalize(text)
        combined = tail + current
        owners = tail_pages + [page_number] * len(current)
        for i in range(len(combined) - 11):
            window = combined[i:i + 12]
            if window in candidates:
                for field, offset in candidates[window]:
                    identity = field, offset, owners[i]
                    if identity not in matched:
                        matched.add(identity)
                        hits.append({"field": field, "offset": offset, "page": owners[i],
                                     "sha256": sha256(window.encode("utf-8")).hexdigest()})
        tail, tail_pages = combined[-11:], owners[-11:]
    return hits


def sentence_count(value):
    """Chinese prose sentence terminators; quotes are closers, not new sentences.

    Semicolons separate clauses. Consecutive !/? count as a single terminator;
    an unterminated final clause still counts. Punctuation alone is not prose.
    """
    return sum(bool(normalize(part)) for part in re.split(r'[。！？.!?]+[”’」』）)]*', value))


def cast_keys(data):
    return {key for key, card in data["cards"].items()
            if card["stages"] and all("summary" in s and "cast" in s for s in card["stages"])}


def validate_structure(data, index=None):
    errors = []
    cards_value = data.get("cards")
    if not isinstance(cards_value, dict):
        return ["cards must be an object"]
    if index is None:
        index = {"pages": [{"book": c.get("book"), "volume": c.get("volume"), "arc_id": c.get("arc_no"),
                            "arc": c.get("arc"), "epub": "synthetic", "arc_page": 1, "global_page": i}
                           for i, c in enumerate(cards_value.values(), 1) if isinstance(c, dict)]}
    expected = grouped_pages(index)
    version = data.get("schema_version")
    if set(data) != {"schema_version", "schema", "cards"} or type(version) is not int or version not in (2, 3) or data.get("schema") != (CAST_SCHEMA if version == 3 else SCHEMA):
        errors.append("invalid public v2/v3 schema; local notes belong in the local cache")
    def public_fields(value, path="root"):
        if isinstance(value, dict):
            for key, v in value.items():
                if key.startswith(("global", "arc_from", "arc_to")) or key == "index_sha256":
                    errors.append(f"{path}: forbidden local metadata field")
                if ".epub" in key.casefold():
                    errors.append(f"{path}: edition filename in public key")
                public_fields(v, path + "." + key)
        elif isinstance(value, list):
            for v in value:
                public_fields(v, path)
        elif isinstance(value, str):
            if ".epub" in value.casefold() or re.search(r"[A-Za-z]:[/\\]|\\\\|(?:^|\s)/(?:home|Users|tmp|mnt)/|file://", value):
                errors.append(f"{path}: local filename/path in public data")
            if path.endswith(".status") and value == "kico_approved":
                return
            if any(name in value.casefold() for name in ("kico", "astarion")):
                errors.append(f"{path}: prohibited private name in public text")
    public_fields(data)
    cards = data.get("cards")
    if not isinstance(cards, dict):
        return ["cards must be an object keyed by book/volume/arc_no"]
    for key in sorted(set(expected) - set(cards)):
        errors.append(f"missing card: {key}")
    for key in sorted(set(cards) - set(expected)):
        errors.append(f"unexpected card: {key}")
    required = {"book", "volume", "arc_no", "arc", "anomaly", "protagonist", "themes", "stages", "status"}
    def text(value):
        return isinstance(value, str) and bool(value.strip())
    for key in set(cards) & set(expected):
        c, pages = cards[key], expected[key]
        if not isinstance(c, dict) or set(c) != required:
            errors.append(f"{key}: invalid card fields")
            continue
        for field in ("book", "volume", "arc_no", "arc"):
            source_field = "arc_id" if field == "arc_no" else field
            if c[field] != pages[0][source_field] or field == "arc_no" and type(c[field]) is not int:
                errors.append(f"{key}: {field} differs from index")
        if c["status"] != ("kico_approved" if key in SAMPLE_KEYS else "draft"):
            errors.append(f"{key}: incorrect preserved approval status")
        if not isinstance(c["anomaly"], dict) or set(c["anomaly"]) != {"what", "bears", "must_admit"} or not all(text(v) for v in c["anomaly"].values()):
            errors.append(f"{key}: anomaly requires three nonempty fields")
        if not text(c["protagonist"]):
            errors.append(f"{key}: missing protagonist")
        themes = c["themes"]
        if not isinstance(themes, list) or not 4 <= len(themes) <= 6 or any(not isinstance(t, dict) or set(t) != {"title", "note"} or not all(text(v) for v in t.values()) for t in themes):
            errors.append(f"{key}: themes require 4–6 title/note pairs")
        stages = c["stages"]
        if not isinstance(stages, list) or not stages:
            errors.append(f"{key}: stages must be nonempty")
            continue
        cursor = Decimal(0)
        with_cast = version == 3 and (key in CAST_BATCH_KEYS or any(isinstance(s, dict) and ("summary" in s or "cast" in s) for s in stages))
        for i, s in enumerate(stages):
            fields = {"name", "from", "to"} | ({"summary", "cast"} if with_cast else {"desc"})
            if not isinstance(s, dict) or not fields <= set(s) or set(s) - (fields | {"section_hint"}):
                errors.append(f"{key}: stage {i} invalid fields")
                continue
            if not text(s["name"]) or "section_hint" in s and not text(s["section_hint"]):
                errors.append(f"{key}: stage {i} missing description")
            if with_cast:
                if not text(s["summary"]) or sentence_count(s["summary"]) != 1:
                    errors.append(f"{key}: stage {i} summary requires exactly one nonempty sentence")
                cast = s["cast"]
                if not isinstance(cast, list) or not cast:
                    errors.append(f"{key}: stage {i} cast must be a nonempty array")
                else:
                    names = set()
                    for j, person in enumerate(cast):
                        if not isinstance(person, dict) or set(person) != {"name", "state"} or not all(text(v) for v in person.values()):
                            errors.append(f"{key}: stage {i} cast {j} requires nonempty name/state")
                            continue
                        if person["name"] in names:
                            errors.append(f"{key}: stage {i} duplicate cast name")
                        names.add(person["name"])
                        if not 1 <= sentence_count(person["state"]) <= 3:
                            errors.append(f"{key}: stage {i} cast {j} state requires 1–3 sentences")
            elif not text(s["desc"]):
                errors.append(f"{key}: stage {i} missing description")
            if any(type(s[k]) not in (int, float) for k in ("from", "to")):
                errors.append(f"{key}: stage {i} requires numeric progress")
                continue
            lo, hi = (Decimal(str(s[k])) for k in ("from", "to"))
            if not all(n.is_finite() and 0 <= n <= 1 for n in (lo, hi)):
                errors.append(f"{key}: stage {i} non-finite or out-of-range progress")
                continue
            if any(n != n.quantize(QUANTUM) for n in (lo, hi)):
                errors.append(f"{key}: stage {i} exceeds three decimal places")
            if lo != cursor or not lo < hi:
                errors.append(f"{key}: stage {i} gap/overlap/empty progress interval")
            cursor = hi
        if cursor != 1:
            errors.append(f"{key}: stage coverage incomplete")
        # Check all user-visible strings, excluding the machine status value.
        def walk(value):
            if isinstance(value, str):
                return [value]
            if isinstance(value, dict):
                return [s for k, v in value.items() if k != "status" for s in walk(v)]
            if isinstance(value, list):
                return [s for v in value for s in walk(v)]
            return []
        if any(name in v.casefold() for v in walk(c) for name in ("kico", "astarion")):
            errors.append(f"{key}: prohibited private name in card text")
    return sorted(errors)


def validate(data, index, manifest, config):
    errors = validate_structure(data)
    if errors:
        return errors
    try:
        derive_pages(data, index)
    except ValueError as exc:
        return [str(exc)]
    grouped = defaultdict(list)
    for p in index["pages"]:
        grouped[p["epub"]].append(p)
    for book in manifest["books"]:
        source_path = Path(config["epub_dir"]) / book["epub"]
        if not source_path.is_file():
            print(f"SKIP 12-character overlap: missing {book['book']}/{book['volume'] or '-'}")
            continue
        epub = read_epub(source_path)
        if epub.digest != book["sha256"]:
            errors.append(f"{book['epub']}: EPUB hash differs from manifest")
            continue
        arcs = defaultdict(list)
        for page in grouped[book["epub"]]:
            arcs[page["arc_id"]].append(page)
        for arc_id, pages in arcs.items():
            key = card_key(book["book"], book["volume"], arc_id)
            card = data["cards"][key]
            source = ((p["global_page"], retrieve(epub, book, p)) for p in pages)
            names = PROPER_NOUNS + ((card["arc"],) if card["arc"] else ())
            for hit in overlap_hits(narrative_fields(card), source, names):
                errors.append(f"{key}: excerpt overlap field={hit['field']} offset={hit['offset']} source_page={hit['page']} sha256={hit['sha256']}")
    return errors


def main(argv=None):
    utf8()
    parser = argparse.ArgumentParser(description="校验卡片结构及 12 字防摘录；缺本地书库时明确跳过防摘录。只读。")
    parser.add_argument("--cards", type=Path, default=CARDS)
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--check", action="store_true", help="兼容显式只读检查；本命令始终只读")
    args = parser.parse_args(argv)
    try:
        data = json.loads(args.cards.read_text(encoding="utf-8"))
        errors = validate_structure(data)
        if not errors and args.config.exists():
            config = load_config(args.config)
            manifest_path, index_path = state_paths(config)
            if manifest_path.exists() and index_path.exists():
                raw = manifest_path.read_bytes()
                manifest, index = json.loads(raw), json.loads(index_path.read_bytes())
                if sha256(raw).hexdigest() != index["manifest_sha256"]:
                    raise ValueError("manifest/index mismatch")
                errors = validate(data, index, manifest, config)
                present = {card_key(b["book"], b["volume"], a["id"]) for b in manifest["books"] for a in b["arcs"]}
                for key in sorted(set(data["cards"]) - present):
                    print(f"SKIP 12-character overlap: unregistered {key}")
            else:
                print("SKIP 12-character overlap: no local library; build explicitly first")
        elif not errors:
            print("SKIP 12-character overlap: no local configuration/EPUBs")
        if errors:
            for error in errors:
                print(f"ERROR: {error}", file=sys.stderr)
            return 1
        print(f"CARDS OK: {len(data['cards'])} arcs, {sum(len(c['stages']) for c in data['cards'].values())} stages")
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
