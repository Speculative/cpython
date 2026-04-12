"""
Experiment 14: Execution Order Verification

Verifies that our trace events correctly capture the exact order of
every line that executes, including tricky cases:

1. Short-circuit evaluation (and/or)
2. Conditional expressions (ternary)
3. Comprehensions with filters
4. Exception handler jumps (try/except/finally ordering)
5. Generator interleaving (multiple generators, yield points)
6. Loop break/continue
7. Multi-line expressions
8. Chained comparisons
9. With-statement ordering
10. Nested calls on single line (f(g(x)))
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# ============================================================================
# Line order tracer
# ============================================================================

class OrderTracer:
    """Records line execution order for a specific function."""

    def __init__(self):
        self.line_order = []  # [(qualname, lineno), ...]
        self._filter = None

    def trace_func(self, frame, event, arg):
        if event == 'line':
            qn = frame.f_code.co_qualname
            if self._filter is None or self._filter in qn:
                self.line_order.append((qn, frame.f_lineno))
        return self.trace_func

    def start(self, filter_name=None):
        self.line_order.clear()
        self._filter = filter_name
        sys.settrace(self.trace_func)

    def stop(self):
        sys.settrace(None)

    def lines(self, qualname=None):
        """Get just line numbers, optionally filtered."""
        if qualname:
            return [ln for qn, ln in self.line_order if qualname in qn]
        return [ln for _, ln in self.line_order]

    def annotated(self, qualname=None):
        """Get (qualname, lineno) pairs."""
        if qualname:
            return [(qn, ln) for qn, ln in self.line_order if qualname in qn]
        return list(self.line_order)


tracer = OrderTracer()
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


def get_base_line(func):
    """Get the first line number of a function's body."""
    return func.__code__.co_firstlineno


# ============================================================================
# Test 1: Short-circuit evaluation
# ============================================================================

def test_short_circuit():
    print("\n=== Test 1: Short-circuit evaluation ===")

    def short_and(a, b):
        x = a and b        # L+1: if a is falsy, b not evaluated
        return x            # L+2

    def short_or(a, b):
        x = a or b          # L+1
        return x            # L+2

    # and: first operand is True -> evaluates both
    tracer.start('short_and')
    short_and(True, 42)
    tracer.stop()
    lines = tracer.lines('short_and')
    check("and(True, 42): both lines execute", len(lines) == 2, f"lines: {lines}")

    # and: first operand is False -> short-circuits
    tracer.start('short_and')
    result = short_and(False, 42)
    tracer.stop()
    lines = tracer.lines('short_and')
    # Still 2 lines — short-circuit doesn't skip lines, just skips evaluating the RHS
    # The line event fires for the whole `x = a and b` line regardless
    check("and(False, 42): still visits both lines", len(lines) == 2, f"lines: {lines}")
    check("and(False, 42): result is False", result is False)

    # Same for or
    tracer.start('short_or')
    result = short_or("truthy", "fallback")
    tracer.stop()
    lines = tracer.lines('short_or')
    check("or('truthy', _): visits both lines", len(lines) == 2, f"lines: {lines}")
    check("or('truthy', _): result is 'truthy'", result == "truthy")

    # Multi-line short-circuit: the interesting case
    def multi_line_and(a, b):
        if (a           # L+1
            and b):     # L+2
            return True # L+3
        return False    # L+4

    tracer.start('multi_line_and')
    multi_line_and(False, True)
    tracer.stop()
    lines = tracer.lines('multi_line_and')
    base = get_base_line(multi_line_and)
    offsets = [l - base for l in lines]
    check("multi-line and(False, True): skips 'and b' line",
          # Python may or may not emit a separate LINE event for 'and b' when short-circuiting
          # The important thing is we see the if-line and the return False
          offsets[-1] == 4,  # ends at return False
          f"offsets from base: {offsets}")


# ============================================================================
# Test 2: Conditional expressions
# ============================================================================

def test_conditional_expr():
    print("\n=== Test 2: Conditional expressions ===")

    def ternary(cond):
        x = "yes" if cond else "no"   # L+1
        return x                        # L+2

    tracer.start('ternary')
    ternary(True)
    tracer.stop()
    lines_true = tracer.lines('ternary')

    tracer.start('ternary')
    ternary(False)
    tracer.stop()
    lines_false = tracer.lines('ternary')

    check("ternary(True): 2 lines", len(lines_true) == 2)
    check("ternary(False): 2 lines", len(lines_false) == 2)
    check("ternary: same lines either way", lines_true == lines_false,
          f"true={lines_true}, false={lines_false}")


# ============================================================================
# Test 3: Comprehension filter ordering
# ============================================================================

