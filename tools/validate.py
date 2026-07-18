"""Conformance validator for drawing-office LLD.md files.

Enforces the machine-checked rules in STANDARD.md. Stdlib only.

Usage: python tools/validate.py <path> [<path>...]
Each path is a directory (recursively globbed for LLD.md) or a file.
Errors go to stdout as `path:line: E### message`, sorted by (path, line).
Exit 1 if any errors, 0 if clean, 2 on usage problems.
"""

import os
import re
import sys

USAGE = 'usage: python tools/validate.py <path> [<path>...]'

CANONICAL_KEYS = [
    'id', 'title', 'revision', 'status', 'author',
    'reviewed_by', 'date', 'part_count', 'supersedes',
]

# Derived-drawing keys (Standard rev C): all-or-none, appended after
# supersedes. Presence of any marks the drawing as derived.
DERIVED_KEYS = ['source', 'source_rev', 'subject', 'derived_by']

REQUIRED_SECTIONS = [
    '## ASSEMBLY DRAWING',
    '## BILL OF MATERIALS',
    '## DETAIL DRAWINGS',
    '## CONTRACTS & TOLERANCES',
    '## PROCESS PLAN',
    '## REVISION HISTORY',
]

FENCE_RE = re.compile(r'^\s{0,3}`{3,}')
MERMAID_FENCE_RE = re.compile(r'^\s{0,3}`{3,}[ \t]?mermaid\b')
H2_RE = re.compile(r'^##\s+')
H3_RE = re.compile(r'^###\s+')
H3_DETAIL_RE = re.compile(r'^###\s+(P[0-9]+)\b')
ID_RE = re.compile(r'DO-[0-9]{3}')
REVISION_RE = re.compile(r'[A-Z]+')
DATE_RE = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}')
INT_RE = re.compile(r'[0-9]+')
PART_RE = re.compile(r'P[0-9]+')
SHA_RE = re.compile(r'[0-9a-f]{40}|[0-9a-f]{64}')

# Evidence citation grammar (rev C): entries split on ';', each entry is a
# kind word followed by whitespace and a non-empty payload.
EVIDENCE_ENTRY_RE = re.compile(r'(code|commit|doc|issue|searched)\s+\S.*')

BASIS_VALUES = {'observed', 'documented', 'inferred', 'unknown'}

# The complete rejection set for tolerance cells: empty, hyphen, em dash, en dash.
DASH_CELLS = {'', '-', '—', '–'}

STATUSES = {'draft', 'released', 'superseded'}


def parse_cells(line):
    """Strip outer pipes, split on |, strip whitespace from each cell."""
    s = line.strip()
    if s.startswith('|'):
        s = s[1:]
    if s.endswith('|'):
        s = s[:-1]
    return [c.strip() for c in s.split('|')]


def is_separator_row(line):
    """A separator row's cells contain only dashes, colons, and spaces."""
    return all(set(cell) <= set('-: ') for cell in parse_cells(line))


def scan_fences(lines):
    """Return (fenced, mermaid_lines).

    fenced[i] is True when 0-based line i is a fence delimiter or inside a
    fence. mermaid_lines is a list of 1-based line numbers of fence lines
    that open a mermaid block.
    """
    fenced = [False] * len(lines)
    mermaid_lines = []
    in_fence = False
    for i, line in enumerate(lines):
        if FENCE_RE.match(line):
            if not in_fence and MERMAID_FENCE_RE.match(line):
                mermaid_lines.append(i + 1)
            in_fence = not in_fence
            fenced[i] = True
        else:
            fenced[i] = in_fence
    return fenced, mermaid_lines


def find_tables(lines, fenced, start, end):
    """Find pipe tables between 1-based lines start..end inclusive.

    A table is 2+ consecutive non-fenced lines starting with optional
    whitespace then |, where the second line is a separator row. Returns
    dicts with 1-based 'header' line and 'data' line list.
    """
    tables = []
    i = start
    while i <= end:
        if not fenced[i - 1] and lines[i - 1].lstrip().startswith('|'):
            j = i
            while (j <= end and not fenced[j - 1]
                   and lines[j - 1].lstrip().startswith('|')):
                j += 1
            # Run covers 1-based lines i..j-1; second line is line i+1.
            if j - i >= 2 and is_separator_row(lines[i]):
                tables.append({'header': i, 'data': list(range(i + 2, j))})
            i = j
        else:
            i += 1
    return tables


