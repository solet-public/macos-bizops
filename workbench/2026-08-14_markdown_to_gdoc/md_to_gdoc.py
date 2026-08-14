#!/usr/bin/env python3
"""Convert a markdown file to a Google Doc via dax's g_suite_plugin.

Scope/limitations (read before extending):
- Handles: H1/H3 headings, paragraphs, bullet lists (incl. one level of
  nesting), numbered lists, **bold**, *italic*, `code` inline spans, bare
  URL auto-linking, and GFM pipe tables.
- Broken/placeholder image references (a bare "!Caption.png" line with no
  markdown image syntax around it -- common in Notion-exported markdown that
  lost its `![]()` wrapper) render as a small italic placeholder note, since
  there's no actual image file to embed.
- Tables are NOT rendered as native Docs grid tables. g_suite_plugin's
  `docs_get` only returns flattened plain text (no structural JSON with
  character indices), and there is no raw `documents().get()` passthrough
  exposed -- so there is no reliable way to learn a freshly-inserted table's
  per-cell content indices before writing into it. Guessing the offsets from
  Google's documented (but version-sensitive) table-insertion index scheme
  was judged too risky to get silently wrong with no easy way to verify.
  Instead, each table row renders as one formatted paragraph: bold row
  label, the status column, then the remaining columns as running text.
  This is fully reliable (pure sequential insertText, no structural
  guessing) and still very readable for a checklist-style report. If a
  future dax release exposes raw document structure (or you want to spend
  the round-trips to insert-then-verify via PDF export + visual read), true
  tables are worth revisiting.

Usage: python3 md_to_gdoc.py <input.md> <title>
Prints the created document's URL on success.
"""
import json, re, subprocess, sys, time

CODE_RE = re.compile(r"`([^`]+)`")
BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
ITALIC_RE = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")
URL_RE = re.compile(r"https?://\S+")
IMAGE_PLACEHOLDER_RE = re.compile(r"^!\s*(.+\.(?:png|jpe?g|gif|webp))\s*$", re.IGNORECASE)


def dax_call(process_key, args):
    r = subprocess.run(["dax", "call", process_key, json.dumps(args)], capture_output=True, text=True)
    try:
        return json.loads(r.stdout.strip())
    except Exception:
        print(f"dax call {process_key} failed:\n{r.stdout}\n{r.stderr}", file=sys.stderr)
        raise


def poll_job(job_id, timeout_s=60, interval_s=2):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        resp = dax_call("service_interface::state_service::read_state", {
            "namespace": "core",
            "query": {"table": "job_payload", "filters": {"job_id": job_id}},
        })
        for rec in resp["result"]["data"]["records"]:
            if rec["payload_type"] in ("result", "error"):
                return rec["payload_type"], json.loads(rec["payload_data"])
        time.sleep(interval_s)
    raise TimeoutError(f"job {job_id} did not complete within {timeout_s}s")


def parse_inline_spans(text):
    """Split text into (text, kind) segments. kind: plain, bold, italic, code, link.
    Markers are stripped from output (except link, which keeps the URL as
    both the visible text and the target)."""
    matches = sorted(
        list(CODE_RE.finditer(text))
        + list(BOLD_RE.finditer(text))
        + list(ITALIC_RE.finditer(text))
        + list(URL_RE.finditer(text)),
        key=lambda m: m.start(),
    )
    segments = []
    pos = 0
    for m in matches:
        if m.start() < pos:
            continue  # overlapping match (e.g. a URL inside already-claimed bold span), skip
        if m.start() > pos:
            segments.append((text[pos:m.start()], "plain", None))
        whole = m.group(0)
        if whole.startswith("**"):
            segments.append((m.group(1), "bold", None))
        elif whole.startswith("`"):
            segments.append((m.group(1), "code", None))
        elif whole.startswith("http"):
            segments.append((whole, "link", whole))
        else:
            segments.append((m.group(1), "italic", None))
        pos = m.end()
    if pos < len(text):
        segments.append((text[pos:], "plain", None))
    return segments or [("", "plain", None)]