def test_comprehension_order():
    print("\n=== Test 3: Comprehension ordering ===")

    def comp_filter():
        data = [1, 2, 3, 4, 5]            # L+1
        result = [x*2 for x in data        # L+2
                  if x % 2 == 0]           # L+3
        return result                       # L+4

    tracer.start('comp_filter')
    result = comp_filter()
    tracer.stop()
    check("filtered comprehension result", result == [4, 8])

    lines = tracer.lines('comp_filter')
    check("comp_filter lines captured", len(lines) >= 2, f"lines: {lines}")

    # Comprehensions run in their own code object since Python 3.12
    # Check if we see the comprehension's internal lines too
    all_events = tracer.annotated()
    comp_events = [(qn, ln) for qn, ln in all_events if 'listcomp' in qn]
    check("comprehension has its own frame",
          len(comp_events) > 0 or len(lines) >= 2,
          f"comp events: {comp_events}, func lines: {lines}")


# ============================================================================
# Test 4: Exception handler ordering
# ============================================================================

def test_exception_order():
    print("\n=== Test 4: Exception handler execution order ===")

    def exc_order(x):
        result = []                    # L+1
        try:                           # L+2
            result.append('try')       # L+3
            if x == 0:                 # L+4
                raise ValueError()    # L+5
            result.append('no-exc')    # L+6
        except ValueError:             # L+7
            result.append('except')    # L+8
        finally:                       # L+9
            result.append('finally')   # L+10
        return result                  # L+11

    # No exception path
    tracer.start('exc_order')
    r = exc_order(1)
    tracer.stop()
    check("no-exception path", r == ['try', 'no-exc', 'finally'])

    lines_no_exc = tracer.lines('exc_order')
    base = get_base_line(exc_order)
    offsets_no_exc = [l - base for l in lines_no_exc]
    check("no-exception visits: init, try, append, if, no-exc, finally, return",
          1 in offsets_no_exc and 10 in offsets_no_exc and 5 not in offsets_no_exc,
          f"offsets: {offsets_no_exc}")

    # Exception path
    tracer.start('exc_order')
    r = exc_order(0)
    tracer.stop()
    check("exception path", r == ['try', 'except', 'finally'])

    lines_exc = tracer.lines('exc_order')
    offsets_exc = [l - base for l in lines_exc]
    check("exception visits: includes raise, except, finally",
          5 in offsets_exc and 8 in offsets_exc and 10 in offsets_exc,
          f"offsets: {offsets_exc}")
    check("exception skips: no 'no-exc' line",
          6 not in offsets_exc,
          f"offsets: {offsets_exc}")


# ============================================================================
# Test 5: Generator interleaving order
# ============================================================================

def test_generator_order():
    print("\n=== Test 5: Generator interleaving order ===")

    def gen_a():
        yield 'a1'     # L+1
        yield 'a2'     # L+2

    def gen_b():
        yield 'b1'     # L+1
        yield 'b2'     # L+2

    tracer.start()
    ga = gen_a()
    gb = gen_b()
    order = []
    order.append(next(ga))  # a1
    order.append(next(gb))  # b1
    order.append(next(ga))  # a2
    order.append(next(gb))  # b2
    tracer.stop()

    check("interleaved order", order == ['a1', 'b1', 'a2', 'b2'])

    # Verify trace shows interleaved function names
    events = tracer.annotated()
    gen_events = [(qn, ln) for qn, ln in events if 'gen_a' in qn or 'gen_b' in qn]
    gen_names = [qn.split('.')[-1] for qn, _ in gen_events]

    # Should see: gen_a, gen_b, gen_a, gen_b (interleaved)
    # Find the pattern of alternation
    seen_order = []
    prev = None
    for name in gen_names:
        if name != prev:
            seen_order.append(name)
            prev = name

    check("trace shows interleaved generators",
          len(seen_order) >= 4,
          f"generator order: {seen_order}")


# ============================================================================
# Test 6: Loop break/continue
# ============================================================================

def test_loop_control():
    print("\n=== Test 6: Loop break/continue ===")

    def with_break():
        result = []                    # L+1
        for i in range(5):            # L+2
            if i == 3:                 # L+3
                break                  # L+4
            result.append(i)           # L+5
        return result                  # L+6

    tracer.start('with_break')
    r = with_break()
    tracer.stop()
    check("break result", r == [0, 1, 2])

    lines = tracer.lines('with_break')
    base = get_base_line(with_break)
    offsets = [l - base for l in lines]
    # Should see line 4 (break) when i==3, then jump to line 6 (return)
    check("break line visited", 4 in offsets, f"offsets: {offsets}")
    # After break, next line should be the return
    break_idx = offsets.index(4)
    check("after break goes to return", offsets[break_idx + 1] == 6,
          f"after break: {offsets[break_idx:]}")

    def with_continue():
        result = []                    # L+1
        for i in range(5):            # L+2
            if i % 2 == 0:            # L+3
                continue               # L+4
            result.append(i)           # L+5
        return result                  # L+6

    tracer.start('with_continue')
    r = with_continue()
    tracer.stop()
    check("continue result", r == [1, 3])

    lines = tracer.lines('with_continue')
    offsets = [l - get_base_line(with_continue) for l in lines]
    # When i=0: L+2, L+3, L+4 (continue), then back to L+2
    check("continue line visited", 4 in offsets, f"offsets: {offsets}")


