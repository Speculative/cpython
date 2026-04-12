# Future Work

Items identified during investigation that are not yet implemented.

## Control Flow Events in the WAL

**Current state:** The C extension (`ctrace_wal`) receives LINE, CALL, and RETURN events from `settrace` and does real work on each (reading variables, processing mutations, managing frame cache). However, it does **not** emit explicit control flow markers in the WAL. The WAL only contains state-change entries (CREATE, BIND, MUTATE, SETATTR, etc.).

**What's missing:** To reconstruct a step-through debugging view, we need to know:
- Which lines executed in what order (LINE events)
- Function entry/exit and call stack (CALL/RETURN events)
- Exception flow (EXCEPTION events)

Without these, the WAL is a bag of state changes — you can reconstruct *what* the state was, but not *how* execution got there.

**Performance impact: depends on WAL format.** The C extension is already invoked on every LINE/CALL/RETURN event — that's how settrace works. However, emitting a WAL entry for each event has measurable cost:

- Without flow events: 3.1M WAL entries, **7.7x** overhead
- With flow events (current bloated struct): 7.9M entries, **12.0x** overhead
- With flow events (header-only write, no full memset): **12.0x** (function call + buffer indexing still costs ~50ns per entry)

The overhead increase comes from 4.8M additional `wal_next` calls, not from the data written. With the compact WAL format (flat byte buffer, no per-entry function call overhead), flow events would add ~11 bytes each to a byte stream — genuinely near-zero CPU cost. **The compact WAL format is a prerequisite for cheap flow events.**

**Proposed WAL entry types to add:**

```
LINE     code_idx(2) + line_number(4)                    = ~17 bytes
CALL     code_idx(2) + line_number(4)                    = ~17 bytes
RETURN   code_idx(2) + line_number(4) + retval(variable) = ~19-25 bytes
RAISE    code_idx(2) + line_number(4) + exc_type(2)      = ~19 bytes
```

**Implementation:** Emit these at the top of each event handler in the trace function, before any variable reading or mutation processing. The LINE handler already runs on every line — just add a `wal_next()` write at the beginning.

## ~~Efficient WAL Format~~ DONE

Implemented in `ctrace_wal_compact.c`. Variable-length byte-stream format averaging ~19 bytes/entry (down from ~530). Reduced overhead from 7.7x (struct, no flow events) to 3.7x (compact, WITH flow events + disk persistence). See EXPERIMENTS.md for details.

## ~~Disk Persistence~~ DONE

Implemented in `ctrace_wal_compact.c`. Synchronous flush of 64MB buffer to disk via `write()` syscall when buffer fills. Adds 0-0.6x overhead for typical workloads. Start with `_ctrace_wal.start(output_file="/path/to/trace.wal")`.

## Threading Support

**Current state:** Single-threaded only. `PyEval_SetTrace` sets per-thread.

### Design

**Per-thread WAL buffers:**
- Each thread gets its own WAL buffer, frame cache, and oid map
- No lock contention during recording
- Use `PyEval_SetTraceAllThreads()` (3.12+) to install trace on all threads

**Cross-thread object tracking:**
- Objects can be created in one thread and passed to another
- Option A: Shared oid map with atomic operations (adds contention)
- Option B: Per-thread oid maps, reconcile by cpython_id when merging WALs
- Option B is simpler and avoids hot-path contention

**Event ordering:**
- Per-thread WAL entries are naturally ordered within each thread
- For global ordering: use `clock_gettime(CLOCK_MONOTONIC)` timestamps or a shared atomic sequence counter
- Merge: interleave per-thread WALs by timestamp during analysis

**Fork handling:**
- After `fork()`, child inherits parent's trace state
- Child should reinitialize WAL buffer (start fresh)
- Detect via `os.register_at_fork(after_in_child=reinit_tracer)`

**Free-threaded (no-GIL) builds:**
- All global state needs synchronization or per-thread copies
- Code analysis cache: read-only after registration, safe to share
- Oid map: needs per-thread or atomic operations
- WAL buffer: already per-thread
- Frame cache: already per-thread (indexed by frame pointer)

### Complexity estimate
Medium. The hard part is cross-thread object identity reconciliation and timestamp-based merge.

## Attribute Chain Depth Limit

**Current state:** `chain[4]` in `MutationInfo` limits attribute chains to 4 levels. `a.b.c.d.items.append(1)` has chain `['b', 'c', 'd', 'items']` — exactly 4. Deeper chains (`a.b.c.d.e.f.method()`) would be truncated.

