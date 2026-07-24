"""
KCET (KEA) engineering cutoff-rank extraction pipeline.

Source PDFs are KEA's official round-wise allotment cutoff reports. Two
"seat type" report families were supplied per round/year:
  - kalyana_karnataka : 371(j) Hyderabad-Karnataka (now "Kalyana Karnataka")
    reservation quota. Category codes carry an 'H' suffix (1H, GMH, SCRH...).
  - rest_of_karnataka : general/state-wide quota. Category codes have no
    'H' suffix (1G, GM, SCR...).

Layout notes (verified by inspecting the PDFs directly, not assumed):
  - Every page draws an invisible 25-column grid (zero-linewidth rects):
    1 branch/course-name cell + 24 category-rank cells, laid out in the
    same left-to-right order as the category-code header row. Column
    pixel widths differ between the pre-2025 template and the 2025
    template, but the "25 cells per grid row, first cell is the name"
    structure is identical, and is derived from each page's own rects
    rather than hardcoded.
  - Cutoff values are sometimes rendered with NO gap between adjacent
    numbers (e.g. "155648191764" for two 6-digit ranks in adjacent
    columns), so naive whitespace tokenization silently corrupts data.
    The grid rects give the authoritative column boundary in that case,
    so every character is bucketed into a cell by its x-position rather
    than by splitting on whitespace.
  - Long branch/course names wrap onto extra lines that have no grid
    rects of their own (they overflow below the row's cell height). These
    are detected as "free text" (chars outside every grid row's vertical
    band) and appended to the most recent data row's name.
  - College headers ("1 E001 <name> (...)" pre-2025, or
    "College: (E001)<name>" in 2025) are also free text and mark the
    start of a new college.
"""

import csv
import os
import re
import sys
import bisect
import random
from collections import defaultdict

import pdfplumber

RAW_DIR = "data/raw"
OUT_DIR = "data/processed"
MASTER_CSV = os.path.join(OUT_DIR, "master_cutoffs.csv")
ERROR_LOG = os.path.join(OUT_DIR, "extraction_errors.log")

FIELDS = [
    "college_code", "college_name", "branch_name", "category",
    "round", "year", "cutoff_rank", "branch_code", "source_file",
]

FNAME_RE = re.compile(
    r'^(\d{4})_(first|second|third)_round_(kalyana_karnataka|rest_of_karnataka)\.pdf$',
    re.IGNORECASE,
)
ROUND_MAP = {"first": 1, "second": 2, "third": 3}

COLLEGE_HEADER_OLD_RE = re.compile(r'^\d+\s+(E\d+)\s+(.+)$')
COLLEGE_HEADER_NEW_RE = re.compile(r'^College:\s*\(([A-Z]\d+)\)\s*(.+)$')

# A category-header cell holds a short alphanumeric code like 1H, 2AKH,
# GMH, SCRH, 1G, 2AG, GM, SCR, STG ... never purely numeric, never "--".
CATEGORY_TOKEN_RE = re.compile(r'^[0-9A-Z]{1,5}$')
NUMERIC_RE = re.compile(r'^\d+(\.\d+)?$')
DASH_RE = re.compile(r'^-+$')

BRANCH_CODE_RE = re.compile(r'^([A-Z]{2})\s+(.+)$')

BOILERPLATE_PATTERNS = [
    re.compile(r'ENGINEERING\s+CUTOFF\s+RANK\s+OF\s+CET', re.IGNORECASE),
    re.compile(r'^\d{1,2}-[A-Z]{3}-\d{2}', re.IGNORECASE),          # 17-AUG-23
    re.compile(r'^\d{1,2}:\d{2}\s*(AM|PM)', re.IGNORECASE),
    re.compile(r'KARNATAKA EXAMINATIONS AUTHORITY', re.IGNORECASE),
    re.compile(r'Non-Interactive Admission System', re.IGNORECASE),
    re.compile(r'PROVISIONAL ALLOTMENT CUT-OFF RANKS', re.IGNORECASE),
    re.compile(r'^Seat Type:', re.IGNORECASE),
    re.compile(r'^Generated on:', re.IGNORECASE),
    re.compile(r'^Page \d+ of \d+$', re.IGNORECASE),
]


def parse_filename(fname):
    m = FNAME_RE.match(fname)
    if not m:
        return None
    year, roundword, region = m.groups()
    return {
        "year": int(year),
        "round": ROUND_MAP[roundword.lower()],
        "region": region.lower(),
    }


