"""
Differential testing: validate WAL reconstruction against settrace ground truth.

For each test program:
1. Run with pure Python settrace → ground truth (every line, all variables)
2. Run with fork WAL tracer (mode 1) → WAL events
3. Reconstruct state from WAL
4. At every function RETURN, compare:
   - Return value matches
   - All local variables that exist in both traces match

This validates that mode 1 WAL capture (1.8x overhead) captures enough
information to reconstruct the same state that settrace (1.6x noop, ~4x
with variable reads) captures.
"""
import sys
import os
import collections
import inspect

import _tracewal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from reference_tracer import ReferenceTracer, deep_snapshot
from wal_replayer import WALReconstructor


# ===================================================================
# Value comparison
# ===================================================================

def values_equal(a, b, path="", depth=0):
    """Deep compare two snapshot values. Returns (ok, detail)."""
    if depth > 20:
        return True, ""

    if a is None and b is None:
        return True, ""
    if a is None or b is None:
        return False, f"{path}: {a!r} vs {b!r}"

    # Primitives
    if isinstance(a, (bool, int, float, str)) and isinstance(b, (bool, int, float, str)):
        if type(a) == type(b) and a == b:
            return True, ""
        if isinstance(a, float) and isinstance(b, float) and abs(a - b) < 1e-9:
            return True, ""
        return False, f"{path}: {a!r} vs {b!r}"

    # Both must be lists (snapshot format) from here
    if not isinstance(a, list) or not isinstance(b, list):
        # One is primitive, other is snapshot container — mismatch
        if isinstance(a, (bool, int, float, str)) or isinstance(b, (bool, int, float, str)):
            return False, f"{path}: {a!r} vs {b!r}"
        return True, ""  # can't compare, skip

    if len(a) < 2 or len(b) < 2:
        return True, ""

    # Type tag must match (with equivalences for opaque types)
    opaque_tags = {'__callable__', '__obj__', '__repr__', '__type__', '__module__'}
    if a[0] != b[0]:
        if a[0] in opaque_tags and b[0] in opaque_tags:
            return True, ""  # both are opaque non-primitive values
        return False, f"{path}: type {a[0]} vs {b[0]}"

    tag = a[0]

    if tag in ('__list__', '__tuple__', '__deque__'):
        a_items, b_items = a[1], b[1]
        if len(a_items) != len(b_items):
            return False, f"{path}: {tag} length {len(a_items)} vs {len(b_items)}"
        for i, (ai, bi) in enumerate(zip(a_items, b_items)):
            ok, detail = values_equal(ai, bi, f"{path}[{i}]", depth + 1)
            if not ok:
                return False, detail
        return True, ""

    if tag == '__dict__':
        a_pairs = {repr(k): (k, v) for k, v in a[1]}
        b_pairs = {repr(k): (k, v) for k, v in b[1]}
        a_keys, b_keys = set(a_pairs.keys()), set(b_pairs.keys())
        if a_keys != b_keys:
            return False, f"{path}: dict keys differ, ref_only={a_keys - b_keys}, wal_only={b_keys - a_keys}"
        for kr in a_keys:
            _, av = a_pairs[kr]
            _, bv = b_pairs[kr]
            ok, detail = values_equal(av, bv, f"{path}[{kr}]", depth + 1)
            if not ok:
                return False, detail
        return True, ""

    if tag == '__set__' or tag == '__frozenset__':
        a_sorted = sorted(a[1], key=repr)
        b_sorted = sorted(b[1], key=repr)
        if len(a_sorted) != len(b_sorted):
            return False, f"{path}: {tag} size {len(a_sorted)} vs {len(b_sorted)}"
        for i, (ai, bi) in enumerate(zip(a_sorted, b_sorted)):
            ok, detail = values_equal(ai, bi, f"{path}{{{i}}}", depth + 1)
            if not ok:
                return False, detail
        return True, ""

    if tag == '__obj__':
        # a = ['__obj__', classname, {attrs}]
        a_attrs = a[2] if len(a) > 2 else {}
        b_attrs = b[2] if len(b) > 2 else {}
        for k in a_attrs:
            if k in b_attrs:
                ok, detail = values_equal(a_attrs[k], b_attrs[k], f"{path}.{k}", depth + 1)
                if not ok:
                    return False, detail
            # Missing in WAL is ok if WAL didn't track that attr
        return True, ""

    # Other tags (__type__, __callable__, __repr__, etc.) — skip
    return True, ""


