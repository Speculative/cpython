"""
Experiment 11: Steady-State Performance with Proper Warmup

Previous experiments mixed one-time costs (code registration, bytecode
instrumentation, frame cache warmup) with steady-state execution. This
experiment properly separates them.

Methodology:
  1. Setup tracing
  2. Run 3 warmup iterations (under tracing — pays all one-time costs)
  3. Run N measurement rounds of M iterations each
  4. Report: first-run cost, steady-state median, min, max, stddev

Also separately measures:
  - Cold start: first execution under tracing (includes instrumentation)
  - Warm steady-state: subsequent executions

Configurations:
  - Baseline (no tracing)
  - C noop (settrace floor)
  - C selective (pre-computed maps)
  - C GetLocals
  - PEP 669 LINE+CR noop
  - Python settrace noop
  - Python settrace + f_locals
"""
import sys
import os
import time
import statistics
import dis
import types
import opcode

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '..', 'exp7_c_extension'))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '..', 'exp10_c_extension'))

from workloads_large import LARGE_WORKLOADS

import _ctrace
import _ctrace2


# ============================================================================
# Code registration (same as exp10)
# ============================================================================

STORE_OPS = set()
for name, op in opcode.opmap.items():
    if 'STORE_FAST' in name or name == 'STORE_NAME' or name == 'STORE_DEREF':
        STORE_OPS.add(op)


def analyze_and_register(code, visited=None):
    if visited is None:
        visited = set()
    if id(code) in visited:
        return
    visited.add(id(code))

    varnames = code.co_varnames
    varname_to_idx = {name: i for i, name in enumerate(varnames)}

    line_bitmasks = {}
    for instr in dis.get_instructions(code):
        if instr.opcode in STORE_OPS and instr.positions.lineno is not None:
            line = instr.positions.lineno
            idx = varname_to_idx.get(instr.argval)
            if idx is not None and idx < 64:
                if line not in line_bitmasks:
                    line_bitmasks[line] = 0
                line_bitmasks[line] |= (1 << idx)

    first_line = code.co_firstlineno
    packed = [(line, mask) for line, mask in line_bitmasks.items()]
    _ctrace2.register_code(code, first_line, packed)

    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            analyze_and_register(const, visited)


def register_workload_code():
    import workloads_large
    registered = set()
    for name, fn in vars(workloads_large).items():
        if callable(fn) and hasattr(fn, '__code__'):
            if id(fn.__code__) not in registered:
                analyze_and_register(fn.__code__)
                registered.add(id(fn.__code__))
    for name, fn in LARGE_WORKLOADS.items():
        if hasattr(fn, '__code__'):
            if id(fn.__code__) not in registered:
                analyze_and_register(fn.__code__)
                registered.add(id(fn.__code__))


# ============================================================================
# Measurement infrastructure
# ============================================================================

