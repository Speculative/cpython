# Experiment 29: Proposed Performance Optimizations (Notes)

Status: design notes. None implemented yet.

After the autopsy-report consumer landed several correctness fixes
on top of exp28 (OID-refresh + snapshot_fresh + type-name fidelity +
WAL_OBJ_SNAPSHOT for user objects), aggregate tracing overhead grew
from the original ~1.0x baseline to ~3.0x. This file enumerates the
big-ticket fork changes that could reclaim — or in some cases push
below — the original 1x.

The risk assessments are first-pass — implementation-dependent and
should be re-checked when actually doing the work.

## At a glance

| # | Optimization | Aggregate win | Code | Invasiveness | Crash risk |
|---|---|---|---|---|---|
| 1 | User-code-only filter | ~5-10x WAL volume | ~30-80 LoC | Touches every hook + per-oid tagging | Low-Medium |
| 2 | Resizable oid_map | Removes a sharp cliff | ~40 LoC | Hash table refactor | Low |
| 3 | Compact event encoding | ~2-3x smaller WAL | ~100 LoC | Touches every emit + every parser | Medium |
| 4 | Streaming compression | ~3-5x smaller WAL | ~50 LoC | flush path + reader | Medium-Low |
| 5 | Skip BIND for unchanged primitives | Modest | ~20 LoC | One hook | Very low |
| 6 | tp_dealloc hooks | ~2x speedup | ~80-150 LoC | Patches CPython type slots | High |

---

## 1. User-code-only filtering at capture time

**Idea.** Tag each registered code object with an `is_user` bool. In
each event-emitting hook, early-return when the current frame's code
isn't user code. Today the loader's `_classify` does this filter at
load time — pushing it to capture time eliminates the wal_reserve /
serialize / write cost for skipped events.

**Why it's worth it.** On a real capture (`order_pipeline.atrace` —
70 lines of user code), 99.7% of WAL events are stdlib churn that we
read, parse, classify, and discard. Skipping them at the source is
the single largest WAL volume reduction available.

**Catch — this is more nuanced than it looks.** Events that affect
*objects* must still fire even from stdlib code, otherwise mutations
done by stdlib on user-visible objects (e.g. `json.dump(my_dict)`'s
internal SETITEMs on a user dict) get lost and the loader's
reconstructed values go stale. The filter should be per-event-type,
not per-frame:

- CALL / RETURN / BIND / UNBIND in stdlib: skip.
- SETATTR / SETITEM / MUTATE / DELITEM / DELATTR / SNAPSHOT /
  OBJ_SNAPSHOT: emit always (object state mutations).
- LINE: skip in stdlib, emit in user code.

So the filter has to consider which event types preserve "external"
state vs which are purely internal control flow.

**Even smarter:** track which oids have been bound to user code.
Mutation events on never-user-bound oids can also be skipped — that
gets us closer to the 99.7% reduction. But it adds state.

**Implementation cost:**
- Simple version (skip CALL/RETURN/BIND/UNBIND in stdlib): ~30 LoC.
  Add `is_user:1` bit to `CodeAnalysis`, expose
  `_tracewal.mark_user_code(code_obj)` so the bootstrap can flag
  user code at registration. Each control-flow hook gets a
  `if (!ca->is_user) return;` early exit.
- Smart version (also gate mutations by user-visibility of the oid):
  ~80 LoC, plus an `is_user_visible` bit in `OidMapEntry`,
  set when the oid is bound in user code, propagated through
  oid creation in OBJ_SNAPSHOT iteration, etc.

**Crash risk:** Low. Defaulting `is_user = true` for all codes
makes unmarked codes capture-everything (current behavior). Worst
failure mode: marker set wrong, lose visibility into some events.
Caught by differential test.

**Correctness risk:** Medium for the smart version — the oid
visibility propagation is easy to get subtly wrong. Need careful
test coverage of cross-domain mutations. The simple version is
straightforward.

**Work order recommendation:** Do this **first** of the perf items.
Highest leverage, decouples from #6 (which mostly affects per-event
cost rather than event count).

## 2. Resizable oid_map

**Idea.** `OID_MAP_SIZE = 16381` is fixed. Open-addressed probe
chains are 32 deep. Workloads with >16K live objects (`mem_dict`
has 50K, real-world captures accumulate similar across imports)
overflow: every subsequent lookup walks 32 mismatched slots, falls
through to `oid_create`, then `oid_create` itself probes another
32 slots without finding free space.