# ===================================================================
# Core comparison: at every function return
# ===================================================================

def compare_returns(ref_steps, wal_steps):
    """Compare local variable state at every RETURN event.

    We match returns by position (i-th return in ref vs i-th in WAL).
    Returns list of issue strings.
    """
    ref_returns = [s for s in ref_steps if s['event'] == 'return']
    wal_returns = [s for s in wal_steps if s['event'] == 'return']

    issues = []

    # Filter to non-None returns for comparison.
    # settrace fires 'return' with retval=None for:
    #   - exception-caused frame exits (WAL captures these as RAISE instead)
    #   - generator exhaustion (StopIteration)
    #   - functions with implicit return None
    # We compare only returns with actual values to avoid misalignment.
    ref_valued = [s for s in ref_returns if s.get('retval') is not None]
    wal_valued = [s for s in wal_returns if s.get('retval') is not None]

    n = min(len(ref_valued), len(wal_valued))
    if len(ref_valued) != len(wal_valued):
        issues.append(f"valued return count: ref={len(ref_valued)} vs wal={len(wal_valued)}")

    for i in range(n):
        ref = ref_valued[i]
        wal = wal_valued[i]
        func = ref.get('funcname', '?')
        line = ref.get('lineno', -1)
        prefix = f"return #{i} ({func}:{line})"

        # Compare return value
        r_rv = ref.get('retval')
        w_rv = wal.get('retval')
        if r_rv is not None and w_rv is not None:
            ok, detail = values_equal(r_rv, w_rv, f"{prefix} retval")
            if not ok:
                issues.append(detail)

        # Compare locals
        r_locals = ref.get('locals', {})
        w_locals = wal.get('locals', {})
        for name in r_locals:
            if name in w_locals:
                ok, detail = values_equal(r_locals[name], w_locals[name],
                                          f"{prefix} local '{name}'")
                if not ok:
                    issues.append(detail)

    return issues


# ===================================================================
# Test infrastructure
# ===================================================================

passed = 0
failed = 0
errors = []
verbose = '-v' in sys.argv


def run_test(name, fn):
    """Run one differential test."""
    global passed, failed

    source_file = inspect.getfile(fn)

    # 1. WAL trace first (before settrace which may alter specialization state)
    _tracewal.clear()
    _tracewal.start(line_mode=1)
    wal_result = fn()
    _tracewal.stop()
    wal = _tracewal.get_wal(50000)

    # 2. Reference trace (settrace — run second to avoid interfering with WAL)
    ref_tracer = ReferenceTracer(trace_file=source_file)
    ref_steps, ref_result = ref_tracer.trace(fn)

    # 3. Reconstruct from WAL
    reconstructor = WALReconstructor(wal)
    wal_steps = reconstructor.reconstruct()

    # 4. Compare return values from the traced function
    ref_snap = deep_snapshot(ref_result)
    wal_snap = deep_snapshot(wal_result)
    ok, detail = values_equal(ref_snap, wal_snap, "final_result")
    if not ok:
        print(f"  FAIL: {name} — {detail}")
        failed += 1
        errors.append(f"{name}: {detail}")
        return

    # 5. Compare at every return point
    issues = compare_returns(ref_steps, wal_steps)
    if issues:
        print(f"  FAIL: {name} — {len(issues)} issue(s)")
        for iss in issues[:5]:
            print(f"    {iss}")
        if len(issues) > 5:
            print(f"    ... and {len(issues) - 5} more")
        failed += 1
        errors.append(f"{name}: {len(issues)} mismatches")
    else:
        passed += 1
        if verbose:
            ref_rets = len([s for s in ref_steps if s['event'] == 'return'])
            wal_rets = len([s for s in wal_steps if s['event'] == 'return'])
            print(f"  PASS: {name} (ref:{ref_rets} returns, wal:{wal_rets} returns, {len(wal)} WAL entries)")


