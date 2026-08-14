#!/usr/bin/env python3
"""Convert a markdown file to a Google Doc via dax's g_suite_plugin.

Scope/limitations (read before extending):
- Handles: H1/H2/H3 headings, paragraphs, **bold** and `code` inline spans,
  and GFM pipe tables.
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

BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
CODE_RE = re.compile(r"`([^`]+)`")


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
    """Split text on **bold** and `code` markers into (text, kind) segments.
    kind is one of 'plain', 'bold', 'code'. Markers are stripped from output.
    """
    segments = []
    pos = 0
    # Interleave bold/code matches in order of appearance.
    matches = sorted(
        list(BOLD_RE.finditer(text)) + list(CODE_RE.finditer(text)),
        key=lambda m: m.start(),
    )
    for m in matches:
        if m.start() < pos:
            continue  # overlapping match (nested), skip -- rare in this doc
        if m.start() > pos:
            segments.append((text[pos:m.start()], "plain"))
        kind = "bold" if m.group(0).startswith("**") else "code"
        segments.append((m.group(1), kind))
        pos = m.end()
    if pos < len(text):
        segments.append((text[pos:], "plain"))
    return segments or [("", "plain")]


def parse_markdown(md_text):
    """Very small parser scoped to this doc's shape: H1-3, paragraphs, GFM tables."""
    blocks = []
    lines = md_text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        heading_match = re.match(r"^(#{1,3})\s+(.*)$", line)
        if heading_match:
            blocks.append({"type": "heading", "level": len(heading_match.group(1)), "text": heading_match.group(2)})
            i += 1
            continue
        if line.strip().startswith("|"):
            # table: header row, separator row, data rows
            header = [c.strip() for c in line.strip().strip("|").split("|")]
            i += 1
            if i < len(lines) and re.match(r"^\s*\|?[\s:|-]+\|?\s*$", lines[i]):
                i += 1  # skip separator row
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            blocks.append({"type": "table", "header": header, "rows": rows})
            continue
        # paragraph: accumulate until blank line or next structural line
        para_lines = [line]
        i += 1
        while i < len(lines) and lines[i].strip() and not lines[i].strip().startswith("|") and not re.match(r"^#{1,3}\s", lines[i]):
            para_lines.append(lines[i])
            i += 1
        blocks.append({"type": "paragraph", "text": " ".join(para_lines)})
    return blocks


HEADING_STYLE = {1: "HEADING_1", 2: "HEADING_2", 3: "HEADING_3"}


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

    def insert_paragraph(text, style_name=None, code_only=False):
        nonlocal cursor
        segments = parse_inline_spans(text) if not code_only else [(text, "plain")]
        full_text = "".join(seg for seg, _ in segments) + "\n"
        start = cursor
        requests.append({"insertText": {"location": {"index": start}, "text": full_text}})
        if style_name:
            requests.append({"updateParagraphStyle": {
                "range": {"startIndex": start, "endIndex": start + len(full_text)},
                "paragraphStyle": {"namedStyleType": style_name},
                "fields": "namedStyleType",
            }})
        seg_pos = start
        for seg_text, kind in segments:
            seg_start, seg_end = seg_pos, seg_pos + len(seg_text)
            if kind == "bold":
                requests.append({"updateTextStyle": {
                    "range": {"startIndex": seg_start, "endIndex": seg_end},
                    "textStyle": {"bold": True}, "fields": "bold",
                }})
            elif kind == "code":
                requests.append({"updateTextStyle": {
                    "range": {"startIndex": seg_start, "endIndex": seg_end},
                    "textStyle": {"weightedFontFamily": {"fontFamily": "Courier New"}},
                    "fields": "weightedFontFamily",
                }})
            seg_pos = seg_end
        cursor = start + len(full_text)

    for block in blocks:
        if block["type"] == "heading":
            insert_paragraph(block["text"], HEADING_STYLE.get(block["level"], "HEADING_3"))
        elif block["type"] == "paragraph":
            insert_paragraph(block["text"])
        elif block["type"] == "table":
            header = block["header"]
            for row in block["rows"]:
                cells = dict(zip(header, row))
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

    # batchUpdate has a request-count/size ceiling in practice; chunk defensively.
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
