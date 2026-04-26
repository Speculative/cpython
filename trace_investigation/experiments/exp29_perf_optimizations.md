# Experiment 29: Proposed Performance Optimizations (Notes)

Status: design notes. Resizable oid_map and pluggable user-code
classifier are scheduled for the next round of work; everything else
deferred to post-demo.

After the autopsy-report consumer landed several correctness fixes on
top of exp28 (OID-refresh + snapshot_fresh + type-name fidelity +
WAL_OBJ_SNAPSHOT for user objects), aggregate tracing overhead grew
from the original ~1.0x baseline to ~3.0x. The autopsy-report demo
needs to credibly claim "tracing is cheap"; that means the next round
of fork work targets the items that bound *both* runtime cost AND
memory growth.

The risk assessments are first-pass — implementation-dependent and
should be re-checked when actually doing the work.

## At a glance

| # | Optimization | Aggregate win | Code | Invasiveness | Crash risk | When |
|---|---|---|---|---|---|---|
| 1 | Resizable oid_map | Removes a sharp cliff | ~40 LoC | Hash table refactor | Low | **next** |
| 2 | Pluggable user-code classifier | ~5-10x WAL volume | ~80 LoC | Per-code tag + Python callback hook + every event-emitting hook | Low-Medium | **after #1** |
| 3 | tp_dealloc hooks | ~2x runtime + bounds map memory | ~80-150 LoC | Patches CPython type slots | High | post-demo |
| 4 | Compact event encoding | ~2-3x smaller WAL | ~100 LoC | Touches every emit + every parser | Medium | post-demo |
| 5 | Streaming compression | ~3-5x smaller WAL | ~50 LoC | flush path + reader | Medium-Low | post-demo |
| 6 | Skip BIND for unchanged primitives | Modest | ~20 LoC | One hook | Very low | post-demo |

---

## Background: the OID map memory story

Today the fork's `g_oid_map` is a static array of 16381 `OidMapEntry`
slots, each ~24 bytes, ~390KB total. `g_next_oid` increments forever;
entries are never removed. We have no destruction signal — `gc.callbacks`
fires only for cyclic GC (most objects die via refcount), and weakrefs
don't work for `list`/`dict`/`tuple`/function/method/builtin (all the
common types). So we can't safely evict: heuristic eviction (LRU,
reference-count-at-the-oid-level, frame-scoped) all break the identity
model — the next access to a still-alive object would create a fresh
oid for it, silently fragmenting identity in the trace.

This means map size grows with **objects-ever-seen**, not **objects
currently live**.

The combinations stack like this:

