"""
Experiment 9: Selective Variable Capture

Instead of reading ALL locals at every LINE event, pre-analyze bytecode
to know which variables each line writes, and only read those.

Approaches:
  A) Baseline: no tracing
  B) C settrace noop (minimum overhead floor)
  C) C settrace + GetLocals every LINE (read all vars — exp7 mode 4)
  D) Python settrace + selective GetVar (only vars written on this line)
  E) Python settrace + selective GetVar + cached previous values
  F) Python settrace + GetLocals + dict diff (call GetLocals, diff against cache)

The key comparison is C (read everything) vs D/E (read selectively).
Once we validate the approach in Python, the same logic can move to C.
"""
import sys
import os
import dis
import time
import opcode

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'exp7_c_extension'))

from workloads_large import LARGE_WORKLOADS, run_workload

ITERATIONS_OVERRIDE = None  # set to int to override auto-calibration


# ============================================================================
# Bytecode pre-analysis
# ============================================================================

STORE_OPS = set()
for name, op in opcode.opmap.items():
    if 'STORE_FAST' in name or name == 'STORE_NAME' or name == 'STORE_DEREF':
        STORE_OPS.add(op)


def analyze_code(code):
    """Pre-analyze a code object. Returns {line_number: set(varnames)}."""
    line_writes = {}
    for instr in dis.get_instructions(code):
        if instr.opcode in STORE_OPS and instr.positions.lineno is not None:
            line = instr.positions.lineno
            if line not in line_writes:
                line_writes[line] = set()
            line_writes[line].add(instr.argval)
    return line_writes


# Global code analysis cache
_code_cache = {}


def get_line_writes(code):
    """Get cached line->written_vars mapping for a code object."""
    cid = id(code)
    if cid not in _code_cache:
        _code_cache[cid] = analyze_code(code)
    return _code_cache[cid]


# ============================================================================
# Approach D: Selective GetVar — only read vars written on this line
# ============================================================================

class SelectiveTracer:
    def __init__(self):
        self.events = 0
        self.getvar_calls = 0
        self.var_cache = {}  # (frame_id, varname) -> last value
        self.captures = 0

    def trace_func(self, frame, event, arg):
        if event == 'call':
            self.events += 1
        elif event == 'line':
            self.events += 1
            code = frame.f_code
            line_writes = get_line_writes(code)
            written_vars = line_writes.get(frame.f_lineno)
            if written_vars:
                fid = id(frame)
                for varname in written_vars:
                    self.getvar_calls += 1
                    try:
                        val = frame.f_locals[varname]
                        cache_key = (fid, varname)
                        prev = self.var_cache.get(cache_key)
                        if val is not prev:
                            self.var_cache[cache_key] = val
                            self.captures += 1
                    except KeyError:
                        pass
        elif event == 'return':
            self.events += 1
            # Clean up frame cache
            fid = id(frame)
            to_remove = [k for k in self.var_cache if k[0] == fid]
            for k in to_remove:
                del self.var_cache[k]
        return self.trace_func

    def reset(self):
        e, g, c = self.events, self.getvar_calls, self.captures
        self.events = 0
        self.getvar_calls = 0
        self.captures = 0
        self.var_cache.clear()
        return e, g, c


# ============================================================================
# Approach E: Selective + only serialize on change
# ============================================================================

class SelectiveChangeTracer:
    """Like SelectiveTracer but explicitly tracks what changed."""
    def __init__(self):
        self.events = 0
        self.getvar_calls = 0
        self.changes_recorded = 0
        self.var_cache = {}

    def trace_func(self, frame, event, arg):
        if event == 'line':
            self.events += 1
            code = frame.f_code
            line_writes = get_line_writes(code)
            written_vars = line_writes.get(frame.f_lineno)
            if written_vars:
                fid = id(frame)
                for varname in written_vars:
                    self.getvar_calls += 1
                    try:
                        val = frame.f_locals[varname]
                        cache_key = (fid, varname)
                        prev = self.var_cache.get(cache_key)
                        if val is not prev:
                            self.var_cache[cache_key] = val
                            self.changes_recorded += 1
                            # Here we'd serialize — just count for now
                    except KeyError:
                        pass
        elif event == 'call':
            self.events += 1
        elif event == 'return':
            self.events += 1
            fid = id(frame)
            to_remove = [k for k in self.var_cache if k[0] == fid]
            for k in to_remove:
                del self.var_cache[k]
        return self.trace_func

    def reset(self):
        e, g, c = self.events, self.getvar_calls, self.changes_recorded
        self.events = 0
        self.getvar_calls = 0
        self.changes_recorded = 0
        self.var_cache.clear()
        return e, g, c


