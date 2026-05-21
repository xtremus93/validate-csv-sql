"""
validate_csv.py
---------------
Validates a local CSV file against a SQL Server table schema (parsed from DDL)
before import. Uses positional column mapping. No external dependencies.

USAGE:
    python validate_csv.py <csv_file> <ddl_file>

ARGUMENTS:
    csv_file    Path to the CSV file to validate.
    ddl_file    Path to a .sql or .txt file containing the CREATE TABLE statement.

EXAMPLE:
    python validate_csv.py employees.csv employees_schema.sql
"""

import argparse
import csv
import re
import sys
from datetime import datetime
from collections import defaultdict


# ---------------------------------------------------------------------------
# DDL PARSER
# ---------------------------------------------------------------------------

# Integer type bounds (inclusive)
_INT_RANGES = {
    "tinyint":   (0, 255),
    "smallint":  (-32768, 32767),
    "int":       (-2_147_483_648, 2_147_483_647),
    "integer":   (-2_147_483_648, 2_147_483_647),
    "bigint":    (-9_223_372_036_854_775_808, 9_223_372_036_854_775_807),
}

# Canonical type families
_TYPE_FAMILY = {
    "tinyint":   "int",
    "smallint":  "int",
    "int":       "int",
    "integer":   "int",
    "bigint":    "int",
    "decimal":   "decimal",
    "numeric":   "decimal",
    "varchar":   "varchar",
    "nvarchar":  "varchar",
    "char":      "varchar",
    "nchar":     "varchar",
    "date":      "date",
    "datetime":  "datetime",
    "datetime2": "datetime",
    "bit":       "bit",
}


def parse_ddl(ddl: str) -> list[dict]:
    """
    Parse a SQL Server CREATE TABLE DDL string into a list of column descriptors.

    Each descriptor is a dict with keys:
        name        str   — column name (no brackets)
        family      str   — 'int' | 'decimal' | 'varchar' | 'date' | 'bit'
        raw_type    str   — lower-case base type token
        nullable    bool  — True if NULL is allowed
        max_len     int | None  — for varchar family
        precision   int | None  — for decimal family
        scale       int | None  — for decimal family
        int_min     int | None  — for int family
        int_max     int | None  — for int family
    """
    # Strip everything outside the outermost parentheses
    body_match = re.search(r'\((.+)\)', ddl, re.DOTALL)
    if not body_match:
        raise ValueError("DDL does not contain a column definition block.")
    body = body_match.group(1)

    # Split on commas that are NOT inside parentheses
    # (handles DECIMAL(10,2) without splitting mid-type)
    col_defs = _split_columns(body)

    columns = []
    for raw in col_defs:
        raw = raw.strip()
        if not raw:
            continue
        # Skip table-level constraints (PRIMARY KEY, UNIQUE, CHECK, INDEX …)
        if re.match(r'(?i)^\s*(primary\s+key|unique|check|constraint|index)', raw):
            continue

        col = _parse_column_def(raw)
        if col:
            columns.append(col)

    if not columns:
        raise ValueError("DDL parser found no column definitions.")
    return columns


def _split_columns(body: str) -> list[str]:
    """Split column-definition body on commas outside parentheses."""
    parts = []
    depth = 0
    current = []
    for ch in body:
        if ch == '(':
            depth += 1
            current.append(ch)
        elif ch == ')':
            depth -= 1
            current.append(ch)
        elif ch == ',' and depth == 0:
            parts.append(''.join(current))
            current = []
        else:
            current.append(ch)
    if current:
        parts.append(''.join(current))
    return parts


def _parse_column_def(raw: str) -> dict | None:
    """Parse a single column definition line."""
    # Match: optional_bracket_name  type_token  optional_args  optional_null
    pattern = re.compile(
        r'^\[?(?P<name>[^\]\s]+)\]?'            # column name, optional brackets
        r'\s+'
        r'\[?(?P<type>[A-Za-z]+)\]?'             # base type, optional brackets (SSMS style)
        r'(?:\s*\((?P<args>[^)]+)\))?'           # optional (precision, scale) or (length)
        r'(?:\s+(?P<nullspec>NOT\s+NULL|NULL))?', # optional NULL / NOT NULL
        re.IGNORECASE
    )
    m = pattern.match(raw.strip())
    if not m:
        return None

    name     = m.group('name')
    raw_type = m.group('type').lower()
    args     = m.group('args')
    nullspec = (m.group('nullspec') or '').upper().replace(' ', '')

    family = _TYPE_FAMILY.get(raw_type)
    if family is None:
        # Unsupported type — treat as unvalidated passthrough
        family = 'unknown'

    nullable = (nullspec != 'NOTNULL')   # default is nullable in SQL Server

    col = {
        'name':      name,
        'family':    family,
        'raw_type':  raw_type,
        'nullable':  nullable,
        'max_len':   None,
        'precision': None,
        'scale':     None,
        'int_min':   None,
        'int_max':   None,
    }

    if family == 'int':
        lo, hi = _INT_RANGES.get(raw_type, (-2_147_483_648, 2_147_483_647))
        col['int_min'] = lo
        col['int_max'] = hi

    elif family == 'decimal' and args:
        parts = [p.strip() for p in args.split(',')]
        col['precision'] = int(parts[0]) if parts[0].isdigit() else 18
        col['scale']     = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0

    elif family == 'varchar' and args:
        arg = args.strip()
        if arg.upper() == 'MAX':
            col['max_len'] = None   # unbounded
        elif arg.isdigit():
            col['max_len'] = int(arg)

    return col


