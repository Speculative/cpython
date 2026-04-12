"""
Experiment 16: Demo — Captured Trace Output Mapped Back to Source

Shows exactly what our tracer captures and how it reconstructs a
step-through debugging view with source code.
"""
import sys
import os
import dis
import types
import opcode
import textwrap
import inspect

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'exp7_c_extension'))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'exp10_c_extension'))

import _ctrace2


# ============================================================================
# Bytecode analysis (reused from exp10/15)
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
                line_bitmasks.setdefault(line, 0)
                line_bitmasks[line] |= (1 << idx)
    first_line = code.co_firstlineno
    packed = [(line, mask) for line, mask in line_bitmasks.items()]
    _ctrace2.register_code(code, first_line, packed)
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            analyze_and_register(const, visited)


# ============================================================================
# Full-fidelity Python tracer that captures everything for display
# ============================================================================

class DemoTracer:
    """Captures full trace with source association for display."""

    def __init__(self):
        self.events = []
        self.code_sources = {}  # id(code) -> {lineno: source_line}
        self.prev_locals = {}   # frame_id -> {name: value}
        self.depth = 0

    def _cache_source(self, code):
        cid = id(code)
        if cid in self.code_sources:
            return
        try:
            source_lines = inspect.getsourcelines(code)
            start_line = source_lines[1]
            self.code_sources[cid] = {
                start_line + i: line.rstrip()
                for i, line in enumerate(source_lines[0])
            }
        except (OSError, TypeError):
            self.code_sources[cid] = {}

    def _get_changes(self, frame):
        fid = id(frame)
        current = dict(frame.f_locals)
        prev = self.prev_locals.get(fid, {})
        changes = {}
        for k, v in current.items():
            if k.startswith('__'):
                continue
            if k not in prev or prev[k] is not v:
                changes[k] = v
        self.prev_locals[fid] = current
        return changes

    def trace_func(self, frame, event, arg):
        code = frame.f_code
        # Skip stdlib frames (but keep our demo functions)
        if '/lib/' in code.co_filename.replace('\\', '/'):
            return self.trace_func
        # Skip the tracer itself and the main() driver
        if code.co_qualname in ('DemoTracer.trace_func', 'DemoTracer._get_changes',
                                'DemoTracer._cache_source', 'DemoTracer.start',
                                'DemoTracer.stop', 'main'):
            return self.trace_func

        self._cache_source(code)
        cid = id(code)

        if event == 'call':
            self.depth += 1
            changes = self._get_changes(frame)
            self.events.append({
                'event': 'call',
                'qualname': code.co_qualname,
                'lineno': frame.f_lineno,
                'code_id': cid,
                'depth': self.depth,
                'changes': changes,
            })
        elif event == 'line':
            changes = self._get_changes(frame)
            self.events.append({
                'event': 'line',
                'qualname': code.co_qualname,
                'lineno': frame.f_lineno,
                'code_id': cid,
                'depth': self.depth,
                'changes': changes,
            })
        elif event == 'return':
            self.events.append({
                'event': 'return',
                'qualname': code.co_qualname,
                'lineno': frame.f_lineno,
                'code_id': cid,
                'depth': self.depth,
                'retval': arg,
            })
            fid = id(frame)
            self.prev_locals.pop(fid, None)
            self.depth -= 1
        elif event == 'exception':
            self.events.append({
                'event': 'exception',
                'qualname': code.co_qualname,
                'lineno': frame.f_lineno,
                'code_id': cid,
                'depth': self.depth,
                'exc': arg[1] if arg else None,
            })

        return self.trace_func

    def start(self):
        self.events.clear()
        self.prev_locals.clear()
        self.depth = 0
        sys.settrace(self.trace_func)

    def stop(self):
        sys.settrace(None)

    def format_value(self, v, max_len=40):
        r = repr(v)
        if len(r) > max_len:
            r = r[:max_len-3] + '...'
        return r

    def render(self, max_events=None):
        """Render the captured trace as a step-through debugging view."""
        lines = []
        count = 0
        for evt in self.events:
            if max_events and count >= max_events:
                lines.append(f"  ... ({len(self.events) - count} more events)")
                break
            count += 1

            indent = "  " * evt['depth']
            source_map = self.code_sources.get(evt['code_id'], {})
            source_line = source_map.get(evt['lineno'], '')

            if evt['event'] == 'call':
                changes = evt['changes']
                args_str = ""
                if changes:
                    args_str = ", ".join(
                        f"{k}={self.format_value(v)}"
                        for k, v in changes.items()
                    )
                lines.append(f"{indent}>> {evt['qualname']}({args_str})")

            elif evt['event'] == 'line':
                # Show the source line
                lineno = evt['lineno']
                src = source_line.strip() if source_line else f"(line {lineno})"
                line = f"{indent}   {lineno:>4} | {src}"

                # Show variable changes
                changes = evt['changes']
                if changes:
                    var_parts = []
                    for k, v in changes.items():
                        var_parts.append(f"{k} = {self.format_value(v)}")
                    line += f"  # {', '.join(var_parts)}"

                lines.append(line)

            elif evt['event'] == 'return':
                retval = self.format_value(evt.get('retval'))
                lines.append(f"{indent}<< return {retval}")

            elif evt['event'] == 'exception':
                exc = evt.get('exc')
                lines.append(f"{indent}!! {type(exc).__name__}: {exc}")

        return "\n".join(lines)


