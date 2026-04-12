"""
Experiment 5: End-to-End Trace Capture Prototype

Combines PEP 669 monitoring with variable capture to produce a working
trace that can reconstruct execution. Tests correctness and measures
end-to-end overhead.

This uses settrace (which gives us frame access) rather than PEP 669
for the prototype, since PEP 669 LINE callbacks don't receive frames.

Architecture:
  - settrace callback captures events with minimal processing
  - Each event: (event_type, code_id, line_or_offset, timestamp, var_snapshot)
  - var_snapshot uses change detection + inline primitives

After capture, we reconstruct a step-through view and verify correctness.
"""
import sys
import time
import types
import dis
import os


class TraceCapture:
    """Full execution tracer using sys.settrace."""

    def __init__(self, max_events=1_000_000):
        self.events = []
        self.max_events = max_events
        self.code_cache = {}  # id(code) -> code metadata
        self.prev_locals = {}  # id(frame) -> {name: value} previous snapshot
        self.active = False

    def _cache_code(self, code):
        cid = id(code)
        if cid not in self.code_cache:
            self.code_cache[cid] = {
                'filename': code.co_filename,
                'qualname': code.co_qualname,
                'name': code.co_name,
                'firstlineno': code.co_firstlineno,
                'varnames': code.co_varnames,
                'nlocals': code.co_nlocals,
            }
        return cid

    def _snapshot_locals(self, frame):
        """Capture local variables with change detection."""
        fid = id(frame)
        prev = self.prev_locals.get(fid, {})

        current = frame.f_locals
        changes = {}

        for name, value in current.items():
            if name not in prev or prev[name] is not value:
                changes[name] = self._serialize_value(value)

        self.prev_locals[fid] = {k: v for k, v in current.items()}
        return changes if changes else None

    def _serialize_value(self, v):
        """Lightweight serialization of a value."""
        if v is None:
            return ('none',)
        if v is True:
            return ('bool', True)
        if v is False:
            return ('bool', False)

        t = type(v)
        if t is int:
            return ('int', v)
        if t is float:
            return ('float', v)
        if t is str:
            if len(v) <= 100:
                return ('str', v)
            return ('str', v[:100] + '...')
        if t is bytes:
            return ('bytes', len(v))
        if t is list:
            return ('list', len(v), id(v))
        if t is dict:
            return ('dict', len(v), id(v))
        if t is tuple:
            return ('tuple', len(v), id(v))

        return ('obj', t.__name__, id(v))

    def trace_func(self, frame, event, arg):
        if not self.active or len(self.events) >= self.max_events:
            return self.trace_func

        code = frame.f_code
        cid = self._cache_code(code)
        ts = time.perf_counter_ns()

        if event == 'call':
            changes = self._snapshot_locals(frame)
            self.events.append(('call', cid, frame.f_lineno, ts, changes))
        elif event == 'line':
            changes = self._snapshot_locals(frame)
            self.events.append(('line', cid, frame.f_lineno, ts, changes))
        elif event == 'return':
            self.events.append(('return', cid, frame.f_lineno, ts, {'__return__': self._serialize_value(arg)}))
            fid = id(frame)
            if fid in self.prev_locals:
                del self.prev_locals[fid]
        elif event == 'exception':
            exc_type, exc_value, _ = arg
            self.events.append(('exception', cid, frame.f_lineno, ts,
                                {'__exception__': ('exc', type(exc_value).__name__, str(exc_value))}))

        return self.trace_func

    def start(self):
        self.active = True
        sys.settrace(self.trace_func)

    def stop(self):
        sys.settrace(None)
        self.active = False

    def clear(self):
        self.events.clear()
        self.code_cache.clear()
        self.prev_locals.clear()

    def format_trace(self, max_lines=50):
        """Format captured trace as a human-readable step-through view."""
        lines = []
        for i, (evt, cid, lineno, ts, vars_) in enumerate(self.events[:max_lines]):
            meta = self.code_cache.get(cid, {})
            fname = meta.get('qualname', '?')
            filename = os.path.basename(meta.get('filename', '?'))

            if evt == 'call':
                indent = ">> "
                label = f"CALL {fname}"
            elif evt == 'return':
                indent = "<< "
                ret = vars_.get('__return__', ('?',)) if vars_ else ('?',)
                label = f"RETURN {fname} -> {ret}"
            elif evt == 'exception':
                indent = "!! "
                exc = vars_.get('__exception__', ('?',)) if vars_ else ('?',)
                label = f"EXCEPTION {exc}"
            else:
                indent = "   "
                label = f"LINE"

            var_str = ""
            if vars_ and evt in ('call', 'line'):
                var_parts = []
                for vname, vval in vars_.items():
                    if vval and len(vval) >= 2:
                        var_parts.append(f"{vname}={vval[1]}")
                    else:
                        var_parts.append(f"{vname}={vval}")
                var_str = " | " + ", ".join(var_parts) if var_parts else ""

            lines.append(f"{indent}{filename}:{lineno:<4} {label}{var_str}")

        if len(self.events) > max_lines:
            lines.append(f"  ... ({len(self.events) - max_lines} more events)")
        return "\n".join(lines)


# === Test Programs ===

def factorial(n):
    if n <= 1:
        return 1
    return n * factorial(n - 1)