LIST_ITEM_RE = re.compile(r"^(\s*)([-*]|\d+\.)\s+(.*)$")


def parse_markdown(md_text):
    """Small parser: H1-6 headings, paragraphs, bullet/numbered lists (one
    nesting level, by leading-whitespace amount), GFM pipe tables, and
    broken image-reference placeholder lines."""
    blocks = []
    lines = md_text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue

        heading_match = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading_match:
            blocks.append({"type": "heading", "level": len(heading_match.group(1)), "text": heading_match.group(2)})
            i += 1
            continue

        image_match = IMAGE_PLACEHOLDER_RE.match(line.strip())
        if image_match:
            blocks.append({"type": "image_placeholder", "caption": image_match.group(1)})
            i += 1
            continue

        if line.strip().startswith("|"):
            header = [c.strip() for c in line.strip().strip("|").split("|")]
            i += 1
            if i < len(lines) and re.match(r"^\s*\|?[\s:|-]+\|?\s*$", lines[i]):
                i += 1
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            blocks.append({"type": "table", "header": header, "rows": rows})
            continue

        list_match = LIST_ITEM_RE.match(line)
        if list_match:
            indent, marker, text = list_match.groups()
            level = 1 if len(indent) >= 4 else 0
            ordered = marker != "-" and marker != "*"
            blocks.append({"type": "list_item", "ordered": ordered, "level": level, "text": text})
            i += 1
            continue

        para_lines = [line]
        i += 1
        while (
            i < len(lines) and lines[i].strip()
            and not lines[i].strip().startswith("|")
            and not re.match(r"^#{1,6}\s", lines[i])
            and not LIST_ITEM_RE.match(lines[i])
            and not IMAGE_PLACEHOLDER_RE.match(lines[i].strip())
        ):
            para_lines.append(lines[i])
            i += 1
        blocks.append({"type": "paragraph", "text": " ".join(para_lines)})
    return blocks


HEADING_STYLE = {1: "HEADING_1", 2: "HEADING_2", 3: "HEADING_3", 4: "HEADING_4", 5: "HEADING_5", 6: "HEADING_6"}
BULLET_PRESET = "BULLET_DISC_CIRCLE_SQUARE"
NUMBERED_PRESET = "NUMBERED_DECIMAL_ALPHA_ROMAN"
INDENT_PER_LEVEL_PT = 18  # magnitude in points; Docs infers nesting depth from indentStart


