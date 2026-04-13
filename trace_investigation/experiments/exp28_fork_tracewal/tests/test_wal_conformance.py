"""
WAL Tracing Conformance Test Suite

Tests that the inline WAL tracer (fork) correctly captures all events needed
to reconstruct a full debugger-style view of program execution.

Categories:
  1. Variable capture (STORE_FAST) — local variable BIND/UNBIND
  2. Object mutations (STORE_ATTR, STORE_SUBSCR) — SETATTR, SETITEM, DELITEM
  3. Mutating method calls — MUTATE via CALL hook
  4. Control flow — CALL/RETURN, LINE events at branches/loops
  5. Exception tracing — RAISE with origin line, EXCEPT for handler entry
  6. Object identity — OID tracking, aliasing
  7. Generators / async — YIELD, RESUME
  8. Global / nonlocal / closure — STORE_GLOBAL, STORE_DEREF
  9. Post-mutation snapshots — SNAPSHOT after sort/reverse
  10. Complex real-world patterns

Run with:
    ./python tests/test_wal_conformance.py [-v]
"""
import sys
import os
import collections

import _tracewal

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

passed = 0
failed = 0
errors = []
verbose = '-v' in sys.argv


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        if verbose:
            print(f"  PASS: {name}")
    else:
        failed += 1
        msg = f"  FAIL: {name}"
        if detail:
            msg += f" -- {detail}"
        print(msg)
        errors.append(msg)


def run_traced(fn, line_mode=1):
    """Run fn under WAL tracing, return the WAL entries."""
    _tracewal.clear()
    _tracewal.start(line_mode=line_mode)
    result = fn()
    _tracewal.stop()
    return _tracewal.get_wal(2000), result


def events_of_type(wal, event_type):
    return [e for e in wal if e['event'] == event_type]


def events_matching(wal, **kwargs):
    """Filter WAL entries matching all given key=value pairs."""
    results = []
    for e in wal:
        if all(e.get(k) == v for k, v in kwargs.items()):
            results.append(e)
    return results


# ===================================================================
# 1. Variable capture (STORE_FAST)
# ===================================================================

def test_primitive_binds():
    """Primitive values (int, float, str, bool, None) emit BIND with inline value."""
    print("\n--- 1a: Primitive variable binds ---")

    def target():
        x = 42
        y = 3.14
        z = "hello"
        b = True
        n = None
        return x, y, z, b, n

    wal, _ = run_traced(target)
    binds = events_of_type(wal, 'BIND')

    x_binds = [e for e in binds if e.get('name') == 'x']
    check("int bind", any(e.get('value') == 42 for e in x_binds), f"{x_binds}")
    y_binds = [e for e in binds if e.get('name') == 'y']
    check("float bind", any(e.get('value') == 3.14 for e in y_binds), f"{y_binds}")
    z_binds = [e for e in binds if e.get('name') == 'z']
    check("str bind", any(e.get('value') == 'hello' for e in z_binds), f"{z_binds}")
    b_binds = [e for e in binds if e.get('name') == 'b']
    check("bool bind", any(e.get('value') == True for e in b_binds), f"{b_binds}")
    n_binds = [e for e in binds if e.get('name') == 'n']
    check("None bind", any(e.get('value') is None for e in n_binds), f"{n_binds}")


def test_mutable_object_binds():
    """Mutable objects (list, dict, set, class instances) emit BIND with oid."""
    print("\n--- 1b: Mutable object binds ---")

    def target():
        items = [1, 2, 3]
        data = {'a': 1}
        s = {1, 2}
        return items, data, s

    wal, _ = run_traced(target)
    binds = events_of_type(wal, 'BIND')
    creates = events_of_type(wal, 'CREATE')

    items_bind = [e for e in binds if e.get('name') == 'items' and e.get('oid', 0) > 0]
    check("list bind with oid", len(items_bind) >= 1, f"{items_bind}")
    data_bind = [e for e in binds if e.get('name') == 'data' and e.get('oid', 0) > 0]
    check("dict bind with oid", len(data_bind) >= 1, f"{data_bind}")
    check("CREATE events for mutable objects", len(creates) >= 3, f"got {len(creates)}")


def test_variable_reassignment():
    """Reassignment emits UNBIND(old) + BIND(new) when oid changes."""
    print("\n--- 1c: Variable reassignment ---")

    def target():
        x = [1, 2]
        x = [3, 4]  # new object, different oid
        return x

    wal, _ = run_traced(target)
    unbinds = events_matching(wal, event='UNBIND', name='x')
    binds = [e for e in events_of_type(wal, 'BIND') if e.get('name') == 'x' and e.get('oid', 0) > 0]
    check("UNBIND on reassignment", len(unbinds) >= 1, f"{unbinds}")
    check("two BIND events for x", len(binds) >= 2, f"{binds}")
    if len(binds) >= 2:
        check("different oids", binds[0]['oid'] != binds[1]['oid'],
              f"oid1={binds[0]['oid']}, oid2={binds[1]['oid']}")


