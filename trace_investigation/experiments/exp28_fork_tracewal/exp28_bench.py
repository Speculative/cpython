"""
Experiment 28: Fork WAL Tracing — Performance Comparison

Compares:
  1. Baseline (no tracing)
  2. C extension WAL (_ctrace_wal via PyEval_SetTrace)
  3. Fork WAL (_tracewal via inline bytecode hooks)

Uses the same workloads as exp27 for direct comparison.
"""
import sys
import os
import time
import statistics
import dis
import opcode
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '..', 'exp27_c_extension'))

# Import fork WAL (built into this CPython)
import _tracewal

# Import C extension WAL (built separately)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '..', 'exp7_c_extension'))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '..', 'exp10_c_extension'))

try:
    import _ctrace_wal
    HAS_CEXT = True
except ImportError:
    print("WARNING: _ctrace_wal not available — skipping C extension comparison")
    HAS_CEXT = False

try:
    import _ctrace
    HAS_CTRACE = True
except ImportError:
    HAS_CTRACE = False


# ============================================================================
# Bytecode analyzer for C extension (from exp27)
# ============================================================================

# Mutation type constants (must match C enum)
MUT_STORE_FAST = 1
MUT_STORE_SUBSCR = 2
MUT_STORE_ATTR = 3
MUT_DELETE_SUBSCR = 4
MUT_DELETE_ATTR = 5
MUT_METHOD_CALL = 6

ARG_NONE = 0
ARG_CONST_INT = 1
ARG_CONST_FLOAT = 2
ARG_CONST_STR = 3
ARG_CONST_NONE = 4
ARG_CONST_BOOL = 5
ARG_LOCAL = 6
ARG_BUILD = 7
ARG_EXPR = 8

STORE_FAST_OPS = set()
for name, op_val in opcode.opmap.items():
    if 'STORE_FAST' in name or name == 'STORE_NAME' or name == 'STORE_DEREF':
        STORE_FAST_OPS.add(op_val)

KNOWN_MUTATING_METHODS = {
    'append', 'clear', 'extend', 'insert', 'pop', 'remove', 'reverse', 'sort',
    'add', 'discard', 'difference_update', 'intersection_update',
    'symmetric_difference_update', 'update',
    'appendleft', 'extendleft', 'popleft', 'rotate',
    'setdefault', 'popitem',
}

FUSED_LOAD_OPS = {'LOAD_FAST_BORROW_LOAD_FAST_BORROW',
                  'LOAD_FAST_LOAD_FAST', 'STORE_FAST_LOAD_FAST'}


def analyze_for_wal(code):
    """Minimal analysis producing the format register_code expects."""
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

    def set_bit(line, varname):
        idx = varname_to_idx.get(varname)
        if idx is not None:
            line_bitmasks[line] = line_bitmasks.get(line, 0) | (1 << idx)

    for instr in instructions:
        line = instr.positions.lineno if hasattr(instr, 'positions') else getattr(instr, 'starts_line', None)
        if line is None:
            continue
        if instr.opcode in STORE_FAST_OPS:
            set_bit(line, instr.argval)

    all_lines = set(line_bitmasks.keys())
    line_data = []
    for ln in sorted(all_lines):
        bitmask = line_bitmasks.get(ln, 0)
        line_data.append((ln, bitmask, []))

    return line_data, string_table


def register_code_cext(code, visited=None):
    """Register code for the C extension (_ctrace_wal)."""
    if not HAS_CEXT:
        return
    if visited is None:
        visited = set()
    if id(code) in visited:
        return
    visited.add(id(code))

    line_data, strings = analyze_for_wal(code)
    _ctrace_wal.register_code(code, code.co_firstlineno, line_data, strings)

    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            register_code_cext(const, visited)


def register_code_fork(code, visited=None):
    """Register code for the fork (_tracewal) — minimal, just for code_idx stability."""
    if visited is None:
        visited = set()
    if id(code) in visited:
        return
    visited.add(id(code))
    _tracewal.register_code(code, code.co_firstlineno, [], [])
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            register_code_fork(const, visited)