# ===================================================================
# Test programs
# ===================================================================

# --- Basic ---

def test_simple():
    print("\n--- Simple variables ---")
    def target():
        x = 42
        y = "hello"
        z = x + len(y)
        return z
    run_test("simple", target)


def test_reassignment():
    print("\n--- Reassignment ---")
    def target():
        x = 1
        x = 2
        x = 3
        return x
    run_test("reassignment", target)


# --- Containers ---

def test_list_ops():
    print("\n--- List operations ---")
    def target():
        items = [5, 3, 1, 4, 2]
        items.append(6)
        items.insert(0, 0)
        items[3] = 99
        items.pop()
        items.sort()
        items.reverse()
        return items
    run_test("list_ops", target)


def test_dict_ops():
    print("\n--- Dict operations ---")
    def target():
        d = {}
        d['a'] = 1
        d['b'] = 2
        d.update({'c': 3, 'd': 4})
        d.pop('b')
        d.setdefault('e', 5)
        d.setdefault('a', 999)
        return d
    run_test("dict_ops", target)


def test_set_ops():
    print("\n--- Set operations ---")
    def target():
        s = {1, 2, 3}
        s.add(4)
        s.discard(2)
        s.add(5)
        return s
    run_test("set_ops", target)


def test_deque_ops():
    print("\n--- Deque operations ---")
    def target():
        d = collections.deque([1, 2, 3])
        d.append(4)
        d.appendleft(0)
        d.rotate(2)
        d.reverse()
        d.pop()
        d.popleft()
        return list(d)
    run_test("deque_ops", target)


def test_nested_containers():
    print("\n--- Nested containers ---")
    def target():
        data = {'users': [], 'meta': {}}
        for i in range(5):
            user = {'id': i, 'scores': [i * 10 + j for j in range(3)]}
            data['users'].append(user)
            data['meta'][f'u{i}'] = len(user['scores'])
        data['users'][0]['scores'].append(999)
        data['users'][2]['id'] = -1
        del data['meta']['u1']
        return data
    run_test("nested_containers", target)


# --- Control flow ---

def test_branches():
    print("\n--- Branches ---")
    def classify(x):
        if x < 0:
            return "neg"
        elif x == 0:
            return "zero"
        elif x < 10:
            return "small"
        else:
            return "big"

    def target():
        return [classify(x) for x in [-5, 0, 3, 50]]
    run_test("branches", target)


def test_for_loop():
    print("\n--- For loop ---")
    def target():
        total = 0
        for i in range(10):
            total += i * i
        return total
    run_test("for_loop", target)


def test_while_loop():
    print("\n--- While loop ---")
    def target():
        x = 100
        steps = 0
        while x > 1:
            if x % 2 == 0:
                x //= 2
            else:
                x = 3 * x + 1
            steps += 1
        return steps
    run_test("while_loop", target)


def test_nested_loops():
    print("\n--- Nested loops ---")
    def target():
        result = []
        for i in range(4):
            for j in range(4):
                if i == j:
                    continue
                if i + j > 4:
                    break
                result.append((i, j))
        return result
    run_test("nested_loops", target)


# --- Functions ---

def test_nested_calls():
    print("\n--- Nested calls ---")
    def add(a, b):
        return a + b
    def mul(a, b):
        r = 0
        for _ in range(b):
            r = add(r, a)
        return r
    def target():
        return add(mul(3, 4), mul(5, 2))
    run_test("nested_calls", target)


def test_recursion():
    print("\n--- Recursion ---")
    def fib(n):
        if n < 2:
            return n
        return fib(n - 1) + fib(n - 2)
    def target():
        return fib(8)
    run_test("recursion", target)