def test_function_arguments():
    """Function arguments emit BIND at CALL time."""
    print("\n--- 1d: Function argument capture ---")

    def inner(a, b, c=10):
        return a + b + c

    def target():
        return inner(1, 2, c=30)

    wal, result = run_traced(target)
    check("result correct", result == 33)
    binds = events_of_type(wal, 'BIND')
    a_bind = [e for e in binds if e.get('name') == 'a']
    b_bind = [e for e in binds if e.get('name') == 'b']
    c_bind = [e for e in binds if e.get('name') == 'c']
    check("arg a captured", any(e.get('value') == 1 for e in a_bind), f"{a_bind}")
    check("arg b captured", any(e.get('value') == 2 for e in b_bind), f"{b_bind}")
    check("kwarg c captured", any(e.get('value') == 30 for e in c_bind), f"{c_bind}")


def test_tuple_unpacking():
    """Tuple unpacking produces BIND for each target variable."""
    print("\n--- 1e: Tuple unpacking ---")

    def target():
        a, b, c = 1, 2, 3
        x, *rest = [10, 20, 30, 40]
        return a, b, c, x, rest

    wal, _ = run_traced(target)
    binds = events_of_type(wal, 'BIND')
    names = [e.get('name') for e in binds]
    check("a captured", 'a' in names)
    check("b captured", 'b' in names)
    check("c captured", 'c' in names)


# ===================================================================
# 2. Object mutations (STORE_ATTR, STORE_SUBSCR, DELETE)
# ===================================================================

def test_store_attr():
    """STORE_ATTR emits SETATTR with attr name and value."""
    print("\n--- 2a: Attribute set ---")

    class Point:
        def __init__(self, x, y):
            self.x = x
            self.y = y

    def target():
        p = Point(1, 2)
        p.x = 10
        p.y = 20
        return p

    wal, _ = run_traced(target)
    setattrs = events_of_type(wal, 'SETATTR')
    x_sets = [e for e in setattrs if e.get('attr') == 'x']
    y_sets = [e for e in setattrs if e.get('attr') == 'y']
    check("p.x set in __init__", any(e.get('value') == 1 for e in x_sets), f"{x_sets}")
    check("p.x set to 10", any(e.get('value') == 10 for e in x_sets), f"{x_sets}")
    check("p.y set to 20", any(e.get('value') == 20 for e in y_sets), f"{y_sets}")


def test_store_subscr():
    """STORE_SUBSCR emits SETITEM with key and value."""
    print("\n--- 2b: Container item set ---")

    def target():
        items = [1, 2, 3]
        items[0] = 99
        d = {}
        d['key'] = 'value'
        d[42] = True
        return items, d

    wal, _ = run_traced(target)
    setitems = events_of_type(wal, 'SETITEM')
    check("list setitem captured", any(e.get('key') == 0 and e.get('value') == 99 for e in setitems),
          f"{setitems}")
    check("dict setitem captured", any(e.get('key') == 'key' for e in setitems), f"{setitems}")
    check("int key dict setitem", any(e.get('key') == 42 for e in setitems), f"{setitems}")


def test_delete_subscr():
    """DELETE_SUBSCR emits DELITEM."""
    print("\n--- 2c: Container item delete ---")

    def target():
        d = {'a': 1, 'b': 2, 'c': 3}
        del d['b']
        items = [10, 20, 30]
        del items[1]
        return d, items

    wal, _ = run_traced(target)
    delitems = events_of_type(wal, 'DELITEM')
    check("dict delitem", any(e.get('key') == 'b' for e in delitems), f"{delitems}")
    check("list delitem", any(e.get('key') == 1 for e in delitems), f"{delitems}")


def test_delete_attr():
    """DELETE_ATTR emits DELATTR."""
    print("\n--- 2d: Attribute delete ---")

    class Obj:
        pass

    def target():
        o = Obj()
        o.x = 10
        o.y = 20
        del o.x
        return o

    wal, _ = run_traced(target)
    delattrs = events_of_type(wal, 'DELATTR')
    check("delattr x", any(e.get('attr') == 'x' for e in delattrs), f"{delattrs}")


# ===================================================================
# 3. Mutating method calls
# ===================================================================

