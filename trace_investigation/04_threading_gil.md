# Threading Model and the GIL

Understanding CPython's threading model is critical for designing a tracer that works correctly under concurrent execution — especially with the free-threaded (no-GIL) build that's becoming the default.

## GIL Background

The Global Interpreter Lock (GIL) has historically ensured that only one thread executes Python bytecode at a time. This simplified the interpreter but prevented true parallelism for CPU-bound Python code.

## Current State: Free-Threaded Build (Py_GIL_DISABLED)

**Status as of this CPython source (3.15-dev/main branch):** The free-threaded build is available via the `--disable-gil` configure option. It's not yet the default, but it's a first-class build configuration.

**Key configure option:** `--disable-gil` sets `Py_GIL_DISABLED`

### GIL State Structure

**File:** `Include/internal/pycore_gil.h`

```c
struct _gil_runtime_state {
#ifdef Py_GIL_DISABLED
    int enabled;              // 0 = disabled, >0 = enabled (transient/permanent)
#endif
    int locked;               // atomic GIL lock state
    unsigned long interval;   // thread switch interval (~5000 microseconds)
    PyThreadState *last_holder;
    PyCOND_T cond;            // condition variable
    PyMutex_T mutex;
};
```

Even in free-threaded builds, the GIL can be dynamically **re-enabled** (e.g., when a C extension that isn't thread-safe is loaded). The `enabled` counter tracks this.

### GIL Acquisition/Release

**File:** `Python/ceval_gil.c`

```c
static void take_gil(PyThreadState *tstate) {
#ifdef Py_GIL_DISABLED
    if (!_Py_atomic_load_int_relaxed(&gil->enabled)) {
        return;  // Skip GIL if free-threaded
    }
#endif
    // ... standard GIL acquisition
}

static void drop_gil(PyInterpreterState *interp, PyThreadState *tstate, ...) {
#ifdef Py_GIL_DISABLED
    if (tstate != NULL && !tstate->holds_gil) {
        return;  // Nothing to release
    }
#endif
    // ... standard GIL release
}
```

## Thread States

### Per-Interpreter State

**File:** `Include/internal/pycore_interp_structs.h`

```c
struct _ceval_state {
    uintptr_t instrumentation_version;   // shared across threads
    struct _gil_runtime_state *gil;
    struct _pending_calls pending;
};
```

The `instrumentation_version` is shared — all threads see instrumentation changes.

### Per-Thread State

**File:** `Include/internal/pycore_tstate.h`

In free-threaded builds, each thread has significantly more local state:

```c
typedef struct _PyThreadStateImpl {
    PyThreadState base;

#ifdef Py_GIL_DISABLED
    // Per-thread allocation tracking
    struct _gc_thread_state gc;

    // Biased reference counting
    struct {
        Py_ssize_t *values;     // thread-local refcounts
        Py_ssize_t size;
    } refcounts;

    // Per-thread memory management
    struct _mimalloc_thread_state mimalloc;
    struct _Py_freelists freelists;
    struct _brc_thread_state brc;

    int32_t tlbc_index;   // thread-local bytecode index

    char __padding[64];   // cache-line padding
#endif
} _PyThreadStateImpl;
```

### Thread Attachment States

**File:** `Include/internal/pycore_pystate.h`

```c
#define _Py_THREAD_DETACHED        0   // not executing Python
#define _Py_THREAD_ATTACHED        1   // running Python code
#define _Py_THREAD_SUSPENDED       2   // paused (stop-the-world)
#define _Py_THREAD_SHUTTING_DOWN   3   // interpreter finalizing
```

**Key difference:**
- **GIL builds:** Only one thread can be `ATTACHED` at a time (the GIL holder)
- **Free-threaded builds:** Multiple threads can be `ATTACHED` simultaneously

## Free-Threaded Object Layout {#free-threaded-object-layout}

**File:** `Include/object.h:156-168`

In free-threaded builds, `PyObject` has a different layout:

```c
#ifdef Py_GIL_DISABLED
struct _object {
    uintptr_t ob_tid;          // owning thread ID
    uint16_t ob_flags;
    PyMutex ob_mutex;          // per-object lock (1 byte!)
    uint8_t ob_gc_bits;
    uint32_t ob_ref_local;     // thread-local refcount
    Py_ssize_t ob_ref_shared;  // shared (atomic) refcount
    PyTypeObject *ob_type;
};
#endif
```

### Biased Reference Counting

Instead of a single atomic refcount (expensive under contention), free-threaded CPython uses **biased reference counting**:

- `ob_ref_local` — the "owning" thread's refcount (cheap, non-atomic)
- `ob_ref_shared` — refcount from other threads (atomic operations)
- `ob_tid` — identifies which thread "owns" the object
- Deferred refcounting (`_Py_REF_DEFERRED`) delays deallocation

This means **reading an object's true refcount requires checking both fields**.

## Synchronization Primitives

### PyMutex (Lightweight Lock)

**File:** `Python/lock.c`, `Include/internal/pycore_lock.h`

```c
typedef struct {
    uint8_t _bits;   // bit 0: locked, bit 1: has parked threads
} PyMutex;
```

Only 1 byte! Uses a spin-then-park strategy:

```c
#if Py_GIL_DISABLED
static const int MAX_SPIN_COUNT = 40;      // spin before parking
#else
static const int MAX_SPIN_COUNT = 0;       // no spinning with GIL
#endif
```

After 1ms, the unlock operation hands off the lock directly to a waiting thread (fairness guarantee).

### Critical Sections

**File:** `Include/internal/pycore_critical_section.h`

In free-threaded builds, container operations (dict lookup, list append, etc.) are protected by per-object critical sections rather than the GIL:

```c
#define Py_BEGIN_CRITICAL_SECTION(op)    // lock op->ob_mutex
#define Py_END_CRITICAL_SECTION(op)      // unlock

#define Py_BEGIN_CRITICAL_SECTION2(a, b) // lock two objects (deadlock-free)
#define Py_END_CRITICAL_SECTION2(a, b)
```

### Stop-the-World

**File:** `Include/internal/pycore_interp_structs.h:408-421`

For operations that need a consistent global view (GC, instrumentation changes):

```c
struct _stoptheworld_state {
    PyMutex mutex;
    bool requested;
    bool world_stopped;
    bool is_global;
    PyEvent stop_event;
    Py_ssize_t thread_countdown;
    PyThreadState *requester;
};
```

Usage:
```c
_PyEval_StopTheWorld(interp);   // pause all other threads
// ... modify shared state (e.g., instrument bytecode) ...
_PyEval_StartTheWorld(interp);  // resume
```

This is how `sys.monitoring.set_events()` safely modifies bytecode across all threads.

## Thread-Local Bytecode (co_tlbc)

**File:** `Include/cpython/code.h:38`

In free-threaded builds, each thread can have its own copy of a code object's bytecode. This is because bytecode is mutated by the adaptive specializer — different threads might specialize the same code differently based on the types they encounter.

```c
_PyCodeArray *co_tlbc;   // array of per-thread bytecode copies
```

Each frame stores a `tlbc_index` indicating which thread-local copy its `instr_ptr` points into.

**Implication for tracing:** When reading `frame->instr_ptr`, we need to account for the fact that the bytecode pointer might be into a thread-local copy, not the main `co_code_adaptive`. The instruction offsets are the same, but the pointer arithmetic must use the correct base.

## Eval Breaker

**File:** `Python/ceval_gil.c:73-99`

The `eval_breaker` is a per-thread flag checked at backward jumps and certain opcodes. It triggers:
- GIL release/reacquire
- Signal handling
- Pending calls
- Instrumentation version checks

In free-threaded builds, the eval breaker is updated **eagerly on all threads** rather than lazily:

```c
#ifdef Py_GIL_DISABLED
    // Free-threaded builds eagerly update eval_breaker on *all* threads
    return;
#endif
```

## Implications for Tracing

### Challenge: Concurrent Event Streams

With the GIL, events are naturally serialized. Without it, multiple threads can generate trace events simultaneously. Our tracer must:

1. **Use per-thread buffers** — avoid lock contention on a shared event buffer
2. **Timestamp events** — for merging per-thread streams into a coherent timeline
3. **Handle concurrent object mutation** — a variable's value might change between the instruction that modifies it and our read of it

### Challenge: Object Safety

Without the GIL, reading object fields requires care:
- Use critical sections for container objects (dict, list)
- Atomic reads for refcounts and simple fields
- Stop-the-world for consistent snapshots (expensive)

### Challenge: Instrumentation Thread Safety

Bytecode instrumentation (PEP 669) already uses stop-the-world for safety. Our tracing should:
- **Piggyback on PEP 669** rather than implementing our own instrumentation mechanism
- Use per-thread trace buffers that don't require cross-thread synchronization during event recording
- Merge/sort buffers offline using timestamps

### Opportunity: Per-Thread Bytecode

The `co_tlbc` mechanism means each thread already has its own bytecode copy. In theory, we could modify a thread's bytecode copy to include tracing instrumentation without affecting other threads. However, PEP 669 already provides a cleaner way to do this.

### Practical Recommendation

For initial implementation, consider:
1. **Target GIL-enabled builds first** — simpler to get correct
2. **Use per-thread ring buffers** — lock-free recording
3. **Support free-threaded builds** by using the existing synchronization primitives (PyMutex, critical sections)
4. **Avoid stop-the-world** during normal tracing — only use it for setup/teardown
