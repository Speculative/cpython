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

def measure(workload_fn, n_warmup=3, n_rounds=10, iters_per_round=None):
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
}

def categorize(name):
    for prefix, cat in CATEGORIES.items():
        if name.startswith(prefix):
            return cat
    return '?'


# ---------------------------------------------------------------------------
# Tracing configurations
# ---------------------------------------------------------------------------

def build_configs(fork_only=False):
    """Build list of (key, label, setup_fn, teardown_fn) tuples."""
    configs = [
        ('baseline', 'Baseline', lambda: None, lambda: None),
    ]

    if not fork_only:
        # C extension settrace noop
        try:
            import _ctrace
            configs.append(('c_noop', 'C noop',
                            lambda: _ctrace.start(0), lambda: _ctrace.stop()))
        except ImportError:
            pass

        # C extension WAL — both modes
        try:
            import _ctrace_wal
            _setup_cext_registration()
            configs.append(('cext_stores', 'C ext stores',
                            lambda: _ctrace_wal.start(line_mode=0), lambda: _ctrace_wal.stop()))
            configs.append(('cext_lines', 'C ext +LINE',
                            lambda: _ctrace_wal.start(line_mode=1), lambda: _ctrace_wal.stop()))
        except ImportError:
            pass

    # Fork WAL modes
    import _tracewal
    configs.append(('fork_m0', 'Fork stores',
                    lambda: _tracewal.start(line_mode=0), lambda: _tracewal.stop()))
    configs.append(('fork_m1', 'Fork ctrl',
                    lambda: _tracewal.start(line_mode=1), lambda: _tracewal.stop()))

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

    for cat in ['compute', 'io', 'memory', 'yield', 'async', 'pattern']:
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

    for cfg_key, cfg_label, setup, teardown in configs:
        print(f"  Running {cfg_label}...")
        for name in workload_names:
            fn = workloads[name]
            setup()
            med = measure(fn, n_warmup=n_warmup, n_rounds=n_rounds)
            teardown()
            results[cfg_key][name] = med

    return results


def main():
    parser = argparse.ArgumentParser(description='WAL Tracing Performance Benchmark')
    parser.add_argument('--quick', action='store_true',
                        help='Use quick workload subset')
    parser.add_argument('--fork-only', action='store_true',
                        help='Only benchmark fork WAL modes')
    parser.add_argument('--rounds', type=int, default=10,
                        help='Number of measurement rounds (default: 10)')
    parser.add_argument('--warmup', type=int, default=3,
                        help='Number of warmup rounds (default: 3)')
    args = parser.parse_args()

    workloads = QUICK_WORKLOADS if args.quick else LARGE_WORKLOADS
    configs = build_configs(fork_only=args.fork_only)

    print("WAL Tracing Performance Benchmark")
    print(f"Python: {sys.version}")
    print(f"Workloads: {len(workloads)}, Rounds: {args.rounds}, Warmup: {args.warmup}")
    print(f"Configs: {', '.join(l for _, l, _, _ in configs)}")
    print()

    results = run_benchmark(workloads=workloads, configs=configs,
                            n_warmup=args.warmup, n_rounds=args.rounds)
    workload_names = list(workloads.keys())
    print_results(workload_names, configs, results)

    # WAL stats for each fork mode
    import _tracewal
    for mode, label in [(0, 'Fork stores (mode 0)'),
                        (1, 'Fork ctrl (mode 1)'),
                        (2, 'Fork full (mode 2)')]:
        _tracewal.clear()
        _tracewal.start(line_mode=mode)
        for name in workload_names:
            workloads[name]()
        _tracewal.stop()
        print_wal_stats(label, _tracewal.stats())

    # C extension stats if available
    try:
        import _ctrace_wal
        _setup_cext_registration()
        _ctrace_wal.start()
        for name in workload_names:
            workloads[name]()
        _ctrace_wal.stop()
        print_wal_stats('C ext WAL', _ctrace_wal.stats())
    except ImportError:
        pass


if __name__ == '__main__':
    main()