def test_list_mutations():
    """Known mutating methods emit MUTATE with method name and args."""
    print("\n--- 3a: List mutations ---")

    def target():
        items = [1, 2, 3]
        items.append(4)
        items.insert(0, 0)
        items.extend([5, 6])
        items.pop()
        items.remove(0)
        items.clear()
        return items

    wal, _ = run_traced(target)
    mutates = events_of_type(wal, 'MUTATE')

    check("append captured", any(e.get('method') == 'append' for e in mutates), f"{mutates}")
    check("insert captured", any(e.get('method') == 'insert' for e in mutates), f"{mutates}")
    check("extend captured", any(e.get('method') == 'extend' for e in mutates), f"{mutates}")
    check("pop captured", any(e.get('method') == 'pop' for e in mutates), f"{mutates}")
    check("remove captured", any(e.get('method') == 'remove' for e in mutates), f"{mutates}")
    check("clear captured", any(e.get('method') == 'clear' for e in mutates), f"{mutates}")

    # Check args
    appends = [e for e in mutates if e.get('method') == 'append']
    check("append(4) has arg", any(4 in (e.get('args') or []) for e in appends), f"{appends}")


def test_dict_mutations():
    """Dict mutating methods captured."""
    print("\n--- 3b: Dict mutations ---")

    def target():
        d = {'a': 1}
        d.update({'b': 2})
        d.setdefault('c', 3)
        d.pop('a')
        return d

    wal, _ = run_traced(target)
    mutates = events_of_type(wal, 'MUTATE')
    methods = [e.get('method') for e in mutates]
    check("update captured", 'update' in methods, f"{methods}")
    check("setdefault captured", 'setdefault' in methods, f"{methods}")
    check("pop captured", 'pop' in methods, f"{methods}")


def test_set_mutations():
    """Set mutating methods captured."""
    print("\n--- 3c: Set mutations ---")

    def target():
        s = {1, 2, 3}
        s.add(4)
        s.discard(2)
        return s

    wal, _ = run_traced(target)
    mutates = events_of_type(wal, 'MUTATE')
    methods = [e.get('method') for e in mutates]
    check("add captured", 'add' in methods, f"{methods}")
    check("discard captured", 'discard' in methods, f"{methods}")


# ===================================================================
# 4. Control flow — CALL/RETURN, LINE events
# ===================================================================

def test_call_return():
    """Function calls produce CALL/RETURN events."""
    print("\n--- 4a: CALL/RETURN ---")

    def inner(x):
        return x * 2

    def target():
        a = inner(5)
        b = inner(10)
        return a + b

    wal, result = run_traced(target)
    check("result correct", result == 30)
    calls = events_of_type(wal, 'CALL')
    returns = events_of_type(wal, 'RETURN')
    check("CALL events >= 3", len(calls) >= 3, f"got {len(calls)}")  # target + 2x inner
    check("RETURN events >= 3", len(returns) >= 3, f"got {len(returns)}")
    check("return value captured", any(e.get('retval') == 10 for e in returns), f"{returns}")


def test_branch_taken():
    """Mode 1: LINE event at branch destination identifies which branch was taken."""
    print("\n--- 4b: Branch taken (if/else) ---")

    def target_true():
        x = 10
        if x > 5:
            result = 'big'
        else:
            result = 'small'
        return result

    def target_false():
        x = 2
        if x > 5:
            result = 'big'
        else:
            result = 'small'
        return result

    wal_t, _ = run_traced(target_true, line_mode=1)
    wal_f, _ = run_traced(target_false, line_mode=1)

    binds_t = [e for e in events_of_type(wal_t, 'BIND') if e.get('name') == 'result']
    binds_f = [e for e in events_of_type(wal_f, 'BIND') if e.get('name') == 'result']
    check("true branch: result='big'", any(e.get('value') == 'big' for e in binds_t), f"{binds_t}")
    check("false branch: result='small'", any(e.get('value') == 'small' for e in binds_f), f"{binds_f}")


def test_branch_no_stores():
    """Mode 1: branches where neither branch has a store still produce LINE events."""
    print("\n--- 4c: Branch with no stores ---")

    def target():
        x = 10
        if x > 5:
            len([1, 2])  # no store, but true branch
        else:
            len([3])     # no store, false branch
        y = 20
        return y

    wal, _ = run_traced(target, line_mode=1)
    lines = events_of_type(wal, 'LINE')
    line_nums = [e.get('line') for e in lines]
    # We should see a LINE event landing in the true branch body
    # The exact line depends on source layout; check that we have LINE events
    check("LINE events present for branch", len(lines) >= 1, f"lines={line_nums}")


def test_for_loop():
    """Mode 1: FOR_ITER produces LINE events for loop iterations."""
    print("\n--- 4d: For loop ---")

    def target():
        total = 0
        for i in range(5):
            total += i
        return total

    wal, result = run_traced(target, line_mode=1)
    check("result correct", result == 10)
    binds = [e for e in events_of_type(wal, 'BIND') if e.get('name') == 'i']
    check("loop var i captured 5 times", len(binds) == 5,
          f"got {len(binds)}: {[e.get('value') for e in binds]}")


