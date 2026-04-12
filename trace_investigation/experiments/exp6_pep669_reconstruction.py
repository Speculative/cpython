"""
Experiment 6: PEP 669 Reconstruction Without Frame Access

Tests whether we can reconstruct execution state using PEP 669 alone:
  - INSTRUCTION events give us (code, offset) at every bytecode op
  - LINE events give us (code, line_number)
  - PY_START/PY_RETURN give us call/return boundaries

The hypothesis: if we pre-analyze bytecode to know which instructions are
STORE_FAST/STORE_NAME/etc., we can detect *when* variables change. The
values are harder — but we can explore strategies:

  A) Track writes by opcode, defer value capture (just record "x changed at line 5")
  B) Use the CALL event's arg0 to capture function arguments
  C) Use PY_RETURN's retval to capture return values
  D) Combine: arguments at call, return values at return, write events at stores

We also measure:
  - Overhead of INSTRUCTION events with a "smart" callback that only records
    writes (vs recording everything)
  - Overhead of LINE-only approach with write detection from bytecode analysis
  - Whether we can reconstruct a useful trace from this information

Part 1: Bytecode analysis — what can we learn statically?
Part 2: Smart INSTRUCTION callback — only fire on STORE_* opcodes
Part 3: LINE + static analysis — reconstruct writes from line-level events
Part 4: Performance comparison of all approaches
"""
import sys
import os
import dis
import time
import types
import opcode

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from workloads import WORKLOADS, run_workload

TOOL_ID = 0
ITERATIONS = 20


# =============================================================================
# Part 1: Static bytecode analysis
# =============================================================================

# Opcodes that write to local variables
STORE_OPCODES = set()
for name, op in opcode.opmap.items():
    if name.startswith('STORE_FAST') or name == 'STORE_NAME' or name == 'STORE_DEREF':
        STORE_OPCODES.add(op)
# Also DELETE variants
for name, op in opcode.opmap.items():
    if name.startswith('DELETE_FAST') or name == 'DELETE_NAME' or name == 'DELETE_DEREF':
        STORE_OPCODES.add(op)


def analyze_code_object(code):
    """Pre-analyze a code object to find all write instructions."""
    instructions = list(dis.get_instructions(code))
    writes = {}  # offset -> (varname, opname, line)
    line_writes = {}  # line -> [(varname, opname, offset), ...]

    for instr in instructions:
        if instr.opcode in STORE_OPCODES:
            writes[instr.offset] = (instr.argval, instr.opname, instr.positions.lineno)
            line = instr.positions.lineno
            if line not in line_writes:
                line_writes[line] = []
            line_writes[line].append((instr.argval, instr.opname, instr.offset))

    return {
        'writes': writes,
        'line_writes': line_writes,
        'total_instructions': len(instructions),
        'write_instructions': len(writes),
        'varnames': code.co_varnames,
        'nlocals': code.co_nlocals,
    }


def demo_static_analysis():
    """Show what static analysis reveals about code objects."""
    print("=== Part 1: Static Bytecode Analysis ===\n")

    def example_func(x, y):
        total = 0
        for i in range(x):
            if i % 2 == 0:
                total += i * y
            else:
                total -= 1
        result = total * 2
        return result

    analysis = analyze_code_object(example_func.__code__)
    print(f"Function: example_func")
    print(f"  Total instructions: {analysis['total_instructions']}")
    print(f"  Write instructions: {analysis['write_instructions']} "
          f"({100*analysis['write_instructions']/analysis['total_instructions']:.0f}%)")
    print(f"  Local variables: {analysis['varnames']}")
    print(f"\n  Writes by line:")
    for line, writes in sorted(analysis['line_writes'].items()):
        for varname, opname, offset in writes:
            print(f"    Line {line}: {opname} {varname} (offset {offset})")

    # Analyze all workload functions
    print(f"\n  Workload analysis:")
    print(f"  {'Function':<25} {'Total ops':>10} {'Writes':>8} {'Write %':>8}")
    print(f"  {'-'*53}")

    from workloads import (fib_iterative, fib_recursive, process_data,
                           transform, should_include, summarize,
                           exception_workload, risky_operation,
                           oop_workload, Counter)

    funcs = [fib_iterative, fib_recursive, process_data, transform,
             should_include, summarize, exception_workload, risky_operation,
             oop_workload]
    for fn in funcs:
        a = analyze_code_object(fn.__code__)
        print(f"  {fn.__name__:<25} {a['total_instructions']:>10} "
              f"{a['write_instructions']:>8} "
              f"{100*a['write_instructions']/max(a['total_instructions'],1):>7.0f}%")

    return analysis


