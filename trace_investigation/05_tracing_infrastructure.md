# Existing Tracing and Profiling Infrastructure

CPython already has extensive tracing infrastructure. Understanding it is critical because (a) we should build on it rather than reinvent it, and (b) it defines the boundaries of what's achievable without forking.

## Overview of Available Mechanisms

| Mechanism | Granularity | Overhead | Multiple Users | C Extension? |
|---|---|---|---|---|
| sys.settrace | Line/call/return | High (~30x) | No (1 per thread) | Via Python callback |
| sys.setprofile | Call/return only | Moderate | No (1 per thread) | Via Python callback |
| PEP 669 (sys.monitoring) | Bytecode events | Low (~2-4x for active events) | Yes (up to 8 tools) | Via Python callback |
| DTrace/SystemTap | Function/line probes | Very low | Yes | Kernel-level |
| _lsprof (cProfile) | Call/return timing | Low-moderate | No | C module |

## 1. Legacy: sys.settrace / sys.setprofile

**Files:** `Python/legacy_tracing.c`, `Python/sysmodule.c`

### Thread State Storage

```c
// Include/cpython/pystate.h:147-150
typedef int (*Py_tracefunc)(PyObject *, PyFrameObject *, int, PyObject *);

Py_tracefunc c_profilefunc;
Py_tracefunc c_tracefunc;
PyObject *c_profileobj;
PyObject *c_traceobj;
```

### API

- `sys.settrace(func)` — set trace function for current thread
- `sys.setprofile(func)` — set profile function for current thread
- `sys._settraceallthreads(func)` — set for all threads (3.12+)
- `sys._setprofileallthreads(func)` — set for all threads (3.12+)

### Events

The trace callback receives:
- `PyTrace_CALL` — function entry
- `PyTrace_RETURN` — function return (with return value)
- `PyTrace_LINE` — new source line
- `PyTrace_EXCEPTION` — exception raised
- `PyTrace_OPCODE` — each bytecode instruction (if `frame.f_trace_opcodes = True`)
- `PyTrace_C_CALL/C_RETURN/C_EXCEPTION` — C function calls

### Implementation (Post-3.12)

Since Python 3.12, `sys.settrace` is **implemented on top of PEP 669 monitoring**. It registers as tool ID 7 (`PY_MONITORING_SYS_TRACE_ID`). The trampoline in `legacy_tracing.c` converts PEP 669 events to legacy `PyTrace_*` events.

### Per-Frame Control

```c
frame->f_trace          // trace function for this specific frame
frame->f_trace_lines    // emit LINE events?
frame->f_trace_opcodes  // emit OPCODE events?
```

### Limitations

- **Only one trace function per thread** — can't compose tracers
- **Python callback overhead** — each event calls into Python, ~microseconds per event
- **Frame materialization** — creates `PyFrameObject` for every call (normally lazy)
- **~30x slowdown** typical for line-level tracing

## 2. PEP 669: sys.monitoring (Python 3.12+)

**Files:** `Python/instrumentation.c` (~3000 lines), `Include/internal/pycore_instruments.h`

This is the modern, efficient monitoring system. It's the foundation we should build on.

### Architecture

PEP 669 works by **rewriting bytecode in-place**. When a tool registers for events:
1. The interpreter replaces relevant opcodes with `INSTRUMENTED_*` variants
2. The instrumented opcodes fire callbacks before executing the original logic
3. When no tools need an event, bytecode is restored to original opcodes

This gives **zero overhead when no monitoring is active**.

### Tool System

Up to 8 tools can be active simultaneously, identified by tool IDs:

```c
#define PY_MONITORING_DEBUGGER_ID    0
#define PY_MONITORING_COVERAGE_ID    1
#define PY_MONITORING_PROFILER_ID    2
// IDs 3-4 available for custom tools
#define PY_MONITORING_OPTIMIZER_ID   5
#define PY_MONITORING_SYS_PROFILE_ID 6   // reserved for sys.setprofile
#define PY_MONITORING_SYS_TRACE_ID   7   // reserved for sys.settrace
```

### Events

**Local events** (require bytecode instrumentation):

| Event | ID | Description |
|---|---|---|
| `PY_START` | 0 | Python function entry |
| `PY_RESUME` | 1 | Generator/coroutine resume |
| `PY_RETURN` | 2 | Function return |
| `PY_YIELD` | 3 | Generator yield |
| `CALL` | 4 | Any call (Python or C) |
| `LINE` | 5 | New source line |
| `INSTRUCTION` | 6 | Every bytecode instruction |
| `JUMP` | 7 | Unconditional jump |
| `BRANCH_LEFT` | 8 | Conditional branch taken |
| `BRANCH_RIGHT` | 9 | Conditional branch not taken |
| `STOP_ITERATION` | 10 | StopIteration in for loop |

