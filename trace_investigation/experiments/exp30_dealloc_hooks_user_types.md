# exp30 — `tp_dealloc` slot-patching for user-defined classes (attempted, abandoned)

**Status (2026-04-26):** abandoned in favor of a single hook in
`Objects/object.c::_Py_Dealloc`. This page documents the slot-patching
attempt and the failure modes that led us to the simpler approach, so a
future round doesn't re-walk the same ground.

## Problem

`oid_get_or_create_refresh` short-circuits via `snapshot_fresh`. When an
address is recycled across object lifetimes, the cached oid carries the
prior occupant's loader state.

For built-in mutable containers (`list`, `dict`, `set`), straightforward:
patch `PyXxx_Type.tp_dealloc` to invalidate the oid_map on free.

For user-defined classes (heap types) the obvious extension was: do the
same thing per-heap-type, lazily as we see them. **It doesn't work in
practice.**

## What we tried

Lazy-patch `tp_dealloc` per heap-type at first sight in `oid_create`:

- Fixed-size table `g_user_type_deallocs[256]` of `(PyTypeObject*, original)`.
- `wal_user_obj_dealloc` looks up `Py_TYPE(obj)` in the table and dispatches
  to the saved original after invalidating the oid_map entry.
- Patch only heap types (`Py_TPFLAGS_HEAPTYPE`).
- Restore originals on `_PyWAL_Stop` so interpreter shutdown isn't routed
  through our wrapper.

## Failure modes encountered (in order discovered)

1. **Static types hung at shutdown.** Patching `type` itself (it's a
   `type_tag = 10` user-object value as far as the trace can tell) wedged
   the interpreter mid-shutdown. Skipped with `Py_TPFLAGS_HEAPTYPE`.
   *(Necessary, not sufficient.)*

2. **Inheritance leaks.** Subclasses of patched types inherit
   `wal_user_obj_dealloc` via `inherit_slots` during `PyType_Ready`. The
   subclass isn't in our table → wrapper returns without calling original
   → object leaks → interpreter wedges. Mitigated by walking the base
   chain at lookup time, but the table lookup never matches the subclass
   itself, only ancestors.

3. **Custom C-extension `tp_dealloc` slots.** `_thread.lock`,
   `_thread.RLock`, weakref subclasses (`KeyedRef`), and friends have
   custom destructors with their own resource invariants. Replacing them:
   - `_thread.lock` hangs interpreter mid-import (locks are load-bearing
     for `importlib._bootstrap`).
   - `KeyedRef` hangs `import enum`/`dataclasses`/`re`.
     `KeyedRef.tp_dealloc` resolves to `subtype_dealloc` (heap-type
     generic), so a pointer-equality filter on
     `g_canonical_subtype_dealloc` doesn't exclude it — patch applied,
     `WeakValueDictionary` cleanup broke downstream.

   Filter "only patch tp_dealloc == canonical subtype_dealloc" was
   necessary but not sufficient: heap-type subclasses of `weakref.ref`
   inherit subtype_dealloc transitively, which our filter doesn't see.

4. **Cumulative complexity.** Each filter ruled out *some* problem types
   while still leaving others. The final attempt before abandoning had
   four layers of guards (heap-only, canonical-subtype-only, MRO walk,
   first-original fallback) and still hung on `import enum`.

## Why slot-patching fails structurally

Slot patching is per-type. Each type has its own `tp_dealloc` field. The
*safe* set of types to patch (pure Python `class Foo: ...` heap types
inheriting `subtype_dealloc`) overlaps with the *unsafe* set (heap types
whose `subtype_dealloc` chain leads to a custom C destructor downstream)
in ways we can't tell apart at slot-write time. Each new failure
discovered another corner — and after spending the day chasing them we
still couldn't enumerate the unsafe set ahead of time.

## What landed instead

A single hook in CPython's `Objects/object.c::_Py_Dealloc`:

```c
if (_PyWAL_enabled) {
    _PyWAL_OnObjectDealloc(op);
}
(*dealloc)(op);
```

`_Py_Dealloc` is the universal funnel for every refcount-driven *and*
GC-driven object death. The hook calls `oid_invalidate((uintptr_t)op)`
before the type's own destructor runs. No per-type tracking, no
inheritance issues, no slot rewrites — just one well-placed call site
in the dispatcher every PyObject already passes through.

This also made the `OidMapEntry.type_ptr` cross-class detection
unnecessary (it was a workaround for the slot-patching approach not
covering user types) and the per-type list/dict/set wrappers redundant.
Net code change vs. the slot-patching attempt: **smaller**, not bigger.

Cost on `LARGE_WORKLOADS`: 2.24x → 2.30x ALL (~3% delta, within
run-to-run noise). Per-workload all under 5x; closes the same-class
user-object pollution gap that slot-patching would've left behind.

## Lesson

When a hook is dispatched to from a single C function in CPython,
intercepting that function (one site, one call) tends to beat patching
N type slots (N sites, fragile invariants per type). We tried slots
first because the demo's prior fix used slot-patching for list/dict/set
and the framing in HANDOFF read "tp_dealloc hooks" naturally as
slot-patching — but the universal funnel was always the cleaner shape,
once you look at it.
