"""
Experiment 7: C-Level Trace Function via PyEval_SetTrace

This is the real test of the C extension approach. Unlike Experiment 3
(which used PEP 669 callbacks), this registers a C function directly
as the trace function via PyEval_SetTrace(). CPython calls it with
the frame object already in hand.

Modes tested:
  0: noop          - Return immediately. Measures minimum settrace overhead.
  1: count         - Increment a counter. Measures C function call cost.
  2: count+line    - Count + read line number from frame.
  3: locals_api    - Read all locals via PyFrame_GetVar() (public API).
  4: f_locals      - Read all locals via PyFrame_GetLocals() (creates dict).
  5: full_capture  - Ring buffer + change detection + inline serialization.

Compared against:
  - No tracing (baseline)
  - Python sys.settrace noop
  - Python sys.settrace with f_locals.copy()
  - PEP 669 LINE noop (from exp1)
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from workloads import WORKLOADS, run_workload

ITERATIONS = 20


def py_trace_noop(frame, event, arg):
    return py_trace_noop

def py_trace_flocals(frame, event, arg):
    if event == 'line':
        frame.f_locals  # triggers locals snapshot
    return py_trace_flocals


def noop_line(code, line_number):
    pass

def noop_py_start(code, offset):
    pass

def noop_py_return(code, offset, retval):
    pass


def main():
    import _ctrace

    print("=" * 70)
    print("Experiment 7: C-Level Trace via PyEval_SetTrace")
    print("=" * 70)

    modes = _ctrace.list_modes()
    print(f"\nAvailable C trace modes:")
    for mode_id, name in modes:
        print(f"  {mode_id}: {name}")

    # --- Correctness: verify full capture mode works ---
    print(f"\n--- Correctness: full capture mode ---")

    def sample_func():
        x = 10
        y = 20
        z = x + y
        return z

    _ctrace.start(5)  # full_capture mode
    result = sample_func()
    _ctrace.stop()

    stats = _ctrace.stats()
    print(f"Result: {result}")
    print(f"Stats: {stats}")

    events = _ctrace.get_events(20)
    print(f"Captured events:")
    for evt_type, code_id, line, changes in events:
        changes_str = ""
        if changes:
            parts = []
            for var_idx, val, type_tag in changes:
                parts.append(f"  var[{var_idx}]={val} ({type_tag})")
            changes_str = " |" + ", ".join(parts)
        print(f"  {evt_type:<10} line={line:<4}{changes_str}")

    # Check we captured variable values
    found_x = found_y = found_z = False
    for evt_type, code_id, line, changes in events:
        for var_idx, val, type_tag in changes:
            if type_tag == 'int':
                if val == 10: found_x = True
                if val == 20: found_y = True
                if val == 30: found_z = True
    print(f"\nCaptured x=10: {found_x}, y=20: {found_y}, z=30: {found_z}")
    if found_x and found_y and found_z:
        print("PASS: Full capture correctly reads variable values from C")
    else:
        print("FAIL: Some variables not captured")

    # --- Performance: all modes ---
    print(f"\n--- Performance comparison ---\n")

    results = {}

    # Baseline
    print("  Running baseline...")
    baseline = {}
    for name in WORKLOADS:
        _, per_iter = run_workload(name, iterations=ITERATIONS)
        baseline[name] = per_iter
    results['baseline'] = baseline

    # C trace modes
    for mode_id, mode_name in modes:
        print(f"  Running C mode {mode_id}: {mode_name}...")
        mode_results = {}
        for name in WORKLOADS:
            _ctrace.start(mode_id)
            _, per_iter = run_workload(name, iterations=ITERATIONS)
            _ctrace.stop()
            mode_results[name] = per_iter
        results[f'c_{mode_name}'] = mode_results

    # Python settrace noop
    print("  Running Python settrace noop...")
    py_noop = {}
    for name in WORKLOADS:
        sys.settrace(py_trace_noop)
        _, per_iter = run_workload(name, iterations=ITERATIONS)
        sys.settrace(None)
        py_noop[name] = per_iter
    results['py_settrace_noop'] = py_noop

    # Python settrace with f_locals
    print("  Running Python settrace + f_locals...")
    py_flocals = {}
    for name in WORKLOADS:
        sys.settrace(py_trace_flocals)
        _, per_iter = run_workload(name, iterations=ITERATIONS)
        sys.settrace(None)
        py_flocals[name] = per_iter
    results['py_settrace_flocals'] = py_flocals

    # PEP 669 LINE noop
    TOOL_ID = 0
    E = sys.monitoring.events
    print("  Running PEP 669 LINE+CR noop...")
    pep669 = {}
    for name in WORKLOADS:
        sys.monitoring.use_tool_id(TOOL_ID, "exp7")
        sys.monitoring.register_callback(TOOL_ID, E.PY_START, noop_py_start)
        sys.monitoring.register_callback(TOOL_ID, E.PY_RETURN, noop_py_return)
        sys.monitoring.register_callback(TOOL_ID, E.LINE, noop_line)
        sys.monitoring.set_events(TOOL_ID, E.PY_START | E.PY_RETURN | E.LINE)
        _, per_iter = run_workload(name, iterations=ITERATIONS)
        sys.monitoring.set_events(TOOL_ID, 0)
        sys.monitoring.free_tool_id(TOOL_ID)
        pep669[name] = per_iter
    results['pep669_line_cr'] = pep669

    # === Print results ===
    print(f"\n{'':=<100}")
    print("OVERHEAD RATIOS (vs no monitoring)")
    print(f"{'':=<100}")

    configs = [
        ('pep669_line_cr', 'PEP669 LINE'),
        ('py_settrace_noop', 'Py st noop'),
        ('py_settrace_flocals', 'Py st+locals'),
        ('c_noop', 'C noop'),
        ('c_count', 'C count'),
        ('c_count+line', 'C cnt+line'),
        ('c_locals_api', 'C GetVar'),
        ('c_f_locals', 'C GetLocals'),
        ('c_full_capture', 'C full'),
    ]

    header = f"{'Workload':<14}"
    for key, label in configs:
        header += f" {label:>12}"
    print(header)
    print("-" * len(header))

    for name in WORKLOADS:
        b = baseline[name]
        row = f"{name:<14}"
        for key, _ in configs:
            if key in results:
                ratio = results[key][name] / b
                row += f" {ratio:>11.2f}x"
            else:
                row += f" {'N/A':>12}"
        print(row)

    # === Summary: C extension vs Python at equivalent functionality ===
    print(f"\n{'':=<100}")
    print("C vs PYTHON at equivalent functionality")
    print(f"{'':=<100}")
    print(f"{'Workload':<14} {'Py st noop':>12} {'C noop':>12} {'Speedup':>10} | "
          f"{'Py st+loc':>12} {'C GetVar':>12} {'C full':>12} {'Speedup(f)':>12}")
    print("-" * 100)

    for name in WORKLOADS:
        b = baseline[name]
        py_n = py_noop[name] / b
        c_n = results['c_noop'][name] / b
        speedup_n = (py_noop[name] - b) / max(results['c_noop'][name] - b, 1)

        py_f = py_flocals[name] / b
        c_gv = results['c_locals_api'][name] / b
        c_fc = results['c_full_capture'][name] / b
        speedup_f = (py_flocals[name] - b) / max(results['c_full_capture'][name] - b, 1)

        print(f"{name:<14} {py_n:>11.2f}x {c_n:>11.2f}x {speedup_n:>9.1f}x | "
              f"{py_f:>11.2f}x {c_gv:>11.2f}x {c_fc:>11.2f}x {speedup_f:>11.1f}x")


if __name__ == '__main__':
    main()
