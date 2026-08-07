# -*- coding: utf-8 -*-
"""
Extracts primary-election candidate counts from the City of San Diego's
council election-history PDFs (city-of-san-diego-election-history-district-
1..9.pdf) and writes primary_candidate_counts.csv.

This parses the PDFs' text layer directly (via pdfplumber) rather than
transcribing by hand, so it can be re-run whenever the city republishes these
documents with newer elections. To replicate: drop the updated PDFs in this
folder (same filenames) and run `python3 extract_primary_candidate_counts.py`.

Why text parsing instead of table extraction: pdfplumber's table-grid
detection silently drops some rows that fall near a page break or have an
unusual cell height (confirmed by cross-checking against a hand-verified
prior version of this dataset -- e.g. a candidate row was dropped entirely
from the table view in two different documents, while the same row survives
intact in the plain text layer). Text extraction preserves every row; the
tradeoff is that column alignment must be reconstructed with regexes instead
of read off a grid, which is what most of this file does.

Known source-PDF defects, confirmed by cross-referencing the pre-2026
revision of these documents (accurate transcription of which is not
recoverable from the current PDFs alone):
  - A run of older elections in the District 1 PDF lost their "DATE OF
    PRIMARY" cell entirely (present in the vote-total/runoff columns, but the
    date itself is blank in the source, not just hard to parse) -- see
    BLANK_DATE_FIXES.
  - District 5's 3/3/2008 race is dated inconsistently with its sibling odd
    districts (1, 3, 7), which all list their same-cycle primary as
    6/3/2008; corrected below.
  - District 2's 3/28/1939 race has two candidates' names interleaved
    character-by-character in the PDF's text layer itself (a genuine
    encoding defect, not an extraction artifact) -- see
    KNOWN_TEXT_CORRUPTIONS.
"""
import csv
import glob
import os
import re

import pdfplumber

DISTRICT_FIX_RE = re.compile(r"D\s*i\s*s\s*t\s*r\s*i\s*c\s*t")
DISTRICT_ALONE_RE = re.compile(r"^District\s*(\d+)\s*$")
DISTRICT_HEADER_RE = re.compile(r"^District\s*(\d+)\s+(.*)$")
NOISE_PATTERNS = [
    re.compile(r"^ELECTION HISTORY", re.I),
    re.compile(r"^OFFICE\b.*CANDIDATE", re.I),
    re.compile(r"^Last[e]?\s*[Uu]pdated", re.I),
    re.compile(r"^Page\s+\d+\s+of\s+\d+$", re.I),
    re.compile(r"DATE OF.*PRIMARY", re.I),
    re.compile(r"^\s*$"),
]
RECALL_KEYWORDS = ["recall", "office?"]
ALL_NUMERIC_RE = re.compile(r"^[\d,.%\s]+$")
HAS_DIGIT_RE = re.compile(r"\d")
HAS_LETTER_RE = re.compile(r"[A-Za-z]")
DATE_LOOSE_RE = re.compile(r"\d\s*\d?\s*/\s*\d\s*\d?\s*/\s*\d\s*\d\s*\d\s*\d")

KNOWN_TEXT_CORRUPTIONS = {
    (2, "3/28/1939"): ["Wilbur A. Thomas", "Louis F. Weggenman", "Byron Gilchrist"],
}
DATE_CORRECTIONS = {
    (5, "3/3/2008"): "6/3/2008",
}
# (district, primary_total) -> date, for the District 1 rows whose "DATE OF
# PRIMARY" cell is blank in the source PDF.
BLANK_DATE_FIXES = {
    (1, "42911"): "3/3/2020",
    (1, "38303"): "6/7/2016",
    (1, "30987"): "6/5/2012",
    (1, "34763"): "6/3/2008",
    (1, "41260"): "3/2/2004",
    (1, "35064"): "3/7/2000",
    (1, "7083"): "3/8/1949",
}


def get_lines(pdf_path):
    """Returns (line, was_kerned) pairs. 'was_kerned' flags lines where the word
    'District' itself was visibly split by a font-kerning bug found on page 2 of
    the district-7 PDF (e.g. 'Distr ict 7 9/19/ 1989 13, 913') -- on those lines
    (and only those), numbers/dates get similar stray internal spaces and need
    extra cleanup before tokenizing."""
    with pdfplumber.open(pdf_path) as pdf:
        text = "\n".join(page.extract_text() for page in pdf.pages)
    lines = []
    for raw in text.split("\n"):
        fixed = DISTRICT_FIX_RE.sub("District", raw).strip()
        if not fixed:
            continue
        if any(p.search(fixed) for p in NOISE_PATTERNS):
            continue
        lines.append((fixed, fixed != raw.strip()))
    return lines