def test_while_loop():
    """Mode 1: while loop condition checks produce LINE events."""
    print("\n--- 4e: While loop ---")

    def target():
        x = 0
        while x < 3:
            x += 1
        return x

    wal, result = run_traced(target, line_mode=1)
    check("result correct", result == 3)
    x_binds = [e for e in events_of_type(wal, 'BIND') if e.get('name') == 'x']
    values = [e.get('value') for e in x_binds]
    check("x incremented through 0,1,2,3", values == [0, 1, 2, 3], f"{values}")


def test_nested_calls():
    """Nested function calls produce correct CALL/RETURN nesting."""
    print("\n--- 4f: Nested calls ---")

    def add(a, b):
        return a + b

    def multiply(a, b):
        result = 0
        for _ in range(b):
            result = add(result, a)
        return result

    def target():
        return multiply(3, 4)

    wal, result = run_traced(target)
    check("result correct", result == 12)
    calls = events_of_type(wal, 'CALL')
    returns = events_of_type(wal, 'RETURN')
    # target + multiply + 4x add = 6 calls
    check("6+ calls", len(calls) >= 6, f"got {len(calls)}")
    check("6+ returns", len(returns) >= 6, f"got {len(returns)}")


# ===================================================================
# 5. Exception tracing
# ===================================================================

def test_explicit_raise():
    """Explicit raise produces RAISE with type, message, and origin line."""
    print("\n--- 5a: Explicit raise ---")

    def target():
        try:
            raise ValueError("bad value")
        except ValueError:
            return "caught"

    wal, result = run_traced(target)
    check("caught", result == "caught")
    raises = events_of_type(wal, 'RAISE')
    check("RAISE event present", len(raises) >= 1, f"{raises}")
    if raises:
        check("exc_type is ValueError", raises[0].get('exc_type') == 'ValueError',
              f"{raises[0]}")
        check("exc_msg is 'bad value'", raises[0].get('exc_msg') == 'bad value',
              f"{raises[0]}")
        check("origin line > 0", raises[0].get('line', -1) > 0, f"{raises[0]}")


def test_c_level_raise():
    """C-level exceptions (KeyError, TypeError, etc.) produce RAISE with origin line."""
    print("\n--- 5b: C-level exception ---")

    def target():
        d = {'a': 1}
        try:
            _ = d['missing']
        except KeyError:
            return "caught"

    wal, result = run_traced(target)
    check("caught", result == "caught")
    raises = events_of_type(wal, 'RAISE')
    check("RAISE for KeyError", any(e.get('exc_type') == 'KeyError' for e in raises), f"{raises}")
    # Origin line should point to the d['missing'] line
    kr = [e for e in raises if e.get('exc_type') == 'KeyError']
    if kr:
        check("origin line captured", kr[0].get('line', -1) > 0, f"{kr[0]}")


def test_except_handler():
    """Entering an except handler produces EXCEPT event."""
    print("\n--- 5c: Exception handler entry ---")

    def target():
        try:
            raise RuntimeError("test")
        except RuntimeError:
            return "handled"

    wal, _ = run_traced(target)
    excepts = events_of_type(wal, 'EXCEPT')
    check("EXCEPT event present", len(excepts) >= 1, f"{excepts}")
    if excepts:
        check("exc_type is RuntimeError", excepts[0].get('exc_type') == 'RuntimeError',
              f"{excepts[0]}")


def test_nested_try_except():
    """Nested try/except correctly tracks exception origin."""
    print("\n--- 5d: Nested try/except ---")

    def target():
        try:
            try:
                d = {}
                _ = d['x']  # KeyError
            except KeyError:
                raise RuntimeError("wrapped")
        except RuntimeError as e:
            return str(e)

    wal, result = run_traced(target)
    check("result correct", result == "wrapped")
    raises = events_of_type(wal, 'RAISE')
    types = [e.get('exc_type') for e in raises]
    check("KeyError raised", 'KeyError' in types, f"{types}")
    check("RuntimeError raised", 'RuntimeError' in types, f"{types}")


def test_exception_in_try_block_origin():
    """We can identify which line in a try block raised the exception."""
    print("\n--- 5e: Exception origin within try block ---")

    def target():
        d = {'a': 1}
        try:
            x = d['a']      # succeeds
            y = d['missing'] # raises
            z = d['a']       # never reached
        except KeyError:
            return x  # x should be 1

    wal, result = run_traced(target)
    check("result correct", result == 1)
    raises = [e for e in events_of_type(wal, 'RAISE') if e.get('exc_type') == 'KeyError']
    if raises:
        # The origin line should be the line with d['missing'], not d['a']
        origin = raises[0].get('line', -1)
        x_binds = [e for e in events_of_type(wal, 'BIND') if e.get('name') == 'x']
        if x_binds:
            x_line = x_binds[0].get('line', -1)
            check("origin line after x assignment", origin > x_line,
                  f"origin={origin}, x_line={x_line}")


# ===================================================================
# 6. Object identity / aliasing
# ===================================================================

