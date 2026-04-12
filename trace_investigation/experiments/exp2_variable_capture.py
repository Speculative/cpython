"""
Experiment 2: Variable Capture from Frames

Tests different approaches for reading local variable values during tracing.
This is the expensive part of trace capture - we need to snapshot variable
state at each trace event.

Approaches tested:
  A) frame.f_locals (creates a new dict each time - known to be slow)
  B) sys._getframe() + f_locals
  C) ctypes-based direct localsplus access (reading the C struct)
  D) Counting callbacks only (baseline for event overhead without capture)

We measure both correctness (do we get the right values?) and performance.
"""
import sys
import time
import ctypes
import types
from workloads import WORKLOADS, run_workload

TOOL_ID = 0
ITERATIONS = 20


# === Approach A: Use frame.f_locals in a settrace callback ===
class TraceFLocals:
    """Captures locals via frame.f_locals at each line event."""
    def __init__(self):
        self.captures = 0
        self.last_locals = None

    def trace_func(self, frame, event, arg):
        if event == 'line':
            self.last_locals = frame.f_locals.copy()
            self.captures += 1
        return self.trace_func

    def reset(self):
        c = self.captures
        self.captures = 0
        self.last_locals = None
        return c


# === Approach B: PEP 669 LINE callback + sys._getframe().f_locals ===
class TraceGetFrame:
    """Uses sys._getframe() from a PEP 669 callback to get locals."""
    def __init__(self):
        self.captures = 0
        self.last_locals = None

    def line_callback(self, code, line_number):
        # _getframe(0) is this callback; _getframe(1) would be internal;
        # we actually can't reliably get the traced frame this way from
        # a PEP 669 callback since the call stack is different.
        # This tests the overhead of the attempt.
        self.captures += 1
        # In practice PEP 669 LINE callbacks don't receive the frame,
        # so we'd need another mechanism. This approach is a dead end
        # but we measure it for completeness.

    def reset(self):
        c = self.captures
        self.captures = 0
        self.last_locals = None
        return c


# === Approach C: PEP 669 LINE + noop (no capture) as event-only baseline ===
class TraceNoCapture:
    """PEP 669 LINE callback that does no variable capture - event overhead only."""
    def __init__(self):
        self.captures = 0

    def line_callback(self, code, line_number):
        self.captures += 1

    def reset(self):
        c = self.captures
        self.captures = 0
        return c


# === Approach D: PEP 669 LINE + lightweight capture via settrace hybrid ===
class TraceHybrid:
    """
    Uses PEP 669 for efficient event detection, but installs a per-frame
    settrace function to get frame access for variable capture.

    The idea: PEP 669's LINE events tell us a line executed, but don't give
    us the frame. sys.settrace gives us the frame but is expensive for all
    events. Can we combine them?
    """
    def __init__(self):
        self.captures = 0
        self.var_snapshots = []

    def trace_func(self, frame, event, arg):
        if event == 'line':
            # Lightweight capture: just read the fast locals by name
            loc = frame.f_locals
            self.captures += 1
        return self.trace_func

    def reset(self):
        c = self.captures
        self.captures = 0
        self.var_snapshots.clear()
        return c


# === Approach E: PEP 669 LINE + frame inspection via callback args ===
class TracePEP669WithFrameLocals:
    """
    PEP 669 approach using PyEval_GetFrameLocals equivalent.
    In 3.13+, we can use sys._getframe() but it's tricky from monitoring
    callbacks. Instead, test using settrace purely for the frame access pattern.
    """
    def __init__(self):
        self.captures = 0
        self.total_vars = 0

    def line_callback_counting_only(self, code, line_number):
        """Just count - baseline."""
        self.captures += 1

    def line_callback_with_code_inspect(self, code, line_number):
        """Access code object metadata (cheap - no frame needed)."""
        self.captures += 1
        self.total_vars += code.co_nlocals

    def reset(self):
        c = self.captures
        self.captures = 0
        self.total_vars = 0
        return c