def is_boilerplate(text):
    return any(p.search(text) for p in BOILERPLATE_PATTERNS)


def chars_to_text(chars_list, line_join=" "):
    """Join a bag of chars into text, grouping by physical line (top) first
    so that two visually stacked lines captured inside the same grid cell
    (e.g. a wrapped branch name, or a fractional cutoff rank too wide for
    its column) don't get interleaved by a flat x0 sort. line_join is the
    separator placed between wrapped lines: a space for name/address text
    ("Artificial" + "Intelligence" -> "Artificial Intelligence"), but ""
    for numeric cells where a long value like "16234.9375" wraps onto a
    second line ("16234.93" / "75") and must NOT gain a space when rejoined."""
    if not chars_list:
        return ""
    ordered = sorted(chars_list, key=lambda c: (c["top"], c["x0"]))
    lines = []
    cur_top = None
    cur = []
    for c in ordered:
        if cur_top is None or abs(c["top"] - cur_top) > 2.0:
            if cur:
                lines.append(cur)
            cur = [c]
            cur_top = c["top"]
        else:
            cur.append(c)
    if cur:
        lines.append(cur)
    parts = []
    for line in lines:
        line_sorted = sorted(line, key=lambda c: c["x0"])
        buf = []
        prev_x1 = None
        for c in line_sorted:
            # Some layout gaps (e.g. between a college's list index and its
            # code, "1" then "E001") have no literal space character, only a
            # positional gap - insert one so words don't fuse together.
            if prev_x1 is not None and c["x0"] - prev_x1 > 1.5:
                buf.append(" ")
            buf.append(c["text"])
            prev_x1 = c["x1"]
        parts.append("".join(buf))
    return " ".join(line_join.join(parts).split())


def cluster_row_bands(rects):
    """Group grid rects into rows keyed by (top, bottom), each row holding
    its sorted, deduped list of (x0, x1) cell boundaries."""
    rows = defaultdict(set)
    for r in rects:
        key = (round(r["top"], 1), round(r["bottom"], 1))
        rows[key].add((round(r["x0"], 2), round(r["x1"], 2)))
    bands = []
    for (top, bottom), cellset in rows.items():
        cells = sorted(cellset, key=lambda c: c[0])
        if len(cells) >= 20:  # a real grid row (name cell + ~24 rank cells)
            bands.append({"top": top, "bottom": bottom, "cells": cells})
    bands.sort(key=lambda b: b["top"])
    return bands


def extract_page_rows(page, page_no, source_file, errors):
    """Return a chronological list of events for a page:
    {'top':..., 'kind':'grid_row', 'cells_text':[...]} or
    {'top':..., 'kind':'free_text', 'text':...}
    """
    chars = sorted(page.chars, key=lambda c: c["top"])
    rects = page.rects
    bands = cluster_row_bands(rects)

    band_tops = [b["top"] for b in bands]
    band_bottoms = [b["bottom"] for b in bands]

    def band_index_for_char(c):
        # binary search: find a band whose [top-0.6, bottom+0.6] contains c.top
        i = bisect.bisect_right(band_tops, c["top"] + 0.6) - 1
        if i < 0:
            return None
        if band_tops[i] - 0.6 <= c["top"] <= band_bottoms[i] + 0.6:
            return i
        return None

    chars_by_band = defaultdict(list)
    free_chars = []
    for c in chars:
        if c["text"] == "":
            continue
        bi = band_index_for_char(c)
        if bi is None:
            free_chars.append(c)
        else:
            chars_by_band[bi].append(c)

    events = []

    for bi, band in enumerate(bands):
        cell_chars = chars_by_band.get(bi, [])
        all_cells = band["cells"]
        if len(all_cells) not in (24, 25):
            errors.append(
                f"{source_file} p{page_no}: grid row at top={band['top']} has "
                f"{len(all_cells)} rank-column rects (expected 24 or 25) - "
                f"row skipped entirely"
            )
            continue
        # The invisible name-cell rect is not always drawn - only rely on the
        # rightmost 24 cells (the category-rank columns) as authoritative;
        # everything to the left of them is the branch/course-name region,
        # captured from raw chars regardless of whether it has its own rect.
        value_cells = all_cells[-24:]
        name_region_end_x = value_cells[0][0]

        name_chars = [c for c in cell_chars if c["x0"] < name_region_end_x - 0.6]
        name_text = chars_to_text(name_chars)

        cells_text = [name_text]
        for (x0, x1) in value_cells:
            xmid_lo, xmid_hi = x0 - 0.6, x1 + 0.6
            sel = [c for c in cell_chars if xmid_lo <= (c["x0"] + c["x1"]) / 2 < xmid_hi]
            cells_text.append(chars_to_text(sel, line_join=""))

        events.append({"top": band["top"], "kind": "grid_row", "cells_text": cells_text})

    # reconstruct free-text lines by clustering leftover chars by 'top'
    free_chars.sort(key=lambda c: (c["top"], c["x0"]))
    line_groups = []
    cur_top = None
    cur = []
    for c in free_chars:
        if cur_top is None or abs(c["top"] - cur_top) > 2.0:
            if cur:
                line_groups.append(cur)
            cur = [c]
            cur_top = c["top"]
        else:
            cur.append(c)
    if cur:
        line_groups.append(cur)

    for grp in line_groups:
        text = chars_to_text(grp)
        if text:
            events.append({"top": min(c["top"] for c in grp), "kind": "free_text", "text": text})

    events.sort(key=lambda e: e["top"])
    return events