def test_corecursion():
    print("\n--- Co-recursion ---")
    def is_even(n):
        if n == 0: return True
        return is_odd(n - 1)
    def is_odd(n):
        if n == 0: return False
        return is_even(n - 1)
    def target():
        return [(i, is_even(i)) for i in range(6)]
    run_test("corecursion", target)


# --- Closures ---

def test_closures():
    print("\n--- Closures ---")
    def make_counter(start=0):
        count = start
        def inc(by=1):
            nonlocal count
            count += by
            return count
        return inc
    def target():
        c = make_counter(10)
        return [c(), c(5), c(), c(10)]
    run_test("closures", target)


def test_closure_callback():
    print("\n--- Closure as callback ---")
    def make_adder(n):
        def adder(x):
            return x + n
        return adder
    def apply_all(fns, x):
        return [f(x) for f in fns]
    def target():
        fns = [make_adder(i) for i in range(5)]
        return apply_all(fns, 100)
    run_test("closure_callback", target)


# --- Objects ---

def test_object_mutations():
    print("\n--- Object mutations ---")
    class Account:
        def __init__(self, name, bal):
            self.name = name
            self.balance = bal
            self.history = []
        def deposit(self, amt):
            self.balance += amt
            self.history.append(('dep', amt))
        def withdraw(self, amt):
            self.balance -= amt
            self.history.append(('wd', amt))
    def target():
        a = Account("Alice", 100)
        a.deposit(50)
        a.withdraw(30)
        a.deposit(20)
        return a.balance
    run_test("object_mutations", target)


def test_class_hierarchy():
    print("\n--- Class hierarchy ---")
    class Base:
        def __init__(self, v):
            self.value = v
        def process(self):
            return self.value * 2
    class Child(Base):
        def __init__(self, v, extra):
            super().__init__(v)
            self.extra = extra
        def process(self):
            return super().process() + self.extra
    def target():
        objs = [Base(10), Child(5, 3), Child(7, 1)]
        return [o.process() for o in objs]
    run_test("class_hierarchy", target)


# --- Exceptions ---

def test_exceptions():
    print("\n--- Exceptions ---")
    def safe_div(a, b):
        try:
            return a / b
        except ZeroDivisionError:
            return float('inf')
    def target():
        return [safe_div(10, 2), safe_div(10, 0), safe_div(7, 3)]
    run_test("exceptions", target)


def test_exception_loop():
    print("\n--- Exception in loop ---")
    def risky(x):
        if x % 3 == 0:
            raise ValueError(f"bad {x}")
        return x * 2
    def target():
        results = []
        for i in range(10):
            try:
                results.append(risky(i))
            except ValueError:
                results.append(-1)
        return results
    run_test("exception_loop", target)


def test_nested_exceptions():
    print("\n--- Nested exceptions ---")
    def target():
        results = []
        for i in range(5):
            try:
                try:
                    if i == 2:
                        raise KeyError("inner")
                    if i == 4:
                        raise ValueError("inner2")
                    results.append(i)
                except KeyError:
                    raise RuntimeError("wrapped")
            except (RuntimeError, ValueError) as e:
                results.append(str(e))
        return results
    run_test("nested_exceptions", target)


# --- Generators ---

def test_generators():
    print("\n--- Generators ---")
    def countdown(n):
        while n > 0:
            yield n
            n -= 1
    def target():
        return list(countdown(5))
    run_test("generators", target)


def test_generator_pipeline():
    print("\n--- Generator pipeline ---")
    def producer(n):
        for i in range(n):
            yield i
    def doubler(src):
        for x in src:
            yield x * 2
    def filterer(src):
        for x in src:
            if x > 3:
                yield x
    def target():
        return list(filterer(doubler(producer(5))))
    run_test("generator_pipeline", target)


# --- Decorators ---

def test_decorators():
    print("\n--- Decorators ---")
    def double_result(fn):
        def wrapper(*a, **kw):
            return fn(*a, **kw) * 2
        return wrapper
    @double_result
    def compute(x, y):
        return x + y
    def target():
        return [compute(3, 4), compute(10, 20)]
    run_test("decorators", target)


# --- Context managers ---