# ---------------------------------------------------------------------------
# VALIDATORS
# ---------------------------------------------------------------------------

_DATE_FORMAT     = "%m/%d/%Y"
_DATETIME_FORMATS = [
    "%m/%d/%Y %H:%M",      # MM/DD/YYYY HH:mm
    "%m/%d/%Y %H:%M:%S",   # MM/DD/YYYY HH:mm:ss
]


def validate_int(value: str, col: dict) -> str | None:
    try:
        iv = int(value)
    except ValueError:
        return f"'{value}' is not a valid integer"
    lo, hi = col['int_min'], col['int_max']
    if lo is not None and not (lo <= iv <= hi):
        return f"'{value}' out of range [{lo}, {hi}] for {col['raw_type'].upper()}"
    return None


def validate_decimal(value: str, col: dict) -> str | None:
    # Allow optional leading sign, digits, optional decimal point + digits
    if not re.match(r'^[+-]?\d+(\.\d+)?$', value):
        return f"'{value}' is not a valid decimal number"
    p = col['precision']
    s = col['scale']
    if p is None:
        return None
    # Split into integer and fractional parts
    if '.' in value:
        int_part, frac_part = value.lstrip('+-').split('.')
    else:
        int_part, frac_part = value.lstrip('+-'), ''
    if s is not None and len(frac_part) > s:
        return f"'{value}' has {len(frac_part)} decimal place(s); max allowed is {s}"
    # Total significant digits: integer digits + actual fractional digits
    total_digits = len(int_part.lstrip('0') or '0') + len(frac_part)
    max_int_digits = p - (s or 0)
    if len(int_part.lstrip('0') or '0') > max_int_digits:
        return (f"'{value}' integer part exceeds allowed {max_int_digits} "
                f"digit(s) for DECIMAL({p},{s})")
    return None


def validate_varchar(value: str, col: dict) -> str | None:
    max_len = col['max_len']
    if max_len is None:
        return None
    if len(value) > max_len:
        return f"length {len(value)} exceeds max {max_len}"
    return None


def validate_date(value: str, col: dict) -> str | None:
    try:
        datetime.strptime(value, _DATE_FORMAT)
    except ValueError:
        return f"'{value}' does not match MM/DD/YYYY"
    return None


def validate_datetime(value: str, col: dict) -> str | None:
    for fmt in _DATETIME_FORMATS:
        try:
            datetime.strptime(value, fmt)
            return None
        except ValueError:
            continue
    return f"'{value}' does not match MM/DD/YYYY HH:mm or MM/DD/YYYY HH:mm:ss"


def validate_bit(value: str, col: dict) -> str | None:
    if value not in ('0', '1'):
        return f"'{value}' is not a valid BIT value (expected 0 or 1)"
    return None


_VALIDATORS = {
    'int':      validate_int,
    'decimal':  validate_decimal,
    'varchar':  validate_varchar,
    'date':     validate_date,
    'datetime': validate_datetime,
    'bit':      validate_bit,
}


# ---------------------------------------------------------------------------
# RANGE COMPRESSION
# ---------------------------------------------------------------------------

def compress_rows(rows: list[int]) -> str:
    """Convert a sorted list of 1-indexed row numbers to a compact string."""
    if not rows:
        return ''
    rows = sorted(set(rows))
    ranges = []
    start = end = rows[0]
    for r in rows[1:]:
        if r == end + 1:
            end = r
        else:
            ranges.append((start, end))
            start = end = r
    ranges.append((start, end))
    parts = []
    for s, e in ranges:
        parts.append(str(s) if s == e else f"{s}–{e}")
    return ', '.join(parts)


# ---------------------------------------------------------------------------
# MAIN VALIDATION LOGIC
# ---------------------------------------------------------------------------