def test_aliasing():
    """Two variables pointing to same object share the same oid."""
    print("\n--- 6a: Aliasing ---")

    def target():
        a = [1, 2, 3]
        b = a
        return a, b

    wal, _ = run_traced(target)
    binds = events_of_type(wal, 'BIND')
    a_binds = [e for e in binds if e.get('name') == 'a' and e.get('oid', 0) > 0]
    b_binds = [e for e in binds if e.get('name') == 'b' and e.get('oid', 0) > 0]
    if a_binds and b_binds:
        check("same oid", a_binds[-1]['oid'] == b_binds[-1]['oid'],
              f"a={a_binds[-1]['oid']}, b={b_binds[-1]['oid']}")
    else:
        check("binds found", False, f"a={a_binds}, b={b_binds}")


def test_oid_across_functions():
    """Object oid is preserved when passed to another function."""
    print("\n--- 6b: OID across function calls ---")

    def modify(items):
        items.append(4)

    def target():
        items = [1, 2, 3]
        modify(items)
        return items

    wal, result = run_traced(target)
    check("result correct", result == [1, 2, 3, 4])
    # items in target and items (as parameter) in modify should share oid
    binds = events_of_type(wal, 'BIND')
    items_bind = [e for e in binds if e.get('name') == 'items' and e.get('oid', 0) > 0]
    if len(items_bind) >= 2:
        check("same oid in caller and callee",
              items_bind[0]['oid'] == items_bind[1]['oid'],
              f"caller={items_bind[0]['oid']}, callee={items_bind[1]['oid']}")
    else:
        check("items bound in both scopes", False, f"{items_bind}")


# ===================================================================
# 7. Generators / async
# ===================================================================

def test_generator_basic():
    """Generator yields produce RETURN events, resumes produce CALL."""
    print("\n--- 7a: Basic generator ---")

    def gen():
        yield 1
        yield 2
        yield 3

    def target():
        return list(gen())

    wal, result = run_traced(target)
    check("result correct", result == [1, 2, 3])
    returns = events_of_type(wal, 'RETURN')
    # Each yield produces a RETURN
    check("RETURN events for yields", len(returns) >= 3, f"got {len(returns)}")


def test_generator_pipeline():
    """Generator pipeline with multiple stages."""
    print("\n--- 7b: Generator pipeline ---")

    def producer(n):
        for i in range(n):
            yield i

    def doubler(source):
        for item in source:
            yield item * 2

    def target():
        return list(doubler(producer(3)))

    wal, result = run_traced(target)
    check("result correct", result == [0, 2, 4])
    calls = events_of_type(wal, 'CALL')
    check("multiple CALL events", len(calls) >= 3, f"got {len(calls)}")


def test_generator_send():
    """Generator with send() captures sent values."""
    print("\n--- 7c: Generator send ---")

    def accumulator():
        total = 0
        while True:
            value = yield total
            if value is None:
                break
            total += value

    def target():
        g = accumulator()
        next(g)       # prime
        g.send(10)
        g.send(20)
        result = g.send(30)
        return result

    wal, result = run_traced(target)
    check("result correct", result == 60)


# ===================================================================
# 8. Global / nonlocal / closure variables
# ===================================================================

def test_global_variable():
    """STORE_GLOBAL emits SETATTR on the globals dict."""
    print("\n--- 8a: Global variable ---")

    _test_global_counter = 0

    def target():
        global _test_global_counter
        _test_global_counter = 0
        _test_global_counter += 1
        _test_global_counter += 1
        return _test_global_counter

    wal, result = run_traced(target)
    check("result correct", result == 2)
    setattrs = events_of_type(wal, 'SETATTR')
    counter_sets = [e for e in setattrs if e.get('attr') == '_test_global_counter']
    check("global writes captured", len(counter_sets) >= 2, f"got {len(counter_sets)}")


def test_nonlocal_variable():
    """STORE_DEREF emits BIND for nonlocal variable mutations."""
    print("\n--- 8b: Nonlocal variable ---")

    def target():
        count = 0

        def increment():
            nonlocal count
            count += 1

        increment()
        increment()
        return count

    wal, result = run_traced(target)
    check("result correct", result == 2)
    binds = [e for e in events_of_type(wal, 'BIND') if e.get('name') == 'count']
    check("count BIND events", len(binds) >= 3, f"got {len(binds)}: {binds}")


def test_closure_returned():
    """Closure returned from a function correctly tracks nonlocal state."""
    print("\n--- 8c: Returned closure ---")

    def make_callback():
        state = {'count': 0}

        def callback(event):
            nonlocal state
            state = {'count': state['count'] + 1, 'last': event}

        return callback

    def target():
        cb = make_callback()
        cb('click')
        cb('hover')
        return True

    wal, _ = run_traced(target)
    # The nonlocal state reassignment should produce BIND events
    state_binds = [e for e in events_of_type(wal, 'BIND') if e.get('name') == 'state']
    check("state bound in make_callback", len(state_binds) >= 1, f"{state_binds}")
    # The callback calls should produce additional binds for the nonlocal
    check("state rebound in callbacks", len(state_binds) >= 3,
          f"got {len(state_binds)} (need >=3: init + 2 calls)")


