"""Uniform secure draw with durable receipt before any source text is output."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha1, sha256
import json
import os
from pathlib import Path
import secrets
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.monogatari.epub_text import char_count, read_epub
from tools.monogatari.paginate import (CONFIG, INDEX, MANIFEST, ROOT, build, encode_json,
                                      load_config, state_paths, sentence_ends, utf8, validate)

LOG = ROOT / ".local/draw_log.jsonl"


def load_verified(config_path=CONFIG):
    config = load_config(config_path)
    manifest_path, index_path = state_paths(config)
    manifest_bytes, index_bytes = manifest_path.read_bytes(), index_path.read_bytes()
    manifest, index = json.loads(manifest_bytes), json.loads(index_bytes)
    if sha256(manifest_bytes).hexdigest() != index["manifest_sha256"]:
        raise ValueError("manifest/index mismatch; run paginate.py after review")
    validate(manifest, index)
    # Verification is bound to the registry, never to folder-wide discovery.
    # Both show and draw pass through this check before output or entropy.
    rebuilt_m, rebuilt_i = build(config, manifest, scan_all=False)
    if encode_json(rebuilt_m) != manifest_bytes or encode_json(rebuilt_i) != index_bytes:
        raise ValueError("source/rules/index changed; review and rebuild before using pages")
    return config, manifest, index, sha256(index_bytes).hexdigest()


def retrieve(epub, book_meta, page):
    """Reconstruct only coordinates allowed by body_spine, including skipped headings."""
    items = {s.path: s for s in epub.spine}
    selected = []
    active = False
    complete = False
    for body in book_meta["body_spine"]:
        item = items[body["spine"]]
        skipped = {p["paragraph"] for p in body["skipped"]}
        for number, text in enumerate(item.paragraphs):
            if number in skipped:
                continue
            key = (item.path, number)
            start = page["start"]
            end = page["end"]
            if key == (start["spine"], start["paragraph"]):
                active = True
                lo = start["offset"]
            else:
                lo = 0
            if not active:
                continue
            hi = end["offset"] if key == (end["spine"], end["paragraph"]) else len(text)
            if not 0 <= lo <= hi <= len(text):
                raise ValueError("invalid page coordinates")
            selected.append(text[lo:hi])
            if key == (end["spine"], end["paragraph"]):
                complete = True
                break
        if complete:
            break
    result = "\n\n".join(selected)
    if not complete or char_count(result) != page["chars"] or sha1(result.encode("utf-8")).hexdigest() != page["sha1"]:
        raise ValueError("page text/coordinate hash mismatch; no text output")
    return result


def context_sentences(text, before=False):
    sentences = []
    for p in text.split("\n\n"):
        start = 0
        for end in sentence_ends(p):
            part = p[start:end]
            if part.strip():
                sentences.append(part)
            start = end
    return "".join(sentences[-2:] if before else sentences[:2])


def page_payload(config, manifest, index, page):
    book = next(b for b in manifest["books"] if b["epub"] == page["epub"])
    epub = read_epub(Path(config["epub_dir"]) / book["epub"])
    if epub.digest != book["sha256"]:
        raise ValueError("EPUB changed during read; no text output")
    output = dict(page)
    output["text"] = retrieve(epub, book, page)
    output["context_before"] = None
    output["context_after"] = None
    for delta, name in ((-1, "context_before"),):
        pos = page["global_page"] - 1 + delta
        if 0 <= pos < len(index["pages"]):
            neighbor = index["pages"][pos]
            if (neighbor["epub"], neighbor["arc_id"]) == (page["epub"], page["arc_id"]):
                output[name] = context_sentences(retrieve(epub, book, neighbor), before=delta < 0)
    return output


def format_page(payload):
    name = payload["book"] + (f'（{payload["volume"]}）' if payload["volume"] else "")
    lines = [f"第 {payload['global_page']} 页 ｜ {name} 第 {payload['book_page']} 页",
             f"{payload['arc'] or '（话名待确认）'} · 本话第 {payload['arc_page']}/{payload['arc_total']} 页"]
    if payload["context_before"] is not None:
        lines.append("[前文语境] " + payload["context_before"])
    lines.append(payload["text"])
    if payload["context_after"] is not None:
        lines.append("[后文语境] " + payload["context_after"])
    return "\n\n".join(lines)


@contextmanager
def locked_log(path):
    """Lock the journal itself; no auxiliary lock file or retrying draw."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as log:
        log.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(log.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(log.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            log.seek(0)
            existing = log.read()
            if existing and not existing.endswith(b"\n"):
                raise ValueError("uncertain partial draw receipt; inspect log, do not redraw")
            for line in existing.splitlines():
                receipt = json.loads(line)
                if not isinstance(receipt, dict) or not {"ts", "question", "global_page", "index_sha256"} <= receipt.keys():
                    raise ValueError("invalid draw receipt; inspect journal before another draw")
            yield log
        finally:
            log.seek(0)
            if os.name == "nt":
                msvcrt.locking(log.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(log.fileno(), fcntl.LOCK_UN)


def commit_draw(question, pages, index_digest, log_path=LOG, chooser=None):
    """Persist selected identity before extraction/output; never retry entropy."""
    choose = chooser or secrets.randbelow
    page = None
    try:
        with locked_log(log_path) as log:
            page = pages[choose(len(pages))]
            receipt = {"ts": datetime.now(timezone.utc).isoformat(), "question": question,
                       **{key: page[key] for key in ("global_page", "book", "volume", "book_page", "arc", "arc_page", "arc_total")},
                       "index_sha256": index_digest}
            encoded = (json.dumps(receipt, ensure_ascii=False) + "\n").encode("utf-8")
            log.seek(0, os.SEEK_END)
            if log.write(encoded) != len(encoded):
                raise OSError("short journal write")
            log.flush()
            os.fsync(log.fileno())
        return page, receipt
    except BaseException:
        if page is not None:
            print(f"抽取已消费；全局页 {page['global_page']}；index {index_digest}。日志状态可能不完整；禁止重抽，核对日志后用 show 恢复。", file=sys.stderr)
        raise


def select_show(pages, page=None, book=None, book_page=None, volume=None):
    if page is not None:
        if page < 1 or page > len(pages):
            raise ValueError(f"--page must be within 1..{len(pages)}")
        return pages[page - 1]
    matches = [p for p in pages if p["book_page"] == book_page and (volume is None or p["volume"] == volume)
               and book in {p["book"], p["book"] + (f'（{p["volume"]}）' if p["volume"] else ""),
                            p["book"] + (f'({p["volume"]})' if p["volume"] else "")}]
    if len(matches) != 1:
        raise ValueError("book/page not found or ambiguous; specify --volume or full book name with volume")
    return matches[0]


def main(argv=None):
    utf8()
    parser = argparse.ArgumentParser(description="draw 消费一次安全随机并先记日志；show 只查看既有页，不消费随机。")
    parser.add_argument("--config", type=Path, default=CONFIG)
    subs = parser.add_subparsers(dest="command", required=True)
    draw = subs.add_parser("draw", help="新抽一次；输出失败用日志页码 show，不要重复 draw")
    draw.add_argument("--question", required=True)
    show = subs.add_parser("show", help="读取指定页，无抽取和日志副作用")
    choice = show.add_mutually_exclusive_group(required=True)
    choice.add_argument("--page", type=int)
    choice.add_argument("--book")
    show.add_argument("--book-page", type=int)
    show.add_argument("--volume", help="同名分册必须指定上/中/下/黑/白，或在 --book 中包含分册")
    for sub in (draw, show):
        sub.add_argument("--json", action="store_true", help="结构化页码、正文和同话语境；不将正文存盘")
    args = parser.parse_args(argv)
    if args.command == "draw" and not args.question.strip():
        parser.error("--question cannot be blank")
    if args.command == "show":
        if args.book is not None and (args.book_page is None or args.book_page < 1):
            parser.error("--book requires a positive --book-page")
        if args.page is not None and (args.book_page is not None or args.volume is not None):
            parser.error("--book-page/--volume require --book, not --page")
    consumed = None
    try:
        config, manifest, index, digest = load_verified(args.config)
        if args.command == "draw":
            if any(b["manual_overrides"].get("boundary_status") == "LOCAL-REVIEW" for b in manifest["books"]):
                raise ValueError("LOCAL-REVIEW: arc boundary decision pending; draw disabled before entropy")
            if manifest["review_decisions"].get("short_arc_tails") != "confirmed":
                raise ValueError("LOCAL-REVIEW: short arc tail acceptance pending; draw disabled before entropy")
            page, receipt = commit_draw(args.question, index["pages"], digest, Path(config["log_path"]))
            consumed = page["global_page"]
        else:
            page = select_show(index["pages"], args.page, args.book, args.book_page, args.volume)
        payload = page_payload(config, manifest, index, page)
        if args.command == "draw":
            payload["receipt"] = receipt
        print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else format_page(payload), flush=True)
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        if consumed is not None:
            print(f"抽取已记入日志：全局页 {consumed}。请用 show --page {consumed} 恢复；不要重抽。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
