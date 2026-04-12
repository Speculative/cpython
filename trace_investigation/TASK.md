# Full Execution Tracing for CPython

## Goal

Build a tracer that captures enough information during Python program execution to reconstruct the complete view a programmer would have if they were stepping through every line of code in a debugger — seeing every variable's value at every step, every function call and return, every mutation to every object.

The tracer should:
- Capture complete execution state (control flow + variable values + object mutations)
- Have low enough overhead to be usable during development (~2-5x target)
- Be distributable as a `pip install` package (no custom Python build required)
- Optionally support a CPython fork path for lower overhead (~1.5-2.5x)
- Work with Python 3.12+

## Current Status

Two working tracer backends producing a **Write-Ahead Log (WAL)** — an object-centric event log that tracks object lifecycles, variable bindings, mutations, control flow, and exceptions. Both are validated by a 112-test conformance suite.

### Backend 1: C Extension (`_ctrace_wal`) — pip-installable

Uses `PyEval_SetTrace` with a C-level `Py_tracefunc` callback. Reads variables via `PyFrame_GetVar`. Requires pre-computed bytecode analysis (Python-side) to register code objects.

**Overhead: ~4x** (23 workloads, compact WAL + disk persistence)

### Backend 2: CPython Fork (`_tracewal`) — inline eval loop hooks

Modifies `Python/bytecodes.c` directly, adding `if (_PyWAL_enabled) { ... }` hooks to 18 bytecode handlers. Uses `frame->localsplus[i]` for direct local access, `_PyFrame_GetCode()` for borrowed refs. No settrace, no frame materialization, no bytecode pre-analysis needed.

**Overhead: ~1.8x** (mode 0, stores + calls only, 23 workloads)

### What both backends capture
- **Variable bindings** (BIND/UNBIND) — every STORE_FAST, including function arguments at call time
- **Object mutations** — SETATTR (attribute set), SETITEM (container item set), DELATTR, DELITEM
- **Mutating method calls** — MUTATE for `append`, `insert`, `sort`, `pop`, `update`, etc. with captured arguments
- **Control flow** — CALL/RETURN for every Python function, LINE events (three modes in fork)
- **Exception flow** — RAISE with origin line + type + message, EXCEPT for handler entry
- **Object identity** — monotonic OID assignment, aliasing detection (shared OIDs)
- **Generator/async** — yield/resume tracked as RETURN/CALL events
- **Global variables** — STORE_GLOBAL captured as SETATTR on globals dict
- **Nonlocal/closure variables** — STORE_DEREF captured as BIND on cell variables
- **Post-mutation snapshots** — full container contents after opaque C mutations: `list.sort()`, `list.reverse()`, `set.pop()`, `deque.reverse()`, `deque.rotate()`. Not triggered for reconstructable mutations like `list.pop()`, `dict.pop()`. (fork only)

### Fork LINE tracking modes
- **Mode 0 (stores only, ~1.8x)**: Events at STORE/CALL/RETURN/RAISE/EXCEPT. Straight-line code between stores is inferrable from source.
- **Mode 1 (control flow, ~3.7x)**: Adds LINE at branch destinations (`POP_JUMP_IF_*`), loop headers (`FOR_ITER`), and jump targets. Sufficient to reconstruct which branch was taken, how many loop iterations ran, and where exceptions jumped to.
- **Mode 2 (full LINE, ~21x)**: LINE on every source line change. Currently expensive due to `PyCode_Addr2Line` being called per-instruction — needs line table caching optimization.

### Performance (23 workloads, 6 categories)

| Category | C noop | C ext (stores) | C ext (+LINE) | Fork mode 0 | Fork mode 1 |
|---|---|---|---|---|---|
| Compute (4) | 1.5x | 4.5x | 4.7x | 1.2x | 1.6x |
| IO (3) | 1.3x | 1.6x | 1.6x | 1.2x | 1.3x |
| Memory (4) | 1.4x | 3.8x | 3.9x | 1.2x | 1.7x |
| Yield (2) | 2.4x | 6.6x | 6.4x | 2.2x | 3.9x |
| Async (2) | 0.7x | 0.7x | 0.6x | 0.5x | 0.6x |
| Pattern (8) | 1.7x | 4.6x | 4.7x | 1.5x | 1.8x |
| **ALL (23)** | **1.6x** | **~3.9x** | **~3.9x** | **~1.3x** | **~1.8x** |