**Options:**
1. Increase to `chain[8]` — handles any practical case, costs 8 more bytes per MutationInfo
2. Dynamic allocation — flexible but more complex
3. Accept the limit — chains deeper than 4 are rare in practice

**Recommendation:** Increase to 8. Negligible memory cost, covers all practical cases.

## Dict Watcher Integration

**Current state:** Mutation detection uses bytecode analysis only. Dict watchers (`PyDict_AddWatcher`) are discussed but not implemented.

**Value:** Dict watchers provide per-key mutation callbacks (ADDED, MODIFIED, DELETED, CLEARED) for free. They would:
- Capture exact key+value for `d.update()`, `d.pop()`, `d.setdefault()` without needing argument resolution
- Detect mutations from C extensions that bypass Python bytecode
- Capture `__dict__` mutations on objects (attribute sets via C code)

**Implementation:**
1. Register a dict watcher callback in the C extension
2. When a local variable is bound to a dict, call `PyDict_Watch(watcher_id, dict)`
3. Watcher callback emits SETITEM/DELITEM WAL entries directly
4. On unbind/dealloc, call `PyDict_Unwatch(watcher_id, dict)`

**Limitation:** Only 5 watcher slots available for extensions (IDs 3-7). Must coordinate if other tools are using watchers.

## Primitive-to-Object Reassignment

**Current state:** The C extension skips oid creation for primitives (int, float, str, bool, None) at runtime. If a variable is reassigned from a primitive to a mutable object, the oid is created on the next assignment. This works correctly because oid assignment is a runtime check.

**Potential issue:** If a primitive variable is passed to a function that stores it in a container, the container's WAL entry would have an inline value for the primitive, not an oid reference. This is correct (primitives are immutable — no need to track them by identity) but means the WAL can't express "this list element is the same int object as variable x." In practice this doesn't matter because ints are immutable.

## WAL Replay / State Reconstruction

**Current state:** Experiment 25 has a basic Python replayer that can reconstruct list/dict/object state from WAL entries. Not integrated with the C extension's WAL format.

**Needed:**
- C extension `get_wal()` returns Python dicts — replayer consumes these
- Full replay with object graph reconstruction
- Time-travel: replay to any WAL sequence number to get state at that point
- UI/viewer integration

## CPython Fork Investigation

**Question:** How much lower could overhead go if we modified CPython itself instead of using the public C API?

**Scope estimate:** ~2-3 days for a working prototype. The bytecode handler modifications are small (1-3 lines per handler). The WAL library is mostly a port from `ctrace_wal.c`. The hardest part is navigating the CALL specialization code paths to find where all method arguments are accessible.

**Architecture:** Unlike the C extension which uses a `Py_tracefunc` callback, the fork would inline WAL emission directly into bytecode handlers in `Python/bytecodes.c`. No callback, no frame materialization, no function pointer dispatch. Shared WAL logic (`wal_emit_*` functions) would live in a new `Python/tracewal.c` and be called from both the bytecode handlers (fork) and the trace callback (C extension).

**What to modify:**
1. `Python/bytecodes.c` — Add `wal_emit_*` calls to ~6 handlers: STORE_FAST, STORE_SUBSCR, STORE_ATTR, DELETE_SUBSCR, DELETE_ATTR, and CALL (for mutating methods). Plus RESUME/RETURN_VALUE/YIELD_VALUE for control flow events. Each is 1-3 lines.
2. New file `Python/tracewal.c` — WAL emission library (~500 lines, ported from `ctrace_wal.c`)
3. `Python/ceval.c` or header — Global `wal_enabled` flag, branch-predicted away when off
4. Python module — Expose start/stop/get_wal/register_code
5. Build system — Add to `Makefile.pre.in`

**Code sharing with C extension:** ~700 of 800 lines in `ctrace_wal.c` are WAL logic (oid map, value serialization, deferred reads, mutation info lookup) that doesn't depend on how we're called. This becomes the shared `tracewal.c`. The C extension wraps it in a `Py_tracefunc`; the fork calls it inline from bytecode handlers.

Our C extension currently hits several bottlenecks that are artifacts of the public API, not fundamental costs. From within CPython, these would be dramatically cheaper:

### `localsplus` Direct Access

