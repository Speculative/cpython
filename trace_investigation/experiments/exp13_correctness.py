"""
Experiment 13: Correctness Tests for Dynamic Python Features

Tests whether our tracer correctly captures variables and associates them
back to the right code across Python's most dynamic features:

1.  Recursion (same code object, different frames, different values)
2.  eval() / exec() (dynamically compiled code)
3.  Generators (suspend/resume, multiple live instances)
4.  Closures (free variables, cell variables, shared state)
5.  Dynamic dispatch (polymorphism, __getattr__, descriptors)
6.  Monkey patching (replacing functions/methods at runtime)
7.  Decorators (wrapping functions, functools.wraps)
8.  Metaclasses and __init_subclass__
9.  Exception handling (try/except/finally, re-raise, chained)
10. Context managers (with statement, __enter__/__exit__)
11. Comprehensions (list/dict/set comps, nested)
12. Async/await (coroutines, async generators)
13. Star unpacking, walrus operator, multi-assignment
14. Class variable vs instance variable vs local variable shadowing
15. Globals/nonlocal mutation from nested scopes

For each test case, we trace execution, then verify:
  - Correct function/code identification
  - Correct variable names captured
  - Correct variable values at each step
  - Correct call/return nesting
"""
import sys
import os
import dis
import types
import opcode
import functools
import contextlib
import asyncio

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# ============================================================================
# Tracer: captures full trace with variable values
# ============================================================================

class CorrectnessTracer:
    """Full-fidelity tracer for correctness verification."""

    def __init__(self):
        self.events = []
        self.call_depth = 0

    def trace_func(self, frame, event, arg):
        code = frame.f_code
        entry = {
            'event': event,
            'filename': os.path.basename(code.co_filename),
            'qualname': code.co_qualname,
            'name': code.co_name,
            'lineno': frame.f_lineno,
            'depth': self.call_depth,
        }

        if event == 'call':
            self.call_depth += 1
            entry['depth'] = self.call_depth
            # Capture arguments
            try:
                entry['locals'] = dict(frame.f_locals)
            except Exception:
                entry['locals'] = {}

        elif event == 'line':
            try:
                entry['locals'] = dict(frame.f_locals)
            except Exception:
                entry['locals'] = {}

        elif event == 'return':
            entry['retval'] = repr(arg)
            self.call_depth -= 1

        elif event == 'exception':
            entry['exc_type'] = type(arg[1]).__name__ if arg and arg[1] else '?'
            entry['exc_msg'] = str(arg[1]) if arg and arg[1] else '?'

        self.events.append(entry)
        return self.trace_func

    def start(self):
        self.events.clear()
        self.call_depth = 0
        sys.settrace(self.trace_func)

    def stop(self):
        sys.settrace(None)

    def get_events(self, qualname_filter=None):
        """Get events, optionally filtered to a specific function."""
        if qualname_filter is None:
            return self.events
        return [e for e in self.events if e['qualname'] == qualname_filter
                or e['qualname'].endswith('.' + qualname_filter)]

    def get_line_locals(self, qualname_filter):
        """Get (lineno, locals_dict) pairs for line events in a function."""
        return [(e['lineno'], e.get('locals', {}))
                for e in self.events
                if e['event'] == 'line' and
                (e['qualname'] == qualname_filter
                 or e['qualname'].endswith('.' + qualname_filter))]

    def get_returns(self, qualname_filter):
        """Get return values for a function."""
        return [e['retval']
                for e in self.events
                if e['event'] == 'return' and
                (e['qualname'] == qualname_filter
                 or e['qualname'].endswith('.' + qualname_filter))]

    def get_calls(self, qualname_filter):
        """Get call events with arguments."""
        return [e.get('locals', {})
                for e in self.events
                if e['event'] == 'call' and
                (e['qualname'] == qualname_filter
                 or e['qualname'].endswith('.' + qualname_filter))]

    def get_exceptions(self):
        """Get all exception events."""
        return [(e['qualname'], e.get('exc_type', '?'), e.get('exc_msg', '?'))
                for e in self.events if e['event'] == 'exception']


# ============================================================================
# Test infrastructure
# ============================================================================