def test_closure_shared_cell():
    """Multiple closures sharing the same cell variable."""
    print("\n--- 8d: Shared closure cell ---")

    def target():
        x = 0

        def getter():
            return x

        def setter(val):
            nonlocal x
            x = val

        setter(10)
        a = getter()
        setter(20)
        b = getter()
        return a, b

    wal, result = run_traced(target)
    check("result correct", result == (10, 20))


# ===================================================================
# 9. Post-mutation snapshots
# ===================================================================

def _snapshots_after_mutate(wal):
    """Get SNAPSHOT events that follow a MUTATE event (post-mutation snapshots).
    Excludes initial creation snapshots."""
    result = []
    prev_was_mutate = False
    for e in wal:
        if e['event'] == 'MUTATE':
            prev_was_mutate = True
        elif e['event'] == 'SNAPSHOT' and prev_was_mutate:
            result.append(e)
            prev_was_mutate = False
        else:
            prev_was_mutate = False
    return result


def test_sort_snapshot():
    """list.sort() produces MUTATE + SNAPSHOT with sorted contents."""
    print("\n--- 9a: list.sort() snapshot ---")

    def target():
        items = [5, 2, 8, 1, 4]
        items.sort()
        return items

    wal, result = run_traced(target)
    check("result correct", result == [1, 2, 4, 5, 8])
    snapshots = _snapshots_after_mutate(wal)
    check("post-mutation SNAPSHOT emitted", len(snapshots) >= 1, f"{snapshots}")
    if snapshots:
        check("snapshot has sorted contents",
              snapshots[0].get('items') == [1, 2, 4, 5, 8],
              f"{snapshots[0].get('items')}")


def test_reverse_snapshot():
    """list.reverse() produces MUTATE + SNAPSHOT with reversed contents."""
    print("\n--- 9b: list.reverse() snapshot ---")

    def target():
        items = [1, 2, 3]
        items.reverse()
        return items

    wal, result = run_traced(target)
    check("result correct", result == [3, 2, 1])
    snapshots = _snapshots_after_mutate(wal)
    check("post-mutation SNAPSHOT emitted", len(snapshots) >= 1, f"{snapshots}")
    if snapshots:
        check("snapshot has reversed contents",
              snapshots[0].get('items') == [3, 2, 1],
              f"{snapshots[0].get('items')}")


def test_sort_then_reverse():
    """sort() followed by reverse() produces two post-mutation snapshots."""
    print("\n--- 9c: sort + reverse ---")

    def target():
        items = [3, 1, 4, 1, 5]
        items.sort()
        items.reverse()
        return items

    wal, result = run_traced(target)
    check("result correct", result == [5, 4, 3, 1, 1])
    snapshots = _snapshots_after_mutate(wal)
    check("two post-mutation SNAPSHOTs", len(snapshots) == 2, f"got {len(snapshots)}")
    if len(snapshots) == 2:
        check("first snapshot sorted", snapshots[0].get('items') == [1, 1, 3, 4, 5],
              f"{snapshots[0].get('items')}")
        check("second snapshot reversed", snapshots[1].get('items') == [5, 4, 3, 1, 1],
              f"{snapshots[1].get('items')}")


def test_set_pop_snapshot():
    """set.pop() produces SNAPSHOT since removal order is implementation-defined."""
    print("\n--- 9d: set.pop() snapshot ---")

    def target():
        s = {10, 20, 30}
        s.pop()
        return s

    wal, result = run_traced(target)
    check("result has 2 elements", len(result) == 2)
    snapshots = _snapshots_after_mutate(wal)
    check("post-mutation SNAPSHOT for set.pop", len(snapshots) >= 1, f"{snapshots}")
    if snapshots:
        check("snapshot has 2 items", len(snapshots[0].get('items', [])) == 2,
              f"{snapshots[0].get('items')}")


def test_deque_reverse_snapshot():
    """deque.reverse() produces SNAPSHOT."""
    print("\n--- 9e: deque.reverse() snapshot ---")

    import collections

    def target():
        d = collections.deque([1, 2, 3])
        d.reverse()
        return d

    wal, result = run_traced(target)
    check("result correct", list(result) == [3, 2, 1])
    snapshots = _snapshots_after_mutate(wal)
    check("post-mutation SNAPSHOT for deque.reverse", len(snapshots) >= 1, f"{snapshots}")
    if snapshots:
        check("snapshot has reversed contents",
              snapshots[0].get('items') == [3, 2, 1],
              f"{snapshots[0].get('items')}")