Key insights:
- **Skipping LINE emission saves nothing for the C extension** (3.9x → 3.9x). The bottleneck is settrace dispatch + PyFrame_GetVar on every LINE event, not the WAL entry write. The C extension pays the full settrace callback overhead even when it doesn't emit LINE entries.
- **Fork mode 0 at 1.3x is below the settrace noop floor (1.6x).** The WAL logic itself is genuinely cheap — the fork's overhead is entirely from `get_current_line()` calls at each store hook.
- **Fork mode 1 at 1.8x adds only +0.5x over stores-only** for full control flow reconstruction (branches, loops, exception jumps). This is the recommended mode for debugger-style replay.
- **Fork mode 2 (per-instruction LINE) is ~21x** — much worse than the C extension's ~4x because the fork polls `PyCode_Addr2Line` on every bytecode instruction (many per source line), while settrace only fires once per source line change. Needs line table caching to be practical.

### Known limitations
- Threading not yet supported (single-thread only)
- id() reuse for built-in types relies on scope-based inference (weakref not supported on built-in types)
- No WAL replay / state reconstruction viewer yet
- Mode 2 LINE tracking needs `PyCode_Addr2Line` caching to be practical
- C extensions modifying objects outside Python bytecode are invisible (except known mutating methods)
- Bytecode analysis for value resolution (C extension only) can be affected by fused LOAD_FAST opcodes in newer CPython versions (handled for 3.15)

### Test suites

| Suite | Location | Tests | What it covers |
|---|---|---|---|
| Conformance | `exp28_fork_tracewal/tests/test_wal_conformance.py` | 124 | All WAL event types, control flow, exceptions, closures, generators, snapshots (list/set/deque), LINE modes |
| Performance | `exp28_fork_tracewal/tests/bench_performance.py` | 23 workloads | Overhead measurement across compute/IO/memory/yield/async/pattern categories |
| C ext correctness | `exp27_c_extension/exp27_wal_c.py` | 16 | Original C extension tests (subset of conformance suite) |

## Dead Ends and Lessons Learned

These findings may save time for future investigation:

- **PEP 669 callbacks can't access the frame.** They receive `(code, line_number)` but not the frame object. This blocks variable capture. Use settrace instead.
- **C extension PEP 669 callbacks are NOT faster than Python PEP 669 callbacks.** The bottleneck is PEP 669's instrumentation dispatch, not the callback itself (Exp 3).
- **C settrace IS faster than PEP 669 for tracing.** A C `Py_tracefunc` registered via `PyEval_SetTrace` has lower overhead than PEP 669 LINE events with Python callbacks, AND gives us the frame.
- **`PyFrame_GetLocals()` is better than per-variable `PyFrame_GetVar()` at scale** for reading all locals. But selective reading (only changed vars) via GetVar is better when bytecode analysis tells us which vars changed.
- **Python-level selective capture is slower than C-level read-everything.** The overhead of Python dict lookups for the selection logic exceeds the savings from reading fewer variables (Exp 9). The selection must be done in C.
- **`frame.f_locals[varname]` triggers a full locals snapshot** every time it's accessed in Python — there's no way to read a single variable cheaply from Python.
- **LINE events fire BEFORE the line executes.** Variables written by line N are only visible at line N+1. The C extension uses a "deferred read" pattern: save the current line's write mask, read those variables on the next LINE event.
- **Function parameters aren't set by STORE_FAST.** They're set by the CALL machinery before the function body begins. Must capture them on PyTrace_CALL, not via bytecode analysis.
- **Background writer threads are counterproductive in Python.** The GIL + `queue.Queue` overhead (~400ns/event) exceeds the tracing cost itself. Synchronous batched writes are faster (Exp 12).
- **Serialization in Python (~241ns/event) is 3x the C capture cost.** Moving serialization to C is important for disk persistence.
- **Full mutation analysis adds negligible overhead** (+0.07x) over STORE_FAST-only analysis. The extra GetVar calls for mutation-flagged lines are cheap because pointer comparison short-circuits (Exp 22). Enable it by default.
- **Fused LOAD_FAST opcodes** (e.g., `LOAD_FAST_BORROW_LOAD_FAST_BORROW`) are a CPython 3.13+ optimization. They load two locals in one instruction with a tuple argval. Bytecode analysis must handle them or value resolution breaks.
- **Fixed-size WAL structs are catastrophically slow.** The original WAL used ~530 byte structs per entry, requiring memset on every write. Adding control flow events (LINE/CALL/RETURN) at ~4.8M events/pass made overhead jump from 7.7x to 12x just from memset. Switching to a compact byte-stream format (~19 bytes/entry) with no memset brought it down to 3.7x — a 3.2x improvement.
- **Nested wal_reserve calls corrupt the buffer.** When value serialization (`wal_write_value_from_obj`) encounters a complex object, it calls `oid_get_or_create` which emits a CREATE entry — nesting one WAL write inside another. Fix: pre-create oids for complex values before starting the WAL entry, or use `oid_create` (no WAL emission) instead of `oid_get_or_create` inside write sequences.
- **Disk flush cost is negligible for most workloads.** Synchronous 64MB buffer flush to disk adds 0-0.6x overhead. Only event-dense workloads (>64MB of WAL) see meaningful disk cost. No background thread needed.
- **Yield overhead is inherent, not a bug.** Generator yields produce 3 trace events (CALL + LINE + RETURN) per element per pipeline stage. Generators that do ~4ns of work per yield pay ~200ns per trace event — 50x ratio. Real programs with meaningful work per yield see 2-3x, not 50x.
- **Inline bytecode hooks achieve ~1.3x overhead (mode 0, stores + calls).** A CPython fork adding `if (_PyWAL_enabled)` checks to 18 bytecode handlers captures full variable state, object mutations, control flow, exceptions, and closure variables at 1.3x overhead (23 workloads). Early measurements showed ~1.0x but were incorrect — RESUME specialization to RESUME_CHECK meant most function calls were silently untraced. After fixing RESUME_CHECK, the correct overhead is 1.3x — below the settrace noop floor (1.6x). Adding control-flow LINE tracking (mode 1) brings it to 1.8x.
- **RESUME gets specialized to RESUME_CHECK after the first call.** The `_QUICKEN_RESUME` op in the RESUME macro rewrites RESUME → RESUME_CHECK after the first execution. RESUME_CHECK skips the instrumentation version check. Any hook added to the RESUME macro must also be added to RESUME_CHECK, RESUME_CHECK_JIT, and INSTRUMENTED_RESUME, or subsequent calls to hot functions will be silently untraced.
- **`PyCode_Addr2Line` is too expensive for per-instruction LINE tracking.** Adding a `_PyWAL_CheckLine` call (which invokes `PyCode_Addr2Line`) to the DISPATCH macro results in ~21x overhead. The line table decoder is designed for occasional use (tracebacks), not every-instruction use. For practical full-LINE mode, need to cache the offset→line mapping per code object or use a precomputed table.
- **Control-flow-only LINE tracking (mode 1) gives full branch reconstruction at ~3.7x.** Hooking only `POP_JUMP_IF_TRUE/FALSE`, `FOR_ITER`, `JUMP_FORWARD`, and `JUMP_BACKWARD_NO_INTERRUPT` — after the jump resolves — captures which branch was taken, which loop iterations ran, and where exceptions jumped to. Combined with BIND events (which carry line numbers), a replayer can infer straight-line execution between control flow points.
- **Branch LINE events must fire AFTER JUMPBY, not before.** The `POP_JUMP_IF_*` ops modify `next_instr` via `JUMPBY`, but `frame->instr_ptr` is set at the handler's top to the pre-jump value. To emit the destination line (which branch was taken), set `frame->instr_ptr = next_instr` after JUMPBY before calling `_PyWAL_CheckLine`.
- **CPython's bytecodes.c DSL accepts side-effectful calls in op bodies.** Adding `if (...) { func(); }` inside `op()` and `inst()` bodies works — the code generator passes them through as raw C, similar to `STAT_INC()` and `LLTRACE_RESUME_FRAME()`. The `replicate(8)` annotation correctly propagates the hook to all specialized variants. All 7 generators (`make regen-cases`) accept the modified bytecodes.c without errors.
- **Specialized STORE_ATTR/STORE_SUBSCR variants bypass base ops.** `STORE_ATTR_INSTANCE_VALUE`, `STORE_ATTR_WITH_HINT`, `STORE_ATTR_SLOT`, `STORE_SUBSCR_LIST_INT`, `STORE_SUBSCR_DICT` each have their own op body and skip `_STORE_ATTR`/`_STORE_SUBSCR`. Hooks must be added to each independently.
- **Exception origin line can be captured at the `error:` label in the eval loop.** At the `error:` label (before `exception_unwind`), `next_instr-1` points to the instruction that raised, and `PyTraceBack_Here` has just been called. Reading the exception from tstate and computing the line from `next_instr-1` gives the exact origin line for both Python-level and C-level exceptions.
- **Post-call snapshots for opaque C mutations work via a pending queue.** For methods where we can't reliably infer the result from inputs (`list.sort`, `list.reverse`, `set.pop`, `deque.reverse`, `deque.rotate`), the CALL hook registers a pending snapshot, and after `_DO_CALL` returns, the snapshot is flushed by serializing the full container contents. Snapshots are NOT triggered for reconstructable mutations like `list.pop`, `dict.pop`, `dict.popitem`, `deque.pop/popleft` — these can be replayed from the method name + args + the replayer's current state. The key distinction: `set.pop()` needs a snapshot because CPython's set iteration order is hash-based and implementation-defined, so the replayer can't predict which element is removed.