def parse_front_matter(lines):
    """Parse the front matter block.

    Returns (entries, close_line, errors). entries maps key -> (value, line)
    for first occurrences of canonical keys. close_line is the 1-based line
    of the closing --- or None. errors is a list of (line, code, message).
    """
    errors = []
    entries = {}
    if not lines or lines[0].strip() != '---':
        errors.append((1, 'E101', 'front matter block missing at line 1'))
        return entries, None, errors
    close_line = None
    for i in range(1, len(lines)):
        if lines[i].strip() == '---':
            close_line = i + 1
            break
    if close_line is None:
        errors.append((1, 'E102', 'front matter never closed'))
        return entries, None, errors
    for i in range(1, close_line - 1):
        ln = i + 1
        raw = lines[i]
        key, sep, value = raw.partition(':')
        key = key.strip()
        value = value.strip()
        if not sep or not key:
            errors.append((ln, 'E103',
                           'malformed front matter line (not key: value)'))
            continue
        if key not in CANONICAL_KEYS and key not in DERIVED_KEYS:
            errors.append((ln, 'E104', 'unknown key ' + key))
            continue
        if key in entries:
            errors.append((ln, 'E105', 'duplicate key ' + key))
            continue
        entries[key] = (value, ln)
        if value == '':
            errors.append((ln, 'E107', 'empty value for key ' + key))
    for key in CANONICAL_KEYS:
        if key not in entries:
            errors.append((close_line, 'E106', 'missing key ' + key))
    return entries, close_line, errors


def check_front_matter_values(entries):
    """Format checks E108, E110-E115 on present, non-empty values."""
    errors = []

    def get(key):
        item = entries.get(key)
        if item is not None and item[0] != '':
            return item
        return None

    v = get('id')
    if v and not ID_RE.fullmatch(v[0]):
        errors.append((v[1], 'E108', 'id does not match DO-NNN (three digits)'))
    v = get('status')
    if v and v[0] not in STATUSES:
        errors.append((v[1], 'E110',
                       'status not one of draft|released|superseded'))
    v = get('revision')
    if v and not REVISION_RE.fullmatch(v[0]):
        errors.append((v[1], 'E111', 'revision is not capital letters'))
    v = get('date')
    if v and not DATE_RE.fullmatch(v[0]):
        errors.append((v[1], 'E112', 'date does not match YYYY-MM-DD'))
    v = get('part_count')
    if v and not (INT_RE.fullmatch(v[0]) and int(v[0]) > 0):
        errors.append((v[1], 'E113', 'part_count not a positive integer'))
    v = get('supersedes')
    if v and v[0] != 'none' and not ID_RE.fullmatch(v[0]):
        errors.append((v[1], 'E114',
                       'supersedes neither none nor DO-NNN'))
    status = get('status')
    reviewed = get('reviewed_by')
    if status and reviewed and status[0] == 'released' and reviewed[0] == 'none':
        errors.append((reviewed[1], 'E115',
                       'status released but reviewed_by is none'))

    # Derived keys (rev C): all-or-none, and source_rev is a pinned hash.
    present = [k for k in DERIVED_KEYS if k in entries]
    if present and len(present) < len(DERIVED_KEYS):
        missing = [k for k in DERIVED_KEYS if k not in entries]
        errors.append((entries[present[0]][1], 'E116',
                       'derived keys incomplete: missing '
                       + ', '.join(missing)))
    v = get('source_rev')
    if v and not SHA_RE.fullmatch(v[0]):
        errors.append((v[1], 'E117',
                       'source_rev is not a full 40- or 64-char lowercase '
                       'commit hash (floating refs are not pinnable)'))
    return errors


