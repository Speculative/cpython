"""
Experiment 4: Value Serialization Benchmarks

Tests the cost of different approaches to serializing Python values
for trace capture. The key question: what's the cheapest way to
record "what are the local variables?" at each trace event.

Approaches:
  A) id() only (pointer identity - cheapest possible)
  B) type tag + id (know what kind of object, defer value capture)
  C) type tag + inline value for primitives (int/float/bool/None/small str)
  D) repr() for all values
  E) Selective: primitives inline, containers by id+len, objects by id+type
"""
import sys
import time
import types

ITERATIONS = 200_000


def make_test_locals():
    """Create a realistic set of local variables."""
    return {
        'x': 42,
        'y': 3.14,
        'name': 'hello',
        'flag': True,
        'nothing': None,
        'items': [1, 2, 3, 4, 5],
        'config': {'key': 'value', 'count': 10},
        'data': b'binary data here',
        'big_int': 10**18,
        'msg': 'a somewhat longer string for testing purposes',
    }


# === Approach A: id() only ===
def serialize_id_only(locals_dict):
    return [(k, id(v)) for k, v in locals_dict.items()]


# === Approach B: type tag + id ===
def serialize_type_id(locals_dict):
    return [(k, type(v).__name__, id(v)) for k, v in locals_dict.items()]


# === Approach C: inline primitives ===
def serialize_inline_primitives(locals_dict):
    result = []
    for k, v in locals_dict.items():
        t = type(v)
        if v is None or v is True or v is False:
            result.append((k, 'const', v))
        elif t is int:
            # Inline small ints, id for big ones
            if -2**63 < v < 2**63:
                result.append((k, 'int', v))
            else:
                result.append((k, 'bigint', id(v)))
        elif t is float:
            result.append((k, 'float', v))
        elif t is str:
            if len(v) <= 64:
                result.append((k, 'str', v))
            else:
                result.append((k, 'str:long', len(v), id(v)))
        elif t is bytes:
            result.append((k, 'bytes', len(v), id(v)))
        else:
            result.append((k, t.__name__, id(v)))
    return result


# === Approach D: repr() everything ===
def serialize_repr(locals_dict):
    return [(k, repr(v)) for k, v in locals_dict.items()]


# === Approach E: Selective ===
def serialize_selective(locals_dict):
    result = []
    for k, v in locals_dict.items():
        t = type(v)
        if v is None or v is True or v is False:
            result.append((k, v))
        elif t is int:
            result.append((k, v if -2**63 < v < 2**63 else ('bigint', id(v))))
        elif t is float:
            result.append((k, v))
        elif t is str:
            result.append((k, v if len(v) <= 64 else f'str[{len(v)}]'))
        elif t is list:
            result.append((k, f'list[{len(v)}]', id(v)))
        elif t is dict:
            result.append((k, f'dict[{len(v)}]', id(v)))
        elif t is tuple:
            result.append((k, f'tuple[{len(v)}]', id(v)))
        elif t is bytes:
            result.append((k, f'bytes[{len(v)}]', id(v)))
        else:
            result.append((k, t.__name__, id(v)))
    return result


# === Approach F: Change detection (pointer comparison) ===
class ChangeDetector:
    def __init__(self, n_locals):
        self.prev = [None] * n_locals

    def detect_and_serialize(self, locals_list):
        """Given a list of local values, return only changed ones."""
        changes = []
        for i, v in enumerate(locals_list):
            if v is not self.prev[i]:
                self.prev[i] = v
                changes.append((i, v))
        return changes


def bench(name, func, arg, iterations):
    # warmup
    for _ in range(1000):
        func(arg)

    start = time.perf_counter_ns()
    for _ in range(iterations):
        func(arg)
    elapsed = time.perf_counter_ns() - start
    per_call = elapsed / iterations
    return per_call


def main():
    print("=" * 70)
    print("Experiment 4: Value Serialization Benchmarks")
    print("=" * 70)

    test_locals = make_test_locals()
    n_vars = len(test_locals)

    print(f"\nTest locals: {n_vars} variables")
    print(f"Types: {', '.join(type(v).__name__ for v in test_locals.values())}")
    print(f"Iterations: {ITERATIONS:,}")

    approaches = [
        ("A: id() only", serialize_id_only),
        ("B: type + id", serialize_type_id),
        ("C: inline primitives", serialize_inline_primitives),
        ("D: repr() all", serialize_repr),
        ("E: selective", serialize_selective),
    ]

    print(f"\n{'Approach':<25} {'ns/call':>10} {'us/call':>10} {'Relative':>10}")
    print("-" * 57)

    baseline = None
    for name, func in approaches:
        per_call = bench(name, func, test_locals, ITERATIONS)
        if baseline is None:
            baseline = per_call
        ratio = per_call / baseline
        print(f"{name:<25} {per_call:>10.0f} {per_call/1000:>10.2f} {ratio:>9.1f}x")

    # Change detection benchmark
    print(f"\n--- Change detection ---")
    locals_list = list(test_locals.values())
    detector = ChangeDetector(n_vars)

    # First call: everything is new
    detector.detect_and_serialize(locals_list)

    # Subsequent calls: nothing changed
    per_call_nochange = bench(
        "no change",
        detector.detect_and_serialize,
        locals_list,
        ITERATIONS,
    )

    # Simulate 2 out of 10 changing
    def simulate_partial_change(locals_list):
        # Mutate 2 variables
        modified = list(locals_list)
        modified[0] = modified[0] + 1 if isinstance(modified[0], int) else modified[0]
        modified[2] = modified[2] + '!' if isinstance(modified[2], str) else modified[2]
        return modified

    changing_locals = simulate_partial_change(locals_list)
    detector2 = ChangeDetector(n_vars)
    detector2.detect_and_serialize(locals_list)  # init

    per_call_partial = bench(
        "2/10 changed",
        detector2.detect_and_serialize,
        changing_locals,
        ITERATIONS,
    )

    print(f"{'No vars changed':<25} {per_call_nochange:>10.0f} {per_call_nochange/1000:>10.2f} {per_call_nochange/baseline:>9.1f}x")
    print(f"{'2/10 vars changed':<25} {per_call_partial:>10.0f} {per_call_partial/1000:>10.2f} {per_call_partial/baseline:>9.1f}x")

    # Context: how does this compare to event overhead?
    print(f"\n--- Context: event overhead comparison ---")
    print(f"From Experiment 1, PEP 669 LINE callback overhead is ~500-2000ns per event")
    print(f"Serialization cost per event with {n_vars} locals:")
    for name, func in approaches:
        per_call = bench(name, func, test_locals, ITERATIONS)
        print(f"  {name:<25} {per_call:>7.0f}ns = {per_call/1000:>5.2f}us")
    print(f"  {'Change detect (0 chg)':<25} {per_call_nochange:>7.0f}ns = {per_call_nochange/1000:>5.2f}us")
    print(f"  {'Change detect (2 chg)':<25} {per_call_partial:>7.0f}ns = {per_call_partial/1000:>5.2f}us")


if __name__ == '__main__':
    main()