| Combination | Map growth | Memory bound | Correctness |
|---|---|---|---|
| Today | Unbounded; cliff at 16K | None | OK |
| + Resize (#1) | Unbounded; no cliff | Linear in objects-ever | OK |
| + User-code filter (#2) | Slow growth (most stdlib oids never created) | Linear in user-visible objects-ever | OK |
| + Dealloc hooks (#3) | Steady-state | Linear in *live* user-visible objects | OK |

Each layer reduces growth rate; only the last bounds it. For the demo
we'll get away with `#1 + #2` — a small script run for ~30 seconds
captures a small bundle, the map stays well under any practical cap.
For a real production-shaped capture (script running for minutes,
allocating millions of short-lived objects), `#3` becomes load-bearing.

---

## 1. Resizable oid_map  *(scheduled)*

**Idea.** Replace the static `OidMapEntry[16381]` with a growable
struct. On insert, if `count > capacity * 0.75`, allocate at 2×
capacity and rehash. All existing lookup/insert sites use the same
32-step probe loop — they pick up the new capacity automatically once
they read the `entries` pointer.

Or, lazier: bump the static cap to ~256K (~16x current, ~6MB total
memory). No growth logic, but covers practical sizes for our demo.

**Why scheduled.** This is what made one of our benchmarks hang for
20+ minutes — once `mem_dict`'s second iteration filled the map,
every event after paid 32 mismatched probes followed by another 32 in
`oid_create`. A sharp cliff that becomes a real-world failure mode for
captures of medium-sized scripts.

**Implementation cost:** ~40 LoC (resizable variant) or ~5 LoC (just
bump the cap).

**Crash risk:** Low. Hash table growth is well-understood. The
existing code uses read-then-act patterns (no held pointers across
calls), so rehash during emission is safe.

**Correctness risk:** Low. Same hash function, same semantics, just
larger capacity.

**Recommendation.** Start with the resizable variant. The bump-the-cap
shortcut is tempting but doesn't actually solve the problem for any
script that genuinely needs >256K oids. With dealloc hooks deferred,
real captures will accumulate millions of oids over minutes — only a
resizable structure handles that gracefully.

## 2. Pluggable user-code classifier  *(scheduled)*

**Idea.** The fork stays out of the "what is user code?" business —
that's project-specific and varies by use case. It only provides the
*mechanism*: a per-`CodeAnalysis` `is_user:1` bit, set via a Python
callback the fork invokes once at first encounter of each code object.
Result is cached on the code; the callback is one-per-unique-code, not
per-event.

Skip CALL/RETURN/BIND/UNBIND/LINE in stdlib. Mutation events
(SETATTR/SETITEM/MUTATE/DELITEM/DELATTR/SNAPSHOT/OBJ_SNAPSHOT) emit
unconditionally — otherwise stdlib mutations to user-visible objects
get lost and the loader's reconstructed values go stale.

### Mechanism vs policy split

The fork exposes:

```python
_tracewal.set_classifier(callable)
# callable(code_obj) -> bool   (True = user code)
```

The autopsy-report bootstrap supplies the policy. Today's
`_build_classifier` (path-based: outside stdlib_dir + outside
site-packages + outside autopsy_report) becomes the default classifier.
Users pass overrides:

```python
capture(
    "script.py",
    scope="auto",                # default heuristic
    include=["requests"],         # also trace this lib (sugar)
    exclude=["mypkg.codegen"],    # but skip this internal pkg (sugar)
    classifier=my_predicate,      # full escape hatch
)
```

`scope="auto"` runs the heuristic. `include`/`exclude` are sugar that
wrap it. `classifier` is the "I know what I'm doing" override.

### Use cases this enables

- **Default**: same as today. User script + their packages, skip stdlib
  + site-packages. Now applied at capture time so the WAL never
  contains stdlib churn.
- **Debug a specific library**: `--include requests` to trace into
  requests.get when hunting a hang. Currently impossible without
  hand-editing `_build_classifier`.
- **Niche custom filtering**: a power user passes a callable that
  matches on file path, qualname, code object flags, anything in
  `co_*`. Rare but cheap to support.
- **Future extension**: three-way tag (full / call-only / skip)
  instead of binary, for "I want to see *that* my code called this
  but not *what* the library did." Same callback returns an enum.
  Deferrable, but the design accommodates it.

### Correctness subtlety

Mutations to user-visible objects from inside stdlib must still emit.
`json.dump(my_dict)` does internal SETITEMs on `my_dict`; if we skip
those, the loader's reconstructed dict goes stale. The simple rule
"skip events when current frame is stdlib" is wrong; the rule must be
event-type-aware:

- CALL / RETURN / BIND / UNBIND / LINE: skip in stdlib.
- SETATTR / SETITEM / MUTATE / DELITEM / DELATTR / SNAPSHOT /
  OBJ_SNAPSHOT: emit always.

A smarter version would also gate mutation events by *user-visibility
of the oid*: if a list was never bound in user code, even its mutations
don't matter to the consumer. Adds an `is_user_visible:1` bit to
`OidMapEntry`, propagated when the oid is bound in user code or
appears in the __dict__ of a user-visible object. More complex; defer
unless the simple version isn't enough.

**Implementation cost:**
- Simple version (callback hook + per-code bit + early-return in
  control-flow hooks): ~80 LoC across the fork plus matching changes
  in autopsy_report's bootstrap.
- Smart version (also gate mutations by oid user-visibility): +~40 LoC,
  more careful test coverage of cross-domain mutations.

**Crash risk:** Low. The callback is invoked at code registration with
a clear error path (fall back to "user code" if it raises). Worst
failure mode: misclassified codes lose visibility into events caught
by the differential test.

**Correctness risk:** Medium for the smart version (oid visibility
propagation has edge cases — a user object containing a stdlib-created
list, etc.). The simple version is straightforward and probably good
enough for the demo.

**Recommendation.** Start with the simple version. Bench. Only do the
smart version if real captures still have too much mutation noise.

## 3. tp_dealloc hooks  *(post-demo)*

**Idea.** At trace start, replace `tp_dealloc` for tracked types
(PyList_Type, PyDict_Type, PySet_Type, PyTuple_Type, type-tag-10
candidates) with a shim that emits `WAL_DEALLOC` and calls
`oid_invalidate` before chaining to the original. Restore on stop.
Gives us the missing destruction signal — most of the OID-refresh
logic that drives our 3x overhead becomes unnecessary because we KNOW
when an oid is dead.

**Why bumped to position #3 in priority** (was #6 in the previous
draft): the runtime-perf framing alone undersold this. It's also the
only thing that **bounds map memory** in long-running captures. With
just #1 + #2, oid_map size grows linearly with objects-ever-allocated
in user code — manageable for short demo scripts, not for real
captures running for minutes. With dealloc hooks, size ≈ live working
set.

**Implementation cost:** ~80-150 LoC. For each tracked type:
- Save `original_dealloc = TYPE.tp_dealloc;` at start.
- Install `shim_dealloc`.
- On stop: `TYPE.tp_dealloc = original_dealloc;`.
- The shim does: `oid_lookup → emit_dealloc → oid_invalidate → call original`.

**Crash risk: HIGH.** The gnarly one. Failure modes:

- **Cleanup ordering on crash.** If Python crashes mid-trace
  (unhandled exception, segfault elsewhere, KeyboardInterrupt during
  teardown), our shim is left installed. The next dealloc anywhere
  in the program calls into our shim, which reads our state — which
  we then free. Segfault. Mitigation: `Py_AtExit` cleanup or
  interpreter-finalization hook.
- **Race conditions.** Multi-threaded programs deallocate from any
  thread. Our shim accesses `g_oid_map` without locking. Need
  either a mutex (slow) or a thread-local oid map (complex).
- **Subclass coverage.** `PyDict_Type.tp_dealloc` covers `dict` but a
  `dict` subclass with its own `tp_dealloc` that doesn't chain to
  base would skip our shim. Detectable (we'd lose dealloc events
  for that class) but a silent gap.
- **Other extensions doing the same thing.** A debug tool that also
  patches `tp_dealloc` could collide with us. The `original` we
  capture might be another tool's shim — chain order matters.
- **Type immortalization (3.12+).** Some types are immortal in 3.12+;
  their `tp_dealloc` is never called. Should be fine for our targets
  but worth verifying.

**Correctness risk:** Medium. Easy to miss types or miss subclass
cases. Easy to have race conditions if multi-threaded.

**Reversibility:** Trivial in code (install/uninstall), but the
crash-window leaves a foot-gun.

**Alternatives ruled out:** `gc.callbacks` fires only for cyclic GC
(misses ~all the events we care about). Weakrefs reject the most
common types. `tp_dealloc` shimming is the only complete signal.

**Why post-demo:** The risk profile makes this a deliberate work
session, not a quick fix. The demo will be on small scripts where
unbounded-but-small map size is acceptable. Plan to do this when
moving from "demo on tiny scripts" to "real captures on longer
running programs."

## 4. Compact event encoding  *(post-demo)*

**Idea.** Current header is 15 bytes (`event_type:u8 + seq:u32 +
oid:u32 + line:i32 + code_idx:u16`). Most events have small bodies
(BIND adds 2 bytes for name_idx, that's it). Headers dominate.
Varint scheme: `seq` as delta from previous (usually 1, fits in 1
byte), `oid`/`line`/`code_idx` as varint (1-3 bytes typically).

Realistic average: 4-6 bytes per event vs current 17-20.

**Why post-demo.** With #2 in place, WAL volume already drops 5-10x.
Compact encoding on top is multiplicative but on a much smaller base.
Risk-reward less attractive than the higher-leverage items.

**Implementation cost:** ~100 LoC. Touches every emission and every
parser.

**Crash risk:** Medium. Varint decoding is finicky.

**Backward compat:** Old bundles unreadable unless we keep both
parsers. Add a version byte.

## 5. Streaming compression  *(post-demo)*

**Idea.** WAL bytes are highly compressible. Stream LZ4 (or zstd)
around the writer.

**Why post-demo.** Same reason as #4 — by then WAL is small enough
that the compression win matters less.

**Implementation cost:** ~50 LoC + a build-system dep on liblz4.

**Crash risk:** Medium-low.

## 6. Skip BIND for unchanged primitive values  *(post-demo)*

**Idea.** STORE_FAST always emits a BIND, even when storing the same
primitive (`x = x`, accumulator patterns where `total += 0`). Compare
new to old before emitting.

**Why post-demo.** Modest. Real-world impact varies. Easy to add when
we're touching the STORE_FAST hook for other reasons.

**Implementation cost:** ~20 LoC.

**Crash risk:** Very low.

---

## Suggested work order

**Pre-demo, in order:**

1. **#1 (resizable oid_map)** — fixes a sharp cliff that already
   makes some real workloads unusable.
2. **#2 (pluggable user-code classifier)** — biggest WAL volume win,
   and necessary infrastructure for the demo itself (so the captured
   bundle is a clean slice of user code, not 99.7% stdlib).

**Post-demo, prioritized:**

3. **#3 (tp_dealloc hooks)** — the architecturally correct fix, and
   the only one that bounds map memory in long captures. High crash
   risk earns it post-demo timing; do it as a deliberate work session.
4. **#4 (compact encoding)** — 2-3x smaller WAL.
5. **#5 (streaming compression)** — additional 3-5x on top of #4.
6. **#6 (skip same-primitive BIND)** — freebie when next touching the
   STORE_FAST hook.