# ============================================================================
# Correctness tests
# ============================================================================

passed = 0
failed = 0
errors = []


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS: {name}")
    else:
        failed += 1
        msg = f"  FAIL: {name}"
        if detail:
            msg += f" — {detail}"
        print(msg)
        errors.append(msg)


def test_basic_store_fast():
    print("\n=== Test 1: STORE_FAST captures ===")
    def target():
        x = 42
        y = "hello"
        z = [1, 2, 3]
        return x, y, z

    _tracewal.clear()
    _tracewal.start()
    result = target()
    _tracewal.stop()

    wal = _tracewal.get_wal(100)
    binds = [e for e in wal if e['event'] == 'BIND']
    x_binds = [e for e in binds if e.get('name') == 'x']
    y_binds = [e for e in binds if e.get('name') == 'y']
    z_binds = [e for e in binds if e.get('name') == 'z']

    check("x=42 captured", any(e.get('value') == 42 for e in x_binds),
          f"x_binds: {x_binds}")
    check("y='hello' captured", any(e.get('value') == 'hello' for e in y_binds),
          f"y_binds: {y_binds}")
    check("z bound to list (oid)", any(e.get('oid', 0) > 0 for e in z_binds),
          f"z_binds: {z_binds}")


def test_store_subscr():
    print("\n=== Test 2: STORE_SUBSCR captures ===")
    def target():
        items = [1, 2, 3]
        items[0] = 99
        d = {}
        d['key'] = 'value'
        return items, d

    _tracewal.clear()
    _tracewal.start()
    result = target()
    _tracewal.stop()

    wal = _tracewal.get_wal(100)
    setitems = [e for e in wal if e['event'] == 'SETITEM']
    check("items[0] = 99 captured", len(setitems) >= 1,
          f"setitems: {setitems}")
    check("d['key'] = 'value' captured", len(setitems) >= 2,
          f"setitems: {setitems}")


def test_store_attr():
    print("\n=== Test 3: STORE_ATTR captures ===")
    class Point:
        def __init__(self, x, y):
            self.x = x
            self.y = y

    def target():
        p = Point(1, 2)
        p.x = 10
        p.y = 20
        return p

    _tracewal.clear()
    _tracewal.start()
    result = target()
    _tracewal.stop()

    wal = _tracewal.get_wal(200)
    setattrs = [e for e in wal if e['event'] == 'SETATTR']
    x_sets = [e for e in setattrs if e.get('attr') == 'x']
    y_sets = [e for e in setattrs if e.get('attr') == 'y']
    check("p.x set captured", len(x_sets) >= 2,  # __init__ + target
          f"x_sets: {x_sets}")
    check("p.y set captured", len(y_sets) >= 2,
          f"y_sets: {y_sets}")


def test_call_return():
    print("\n=== Test 4: CALL/RETURN flow ===")
    def inner(a, b):
        return a + b

    def target():
        x = inner(3, 4)
        return x

    _tracewal.clear()
    _tracewal.start()
    result = target()
    _tracewal.stop()

    wal = _tracewal.get_wal(100)
    calls = [e for e in wal if e['event'] == 'CALL']
    returns = [e for e in wal if e['event'] == 'RETURN']
    check("CALL events present", len(calls) >= 2,  # target + inner
          f"calls: {len(calls)}")
    check("RETURN events present", len(returns) >= 2,
          f"returns: {len(returns)}")


def test_aliasing():
    print("\n=== Test 5: Aliasing (shared OID) ===")
    def target():
        a = [1, 2, 3]
        b = a
        return a, b

    _tracewal.clear()
    _tracewal.start()
    result = target()
    _tracewal.stop()

    wal = _tracewal.get_wal(100)
    binds = [e for e in wal if e['event'] == 'BIND']
    a_binds = [e for e in binds if e.get('name') == 'a' and e.get('oid', 0) > 0]
    b_binds = [e for e in binds if e.get('name') == 'b' and e.get('oid', 0) > 0]

    if a_binds and b_binds:
        check("a and b share same oid", a_binds[-1]['oid'] == b_binds[-1]['oid'],
              f"a_oid={a_binds[-1]['oid']}, b_oid={b_binds[-1]['oid']}")
    else:
        check("found binds for a and b", False,
              f"a_binds={a_binds}, b_binds={b_binds}")