def build_requests(blocks):
    """Walk blocks top to bottom, tracking a cursor, building insert+style requests.

    Requests execute strictly in list order; every index used here is
    computed against the document state as it will exist once all PRIOR
    requests in this same list have applied -- this is the standard safe
    pattern for scripting Docs batchUpdate (see module docstring for why
    tables are the one thing this pattern does NOT cover).
    """
    requests = []
    cursor = 1  # Docs body content starts at index 1

    def insert_paragraph(text, style_name=None, list_meta=None, italic_whole=False):
        nonlocal cursor
        segments = parse_inline_spans(text)
        full_text = "".join(seg for seg, _, _ in segments) + "\n"
        start = cursor
        requests.append({"insertText": {"location": {"index": start}, "text": full_text}})
        para_range = {"startIndex": start, "endIndex": start + len(full_text)}
        if style_name:
            requests.append({"updateParagraphStyle": {
                "range": para_range,
                "paragraphStyle": {"namedStyleType": style_name},
                "fields": "namedStyleType",
            }})
        if list_meta:
            preset = NUMBERED_PRESET if list_meta["ordered"] else BULLET_PRESET
            requests.append({"createParagraphBullets": {"range": para_range, "bulletPreset": preset}})
            if list_meta["level"] > 0:
                requests.append({"updateParagraphStyle": {
                    "range": para_range,
                    "paragraphStyle": {"indentStart": {"magnitude": INDENT_PER_LEVEL_PT * (list_meta["level"] + 1), "unit": "PT"},
                                       "indentFirstLine": {"magnitude": INDENT_PER_LEVEL_PT * list_meta["level"], "unit": "PT"}},
                    "fields": "indentStart,indentFirstLine",
                }})
        seg_pos = start
        for seg_text, kind, link_target in segments:
            seg_start, seg_end = seg_pos, seg_pos + len(seg_text)
            if kind == "bold":
                requests.append({"updateTextStyle": {
                    "range": {"startIndex": seg_start, "endIndex": seg_end},
                    "textStyle": {"bold": True}, "fields": "bold",
                }})
            elif kind == "italic":
                requests.append({"updateTextStyle": {
                    "range": {"startIndex": seg_start, "endIndex": seg_end},
                    "textStyle": {"italic": True}, "fields": "italic",
                }})
            elif kind == "code":
                requests.append({"updateTextStyle": {
                    "range": {"startIndex": seg_start, "endIndex": seg_end},
                    "textStyle": {"weightedFontFamily": {"fontFamily": "Courier New"}},
                    "fields": "weightedFontFamily",
                }})
            elif kind == "link":
                requests.append({"updateTextStyle": {
                    "range": {"startIndex": seg_start, "endIndex": seg_end},
                    "textStyle": {"link": {"url": link_target}}, "fields": "link",
                }})
            seg_pos = seg_end
        if italic_whole:
            requests.append({"updateTextStyle": {
                "range": {"startIndex": start, "endIndex": start + len(full_text) - 1},
                "textStyle": {"italic": True}, "fields": "italic",
            }})
        cursor = start + len(full_text)

    for block in blocks:
        if block["type"] == "heading":
            insert_paragraph(block["text"], HEADING_STYLE.get(block["level"], "HEADING_6"))
        elif block["type"] == "paragraph":
            insert_paragraph(block["text"])
        elif block["type"] == "list_item":
            insert_paragraph(block["text"], list_meta={"ordered": block["ordered"], "level": block["level"]})
        elif block["type"] == "image_placeholder":
            insert_paragraph(f"[Image: {block['caption']}]", italic_whole=True)
        elif block["type"] == "table":
            header = block["header"]
            for row in block["rows"]:
                label = row[0] if row else ""
                rest = " — ".join(c for c in row[1:] if c)
                insert_paragraph(f"{label} — {rest}")

    return requests


def main():
    if len(sys.argv) != 3:
        print("usage: md_to_gdoc.py <input.md> <title>", file=sys.stderr)
        sys.exit(1)
    md_path, title = sys.argv[1], sys.argv[2]
    md_text = open(md_path).read()
    blocks = parse_markdown(md_text)
    requests = build_requests(blocks)

    print(f"Creating doc '{title}' ({len(blocks)} blocks, {len(requests)} requests)...", file=sys.stderr)
    resp = dax_call("plugin::g_suite_plugin::docs_create", {"title": title})
    job_id = resp["result"]["data"]["job_id"]
    kind, payload = poll_job(job_id)
    if kind == "error":
        print(f"docs_create FAILED: {payload}", file=sys.stderr)
        sys.exit(1)
    doc_id = payload["id"]
    print(f"Created empty doc: https://docs.google.com/document/d/{doc_id}/edit", file=sys.stderr)

    CHUNK = 300
    for start in range(0, len(requests), CHUNK):
        chunk = requests[start:start + CHUNK]
        resp = dax_call("plugin::g_suite_plugin::docs_batch_update", {"id": doc_id, "requests": chunk})
        job_id = resp["result"]["data"]["job_id"]
        kind, payload = poll_job(job_id, timeout_s=90)
        if kind == "error":
            print(f"docs_batch_update chunk starting at {start} FAILED: {payload}", file=sys.stderr)
            sys.exit(1)
        print(f"  applied requests {start}..{start+len(chunk)}", file=sys.stderr)

    print(f"https://docs.google.com/document/d/{doc_id}/edit")


if __name__ == "__main__":
    main()