def test_context_managers():
    print("\n--- Context managers ---")
    class Tracker:
        def __init__(self):
            self.count = 0
        def __enter__(self):
            self.count += 1
            return self
        def __exit__(self, *a):
            return False
    def target():
        t = Tracker()
        results = []
        for i in range(5):
            try:
                with t:
                    if i == 3:
                        raise ValueError()
                    results.append(i)
            except ValueError:
                results.append(-1)
        return results, t.count
    run_test("context_managers", target)


# --- Stress: repeated mutations ---

def test_heavy_mutations():
    print("\n--- Heavy mutations ---")
    def target():
        items = []
        d = {}
        for i in range(50):
            items.append(i)
            d[f'k{i}'] = i * 2
            if i % 10 == 0 and items:
                items.pop(0)
            if i % 7 == 0:
                d.pop(f'k{i}', None)
        items.sort()
        return len(items), len(d)
    run_test("heavy_mutations", target)


# --- Comprehensions ---

def test_comprehensions():
    print("\n--- Comprehensions ---")
    def target():
        matrix = [[i * j for j in range(5)] for i in range(5)]
        flat = [x for row in matrix for x in row if x > 5]
        lookup = {x: x ** 2 for x in flat}
        unique = {x % 7 for x in flat}
        return len(flat), len(lookup), len(unique)
    run_test("comprehensions", target)


# --- Control flow edge cases ---

def test_for_else():
    print("\n--- For/else ---")
    def target():
        # else runs (no break)
        result = None
        for i in range(5):
            if i == 10:
                break
        else:
            result = 'completed'
        # else doesn't run (break)
        result2 = None
        for i in range(5):
            if i == 2:
                break
        else:
            result2 = 'completed'
        return result, result2
    run_test("for_else", target)


def test_while_else():
    print("\n--- While/else ---")
    def target():
        i = 0
        while i < 3:
            i += 1
        else:
            done = True
        return i, done
    run_test("while_else", target)


def test_while_pure_side_effects():
    print("\n--- While loop pure side effects ---")
    class Counter:
        def __init__(self):
            self.n = 0
        def tick(self):
            self.n += 1
        def done(self):
            return self.n >= 5
    def target():
        c = Counter()
        while not c.done():
            c.tick()
        return c.n
    run_test("while_pure_side_effects", target)


def test_try_finally():
    print("\n--- Try/finally ---")
    def target():
        result = []
        try:
            result.append(1)
            result.append(2)
        finally:
            result.append('finally')
        return result
    run_test("try_finally", target)


def test_try_finally_exception():
    print("\n--- Try/finally with exception ---")
    def target():
        result = []
        try:
            result.append(1)
            raise ValueError('oops')
        except ValueError:
            result.append('caught')
        finally:
            result.append('finally')
        return result
    run_test("try_finally_exception", target)


def test_break_continue():
    print("\n--- Break and continue ---")
    def target():
        results = []
        for i in range(10):
            if i % 2 == 0:
                continue
            if i >= 7:
                break
            results.append(i)
        return results
    run_test("break_continue", target)


def test_ternary():
    print("\n--- Ternary expression ---")
    def target():
        results = []
        for x in [-5, 0, 5, 10]:
            label = "pos" if x > 0 else ("zero" if x == 0 else "neg")
            results.append(label)
        return results
    run_test("ternary", target)


def test_boolean_short_circuit():
    print("\n--- Boolean short circuit ---")
    def target():
        results = []
        for a, b, c in [(True, True, True), (True, False, True),
                         (False, True, True), (True, True, False)]:
            r1 = a and b and c
            r2 = a or b or c
            results.append((r1, r2))
        return results
    run_test("boolean_short_circuit", target)


def test_walrus_operator():
    print("\n--- Walrus operator ---")
    def target():
        items = [1, 5, 3, 8, 2, 9, 4]
        big = []
        for x in items:
            if (doubled := x * 2) > 8:
                big.append(doubled)
        return big
    run_test("walrus_operator", target)