def measure_workload(workload_fn, n_warmup=3, n_rounds=10, iters_per_round=None):
    """
    Measure a workload with proper warmup.

    Returns: {
        'cold': time of first execution (ns),
        'warmup_times': [ns per iter for each warmup round],
        'round_times': [ns per iter for each measurement round],
        'median': median of round times,
        'min': min,
        'max': max,
        'stdev': standard deviation,
    }
    """
    if iters_per_round is None:
        # Auto-calibrate: aim for ~50ms per round
        start = time.perf_counter_ns()
        workload_fn()
        single = time.perf_counter_ns() - start
        iters_per_round = max(1, 50_000_000 // max(single, 1))
        iters_per_round = min(iters_per_round, 100)

    # Cold start (first execution — no prior warmup)
    start = time.perf_counter_ns()
    workload_fn()
    cold = time.perf_counter_ns() - start

    # Warmup phase
    warmup_times = []
    for _ in range(n_warmup):
        start = time.perf_counter_ns()
        for _ in range(iters_per_round):
            workload_fn()
        elapsed = time.perf_counter_ns() - start
        warmup_times.append(elapsed // iters_per_round)

    # Measurement rounds
    round_times = []
    for _ in range(n_rounds):
        start = time.perf_counter_ns()
        for _ in range(iters_per_round):
            workload_fn()
        elapsed = time.perf_counter_ns() - start
        round_times.append(elapsed // iters_per_round)

    return {
        'cold': cold,
        'warmup_times': warmup_times,
        'round_times': round_times,
        'iters_per_round': iters_per_round,
        'median': statistics.median(round_times),
        'min': min(round_times),
        'max': max(round_times),
        'stdev': statistics.stdev(round_times) if len(round_times) > 1 else 0,
    }


# ============================================================================
# Tracing configs
# ============================================================================

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


TOOL_ID = 0

class TracingConfig:
    def __init__(self, key, label, setup_fn, teardown_fn):
        self.key = key
        self.label = label
        self.setup_fn = setup_fn
        self.teardown_fn = teardown_fn


def make_configs():
    E = sys.monitoring.events

    def setup_pep669():
        sys.monitoring.use_tool_id(TOOL_ID, "exp11")
        sys.monitoring.register_callback(TOOL_ID, E.LINE, noop_line)
        sys.monitoring.register_callback(TOOL_ID, E.PY_START, noop_py_start)
        sys.monitoring.register_callback(TOOL_ID, E.PY_RETURN, noop_py_return)
        sys.monitoring.set_events(TOOL_ID, E.LINE | E.PY_START | E.PY_RETURN)

    def teardown_pep669():
        sys.monitoring.set_events(TOOL_ID, 0)
        sys.monitoring.free_tool_id(TOOL_ID)

    return [
        TracingConfig('baseline', 'Baseline', lambda: None, lambda: None),
        TracingConfig('c_noop', 'C noop', lambda: _ctrace.start(0), _ctrace.stop),
        TracingConfig('c_selective', 'C selective', lambda: _ctrace2.start(1), _ctrace2.stop),
        TracingConfig('c_getloc', 'C GetLocals', lambda: _ctrace.start(4), _ctrace.stop),
        TracingConfig('pep669', 'PEP669 L+CR', setup_pep669, teardown_pep669),
        TracingConfig('py_noop', 'Py st noop',
                      lambda: sys.settrace(py_trace_noop), lambda: sys.settrace(None)),
        TracingConfig('py_floc', 'Py st+floc',
                      lambda: sys.settrace(py_trace_flocals), lambda: sys.settrace(None)),
    ]


# ============================================================================
# Main
# ============================================================================

def main():
    N_WARMUP = 3
    N_ROUNDS = 10

    workload_names = list(LARGE_WORKLOADS.keys())
    configs = make_configs()

    # Pre-register code objects for C selective
    _ctrace2.clear()
    register_workload_code()
    print(f"Registered {_ctrace2.stats()['registered_codes']} code objects\n")

    # All results: results[config_key][workload_name] = measurement_dict
    all_results = {cfg.key: {} for cfg in configs}

    for wl_name in workload_names:
        workload_fn = LARGE_WORKLOADS[wl_name]
        print(f"--- {wl_name} ---")

        for cfg in configs:
            # Setup tracing
            cfg.setup_fn()

            # Measure
            m = measure_workload(workload_fn, n_warmup=N_WARMUP, n_rounds=N_ROUNDS)
            all_results[cfg.key][wl_name] = m

            # Teardown
            cfg.teardown_fn()

            print(f"  {cfg.label:<14} cold={m['cold']/1e6:>7.1f}ms  "
                  f"median={m['median']/1e6:>7.2f}ms  "
                  f"min={m['min']/1e6:>7.2f}ms  "
                  f"stdev={m['stdev']/1e6:>6.2f}ms  "
                  f"({m['iters_per_round']} iters/round)")

    # ========================================================================
    # Summary tables
    # ========================================================================

    categories = {
        'comp_': 'compute', 'io_': 'io', 'mem_': 'memory',
        'yield_': 'yield', 'async_': 'async',
    }

    # --- Steady-state overhead (median) ---
    print(f"\n{'='*100}")
    print("STEADY-STATE overhead ratios (median, after warmup)")
    print(f"{'='*100}")

    non_baseline = [c for c in configs if c.key != 'baseline']
    header = f"{'Workload':<20} {'Cat':<8}"
    for cfg in non_baseline:
        header += f" {cfg.label:>12}"
    print(header)
    print("-" * len(header))

    for name in workload_names:
        cat = next((v for k, v in categories.items() if name.startswith(k)), '?')
        base_med = all_results['baseline'][name]['median']
        row = f"{name:<20} {cat:<8}"
        for cfg in non_baseline:
            med = all_results[cfg.key][name]['median']
            ratio = med / base_med
            row += f" {ratio:>11.2f}x"
        print(row)

    # Category averages
    cat_workloads = {}
    for name in workload_names:
        cat = next((v for k, v in categories.items() if name.startswith(k)), '?')
        cat_workloads.setdefault(cat, []).append(name)

    print(f"\n{'Category':<12} {'#':>3}", end="")
    for cfg in non_baseline:
        print(f" {cfg.label:>12}", end="")
    print()
    print("-" * (18 + 13 * len(non_baseline)))

    for cat, names in cat_workloads.items():
        print(f"{cat:<12} {len(names):>3}", end="")
        for cfg in non_baseline:
            ratios = [all_results[cfg.key][n]['median'] / all_results['baseline'][n]['median']
                      for n in names]
            avg = sum(ratios) / len(ratios)
            print(f" {avg:>11.2f}x", end="")
        print()

    print(f"{'ALL':<12} {len(workload_names):>3}", end="")
    for cfg in non_baseline:
        ratios = [all_results[cfg.key][n]['median'] / all_results['baseline'][n]['median']
                  for n in workload_names]
        avg = sum(ratios) / len(ratios)
        print(f" {avg:>11.2f}x", end="")
    print()

    # --- Cold start overhead ---
    print(f"\n{'='*100}")
    print("COLD START overhead ratios (first execution under tracing)")
    print(f"{'='*100}")

    header = f"{'Workload':<20} {'Cat':<8}"
    for cfg in non_baseline:
        header += f" {cfg.label:>12}"
    print(header)
    print("-" * len(header))

    for name in workload_names:
        cat = next((v for k, v in categories.items() if name.startswith(k)), '?')
        base_cold = all_results['baseline'][name]['cold']
        row = f"{name:<20} {cat:<8}"
        for cfg in non_baseline:
            cold = all_results[cfg.key][name]['cold']
            ratio = cold / base_cold
            row += f" {ratio:>11.2f}x"
        print(row)

    # --- Cold vs steady-state comparison ---
    print(f"\n{'='*100}")
    print("COLD vs STEADY-STATE: ratio of cold start to steady-state median")
    print("(Values > 1.0 mean cold start is slower than steady state)")
    print(f"{'='*100}")

    header = f"{'Workload':<20} {'Cat':<8}"
    for cfg in configs:
        header += f" {cfg.label:>12}"
    print(header)
    print("-" * len(header))

    for name in workload_names:
        cat = next((v for k, v in categories.items() if name.startswith(k)), '?')
        row = f"{name:<20} {cat:<8}"
        for cfg in configs:
            m = all_results[cfg.key][name]
            if m['median'] > 0:
                ratio = m['cold'] / m['median']
                row += f" {ratio:>11.2f}x"
            else:
                row += f" {'N/A':>12}"
        print(row)

    # --- Measurement stability ---
    print(f"\n{'='*100}")
    print("MEASUREMENT STABILITY: coefficient of variation (stdev/median) %")
    print(f"{'='*100}")

    header = f"{'Workload':<20}"
    for cfg in configs:
        header += f" {cfg.label:>12}"
    print(header)
    print("-" * len(header))

    for name in workload_names:
        row = f"{name:<20}"
        for cfg in configs:
            m = all_results[cfg.key][name]
            if m['median'] > 0:
                cv = 100 * m['stdev'] / m['median']
                row += f" {cv:>11.1f}%"
            else:
                row += f" {'N/A':>12}"
        print(row)


if __name__ == '__main__':
    main()
