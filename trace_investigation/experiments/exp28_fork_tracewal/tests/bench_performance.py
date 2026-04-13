"""
WAL Tracing Performance Benchmark Suite

Reusable benchmark harness that measures tracing overhead across the
workloads_large.py workload suite. Supports multiple tracing configurations
and reports per-workload, per-category, and overall overhead factors.

Usage:
    # Run all configs (baseline + available tracers):
    ./python tests/bench_performance.py

    # Quick mode (skip async_network):
    ./python tests/bench_performance.py --quick

    # Only fork modes:
    ./python tests/bench_performance.py --fork-only

    # Custom rounds/warmup:
    ./python tests/bench_performance.py --rounds=20 --warmup=5
"""
import sys
import os
import time
import statistics
import argparse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))

from workloads_large import LARGE_WORKLOADS, QUICK_WORKLOADS

# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------

def measure(workload_fn, n_warmup=3, n_rounds=7, iters_per_round=None):
    """Measure median execution time of workload_fn in nanoseconds.

    Auto-calibrates iters_per_round to target ~50ms per round.
    Returns median per-iteration time in nanoseconds.
    """
    if iters_per_round is None:
        start = time.perf_counter_ns()
        workload_fn()
        single = time.perf_counter_ns() - start
        iters_per_round = max(1, 50_000_000 // max(single, 1))
        iters_per_round = min(iters_per_round, 50)

    for _ in range(n_warmup):
        for _ in range(iters_per_round):
            workload_fn()

    times = []
    for _ in range(n_rounds):
        start = time.perf_counter_ns()
        for _ in range(iters_per_round):
            workload_fn()
        times.append((time.perf_counter_ns() - start) // iters_per_round)

    return statistics.median(times)


# ---------------------------------------------------------------------------
# Workload categories
# ---------------------------------------------------------------------------

CATEGORIES = {
    'comp_': 'compute',
    'io_': 'io',
    'mem_': 'memory',
    'yield_': 'yield',
    'async_': 'async',
    'pat_': 'pattern',
    'stress_': 'stress',
}

def categorize(name):
    for prefix, cat in CATEGORIES.items():
        if name.startswith(prefix):
            return cat
    return '?'


# ---------------------------------------------------------------------------
# Tracing configurations
# ---------------------------------------------------------------------------

def build_configs(cext=False, stores=False):
    """Build list of (key, label, setup_fn, teardown_fn) tuples.

    Default: baseline + fork mode 1 only (the blessed capture path).
    --cext: add C extension WAL (+LINE) and settrace noop for comparison.
    --stores: add store-only modes (fork m0, C ext stores) for analysis.
    """
    import _tracewal

    # Use /dev/null as output to flush the buffer when full (realistic behavior)
    # without actual disk I/O cost. This prevents buffer overflow from silently
    # dropping events and skewing measurements.
    _wal_output = '/dev/null'

    configs = [
        ('baseline', 'Baseline', lambda: None, lambda: None),
    ]

    if stores:
        configs.append(('fork_m0', 'Fork stores',
                        lambda: _tracewal.start(line_mode=0, output_file=_wal_output),
                        lambda: _tracewal.stop()))

    configs.append(('fork_m1', 'Fork ctrl',
                    lambda: _tracewal.start(line_mode=1, output_file=_wal_output),
                    lambda: _tracewal.stop()))

    if cext:
        try:
            import _ctrace
            configs.append(('c_noop', 'C noop',
                            lambda: _ctrace.start(0), lambda: _ctrace.stop()))
        except ImportError:
            pass

        try:
            import _ctrace_wal
            _setup_cext_registration()
            if stores:
                configs.append(('cext_stores', 'C ext stores',
                                lambda: _ctrace_wal.start(line_mode=0, output_file=_wal_output),
                                lambda: _ctrace_wal.stop()))
            configs.append(('cext_lines', 'C ext WAL',
                            lambda: _ctrace_wal.start(line_mode=1, output_file=_wal_output),
                            lambda: _ctrace_wal.stop()))
        except ImportError:
            pass

    return configs


def _setup_cext_registration():
    """Register workload code objects with the C extension."""
    import dis
    import opcode
    import types
    import _ctrace_wal

    STORE_FAST_OPS = set()
    for name, op_val in opcode.opmap.items():
        if 'STORE_FAST' in name or name == 'STORE_NAME' or name == 'STORE_DEREF':
            STORE_FAST_OPS.add(op_val)

    def analyze_for_wal(code):
        instructions = list(dis.get_instructions(code))
        varnames = code.co_varnames
        varname_to_idx = {name: i for i, name in enumerate(varnames) if i < 64}
        string_table = []
        string_index = {}

        def intern_str(s):
            if s in string_index:
                return string_index[s]
            idx = len(string_table)
            string_table.append(s)
            string_index[s] = idx
            return idx

        for name in varnames:
            intern_str(name)

        line_bitmasks = {}
        for instr in instructions:
            line = instr.positions.lineno if hasattr(instr, 'positions') else None
            if line is None:
                continue
            if instr.opcode in STORE_FAST_OPS:
                idx = varname_to_idx.get(instr.argval)
                if idx is not None:
                    line_bitmasks[line] = line_bitmasks.get(line, 0) | (1 << idx)

        line_data = [(ln, mask, []) for ln, mask in sorted(line_bitmasks.items())]
        return line_data, string_table

    def register(code, visited=None):
        if visited is None:
            visited = set()
        if id(code) in visited:
            return
        visited.add(id(code))
        line_data, strings = analyze_for_wal(code)
        _ctrace_wal.register_code(code, code.co_firstlineno, line_data, strings)
        for const in code.co_consts:
            if isinstance(const, types.CodeType):
                register(const, visited)

    _ctrace_wal.clear()
    import workloads_large
    for name, fn in vars(workloads_large).items():
        if callable(fn) and hasattr(fn, '__code__'):
            register(fn.__code__)
    for name, fn in LARGE_WORKLOADS.items():
        if hasattr(fn, '__code__'):
            register(fn.__code__)


# ---------------------------------------------------------------------------
# Result formatting
# ---------------------------------------------------------------------------

def print_results(workload_names, configs, results):
    """Print formatted results table."""
    non_baseline = [(k, l) for k, l, _, _ in configs if k != 'baseline']

    # Per-workload table
    header = f"{'Workload':<22}{'Cat':<8}"
    for _, label in non_baseline:
        header += f"{label:>12}"
    print(f"\n{header}")
    print("-" * len(header))

    for name in workload_names:
        cat = categorize(name)
        b = results['baseline'][name]
        row = f"{name:<22}{cat:<8}"
        for key, _ in non_baseline:
            ratio = results[key][name] / b
            row += f"{ratio:>11.2f}x"
        print(row)

    # Category averages
    cat_wl = {}
    for name in workload_names:
        cat = categorize(name)
        cat_wl.setdefault(cat, []).append(name)

    print()
    header = f"{'Category':<12}{'#':>3}"
    for _, label in non_baseline:
        header += f"{label:>12}"
    print(header)
    print("-" * len(header))

    for cat in ['compute', 'io', 'memory', 'yield', 'async', 'pattern', 'stress']:
        names = cat_wl.get(cat, [])
        if not names:
            continue
        n = len(names)
        row = f"{cat:<12}{n:>3}"
        for key, _ in non_baseline:
            avg = sum(results[key][nm] / results['baseline'][nm] for nm in names) / n
            row += f"{avg:>11.2f}x"
        print(row)

    n_all = len(workload_names)
    row = f"{'ALL':<12}{n_all:>3}"
    for key, _ in non_baseline:
        avg = sum(results[key][nm] / results['baseline'][nm] for nm in workload_names) / n_all
        row += f"{avg:>11.2f}x"
    print(row)


def print_wal_stats(label, stats):
    """Print WAL statistics."""
    print(f"\n--- {label} ---")
    for k, v in stats.items():
        print(f"  {k}: {v:,}" if isinstance(v, int) else f"  {k}: {v}")


# ---------------------------------------------------------------------------
# Main benchmark runner
# ---------------------------------------------------------------------------

def run_benchmark(workloads=None, configs=None, n_warmup=3, n_rounds=10):
    """Run the full benchmark suite.

    Args:
        workloads: dict of name->fn (default: LARGE_WORKLOADS)
        configs: list of (key, label, setup, teardown) tuples
        n_warmup: warmup rounds before measurement
        n_rounds: measurement rounds

    Returns:
        dict of config_key -> {workload_name -> median_ns}
    """
    if workloads is None:
        workloads = LARGE_WORKLOADS
    if configs is None:
        configs = build_configs()

    workload_names = list(workloads.keys())
    results = {k: {} for k, _, _, _ in configs}

    # Interleave configs per workload so each workload gets equal warmth
    for name in workload_names:
        fn = workloads[name]
        for cfg_key, cfg_label, setup, teardown in configs:
            setup()
            med = measure(fn, n_warmup=n_warmup, n_rounds=n_rounds)
            teardown()
            results[cfg_key][name] = med

    return results


def main():
    parser = argparse.ArgumentParser(description='WAL Tracing Performance Benchmark')
    parser.add_argument('--quick', action='store_true',
                        help='Use quick workload subset')
    parser.add_argument('--cext', action='store_true',
                        help='Include C extension WAL for comparison')
    parser.add_argument('--stores', action='store_true',
                        help='Include store-only modes (fork m0, C ext stores)')
    parser.add_argument('--rounds', type=int, default=7,
                        help='Number of measurement rounds (default: 7)')
    parser.add_argument('--warmup', type=int, default=3,
                        help='Number of warmup rounds (default: 3)')
    args = parser.parse_args()

    workloads = QUICK_WORKLOADS if args.quick else LARGE_WORKLOADS
    configs = build_configs(cext=args.cext, stores=args.stores)

    print("WAL Tracing Performance Benchmark")
    print(f"Python: {sys.version}")
    print(f"Workloads: {len(workloads)}, Rounds: {args.rounds}, Warmup: {args.warmup}")
    print(f"Configs: {', '.join(l for _, l, _, _ in configs)}")
    print()

    results = run_benchmark(workloads=workloads, configs=configs,
                            n_warmup=args.warmup, n_rounds=args.rounds)
    workload_names = list(workloads.keys())
    print_results(workload_names, configs, results)

    # WAL stats
    import _tracewal
    modes_to_stat = [(1, 'Fork ctrl (mode 1)')]
    if args.stores:
        modes_to_stat.insert(0, (0, 'Fork stores (mode 0)'))
    for mode, label in modes_to_stat:
        _tracewal.clear()
        _tracewal.start(line_mode=mode, output_file='/dev/null')
        for name in workload_names:
            workloads[name]()
        _tracewal.stop()
        print_wal_stats(label, _tracewal.stats())

    if args.cext:
        try:
            import _ctrace_wal
            _setup_cext_registration()
            _ctrace_wal.start(output_file='/dev/null')
            for name in workload_names:
                workloads[name]()
            _ctrace_wal.stop()
            print_wal_stats('C ext WAL', _ctrace_wal.stats())
        except ImportError:
            pass


if __name__ == '__main__':
    main()