def process_file(path, meta, writer, errors, sample_rows):
    fname = os.path.basename(path)
    row_count = 0
    with pdfplumber.open(path) as pdf:
        current_college_code = None
        current_college_name = None
        current_category_order = None  # list of 24 codes, left-to-right
        pending_row = None  # dict for the data row currently accepting name continuation
        college_header_open = False  # True between a college-header line and the next grid row

        def flush_pending():
            nonlocal row_count
            if pending_row is None:
                return
            branch_name = " ".join(pending_row["name_parts"]).strip()
            branch_name = " ".join(branch_name.split())
            if current_college_code is None or current_category_order is None:
                errors.append(
                    f"{fname}: data row for branch '{branch_name}' skipped - "
                    f"missing college ({current_college_code}) or category header "
                    f"({current_category_order is not None})"
                )
                return
            if len(pending_row["values"]) != len(current_category_order):
                errors.append(
                    f"{fname}: branch '{branch_name}' has {len(pending_row['values'])} "
                    f"values but {len(current_category_order)} categories are active - "
                    f"row skipped"
                )
                return
            for cat, val in zip(current_category_order, pending_row["values"]):
                if val == "" or DASH_RE.match(val):
                    continue
                if not NUMERIC_RE.match(val):
                    errors.append(
                        f"{fname}: college={current_college_code} branch='{branch_name}' "
                        f"category={cat} unparseable value='{val}' - cell skipped"
                    )
                    continue
                cutoff = float(val) if "." in val else int(val)
                writer.writerow({
                    "college_code": current_college_code,
                    "college_name": current_college_name,
                    "branch_name": branch_name,
                    "category": cat,
                    "round": meta["round"],
                    "year": meta["year"],
                    "cutoff_rank": cutoff,
                    "branch_code": pending_row["code"] or "",
                    "source_file": fname,
                })
                row_count += 1
                if random.random() < 0.002 or len(sample_rows[fname]) < 5:
                    if len(sample_rows[fname]) < 200:
                        sample_rows[fname].append({
                            "college_code": current_college_code,
                            "college_name": current_college_name,
                            "branch_name": branch_name,
                            "category": cat,
                            "round": meta["round"],
                            "year": meta["year"],
                            "cutoff_rank": cutoff,
                            "branch_code": pending_row["code"] or "",
                        })

        for page_no, page in enumerate(pdf.pages, start=1):
            events = extract_page_rows(page, page_no, fname, errors)
            for ev in events:
                if ev["kind"] == "grid_row":
                    cells = ev["cells_text"]
                    name_cell = cells[0] if cells else ""
                    rank_cells = cells[1:] if len(cells) > 1 else []

                    non_empty = [t for t in rank_cells if t]
                    is_header = (
                        len(non_empty) >= max(1, int(0.7 * len(rank_cells)))
                        and all(
                            CATEGORY_TOKEN_RE.match(t) and not NUMERIC_RE.match(t) and not DASH_RE.match(t)
                            for t in non_empty
                        )
                    )
                    if is_header:
                        flush_pending()
                        pending_row = None
                        college_header_open = False
                        current_category_order = rank_cells
                        continue

                    # data row
                    flush_pending()
                    college_header_open = False
                    m = BRANCH_CODE_RE.match(name_cell)
                    if m:
                        code, name_first = m.group(1), m.group(2)
                    else:
                        code, name_first = None, name_cell
                    pending_row = {
                        "code": code,
                        "name_parts": [name_first] if name_first else [],
                        "values": rank_cells,
                    }
                else:
                    text = ev["text"]
                    if is_boilerplate(text):
                        continue
                    m_old = COLLEGE_HEADER_OLD_RE.match(text)
                    m_new = COLLEGE_HEADER_NEW_RE.match(text)
                    if m_old or m_new:
                        flush_pending()
                        pending_row = None
                        code, name = (m_old or m_new).groups()
                        current_college_code = code.strip()
                        current_college_name = name.strip()
                        college_header_open = True
                        continue
                    # otherwise: continuation of either the college address
                    # (right after a college header) or the current branch name
                    if college_header_open:
                        current_college_name = (current_college_name + " " + text).strip()
                    elif pending_row is not None:
                        pending_row["name_parts"].append(text)
                    else:
                        errors.append(
                            f"{fname} p{page_no}: unclassified free text ignored: '{text}'"
                        )
        flush_pending()
    return row_count


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    raw_files = sorted(f for f in os.listdir(RAW_DIR) if f.lower().endswith(".pdf"))

    print("=" * 70)
    print("STEP 1 - INVENTORY")
    print("=" * 70)
    inventory = []
    for fname in raw_files:
        path = os.path.join(RAW_DIR, fname)
        meta = parse_filename(fname)
        with pdfplumber.open(path) as pdf:
            n_pages = len(pdf.pages)
            p1_text = pdf.pages[0].extract_text() or ""
            first_lines = [l for l in p1_text.split("\n") if l.strip()][:3]
        inventory.append((fname, meta, n_pages, first_lines))
        print(f"\nFile: {fname}")
        if meta:
            print(f"  Parsed from filename -> year={meta['year']} round={meta['round']} region={meta['region']}")
        else:
            print("  WARNING: filename did not match expected pattern, skipping this file")
        print(f"  Format: text-based PDF (pdfplumber), {n_pages} pages")
        print(f"  First lines on page 1:")
        for l in first_lines:
            print(f"    {l}")
        print(f"  Table structure: 25-column invisible grid per row "
              f"(1 branch/course-name cell + 24 category-rank cells), "
              f"category codes read from each block's own header row.")

    errors = []
    sample_rows = defaultdict(list)
    total_by_file = {}

    print("\n" + "=" * 70)
    print("STEP 3 - RUNNING EXTRACTION PIPELINE")
    print("=" * 70)
    with open(MASTER_CSV, "w", newline="", encoding="utf-8") as fcsv:
        writer = csv.DictWriter(fcsv, fieldnames=FIELDS)
        writer.writeheader()
        for fname, meta, n_pages, _ in inventory:
            if not meta:
                continue
            path = os.path.join(RAW_DIR, fname)
            print(f"Processing {fname} ...")
            n = process_file(path, meta, writer, errors, sample_rows)
            total_by_file[fname] = n
            print(f"  -> {n} rows extracted")

    with open(ERROR_LOG, "w", encoding="utf-8") as flog:
        for e in errors:
            flog.write(e + "\n")

    print("\n" + "=" * 70)
    print("STEP 4 - VERIFICATION")
    print("=" * 70)
    print(f"\nTotal rows per file:")
    grand_total = 0
    for fname, _, _, _ in inventory:
        n = total_by_file.get(fname, 0)
        grand_total += n
        print(f"  {fname}: {n} rows")
    print(f"\nGRAND TOTAL: {grand_total} rows written to {MASTER_CSV}")

    print(f"\nRandom sample of up to 5 rows per file:")
    for fname, _, _, _ in inventory:
        rows = sample_rows.get(fname, [])
        if not rows:
            print(f"\n  {fname}: (no rows)")
            continue
        sample = random.sample(rows, min(5, len(rows)))
        print(f"\n  {fname}:")
        for r in sample:
            print(f"    {r}")

    print(f"\nErrors logged: {len(errors)} (see {ERROR_LOG})")
    if errors:
        print("  First 15 errors:")
        for e in errors[:15]:
            print(f"    {e}")


if __name__ == "__main__":
    sys.exit(main())
