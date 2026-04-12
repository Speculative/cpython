# Bytecode-to-Source Mapping

One of our core requirements is mapping every traced bytecode instruction back to its exact source code location. CPython provides rich source location data — not just line numbers but precise column offsets.

## Overview

Every `PyCodeObject` contains a `co_linetable` field — a compact bytes object that maps each bytecode instruction to a 4-tuple:

```
(start_line, end_line, start_column, end_column)
```

This was introduced in Python 3.11 (PEP 657) for enhanced error messages. For our purposes, this means we can trace execution at **sub-expression** granularity.

## The co_linetable Encoding Format

**Reference:** `InternalDocs/code_objects.md`, `Objects/codeobject.c`

The linetable is a sequence of variable-length entries. Each entry starts with a byte whose MSB is 1, followed by zero or more continuation bytes with MSB 0.

### First Byte Structure

```
Bit 7:    1 (always set — marks start of entry)
Bits 3-6: Location info code (0-15)
Bits 0-2: Length - 1 (bytecode units covered, 1-8)
```

### Location Info Codes

Defined in `Include/cpython/code.h:314-325`:

```c
typedef enum _PyCodeLocationInfoKind {
    PY_CODE_LOCATION_INFO_SHORT0 = 0,    // codes 0-9: short form
    // ...
    PY_CODE_LOCATION_INFO_ONE_LINE0 = 10, // codes 10-12: one-line
    PY_CODE_LOCATION_INFO_ONE_LINE1 = 11,
    PY_CODE_LOCATION_INFO_ONE_LINE2 = 12,
    PY_CODE_LOCATION_INFO_NO_COLUMNS = 13, // line only, no columns
    PY_CODE_LOCATION_INFO_LONG = 14,       // full location
    PY_CODE_LOCATION_INFO_NONE = 15        // no location
} _PyCodeLocationInfoKind;
```

### Encoding Variants

| Code | Type | When Used | Format |
|---|---|---|---|
| 0-9 | Short | Same line, col < 80, narrow span | Code encodes `col >> 3`; second byte has `(col_low << 4) \| span` |
| 10-12 | One-line | Line delta 0-2, columns < 128 | Code - 10 = line delta; then col, end_col bytes |
| 13 | No columns | Column info unavailable | Line delta as signed varint |
| 14 | Long | Complex locations, multiline | Line delta (svarint), end_line_delta (varint), col+1 (varint), endcol+1 (varint) |
| 15 | None | No source location | No payload |

### Variable-Length Integer Encoding

The linetable uses a varint encoding with 6-bit chunks:
- **Unsigned varint:** Bit 6 set = more bytes follow. Values accumulated from LSB.
- **Signed varint:** Zigzag encoding — `positive → val << 1`, `negative → ((-val) << 1) | 1`

Implementation: `Include/internal/pycore_code.h:388-423`

## C APIs for Location Lookup

### Quick line number lookup

```c
// Objects/codeobject.c:1012-1028
int PyCode_Addr2Line(PyCodeObject *co, int bytecode_offset);
```

Returns just the start line number for a given bytecode offset.

### Full position lookup

```c
// Objects/codeobject.c:1248-1265
int PyCode_Addr2Location(
    PyCodeObject *co, int offset,
    int *start_line, int *start_column,
    int *end_line, int *end_column
);
```

Returns the complete 4-tuple position.

### Iterator API

For walking all entries efficiently (better than repeated point lookups):

```c
// Initialize iterator
void _PyCode_InitAddressRange(PyCodeObject *co, PyCodeAddressRange *bounds);

// Advance to next entry
int _PyLineTable_NextAddressRange(PyCodeAddressRange *range);

// Go backward
int _PyLineTable_PreviousAddressRange(PyCodeAddressRange *range);
```

The `PyCodeAddressRange` struct:
```c
typedef struct _line_offsets {
    int ar_start;        // start bytecode offset
    int ar_end;          // end bytecode offset
    int ar_line;         // line number
    struct _opaque opaque; // internal parsing state
} PyCodeAddressRange;
```

## Python-Level APIs

### `code.co_lines()`

Returns iterator of `(start_offset, end_offset, line_number)` tuples. Line-level granularity only.

### `code.co_positions()`

Returns iterator of `(start_line, end_line, start_col, end_col)` tuples — one per instruction. This is the full-fidelity API.

### `dis.get_instructions()`

Returns `Instruction` namedtuples that include a `positions` field with full location info:

```python
Instruction(
    opname='LOAD_FAST',
    opcode=124,
    arg=0,
    argval='x',
    offset=0,
    positions=Positions(lineno=1, end_lineno=1, col_offset=0, end_col_offset=1)
)
```

## Linetable Generation

**File:** `Python/assemble.c`

During assembly, the compiler emits linetable entries using a cascade of encoding functions:

1. `write_location_info_short_form()` — tries the most compact encoding first
2. `write_location_info_oneline_form()` — one-line with explicit columns
3. `write_location_info_long_form()` — full encoding for complex cases
4. `write_location_info_no_column()` — line only, when columns unavailable
5. `write_location_info_none()` — no location (synthetic instructions)

The decision logic in `write_location_info_entry()` (lines 286-321) tries encodings from most to least compact.

## Example: Decoding in Practice

For the expression `result = x + y * z` on line 5:

```
Instruction    Offset   Position
LOAD_FAST x    0        (5, 5, 9, 10)    -- "x"
LOAD_FAST y    2        (5, 5, 13, 14)   -- "y"
LOAD_FAST z    4        (5, 5, 17, 18)   -- "z"
BINARY_OP *    6        (5, 5, 13, 18)   -- "y * z"
BINARY_OP +    10       (5, 5, 9, 18)    -- "x + y * z"
STORE_FAST     12       (5, 5, 0, 18)    -- "result = x + y * z"
```

Each instruction maps to the exact sub-expression it computes. This level of detail would allow our tracer to highlight the precise expression being evaluated at each step.

## Legacy Compatibility: co_lnotab

The old `co_lnotab` format (Python 3.10 and earlier) is lazily generated from `co_linetable` when accessed via the `co_lnotab` attribute. It strips column information and preserves only line-level granularity. We should use `co_linetable` directly.

Implementation: `remove_column_info()` in `Objects/codeobject.c:642-682`

## Implications for Tracing

### Pre-computation strategy

Since code objects are immutable, we can **pre-compute the full offset-to-location mapping** when a code object is first encountered, then do O(1) lookups during tracing:

```
// Pseudo-code for pre-computation
for each code object:
    build array: instruction_offset -> (line, end_line, col, end_col)
    cache this mapping keyed by code object identity
```

### Sub-expression tracing

The column-level precision means we could theoretically trace at sub-expression granularity. However, this would generate enormous volumes of data. A practical approach might be:

- **Default:** Trace at line granularity (using `LINE` events from PEP 669)
- **Detailed mode:** For specific code ranges, trace at instruction level with full positions
- **Reconstruction:** Use the mapping table to enrich line-level traces with expression detail post-hoc

### Key files

| File | Purpose |
|---|---|
| `Include/cpython/code.h:314-325` | Location info enum |
| `Include/internal/pycore_code.h:388-423` | Varint encoding helpers |
| `Objects/codeobject.c:1012-1293` | Decoding functions |
| `Python/assemble.c:233-366` | Encoding functions |
| `Lib/dis.py:285-294, 658-686` | Python-level position APIs |
| `InternalDocs/code_objects.md` | Official format documentation |