# ============================================================================
# Approach F: GetLocals + dict diff
# ============================================================================

class DictDiffTracer:
    """Call GetLocals once, diff against cached dict."""
    def __init__(self):
        self.events = 0
        self.changes_recorded = 0
        self.prev_locals = {}  # frame_id -> {name: value}

    def trace_func(self, frame, event, arg):
        if event == 'line':
            self.events += 1
            fid = id(frame)
            current = frame.f_locals
            prev = self.prev_locals.get(fid)
            if prev is None:
                # First time seeing this frame — everything is new
                self.prev_locals[fid] = dict(current)
                self.changes_recorded += len(current)
            else:
                # Diff
                for k, v in current.items():
                    if k not in prev or prev[k] is not v:
                        self.changes_recorded += 1
                        prev[k] = v
        elif event == 'call':
            self.events += 1
        elif event == 'return':
            self.events += 1
            fid = id(frame)
            self.prev_locals.pop(fid, None)
        return self.trace_func

    def reset(self):
        e, c = self.events, self.changes_recorded
        self.events = 0
        self.changes_recorded = 0
        self.prev_locals.clear()
        return e, c


# ============================================================================
# Reference: noop and full f_locals
# ============================================================================

def py_trace_noop(frame, event, arg):
    return py_trace_noop


def py_trace_flocals(frame, event, arg):
    if event == 'line':
        frame.f_locals
    return py_trace_flocals


# ============================================================================
# Main experiment
# ============================================================================