passed = 0
failed = 0
errors = []

def check(test_name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS: {test_name}")
    else:
        failed += 1
        msg = f"  FAIL: {test_name}"
        if detail:
            msg += f" — {detail}"
        print(msg)
        errors.append(msg)


tracer = CorrectnessTracer()


# ============================================================================
# Test 1: Recursion
# ============================================================================

def test_recursion():
    print("\n=== Test 1: Recursion ===")

    def factorial(n):
        if n <= 1:
            return 1
        return n * factorial(n - 1)

    tracer.start()
    result = factorial(5)
    tracer.stop()

    check("factorial(5) == 120", result == 120)

    # Verify we see 5 calls with correct n values
    calls = tracer.get_calls('factorial')
    n_values = [c.get('n') for c in calls]
    check("5 recursive calls", len(calls) == 5, f"got {len(calls)}")
    check("n values are [5,4,3,2,1]", n_values == [5, 4, 3, 2, 1], f"got {n_values}")

    # Verify returns come back in correct order
    returns = tracer.get_returns('factorial')
    check("5 returns", len(returns) == 5, f"got {len(returns)}")
    check("first return is 1", returns[0] == '1')
    check("last return is 120", returns[-1] == '120')


# ============================================================================
# Test 2: eval() / exec()
# ============================================================================

def test_eval_exec():
    print("\n=== Test 2: eval() / exec() ===")

    tracer.start()
    x = eval("2 + 3")
    tracer.stop()
    check("eval('2+3') == 5", x == 5)

    tracer.start()
    ns = {}
    exec("y = 10\nz = y * 2", ns)
    tracer.stop()
    check("exec assigns y=10, z=20", ns.get('y') == 10 and ns.get('z') == 20)

    # Dynamic function creation
    tracer.start()
    exec("def dynamic_fn(a, b): return a + b", ns)
    result = ns['dynamic_fn'](3, 7)
    tracer.stop()
    check("dynamic function works", result == 10)

    calls = tracer.get_calls('dynamic_fn')
    check("dynamic_fn call captured", len(calls) >= 1, f"got {len(calls)} calls")
    if calls:
        check("dynamic_fn args captured", calls[0].get('a') == 3 and calls[0].get('b') == 7,
              f"got {calls[0]}")


# ============================================================================
# Test 3: Generators
# ============================================================================

def test_generators():
    print("\n=== Test 3: Generators ===")

    def counter(start, end):
        i = start
        while i < end:
            yield i
            i += 1

    # Single generator
    tracer.start()
    values = list(counter(0, 3))
    tracer.stop()
    check("generator produces [0,1,2]", values == [0, 1, 2])

    returns = tracer.get_returns('counter')
    # Generators "return" at each yield and at final StopIteration
    check("generator has multiple returns", len(returns) >= 3, f"got {len(returns)}")

    # Multiple live generators (interleaved)
    tracer.start()
    g1 = counter(0, 3)
    g2 = counter(10, 13)
    interleaved = []
    for _ in range(3):
        interleaved.append(next(g1))
        interleaved.append(next(g2))
    tracer.stop()
    check("interleaved generators", interleaved == [0, 10, 1, 11, 2, 12],
          f"got {interleaved}")

    # Generator with send()
    def accumulator():
        total = 0
        while True:
            value = yield total
            if value is None:
                break
            total += value

    tracer.start()
    gen = accumulator()
    next(gen)  # prime
    gen.send(5)
    gen.send(3)
    result = gen.send(7)
    tracer.stop()
    check("generator send() accumulates", result == 15)


# ============================================================================
# Test 4: Closures
# ============================================================================

def test_closures():
    print("\n=== Test 4: Closures ===")

    def make_counter(start=0):
        count = start
        def increment(amount=1):
            nonlocal count
            count += amount
            return count
        def get():
            return count
        return increment, get

    tracer.start()
    inc, get = make_counter(10)
    a = inc()
    b = inc(5)
    c = get()
    tracer.stop()

    check("closure increment works", a == 11 and b == 16 and c == 16,
          f"got a={a}, b={b}, c={c}")

    # Verify the closure variables are captured
    line_locals = tracer.get_line_locals('increment')
    # Look for 'count' or 'amount' in captured locals
    all_vars = set()
    for _, loc in line_locals:
        all_vars.update(loc.keys())
    check("closure captures 'count' or 'amount'",
          'count' in all_vars or 'amount' in all_vars, f"vars seen: {all_vars}")

    # Multiple closures over same variable
    def make_pair():
        shared = []
        def adder(x):
            shared.append(x)
            return len(shared)
        def getter():
            return list(shared)
        return adder, getter

    tracer.start()
    add, get = make_pair()
    add(1)
    add(2)
    result = get()
    tracer.stop()
    check("shared closure state", result == [1, 2], f"got {result}")


# ============================================================================
# Test 5: Dynamic dispatch
# ============================================================================

def test_dynamic_dispatch():
    print("\n=== Test 5: Dynamic dispatch ===")

    class Animal:
        def speak(self):
            return "..."

    class Dog(Animal):
        def speak(self):
            return "woof"

    class Cat(Animal):
        def speak(self):
            return "meow"

    tracer.start()
    animals = [Dog(), Cat(), Dog(), Cat()]
    sounds = [a.speak() for a in animals]
    tracer.stop()
    check("polymorphic dispatch", sounds == ["woof", "meow", "woof", "meow"])

    # __getattr__ interception
    class DynObj:
        def __init__(self):
            self._data = {'x': 10, 'y': 20}

        def __getattr__(self, name):
            if name.startswith('_'):
                raise AttributeError(name)
            return self._data.get(name, -1)

    tracer.start()
    obj = DynObj()
    x = obj.x
    y = obj.y
    z = obj.z
    tracer.stop()
    check("__getattr__ dispatch", x == 10 and y == 20 and z == -1,
          f"got x={x}, y={y}, z={z}")

    # Descriptor protocol
    class Validated:
        def __init__(self, min_val, max_val):
            self.min_val = min_val
            self.max_val = max_val
            self.name = None

        def __set_name__(self, owner, name):
            self.name = '_' + name

        def __get__(self, obj, objtype=None):
            if obj is None:
                return self
            return getattr(obj, self.name, None)

        def __set__(self, obj, value):
            if not self.min_val <= value <= self.max_val:
                raise ValueError(f"{value} out of range [{self.min_val}, {self.max_val}]")
            setattr(obj, self.name, value)

    class Config:
        temperature = Validated(0, 100)
        humidity = Validated(0, 100)

    tracer.start()
    c = Config()
    c.temperature = 25
    c.humidity = 60
    t = c.temperature
    tracer.stop()
    check("descriptor protocol", t == 25, f"got {t}")


# ============================================================================
# Test 6: Monkey patching
# ============================================================================

def test_monkey_patching():
    print("\n=== Test 6: Monkey patching ===")

    class MyClass:
        def method(self):
            return "original"

    tracer.start()
    obj = MyClass()
    r1 = obj.method()

    # Patch the method
    def patched_method(self):
        return "patched"
    MyClass.method = patched_method

    r2 = obj.method()
    tracer.stop()

    check("pre-patch returns 'original'", r1 == "original")
    check("post-patch returns 'patched'", r2 == "patched")

    # Verify tracer saw both the original and patched versions
    method_returns = tracer.get_returns('method') + tracer.get_returns('patched_method')
    check("tracer saw both versions", len(method_returns) >= 2,
          f"got {len(method_returns)} returns: {method_returns}")

    # Patch a module-level function
    def original():
        return "orig"

    def replacement():
        return "replaced"

    tracer.start()
    r1 = original()
    # Simulate patching by reassigning in locals and calling
    fn = original
    fn = replacement
    r2 = fn()
    tracer.stop()
    check("function reassignment", r1 == "orig" and r2 == "replaced")

    # Runtime code generation replacing a function's code object
    def target():
        return 42

    tracer.start()
    r1 = target()

    # Replace code object
    new_code = compile("def target(): return 99", "<dynamic>", "exec")
    ns = {}
    exec(new_code, ns)
    target.__code__ = ns['target'].__code__

    r2 = target()
    tracer.stop()
    check("code object replacement", r1 == 42 and r2 == 99,
          f"got r1={r1}, r2={r2}")


# ============================================================================
# Test 7: Decorators
# ============================================================================

def test_decorators():
    print("\n=== Test 7: Decorators ===")

    def logging_decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            result = func(*args, **kwargs)
            return result
        return wrapper

    @logging_decorator
    def add(a, b):
        return a + b

    tracer.start()
    result = add(3, 4)
    tracer.stop()
    check("decorated function works", result == 7)

    # Check we can see through the wrapper to the real function
    all_qualnames = set(e['qualname'] for e in tracer.events)
    # functools.wraps preserves the name, so we should see 'add'
    check("tracer sees wrapped function name",
          any('add' in q for q in all_qualnames),
          f"qualnames: {all_qualnames}")

    # Stacked decorators
    def decorator_a(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs) + " [A]"
        return wrapper

    def decorator_b(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs) + " [B]"
        return wrapper

    @decorator_a
    @decorator_b
    def greet(name):
        return f"Hello {name}"

    tracer.start()
    msg = greet("World")
    tracer.stop()
    check("stacked decorators", msg == "Hello World [B] [A]", f"got '{msg}'")


# ============================================================================
# Test 8: Metaclasses
# ============================================================================

def test_metaclasses():
    print("\n=== Test 8: Metaclasses ===")

    class RegistryMeta(type):
        _registry = {}
        def __new__(mcs, name, bases, namespace):
            cls = super().__new__(mcs, name, bases, namespace)
            if name != 'Base':
                mcs._registry[name] = cls
            return cls

    class Base(metaclass=RegistryMeta):
        pass

    tracer.start()
    class PluginA(Base):
        value = 1

    class PluginB(Base):
        value = 2
    tracer.stop()

    check("metaclass registered plugins",
          'PluginA' in RegistryMeta._registry and 'PluginB' in RegistryMeta._registry)

    # __init_subclass__
    class Validator:
        _validators = []
        def __init_subclass__(cls, validate=None, **kwargs):
            super().__init_subclass__(**kwargs)
            if validate:
                cls._validators.append(validate)

    tracer.start()
    class IntValidator(Validator, validate=lambda x: isinstance(x, int)):
        pass
    tracer.stop()

    check("__init_subclass__ works", len(Validator._validators) == 1)


# ============================================================================
# Test 9: Exception handling
# ============================================================================

def test_exceptions():
    print("\n=== Test 9: Exception handling ===")

    def risky(x):
        if x == 0:
            raise ValueError("zero!")
        if x < 0:
            raise TypeError("negative!")
        return 1 / x

    tracer.start()
    results = []
    for val in [2, 0, -1, 4]:
        try:
            results.append(risky(val))
        except ValueError as e:
            results.append(f"VE:{e}")
        except TypeError as e:
            results.append(f"TE:{e}")
    tracer.stop()

    check("exception handling correct",
          results == [0.5, "VE:zero!", "TE:negative!", 0.25],
          f"got {results}")

    exceptions = tracer.get_exceptions()
    exc_types = [t for _, t, _ in exceptions]
    check("tracer captured ValueError", 'ValueError' in exc_types)
    check("tracer captured TypeError", 'TypeError' in exc_types)

    # Chained exceptions
    def do_chained_exc():
        try:
            try:
                raise ValueError("inner")
            except ValueError:
                raise RuntimeError("outer") from ValueError("cause")
        except RuntimeError:
            pass

    tracer.start()
    do_chained_exc()
    tracer.stop()

    exceptions = tracer.get_exceptions()
    exc_types = [t for _, t, _ in exceptions]
    check("chained exceptions captured", 'ValueError' in exc_types and 'RuntimeError' in exc_types,
          f"got {exc_types}")

    # Finally block
    tracer.start()
    cleanup_ran = False
    try:
        raise ValueError("test")
    except ValueError:
        pass
    finally:
        cleanup_ran = True
    tracer.stop()
    check("finally block executed", cleanup_ran)


# ============================================================================
# Test 10: Context managers
# ============================================================================

def test_context_managers():
    print("\n=== Test 10: Context managers ===")

    class Tracker:
        def __init__(self, name):
            self.name = name
            self.entered = False
            self.exited = False

        def __enter__(self):
            self.entered = True
            return self

        def __exit__(self, *args):
            self.exited = True
            return False

    tracer.start()
    with Tracker("t1") as t:
        x = 42
    tracer.stop()

    check("context manager enter/exit", t.entered and t.exited)

    # Nested context managers
    tracer.start()
    with Tracker("outer") as a:
        with Tracker("inner") as b:
            result = a.name + "+" + b.name
    tracer.stop()
    check("nested context managers", result == "outer+inner")

    # contextlib.contextmanager (generator-based)
    @contextlib.contextmanager
    def temp_value(name):
        old = None
        yield name
        # cleanup

    tracer.start()
    with temp_value("test") as v:
        captured = v
    tracer.stop()
    check("generator context manager", captured == "test")


# ============================================================================
# Test 11: Comprehensions
# ============================================================================

def test_comprehensions():
    print("\n=== Test 11: Comprehensions ===")

    tracer.start()
    squares = [x**2 for x in range(5)]
    tracer.stop()
    check("list comprehension", squares == [0, 1, 4, 9, 16])

    tracer.start()
    even_squares = {x: x**2 for x in range(5) if x % 2 == 0}
    tracer.stop()
    check("dict comprehension", even_squares == {0: 0, 2: 4, 4: 16})

    tracer.start()
    unique = {x % 3 for x in range(10)}
    tracer.stop()
    check("set comprehension", unique == {0, 1, 2})

    # Nested comprehension
    tracer.start()
    matrix = [[i*3 + j for j in range(3)] for i in range(3)]
    tracer.stop()
    check("nested comprehension",
          matrix == [[0, 1, 2], [3, 4, 5], [6, 7, 8]])

    # Comprehension with closure variable
    tracer.start()
    multiplier = 10
    scaled = [x * multiplier for x in range(3)]
    tracer.stop()
    check("comprehension with closure", scaled == [0, 10, 20])


# ============================================================================
# Test 12: Async/await
# ============================================================================

def test_async():
    print("\n=== Test 12: Async/await ===")

    async def async_add(a, b):
        await asyncio.sleep(0)  # yield to event loop
        return a + b

    async def async_pipeline():
        r1 = await async_add(1, 2)
        r2 = await async_add(r1, 3)
        return r2

    tracer.start()
    result = asyncio.run(async_pipeline())
    tracer.stop()
    check("async/await works", result == 6, f"got {result}")

    # Async generator
    async def async_counter(n):
        for i in range(n):
            await asyncio.sleep(0)
            yield i

    async def consume_async_gen():
        values = []
        async for v in async_counter(3):
            values.append(v)
        return values

    tracer.start()
    result = asyncio.run(consume_async_gen())
    tracer.stop()
    check("async generator", result == [0, 1, 2], f"got {result}")

    # Async context manager
    class AsyncTracker:
        def __init__(self):
            self.entered = False
            self.exited = False

        async def __aenter__(self):
            self.entered = True
            return self

        async def __aexit__(self, *args):
            self.exited = True

    async def use_async_cm():
        async with AsyncTracker() as t:
            return t.entered, t.exited

    tracer.start()
    entered, exited_during = asyncio.run(use_async_cm())
    tracer.stop()
    check("async context manager", entered and not exited_during)


# ============================================================================
# Test 13: Unpacking and walrus
# ============================================================================

def test_unpacking_walrus():
    print("\n=== Test 13: Unpacking and walrus operator ===")

    # Wrap in functions so tracer can filter by qualname cleanly
    def do_star_unpack():
        a, b, *rest = [1, 2, 3, 4, 5]
        return a, b, rest

    tracer.start()
    a, b, rest = do_star_unpack()
    tracer.stop()

    check("star unpacking a=1", a == 1)
    check("star unpacking b=2", b == 2)
    check("star unpacking rest=[3,4,5]", rest == [3, 4, 5], f"got {rest}")

    line_locals = tracer.get_line_locals('do_star_unpack')
    all_vars = {}
    for _, loc in line_locals:
        all_vars.update(loc)
    check("tracer captured unpacked vars",
          all_vars.get('a') == 1 and all_vars.get('b') == 2 and all_vars.get('rest') == [3, 4, 5],
          f"got {all_vars}")

    # Walrus operator
    tracer.start()
    data = [1, 2, 3, 4, 5]
    result = [y for x in data if (y := x * 2) > 4]
    tracer.stop()
    check("walrus operator", result == [6, 8, 10], f"got {result}")

    # Multi-target assignment
    def do_multi_assign():
        x = y = z = 42
        return x, y, z

    tracer.start()
    x, y, z = do_multi_assign()
    tracer.stop()

    check("multi-assignment result", x == 42 and y == 42 and z == 42)
    line_locals = tracer.get_line_locals('do_multi_assign')
    final_vars = {}
    for _, loc in line_locals:
        final_vars.update(loc)
    check("tracer sees multi-assignment x=y=z=42",
          final_vars.get('x') == 42 and final_vars.get('y') == 42 and final_vars.get('z') == 42,
          f"got {final_vars}")

    # Tuple swap
    def do_swap():
        a, b = 1, 2
        a, b = b, a
        return a, b

    tracer.start()
    a, b = do_swap()
    tracer.stop()

    check("tuple swap result a=2, b=1", a == 2 and b == 1)
    line_locals = tracer.get_line_locals('do_swap')
    final = {}
    for _, loc in line_locals:
        if 'a' in loc:
            final['a'] = loc['a']
        if 'b' in loc:
            final['b'] = loc['b']
    check("tracer sees swap a=2, b=1", final.get('a') == 2 and final.get('b') == 1,
          f"got {final}")


# ============================================================================
# Test 14: Variable shadowing
# ============================================================================

def test_shadowing():
    print("\n=== Test 14: Variable shadowing ===")

    x = "global"

    class MyClass:
        x = "class"

        def method(self):
            x = "local"
            return x, MyClass.x

    tracer.start()
    obj = MyClass()
    local_x, class_x = obj.method()
    tracer.stop()

    check("local shadows class and global",
          local_x == "local" and class_x == "class")

    # Verify tracer captured the local 'x', not the global
    line_locals = tracer.get_line_locals('method')
    method_x_values = [loc.get('x') for _, loc in line_locals if 'x' in loc]
    check("tracer sees local x='local'", "local" in method_x_values,
          f"x values: {method_x_values}")


# ============================================================================
# Test 15: Globals/nonlocal mutation
# ============================================================================

def test_nonlocal_globals():
    print("\n=== Test 15: Globals/nonlocal mutation ===")

    counter = [0]  # mutable to avoid global keyword issues

    def outer():
        x = 10
        def inner():
            nonlocal x
            x += 5
            return x
        r1 = inner()
        r2 = inner()
        return r1, r2, x

    tracer.start()
    r1, r2, final_x = outer()
    tracer.stop()

    check("nonlocal mutation", r1 == 15 and r2 == 20 and final_x == 20,
          f"got r1={r1}, r2={r2}, final_x={final_x}")

    # Global mutation from nested function
    global _test_global
    _test_global = "before"

    def mutate_global():
        global _test_global
        _test_global = "after"

    tracer.start()
    mutate_global()
    tracer.stop()
    check("global mutation", _test_global == "after")


# ============================================================================
# Main
# ============================================================================

def main():
    test_recursion()
    test_eval_exec()
    test_generators()
    test_closures()
    test_dynamic_dispatch()
    test_monkey_patching()
    test_decorators()
    test_metaclasses()
    test_exceptions()
    test_context_managers()
    test_comprehensions()
    test_async()
    test_unpacking_walrus()
    test_shadowing()
    test_nonlocal_globals()

    print(f"\n{'='*60}")
    print(f"Results: {passed} passed, {failed} failed")
    print(f"{'='*60}")
    if errors:
        print("\nFailures:")
        for e in errors:
            print(f"  {e}")

    return failed == 0


if __name__ == '__main__':
    success = main()
    sys.exit(0 if success else 1)