def validate_file(path, text):
    """Validate one LLD file. Returns a list of (path, line, code, message)."""
    lines = text.splitlines()
    raw = []

    entries, close_line, fm_errors = parse_front_matter(lines)
    raw.extend(fm_errors)

    part_count = None
    part_count_line = None
    if close_line is not None:
        raw.extend(check_front_matter_values(entries))
        item = entries.get('part_count')
        if item and INT_RE.fullmatch(item[0]) and int(item[0]) > 0:
            part_count = int(item[0])
            part_count_line = item[1]

    # A drawing is derived iff its title block carries any derived key
    # (rev C). E116 separately enforces that all four are present.
    derived = any(k in entries for k in DERIVED_KEYS)

    content_start = close_line + 1 if close_line is not None else 1
    fenced, mermaid_lines = scan_fences(lines)

    # --- Sections (E201-E203) ---
    h2s = []
    for i in range(content_start - 1, len(lines)):
        if not fenced[i] and H2_RE.match(lines[i]):
            h2s.append((i + 1, lines[i].strip()))
    h2_lines = [ln for ln, _ in h2s]

    expected_seen = set()
    ordered_found = []
    for ln, title in h2s:
        if title in REQUIRED_SECTIONS and title not in expected_seen:
            expected_seen.add(title)
            ordered_found.append((ln, title))
        else:
            raw.append((ln, 'E203', 'unexpected extra section: ' + title))
    eof_line = len(lines) if lines else 1
    for title in REQUIRED_SECTIONS:
        if title not in expected_seen:
            raw.append((eof_line, 'E201',
                        'missing required section: ' + title))
    want = [t for t in REQUIRED_SECTIONS if t in expected_seen]
    got = [t for _, t in ordered_found]
    if got != want:
        for (ln, title), expected_title in zip(ordered_found, want):
            if title != expected_title:
                raw.append((ln, 'E202', 'required sections out of order'))
                break

    first_occurrence = {title: ln for ln, title in ordered_found}

    def section_span(title):
        """(heading_line, last_line) of a section, or None if missing."""
        if title not in first_occurrence:
            return None
        start = first_occurrence[title]
        following = [l for l in h2_lines if l > start]
        end = min(following) - 1 if following else len(lines)
        return start, end

    # --- ASSEMBLY DRAWING (E301) ---
    span = section_span('## ASSEMBLY DRAWING')
    if span is not None:
        head, end = span
        if not any(head < ml <= end for ml in mermaid_lines):
            raw.append((head, 'E301', 'no mermaid fence in ASSEMBLY DRAWING'))

    # --- BILL OF MATERIALS (E401-E404) ---
    bom_rows = []          # (part, line) per data row, in order
    bom_part_set = set()
    bom_present = False
    span = section_span('## BILL OF MATERIALS')
    if span is not None:
        head, end = span
        tables = find_tables(lines, fenced, head + 1, end)
        if not tables:
            raw.append((head, 'E401', 'no table in BILL OF MATERIALS'))
        else:
            bom_present = True
            table = tables[0]
            seen_parts = set()
            for row_ln in table['data']:
                cells = parse_cells(lines[row_ln - 1])
                part = cells[0] if cells else ''
                bom_rows.append((part, row_ln))
                bom_part_set.add(part)
                if not PART_RE.fullmatch(part):
                    raw.append((row_ln, 'E402',
                                'BOM part number does not match P<digits>: '
                                + repr(part)))
                elif part in seen_parts:
                    raw.append((row_ln, 'E403',
                                'duplicate BOM part number ' + part))
                seen_parts.add(part)
            if part_count is not None and part_count != len(table['data']):
                raw.append((part_count_line, 'E404',
                            'part_count %d does not equal BOM data-row '
                            'count %d' % (part_count, len(table['data']))))
            # Ref column (E405-E407): Part is the local find number, Ref is
            # the global part number — local, or the DO-NNN of a registered
            # part. Resolved by header name, like the contract columns.
            header_cells = parse_cells(lines[table['header'] - 1])
            ref_idx = None
            for ci, cell in enumerate(header_cells):
                if cell.lower() == 'ref':
                    ref_idx = ci
            if ref_idx is None:
                raw.append((table['header'], 'E406',
                            'BOM table has no Ref column'))
            else:
                own_id = None
                item = entries.get('id')
                if item is not None and item[0] != '':
                    own_id = item[0]
                for row_ln in table['data']:
                    cells = parse_cells(lines[row_ln - 1])
                    ref = cells[ref_idx] if ref_idx < len(cells) else ''
                    if ref != 'local' and not ID_RE.fullmatch(ref):
                        raw.append((row_ln, 'E405',
                                    'BOM ref neither local nor DO-NNN: '
                                    + repr(ref)))
                    elif own_id is not None and ref == own_id:
                        raw.append((row_ln, 'E407',
                                    "BOM ref cites the drawing's own id"))

    # --- DETAIL DRAWINGS (E501-E503) ---
    span = section_span('## DETAIL DRAWINGS')
    if span is not None:
        head, end = span
        h3s = []  # (line, part-or-None) for every H3 in the section
        for i in range(head, end):
            if fenced[i]:
                continue
            if H3_RE.match(lines[i]):
                m = H3_DETAIL_RE.match(lines[i])
                h3s.append((i + 1, m.group(1) if m else None))
        detail_parts = set()
        for idx, (ln, part) in enumerate(h3s):
            if part is None:
                continue
            detail_parts.add(part)
            if part not in bom_part_set:
                raw.append((ln, 'E503',
                            'detail heading references a part not in the '
                            'BOM: ' + part))
            body_end = h3s[idx + 1][0] - 1 if idx + 1 < len(h3s) else end
            has_mermaid = any(ln < ml <= body_end for ml in mermaid_lines)
            has_commodity = any('commodity part' in lines[j].lower()
                                for j in range(ln, body_end))
            has_external = any('external part' in lines[j].lower()
                               for j in range(ln, body_end))
            if not has_mermaid and not has_commodity and not has_external:
                raw.append((ln, 'E502',
                            'detail entry has neither a mermaid fence nor '
                            'a commodity or external-part note'))
            # E504 (rev C): in a derived drawing every drawn part states
            # where it lives in the source — a `Source: ` anchor line.
            # Commodity and external parts are exempt.
            if derived and not has_commodity and not has_external:
                has_anchor = any(
                    lines[j].strip().startswith('Source: ')
                    and lines[j].strip() != 'Source: '
                    for j in range(ln, body_end))
                if not has_anchor:
                    raw.append((ln, 'E504',
                                'derived detail entry has no Source: '
                                'anchor'))
        if bom_present:
            for part, row_ln in bom_rows:
                if part not in detail_parts:
                    raw.append((row_ln, 'E501',
                                'BOM part has no detail heading: ' + part))

    # Collect Process Plan op numbers for the E606 cross-reference. Same
    # dangling-reference pattern as BOM part <-> detail heading (E501/E503).
    process_ops = set()
    span_pp = section_span('## PROCESS PLAN')
    if span_pp is not None:
        head_pp, end_pp = span_pp
        pp_tables = find_tables(lines, fenced, head_pp + 1, end_pp)
        if pp_tables:
            for row_ln in pp_tables[0]['data']:
                cells = parse_cells(lines[row_ln - 1])
                if cells and INT_RE.fullmatch(cells[0]):
                    process_ops.add(cells[0])

    # --- CONTRACTS & TOLERANCES (E601-E606) ---
    op_ref_re = re.compile(r'Op\s+([0-9]+)')
    span = section_span('## CONTRACTS & TOLERANCES')
    if span is not None:
        head, end = span
        tables = find_tables(lines, fenced, head + 1, end)
        if not tables:
            raw.append((head, 'E601', 'no table in CONTRACTS & TOLERANCES'))
        for table in tables:
            header_cells = parse_cells(lines[table['header'] - 1])
            tol_idx = None
            op_idx = None
            basis_idx = None
            evidence_idx = None
            for ci, cell in enumerate(header_cells):
                low = cell.lower()
                if low == 'tolerance':
                    tol_idx = ci
                elif low == 'inspection op':
                    op_idx = ci
                elif low == 'basis':
                    basis_idx = ci
                elif low == 'evidence':
                    evidence_idx = ci
            if tol_idx is None:
                raw.append((table['header'], 'E602',
                            'contract table has no Tolerance column'))
                continue
            # E604: the structured op-reference column must exist, so that
            # tolerance->op coverage is machine-checkable at all. Resolved by
            # header name, not a fixed index.
            if op_idx is None:
                raw.append((table['header'], 'E604',
                            'contract table has no "Inspection op" column'))
            # E607 (rev C): a derived drawing's claims must be labeled and
            # evidence-bearing — both columns, or the table is nonconforming.
            if derived and (basis_idx is None or evidence_idx is None):
                raw.append((table['header'], 'E607',
                            'derived contract table lacks Basis or '
                            'Evidence column'))
            for row_ln in table['data']:
                cells = parse_cells(lines[row_ln - 1])
                cell = cells[tol_idx] if tol_idx < len(cells) else ''
                if cell in DASH_CELLS:
                    raw.append((row_ln, 'E603',
                                'tolerance cell empty or dash-only'))
                if derived and basis_idx is not None \
                        and evidence_idx is not None:
                    basis = cells[basis_idx] if basis_idx < len(cells) else ''
                    ev_cell = (cells[evidence_idx]
                               if evidence_idx < len(cells) else '')
                    # E608: the basis label is the claim's epistemic status.
                    if basis not in BASIS_VALUES:
                        raw.append((row_ln, 'E608',
                                    'Basis not one of observed|documented|'
                                    'inferred|unknown: ' + repr(basis)))
                        basis = None
                    # E609: every entry matches the citation grammar.
                    entries_ok = []
                    if ev_cell in DASH_CELLS:
                        raw.append((row_ln, 'E609',
                                    'Evidence cell empty or dash-only'))
                    else:
                        for entry in ev_cell.split(';'):
                            entry = entry.strip()
                            m = EVIDENCE_ENTRY_RE.fullmatch(entry)
                            if m is None:
                                raw.append((row_ln, 'E609',
                                            'evidence entry does not match '
                                            'citation grammar: '
                                            + repr(entry)))
                            else:
                                entries_ok.append(m.group(1))
                    # E610: basis and evidence cohere in form. observed
                    # needs code; documented needs doc/commit/issue;
                    # inferred needs a non-searched citation; searched is
                    # legal only under unknown.
                    if basis is not None and entries_ok:
                        kinds = set(entries_ok)
                        if basis == 'observed' and 'code' not in kinds:
                            raw.append((row_ln, 'E610',
                                        'Basis observed without a code '
                                        'citation'))
                        elif basis == 'documented' and not (
                                kinds & {'doc', 'commit', 'issue'}):
                            raw.append((row_ln, 'E610',
                                        'Basis documented without a doc, '
                                        'commit, or issue citation'))
                        elif basis == 'inferred' and kinds == {'searched'}:
                            raw.append((row_ln, 'E610',
                                        'Basis inferred with only searched '
                                        'entries'))
                        if 'searched' in kinds and basis != 'unknown':
                            raw.append((row_ln, 'E610',
                                        'searched entry outside Basis '
                                        'unknown'))
                    # E611: undetermined tolerance iff unknown basis —
                    # anything else is either an invented value or an
                    # unlabeled gap.
                    if basis is not None:
                        undet = cell.strip() == 'undetermined'
                        if undet and basis != 'unknown':
                            raw.append((row_ln, 'E611',
                                        'tolerance undetermined but Basis '
                                        'is not unknown'))
                        elif not undet and basis == 'unknown':
                            raw.append((row_ln, 'E611',
                                        'Basis unknown but tolerance is '
                                        'not undetermined'))
                if op_idx is None:
                    continue
                op_cell = cells[op_idx] if op_idx < len(cells) else ''
                # E605: every tolerance row must name an inspection op — the
                # coverage-gap check. Empty/dash or no "Op NN" both fire.
                if op_cell in DASH_CELLS:
                    raw.append((row_ln, 'E605',
                                'tolerance has no inspection op (coverage gap)'))
                    continue
                refs = op_ref_re.findall(op_cell)
                if not refs:
                    raw.append((row_ln, 'E605',
                                'inspection-op cell has no Op NN reference: '
                                + repr(op_cell)))
                    continue
                # E606: every referenced op must exist in the Process Plan.
                for num in refs:
                    if num not in process_ops:
                        raw.append((row_ln, 'E606',
                                    'inspection op Op %s not in Process Plan'
                                    % num))

    return [(path, ln, code, msg) for ln, code, msg in raw]


