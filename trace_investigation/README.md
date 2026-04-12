# CPython Execution Tracing Investigation

This wiki documents an investigation into CPython internals for the purpose of implementing full execution tracing — capturing enough information during Python program execution to reconstruct a step-through debugging view after the fact.

## Documents

### Core Internals

1. **[Execution Pipeline](01_execution_pipeline.md)** — How Python source code is compiled to bytecode and executed. Covers tokenization, parsing, AST, symbol tables, compilation, assembly, and the eval loop.

2. **[Object and Value Storage](02_object_storage.md)** — How CPython represents objects in memory. Covers PyObject, reference counting, primitive types (int, float, str), containers (list, dict, tuple), code objects, function objects, closures, and frames.

3. **[Bytecode-to-Source Mapping](03_bytecode_source_mapping.md)** — How bytecode instructions map back to exact source locations (line and column). Covers the co_linetable format, encoding schemes, C and Python APIs for location lookup.

4. **[Threading and GIL](04_threading_gil.md)** — CPython's threading model including the free-threaded (no-GIL) build. Covers GIL state, thread attachment, per-object locks, biased reference counting, stop-the-world, and thread-local bytecode.

### Tracing Strategy

5. **[Existing Tracing Infrastructure](05_tracing_infrastructure.md)** — What CPython already provides for tracing and profiling. Covers sys.settrace, PEP 669 (sys.monitoring), DTrace probes, and cProfile/_lsprof. Includes performance characteristics.

6. **[Trace Capture Strategy](06_trace_capture_strategy.md)** — Synthesis and recommendations. Covers what data to capture, extension vs. fork decision, architecture design, event encoding, value serialization, and performance estimates.

## Key Findings Summary

### Can we do this with a native extension?

**Yes.** PEP 669 (`sys.monitoring`, Python 3.12+) provides a low-overhead event system that gives us zero cost when inactive and ~2-5x overhead for line-level tracing with C callbacks. This is sufficient for our needs. **A CPython fork is not required.**

### What events do we need?

| Event | Purpose | PEP 669 Support |
|---|---|---|
| Function entry/exit | Call stack reconstruction | `PY_START`, `PY_RETURN` |
| Line execution | Step-through view | `LINE` |
| Variable values | State inspection | Read from `frame->localsplus` |
| Exceptions | Error flow | `RAISE`, `EXCEPTION_HANDLED` |
| Generator yield/resume | Async flow | `PY_YIELD`, `PY_RESUME` |
| Branch decisions | Control flow detail | `BRANCH_LEFT`, `BRANCH_RIGHT` |

### What's the expected overhead?

- **Line-level tracing with C extension:** ~2-5x slowdown
- **Instruction-level tracing:** ~5-15x slowdown
- **Call/return only:** ~1.1-1.5x slowdown

### What about free-threaded Python?

The design uses per-thread ring buffers and PEP 669's built-in thread safety. It works with both GIL and free-threaded builds. Concurrent event streams are merged post-hoc using timestamps.

## Original Task

See [TASK.md](TASK.md) for the original investigation prompt.
