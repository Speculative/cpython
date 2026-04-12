"""
Experiment 22: Performance of Mutation-Aware Tracing

Measures the overhead of our C extension when bytecode analysis flags
mutation lines (STORE_SUBSCR, STORE_ATTR, known-method calls) in
addition to STORE_FAST.

Compares:
  A) Baseline (no tracing)
  B) C noop (settrace floor)
  C) C selective — STORE_FAST only (original analysis from exp10)
  D) C selective — full mutation analysis (STORE_FAST + SUBSCR + ATTR + methods)
  E) C GetLocals (reads all locals every line — for reference)

Tests against workloads that exercise different mutation patterns:
  - Reassignment-heavy (lots of STORE_FAST, few mutations)
  - List-mutation-heavy (lots of .append/.pop/.sort)
  - Dict-mutation-heavy (lots of d[k]=v, d.update)
  - Object-attribute-heavy (lots of obj.attr = val)
  - Mixed real-world patterns
"""
import sys
import os
import time
import dis
import types
import opcode
import statistics
import collections

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'exp7_c_extension'))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'exp10_c_extension'))

import _ctrace
import _ctrace2


# ============================================================================
# Bytecode analysis: two versions
# ============================================================================

STORE_FAST_OPS = set()
for name, op in opcode.opmap.items():
    if 'STORE_FAST' in name or name == 'STORE_NAME' or name == 'STORE_DEREF':
        STORE_FAST_OPS.add(op)

STORE_SUBSCR_OP = opcode.opmap.get('STORE_SUBSCR')
STORE_ATTR_OP = opcode.opmap.get('STORE_ATTR')
DELETE_SUBSCR_OP = opcode.opmap.get('DELETE_SUBSCR')
DELETE_ATTR_OP = opcode.opmap.get('DELETE_ATTR')

KNOWN_MUTATING_METHODS = {
    'append', 'clear', 'extend', 'insert', 'pop', 'remove', 'reverse', 'sort',
    'add', 'discard', 'difference_update', 'intersection_update',
    'symmetric_difference_update', 'update',
    'appendleft', 'extendleft', 'popleft', 'rotate',
    'resize', 'setdefault', 'popitem',
}


def analyze_store_fast_only(code):
    """Original analysis: only STORE_FAST."""
    varname_to_idx = {name: i for i, name in enumerate(code.co_varnames) if i < 64}
    line_bitmasks = {}
    for instr in dis.get_instructions(code):
        if instr.opcode in STORE_FAST_OPS and instr.positions.lineno is not None:
            idx = varname_to_idx.get(instr.argval)
            if idx is not None:
                line = instr.positions.lineno
                line_bitmasks[line] = line_bitmasks.get(line, 0) | (1 << idx)
    return line_bitmasks


def analyze_full_mutations(code):
    """Full analysis: STORE_FAST + SUBSCR + ATTR + method mutations."""
    instructions = list(dis.get_instructions(code))
    varname_to_idx = {name: i for i, name in enumerate(code.co_varnames) if i < 64}
    line_bitmasks = {}

    def set_bit(line, varname):
        idx = varname_to_idx.get(varname)
        if idx is not None:
            line_bitmasks[line] = line_bitmasks.get(line, 0) | (1 << idx)

    def find_load_fast_before(i, steps=3):
        for j in range(max(0, i - steps), i):
            if 'LOAD_FAST' in instructions[j].opname:
                return instructions[j].argval
        return None

    for i, instr in enumerate(instructions):
        line = instr.positions.lineno
        if line is None:
            continue
        if instr.opcode in STORE_FAST_OPS:
            set_bit(line, instr.argval)
        elif instr.opcode == STORE_SUBSCR_OP:
            t = find_load_fast_before(i, 3)
            if t: set_bit(line, t)
        elif instr.opcode == DELETE_SUBSCR_OP:
            t = find_load_fast_before(i, 2)
            if t: set_bit(line, t)
        elif instr.opcode == STORE_ATTR_OP:
            t = find_load_fast_before(i, 2)
            if t: set_bit(line, t)
        elif instr.opcode == DELETE_ATTR_OP:
            t = find_load_fast_before(i, 1)
            if t: set_bit(line, t)
        elif instr.opname == 'LOAD_ATTR' and instr.argval in KNOWN_MUTATING_METHODS:
            if i > 0 and 'LOAD_FAST' in instructions[i - 1].opname:
                set_bit(line, instructions[i - 1].argval)

    return line_bitmasks