def extract_id(text):
    """Return (id_value, line) from the front matter, or None."""
    entries, _, _ = parse_front_matter(text.splitlines())
    item = entries.get('id')
    if item is not None and item[0] != '':
        return item
    return None


def extract_refs(text):
    """Return [(ref_id, line), ...] — DO-NNN values in the BOM Ref column.

    Register-level input for the E408/E409 checks in main(). Malformed refs
    are ignored here; validate_file already reports them as E405.
    """
    lines = text.splitlines()
    fenced, _ = scan_fences(lines)
    refs = []
    start = None
    end = len(lines)
    for i, line in enumerate(lines):
        if fenced[i] or not H2_RE.match(line):
            continue
        if start is None:
            if line.strip() == '## BILL OF MATERIALS':
                start = i + 1
        else:
            end = i
            break
    if start is None:
        return refs
    tables = find_tables(lines, fenced, start + 1, end)
    if not tables:
        return refs
    table = tables[0]
    header_cells = parse_cells(lines[table['header'] - 1])
    ref_idx = None
    for ci, cell in enumerate(header_cells):
        if cell.lower() == 'ref':
            ref_idx = ci
    if ref_idx is None:
        return refs
    for row_ln in table['data']:
        cells = parse_cells(lines[row_ln - 1])
        ref = cells[ref_idx] if ref_idx < len(cells) else ''
        if ID_RE.fullmatch(ref):
            refs.append((ref, row_ln))
    return refs