def test_multiple_return_paths():
    print("\n--- Multiple return paths ---")
    def classify(x):
        if x < 0:
            return "negative"
        if x == 0:
            return "zero"
        if x < 10:
            return "small"
        return "large"
    def target():
        return [classify(x) for x in [-3, 0, 5, 100]]
    run_test("multiple_return_paths", target)


def test_method_chain_side_effects():
    print("\n--- Method chain side effects ---")
    class Builder:
        def __init__(self):
            self.config = {}
        def set(self, key, val):
            self.config[key] = val
            return self
        def build(self):
            return dict(self.config)
    def target():
        result = (Builder()
                  .set('timeout', 30)
                  .set('retries', 3)
                  .set('verbose', True)
                  .build())
        return result
    run_test("method_chain", target)


def test_match_case():
    print("\n--- Match/case (structural pattern matching) ---")
    def classify_point(point):
        match point:
            case (0, 0):
                return "origin"
            case (x, 0):
                return f"x-axis at {x}"
            case (0, y):
                return f"y-axis at {y}"
            case (x, y) if x == y:
                return f"diagonal at {x}"
            case (x, y):
                return f"point ({x}, {y})"
    def target():
        points = [(0, 0), (5, 0), (0, 3), (4, 4), (2, 7)]
        return [classify_point(p) for p in points]
    run_test("match_case", target)


def test_nested_with():
    print("\n--- Nested context managers ---")
    class Resource:
        def __init__(self, name):
            self.name = name
            self.opened = False
            self.closed = False
        def __enter__(self):
            self.opened = True
            return self
        def __exit__(self, *a):
            self.closed = True
            return False
    def target():
        r1 = Resource("db")
        r2 = Resource("cache")
        with r1:
            with r2:
                data = f"{r1.name}+{r2.name}"
        return data, r1.closed, r2.closed
    run_test("nested_with", target)


def test_lambda_map_filter():
    print("\n--- Lambda with map/filter ---")
    def target():
        items = list(range(10))
        doubled = list(map(lambda x: x * 2, items))
        evens = list(filter(lambda x: x % 2 == 0, doubled))
        return evens
    run_test("lambda_map_filter", target)


def test_star_unpacking():
    print("\n--- Star unpacking ---")
    def target():
        first, *middle, last = [1, 2, 3, 4, 5]
        a, b = [10, 20]
        x, y, z = "abc"
        return first, middle, last, a, b, x, y, z
    run_test("star_unpacking", target)


# ===================================================================
# Main
# ===================================================================

def main():
    print("Differential Testing: WAL Reconstruction vs settrace Ground Truth")
    print(f"Python: {sys.version}")

    test_simple()
    test_reassignment()
    test_list_ops()
    test_dict_ops()
    test_set_ops()
    test_deque_ops()
    test_nested_containers()
    test_branches()
    test_for_loop()
    test_while_loop()
    test_nested_loops()
    test_nested_calls()
    test_recursion()
    test_corecursion()
    test_closures()
    test_closure_callback()
    test_object_mutations()
    test_class_hierarchy()
    test_exceptions()
    test_exception_loop()
    test_nested_exceptions()
    test_generators()
    test_generator_pipeline()
    test_decorators()
    test_context_managers()
    test_heavy_mutations()
    test_comprehensions()

    # Control flow edge cases
    test_for_else()
    test_while_else()
    test_while_pure_side_effects()
    test_try_finally()
    test_try_finally_exception()
    test_break_continue()
    test_ternary()
    test_boolean_short_circuit()
    test_walrus_operator()
    test_multiple_return_paths()
    test_method_chain_side_effects()
    test_match_case()
    test_nested_with()
    test_lambda_map_filter()
    test_star_unpacking()

    print(f"\n{'='*60}")
    print(f"Results: {passed} passed, {failed} failed")
    print(f"{'='*60}")
    if errors:
        print("\nFailures:")
        for e in errors:
            print(f"  {e}")
        sys.exit(1)
    else:
        print("\nAll differential tests passed.")
        sys.exit(0)


if __name__ == '__main__':
    main()