def register_with_analysis(code, analyze_fn, visited=None):
    if visited is None:
        visited = set()
    if id(code) in visited:
        return
    visited.add(id(code))
    bitmasks = analyze_fn(code)
    first_line = code.co_firstlineno
    packed = [(line, mask) for line, mask in bitmasks.items()]
    _ctrace2.register_code(code, first_line, packed)
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            register_with_analysis(const, analyze_fn, visited)


# ============================================================================
# Mutation-heavy workloads
# ============================================================================

def wl_reassignment_heavy():
    """Mostly STORE_FAST — variable reassignment in loops."""
    total = 0
    x = 0
    y = 0
    for i in range(10000):
        x = i * 3
        y = x + 7
        total = total + y
        if i % 100 == 0:
            x = 0
            y = 0
    return total


def wl_list_mutation_heavy():
    """Heavy list mutations: append, pop, sort, insert."""
    items = []
    for i in range(5000):
        items.append(i)
        if len(items) > 100:
            items.pop(0)
        if i % 500 == 0:
            items.sort(reverse=True)
            items.reverse()
    result = []
    for i in range(0, len(items), 10):
        result.append(items[i])
    items.clear()
    return len(result)


def wl_dict_mutation_heavy():
    """Heavy dict mutations: subscript writes, update, pop."""
    d = {}
    for i in range(5000):
        d[f'key_{i}'] = i * 2
        if i % 100 == 0:
            d.update({f'batch_{i}': i})
        if i % 200 == 0 and i > 0:
            d.pop(f'key_{i-1}', None)
    total = 0
    for k, v in d.items():
        total += v
    d.clear()
    return total


def wl_object_attr_heavy():
    """Heavy object attribute mutations."""
    class Point:
        __slots__ = ('x', 'y', 'label')
        def __init__(self, x, y):
            self.x = x
            self.y = y
            self.label = ""
        def move(self, dx, dy):
            self.x += dx
            self.y += dy
        def set_label(self, label):
            self.label = label

    points = []
    for i in range(1000):
        p = Point(i, i * 2)
        p.set_label(f"p{i}")
        points.append(p)
    for _ in range(5):
        for p in points:
            p.move(1, -1)
    total = sum(p.x + p.y for p in points)
    return total


def wl_mixed_realistic():
    """Mixed: data processing pipeline with various mutation patterns."""
    records = []
    for i in range(3000):
        record = {
            'id': i,
            'name': f'item_{i}',
            'values': [j * i for j in range(5)],
            'score': 0.0,
        }
        records.append(record)

    # Mutate via subscript and method calls
    for r in records:
        r['score'] = sum(r['values']) / max(len(r['values']), 1)
        if r['score'] > 1000:
            r['values'].append(r['score'])

    # Filter and sort
    filtered = [r for r in records if r['score'] > 100]
    filtered.sort(key=lambda r: r['score'], reverse=True)

    # Build summary
    summary = {}
    for r in filtered[:100]:
        bucket = int(r['score']) // 100
        if bucket not in summary:
            summary[bucket] = []
        summary[bucket].append(r['id'])

    return len(filtered), len(summary)


WORKLOADS = {
    'reassign': wl_reassignment_heavy,
    'list_mut': wl_list_mutation_heavy,
    'dict_mut': wl_dict_mutation_heavy,
    'obj_attr': wl_object_attr_heavy,
    'mixed': wl_mixed_realistic,
}


# ============================================================================
# Measurement
# ============================================================================