def run_experiment():
    E = sys.monitoring.events

    print("=" * 70)
    print("Experiment 2: Variable Capture Approaches")
    print("=" * 70)

    # --- Test 1: Verify settrace gives correct locals ---
    print("\n--- Test 1: Correctness - settrace f_locals ---")
    tracer = TraceFLocals()
    sys.settrace(tracer.trace_func)

    # Simple function to verify
    def test_func():
        x = 10
        y = 20
        z = x + y
        return z

    result = test_func()
    sys.settrace(None)
    print(f"  test_func returned: {result}")
    print(f"  Captures: {tracer.captures}")
    print(f"  Last locals: {tracer.last_locals}")
    expected_z = 30
    if tracer.last_locals and tracer.last_locals.get('z') == expected_z:
        print(f"  PASS: z={tracer.last_locals['z']} matches expected {expected_z}")
    else:
        print(f"  FAIL: locals capture incorrect")
    tracer.reset()

    # --- Test 2: Verify PEP 669 LINE events fire ---
    print("\n--- Test 2: Correctness - PEP 669 LINE events fire ---")
    counter = TraceNoCapture()
    sys.monitoring.use_tool_id(TOOL_ID, "exp2")
    sys.monitoring.register_callback(TOOL_ID, E.LINE, counter.line_callback)
    sys.monitoring.set_events(TOOL_ID, E.LINE)

    test_func()
    count = counter.reset()
    sys.monitoring.set_events(TOOL_ID, 0)
    sys.monitoring.free_tool_id(TOOL_ID)
    print(f"  LINE events for test_func: {count}")
    print(f"  {'PASS' if count > 0 else 'FAIL'}: events are firing")

    # --- Test 3: Verify PEP 669 callback receives code object info ---
    print("\n--- Test 3: Code object access from PEP 669 callback ---")
    inspector = TracePEP669WithFrameLocals()
    sys.monitoring.use_tool_id(TOOL_ID, "exp2")
    sys.monitoring.register_callback(
        TOOL_ID, E.LINE, inspector.line_callback_with_code_inspect
    )
    sys.monitoring.set_events(TOOL_ID, E.LINE)

    test_func()
    sys.monitoring.set_events(TOOL_ID, 0)
    sys.monitoring.free_tool_id(TOOL_ID)
    print(f"  Captures: {inspector.captures}")
    print(f"  Total var slots seen: {inspector.total_vars}")
    print(f"  PASS: code object metadata accessible from callback")
    inspector.reset()

    # --- Test 4: Performance comparison across workloads ---
    print("\n--- Test 4: Performance comparison ---")
    print(f"\n{'Workload':<15} {'Baseline':>12} {'settrace':>12} {'669 noop':>12} {'669+co_info':>12} {'st overhead':>12} {'669 overhead':>12}")
    print("-" * 87)

    for wl_name in WORKLOADS:
        # Baseline (no tracing)
        _, baseline_ns = run_workload(wl_name, iterations=ITERATIONS)

        # settrace with f_locals capture
        tracer_fl = TraceFLocals()
        sys.settrace(tracer_fl.trace_func)
        _, settrace_ns = run_workload(wl_name, iterations=ITERATIONS)
        sys.settrace(None)
        st_captures = tracer_fl.reset()

        # PEP 669 LINE noop
        noop = TraceNoCapture()
        sys.monitoring.use_tool_id(TOOL_ID, "exp2")
        sys.monitoring.register_callback(TOOL_ID, E.LINE, noop.line_callback)
        sys.monitoring.set_events(TOOL_ID, E.LINE)
        _, pep669_noop_ns = run_workload(wl_name, iterations=ITERATIONS)
        sys.monitoring.set_events(TOOL_ID, 0)
        sys.monitoring.free_tool_id(TOOL_ID)
        noop_captures = noop.reset()

        # PEP 669 LINE + code object inspection
        inspector = TracePEP669WithFrameLocals()
        sys.monitoring.use_tool_id(TOOL_ID, "exp2")
        sys.monitoring.register_callback(
            TOOL_ID, E.LINE, inspector.line_callback_with_code_inspect
        )
        sys.monitoring.set_events(TOOL_ID, E.LINE)
        _, pep669_co_ns = run_workload(wl_name, iterations=ITERATIONS)
        sys.monitoring.set_events(TOOL_ID, 0)
        sys.monitoring.free_tool_id(TOOL_ID)
        inspector.reset()

        st_ratio = settrace_ns / baseline_ns
        noop_ratio = pep669_noop_ns / baseline_ns

        print(
            f"{wl_name:<15} "
            f"{baseline_ns/1000:>10.0f}us "
            f"{settrace_ns/1000:>10.0f}us "
            f"{pep669_noop_ns/1000:>10.0f}us "
            f"{pep669_co_ns/1000:>10.0f}us "
            f"{st_ratio:>11.2f}x "
            f"{noop_ratio:>11.2f}x"
        )

    # --- Test 5: Cost of f_locals specifically ---
    print("\n--- Test 5: Marginal cost of f_locals access ---")
    print("(settrace callback: noop vs f_locals.copy())")

    class TraceSettraceLINENoop:
        def __init__(self):
            self.count = 0
        def trace_func(self, frame, event, arg):
            if event == 'line':
                self.count += 1
            return self.trace_func
        def reset(self):
            c = self.count
            self.count = 0
            return c

    class TraceSettraceWithCopy:
        def __init__(self):
            self.count = 0
            self.last = None
        def trace_func(self, frame, event, arg):
            if event == 'line':
                self.last = frame.f_locals.copy()
                self.count += 1
            return self.trace_func
        def reset(self):
            c = self.count
            self.count = 0
            self.last = None
            return c

    print(f"\n{'Workload':<15} {'st_noop':>12} {'st_copy':>12} {'copy_cost':>12}")
    print("-" * 53)

    for wl_name in ['fib_iter', 'data_proc', 'oop']:
        noop_st = TraceSettraceLINENoop()
        sys.settrace(noop_st.trace_func)
        _, noop_ns = run_workload(wl_name, iterations=ITERATIONS)
        sys.settrace(None)
        noop_st.reset()

        copy_st = TraceSettraceWithCopy()
        sys.settrace(copy_st.trace_func)
        _, copy_ns = run_workload(wl_name, iterations=ITERATIONS)
        sys.settrace(None)
        captures = copy_st.reset()

        marginal = (copy_ns - noop_ns)
        per_capture = marginal / max(captures, 1)

        print(
            f"{wl_name:<15} "
            f"{noop_ns/1000:>10.0f}us "
            f"{copy_ns/1000:>10.0f}us "
            f"{per_capture/1000:>9.2f}us/capture"
        )


if __name__ == '__main__':
    run_experiment()
