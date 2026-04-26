# exp30 — `tp_dealloc` hooks for user-defined classes (attempted, abandoned)

**Status (2026-04-26):** abandoned in favor of type-pointer comparison in
`oid_get_or_create`. Documented here so a future round can either pick this
back up under different constraints, or reuse the failure analysis.

## Problem

`oid_get_or_create_refresh` short-circuits via `snapshot_fresh`. When an
address is recycled across object lifetimes, the cached oid carries the
prior occupant's loader state. Phase-1 fix landed `tp_dealloc` hooks on
`PyList_Type`, `PyDict_Type`, `PySet_Type` (built-in mutable containers).
That fixes the list/dict/set case but not user-defined classes:
**Tokenizer instances dying at addresses later occupied by Items** caused
the inspector to render Items as Tokenizers (with mixed Tokenizer + Item
attrs) — spotted in `order_pipeline.atrace`'s second
`OrderProcessor.process` call.

## Approach attempted

Lazy-patch `tp_dealloc` per heap-type at first sight in `oid_create`:

- Maintain a fixed-size table `g_user_type_deallocs[256]` of
  `(PyTypeObject*, original tp_dealloc)`.
- `wal_user_obj_dealloc` looks up `Py_TYPE(obj)` in the table and dispatches
  to the saved original after invalidating the oid_map entry.
- Patch only heap types (`Py_TPFLAGS_HEAPTYPE`).
- Restore originals on `_PyWAL_Stop` so interpreter shutdown isn't routed
  through our wrapper.

## Failure modes encountered (in order discovered)

1. **Static types (`type`, exceptions) hung at shutdown.**
   Skipped with the `Py_TPFLAGS_HEAPTYPE` check. *(Necessary, not
   sufficient.)*

2. **Inheritance leaks.** Subclasses of patched types inherit
   `wal_user_obj_dealloc` at their slot via `inherit_slots` during
   `PyType_Ready`. The subclass isn't in our table → wrapper returns
   without calling original → object leaks → interpreter wedges.
   Mitigated by walking the base chain at lookup time, but only partially.

3. **Custom C-extension `tp_dealloc` slots.** `_thread.lock`,
   `_thread.RLock`, weakref subclasses (`KeyedRef`), and friends have
   custom destructors with their own resource invariants. Replacing them
   with our wrapper:
   - For `_thread.lock`: hangs the interpreter mid-import (locks are
     load-bearing for `importlib._bootstrap`).
   - For `KeyedRef`: causes import of `enum`/`dataclasses`/`re` to hang.
     `KeyedRef.tp_dealloc` resolves to `subtype_dealloc` (heap-type
     generic), which our pointer-equality filter on
     `g_canonical_subtype_dealloc` did *not* exclude — so the patch
     applied and broke `WeakValueDictionary` cleanup downstream.

   Filter "only patch tp_dealloc == canonical subtype_dealloc" was
   necessary but again not sufficient: heap-type subclasses of
   `weakref.ref` inherit subtype_dealloc transitively.

4. **Cumulative complexity.** Each filter ruled out *some* problem types
   while still leaving others. Final attempt before abandoning had four
   layers of guards (heap-only, canonical-subtype-only, MRO walk,
   first-original fallback) and still hung on `import enum`.

## Why we abandoned

- Each new failure required another guard. The set of types whose
  destructor is unsafe to wrap appears to be open-ended (any C extension
  with custom `tp_dealloc`, plus their pure-Python heap-type subclasses).
- The structural fix (true dealloc invalidation for *every* heap type)
  needs deeper integration — likely a GC-callback rather than slot
  patching — and that's a multi-day investigation in CPython internals.
- For the demo we accept slightly more work in `oid_get_or_create` (a
  pointer compare on every type-tag-10 lookup) in exchange for a
  one-line surface area and zero interpreter-internals risk.

## What landed instead

`OidMapEntry.type_ptr` records `Py_TYPE(obj)` at insert time. On lookup
in `oid_get_or_create` (and `oid_get_or_create_refresh`), if `type_ptr`
mismatches `Py_TYPE(obj)`, the entry is invalidated → fresh oid +
CREATE+OBJ_SNAPSHOT. Cost: one pointer compare per type-10 lookup (no
`InternFromString`); ~0% measured perf delta on the LARGE_WORKLOADS
suite (2.24x → 2.26x ALL, within noise).

Limitation kept: same-class same-address reuse with a different initial
`__dict__` still slips through. Not exercised by current tests; would
need true dealloc invalidation to close.

## If you pick this back up

The path that's most likely to actually work:
1. **GC callback** instead of slot patching — register a callback that
   fires on every collected/destroyed object, regardless of type. CPython
   3.13's `PyUnstable_GC_VisitObjects` or a `gc.callbacks` listener
   could be the seam, though the latter is Python-level and per-cycle.
2. **PEP 683 immortal-object hardening** affects how some types' refcount
   behaves at shutdown — verify before patching slots.
3. Look at how `tracemalloc` handles this — it tracks every allocation
   without slot-patching, via `PyMem_SetAllocator` interposition. That's
   probably the right model.
