# Trace Capture Strategy and Recommendations

This document synthesizes findings from the investigation into a concrete strategy for implementing full execution tracing in CPython.

## Goal Recap

Capture enough information during execution to reconstruct the view a programmer would have if they were stepping into every line of code and examining all variables at each step. Do this with minimal performance impact.

## What Information Must Be Captured

To reconstruct a "stepping debugger" view, each trace event needs:

### Per-Event (at each line or instruction)

| Data | How to Get It | Size |
|---|---|---|
| Timestamp | `PyTime_PerfCounterRaw()` | 8 bytes |
| Thread ID | `tstate->thread_id` | 8 bytes |
| Code object identity | `frame->f_executable` pointer or `code->co_version` | 8 bytes |
| Instruction offset | `frame->instr_ptr - code_start` | 4 bytes |
| Event type | PEP 669 event ID | 1 byte |

**~29 bytes per event minimum** (just metadata, no variable values)

### Per-Event Variable Snapshot (the expensive part)

For each local variable that changed since the last event:

| Data | How to Get It | Size |
|---|---|---|
| Variable index | Index into `localsplus` | 2 bytes |
| Value type tag | `ob_type` pointer or type ID | 2-8 bytes |
| Value | Type-dependent serialization | Variable |

### Per-Code-Object (cached, captured once)

| Data | How to Get It | Size |
|---|---|---|
| Filename | `code->co_filename` | String |
| Function name | `code->co_qualname` | String |
| Variable names | `code->co_localsplusnames` | Tuple of strings |
| Variable kinds | `code->co_localspluskinds` | Bytes |
| Source location table | `code->co_linetable` | Bytes |
| Source code | Read from filesystem | String |
| Argument count | `code->co_argcount` etc. | Ints |

## Architecture Decision: Native Extension vs. Fork

### Option A: C Extension Module using PEP 669

**Approach:** Build a C extension that registers as a PEP 669 monitoring tool (tool IDs 0-4 are available). The extension implements callbacks in C for maximum speed, writes events to a per-thread ring buffer, and provides a Python API for controlling tracing and reading results.

**Pros:**
- No CPython fork required — works with stock Python 3.12+
- PEP 669 provides zero-overhead when inactive
- Can coexist with other tools (debuggers, profilers)
- Installable via pip
- Automatically benefits from CPython improvements

**Cons:**
- Callback overhead per event (~50-200ns per PEP 669 callback from C)
- Cannot access some internal structures without `Py_BUILD_CORE`
- Limited by the events PEP 669 exposes
- Cannot trace inside the eval loop at sub-opcode granularity
- Variable snapshots require `PyFrame_GetLocals()` or direct `localsplus` access

**Estimated overhead:** 2-5x for line-level tracing, 5-15x for instruction-level

### Option B: CPython Fork with Eval Loop Instrumentation

**Approach:** Fork CPython and modify `Python/ceval.c` to emit trace events directly from the eval loop, bypassing the PEP 669 callback mechanism entirely.

**Pros:**
- Minimal per-event overhead (inline in eval loop, no function call)
- Full access to all internal state (frame, stack, locals)
- Can capture information PEP 669 doesn't expose (stack contents, intermediate values)
- Could implement "always-on" tracing with <2x overhead

**Cons:**
- Must track upstream CPython changes (significant maintenance burden)
- Users must install a custom Python build
- Risk of introducing bugs in the core interpreter
- Harder to distribute

**Estimated overhead:** 1.3-2x for line-level, 2-5x for instruction-level

### Option C: Hybrid — C Extension with Strategic Internal Access

**Approach:** Build a C extension that uses PEP 669 for event notification but accesses internal CPython structures (via `Py_BUILD_CORE` or carefully crafted struct definitions) for efficient data capture.

**Pros:**
- Works with stock CPython (if we avoid `Py_BUILD_CORE` and use offset-based access)
- Or works with a minimal patch (just exposing a few internal APIs)
- Near-fork performance for data capture
- PEP 669 handles the hard parts (instrumentation, thread safety)

**Cons:**
- Fragile against CPython version changes (struct layouts change)
- Requires per-version compatibility shims
- Gray area of API stability

### Recommendation: Start with Option A, Evolve to Option C

1. **Phase 1:** Pure C extension using public APIs and PEP 669. Prove the concept, get the data model right.
2. **Phase 2:** Optimize hot paths by accessing internal structures. Use `PyFrame_GetLocals()` initially, then direct `localsplus` access.
3. **Phase 3:** If performance is still insufficient, consider a minimal CPython patch that exposes a fast trace buffer API.