def test_deque_rotate_snapshot():
    """deque.rotate() produces SNAPSHOT."""
    print("\n--- 9f: deque.rotate() snapshot ---")

    import collections

    def target():
        d = collections.deque([1, 2, 3, 4])
        d.rotate(2)
        return d

    wal, result = run_traced(target)
    check("result correct", list(result) == [3, 4, 1, 2])
    snapshots = _snapshots_after_mutate(wal)
    check("post-mutation SNAPSHOT for deque.rotate", len(snapshots) >= 1, f"{snapshots}")
    if snapshots:
        check("snapshot has rotated contents",
              snapshots[0].get('items') == [3, 4, 1, 2],
              f"{snapshots[0].get('items')}")


def test_list_pop_no_snapshot():
    """list.pop() should NOT produce a post-mutation snapshot (reconstructable)."""
    print("\n--- 9g: list.pop() no snapshot ---")

    def target():
        items = [10, 20, 30]
        items.pop()
        items.pop(0)
        return items

    wal, result = run_traced(target)
    check("result correct", result == [20])
    post_mut_snapshots = _snapshots_after_mutate(wal)
    mutates = [e for e in events_of_type(wal, 'MUTATE') if e.get('method') == 'pop']
    check("pop MUTATE events captured", len(mutates) >= 2, f"got {len(mutates)}")
    check("NO post-mutation snapshot for list.pop", len(post_mut_snapshots) == 0,
          f"got {len(post_mut_snapshots)}: {post_mut_snapshots}")


# ===================================================================
# 10. Complex real-world patterns
# ===================================================================

def test_class_hierarchy():
    """Inheritance with super() calls and @property."""
    print("\n--- 10a: Class hierarchy ---")

    class Base:
        def __init__(self, value):
            self.value = value

        @property
        def doubled(self):
            return self.value * 2

    class Child(Base):
        def __init__(self, value, extra):
            super().__init__(value)
            self.extra = extra

    def target():
        c = Child(10, 'bonus')
        d = c.doubled
        return d, c.extra

    wal, result = run_traced(target)
    check("result correct", result == (20, 'bonus'))
    setattrs = events_of_type(wal, 'SETATTR')
    check("value set", any(e.get('attr') == 'value' for e in setattrs), f"{setattrs}")
    check("extra set", any(e.get('attr') == 'extra' for e in setattrs), f"{setattrs}")


def test_context_manager():
    """with statement triggers __enter__/__exit__ calls."""
    print("\n--- 10b: Context manager ---")

    class Counter:
        def __init__(self):
            self.count = 0

        def __enter__(self):
            self.count += 1
            return self

        def __exit__(self, *args):
            return False

    def target():
        c = Counter()
        with c:
            x = 1
        with c:
            y = 2
        return c.count

    wal, result = run_traced(target)
    check("result correct", result == 2)
    setattrs = [e for e in events_of_type(wal, 'SETATTR') if e.get('attr') == 'count']
    check("count set multiple times", len(setattrs) >= 3, f"got {len(setattrs)}")


def test_decorator():
    """Decorator wrapping preserves tracing of inner function."""
    print("\n--- 10c: Decorator ---")

    def double_result(fn):
        def wrapper(*args, **kwargs):
            result = fn(*args, **kwargs)
            return result * 2
        return wrapper

    @double_result
    def compute(x):
        return x + 1

    def target():
        return compute(5)

    wal, result = run_traced(target)
    check("result correct", result == 12)
    calls = events_of_type(wal, 'CALL')
    check("wrapper and inner both called", len(calls) >= 3, f"got {len(calls)}")


def test_comprehension():
    """List/dict/set comprehensions are traced (they compile to nested functions)."""
    print("\n--- 10d: Comprehensions ---")

    def target():
        squares = [x * x for x in range(5)]
        evens = {x for x in squares if x % 2 == 0}
        index = {x: x * x for x in range(3)}
        return squares, evens, index

    wal, result = run_traced(target)
    check("result correct", result == ([0, 1, 4, 9, 16], {0, 4, 16}, {0: 0, 1: 1, 2: 4}))


def test_recursion():
    """Recursive function calls are correctly nested."""
    print("\n--- 10e: Recursion ---")

    def fib(n):
        if n < 2:
            return n
        return fib(n - 1) + fib(n - 2)

    def target():
        return fib(6)

    wal, result = run_traced(target)
    check("result correct", result == 8)
    calls = events_of_type(wal, 'CALL')
    returns = events_of_type(wal, 'RETURN')
    check("many recursive calls", len(calls) >= 20, f"got {len(calls)}")
    check("calls == returns", len(calls) == len(returns),
          f"calls={len(calls)}, returns={len(returns)}")