# =============================================================================
# Part 2: Smart INSTRUCTION callback — only record on STORE_* opcodes
# =============================================================================

class SmartInstructionTracer:
    """
    PEP 669 INSTRUCTION callback that uses pre-analyzed bytecode to
    only record events when a STORE_* opcode executes.

    This tests the idea: "what if we selectively DISABLE non-write instructions?"
    PEP 669 supports returning DISABLE from a callback to remove instrumentation
    for that specific instruction.
    """
    def __init__(self):
        self.code_analysis = {}  # id(code) -> analysis
        self.write_events = []
        self.total_callbacks = 0
        self.store_callbacks = 0

    def _get_analysis(self, code):
        cid = id(code)
        if cid not in self.code_analysis:
            self.code_analysis[cid] = analyze_code_object(code)
        return self.code_analysis[cid]

    def instruction_callback(self, code, offset):
        """Called for every instruction. Returns DISABLE for non-writes."""
        self.total_callbacks += 1
        analysis = self._get_analysis(code)
        if offset in analysis['writes']:
            varname, opname, line = analysis['writes'][offset]
            self.write_events.append((id(code), offset, varname, line))
            self.store_callbacks += 1
            return  # Keep instrumentation for this offset
        else:
            # Disable instrumentation for this non-write instruction
            return sys.monitoring.DISABLE

    def instruction_callback_no_disable(self, code, offset):
        """Called for every instruction. Does NOT use DISABLE."""
        self.total_callbacks += 1
        analysis = self._get_analysis(code)
        if offset in analysis['writes']:
            varname, opname, line = analysis['writes'][offset]
            self.write_events.append((id(code), offset, varname, line))
            self.store_callbacks += 1

    def reset(self):
        total = self.total_callbacks
        stores = self.store_callbacks
        events = len(self.write_events)
        self.total_callbacks = 0
        self.store_callbacks = 0
        self.write_events.clear()
        return total, stores, events


# =============================================================================
# Part 3: LINE + call/return with static write analysis
# =============================================================================

class LineWriteTracer:
    """
    Uses LINE events and pre-analyzed bytecode to infer which variables
    *might* have changed on each line (without seeing actual values).

    The trace records: "at line X in function Y, variables Z1, Z2 were written"
    This is a conservative over-approximation (a conditional write may not execute).
    """
    def __init__(self):
        self.code_analysis = {}
        self.events = []
        self.call_stack = []

    def _get_analysis(self, code):
        cid = id(code)
        if cid not in self.code_analysis:
            self.code_analysis[cid] = analyze_code_object(code)
        return self.code_analysis[cid]

    def py_start(self, code, offset):
        analysis = self._get_analysis(code)
        self.call_stack.append(code)
        self.events.append(('call', id(code), code.co_qualname, None))

    def py_return(self, code, offset, retval):
        self.events.append(('return', id(code), code.co_qualname,
                          type(retval).__name__))
        if self.call_stack:
            self.call_stack.pop()

    def line(self, code, line_number):
        analysis = self._get_analysis(code)
        writes_on_line = analysis['line_writes'].get(line_number, [])
        if writes_on_line:
            varnames = [w[0] for w in writes_on_line]
            self.events.append(('line', id(code), line_number, varnames))
        else:
            self.events.append(('line', id(code), line_number, None))

    def call(self, code, offset, callable_, arg0):
        # CALL events give us the callable and first argument
        if isinstance(arg0, (int, float, str, bool, type(None))):
            self.events.append(('call_arg', id(code), offset,
                              (getattr(callable_, '__name__', '?'), arg0)))

    def reset(self):
        events = list(self.events)
        self.events.clear()
        self.code_analysis.clear()
        self.call_stack.clear()
        return events


# =============================================================================
# Part 4: Performance comparison
# =============================================================================

def noop_line(code, line_number):
    pass

def noop_py_start(code, offset):
    pass

def noop_py_return(code, offset, retval):
    pass

def noop_instruction(code, offset):
    pass


