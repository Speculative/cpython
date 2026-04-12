# How Code, Values, and Objects Are Stored

Understanding CPython's object model is essential for knowing what data we need to capture during tracing and how to read it efficiently.

## The Universal Base: PyObject

Every Python value is a `PyObject`. The base struct (from `Include/object.h:127-168`) is:

```c
struct _object {
    union {
        int64_t ob_refcnt_full;        // 64-bit platforms
        struct {
            uint32_t ob_refcnt;        // reference count
            uint16_t ob_overflow;      // overflow tracking
            uint16_t ob_flags;         // GC and type flags
        };
    };
    PyTypeObject *ob_type;             // pointer to type object
};
```

Variable-size objects (lists, tuples, etc.) extend this:
```c
struct PyVarObject {
    PyObject ob_base;
    Py_ssize_t ob_size;   // number of items
};
```

### Reference Counting

Objects are managed by reference counting (`Include/refcount.h`):
- `Py_INCREF(op)` — increments `ob_refcnt`
- `Py_DECREF(op)` — decrements; when it hits 0, calls `tp_dealloc`
- **Immortal objects**: refcount >= 2^31 are never deallocated (None, True, False, small ints)

**Free-threaded builds** use a different layout (see [Threading and GIL](04_threading_gil.md#free-threaded-object-layout)).

### The Type System

Types are themselves objects (`PyTypeObject` in `Include/cpython/object.h`). Each type defines:
- `tp_basicsize` / `tp_itemsize` — memory layout
- Method tables: `tp_as_number`, `tp_as_sequence`, `tp_as_mapping`
- `tp_traverse` / `tp_clear` — GC cycle detection hooks
- `tp_dealloc` — destructor

## Primitive Type Representations

### Integers (PyLongObject)

**File:** `Include/cpython/longintrepr.h`, `Objects/longobject.c`

```c
typedef struct _PyLongValue {
    uintptr_t lv_tag;       // sign + digit count encoded in bits
    digit ob_digit[1];      // variable-length array of digits
} _PyLongValue;

struct _longobject {
    PyObject_HEAD
    _PyLongValue long_value;
};
```

- Arbitrary precision using base 2^30 digits
- Sign in low 2 bits of `lv_tag` (0=positive, 1=zero, 2=negative)
- Digit count in high bits (`lv_tag >> 3`)
- **Small integer cache**: integers -5 to 256 are pre-allocated immortal singletons

### Floats (PyFloatObject)

**File:** `Include/cpython/floatobject.h`

```c
typedef struct {
    PyObject_HEAD
    double ob_fval;    // 64-bit IEEE double
} PyFloatObject;
```

Simple wrapper around C `double`.

### Strings (PyUnicodeObject)

**File:** `Include/cpython/unicodeobject.h`

Strings have three storage variants optimized by content:

| Variant | Use Case | Char Width |
|---|---|---|
| `PyASCIIObject` | ASCII-only strings | 1 byte (Py_UCS1) |
| `PyCompactUnicodeObject` | Non-ASCII, compact | 1, 2, or 4 bytes |
| `PyUnicodeObject` | Legacy/subclassed | Flexible |

```c
// ASCII-only (most common for code-related strings)
typedef struct {
    PyObject_HEAD
    Py_ssize_t length;
    Py_hash_t hash;                        // cached hash
    struct _PyUnicodeObject_state state;   // kind, interned, etc.
    // data follows immediately in memory
} PyASCIIObject;
```

The `kind` field determines character width: 1-byte (Latin-1), 2-byte (UCS-2), or 4-byte (UCS-4).

## Container Types

### Lists (PyListObject)

**File:** `Include/cpython/listobject.h`

```c
typedef struct {
    PyObject_VAR_HEAD
    PyObject **ob_item;       // heap-allocated array of pointers
    Py_ssize_t allocated;     // capacity (>= ob_size)
} PyListObject;
```

Over-allocates to amortize append cost. Resizing follows a growth pattern.

### Tuples (PyTupleObject)

**File:** `Include/cpython/tupleobject.h`

```c
typedef struct {
    PyObject_VAR_HEAD
    Py_hash_t ob_hash;         // cached hash
    PyObject *ob_item[1];      // inline variable-length array
} PyTupleObject;
```

Immutable, allocated once. Items are stored inline (no separate heap allocation).

### Dicts (PyDictObject)

**File:** `Include/cpython/dictobject.h`

```c
typedef struct {
    PyObject_HEAD
    Py_ssize_t ma_used;                // number of items
    uint64_t _ma_watcher_tag;          // mutation tracking
    PyDictKeysObject *ma_keys;         // hash table structure
    PyDictValues *ma_values;           // separate values (split table mode)
} PyDictObject;
```

Two layouts:
- **Combined table**: keys and values in a single hash table (general dicts)
- **Split table**: shared keys object, separate values array (instance `__dict__`s sharing the same attribute layout)

## Code Objects (PyCodeObject)

**File:** `Include/cpython/code.h:45-112`

The code object is the compiled representation of a block of Python code. It's the most important structure for tracing.

```c
struct PyCodeObject {
    PyObject_VAR_HEAD

    // === Hot fields (used in eval loop) ===
    PyObject *co_consts;            // tuple of constants
    PyObject *co_names;             // tuple of names (globals, attributes)
    PyObject *co_exceptiontable;    // exception handler mapping
    int co_flags;                   // CO_OPTIMIZED, CO_GENERATOR, etc.

    // === Parameter info ===
    int co_argcount;                // positional args (excl. *args)
    int co_posonlyargcount;         // positional-only args
    int co_kwonlyargcount;          // keyword-only args
    int co_stacksize;               // max eval stack depth

    // === Variable info ===
    int co_nlocals;                 // number of local variables
    int co_ncellvars;               // cell variables (for closures)
    int co_nfreevars;               // free variables (from closures)
    PyObject *co_localsplusnames;   // tuple: all local/cell/free var names
    PyObject *co_localspluskinds;   // bytes: kind of each variable

    // === Source mapping ===
    int co_firstlineno;             // first source line number
    PyObject *co_filename;          // source file path
    PyObject *co_name;              // function/class name
    PyObject *co_qualname;          // qualified name
    PyObject *co_linetable;         // bytecode offset -> source location

    // === Optimization & Monitoring ===
    _PyExecutorArray *co_executors; // Tier 2 JIT executors
    uintptr_t _co_instrumentation_version;
    struct _PyCoMonitoringData *_co_monitoring;
    int _co_firsttraceable;         // index of first traceable instruction

    // === Free-threaded support ===
    _PyCodeArray *co_tlbc;          // thread-local bytecode copies

    // === Bytecode (variable length) ===
    char co_code_adaptive[1];       // the actual bytecode + inline caches
};
```

### What's in `co_localsplusnames`

This tuple contains names in a specific order:
1. Parameters (positional, then keyword-only, then *args, **kwargs)
2. Other local variables
3. Cell variables (captured by inner functions)
4. Free variables (captured from outer scope)

The `co_localspluskinds` bytes indicate which category each entry belongs to.

## Function Objects (PyFunctionObject)

**File:** `Include/cpython/funcobject.h`

```c
typedef struct {
    PyObject_HEAD
    PyObject *func_globals;       // global namespace dict
    PyObject *func_builtins;      // builtins namespace
    PyObject *func_name;          // __name__
    PyObject *func_qualname;      // __qualname__
    PyObject *func_code;          // -> PyCodeObject
    PyObject *func_defaults;      // default arg values (tuple)
    PyObject *func_kwdefaults;    // keyword-only defaults (dict)
    PyObject *func_closure;       // closure cells (tuple of PyCellObject)
    PyObject *func_doc;           // docstring
    PyObject *func_dict;          // __dict__
    PyObject *func_module;        // __module__
    PyObject *func_annotations;   // type annotations
    vectorcallfunc vectorcall;    // fast calling convention
    uint32_t func_version;        // for specialization
} PyFunctionObject;
```

**Key relationship:** A function object wraps a code object with runtime context (globals, defaults, closures). Multiple function objects can share the same code object (e.g., a `def` inside a loop creates new function objects each iteration, but they all point to the same code object).

### Closures and Cell Objects

**File:** `Include/cpython/cellobject.h`

```c
typedef struct {
    PyObject_HEAD
    PyObject *ob_ref;    // the captured variable value
} PyCellObject;
```

When a function captures variables from an outer scope:
- The outer function stores those variables as **cell variables** (in `localsplus`)
- The inner function receives them as **free variables** (also in `localsplus`)
- Both point to the same `PyCellObject`, so mutations are shared

## Frames {#frames}

**File:** `Include/internal/pycore_interpframe_structs.h`

The `_PyInterpreterFrame` is the runtime execution context:

```c
struct _PyInterpreterFrame {
    _PyStackRef f_executable;                // code object
    struct _PyInterpreterFrame *previous;    // caller's frame
    _PyStackRef f_funcobj;                   // function object
    PyObject *f_globals;                     // globals dict
    PyObject *f_builtins;                    // builtins dict
    PyObject *f_locals;                      // locals dict (may be NULL)
    PyFrameObject *frame_obj;               // Python-visible frame (lazy)
    _Py_CODEUNIT *instr_ptr;               // current instruction
    _PyStackRef *stackpointer;             // top of eval stack
    uint16_t return_offset;
    char owner;                            // THREAD/GENERATOR/FRAME_OBJECT/INTERPRETER

    _PyStackRef localsplus[1];             // [locals | cells | free | stack]
};
```

### Frame Memory Layout

```
+-------------------+
| Specials          |  (f_executable, previous, f_funcobj, f_globals, ...)
+-------------------+
| Locals            |  localsplus[0..co_nlocals-1]
+-------------------+
| Cell variables    |  localsplus[co_nlocals..co_nlocals+co_ncellvars-1]
+-------------------+
| Free variables    |  localsplus[...+co_nfreevars-1]
+-------------------+
| Evaluation stack  |  localsplus[co_nlocalsplus..] up to stackpointer
+-------------------+
```

### Frame Allocation

Frames are allocated on a **per-thread contiguous stack** (`_PyThreadState_PushFrame` in `Python/pystate.c`), not on the heap. This gives excellent cache locality. Generator/coroutine frames are embedded in the generator object instead.

A Python-visible `PyFrameObject` is only created lazily when needed (e.g., `sys._getframe()`, tracebacks). When the stack-allocated frame is popped, the data is copied into the `PyFrameObject` if one exists.

## Memory Allocator

**Files:** `Include/pymem.h`, `Objects/mimalloc/`

CPython uses **mimalloc** (Microsoft's allocator) for object allocation:
- Thread-local heaps for concurrency
- Size-class-based allocation pools
- Excellent cache locality for small objects

Two API levels:
- `PyMem_Malloc/Free` — raw memory
- `PyObject_Malloc/Free` — object-aware (includes GC tracking)

## Implications for Tracing

### What we need to capture per event

For each traced execution step, we need to read:

| Data | Source | Cost |
|---|---|---|
| Current function | `frame->f_funcobj` | Pointer deref |
| Source location | `frame->instr_ptr` offset + `code->co_linetable` | Decode (see [Bytecode Mapping](03_bytecode_source_mapping.md)) |
| Local variables | `frame->localsplus[0..co_nlocals-1]` | Array scan + object reads |
| Variable names | `code->co_localsplusnames` | Tuple index |
| Globals (if needed) | `frame->f_globals` | Dict lookup |
| Call stack | `frame->previous` chain | Pointer walk |
| Return values | Top of eval stack | Stack peek |

### Key observations for efficiency

1. **Code objects are immutable** (for tracing purposes) — we can cache metadata derived from code objects.
2. **Frames are stack-allocated** — we must capture data before the frame is popped, or it's gone.
3. **Reference counting means values change** — if we want to snapshot object values, we need to either serialize immediately or `Py_INCREF` to keep them alive.
4. **Small integers are singletons** — for ints -5..256, we can store the value directly rather than snapshotting the object.
5. **String interning** — many strings (especially attribute names and variable names) are interned, so pointer equality can be used for comparison.