**Current cost:** `PyFrame_GetVar(frame, name)` is our #1 expense. It does a name-based string lookup to find the variable index, creates a new reference, and returns a PyObject*. We call it ~2.4M times per workload pass.

**From within CPython:** `frame->localsplus[i]` is a direct array index — a single pointer read (~1ns). We already know `i` from bytecode analysis. This is the single biggest win.

**Estimated savings:** Would reduce our per-variable-read cost from ~100ns to ~1ns. For 2.4M reads, that's ~240ms → ~2.4ms.

### Frame Access Without `PyFrame_GetCode`

**Current cost:** `PyFrame_GetCode(frame)` creates a new reference to the code object (INCREF + return + DECREF by caller). Called on every CALL event and for code hash lookups.

**From within CPython:** `_PyFrame_GetCode(frame)` is an inline function that returns a borrowed reference — no refcount overhead.

### Instruction Pointer / Line Number

**Current cost:** `PyFrame_GetLineNumber(frame)` calls into the line table decoder.

**From within CPython:** `frame->instr_ptr` gives the current instruction pointer directly. Combined with the pre-decoded line table we already build during bytecode analysis, we could do the offset→line lookup ourselves in O(1).

### Eval Stack Access (Mutation Arguments)

**Current cost:** We can't access mutation method arguments (e.g., the `4` in `list.append(4)`) because they're on the eval stack, which is not exposed by any public API. PEP 669's CALL event only provides `arg0` (self).

**From within CPython:** `frame->stackpointer` and `frame->localsplus[co_nlocalsplus..]` give direct access to the eval stack. At the point where `_MONITOR_CALL` fires, `args[0..oparg-1]` contains all call arguments. This would let us capture **every** mutation argument with zero additional API calls.

### Bytecode Instrumentation (Inline Tracing)

**Current cost:** `PyEval_SetTrace` works through the legacy tracing path — PEP 669 INSTRUMENTED_* opcodes fire, which call into `legacy_tracing.c`, which materializes a `PyFrameObject`, which calls our C function. The frame materialization and callback dispatch are unavoidable overhead.

**From within CPython:** We could instrument the eval loop directly in `Python/ceval.c`. Instead of going through the settrace/PEP 669 callback chain, emit WAL entries inline at STORE_FAST, STORE_SUBSCR, STORE_ATTR, and CALL instructions. This eliminates:
- `PyFrameObject` materialization (the frame object is only created lazily; settrace forces it for every call)
- The callback dispatch overhead (~50ns per event)
- The `PyFrame_GetVar` name-based lookup (use `localsplus[i]` directly)

### Estimated Overhead With CPython Fork

| Component | Current (C extension) | With fork |
|---|---|---|
| Trace dispatch (settrace machinery) | ~50-100ns/event | ~5-10ns (inline in eval loop) |
| Variable read (GetVar) | ~100ns/read | ~1ns (localsplus[i]) |
| Line number resolution | ~20ns | ~5ns (pre-decoded table) |
| Oid mapping | ~10ns (hash lookup) | ~10ns (same) |
| WAL entry write | ~20-50ns | ~20-50ns (same) |
| **Total per-event** | **~200-300ns** | **~40-80ns** |

**Estimated overhead with fork: 1.5-2.5x** for typical workloads (vs 7.7x current). The yield worst case would drop from 23x to ~5-8x.

This estimate includes control flow events (LINE/CALL/RETURN) — we know from Experiment 27 that our current overhead already pays the full cost of being invoked on every event. The fork's savings come from eliminating the callback dispatch and `PyFrame_GetVar` overhead, not from tracing fewer events.

### What We'd Need to Modify

| File | Change | Risk |
|---|---|---|
| `Python/ceval.c` | Add WAL emission at STORE_FAST, STORE_SUBSCR, STORE_ATTR, CALL | High — core eval loop, performance-critical |
| `Python/bytecodes.c` | Add instrumentation to bytecode definitions | Medium — generated code |
| `Include/internal/pycore_frame.h` | Expose `localsplus` access helpers | Low — internal header |
| `Python/instrumentation.c` | Possibly extend PEP 669 to pass more CALL args | Medium |

### Recommendation

A CPython fork is the path to production-grade overhead (<2x). The C extension approach (7.7x, improvable to ~4-5x) is sufficient for development-time tracing and prototyping the WAL architecture. The fork should only be pursued once the WAL format, replay, and viewer are mature — there's no point optimizing the capture path until we know the data model is right.