def validate(csv_path: str, ddl: str) -> None:
    # --- Parse DDL ---
    try:
        schema = parse_ddl(ddl)
    except ValueError as exc:
        print(f"[FATAL] DDL parsing failed: {exc}")
        return

    # --- Read CSV ---
    try:
        with open(csv_path, newline='', encoding='utf-8') as fh:
            reader = csv.reader(fh)
            try:
                header = next(reader)
            except StopIteration:
                print("[FATAL] CSV file is empty.")
                return
            rows = list(reader)
    except FileNotFoundError:
        print(f"[FATAL] File not found: {csv_path}")
        return
    except Exception as exc:
        print(f"[FATAL] Could not read CSV: {exc}")
        return

    # Strip BOM / whitespace from header tokens
    header = [h.strip().lstrip('\ufeff') for h in header]

    # -----------------------------------------------------------------------
    # CHECK 1 — Column count
    # -----------------------------------------------------------------------
    if len(header) != len(schema):
        print("❌ Validation failed. 1 error(s) found.\n")
        print("COLUMN COUNT ERROR")
        print(f"  CSV has {len(header)} column(s); DDL defines {len(schema)} column(s).")
        print("  Aborting — column count mismatch makes further validation unreliable.")
        return

    # -----------------------------------------------------------------------
    # CHECK 2 — Column names (positional, case-insensitive)
    # -----------------------------------------------------------------------
    name_errors = []
    for idx, (csv_col, ddl_col) in enumerate(zip(header, schema)):
        if csv_col.lower() != ddl_col['name'].lower():
            name_errors.append(
                f"  Position {idx + 1}: CSV header '{csv_col}' "
                f"!= DDL column '{ddl_col['name']}'"
            )

    # Collect data-level errors
    # Structure: errors[error_type][col_name] = {'rows': [], 'detail': str}
    # We use a list of tuples per column to handle distinct detail messages
    dtype_errors   = defaultdict(list)   # (col_name, detail) -> [row_nums]
    null_errors    = defaultdict(list)   # col_name -> [row_nums]

    # Use a dict keyed by (col_name, detail) for dtype grouping
    dtype_map  = defaultdict(list)   # (col_name, detail) -> [row_nums]
    null_map   = defaultdict(list)   # col_name -> [row_nums]

    total_data_rows = len(rows)

    for row_idx, row in enumerate(rows, start=1):
        # Pad short rows / truncate long rows to schema width for cell-level checks
        padded = row + [''] * max(0, len(schema) - len(row))

        for col_idx, col in enumerate(schema):
            if col_idx >= len(padded):
                raw_val = ''
            else:
                raw_val = padded[col_idx].strip()

            col_name = col['name']

            # --- NULL check ---
            if raw_val == '':
                if not col['nullable']:
                    null_map[col_name].append(row_idx)
                # Skip type check for empty cells (null check already flagged it)
                continue

            # --- Type check ---
            validator = _VALIDATORS.get(col['family'])
            if validator is None:
                continue   # unknown / unsupported type — skip
            error_msg = validator(raw_val, col)
            if error_msg:
                dtype_map[(col_name, error_msg)].append(row_idx)

    # -----------------------------------------------------------------------
    # REPORT
    # -----------------------------------------------------------------------
    all_errors = bool(name_errors or dtype_map or null_map)

    if not all_errors:
        print(f"✅ Validation passed. File is ready for import.")
        print(f"   Rows validated: {total_data_rows:,}")
        return

    error_count = len(name_errors) + len(dtype_map) + len(null_map)
    print(f"❌ Validation failed. {error_count} error pattern(s) found.\n")

    if name_errors:
        print("─" * 70)
        print("COLUMN NAME ERRORS")
        for e in name_errors:
            print(e)
        print()

    if dtype_map:
        print("─" * 70)
        print("DATA TYPE ERRORS")
        for (col_name, detail), row_list in sorted(dtype_map.items()):
            row_str = compress_rows(row_list)
            print(f"  [DATA TYPE] Column: {col_name} | "
                  f"Rows: {row_str} | Detail: {detail}")
        print()

    if null_map:
        print("─" * 70)
        print("NULL CONSTRAINT ERRORS")
        for col_name, row_list in sorted(null_map.items()):
            row_str = compress_rows(row_list)
            print(f"  [NOT NULL] Column: {col_name} | "
                  f"Rows: {row_str} | Detail: empty/missing value in NOT NULL column")
        print()

    print("─" * 70)
    print(f"Total rows inspected: {total_data_rows:,}")


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------

def _load_ddl(ddl_path: str) -> str:
    valid_extensions = ('.sql', '.txt')
    ext = ddl_path.lower()[-4:]
    if not any(ddl_path.lower().endswith(e) for e in valid_extensions):
        print(f"[FATAL] DDL file must be .sql or .txt (got: {ddl_path})")
        sys.exit(1)
    try:
        with open(ddl_path, encoding='utf-8') as fh:
            return fh.read()
    except FileNotFoundError:
        print(f"[FATAL] DDL file not found: {ddl_path}")
        sys.exit(1)
    except Exception as exc:
        print(f"[FATAL] Could not read DDL file: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Validate a CSV file against a SQL Server CREATE TABLE schema."
    )
    parser.add_argument("csv_file", help="Path to the CSV file to validate.")
    parser.add_argument(
        "ddl_file",
        help="Path to a .sql or .txt file containing the CREATE TABLE statement.",
    )
    args = parser.parse_args()

    ddl = _load_ddl(args.ddl_file)
    validate(args.csv_file, ddl)
