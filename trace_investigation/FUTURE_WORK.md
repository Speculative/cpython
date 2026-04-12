# Future Work

Items identified during investigation that are not yet implemented.

## ~~Control Flow Events in the WAL~~ DONE

Implemented in both backends. The C extension (`ctrace_wal_compact.c`) emits LINE/CALL/RETURN/EXCEPTION on every settrace event. The fork (`tracewal.c`) emits CALL/RETURN at RESUME/_RETURN_VALUE, LINE at control flow points (mode 1) or every instruction (mode 2), RAISE with origin line at the error label and RAISE_VARARGS/RERAISE, and EXCEPT at PUSH_EXC_INFO. See Experiment 28 for full details.

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

## Attribute Chain Depth Limit (C extension only)

**Current state:** `chain[4]` in the C extension's `MutationInfo` limits attribute chains to 4 levels. Deeper chains would be truncated. The fork doesn't have this limitation — it hooks `STORE_ATTR` directly on whatever object is on the eval stack, regardless of chain depth.

**Recommendation:** Only relevant if the C extension path continues to be developed. Increase to 8 if needed.

## Dict Watcher Integration

**Current state:** Both backends detect dict mutations via Python bytecode hooks (`STORE_SUBSCR` for `d[key] = val`, CALL hook for `d.update()`/`d.pop()`/etc.). However, neither backend can detect mutations made directly from C code via `PyDict_SetItem` — these bypass Python bytecodes entirely.

**Value:** `PyDict_AddWatcher` (CPython 3.12+) provides per-key mutation callbacks (ADDED, MODIFIED, DELETED, CLEARED) from the C level. This would:
- Catch mutations from C extensions that bypass Python bytecode
- Capture `__dict__` mutations on objects (attribute sets via C code, e.g., `ctypes`, `Cython`)
- Provide redundant but authoritative confirmation of mutations we already capture via bytecode hooks

**Applicable to:** Both backends. The watcher callback would emit WAL entries directly, same as any other hook.

**Implementation:**
1. Register a dict watcher callback in the WAL library
2. When a tracked object's `__dict__` or a local dict variable is encountered, call `PyDict_Watch(watcher_id, dict)`
3. Watcher callback emits SETITEM/DELITEM WAL entries directly
4. On unbind/dealloc, call `PyDict_Unwatch(watcher_id, dict)`

**Limitation:** Only 5 watcher slots available for extensions (IDs 3-7). Must coordinate if other tools are using watchers.

**Priority:** Low for typical Python code. Higher if tracing code that uses C extensions heavily (numpy, pandas, etc.).

## Primitive-to-Object Reassignment (design note, not a bug)

Both backends skip OID creation for primitives (int, float, str, bool, None) and serialize them inline. If a primitive is stored in a container, the WAL has an inline value rather than an OID reference. This means the WAL can't express "this list element is the same int object as variable x" — but since primitives are immutable, identity tracking has no practical value. Reassignment from primitive to mutable object works correctly in both backends because OID assignment is a runtime check.

## WAL Replay / State Reconstruction

**Current state:** Experiment 25 has a basic Python replayer that can reconstruct list/dict/object state from WAL entries. Not integrated with the C extension or fork WAL formats. The fork's 124-test conformance suite validates that the event model captures enough information for full reconstruction.

**WAL event model (validated by conformance tests):**
- CREATE(oid, type) — object born
- BIND(oid, name, [value]) / UNBIND(oid, name) — variable→object binding
- SETATTR(oid, attr, value) / DELATTR(oid, attr) — attribute mutations
- SETITEM(oid, key, value) / DELITEM(oid, key) — container mutations
- MUTATE(oid, method, args) — method call with arguments
- SNAPSHOT(oid, items) — full container contents after opaque C mutation (list sort/reverse, set pop, deque reverse/rotate)
- CALL(line, code_idx) / RETURN(line, code_idx, retval) — function boundaries
- RAISE(line, code_idx, exc_type, exc_msg) / EXCEPT(line, code_idx, exc_type) — exception flow
- LINE(line, code_idx) — source line (modes 1 and 2)

**Object references:** Values in WAL entries are either inline primitives (int, float, str, bool, None — tag 0-4) or OID references (tag 5, `{ref: oid}`). This means object graphs are fully representable — a list containing a dict would have SETITEM entries where the value is `{ref: dict_oid}`.

**Needed:**
- Both backends' `get_wal()` return Python dicts — replayer consumes these
- Full replay engine: given a WAL and a sequence number, reconstruct all object states at that point
- Time-travel: efficiently seek to any WAL position
- Handle SNAPSHOT events: replace reconstructed state with snapshot contents
- UI/viewer integration

## ~~CPython Fork Investigation~~ DONE

Implemented in Experiment 28. The fork modifies `Python/bytecodes.c` to add `if (_PyWAL_enabled) { _PyWAL_On*(); }` hooks to 18 bytecode handlers. WAL library in `Python/tracewal.c` (~1000 lines), Python module in `Modules/_tracewalmodule.c`.

**Measured overhead: ~1.3x** (mode 0, stores + calls), **~1.8x** (mode 1, + control flow). 23 workloads, vs predicted 1.5-2.5x. Full details in EXPERIMENTS.md Experiment 28.

Key findings vs pre-investigation predictions:
- `localsplus[i]` direct access confirmed ~1ns vs ~100ns for PyFrame_GetVar — biggest win.
- Eval stack access for mutation arguments (e.g., `list.append(4)`) works — `_PyStackRef *args` in `_DO_CALL` gives all call arguments.
- RESUME specialization to RESUME_CHECK was an unexpected hurdle — hooks must be added to all RESUME variants or hot function calls are silently untraced.
- `PyCode_Addr2Line` per instruction is too expensive for full-LINE mode (~21x) — needs caching.
- Three LINE tracking modes (stores-only / control-flow / full) provide a clean performance-fidelity tradeoff.