**Why it's worth it.** This is what made my first benchmark hang
for 20+ minutes — once `mem_dict`'s second iteration filled the
map, every event after paid ~64 hash probes. Cliff-shaped.

**Implementation cost:** ~40 LoC. Replace the static array with
a struct holding `entries`, `capacity`, `count`. On insert, if
`count > capacity * 0.75`, allocate a new array at 2× capacity
and rehash. All existing lookup/insert sites use the same probe
loop — they pick up the larger capacity automatically once they
read from the new pointer.

Or, even simpler: bump `OID_MAP_SIZE` to 256K (~16x current,
~16MB memory total). Still fixed, no growth logic, but covers
practical sizes. Trades memory for code simplicity.

**Crash risk:** Low. Hash table growth is well-understood. Edge
case: rehash during emission would be unsafe if anything held a
pointer to an `OidMapEntry`. Looking at the code, all uses are
read-then-act (no held pointers across function calls), so safe.

**Correctness risk:** Low. Same hash function, same semantics.

## 3. Compact event encoding

**Idea.** Current header is 15 bytes (`event_type:u8 + seq:u32 +
oid:u32 + line:i32 + code_idx:u16`). Most events have small
bodies (BIND adds 2 bytes for name_idx, that's it). Headers
dominate. A varint scheme:

- `seq` is monotonic; encode as delta from previous (usually 1,
  fits in 1 byte).
- `oid` is small for most objects (varint, 1-3 bytes vs fixed 4).
- `line` rarely exceeds 16 bits; varint.
- `code_idx` similar.

Realistic average: 4-6 bytes per event vs current 17-20.

**Why it's worth it.** Halves WAL size, proportionally faster
disk I/O and bundle load time. Doesn't help capture-time CPU much
(varint encode/decode is cheap but non-zero).

**Implementation cost:** ~100 LoC. Touches:
- Every `wal_write_header` call site (~20).
- Every `wal_read_*` call in our parser.
- `get_wal()`'s decoder in the fork.
- Our `wal.py`'s parser.

**Crash risk:** Medium. Varint decoding is finicky — off-by-one
causes downstream parse errors that look like garbage data. Easy
to get a seemingly-correct partial implementation that passes
small tests but corrupts large WALs.

**Correctness risk:** Medium-high. Lots of touch points. Need to
be paired with extensive cross-version testing.

**Backward compat:** Old bundles unreadable unless we keep the
v1 parser around. Add a version byte at the start of the WAL.

## 4. Streaming compression

**Idea.** WAL bytes are highly compressible (many repeated headers
and code_idx values). Stream LZ4 (or zstd) around the writer —
events are written compressed, read decompressed.

**Why it's worth it.** 3-5x smaller bundles. Doesn't help
capture-time CPU (compression is CPU work), but makes the bundles
storable / shareable for real captures.

**Implementation cost:** ~50 LoC + a build-system dep on liblz4.

**Crash risk:** Medium-low. LZ4 / zstd are battle-tested. The
risk is in the integration: making sure the buffer-flush pipeline
plays nicely with compression context state, and handling the
end-of-stream marker correctly.

**Correctness risk:** Low if we use a tested library.

**Backward compat:** Add a version / format byte; reader
auto-detects.

**Stacks well with #3.** Compression on top of compact encoding
gives much better ratios than either alone (compact encoding
removes redundancy compression would also remove, but gets the
size win without the CPU cost on read).

## 5. Skip BIND for unchanged primitive values

**Idea.** STORE_FAST always emits a BIND, even when storing the
same primitive value that was already there (`x = x`, accumulator
patterns where `total += 0`, etc.). We already skip when the new
oid matches the old oid for non-primitives. Add the analog for
primitives: compare `new_obj` to `old_obj` (cheap for
small-ints/None/bools via pointer; needs Py_RichCompare for
strings/floats which is more work).

**Why it's worth it.** Modest. Real-world impact varies — some
workloads have many same-value rebinds, most don't.

**Implementation cost:** ~20 LoC in `_PyWAL_OnStoreFast`. Need a
per-frame-slot cache of "last primitive value bound" for the
comparison.

**Crash risk:** Very low.

**Correctness risk:** Low — we skip emit on no-op stores; same
final state.

## 6. tp_dealloc hooks

**Idea.** At trace start, replace `tp_dealloc` for tracked types
(PyList_Type, PyDict_Type, PySet_Type, PyTuple_Type, type-tag-10
candidates) with a shim that emits `WAL_DEALLOC` and calls
`oid_invalidate` before chaining to the original. Restore on
stop. This gives us the missing destruction signal — most of the
OID-refresh logic that drives our 3x overhead becomes unnecessary
because we KNOW when an oid is dead.

**Why it's worth it.** This is the silver bullet for the bulk of
our regression. Today we re-snapshot at every binding site "in
case the address was reused" — almost always it wasn't.
With dealloc events we'd only snapshot at CREATE (rare),
invalidate on dealloc (cheap), and skip the per-binding refresh
entirely. Estimated: brings 3.0x → ~1.2-1.5x.

**Implementation cost:** ~80-150 LoC. For each tracked type:
- Save `original_dealloc = TYPE.tp_dealloc;` at start.
- Install `shim_dealloc`.
- On stop: `TYPE.tp_dealloc = original_dealloc;`.
- The shim does: `oid_lookup → emit_dealloc → oid_invalidate →
  call original`.

**Crash risk: HIGH.** This is the gnarly one. Failure modes:

- **Cleanup ordering on crash.** If Python crashes mid-trace
  (unhandled exception, segfault elsewhere, KeyboardInterrupt
  during teardown), our shim is left installed. The next
  dealloc anywhere in the program calls into our shim, which
  reads our state — which we then free. Segfault. Mitigation:
  `Py_AtExit` cleanup or interpreter-finalization hook.

- **Race conditions.** Multi-threaded programs deallocate from
  any thread. Our shim accesses `g_oid_map` without locking.
  Need either a mutex (slow) or a thread-local oid map (complex).

- **Subclass coverage.** `PyDict_Type.tp_dealloc` covers `dict`
  but a `dict` subclass with its own `tp_dealloc` that doesn't
  chain to base would skip our shim. Detectable (we'd lose
  dealloc events for that class) but a silent gap.

- **Other extensions doing the same thing.** A debug tool that
  also patches `tp_dealloc` could collide with us. The `original`
  we capture might be another tool's shim, not the real
  dealloc — chain order matters.

- **Type immortalization (3.12+).** Some types are immortal in
  3.12+; their `tp_dealloc` is never called. Should be fine for
  our targets but worth verifying.

**Correctness risk:** Medium. Easy to miss types or miss subclass
cases. Easy to have race conditions if multi-threaded.

**Reversibility:** Trivial in code (install/uninstall), but the
crash-window leaves a foot-gun. Suggest implementing AFTER #1
(which already gives most of the speedup we need without the
risk).

**Alternative considered:** `gc.callbacks` (Python-level GC
hook). Only fires for the cyclic GC; most objects die via
refcount, not GC. So `gc.callbacks` misses ~all the events we
care about. Not viable.

**Alternative considered:** weakrefs. `list`/`dict`/`tuple`/
`function`/`method` all reject `weakref.ref()`. Useless for the
most common containers.

So `tp_dealloc` shimming is the only complete signal. The risk
profile is real but tractable if done with appropriate care
(atexit handler, defensive nulls, document threading caveat).

---

## Suggested work order

If we ever do this work:

1. **#2 (resizable oid_map)** first. Cheap, removes a cliff that
   already makes some real workloads unusable.
2. **#1 (user-code filter)** next. Highest WAL-volume win.
   Implements the simple variant first; bench, then decide if
   the smart variant is worth the complexity.
3. **#5 (skip same-primitive BIND)** as a freebie if doing other
   STORE_FAST work.
4. **#3 (compact encoding)** after #1 — by then WAL volume is
   already much smaller, and the compaction win is on a smaller
   base.
5. **#4 (compression)** after #3 — compaction first, then squeeze
   what's left.
6. **#6 (tp_dealloc hooks)** last. By the time we get here, #1
   has eliminated most of the per-binding-site work that #6
   would also eliminate, so the marginal win is smaller and the
   risk is the same. But it's the architecturally cleanest fix
   if perf is still a problem after the others.