def run_perf_comparison():
    E = sys.monitoring.events
    print("\n=== Part 4: Performance Comparison ===\n")

    configs = {}

    # Baseline
    print("Running baseline...")
    baseline = {}
    for name in WORKLOADS:
        _, per_iter = run_workload(name, iterations=ITERATIONS)
        baseline[name] = per_iter
    configs['baseline'] = baseline

    # PEP 669 LINE + call/return (noop) — from exp1
    print("Running PEP 669 LINE+CR noop...")
    sys.monitoring.use_tool_id(TOOL_ID, "exp6")
    sys.monitoring.register_callback(TOOL_ID, E.PY_START, noop_py_start)
    sys.monitoring.register_callback(TOOL_ID, E.PY_RETURN, noop_py_return)
    sys.monitoring.register_callback(TOOL_ID, E.LINE, noop_line)
    sys.monitoring.set_events(TOOL_ID, E.PY_START | E.PY_RETURN | E.LINE)
    line_cr = {}
    for name in WORKLOADS:
        _, per_iter = run_workload(name, iterations=ITERATIONS)
        line_cr[name] = per_iter
    sys.monitoring.set_events(TOOL_ID, 0)
    sys.monitoring.free_tool_id(TOOL_ID)
    configs['line_cr_noop'] = line_cr

    # PEP 669 LINE + call/return with write analysis
    print("Running PEP 669 LINE+CR with write analysis...")
    lwt = LineWriteTracer()
    sys.monitoring.use_tool_id(TOOL_ID, "exp6")
    sys.monitoring.register_callback(TOOL_ID, E.PY_START, lwt.py_start)
    sys.monitoring.register_callback(TOOL_ID, E.PY_RETURN, lwt.py_return)
    sys.monitoring.register_callback(TOOL_ID, E.LINE, lwt.line)
    sys.monitoring.set_events(TOOL_ID, E.PY_START | E.PY_RETURN | E.LINE)
    line_write = {}
    for name in WORKLOADS:
        _, per_iter = run_workload(name, iterations=ITERATIONS)
        line_write[name] = per_iter
    sys.monitoring.set_events(TOOL_ID, 0)
    sys.monitoring.free_tool_id(TOOL_ID)
    configs['line_write_analysis'] = line_write

    # PEP 669 INSTRUCTION (noop) — all instructions
    print("Running PEP 669 INSTRUCTION noop...")
    sys.monitoring.use_tool_id(TOOL_ID, "exp6")
    sys.monitoring.register_callback(TOOL_ID, E.INSTRUCTION, noop_instruction)
    sys.monitoring.set_events(TOOL_ID, E.INSTRUCTION)
    instr_noop = {}
    for name in WORKLOADS:
        _, per_iter = run_workload(name, iterations=ITERATIONS)
        instr_noop[name] = per_iter
    sys.monitoring.set_events(TOOL_ID, 0)
    sys.monitoring.free_tool_id(TOOL_ID)
    configs['instr_noop'] = instr_noop

    # PEP 669 INSTRUCTION with DISABLE for non-writes
    print("Running PEP 669 INSTRUCTION with DISABLE for non-writes...")
    smart = SmartInstructionTracer()
    sys.monitoring.use_tool_id(TOOL_ID, "exp6")
    sys.monitoring.register_callback(TOOL_ID, E.INSTRUCTION, smart.instruction_callback)
    sys.monitoring.set_events(TOOL_ID, E.INSTRUCTION)
    instr_smart = {}
    for name in WORKLOADS:
        _, per_iter = run_workload(name, iterations=ITERATIONS)
        instr_smart[name] = per_iter
    total_cb, store_cb, _ = smart.reset()
    sys.monitoring.set_events(TOOL_ID, 0)
    sys.monitoring.free_tool_id(TOOL_ID)
    configs['instr_smart_disable'] = instr_smart
    print(f"  Smart tracer: {total_cb:,} total callbacks, {store_cb:,} store callbacks")
    if total_cb > 0:
        print(f"  {100*store_cb/total_cb:.1f}% were actual stores")

    # PEP 669 INSTRUCTION smart (no DISABLE, records stores only)
    print("Running PEP 669 INSTRUCTION smart (no DISABLE)...")
    smart2 = SmartInstructionTracer()
    sys.monitoring.use_tool_id(TOOL_ID, "exp6")
    sys.monitoring.register_callback(TOOL_ID, E.INSTRUCTION, smart2.instruction_callback_no_disable)
    sys.monitoring.set_events(TOOL_ID, E.INSTRUCTION)
    instr_smart_nd = {}
    for name in WORKLOADS:
        _, per_iter = run_workload(name, iterations=ITERATIONS)
        instr_smart_nd[name] = per_iter
    total_cb2, store_cb2, _ = smart2.reset()
    sys.monitoring.set_events(TOOL_ID, 0)
    sys.monitoring.free_tool_id(TOOL_ID)
    configs['instr_smart_no_disable'] = instr_smart_nd

    # Results table
    print(f"\n{'Workload':<15} {'Baseline':>10} {'LINE+CR':>10} {'LINE+wrt':>10} "
          f"{'INSTR all':>10} {'INSTR dis':>10} {'INSTR nd':>10}")
    print(f"{'':<15} {'(us)':>10} {'overhead':>10} {'overhead':>10} "
          f"{'overhead':>10} {'overhead':>10} {'overhead':>10}")
    print("-" * 75)

    for name in WORKLOADS:
        b = baseline[name]
        row = f"{name:<15} {b/1000:>9.0f} "
        row += f"{line_cr[name]/b:>9.2f}x "
        row += f"{line_write[name]/b:>9.2f}x "
        row += f"{instr_noop[name]/b:>9.2f}x "
        row += f"{instr_smart[name]/b:>9.2f}x "
        row += f"{instr_smart_nd[name]/b:>9.2f}x "
        print(row)

    print(f"\nLegend:")
    print(f"  LINE+CR    = PEP 669 LINE + call/return, noop callback")
    print(f"  LINE+wrt   = PEP 669 LINE + call/return, with bytecode write analysis per line")
    print(f"  INSTR all  = PEP 669 INSTRUCTION, noop callback on every instruction")
    print(f"  INSTR dis  = PEP 669 INSTRUCTION, DISABLE returned for non-STORE instructions")
    print(f"  INSTR nd   = PEP 669 INSTRUCTION, filter in callback but no DISABLE")