An intermediate option: **propose a CPython patch** that exposes a few key internals:
- A "fast locals enumeration" API that returns `(index, PyObject*)` pairs without name lookup
- Extended PEP 669 CALL callback that passes the full argument array
- These would benefit other tracing tools too and might be accepted upstream

## Shared Library Architecture (C Extension + CPython Fork)

**Goal:** Users can choose between two backends — a pip-installable C extension (easy, ~5-8x overhead) or a CPython fork (fast, ~1.5-2.5x overhead) — with maximum code sharing.

### Proposed structure

```
tracer/
├── python/                          # 100% shared
│   ├── __init__.py                  # Public API: start/stop/configure
│   ├── analyzer.py                  # Bytecode analysis, code registration
│   ├── replay.py                    # WAL replay, state reconstruction
│   ├── viewer.py                    # Step-through debugging UI
│   └── known_mutators.py           # Known mutating method list
│
├── shared_c/                        # ~95% shared C code
│   ├── wal_core.h / wal_core.c     # WAL buffer, entry construction, format
│   ├── oid_map.h / oid_map.c       # Object ID tracking, scope invalidation
│   ├── value_ser.h / value_ser.c   # Value serialization (PyObject → WALValue)
│   ├── mutation_info.h / .c        # Pre-computed mutation info data structures
│   └── frame_cache.h / frame_cache.c  # Per-frame state, deferred reads
│
├── ext_backend/                     # C extension backend (~100 lines unique)
│   ├── ctrace_ext.c                # Py_tracefunc callback, PyFrame_GetVar reads
│   └── setup.py                    # pip-installable: `pip install tracer`
│
└── fork_backend/                    # CPython fork backend (~50 lines unique)
    ├── ceval_patch.diff            # Additions to Python/bytecodes.c
    └── tracewal_module.c          # Python module exposing start/stop/get_wal
```

### What's shared vs unique

| Component | Lines (est.) | Shared? |
|---|---|---|
| Python API, analyzer, replay, viewer | ~2000 | 100% shared |
| WAL core (buffer, format, entry types) | ~300 | 100% shared |
| Oid map | ~150 | 100% shared |
| Value serialization | ~100 | 100% shared |
| Mutation info structures | ~100 | 100% shared |
| Frame cache + deferred reads | ~100 | 100% shared |
| **C extension trace function** | **~100** | **Unique** — uses `PyEval_SetTrace`, `PyFrame_GetVar` |
| **Fork bytecode handler patches** | **~50** | **Unique** — inline calls in `bytecodes.c`, direct `localsplus` access |

**~97% of code is shared.** The backends differ only in how they receive events (callback vs inline) and how they read values (public API vs internal structs).

### Build/distribution

- **C extension:** `pip install our-tracer` — compiles `shared_c/*.c` + `ext_backend/ctrace_ext.c` into a single `.so`. Works with stock Python 3.12+.
- **CPython fork:** Apply `ceval_patch.diff` to CPython source, compile `shared_c/*.c` + `fork_backend/tracewal_module.c` as a built-in module. Users build CPython from source or use our pre-built binaries.
- **Python layer:** Identical. `import tracer` detects which backend is available and uses it.

```python
# User code — identical regardless of backend
import tracer
tracer.start()
my_program()
tracer.stop()
trace = tracer.get_trace()
trace.replay_to(line=42)
```

## Performance Optimization Targets

Current C WAL overhead (compact format, with flow events + disk):

| Category | Current (C ext) | Target (C ext) | Target (fork) |
|---|---|---|---|
| Compute | 2.5-4x | ~2-3x | ~1.5x |
| IO | 1.5-2.5x | ~1.5x | ~1.2x |
| Memory | 1.5-5x | ~2-3x | ~1.5x |
| Yield (synthetic) | 8-11x | ~5-8x | ~3-5x |
| Async | ~1x | ~1x | ~1x |
| **Overall average** | **~3.7x (mem) / ~4x (disk)** | **~2.5-3x** | **~1.5-2x** |

Key optimizations not yet implemented:
1. ~~Compact WAL format~~ **DONE** — reduced from 7.7x to 3.7x
2. Avoid redundant GetVar calls (some mutation targets are read twice — once by pending_mask, once by mutation handler)
3. Direct `localsplus` access (eliminates PyFrame_GetVar overhead entirely — requires CPython fork or internal API patch)
4. Larger buffer or adaptive flush threshold (reduce flush frequency for event-dense workloads)