## Documentation Structure

### Reference documents (in `trace_investigation/`)

| File | Contents |
|---|---|
| `TASK.md` | This file — project goals, status, and orientation |
| `README.md` | Index of explainer documents with early findings summary (partially outdated by experiments) |
| `EXPERIMENTS.md` | Detailed results from all experiments — the primary record of what we tested and found |
| `FUTURE_WORK.md` | Unimplemented items: threading, CPython fork, shared library architecture, WAL replay/viewer |
| `01_execution_pipeline.md` | How Python source → bytecode → execution works |
| `02_object_storage.md` | How CPython stores objects, types, frames in memory |
| `03_bytecode_source_mapping.md` | How bytecode maps back to source lines and columns |
| `04_threading_gil.md` | CPython threading model and free-threaded build |
| `05_tracing_infrastructure.md` | Existing tracing hooks: settrace, PEP 669, DTrace, cProfile |
| `06_trace_capture_strategy.md` | Early strategy document (some recommendations superseded by experiments) |

### Experiments (in `trace_investigation/experiments/`)

| Experiment | What it tested | Key takeaway |
|---|---|---|
| **Exp 1**: PEP 669 overhead | Event granularity vs overhead | LINE events ~1.5-4x, INSTRUCTION ~3-15x |
| **Exp 2**: Variable capture | How to read locals from frames | PEP 669 can't access frames; settrace is 7-28x with f_locals |
| **Exp 3**: C ring buffer via PEP 669 | C callbacks vs Python callbacks | C is NOT faster — PEP 669 dispatch is the bottleneck |
| **Exp 4**: Serialization formats | Cost of different value serialization strategies | All sub-microsecond; change detection is cheapest |
| **Exp 5**: End-to-end Python prototype | Full tracer in pure Python | Works correctly at 16-62x overhead |
| **Exp 6**: PEP 669 reconstruction | Can we reconstruct without frame access? | Structural trace possible; no variable values without frames |
| **Exp 7**: C settrace (`PyEval_SetTrace`) | C trace function performance | C noop ~1.5x, C+GetVar ~2-6x — faster than PEP 669 |
| **Exp 8**: Large realistic workloads | Performance at scale (compute/IO/memory/async/yield) | C noop 1.5x avg, C GetLocals 3.25x avg |
| **Exp 9**: Selective capture in Python | Read only changed variables from Python | Slower than reading everything — Python overhead dominates |
| **Exp 10**: C selective capture | Pre-computed bytecode maps in C | 2.74x avg — the sweet spot for variable capture |
| **Exp 11**: Steady-state measurement | Proper warmup + multiple rounds | Confirmed previous numbers are steady-state, not skewed by one-time costs |
| **Exp 12**: Serialization + disk I/O | Can we write to disk fast enough? | Sync batched writes at 4M events/sec; disk is not the bottleneck |
| **Exp 13**: Dynamic Python correctness | Recursion, eval, generators, closures, monkey patching, etc. | 60/60 passed — settrace handles all dynamic features correctly |
| **Exp 14**: Execution order | Line order for tricky control flow | 35/35 passed — exact execution order captured including exceptions, generators, break/continue |
| **Exp 15**: C extension correctness | Tests through actual C extension | 33/33 passed; found and fixed deferred-read timing bug and parameter capture |
| **Exp 16**: Demo output | Human-readable trace with source mapping | Shows what reconstructed step-through view looks like |
| **Exp 17**: Stress test (Python) | Long-running with disk persistence | ~820K events/sec, 6.2x overhead, ~45 GB/hour at 16 bytes/event |
| **Exp 18**: Stress test (C extension) | C extension at sustained load | 2.0x selective, 61M events/sec capture throughput |
| **Exp 19**: Mutation capture gaps | What pointer comparison misses | In-place mutations (append, sort, attr set) invisible to pointer comparison |
| **Exp 20**: Mutation detection (Python) | Bytecode analysis for all mutation patterns | 18/18 — all built-in type mutations detected, zero false positives |
| **Exp 21**: Mutation detection (C) | Same tests through C extension | 24/24 passed |
| **Exp 22**: Mutation detection performance | Cost of expanded mutation analysis | +0.07x over STORE_FAST-only — essentially free |
| **Exp 23**: State reconstruction analysis | WAL replay, object destruction, id reuse, aliasing | WAL approach validated; id reuse and BUILD_* values need special handling |
| **Exp 24**: Argument static analysis | Can we resolve mutation args from bytecode? | 67% fully resolvable from constants + locals; rest reconstructable from inputs |
| **Exp 25**: WAL prototype (Python) | Full WAL with CREATE/BIND/MUTATE/SETATTR/UNBIND/DEALLOC | 26/28 passed; validated WAL architecture |
| **Exp 26**: WAL edge cases | Deferred reads, id reuse, globals, nonlocals, attr chains | 22/22 passed after fixes |
| **Exp 27**: WAL C extension | Full WAL in C, compact format, control flow, disk | 16/16 correctness; **3.7x** memory, **~4x** with disk persistence |
| **Exp 28**: CPython fork WAL | Inline WAL hooks in bytecode handlers, 3 LINE modes, exceptions, globals/nonlocals, snapshots | 112/112 conformance; **~1.8x** (mode 0), **~3.7x** (mode 1), **~21x** (mode 2) |