**Global events** (no bytecode modification needed):

| Event | ID | Description |
|---|---|---|
| `RAISE` | 11 | Exception raised |
| `EXCEPTION_HANDLED` | 12 | Exception caught |
| `PY_UNWIND` | 13 | Stack unwinding |
| `PY_THROW` | 14 | Generator.throw() |
| `RERAISE` | 15 | Exception re-raised |
| `C_RETURN` | 16 | C function returned |
| `C_RAISE` | 17 | C function raised |

### API

```python
import sys

# Claim a tool ID
sys.monitoring.use_tool_id(tool_id, "my_tracer")

# Register callbacks
sys.monitoring.register_callback(tool_id, sys.monitoring.events.LINE, my_line_handler)
sys.monitoring.register_callback(tool_id, sys.monitoring.events.PY_START, my_call_handler)

# Activate events (bitmask)
events = sys.monitoring.events.LINE | sys.monitoring.events.PY_START | sys.monitoring.events.PY_RETURN
sys.monitoring.set_events(tool_id, events)

# Per-code-object events
sys.monitoring.set_local_events(tool_id, code_object, events)

# Deactivate
sys.monitoring.set_events(tool_id, 0)
sys.monitoring.free_tool_id(tool_id)
```

### Callback Signatures

```python
def py_start_handler(code: types.CodeType, instruction_offset: int) -> DISABLE | None: ...
def line_handler(code: types.CodeType, line_number: int) -> DISABLE | None: ...
def py_return_handler(code: types.CodeType, instruction_offset: int, retval: object) -> DISABLE | None: ...
def call_handler(code: types.CodeType, instruction_offset: int, callable: object, arg0: object) -> DISABLE | None: ...
```

Returning `sys.monitoring.DISABLE` removes instrumentation for that specific (tool, code, offset) triple — powerful for selective tracing.

### Per-Code Monitoring Data

```c
struct _PyCoMonitoringData {
    _Py_LocalMonitors local_monitors;           // per-code monitors
    _Py_LocalMonitors active_monitors;          // combined local + global
    uint8_t *tools;                             // per-instruction tool bits
    uintptr_t tool_versions[8];                 // version tracking per tool
    _PyCoLineInstrumentationData *lines;        // line mapping for instrumented ops
    uint8_t *line_tools;                        // per-instruction line event tools
    uint8_t *per_instruction_opcodes;           // for INSTRUMENTED_INSTRUCTION
    uint8_t *per_instruction_tools;             // per-instruction tools
};
```

### Instrumented Opcodes (21 total)

Key examples:
- `INSTRUMENTED_RESUME` (244) — fires PY_START/PY_RESUME
- `INSTRUMENTED_RETURN_VALUE` (245) — fires PY_RETURN
- `INSTRUMENTED_CALL` (249) — fires CALL
- `INSTRUMENTED_LINE` (253) — fires LINE
- `INSTRUMENTED_INSTRUCTION` (237) — fires INSTRUCTION
- `INSTRUMENTED_JUMP_BACKWARD` (252) — fires JUMP
- `INSTRUMENTED_POP_JUMP_IF_*` (240-243) — fires BRANCH_LEFT/RIGHT

### Internal Callback Flow

```
INSTRUMENTED_* opcode executes
    |
    v
_Py_call_instrumentation*()          (instrumentation.c)
    |
    v
call_instrumentation_vector()
    |-- Check tstate->tracing (prevent recursion)
    |-- Get tools bitmask for this instruction
    |-- For each active tool (MSB first):
    |       |
    |       v
    |   call_one_instrument()
    |       |-- Get callback from interp->monitoring_callables[tool][event]
    |       |-- Set tstate->tracing++
    |       |-- Call _PyObject_VectorcallTstate(callback, args...)
    |       |-- Decrement tstate->tracing
    |       |-- Check for DISABLE return value
    |
    v
Execute original instruction logic
```

### Instrumentation Version Tracking

```c
interp->ceval.instrumentation_version    // global version counter
code->_co_instrumentation_version        // per-code version
// When versions mismatch, bytecode is re-instrumented
```

This allows lazy re-instrumentation — bytecode is only updated when actually executed after a monitoring change.

### Thread Safety

For free-threaded builds, instrumentation changes use stop-the-world:
```c
_PyEval_StopTheWorld(interp);
// ... modify bytecode ...
_PyEval_StartTheWorld(interp);
```