The intermediate option (propose CPython upstream API patches) is still viable for improving the C extension path without requiring a fork.

## Shared Library Architecture (C Extension + CPython Fork)

**Goal:** Users can choose between two backends — a pip-installable C extension (easy, ~3.9x overhead) or a CPython fork (fast, ~1.3-1.8x overhead).

### What we actually built

The two backends share the same WAL byte-stream format and Python-level API, but share less C code than originally predicted. The fork's architecture is fundamentally different — it hooks bytecodes directly and doesn't need the C extension's bytecode pre-analysis, deferred reads, or pending_mask machinery.

| Component | C extension (`ctrace_wal_compact.c`) | Fork (`tracewal.c`) |
|---|---|---|
| Event source | `PyEval_SetTrace` callback | `if (_PyWAL_enabled)` in bytecode handlers |
| Variable reads | `PyFrame_GetVar(frame, name)` | `frame->localsplus[i]` |
| Mutation detection | Pre-computed bytecode analysis (Python-side) | Direct CALL hook + method name check |
| Deferred reads | Yes (pending_mask, next LINE event) | Not needed (see stores directly) |
| Exception tracking | `PyTrace_EXCEPTION` callback | Error label hook + `PUSH_EXC_INFO` hook |
| Post-mutation snapshots | Not implemented | SNAPSHOT after sort/reverse/set.pop/deque ops |
| Global/nonlocal | Not implemented | `STORE_GLOBAL` + `STORE_DEREF` hooks |
| WAL format | Compact byte-stream, ~19 bytes/entry | Same format, same decoder |
| Lines of C | ~1300 | ~1100 |

### What IS shared

- **WAL byte-stream format** — identical entry headers, value encoding, event types
- **WAL decoder** (`get_wal()` → list of Python dicts) — same logic in both
- **Python API** — `start()/stop()/stats()/get_wal()/clear()` — same interface
- **Replay engine** (future) — consumes the same WAL format from either backend

### Build/distribution

- **C extension:** Build as `.so` with `gcc`. Works with stock Python 3.12+. No CPython source modifications needed.
- **CPython fork:** Modify `bytecodes.c` + add `tracewal.c` + `_tracewalmodule.c`. Build CPython from source.
- **Python layer:** `import _tracewal` (fork) or `import _ctrace_wal` (C extension). A wrapper module could detect which is available.

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

Current overhead (23 workloads, 10 rounds, apples-to-apples):

| Config | LINE tracking | Overall | Description |
|---|---|---|---|
| C noop (settrace floor) | — | 1.6x | Just receiving settrace callbacks, no WAL |
| C ext WAL (stores only) | none | 3.9x | WAL capture via settrace, skip LINE emission |
| C ext WAL (+LINE) | every line | 3.9x | WAL + LINE events (original mode) |
| Fork mode 0 | none | **1.3x** | Stores + calls + mutations + exceptions |
| Fork mode 1 | control flow | **1.8x** | + branches, loops, jumps |
| Fork mode 2 | every line | ~21x | Per-instruction `PyCode_Addr2Line` — needs caching |

Note: skipping LINE emission in the C extension saves essentially nothing (3.9x either way) — the bottleneck is settrace dispatch + PyFrame_GetVar, not WAL writes. Fork mode 2 is worse than the C extension because it polls `PyCode_Addr2Line` per instruction instead of per line change.

Completed optimizations:
1. ~~Compact WAL format~~ **DONE** — reduced from 7.7x to 3.7x (C extension)
2. ~~Direct `localsplus` access~~ **DONE** — fork mode 0 at 1.3x (vs C ext 3.8x)
3. ~~Control-flow-only LINE tracking~~ **DONE** — fork mode 1 at 1.8x with full branch reconstruction
4. ~~Exception tracing~~ **DONE** — RAISE with origin line, EXCEPT for handler entry
5. ~~Global/nonlocal/closure variables~~ **DONE** — STORE_GLOBAL, STORE_DEREF hooks
6. ~~Post-mutation snapshots~~ **DONE** — SNAPSHOT after opaque C mutations (list sort/reverse, set pop, deque reverse/rotate). Reconstructable mutations (list/dict/deque pop, etc.) correctly excluded.

Key optimizations not yet implemented:
1. **`PyCode_Addr2Line` caching / per-line dispatch hook** — fork mode 2 is ~21x (worse than the C extension's ~4x) because `_PyWAL_CheckLine` in the DISPATCH macro calls `PyCode_Addr2Line` on every bytecode instruction. Since there are ~10-50 instructions per source line, this is 10-50x more calls than settrace's line-change callbacks. Two fix options: (a) pre-build an offset→line lookup table per code object so the check is a single array dereference, or (b) hook `INSTRUMENTED_LINE` (which CPython already fires once per line change) instead of DISPATCH, getting the same granularity as settrace without the callback overhead.
2. **Selective function tracing** — currently the fork traces ALL Python function calls including stdlib internals. Adding a filter (e.g., only trace user code, or code registered by the analyzer) would reduce overhead for workloads that call many stdlib functions.
3. **C extension: propose upstream API patches** — a "fast locals enumeration" API returning `(index, PyObject*)` pairs without name lookup would be the biggest C extension win
4. **Larger buffer or adaptive flush threshold** — reduce flush frequency for event-dense workloads