def bubble_sort(arr):
    arr = list(arr)
    n = len(arr)
    for i in range(n):
        for j in range(0, n - i - 1):
            if arr[j] > arr[j + 1]:
                arr[j], arr[j + 1] = arr[j + 1], arr[j]
    return arr


def string_processing():
    words = "the quick brown fox jumps over the lazy dog".split()
    result = []
    for w in words:
        upper = w.upper()
        if len(upper) > 3:
            result.append(upper)
    return " ".join(result)


def exception_handling():
    results = []
    for i in range(10):
        try:
            if i % 3 == 0:
                raise ValueError(f"bad {i}")
            results.append(i * 2)
        except ValueError as e:
            results.append(-1)
    return results


def main():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from workloads import WORKLOADS, run_workload

    ITERATIONS = 20

    print("=" * 70)
    print("Experiment 5: End-to-End Trace Capture Prototype")
    print("=" * 70)

    tracer = TraceCapture(max_events=500_000)

    # --- Test 1: Correctness - factorial ---
    print("\n--- Test 1: Correctness - factorial(5) ---")
    tracer.clear()
    tracer.start()
    result = factorial(5)
    tracer.stop()
    print(f"Result: {result} (expected 120): {'PASS' if result == 120 else 'FAIL'}")
    print(f"Events captured: {len(tracer.events)}")
    print(f"Code objects seen: {len(tracer.code_cache)}")
    print(f"\nTrace:\n{tracer.format_trace(30)}")

    # --- Test 2: Correctness - bubble sort ---
    print("\n--- Test 2: Correctness - bubble_sort ---")
    tracer.clear()
    tracer.start()
    result = bubble_sort([5, 3, 8, 1, 2])
    tracer.stop()
    print(f"Result: {result} (expected [1,2,3,5,8]): {'PASS' if result == [1,2,3,5,8] else 'FAIL'}")
    print(f"Events captured: {len(tracer.events)}")
    print(f"\nTrace (first 25 events):\n{tracer.format_trace(25)}")

    # --- Test 3: Correctness - exceptions ---
    print("\n--- Test 3: Correctness - exception handling ---")
    tracer.clear()
    tracer.start()
    result = exception_handling()
    tracer.stop()
    print(f"Result: {result}")
    print(f"Events captured: {len(tracer.events)}")
    exception_events = [e for e in tracer.events if e[0] == 'exception']
    print(f"Exception events: {len(exception_events)}")
    print(f"\nTrace (first 30 events):\n{tracer.format_trace(30)}")

    # --- Test 4: Correctness verification ---
    print("\n--- Test 4: Verify trace captures variable changes ---")
    tracer.clear()
    tracer.start()
    result = string_processing()
    tracer.stop()
    print(f"Result: '{result}'")
    # Check that we captured variable assignments
    var_events = [e for e in tracer.events if e[4] is not None and e[0] in ('call', 'line')]
    var_names_seen = set()
    for _, _, _, _, changes in var_events:
        var_names_seen.update(changes.keys())
    print(f"Variables captured: {var_names_seen}")
    expected_vars = {'words', 'result', 'w', 'upper'}
    if expected_vars.issubset(var_names_seen):
        print(f"PASS: all expected variables ({expected_vars}) were captured")
    else:
        missing = expected_vars - var_names_seen
        print(f"FAIL: missing variables: {missing}")

    # --- Test 5: Performance overhead ---
    print("\n--- Test 5: End-to-end performance overhead ---")
    print(f"\n{'Workload':<15} {'Baseline (us)':>14} {'Traced (us)':>14} {'Overhead':>10} {'Events/iter':>12}")
    print("-" * 67)

    for wl_name in WORKLOADS:
        # Baseline
        _, baseline_ns = run_workload(wl_name, iterations=ITERATIONS)

        # Traced
        tracer.clear()
        tracer.start()
        _, traced_ns = run_workload(wl_name, iterations=ITERATIONS)
        tracer.stop()
        events_total = len(tracer.events)
        events_per_iter = events_total // ITERATIONS

        ratio = traced_ns / baseline_ns

        print(
            f"{wl_name:<15} "
            f"{baseline_ns/1000:>12.0f} "
            f"{traced_ns/1000:>14.0f} "
            f"{ratio:>9.1f}x "
            f"{events_per_iter:>12,}"
        )

    # --- Test 6: Data volume estimation ---
    print("\n--- Test 6: Data volume estimation ---")
    tracer.clear()
    tracer.start()
    from workloads import process_data
    process_data(range(1000))
    tracer.stop()

    total_events = len(tracer.events)
    # Estimate serialized size
    import pickle
    pickled = pickle.dumps(tracer.events)
    print(f"Events from process_data(1000): {total_events}")
    print(f"Pickled size: {len(pickled):,} bytes ({len(pickled)/total_events:.1f} bytes/event)")
    print(f"Extrapolation: 1M events = {len(pickled)/total_events * 1_000_000 / 1024/1024:.1f} MB (pickled)")

    events_with_vars = sum(1 for e in tracer.events if e[4] is not None)
    print(f"Events with variable changes: {events_with_vars}/{total_events} ({100*events_with_vars/total_events:.0f}%)")


if __name__ == '__main__':
    main()