# =============================================================================
# Part 5: Reconstruction demo
# =============================================================================

def demo_reconstruction():
    """Show what we can reconstruct from LINE + write analysis alone."""
    E = sys.monitoring.events

    print("\n=== Part 5: Reconstruction Demo ===\n")

    def target_function(n):
        total = 0
        items = []
        for i in range(n):
            x = i * 2
            if x > 5:
                total += x
                items.append(x)
        return total, items

    lwt = LineWriteTracer()
    sys.monitoring.use_tool_id(TOOL_ID, "exp6")
    sys.monitoring.register_callback(TOOL_ID, E.PY_START, lwt.py_start)
    sys.monitoring.register_callback(TOOL_ID, E.PY_RETURN, lwt.py_return)
    sys.monitoring.register_callback(TOOL_ID, E.LINE, lwt.line)
    sys.monitoring.register_callback(TOOL_ID, E.CALL, lwt.call)
    sys.monitoring.set_events(TOOL_ID, E.PY_START | E.PY_RETURN | E.LINE | E.CALL)

    result = target_function(5)
    events = lwt.reset()

    sys.monitoring.set_events(TOOL_ID, 0)
    sys.monitoring.free_tool_id(TOOL_ID)

    print(f"Result: {result}")
    print(f"Events captured: {len(events)}")
    print(f"\nReconstructed trace:")
    print(f"{'Event':<12} {'Detail':<50} {'Vars written'}")
    print("-" * 80)
    for evt in events:
        if evt[0] == 'call':
            print(f"{'>> CALL':<12} {evt[2]:<50}")
        elif evt[0] == 'return':
            print(f"{'<< RETURN':<12} {evt[2]:<50} retval_type={evt[3]}")
        elif evt[0] == 'line':
            writes = evt[3]
            write_str = ', '.join(writes) if writes else '-'
            print(f"{'   LINE':<12} {'line ' + str(evt[2]):<50} {write_str}")
        elif evt[0] == 'call_arg':
            print(f"{'   CALL_ARG':<12} {str(evt[3]):<50}")

    print(f"\n--- What we know vs don't know ---")
    print(f"KNOW: which function was called and when")
    print(f"KNOW: which line executed in what order")
    print(f"KNOW: which variables were written on each line (from bytecode analysis)")
    print(f"KNOW: return value types")
    print(f"KNOW: first argument to function calls (from CALL event)")
    print(f"DON'T KNOW: actual variable values (without frame access)")
    print(f"DON'T KNOW: which branch of conditional writes executed")
    print(f"\nThe 'variables written' column is a conservative over-approximation:")
    print(f"it lists all possible writes on that line, even if a branch wasn't taken.")


def main():
    analysis = demo_static_analysis()
    run_perf_comparison()
    demo_reconstruction()


if __name__ == '__main__':
    main()
