"""Read-only lookup: where a global page sits in its arc, and the arc card for that spot.

No randomness, no log, no EPUB text. Stage page ranges are derived from the
public progress anchors against the local page index, so the result does not
depend on the local page cache.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.monogatari.paginate import CONFIG, load_config, state_paths, utf8
from tools.monogatari.validate_cards import CARDS, derive_pages, page_key


def lookup(page_no, cards_doc, index, derived=None):
    """`derived` may carry a precomputed derive_pages() result for repeated lookups."""
    pages = index["pages"]
    if not 1 <= page_no <= len(pages):
        raise ValueError(f"page must be between 1 and {len(pages)}")
    page = pages[page_no - 1]
    key = page_key(page)
    card = cards_doc["cards"][key]
    ranges = (derived or derive_pages(cards_doc, index))[key]["stages"]
    pos = next(i for i, r in enumerate(ranges) if r["global_from"] <= page_no <= r["global_to"])
    stage, span = card["stages"][pos], ranges[pos]
    previous = None
    if pos > 0:
        previous = {"name": card["stages"][pos - 1]["name"], "summary": card["stages"][pos - 1]["summary"]}
    return {
        "global_page": page_no,
        "key": key,
        "book": page["book"],
        "volume": page["volume"],
        "book_page": page["book_page"],
        "arc": card["arc"],
        "arc_page": page["arc_page"],
        "arc_total": page["arc_total"],
        "anomaly": card["anomaly"],
        "protagonist": card["protagonist"],
        "themes": card["themes"],
        "stage_no": pos + 1,
        "stage_total": len(card["stages"]),
        "stage": {"name": stage["name"], "summary": stage["summary"], "cast": stage["cast"],
                  "global_from": span["global_from"], "global_to": span["global_to"]},
        "page_in_stage": page_no - span["global_from"] + 1,
        "stage_pages": span["global_to"] - span["global_from"] + 1,
        "previous_stage": previous,
        # Same-stage pages before this one: readable background. Pages after it stay unread.
        "prior_pages": [span["global_from"], page_no - 1] if page_no > span["global_from"] else None,
    }


def format_lookup(r):
    name = r["book"] + (f'（{r["volume"]}）' if r["volume"] else "")
    s = r["stage"]
    lines = [
        f"第 {r['global_page']} 页 ｜ {name} 第 {r['book_page']} 页",
        f"{r['arc'] or '（无话名）'} · 本话第 {r['arc_page']}/{r['arc_total']} 页",
        "",
        "## 话卡",
        f"怪异：{r['anomaly']['what']}",
        f"  它替人承担了什么：{r['anomaly']['bears']}",
        f"  没有它，必须承认什么：{r['anomaly']['must_admit']}",
        f"主角：{r['protagonist']}",
        "主旨：",
        *[f"  - {t['title']}：{t['note']}" for t in r["themes"]],
        "",
        f"## 所在阶段：第 {r['stage_no']}/{r['stage_total']} 段「{s['name']}」"
        f"（第 {s['global_from']}–{s['global_to']} 页；本页是本段第 {r['page_in_stage']}/{r['stage_pages']} 页）",
        s["summary"],
        *[f"- {c['name']}：{c['state']}" for c in s["cast"]],
    ]
    if r["page_in_stage"] < r["stage_pages"]:
        lines.append("（人物栏描述整段；本页之后才发生的内容，不能当作已经发生。以本页原文为准。）")
    if r["prior_pages"]:
        a, b = r["prior_pages"]
        rng = f"第 {a} 页" if a == b else f"第 {a}–{b} 页"
        lines.append(f"前情可查（同段、本页之前，只读）：{rng}；用 draw.py show --page 查看。本页之后的页不看。")
    if r["previous_stage"]:
        p = r["previous_stage"]
        lines += ["", f"## 上一段「{p['name']}」", p["summary"]]
    return "\n".join(lines)


def main(argv=None):
    utf8()
    parser = argparse.ArgumentParser(description="只读查询：某一全局页所在的话卡与阶段人物栏；不抽取、不写日志、不读原文。")
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--page", type=int, required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        cards_doc = json.loads(CARDS.read_text(encoding="utf-8"))
        config = load_config(args.config)
        manifest_path, index_path = state_paths(config)
        from hashlib import sha256
        index = json.loads(index_path.read_text(encoding="utf-8"))
        if sha256(manifest_path.read_bytes()).hexdigest() != index["manifest_sha256"]:
            raise ValueError("manifest/index mismatch")
        result = lookup(args.page, cards_doc, index)
    except (OSError, ValueError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else format_lookup(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