### C extensions (in `experiments/`)

| Directory | Extension | Purpose |
|---|---|---|
| `exp3_c_extension/` | `_tracebuf` | PEP 669 C callbacks with ring buffer (dead end — PEP 669 dispatch is the bottleneck) |
| `exp7_c_extension/` | `_ctrace` | Multiple settrace modes: noop, count, GetVar, GetLocals, full capture |
| `exp10_c_extension/` | `_ctrace2` | Selective capture with pre-computed bytecode bitmasks — the variable-capture approach |
| `exp27_c_extension/` | `_ctrace_wal` | Full WAL extension: compact byte-stream format, mutation tracking, oid management, argument resolution, control flow events, disk persistence. Two source files: `ctrace_wal.c` (original struct-based, kept for reference) and `ctrace_wal_compact.c` (current, builds as `_ctrace_wal.so`) |

### CPython fork files (in cpython source tree)

| File | Purpose |
|---|---|
| `Include/internal/pycore_tracewal.h` | Header: `_PyWAL_enabled` flag, `_PyWAL_line_mode`, hook function declarations |
| `Python/tracewal.c` | WAL core: buffer, OID map, frame cache, value serialization, hook implementations (~1000 lines) |
| `Python/bytecodes.c` | Modified: `if (_PyWAL_enabled)` hooks in 18 bytecode handlers |
| `Python/ceval_macros.h` | Modified: `_WAL_LINE_CHECK()` macro in DISPATCH for mode 2 |
| `Modules/_tracewalmodule.c` | Python module: start/stop/stats/get_wal/clear/register_code |
| `Makefile.pre.in` | Modified: `Python/tracewal.o` added to PYTHON_OBJS |
| `Modules/Setup.bootstrap.in` | Modified: `_tracewal _tracewalmodule.c` added as built-in module |

### Build

The experiments use an optimized CPython build (PGO + LTO) at `build-opt/`:
```
cd cpython && mkdir build-opt && cd build-opt
../configure --enable-optimizations --with-lto
make -j$(nproc)
```

C extensions are compiled directly:
```
cd experiments/exp27_c_extension
gcc -O2 -shared -fPIC -I../../Include -I../../build-opt -o _ctrace_wal.cpython-315-x86_64-linux-gnu.so ctrace_wal.c
```

Run experiments from the experiments directory with PYTHONPATH set:
```
cd experiments
PYTHONPATH=exp7_c_extension:exp10_c_extension:exp27_c_extension ../../build-opt/python exp27_c_extension/exp27_wal_c.py
```