def test_line_events():
    print("\n=== Test 6: LINE events ===")
    def target():
        x = 1
        y = 2
        z = x + y
        return z

    _tracewal.clear()
    _tracewal.start()
    result = target()
    _tracewal.stop()

    wal = _tracewal.get_wal(100)
    lines = [e for e in wal if e['event'] == 'LINE']
    check("LINE events emitted", len(lines) >= 3,
          f"got {len(lines)} LINE events")


def test_generator():
    print("\n=== Test 7: Generator yield/resume ===")
    def gen():
        yield 1
        yield 2
        yield 3

    def target():
        return list(gen())

    _tracewal.clear()
    _tracewal.start()
    result = target()
    _tracewal.stop()

    wal = _tracewal.get_wal(200)
    returns = [e for e in wal if e['event'] == 'RETURN']
    calls = [e for e in wal if e['event'] == 'CALL']
    check("Generator produces RETURN events (yield)", len(returns) >= 3,
          f"got {len(returns)}")


def test_delete_subscr():
    print("\n=== Test 8: DELETE_SUBSCR captures ===")
    def target():
        d = {'a': 1, 'b': 2}
        del d['a']
        return d

    _tracewal.clear()
    _tracewal.start()
    result = target()
    _tracewal.stop()

    wal = _tracewal.get_wal(100)
    delitems = [e for e in wal if e['event'] == 'DELITEM']
    check("del d['a'] captured", len(delitems) >= 1,
          f"delitems: {delitems}")


# ============================================================================
# Performance measurement
# ============================================================================

def measure(workload_fn, n_warmup=3, n_rounds=10, iters_per_round=None):
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