def _tokenize_kerned(remainder):
    """The kerning bug inserts stray spaces *inside* tokens but leaves the
    separator space *between* tokens intact -- so a blind whitespace strip
    merges adjacent tokens (e.g. primary_total + the next date's day digits).
    Instead: pull out date-shaped spans first (their internal '/' anchors make
    them unambiguous even with ragged spacing), clean each one, and only then
    strip whitespace from what's left *between* dates -- each such gap holds
    at most one total/none token, so stripping it fully is safe."""

    def gap_tokens(gap):
        gap_clean = re.sub(r"\s+", "", gap)
        out = []
        for gm in re.finditer(r"none|[\d,]+", gap_clean, re.I):
            out.append(("none", "none") if gm.group().lower() == "none" else ("num", gm.group()))
        return out

    tokens = []
    pos = 0
    for m in DATE_LOOSE_RE.finditer(remainder):
        tokens.extend(gap_tokens(remainder[pos : m.start()]))
        tokens.append(("date", re.sub(r"\s+", "", m.group())))
        pos = m.end()
    tokens.extend(gap_tokens(remainder[pos:]))
    return tokens


def align_header_tokens(remainder, was_kerned):
    """Aligns whatever date/number/'none' tokens survived in a header line against
    the canonical 4-slot template [primary_date, primary_total, runoff_date, runoff_total].
    Columns the source PDF left blank simply don't emit a token, so slots those
    tokens don't match are left blank rather than mis-assigned."""
    if was_kerned:
        tokens = _tokenize_kerned(remainder)
    else:
        tokens = []
        for m in re.finditer(r"\d{1,2}/\d{1,2}/\d{4}|none|[\d,]+", remainder, re.I):
            tok = m.group()
            if tok.lower() == "none":
                tokens.append(("none", tok))
            elif "/" in tok:
                tokens.append(("date", tok))
            else:
                tokens.append(("num", tok))

    slots = ["primary_date", "primary_total", "runoff_date", "runoff_total"]
    accepts = {
        "primary_date": {"date"},
        "primary_total": {"num"},
        "runoff_date": {"date", "none"},
        "runoff_total": {"num"},
    }
    result = {s: "" for s in slots}
    ti = 0
    for slot in slots:
        if ti < len(tokens) and tokens[ti][0] in accepts[slot]:
            result[slot] = tokens[ti][1]
            ti += 1
    return result["primary_date"], result["primary_total"], result["runoff_date"], result["runoff_total"]


def split_candidate_name(line):
    """Splits a candidate row into its name portion, cutting at the first
    vote-like token ('No results' or a number). Returns None if the line has
    no name portion (pure-numeric noise, e.g. a stray runoff total on its
    own line)."""
    m = re.search(r"(No results\b|\d[\d,]*\b)", line)
    if not m:
        return None
    name = line[: m.start()].strip()
    if not name or not HAS_LETTER_RE.search(name):
        return None
    return name


def clean_name(name):
    name = name.replace("(elected)", "").strip()
    name = name.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    name = re.sub(r"'\s+", "'", name)  # kerning artifact: "O’ Rourke" -> "O'Rourke"
    name = re.sub(r"-\s+", "-", name)  # kerning artifact: "Arends- Biddlecome" -> "Arends-Biddlecome"
    name = re.sub(r"\s+", " ", name).strip()
    return name