def main():
    try:
        import _ctrace
        has_ctrace = True
    except ImportError:
        has_ctrace = False
        print("WARNING: _ctrace not available")

    workload_names = list(LARGE_WORKLOADS.keys())

    # Calibrate iterations
    print("Calibrating...")
    iterations = {}
    for name in workload_names:
        if ITERATIONS_OVERRIDE:
            iterations[name] = ITERATIONS_OVERRIDE
            continue
        _, single = run_workload(name, iterations=1)
        target_ns = 200_000_000
        iters = max(2, target_ns // max(single, 1))
        iters = min(iters, 30)
        iterations[name] = iters

    # === Correctness check ===
    print("\n--- Correctness: selective capture ---")

    def test_func():
        x = 10
        y = 20
        z = x + y
        w = z * 2
        return w

    _code_cache.clear()
    analysis = analyze_code(test_func.__code__)
    print(f"  Bytecode analysis of test_func:")
    for line, vars_ in sorted(analysis.items()):
        print(f"    Line {line}: writes {vars_}")

    tracer = SelectiveTracer()
    sys.settrace(tracer.trace_func)
    result = test_func()
    sys.settrace(None)
    events, getvar_calls, captures = tracer.reset()
    print(f"  Result: {result} (expected 60)")
    print(f"  Events: {events}, GetVar calls: {getvar_calls}, Captures: {captures}")

    # Compare: how many GetVar calls would reading ALL locals need?
    nlocals = test_func.__code__.co_nlocals
    print(f"  co_nlocals={nlocals}, so reading all locals would be {nlocals * events} GetVar calls")
    print(f"  Selective: {getvar_calls} GetVar calls ({100*getvar_calls/max(nlocals*events,1):.0f}% of all-locals)")

    # === Measure GetVar savings across workloads ===
    print(f"\n--- GetVar call reduction analysis ---")
    print(f"{'Workload':<20} {'Events':>10} {'All GetVar':>12} {'Sel GetVar':>12} "
          f"{'Reduction':>10} {'Changes':>10}")
    print("-" * 76)

    _code_cache.clear()
    for name in workload_names:
        tracer = SelectiveTracer()
        sys.settrace(tracer.trace_func)
        LARGE_WORKLOADS[name]()
        sys.settrace(None)
        events, selective_calls, captures = tracer.reset()

        # Estimate "all locals" calls (need nlocals per line event)
        # We'll just use a rough estimate: events * avg_nlocals
        # More accurate: count during trace
        all_calls_est = events * 5  # rough average nlocals

        reduction = (1 - selective_calls / max(all_calls_est, 1)) * 100
        print(f"{name:<20} {events:>10,} {all_calls_est:>12,} {selective_calls:>12,} "
              f"{reduction:>9.0f}% {captures:>10,}")

    # === Performance comparison ===
    print(f"\n{'='*90}")
    print("Performance comparison")
    print(f"{'='*90}")

    configs = [
        ('baseline', 'Baseline'),
        ('py_noop', 'Py st noop'),
        ('py_flocals', 'Py st+floc'),
        ('selective', 'Selective'),
        ('sel_change', 'Sel+change'),
        ('dict_diff', 'Dict diff'),
    ]
    if has_ctrace:
        configs.extend([
            ('c_noop', 'C noop'),
            ('c_getloc', 'C GetLocals'),
        ])

    results = {cfg: {} for cfg, _ in configs}

    for cfg_key, cfg_label in configs:
        print(f"  Running {cfg_label}...")

        for name in workload_names:
            iters = iterations[name]

            if cfg_key == 'baseline':
                pass
            elif cfg_key == 'py_noop':
                sys.settrace(py_trace_noop)
            elif cfg_key == 'py_flocals':
                sys.settrace(py_trace_flocals)
            elif cfg_key == 'selective':
                t = SelectiveTracer()
                sys.settrace(t.trace_func)
            elif cfg_key == 'sel_change':
                t = SelectiveChangeTracer()
                sys.settrace(t.trace_func)
            elif cfg_key == 'dict_diff':
                t = DictDiffTracer()
                sys.settrace(t.trace_func)
            elif cfg_key == 'c_noop':
                _ctrace.start(0)
            elif cfg_key == 'c_getloc':
                _ctrace.start(4)

            _, per_iter = run_workload(name, iterations=iters)
            results[cfg_key][name] = per_iter

            if cfg_key.startswith('py_') or cfg_key == 'baseline':
                sys.settrace(None)
            elif cfg_key in ('selective', 'sel_change', 'dict_diff'):
                sys.settrace(None)
            elif cfg_key.startswith('c_'):
                _ctrace.stop()

    # === Print results ===
    print(f"\n{'='*90}")
    print("Overhead ratios (vs baseline)")
    print(f"{'='*90}")

    categories = {
        'comp_': 'compute', 'io_': 'io', 'mem_': 'memory',
        'yield_': 'yield', 'async_': 'async',
    }

    ratio_configs = [c for c in configs if c[0] != 'baseline']
    header = f"{'Workload':<20} {'Cat':<8}"
    for _, label in ratio_configs:
        header += f" {label:>12}"
    print(header)
    print("-" * len(header))

    for name in workload_names:
        cat = next((v for k, v in categories.items() if name.startswith(k)), '?')
        b = results['baseline'][name]
        row = f"{name:<20} {cat:<8}"
        for cfg_key, _ in ratio_configs:
            ratio = results[cfg_key][name] / b
            row += f" {ratio:>11.2f}x"
        print(row)

    # === Category averages ===
    print(f"\n{'='*90}")
    print("Category averages")
    print(f"{'='*90}")

    cat_workloads = {}
    for name in workload_names:
        cat = next((v for k, v in categories.items() if name.startswith(k)), '?')
        cat_workloads.setdefault(cat, []).append(name)

    header = f"{'Category':<12} {'#':>3}"
    for _, label in ratio_configs:
        header += f" {label:>12}"
    print(header)
    print("-" * len(header))

    for cat, names in cat_workloads.items():
        row = f"{cat:<12} {len(names):>3}"
        for cfg_key, _ in ratio_configs:
            ratios = [results[cfg_key][n] / results['baseline'][n] for n in names]
            avg = sum(ratios) / len(ratios)
            row += f" {avg:>11.2f}x"
        print(row)

    all_names = workload_names
    row = f"{'ALL':<12} {len(all_names):>3}"
    for cfg_key, _ in ratio_configs:
        ratios = [results[cfg_key][n] / results['baseline'][n] for n in all_names]
        avg = sum(ratios) / len(ratios)
        row += f" {avg:>11.2f}x"
    print(row)

    # Key comparison
    print(f"\n--- Key comparison: selective vs full capture ---")
    print(f"{'Workload':<20} {'Py floc':>10} {'Selective':>10} {'Dict diff':>10}"
          + (f" {'C GetLoc':>10}" if has_ctrace else ""))
    print("-" * (52 + (12 if has_ctrace else 0)))
    for name in workload_names:
        b = results['baseline'][name]
        row = f"{name:<20} {results['py_flocals'][name]/b:>9.2f}x"
        row += f" {results['selective'][name]/b:>9.2f}x"
        row += f" {results['dict_diff'][name]/b:>9.2f}x"
        if has_ctrace:
            row += f" {results['c_getloc'][name]/b:>9.2f}x"
        print(row)


if __name__ == '__main__':
    main()