def collect_files(args):
    """Expand each argument into LLD.md file paths. Returns (files, bad_arg)."""
    files = []
    for arg in args:
        if os.path.isdir(arg):
            for root, dirs, names in os.walk(arg):
                dirs.sort()
                if 'LLD.md' in names:
                    files.append(os.path.join(root, 'LLD.md'))
        elif os.path.isfile(arg):
            files.append(arg)
        else:
            return files, arg
    unique = []
    seen = set()
    for path in files:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique, None


def main(argv):
    if len(argv) < 2:
        print(USAGE, file=sys.stderr)
        return 2
    files, bad_arg = collect_files(argv[1:])
    if bad_arg is not None:
        print('error: no such file or directory: ' + bad_arg, file=sys.stderr)
        return 2
    errors = []
    seen_ids = {}
    refs_by_path = []  # (path, id_or_None, [(ref, line), ...])
    for path in files:
        try:
            with open(path, encoding='utf-8-sig') as fh:
                text = fh.read()
        except OSError as exc:
            print('error: cannot read %s: %s' % (path, exc), file=sys.stderr)
            return 2
        errors.extend(validate_file(path, text))
        found = extract_id(text)
        file_id = None
        if found is not None:
            value, line = found
            file_id = value
            if value in seen_ids:
                errors.append((path, line, 'E109',
                               'duplicate id %s across the validated set '
                               '(first in %s)' % (value, seen_ids[value])))
            else:
                seen_ids[value] = path
        refs_by_path.append((path, file_id, extract_refs(text)))

    # Register-wide composition checks (E408-E409). Like E109, these are
    # properties of the validated set: run over designs/ (the CI run) for a
    # verdict on the whole register.
    graph = {}  # id -> [(ref_id, path, line), ...], resolved edges only
    for path, file_id, refs in refs_by_path:
        for ref, line in refs:
            if ref not in seen_ids:
                errors.append((path, line, 'E408',
                               'BOM ref %s not found in validated set' % ref))
            elif file_id is not None and ref != file_id:
                graph.setdefault(file_id, []).append((ref, path, line))

    state = {}  # 0 unvisited, 1 on stack, 2 done
    reported = set()

    def visit(node, trail):
        state[node] = 1
        trail.append(node)
        for ref, path, line in graph.get(node, []):
            if state.get(ref, 0) == 1:
                cycle = trail[trail.index(ref):] + [ref]
                key = frozenset(cycle)
                if key not in reported:
                    reported.add(key)
                    errors.append((path, line, 'E409',
                                   'BOM ref cycle: ' + ' -> '.join(cycle)))
            elif state.get(ref, 0) == 0:
                visit(ref, trail)
        trail.pop()
        state[node] = 2

    for node in sorted(graph):
        if state.get(node, 0) == 0:
            visit(node, [])

    errors.sort(key=lambda e: (e[0], e[1], e[2]))
    for path, line, code, message in errors:
        print('%s:%d: %s %s' % (path, line, code, message))
    if errors:
        return 1
    print('OK: %d file(s), 0 errors' % len(files))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