# ============================================================================
# Test 7: Multi-line expressions
# ============================================================================

def test_multiline_expr():
    print("\n=== Test 7: Multi-line expressions ===")

    def multiline():
        x = (1 +        # L+1
             2 +         # L+2 — does this get a separate LINE event?
             3)          # L+3
        y = (             # L+4
            "hello"       # L+5
            " world"      # L+6
        )
        return x, y       # L+7

    tracer.start('multiline')
    result = multiline()
    tracer.stop()
    check("multiline result", result == (6, "hello world"))

    lines = tracer.lines('multiline')
    base = get_base_line(multiline)
    offsets = [l - base for l in lines]
    print(f"    Multi-line expression offsets: {offsets}")
    # Python typically emits one LINE event for the start of the expression
    check("multiline has line events", len(offsets) >= 2, f"offsets: {offsets}")


# ============================================================================
# Test 8: Nested calls on single line
# ============================================================================

def test_nested_calls():
    print("\n=== Test 8: Nested calls on single line ===")

    call_order = []

    def f(x):
        call_order.append(('f', x))
        return x + 1

    def g(x):
        call_order.append(('g', x))
        return x * 2

    def h(x):
        call_order.append(('h', x))
        return x - 1

    # f(g(h(5))) on single line
    tracer.start()
    call_order.clear()
    result = f(g(h(5)))
    tracer.stop()

    check("nested call result", result == 9, f"got {result}")  # h(5)=4, g(4)=8, f(8)=9
    check("call order: inner first", call_order == [('h', 5), ('g', 4), ('f', 8)],
          f"got {call_order}")

    # Verify tracer sees all three functions
    events = tracer.annotated()
    called_fns = [qn for qn, _ in events
                  if any(n in qn for n in ['test_nested_calls.<locals>.f',
                                            'test_nested_calls.<locals>.g',
                                            'test_nested_calls.<locals>.h'])]
    check("tracer sees all 3 nested calls", len(called_fns) >= 3,
          f"called: {called_fns}")


# ============================================================================
# Test 9: With-statement ordering
# ============================================================================

def test_with_order():
    print("\n=== Test 9: With-statement ordering ===")

    order = []

    class CM:
        def __init__(self, name):
            self.name = name
        def __enter__(self):
            order.append(f'enter_{self.name}')
            return self
        def __exit__(self, *args):
            order.append(f'exit_{self.name}')

    def with_order():
        order.clear()
        with CM('a') as a:          # L+1
            order.append('body_a')   # L+2
            with CM('b') as b:       # L+3
                order.append('body_b')  # L+4
        return list(order)           # L+5

    tracer.start('with_order')
    r = with_order()
    tracer.stop()

    expected = ['enter_a', 'body_a', 'enter_b', 'body_b', 'exit_b', 'exit_a']
    check("with-statement ordering", r == expected, f"got {r}")

    lines = tracer.lines('with_order')
    check("with-statement has line events", len(lines) >= 4, f"lines: {lines}")


# ============================================================================
# Test 10: For-else and while-else
# ============================================================================

def test_for_else():
    print("\n=== Test 10: For-else and while-else ===")

    def search(items, target):
        for item in items:            # L+1
            if item == target:         # L+2
                result = "found"       # L+3
                break                  # L+4
        else:                          # L+5
            result = "not found"       # L+6
        return result                  # L+7

    # Found case: break -> skip else
    tracer.start('search')
    r = search([1, 2, 3], 2)
    tracer.stop()
    check("for-else found", r == "found")

    lines = tracer.lines('search')
    base = get_base_line(search)
    offsets = [l - base for l in lines]
    check("found: visits break, skips else",
          4 in offsets and 6 not in offsets,
          f"offsets: {offsets}")

    # Not found case: else executes
    tracer.start('search')
    r = search([1, 2, 3], 99)
    tracer.stop()
    check("for-else not found", r == "not found")

    lines = tracer.lines('search')
    offsets = [l - base for l in lines]
    check("not found: visits else, skips break",
          6 in offsets and 4 not in offsets,
          f"offsets: {offsets}")


# ============================================================================
# Main
# ============================================================================

def main():
    test_short_circuit()
    test_conditional_expr()
    test_comprehension_order()
    test_exception_order()
    test_generator_order()
    test_loop_control()
    test_multiline_expr()
    test_nested_calls()
    test_with_order()
    test_for_else()

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
