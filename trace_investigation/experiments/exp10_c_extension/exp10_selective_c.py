"""
Experiment 10: C-Level Selective Variable Capture

Pre-computes bytecode analysis in Python (once per code object),
passes it to a C extension, which then uses it on every trace event
to only read variables that the current line writes to.

Compares:
  - C noop (floor)
  - C selective (pre-computed maps + targeted GetVar)
  - C GetLocals diff (read all locals as dict)
  - Python settrace noop (reference)
  - Python settrace + f_locals (reference)
"""
import sys
import os
import dis
import time
import types
import opcode

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '..', 'exp7_c_extension'))

from workloads_large import LARGE_WORKLOADS, run_workload
import _ctrace2

# Also import _ctrace for comparison
try:
    import _ctrace
    has_ctrace1 = True
except ImportError:
    has_ctrace1 = False


# ============================================================================
# Bytecode analysis (Python side, run once per code object)
# ============================================================================

STORE_OPS = set()
for name, op in opcode.opmap.items():
    if 'STORE_FAST' in name or name == 'STORE_NAME' or name == 'STORE_DEREF':
        STORE_OPS.add(op)


def analyze_and_register(code, visited=None):
    """Analyze a code object and register it with the C extension.
    Recursively handles nested code objects (inner functions, classes)."""
    if visited is None:
        visited = set()
    if id(code) in visited:
        return
    visited.add(id(code))

    # Build line -> variable index bitmask
    varnames = code.co_varnames
    varname_to_idx = {name: i for i, name in enumerate(varnames)}

    line_bitmasks = {}  # line -> bitmask
    for instr in dis.get_instructions(code):
        if instr.opcode in STORE_OPS and instr.positions.lineno is not None:
            line = instr.positions.lineno
            idx = varname_to_idx.get(instr.argval)
            if idx is not None and idx < 64:
                if line not in line_bitmasks:
                    line_bitmasks[line] = 0
                line_bitmasks[line] |= (1 << idx)

    # Pack as list of (line, bitmask) tuples
    first_line = code.co_firstlineno
    packed = [(line, mask) for line, mask in line_bitmasks.items()]

    _ctrace2.register_code(code, first_line, packed)

    # Recurse into nested code objects
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            analyze_and_register(const, visited)


def register_all_workload_code():
    """Pre-register all code objects from workload functions."""
    import workloads_large
    registered = set()

    for name, fn in vars(workloads_large).items():
        if callable(fn) and hasattr(fn, '__code__'):
            if id(fn.__code__) not in registered:
                analyze_and_register(fn.__code__)
                registered.add(id(fn.__code__))

    # Also register code for lambdas in LARGE_WORKLOADS
    for name, fn in LARGE_WORKLOADS.items():
        if hasattr(fn, '__code__'):
            if id(fn.__code__) not in registered:
                analyze_and_register(fn.__code__)
                registered.add(id(fn.__code__))


# ============================================================================
# Reference: Python settrace
# ============================================================================

def py_trace_noop(frame, event, arg):
    return py_trace_noop

def py_trace_flocals(frame, event, arg):
    if event == 'line':
        frame.f_locals
    return py_trace_flocals


# ============================================================================
# Main
# ============================================================================