def test_performance():
    print("\n" + "=" * 70)
    print("Performance comparison: Fork WAL vs C Extension WAL")
    print("=" * 70)

    from workloads_large import LARGE_WORKLOADS

    workload_names = list(LARGE_WORKLOADS.keys())

    # Register all code for C extension
    if HAS_CEXT:
        _ctrace_wal.clear()
        import workloads_large
        for name, fn in vars(workloads_large).items():
            if callable(fn) and hasattr(fn, '__code__'):
                register_code_cext(fn.__code__)
        for name, fn in LARGE_WORKLOADS.items():
            if hasattr(fn, '__code__'):
                register_code_cext(fn.__code__)

    configs = [
        ('baseline', 'Baseline', lambda: None, lambda: None),
    ]

    if HAS_CTRACE:
        configs.append(('c_noop', 'C noop', lambda: _ctrace.start(0), lambda: _ctrace.stop()))

    if HAS_CEXT:
        configs.append(('c_wal', 'C ext WAL', lambda: _ctrace_wal.start(), lambda: _ctrace_wal.stop()))

    # Three fork modes: stores only, control flow, full LINE
    configs.append(('fork_m0', 'Fork stores', lambda: _tracewal.start(line_mode=0), lambda: _tracewal.stop()))
    configs.append(('fork_m1', 'Fork ctrl', lambda: _tracewal.start(line_mode=1), lambda: _tracewal.stop()))
    configs.append(('fork_m2', 'Fork full', lambda: _tracewal.start(line_mode=2), lambda: _tracewal.stop()))

    results = {k: {} for k, _, _, _ in configs}

    for cfg_key, cfg_label, setup, teardown in configs:
        print(f"  Running {cfg_label}...")
        for name in workload_names:
            fn = LARGE_WORKLOADS[name]
            setup()
            med = measure(fn)
            teardown()
            results[cfg_key][name] = med

    # Print results
    categories = {
        'comp_': 'compute', 'io_': 'io', 'mem_': 'memory',
        'yield_': 'yield', 'async_': 'async', 'pat_': 'pattern',
    }

    # Build header
    header_parts = [f"{'Workload':<20}", f"{'Cat':<8}"]
    for cfg_key, cfg_label, _, _ in configs:
        if cfg_key != 'baseline':
            header_parts.append(f"{cfg_label:>12}")
    print(f"\n{''.join(header_parts)}")
    print("-" * (28 + 12 * (len(configs) - 1)))

    for name in workload_names:
        cat = next((v for k, v in categories.items() if name.startswith(k)), '?')
        b = results['baseline'][name]
        parts = [f"{name:<20}", f"{cat:<8}"]
        for cfg_key, cfg_label, _, _ in configs:
            if cfg_key != 'baseline':
                ratio = results[cfg_key][name] / b
                parts.append(f"{ratio:>11.2f}x")
        print(''.join(parts))

    # Category averages
    print()
    cat_wl = {}
    for name in workload_names:
        cat = next((v for k, v in categories.items() if name.startswith(k)), '?')
        cat_wl.setdefault(cat, []).append(name)

    header_parts = [f"{'Category':<12}", f"{'#':>3}"]
    for cfg_key, cfg_label, _, _ in configs:
        if cfg_key != 'baseline':
            header_parts.append(f"{cfg_label:>12}")
    print(''.join(header_parts))
    print("-" * (15 + 12 * (len(configs) - 1)))

    for cat, names in cat_wl.items():
        n = len(names)
        parts = [f"{cat:<12}", f"{n:>3}"]
        for cfg_key, cfg_label, _, _ in configs:
            if cfg_key != 'baseline':
                avg = sum(results[cfg_key][nm] / results['baseline'][nm] for nm in names) / n
                parts.append(f"{avg:>11.2f}x")
        print(''.join(parts))

    n_all = len(workload_names)
    parts = [f"{'ALL':<12}", f"{n_all:>3}"]
    for cfg_key, cfg_label, _, _ in configs:
        if cfg_key != 'baseline':
            avg = sum(results[cfg_key][nm] / results['baseline'][nm] for nm in workload_names) / n_all
            parts.append(f"{avg:>11.2f}x")
    print(''.join(parts))

    # WAL stats for fork
    print("\n--- Fork WAL stats (all workloads, single iteration) ---")
    _tracewal.clear()
    _tracewal.start()
    for name in workload_names:
        LARGE_WORKLOADS[name]()
    _tracewal.stop()
    stats = _tracewal.stats()
    for k, v in stats.items():
        print(f"  {k}: {v:,}" if isinstance(v, int) else f"  {k}: {v}")

    if HAS_CEXT:
        print("\n--- C ext WAL stats (all workloads, single iteration) ---")
        _ctrace_wal.clear()
        # Re-register for C ext
        import workloads_large
        for name, fn in vars(workloads_large).items():
            if callable(fn) and hasattr(fn, '__code__'):
                register_code_cext(fn.__code__)
        for name, fn in LARGE_WORKLOADS.items():
            if hasattr(fn, '__code__'):
                register_code_cext(fn.__code__)
        _ctrace_wal.start()
        for name in workload_names:
            LARGE_WORKLOADS[name]()
        _ctrace_wal.stop()
        stats = _ctrace_wal.stats()
        for k, v in stats.items():
            print(f"  {k}: {v:,}" if isinstance(v, int) else f"  {k}: {v}")


# ============================================================================
# Main
# ============================================================================

def main():
    print("Experiment 28: Fork WAL Tracing")
    print(f"Python: {sys.version}")
    print(f"Fork WAL available: True")
    print(f"C ext WAL available: {HAS_CEXT}")

    # Correctness tests
    test_basic_store_fast()
    test_store_subscr()
    test_store_attr()
    test_call_return()
    test_aliasing()
    test_line_events()
    test_generator()
    test_delete_subscr()

    print(f"\n{'='*60}")
    print(f"Correctness: {passed} passed, {failed} failed")
    print(f"{'='*60}")
    if errors:
        print("\nFailures:")
        for e in errors:
            print(f"  {e}")

    if '--perf' in sys.argv or '--bench' in sys.argv or failed == 0:
        test_performance()


if __name__ == '__main__':
    main()