During normal execution, `tstate->tracing` prevents recursive monitoring (the counter is thread-local, no synchronization needed).

## 3. DTrace/SystemTap Probes

**Files:** `Include/pydtrace.h`, `Include/pydtrace.d`

Available probes (when compiled `WITH_DTRACE`):

```
python:::function__entry(filename, funcname, lineno)
python:::function__return(filename, funcname, lineno)
python:::line(filename, funcname, lineno)
python:::gc__start(generation)
python:::gc__done(collected)
python:::import__find__load__start(name)
python:::import__find__load__done(name, success)
python:::audit(event, arg)
python:::instance__new__start(classname, module)
python:::instance__new__done(classname, module)
python:::instance__delete__start(classname, module)
python:::instance__delete__done(classname, module)
```

**Characteristics:**
- Near-zero cost when disabled (probe check is a single memory load)
- System-level: works with OS tracing tools
- Cannot capture Python object values (only strings and integers)
- Requires kernel support (DTrace on macOS/Solaris, SystemTap on Linux)

## 4. _lsprof (cProfile)

**File:** `Modules/_lsprof.c`

A C-implemented deterministic profiler that uses PEP 669 monitoring:

```c
// Registers for these events:
PY_START, PY_RESUME, PY_THROW    → ptrace_enter_call()
PY_RETURN, PY_YIELD, PY_UNWIND   → ptrace_leave_call()
CALL, C_RETURN, C_RAISE           → tracks builtin calls
```

Uses a rotating tree for O(log n) per-function lookup. Tracks:
- Call count, recursive call count
- Total time, inline time (excluding subcalls)
- Per-caller breakdown (subcalls)

**Timer:** Uses `PyTime_PerfCounterRaw()` by default (nanosecond resolution).

## Performance Characteristics

### Measured Overhead Patterns

| Mechanism | No Monitoring | LINE events | INSTRUCTION events |
|---|---|---|---|
| Base interpreter | 1x | 1x | 1x |
| sys.settrace | 1x | ~30x | ~100x |
| PEP 669 (Python callback) | 1x | ~3-5x | ~10-20x |
| PEP 669 (C callback via ext) | 1x | ~1.5-2x | ~3-5x |
| DTrace probes | ~1x | ~1.1x | N/A |

The key insight: **PEP 669 with a C extension callback is dramatically faster than sys.settrace**, and approaches DTrace-level overhead for call/return events.

## Implications for Our Tracer

### Build on PEP 669

PEP 669 is clearly the right foundation:
1. **Zero overhead when inactive** — no cost when not tracing
2. **Selective instrumentation** — can trace specific code objects, disable per-instruction
3. **Multiple tools** — doesn't conflict with debuggers or profilers
4. **Bytecode-level precision** — can trace at instruction granularity
5. **Already thread-safe** — handles free-threaded builds

### C Extension for Callbacks

The biggest performance bottleneck is the callback itself. A Python callback for every LINE event is ~3-5x overhead; a C callback can be ~1.5-2x. For INSTRUCTION-level events, the difference is even larger.

**This strongly suggests implementing the trace callback as a C extension module.**

### What Events to Monitor

For full execution trace reconstruction, we need at minimum:
- `PY_START` + `PY_RETURN` — call/return pairs
- `LINE` — line-level stepping
- `PY_YIELD` + `PY_RESUME` — generator state
- `RAISE` + `EXCEPTION_HANDLED` — exception flow

For instruction-level detail:
- `INSTRUCTION` — every bytecode op (expensive but complete)
- `BRANCH_LEFT` + `BRANCH_RIGHT` — control flow decisions
- `JUMP` — unconditional jumps

### The DISABLE Optimization

PEP 669 callbacks can return `DISABLE` to remove instrumentation for a specific instruction. This is powerful for:
- Skipping standard library code
- Disabling tracing in hot loops after capturing enough data
- Implementing "record this function only" semantics

### Key Files Reference

| File | Purpose |
|---|---|
| `Python/instrumentation.c` | PEP 669 implementation (~3000 lines) |
| `Python/legacy_tracing.c` | sys.settrace/setprofile wrapper (~775 lines) |
| `Include/internal/pycore_instruments.h` | Monitoring data structures |
| `Include/cpython/monitoring.h` | Public monitoring C API |
| `Modules/_lsprof.c` | cProfile implementation (~1200 lines) |
| `Include/pydtrace.h` | DTrace probe definitions |
| `Python/sysmodule.c:1086-1290` | sys.settrace/setprofile Python API |