def main():
    workload_names = list(LARGE_WORKLOADS.keys())

    # Calibrate
    print("Calibrating...")
    iterations = {}
    for name in workload_names:
        _, single = run_workload(name, iterations=1)
        target_ns = 200_000_000
        iters = max(2, target_ns // max(single, 1))
        iters = min(iters, 30)
        iterations[name] = iters

    # Register all code objects with C extension
    print("Pre-registering code objects...")
    _ctrace2.clear()
    register_all_workload_code()
    stats = _ctrace2.stats()
    print(f"  Registered {stats['registered_codes']} code objects")

    # === Correctness check ===
    print("\n--- Correctness check ---")

    def test_target():
        x = 10
        y = 20
        z = x + y
        return z

    analyze_and_register(test_target.__code__)

    _ctrace2.start(1)  # selective mode
    result = test_target()
    _ctrace2.stop()
    stats = _ctrace2.stats()
    print(f"  Result: {result} (expected 30)")
    print(f"  Stats: {stats}")
    print(f"  Events: {stats['events']}, Lines w/ writes: {stats['lines_with_writes']}, "
          f"Vars checked: {stats['vars_checked']}, Changed: {stats['vars_changed']}")

    # === Performance comparison ===
    print(f"\n{'='*90}")
    print("Performance comparison")
    print(f"{'='*90}")

    configs = [
        ('baseline', 'Baseline', None),
        ('c_noop', 'C noop', lambda: _ctrace2.start(0)),
        ('c_selective', 'C selective', lambda: _ctrace2.start(1)),
        ('c_getloc', 'C GetLocals', lambda: _ctrace2.start(2)),
        ('py_noop', 'Py st noop', lambda: sys.settrace(py_trace_noop)),
        ('py_floc', 'Py st+floc', lambda: sys.settrace(py_trace_flocals)),
    ]
    if has_ctrace1:
        configs.insert(4, ('c_getloc_v1', 'C GetLoc(v1)', lambda: _ctrace.start(4)))

    results = {cfg: {} for cfg, _, _ in configs}

    for cfg_key, cfg_label, setup_fn in configs:
        print(f"  Running {cfg_label}...")
        for name in workload_names:
            iters = iterations[name]

            if setup_fn:
                setup_fn()

            _, per_iter = run_workload(name, iterations=iters)
            results[cfg_key][name] = per_iter

            if cfg_key.startswith('c_') and cfg_key != 'c_getloc_v1':
                _ctrace2.stop()
            elif cfg_key == 'c_getloc_v1':
                _ctrace.stop()
            elif cfg_key.startswith('py_'):
                sys.settrace(None)

    # === Print results ===
    print(f"\n{'='*90}")
    print("Overhead ratios (vs baseline)")
    print(f"{'='*90}")

    categories = {
        'comp_': 'compute', 'io_': 'io', 'mem_': 'memory',
        'yield_': 'yield', 'async_': 'async',
    }

    ratio_configs = [(k, l) for k, l, _ in configs if k != 'baseline']
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
            if cfg_key in results:
                ratio = results[cfg_key][name] / b
                row += f" {ratio:>11.2f}x"
        print(row)

    # Category averages
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

    row = f"{'ALL':<12} {len(workload_names):>3}"
    for cfg_key, _ in ratio_configs:
        ratios = [results[cfg_key][n] / results['baseline'][n] for n in workload_names]
        avg = sum(ratios) / len(ratios)
        row += f" {avg:>11.2f}x"
    print(row)

    # Stats from selective run
    print(f"\n--- Selective capture statistics ---")
    _ctrace2.start(1)
    for name in workload_names:
        LARGE_WORKLOADS[name]()
    _ctrace2.stop()
    stats = _ctrace2.stats()
    print(f"  Total events:        {stats['events']:>12,}")
    print(f"  Line events:         {stats['line_events']:>12,}")
    print(f"  Lines with writes:   {stats['lines_with_writes']:>12,}")
    print(f"  Vars checked:        {stats['vars_checked']:>12,}")
    print(f"  Vars changed:        {stats['vars_changed']:>12,}")
    print(f"  GetVar calls:        {stats['getvar_calls']:>12,}")
    if stats['line_events'] > 0:
        print(f"  Avg vars checked/line: {stats['vars_checked']/stats['line_events']:.2f}")
        print(f"  % lines with writes:   {100*stats['lines_with_writes']/stats['line_events']:.1f}%")
    if stats['vars_checked'] > 0:
        print(f"  % vars actually changed: {100*stats['vars_changed']/stats['vars_checked']:.1f}%")


if __name__ == '__main__':
    main()