def measure(workload_fn, n_warmup=3, n_rounds=10, iters_per_round=None):
    if iters_per_round is None:
        start = time.perf_counter_ns()
        workload_fn()
        single = time.perf_counter_ns() - start
        iters_per_round = max(1, 50_000_000 // max(single, 1))
        iters_per_round = min(iters_per_round, 50)

    # Warmup
    for _ in range(n_warmup):
        for _ in range(iters_per_round):
            workload_fn()

    # Measure
    round_times = []
    for _ in range(n_rounds):
        start = time.perf_counter_ns()
        for _ in range(iters_per_round):
            workload_fn()
        elapsed = time.perf_counter_ns() - start
        round_times.append(elapsed // iters_per_round)

    return {
        'median': statistics.median(round_times),
        'min': min(round_times),
        'stdev': statistics.stdev(round_times) if len(round_times) > 1 else 0,
        'iters': iters_per_round,
    }


# ============================================================================
# Main
# ============================================================================

def main():
    workload_names = list(WORKLOADS.keys())

    configs = [
        ('baseline', 'Baseline'),
        ('c_noop', 'C noop'),
        ('c_store_fast', 'C STORE_FAST'),
        ('c_full_mut', 'C full mut'),
        ('c_getlocals', 'C GetLocals'),
    ]

    print("=" * 80)
    print("Experiment 22: Mutation-Aware Tracing Performance")
    print("=" * 80)

    # First, show how many extra lines get flagged with full mutation analysis
    print("\n--- Bytecode analysis comparison ---")
    print(f"{'Workload':<15} {'STORE_FAST lines':>18} {'Full mut lines':>16} {'Extra lines':>13} {'Ratio':>8}")
    print("-" * 72)

    for name in workload_names:
        fn = WORKLOADS[name]
        sf = analyze_store_fast_only(fn.__code__)
        fm = analyze_full_mutations(fn.__code__)
        sf_count = len(sf)
        fm_count = len(fm)
        extra = fm_count - sf_count
        ratio = fm_count / max(sf_count, 1)
        print(f"{name:<15} {sf_count:>18} {fm_count:>16} {extra:>13} {ratio:>7.1f}x")

    # Performance measurements
    print(f"\n--- Performance ---")

    results = {cfg: {} for cfg, _ in configs}

    for cfg_key, cfg_label in configs:
        print(f"  Running {cfg_label}...")

        for name in workload_names:
            fn = WORKLOADS[name]

            if cfg_key == 'baseline':
                m = measure(fn)
            elif cfg_key == 'c_noop':
                _ctrace.start(0)
                m = measure(fn)
                _ctrace.stop()
            elif cfg_key == 'c_store_fast':
                _ctrace2.clear()
                register_with_analysis(fn.__code__, analyze_store_fast_only)
                _ctrace2.start(1)
                m = measure(fn)
                _ctrace2.stop()
            elif cfg_key == 'c_full_mut':
                _ctrace2.clear()
                register_with_analysis(fn.__code__, analyze_full_mutations)
                _ctrace2.start(1)
                m = measure(fn)
                _ctrace2.stop()
            elif cfg_key == 'c_getlocals':
                _ctrace.start(4)
                m = measure(fn)
                _ctrace.stop()

            results[cfg_key][name] = m

    # Print overhead ratios
    print(f"\n{'='*80}")
    print("Overhead ratios (median, vs baseline)")
    print(f"{'='*80}")

    non_base = [c for c in configs if c[0] != 'baseline']
    header = f"{'Workload':<15}"
    for _, label in non_base:
        header += f" {label:>14}"
    header += f" {'Full/SF delta':>14}"
    print(header)
    print("-" * len(header))

    for name in workload_names:
        b = results['baseline'][name]['median']
        row = f"{name:<15}"
        for cfg_key, _ in non_base:
            ratio = results[cfg_key][name]['median'] / b
            row += f" {ratio:>13.2f}x"
        # Delta: how much extra does full mutation analysis cost vs STORE_FAST only?
        sf_time = results['c_store_fast'][name]['median']
        fm_time = results['c_full_mut'][name]['median']
        delta = (fm_time - sf_time) / b
        row += f" {delta:>+13.2f}x"
        print(row)

    # Averages
    print()
    row = f"{'AVERAGE':<15}"
    for cfg_key, _ in non_base:
        ratios = [results[cfg_key][n]['median'] / results['baseline'][n]['median']
                  for n in workload_names]
        avg = sum(ratios) / len(ratios)
        row += f" {avg:>13.2f}x"
    deltas = [(results['c_full_mut'][n]['median'] - results['c_store_fast'][n]['median'])
              / results['baseline'][n]['median'] for n in workload_names]
    avg_delta = sum(deltas) / len(deltas)
    row += f" {avg_delta:>+13.2f}x"
    print(row)

    # Stats from full mutation run
    print(f"\n--- C extension stats (full mutation analysis, last workload) ---")
    _ctrace2.clear()
    for name in workload_names:
        register_with_analysis(WORKLOADS[name].__code__, analyze_full_mutations)
    _ctrace2.start(1)
    for name in workload_names:
        WORKLOADS[name]()
    _ctrace2.stop()
    stats = _ctrace2.stats()
    print(f"  Events: {stats['events']:,}")
    print(f"  Line events: {stats['line_events']:,}")
    print(f"  Lines with writes: {stats['lines_with_writes']:,}")
    print(f"  Vars checked: {stats['vars_checked']:,}")
    print(f"  Vars changed: {stats['vars_changed']:,}")
    if stats['line_events'] > 0:
        print(f"  % lines with writes: {100*stats['lines_with_writes']/stats['line_events']:.1f}%")
    if stats['vars_checked'] > 0:
        print(f"  % vars changed: {100*stats['vars_changed']/stats['vars_checked']:.1f}%")


if __name__ == '__main__':
    main()