def test_mixed_exception_and_mutation():
    """Exception handling interleaved with mutations."""
    print("\n--- 10f: Exception + mutation ---")

    def target():
        items = []
        for i in range(5):
            try:
                if i == 3:
                    raise ValueError(f"bad: {i}")
                items.append(i)
            except ValueError:
                items.append(-1)
        return items

    wal, result = run_traced(target)
    check("result correct", result == [0, 1, 2, -1, 4])
    raises = events_of_type(wal, 'RAISE')
    mutates = events_of_type(wal, 'MUTATE')
    check("exception raised", len(raises) >= 1, f"{raises}")
    check("appends captured", len(mutates) >= 5, f"got {len(mutates)}")


# ===================================================================
# 11. LINE mode comparison
# ===================================================================

def test_mode0_no_lines():
    """Mode 0: no LINE events emitted."""
    print("\n--- 11a: Mode 0 (stores only) ---")

    def target():
        x = 1
        y = 2
        return x + y

    wal, _ = run_traced(target, line_mode=0)
    lines = events_of_type(wal, 'LINE')
    check("no LINE events in mode 0", len(lines) == 0, f"got {len(lines)}")
    binds = events_of_type(wal, 'BIND')
    check("BIND events still present", len(binds) >= 2)


def test_mode2_all_lines():
    """Mode 2: every source line change produces a LINE event."""
    print("\n--- 11b: Mode 2 (full LINE) ---")

    def target():
        x = 1        # line A
        y = 2        # line B
        len([1, 2])  # line C - no store
        z = 3        # line D
        return z

    wal, _ = run_traced(target, line_mode=2)
    lines = events_of_type(wal, 'LINE')
    check("LINE events for all lines including no-store", len(lines) >= 4,
          f"got {len(lines)}")


def test_mode1_control_flow_only():
    """Mode 1: LINE events at control flow points, inferrable elsewhere."""
    print("\n--- 11c: Mode 1 (control flow) ---")

    def target():
        x = 1
        y = 2
        if x > 0:
            z = 3
        else:
            z = 4
        for i in range(2):
            pass
        return z

    wal, _ = run_traced(target, line_mode=1)
    lines = events_of_type(wal, 'LINE')
    check("LINE events present for branches/loops", len(lines) >= 1, f"got {len(lines)}")
    # Mode 1 should have fewer LINE events than mode 2
    wal2, _ = run_traced(target, line_mode=2)
    lines2 = events_of_type(wal2, 'LINE')
    check("mode 1 has fewer LINEs than mode 2", len(lines) < len(lines2),
          f"mode1={len(lines)}, mode2={len(lines2)}")


# ===================================================================
# Main
# ===================================================================

def main():
    print("WAL Tracing Conformance Test Suite")
    print(f"Python: {sys.version}")
    print(f"Mode: {'verbose' if verbose else 'summary'}")

    # 1. Variable capture
    test_primitive_binds()
    test_mutable_object_binds()
    test_variable_reassignment()
    test_function_arguments()
    test_tuple_unpacking()

    # 2. Object mutations
    test_store_attr()
    test_store_subscr()
    test_delete_subscr()
    test_delete_attr()

    # 3. Mutating method calls
    test_list_mutations()
    test_dict_mutations()
    test_set_mutations()

    # 4. Control flow
    test_call_return()
    test_branch_taken()
    test_branch_no_stores()
    test_for_loop()
    test_while_loop()
    test_nested_calls()

    # 5. Exceptions
    test_explicit_raise()
    test_c_level_raise()
    test_except_handler()
    test_nested_try_except()
    test_exception_in_try_block_origin()

    # 6. Object identity
    test_aliasing()
    test_oid_across_functions()

    # 7. Generators
    test_generator_basic()
    test_generator_pipeline()
    test_generator_send()

    # 8. Global / nonlocal / closure
    test_global_variable()
    test_nonlocal_variable()
    test_closure_returned()
    test_closure_shared_cell()

    # 9. Snapshots
    test_sort_snapshot()
    test_reverse_snapshot()
    test_sort_then_reverse()
    test_set_pop_snapshot()
    test_deque_reverse_snapshot()
    test_deque_rotate_snapshot()
    test_list_pop_no_snapshot()

    # 10. Complex patterns
    test_class_hierarchy()
    test_context_manager()
    test_decorator()
    test_comprehension()
    test_recursion()
    test_mixed_exception_and_mutation()

    # 11. LINE modes
    test_mode0_no_lines()
    test_mode2_all_lines()
    test_mode1_control_flow_only()

    # Summary
    print(f"\n{'='*60}")
    print(f"Results: {passed} passed, {failed} failed")
    print(f"{'='*60}")
    if errors:
        print("\nFailures:")
        for e in errors:
            print(f"  {e}")
        sys.exit(1)
    else:
        print("\nAll tests passed.")
        sys.exit(0)


if __name__ == '__main__':
    main()