**Rationale:** A fork is too expensive to maintain long-term. PEP 669 already provides 90% of what we need. The remaining 10% (efficient variable capture) can be solved with careful C code that reads internal structures.

## Detailed Design: C Extension Tracer

### Component Architecture

```
┌─────────────────────────────────────────────────┐
│                  Python API Layer                │
│  tracer.start() / tracer.stop() / tracer.dump() │
└─────────────────────┬───────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────┐
│              C Extension Module                  │
│                                                  │
│  ┌──────────────┐  ┌─────────────────────────┐  │
│  │  PEP 669     │  │   Code Object Cache     │  │
│  │  Callbacks   │  │   (metadata, loc table)  │  │
│  │  (C funcs)   │  └─────────────────────────┘  │
│  └──────┬───────┘                                │
│         │           ┌─────────────────────────┐  │
│         ├──────────►│  Per-Thread Ring Buffer  │  │
│         │           │  (lock-free, fixed-size) │  │
│         │           └──────────┬──────────────┘  │
│         │                      │                 │
│         │           ┌──────────▼──────────────┐  │
│         │           │  Value Serializer       │  │
│         │           │  (type-aware snapshots)  │  │
│         │           └─────────────────────────┘  │
└─────────────────────────────────────────────────┘
                      │
                      ▼
              Trace Output (file / memory / stream)
```

### Per-Thread Ring Buffer

Each thread gets a pre-allocated ring buffer (e.g., 16MB) for lock-free event recording:

```c
typedef struct {
    uint8_t *data;           // buffer memory
    size_t capacity;         // total size
    size_t write_pos;        // current write position (atomic for free-threaded)
    size_t flush_pos;        // last flushed position
    int64_t thread_id;       // owning thread
} TraceBuffer;
```

Events are written sequentially. When the buffer fills, either:
- **Overwrite oldest** (circular buffer, for always-on tracing)
- **Flush to disk** (for complete capture)
- **Stop tracing** (for bounded capture)

### Event Encoding

Compact binary format to minimize buffer usage:

```
Event header (1 byte):
  Bits 0-3: event type (16 types)
  Bits 4-7: flags (has_vars, has_timestamp, etc.)

Followed by (depending on flags):
  - Timestamp delta (1-8 bytes, varint)
  - Code object ID (4 bytes, cached index)
  - Instruction offset (2 bytes)
  - Variable changes (variable length):
      - Count (1 byte)
      - For each: index (1 byte) + serialized value
```

### Value Serialization Strategy

Full object serialization is expensive. Tiered approach:

**Tier 1: Inline values (≤16 bytes, no allocation)**
- `None`, `True`, `False` — 1 byte tag
- Small ints (-128..127) — 1 byte tag + 1 byte value
- Larger ints (fits in 64 bits) — 1 byte tag + 8 bytes
- Floats — 1 byte tag + 8 bytes

**Tier 2: Short strings/bytes (≤64 bytes)**
- 1 byte tag + 1 byte length + data

**Tier 3: Object identity only (for complex objects)**
- 1 byte tag + 8 bytes `id()` + type tag
- Full serialization deferred to post-processing

**Tier 4: Deep snapshot (optional, expensive)**
- repr() or custom serialization
- Only for objects explicitly requested

### Change Detection for Variables

Rather than snapshotting all locals at every line, detect which changed:

```c
// In the LINE event callback:
for (int i = 0; i < code->co_nlocals; i++) {
    PyObject *current = localsplus[i];
    if (current != prev_locals[i]) {
        // Variable changed (pointer comparison)
        record_variable_change(buf, i, current);
        prev_locals[i] = current;
    }
}
```

For immutable types (int, str, tuple), pointer comparison is sufficient to detect changes. For mutable types (list, dict), we'd need either:
- Version counters (dicts have `ma_version_tag` — but this was removed in 3.12)
- Identity-only tracking (record that the object was mutated, defer deep snapshot)
- Hash comparison (expensive but thorough)

**Practical approach:** Use pointer comparison for all types. This catches reassignment (`x = new_value`) but not mutation (`x.append(item)`). For mutation tracking, optionally use dict/list watchers (another 3.12+ feature).

### Code Object Cache

Since code object metadata is immutable, cache it on first encounter:

```c
typedef struct {
    PyCodeObject *code;        // weak reference
    uint32_t cache_id;         // compact ID for trace events
    char *filename;            // cached co_filename (UTF-8)
    char *qualname;            // cached co_qualname (UTF-8)
    int *line_table;           // pre-decoded: offset -> line number
    // ... variable names, etc.
} CodeObjectCache;
```

