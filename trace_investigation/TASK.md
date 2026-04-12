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

We have a working prototype C extension (`ctrace_wal`) that produces a **Write-Ahead Log (WAL)** — an object-centric event log that tracks object lifecycles, variable bindings, and mutations. The WAL architecture has been validated for correctness across Python's dynamic features (recursion, generators, closures, eval/exec, monkey patching, decorators, async, etc.).

### What works
- **Bytecode analysis** identifies all mutation patterns (STORE_FAST, STORE_SUBSCR, STORE_ATTR, known mutating methods like `list.append`) with zero false positives
- **Argument resolution** reads mutation arguments from locals/constants before execution (e.g., `items.append(x)` captures the value of `x`)
- **Attribute chains** work to arbitrary depth (`a.b.c.items.append(1)` correctly targets the nested list)
- **Object identity tracking** via monotonic oid assignment with scope-based invalidation for id() reuse
- **Deferred reads** capture values from BUILD_* stack operations (e.g., `self.items = []`)
- **Aliasing detection** — multiple variables pointing to the same object share an oid
- **Deallocation detection** via weakref for user objects, scope-based inference for built-in types

### Key architectural decisions
- **WAL over snapshots**: Record operations (CREATE, BIND, MUTATE, SETATTR, SETITEM, UNBIND, DEALLOC) rather than full object state at each step. This is compact and enables replay.
- **Pre-computed bytecode analysis**: Python analyzes each code object once, packs mutation info into C-friendly data structures, passes to C extension at registration time. Zero Python involvement at trace time.
- **C extension via `PyEval_SetTrace`**: Registers a C-level `Py_tracefunc` callback — faster than Python settrace callbacks or PEP 669 Python callbacks.
- **Read inputs before execution**: Since LINE events fire before the line runs, we read mutation arguments from locals at that point rather than trying to capture the result state after.

### Performance (compact WAL with control flow + disk persistence)

The WAL uses a compact variable-length byte-stream format (~19 bytes/entry average) with synchronous flush-to-disk when the 64MB buffer fills.

| Category | Overhead (memory) | Overhead (with disk) |
|---|---|---|
| IO-bound / async | ~1-1.5x | ~1-1.5x |
| Compute | ~2-3x | ~2.5-4x |
| Memory-intensive | ~1.5-5x | ~2-7x |
| Yield-heavy (synthetic worst case) | ~8-11x | ~11x |
| **Overall average (15 workloads)** | **~3.7x** | **~4x** |

Disk flush adds 0-0.6x overhead for typical workloads. Workloads generating <64MB of WAL data see zero disk overhead during tracing.

### Known limitations
- Threading not yet supported (single-thread only)
- `list.sort()`, `list.reverse()`, and some set operations can't capture arguments (they're C builtin call args on the eval stack, not accessible via public API)
- id() reuse for built-in types (list, set) relies on scope-based inference rather than deallocation callbacks (weakref not supported on built-in types)
- No WAL replay / state reconstruction viewer yet
- Bytecode analysis for value resolution can be affected by fused LOAD_FAST opcodes in newer CPython versions (handled for 3.15, may need updates for future versions)

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

### C extensions (in `experiments/`)

| Directory | Extension | Purpose |
|---|---|---|
| `exp3_c_extension/` | `_tracebuf` | PEP 669 C callbacks with ring buffer (dead end — PEP 669 dispatch is the bottleneck) |
| `exp7_c_extension/` | `_ctrace` | Multiple settrace modes: noop, count, GetVar, GetLocals, full capture |
| `exp10_c_extension/` | `_ctrace2` | Selective capture with pre-computed bytecode bitmasks — the variable-capture approach |
| `exp27_c_extension/` | `_ctrace_wal` | Full WAL extension: compact byte-stream format, mutation tracking, oid management, argument resolution, control flow events, disk persistence. Two source files: `ctrace_wal.c` (original struct-based, kept for reference) and `ctrace_wal_compact.c` (current, builds as `_ctrace_wal.so`) |

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