# ============================================================================
# Demo programs
# ============================================================================

def binary_search(arr, target):
    low = 0
    high = len(arr) - 1
    while low <= high:
        mid = (low + high) // 2
        if arr[mid] == target:
            return mid
        elif arr[mid] < target:
            low = mid + 1
        else:
            high = mid - 1
    return -1


def fizzbuzz(n):
    results = []
    for i in range(1, n + 1):
        if i % 15 == 0:
            results.append("FizzBuzz")
        elif i % 3 == 0:
            results.append("Fizz")
        elif i % 5 == 0:
            results.append("Buzz")
        else:
            results.append(str(i))
    return results


class BankAccount:
    def __init__(self, owner, balance=0):
        self.owner = owner
        self.balance = balance
        self.history = []

    def deposit(self, amount):
        self.balance += amount
        self.history.append(('deposit', amount))
        return self.balance

    def withdraw(self, amount):
        if amount > self.balance:
            raise ValueError(f"Insufficient funds: {self.balance} < {amount}")
        self.balance -= amount
        self.history.append(('withdraw', amount))
        return self.balance


def transfer(src, dst, amount):
    src.withdraw(amount)
    dst.deposit(amount)
    return src.balance, dst.balance


def make_multiplier(factor):
    def multiply(x):
        return x * factor
    return multiply


def pipeline_demo():
    data = [3, 1, 4, 1, 5, 9, 2, 6]
    double = make_multiplier(2)
    tripled = [double(x) + x for x in data if x > 3]
    total = sum(tripled)
    return total


# ============================================================================
# Run demos
# ============================================================================

def main():
    tracer = DemoTracer()

    # --- Demo 1: Binary Search ---
    print("=" * 70)
    print("DEMO 1: Binary Search — finding 7 in [1, 3, 5, 7, 9, 11, 13]")
    print("=" * 70)

    tracer.start()
    result = binary_search([1, 3, 5, 7, 9, 11, 13], 7)
    tracer.stop()

    print(f"\nResult: {result}")
    print(f"\nCaptured trace ({len(tracer.events)} events):\n")
    print(tracer.render())

    # --- Demo 2: FizzBuzz ---
    print("\n" + "=" * 70)
    print("DEMO 2: FizzBuzz(7)")
    print("=" * 70)

    tracer.start()
    result = fizzbuzz(7)
    tracer.stop()

    print(f"\nResult: {result}")
    print(f"\nCaptured trace ({len(tracer.events)} events):\n")
    print(tracer.render())

    # --- Demo 3: Bank Account with exception ---
    print("\n" + "=" * 70)
    print("DEMO 3: Bank Account — deposit, withdraw, failed transfer")
    print("=" * 70)

    tracer.start()
    alice = BankAccount("Alice", 100)
    bob = BankAccount("Bob", 50)
    alice.deposit(25)
    bob.deposit(30)
    try:
        transfer(bob, alice, 200)  # Should fail — Bob only has 80
    except ValueError as e:
        error = str(e)
    tracer.stop()

    print(f"\nAlice: {alice.balance}, Bob: {bob.balance}, Error: {error}")
    print(f"\nCaptured trace ({len(tracer.events)} events):\n")
    print(tracer.render())

    # --- Demo 4: Closure + comprehension pipeline ---
    print("\n" + "=" * 70)
    print("DEMO 4: Closure + comprehension pipeline")
    print("=" * 70)

    tracer.start()
    result = pipeline_demo()
    tracer.stop()

    print(f"\nResult: {result}")
    print(f"\nCaptured trace ({len(tracer.events)} events):\n")
    print(tracer.render())

    # --- Demo 5: Recursive Fibonacci with variable tracking ---
    print("\n" + "=" * 70)
    print("DEMO 5: Recursive function — factorial(5)")
    print("=" * 70)

    def factorial(n):
        if n <= 1:
            return 1
        result = n * factorial(n - 1)
        return result

    tracer.start()
    result = factorial(5)
    tracer.stop()

    print(f"\nResult: {result}")
    print(f"\nCaptured trace ({len(tracer.events)} events):\n")
    print(tracer.render())


if __name__ == '__main__':
    main()