Use a hash table keyed by code object pointer (or `co_version`).

## Efficient Capture Opportunities

### 1. Selective Tracing

PEP 669's `set_local_events()` allows per-code-object event control. Combined with `DISABLE` return values, we can:

- Trace only user code (skip stdlib, site-packages)
- Trace only specific modules or functions
- Dynamically adjust granularity based on region of interest

### 2. Tiered Recording Granularity

```
Level 0: Call/return only     (PY_START + PY_RETURN)          — ~1.1x overhead
Level 1: Line-level           (+ LINE)                         — ~2-3x overhead
Level 2: Branch-level         (+ BRANCH_LEFT/RIGHT + JUMP)     — ~3-5x overhead  
Level 3: Instruction-level    (+ INSTRUCTION)                   — ~5-15x overhead
```

Users choose the level. Level 1 is the sweet spot for most debugging scenarios.

### 3. Lazy Variable Capture

Don't serialize variables eagerly. Instead:

1. Record a pointer/identity snapshot (fast)
2. Keep objects alive via `Py_INCREF` (or a shadow reference array)
3. Serialize lazily when the trace is read/exported
4. Release references when no longer needed

**Risk:** Keeping references alive changes GC behavior and memory usage. Must be bounded.

### 4. Deferred Serialization to Background Thread

The tracing callback records minimal data (event type, offsets, pointer snapshots). A background thread processes the ring buffer and performs:
- Value serialization
- Source location decoding
- Compression
- Disk I/O

This keeps the traced thread's overhead to pointer writes only.

### 5. Sampling Mode

For very hot code, switch from deterministic to sampling:
- Use `DISABLE` on hot instructions after N captures
- Re-enable periodically via timer or counter
- Provides statistical coverage without full overhead

## Free-Threaded Build Considerations

1. **Per-thread ring buffers** — no cross-thread contention during recording
2. **Atomic write position** — use `_Py_atomic_store_*` for buffer write pointer
3. **Object capture safety** — use `Py_INCREF` under critical section if needed
4. **Merge strategy** — post-process: merge per-thread buffers using timestamps, sort into global timeline

## Estimated Event Volume

For typical Python code:
- ~5-20 LINE events per function call
- ~50-200 INSTRUCTION events per function call
- At 1M function calls/second (fast code): 5-20M line events/second

At 29 bytes/event (no variables): **145-580 MB/second** for line-level tracing.

With variable snapshots (~50 bytes avg): **250MB-1GB/second**.

This is manageable with:
- Memory-mapped ring buffers
- Compression (LZ4 gives ~4:1 on trace data)
- Selective tracing to reduce volume

## Verdict: Extension vs. Fork

**A C extension using PEP 669 is sufficient.** Here's why:

1. PEP 669 provides all the events we need (line, call, return, branch, instruction)
2. C callbacks are fast enough (~50-200ns per event)
3. The public API gives access to code objects and frames
4. Variable access via `localsplus` is technically internal but stable in layout
5. Per-code-object event control and `DISABLE` give us fine-grained overhead control

**We do NOT need to fork CPython.** The one scenario where a fork helps is reducing per-event callback overhead below ~50ns — but this level of optimization is only needed for instruction-level tracing of extremely hot code, which is a niche use case.

### Recommended Implementation Plan

1. **Build a C extension** (`_tracer.c`) that:
   - Registers as PEP 669 tool ID 0 (debugger)
   - Implements LINE + PY_START + PY_RETURN callbacks in C
   - Writes to per-thread ring buffers
   - Provides Python API for start/stop/configure/dump

2. **Build a Python wrapper** (`tracer.py`) that:
   - Manages tracing lifecycle
   - Handles code object caching and metadata
   - Reads and decodes trace buffers
   - Provides timeline reconstruction API

3. **Build a viewer** that:
   - Reads trace files
   - Presents step-through debugging view
   - Maps events to source code using cached location tables

## Key Files Reference

| File | Relevance |
|---|---|
| `Python/instrumentation.c` | How PEP 669 works (model for our callbacks) |
| `Modules/_lsprof.c` | Example of C extension using PEP 669 |
| `Include/cpython/code.h` | Code object structure (what to cache) |
| `Include/internal/pycore_interpframe_structs.h` | Frame structure (where to read locals) |
| `Objects/codeobject.c:1012-1293` | Source location decoding |
| `Include/internal/pycore_instruments.h` | Monitoring data structures |
| `Python/ceval.c` | Eval loop (understanding execution flow) |