def parse_pdf(path):
    lines = get_lines(path)
    records = []
    cur = None
    pending_name = None
    i = 0
    n = len(lines)
    while i < n:
        line, was_kerned = lines[i]

        if DISTRICT_ALONE_RE.match(line):
            # Two-line appointment/note marker: this line + the next carry no
            # candidates (someone appointed to fill a vacancy, or a
            # redistricting reassignment note), so they add nothing to the
            # current still-open block.
            i += 2
            pending_name = None
            continue

        m = DISTRICT_HEADER_RE.match(line)
        if m:
            remainder = m.group(2)
            if "Appointed" in remainder:
                # Single-line appointment variant: "District N Name date Appointed".
                i += 1
                pending_name = None
                continue
            if cur:
                records.append(cur)
            district = int(m.group(1))
            pdate, ptotal, rdate, rtotal = align_header_tokens(remainder, was_kerned)
            cur = {
                "district": district,
                "primary_date": pdate,
                "primary_total": ptotal,
                "runoff_date": rdate,
                "runoff_total": rtotal,
                "candidates": [],
                "flags": [],
            }
            pending_name = None
            i += 1
            continue

        # Body line (candidate row, recall fragment, or wrapped-name fragment).
        if cur is None:
            i += 1
            continue

        if pending_name is not None:
            if ALL_NUMERIC_RE.match(line) or line.startswith("(elected)"):
                # name/votes split across lines, sometimes with "(elected)" glued
                # to the votes line instead of getting its own
                cur["candidates"].append(clean_name(pending_name))
                pending_name = None
                i += 1
                continue
            else:
                pending_name = None  # orphaned fragment (recall ballot text etc.); discard and reprocess this line fresh

        if line == "(elected)":
            i += 1  # already counted when its name line was appended
            continue

        if not HAS_LETTER_RE.search(line):
            i += 1  # pure-numeric noise (e.g. a stray recall vote total on its own line)
            continue

        low = line.lower()
        if any(k in low for k in RECALL_KEYWORDS):
            cur["flags"].append("recall")
            i += 1
            continue

        if not HAS_DIGIT_RE.search(line):
            pending_name = line  # name wrapped onto its own line; votes expected next
            i += 1
            continue

        name = split_candidate_name(line)
        if name:
            cur["candidates"].append(clean_name(name))
        i += 1

    if cur:
        records.append(cur)

    # Safety net: a handful of appointment/vacancy notices ("District N <date>"
    # with no other header content, followed by "Name Appointed" with no
    # trailing digits) don't match any of the other known appointment-line
    # shapes and get misread as a zero-candidate election. A real election
    # always has at least one candidate, so drop any record that has none.
    records = [r for r in records if r["candidates"]]

    for r in records:
        key = (r["district"], r["primary_date"])
        if key in KNOWN_TEXT_CORRUPTIONS:
            r["candidates"] = list(KNOWN_TEXT_CORRUPTIONS[key])
            r["flags"].append("corrected:text_corruption")
        if key in DATE_CORRECTIONS:
            r["primary_date"] = DATE_CORRECTIONS[key]
            r["flags"].append("corrected:date")
        if not r["primary_date"]:
            fix = BLANK_DATE_FIXES.get((r["district"], r["primary_total"]))
            if fix:
                r["primary_date"] = fix
                r["flags"].append("corrected:blank_date")

    return records


def get_last_updated(path):
    with pdfplumber.open(path) as pdf:
        text = pdf.pages[0].extract_text()
    m = re.search(r"[Uu]pdated:?\s*(\d{1,2}/\d{1,2}/\d{2,4})", text)
    return m.group(1) if m else ""


def build_rows():
    rows = []
    here = os.path.dirname(os.path.abspath(__file__))
    for path in sorted(glob.glob(os.path.join(here, "city-of-san-diego-election-history-district-*.pdf"))):
        source_pdf = os.path.basename(path)
        last_updated = get_last_updated(path)
        for r in parse_pdf(path):
            n = len(r["candidates"])
            election_type = "recall" if "recall" in r["flags"] else "primary"
            notes = "uncontested, elected 100%" if n == 1 and election_type == "primary" else ""
            rows.append(
                (
                    r["district"],
                    r["primary_date"],
                    n,
                    "; ".join(r["candidates"]),
                    election_type,
                    notes,
                    source_pdf,
                    last_updated,
                )
            )
    rows.sort(key=lambda r: (r[0], -_year(r[1])))
    return rows


def _year(date_str):
    try:
        return int(date_str.split("/")[-1])
    except (ValueError, IndexError):
        return 0


if __name__ == "__main__":
    rows = build_rows()
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "primary_candidate_counts.csv")
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["district", "primary_date", "num_candidates", "candidates", "election_type", "notes", "source_pdf", "source_last_updated"])
        writer.writerows(rows)

    print(f"Wrote {len(rows)} rows to {out_path}")
    from collections import Counter

    c = Counter(r[0] for r in rows)
    for d in sorted(c):
        print(f"District {d}: {c[d]} primary elections on record")
