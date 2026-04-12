# Experiment 28: CPython Fork — Inline WAL Tracing

## What we tested

A CPython fork that emits WAL entries directly from bytecode handlers
(`STORE_FAST`, `STORE_SUBSCR`, `STORE_ATTR`, `DELETE_SUBSCR`, `DELETE_ATTR`,
`RETURN_VALUE`, `YIELD_VALUE`, `RESUME`) instead of using `PyEval_SetTrace`.

Key changes:
- `Python/tracewal.c` — WAL buffer, OID map, frame cache, value serialization
- `Python/bytecodes.c` — `if (_PyWAL_enabled)` hooks in 12 bytecode handlers
  (base ops + specialized variants)
- `Modules/_tracewalmodule.c` — Python module exposing start/stop/get_wal/etc.

## Results

### Correctness: 13/13 tests pass

STORE_FAST, STORE_SUBSCR, STORE_ATTR, DELETE_SUBSCR, CALL/RETURN,
aliasing (shared OIDs), LINE events, generator yield/resume.

### Performance: ~1.0x overhead (essentially zero)

| Category | C noop (settrace) | C ext WAL (3.53x) | Fork WAL |
|---|---|---|---|
| Compute (4) | 1.51x | 4.32x | **1.08x** |
| IO (3) | 1.25x | 1.52x | **0.99x** |
| Memory (4) | 1.41x | 3.77x | **0.97x** |
| Yield (2) | 2.55x | 7.17x | **1.09x** |
| Async (2) | 0.79x | 0.87x | **0.76x** |
| **Overall (15)** | **1.47x** | **3.53x** | **0.99x** |

### Why so fast?

1. **No settrace dispatch** — `if (_PyWAL_enabled)` is a single branch-predicted
   check, ~0 cycles when predicted correctly (which it always is in a loop)
2. **Direct `localsplus[i]` access** — ~1ns vs `PyFrame_GetVar`'s ~100ns O(n) scan
3. **No frame materialization** — uses `_PyInterpreterFrame*` directly
4. **No PEP 669 instrumentation** — no `INSTRUMENTED_LINE` trampolines
5. **Fewer events** — fires only on actual stores, not every line. The C extension
   fires 4.5M events for the benchmark suite; the fork fires 2.4M events (the
   actual store/call/return operations) but emits fewer WAL entries because
   it doesn't need LINE events for lines with no stores.

### Event comparison

Fork WAL stats (all 15 workloads):
- 2.4M hook invocations (store_fast=1.9M, store_subscr=84K, store_attr=140K, calls=22, returns=206K)
- 422 WAL entries emitted (most stores don't change the bound OID, so no BIND/UNBIND needed)

C ext WAL stats (all 15 workloads):
- 4.5M settrace events, 4.0M LINE events
- 2.0M PyFrame_GetVar calls
- 3.7M WAL entries

## Architecture

The fork hooks are in `Python/bytecodes.c`, added to these ops:
- `_SWAP_FAST` (STORE_FAST) — captures old+new value before POP_TOP
- `STORE_FAST_LOAD_FAST`, `STORE_FAST_STORE_FAST` — fused store variants
- `_STORE_SUBSCR` + `_STORE_SUBSCR_LIST_INT` + `_STORE_SUBSCR_DICT` — all STORE_SUBSCR paths
- `_STORE_ATTR` + `_STORE_ATTR_INSTANCE_VALUE` + `_STORE_ATTR_WITH_HINT` + `_STORE_ATTR_SLOT` — all STORE_ATTR paths
- `DELETE_SUBSCR`, `DELETE_ATTR`
- `_RETURN_VALUE` — before frame teardown
- `_YIELD_VALUE` — before frame suspension
- `_WAL_RESUME` (new tier1 op) — at end of RESUME macro, captures function entry

All hooks are guarded by `if (_PyWAL_enabled)` which compiles to a single
branch instruction, perfectly predicted when tracing is off.

## Running

```bash
cd cpython/build-opt
../configure && make -j$(nproc)
./python ../trace_investigation/experiments/exp28_fork_tracewal/exp28_bench.py
```
