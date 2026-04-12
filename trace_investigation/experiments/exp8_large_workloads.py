"""
Experiment 8: Large Workload Performance Testing

Tests our key tracing approaches against realistic workloads spanning
compute-intensive, IO-intensive, memory-intensive, and async/yield patterns.

Approaches tested:
  1. Baseline (no tracing)
  2. PEP 669 LINE + call/return (Python noop callbacks)
  3. C settrace noop (via _ctrace mode 0)
  4. C settrace + read line (mode 2)
  5. C settrace + GetVar (mode 3 — reads all locals)
  6. C settrace + GetLocals (mode 4 — creates locals dict)
  7. Python settrace noop
  8. Python settrace + f_locals.copy()
"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'exp7_c_extension'))

from workloads_large import LARGE_WORKLOADS, run_workload

TOOL_ID = 0


# === Python callbacks ===

def noop_line(code, line_number):
    pass

def noop_py_start(code, offset):
    pass

def noop_py_return(code, offset, retval):
    pass

def py_trace_noop(frame, event, arg):
    return py_trace_noop

def py_trace_flocals(frame, event, arg):
    if event == 'line':
        frame.f_locals
    return py_trace_flocals


# Event counter for PEP 669
class EventCounter:
    def __init__(self):
        self.lines = 0
        self.calls = 0
        self.returns = 0

    def line(self, code, line_number):
        self.lines += 1

    def py_start(self, code, offset):
        self.calls += 1

    def py_return(self, code, offset, retval):
        self.returns += 1

    def total(self):
        return self.lines + self.calls + self.returns

    def reset(self):
        t = self.total()
        self.lines = self.calls = self.returns = 0
        return t


def main():
    try:
        import _ctrace
        has_ctrace = True
    except ImportError:
        has_ctrace = False
        print("WARNING: _ctrace not available, skipping C extension tests")

    workload_names = list(LARGE_WORKLOADS.keys())

    # Determine iterations per workload (aim for ~100ms+ per measurement)
    print("Calibrating iterations...")
    iterations = {}
    for name in workload_names:
        _, single = run_workload(name, iterations=1)
        # Aim for at least 200ms total, min 2 iterations
        target_ns = 200_000_000
        iters = max(2, target_ns // max(single, 1))
        iters = min(iters, 50)  # cap at 50
        iterations[name] = iters

    print(f"{'Workload':<20} {'Iters':>6} {'Single (ms)':>12}")
    for name in workload_names:
        _, single = run_workload(name, iterations=1)
        print(f"{name:<20} {iterations[name]:>6} {single/1_000_000:>10.1f}ms")

    # === Count events per workload ===
    print(f"\n{'='*80}")
    print("Event counts (single iteration)")
    print(f"{'='*80}")

    E = sys.monitoring.events
    counter = EventCounter()

    print(f"{'Workload':<20} {'LINE':>10} {'CALL':>10} {'RETURN':>10} {'TOTAL':>10}")
    print("-" * 62)

    for name in workload_names:
        sys.monitoring.use_tool_id(TOOL_ID, "exp8")
        sys.monitoring.register_callback(TOOL_ID, E.LINE, counter.line)
        sys.monitoring.register_callback(TOOL_ID, E.PY_START, counter.py_start)
        sys.monitoring.register_callback(TOOL_ID, E.PY_RETURN, counter.py_return)
        sys.monitoring.set_events(TOOL_ID, E.LINE | E.PY_START | E.PY_RETURN)
        counter.reset()

        LARGE_WORKLOADS[name]()

        sys.monitoring.set_events(TOOL_ID, 0)
        sys.monitoring.free_tool_id(TOOL_ID)

        print(f"{name:<20} {counter.lines:>10,} {counter.calls:>10,} "
              f"{counter.returns:>10,} {counter.total():>10,}")
        counter.reset()

    # === Performance measurements ===
    print(f"\n{'='*80}")
    print("Performance measurements")
    print(f"{'='*80}")

    configs = [
        ('baseline', 'Baseline'),
        ('pep669_lcr', 'PEP669 L+CR'),
        ('py_st_noop', 'Py st noop'),
        ('py_st_floc', 'Py st+floc'),
    ]
    if has_ctrace:
        configs.extend([
            ('c_noop', 'C noop'),
            ('c_line', 'C cnt+line'),
            ('c_getvar', 'C GetVar'),
            ('c_getloc', 'C GetLocals'),
        ])

    results = {cfg: {} for cfg, _ in configs}

    for cfg_key, cfg_label in configs:
        print(f"  Running {cfg_label}...")
        for name in workload_names:
            iters = iterations[name]

            # Setup
            if cfg_key == 'baseline':
                pass
            elif cfg_key == 'pep669_lcr':
                sys.monitoring.use_tool_id(TOOL_ID, "exp8")
                sys.monitoring.register_callback(TOOL_ID, E.LINE, noop_line)
                sys.monitoring.register_callback(TOOL_ID, E.PY_START, noop_py_start)
                sys.monitoring.register_callback(TOOL_ID, E.PY_RETURN, noop_py_return)
                sys.monitoring.set_events(TOOL_ID, E.LINE | E.PY_START | E.PY_RETURN)
            elif cfg_key == 'py_st_noop':
                sys.settrace(py_trace_noop)
            elif cfg_key == 'py_st_floc':
                sys.settrace(py_trace_flocals)
            elif cfg_key.startswith('c_'):
                mode_map = {'c_noop': 0, 'c_line': 2, 'c_getvar': 3, 'c_getloc': 4}
                _ctrace.start(mode_map[cfg_key])

            _, per_iter = run_workload(name, iterations=iters)
            results[cfg_key][name] = per_iter

            # Teardown
            if cfg_key == 'pep669_lcr':
                sys.monitoring.set_events(TOOL_ID, 0)
                sys.monitoring.free_tool_id(TOOL_ID)
            elif cfg_key.startswith('py_st'):
                sys.settrace(None)
            elif cfg_key.startswith('c_'):
                _ctrace.stop()

    # === Print absolute times ===
    print(f"\n{'='*80}")
    print("Absolute times (ms/iter)")
    print(f"{'='*80}")

    header = f"{'Workload':<20}"
    for _, label in configs:
        header += f" {label:>12}"
    print(header)
    print("-" * len(header))

    for name in workload_names:
        row = f"{name:<20}"
        for cfg_key, _ in configs:
            t = results[cfg_key][name]
            row += f" {t/1_000_000:>11.1f}m"
        print(row)

    # === Print overhead ratios ===
    print(f"\n{'='*80}")
    print("Overhead ratios (vs baseline)")
    print(f"{'='*80}")

    ratio_configs = [c for c in configs if c[0] != 'baseline']
    header = f"{'Workload':<20} {'Category':<10}"
    for _, label in ratio_configs:
        header += f" {label:>12}"
    print(header)
    print("-" * len(header))

    categories = {
        'comp_': 'compute',
        'io_': 'io',
        'mem_': 'memory',
        'yield_': 'yield',
        'async_': 'async',
    }

    for name in workload_names:
        cat = next((v for k, v in categories.items() if name.startswith(k)), '?')
        b = results['baseline'][name]
        row = f"{name:<20} {cat:<10}"
        for cfg_key, _ in ratio_configs:
            ratio = results[cfg_key][name] / b
            row += f" {ratio:>11.2f}x"
        print(row)

    # === Category averages ===
    print(f"\n{'='*80}")
    print("Category average overhead")
    print(f"{'='*80}")

    cat_workloads = {}
    for name in workload_names:
        cat = next((v for k, v in categories.items() if name.startswith(k)), '?')
        cat_workloads.setdefault(cat, []).append(name)

    header = f"{'Category':<12} {'Count':>6}"
    for _, label in ratio_configs:
        header += f" {label:>12}"
    print(header)
    print("-" * len(header))

    for cat, names in cat_workloads.items():
        row = f"{cat:<12} {len(names):>6}"
        for cfg_key, _ in ratio_configs:
            ratios = [results[cfg_key][n] / results['baseline'][n] for n in names]
            avg = sum(ratios) / len(ratios)
            row += f" {avg:>11.2f}x"
        print(row)

    # Grand average
    row = f"{'ALL':<12} {len(workload_names):>6}"
    for cfg_key, _ in ratio_configs:
        ratios = [results[cfg_key][n] / results['baseline'][n] for n in workload_names]
        avg = sum(ratios) / len(ratios)
        row += f" {avg:>11.2f}x"
    print(row)

    # === C extension comparison ===
    if has_ctrace:
        print(f"\n{'='*80}")
        print("C settrace advantage over Python settrace (overhead reduction %)")
        print(f"{'='*80}")

        print(f"{'Workload':<20} {'Category':<10} {'C noop / Py noop':>18} "
              f"{'C GetVar / Py floc':>20}")
        print("-" * 70)

        for name in workload_names:
            cat = next((v for k, v in categories.items() if name.startswith(k)), '?')
            b = results['baseline'][name]

            py_noop_oh = results['py_st_noop'][name] - b
            c_noop_oh = results['c_noop'][name] - b

            py_floc_oh = results['py_st_floc'][name] - b
            c_getvar_oh = results['c_getvar'][name] - b

            noop_reduction = (1 - c_noop_oh / max(py_noop_oh, 1)) * 100
            getvar_reduction = (1 - c_getvar_oh / max(py_floc_oh, 1)) * 100

            print(f"{name:<20} {cat:<10} {noop_reduction:>17.0f}% "
                  f"{getvar_reduction:>19.0f}%")


if __name__ == '__main__':
    main()
