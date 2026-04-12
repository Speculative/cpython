"""
Experiment 1: PEP 669 Callback Overhead

Measures the overhead of sys.monitoring at different event granularities
using Python-level callbacks. This establishes a baseline for what PEP 669
costs and helps determine whether a C extension is necessary.

Test matrix:
  - No monitoring (baseline)
  - PY_START + PY_RETURN only (call/return)
  - LINE events (line-level tracing)
  - LINE + PY_START + PY_RETURN (typical debugger)
  - INSTRUCTION events (every opcode)
  - sys.settrace for comparison (legacy)

Each configuration is run against all workloads.
"""
import sys
import time
from workloads import WORKLOADS, run_workload

TOOL_ID = 0
ITERATIONS = 20


def noop_py_start(code, instruction_offset):
    pass


def noop_py_return(code, instruction_offset, retval):
    pass


def noop_line(code, line_number):
    pass


def noop_instruction(code, instruction_offset):
    pass


def noop_call(code, instruction_offset, callable, arg0):
    pass


def noop_raise(code, instruction_offset, exception):
    pass


# Counting versions - to verify events are actually firing
class EventCounter:
    def __init__(self):
        self.count = 0

    def py_start(self, code, instruction_offset):
        self.count += 1

    def py_return(self, code, instruction_offset, retval):
        self.count += 1

    def line(self, code, line_number):
        self.count += 1

    def instruction(self, code, instruction_offset):
        self.count += 1

    def reset(self):
        c = self.count
        self.count = 0
        return c


def setup_monitoring(config):
    """Set up monitoring for a given configuration. Returns teardown function."""
    E = sys.monitoring.events

    sys.monitoring.use_tool_id(TOOL_ID, "exp1")

    if config == 'none':
        sys.monitoring.free_tool_id(TOOL_ID)
        return lambda: None

    elif config == 'call_return':
        sys.monitoring.register_callback(TOOL_ID, E.PY_START, noop_py_start)
        sys.monitoring.register_callback(TOOL_ID, E.PY_RETURN, noop_py_return)
        sys.monitoring.set_events(TOOL_ID, E.PY_START | E.PY_RETURN)

    elif config == 'line':
        sys.monitoring.register_callback(TOOL_ID, E.LINE, noop_line)
        sys.monitoring.set_events(TOOL_ID, E.LINE)

    elif config == 'line_call_return':
        sys.monitoring.register_callback(TOOL_ID, E.PY_START, noop_py_start)
        sys.monitoring.register_callback(TOOL_ID, E.PY_RETURN, noop_py_return)
        sys.monitoring.register_callback(TOOL_ID, E.LINE, noop_line)
        sys.monitoring.set_events(TOOL_ID, E.PY_START | E.PY_RETURN | E.LINE)

    elif config == 'line_call_return_exc':
        sys.monitoring.register_callback(TOOL_ID, E.PY_START, noop_py_start)
        sys.monitoring.register_callback(TOOL_ID, E.PY_RETURN, noop_py_return)
        sys.monitoring.register_callback(TOOL_ID, E.LINE, noop_line)
        sys.monitoring.register_callback(TOOL_ID, E.RAISE, noop_raise)
        sys.monitoring.set_events(TOOL_ID, E.PY_START | E.PY_RETURN | E.LINE | E.RAISE)

    elif config == 'instruction':
        sys.monitoring.register_callback(TOOL_ID, E.INSTRUCTION, noop_instruction)
        sys.monitoring.set_events(TOOL_ID, E.INSTRUCTION)

    elif config == 'full':
        sys.monitoring.register_callback(TOOL_ID, E.PY_START, noop_py_start)
        sys.monitoring.register_callback(TOOL_ID, E.PY_RETURN, noop_py_return)
        sys.monitoring.register_callback(TOOL_ID, E.LINE, noop_line)
        sys.monitoring.register_callback(TOOL_ID, E.INSTRUCTION, noop_instruction)
        sys.monitoring.register_callback(TOOL_ID, E.RAISE, noop_raise)
        sys.monitoring.set_events(
            TOOL_ID,
            E.PY_START | E.PY_RETURN | E.LINE | E.INSTRUCTION | E.RAISE
        )

    def teardown():
        sys.monitoring.set_events(TOOL_ID, 0)
        sys.monitoring.free_tool_id(TOOL_ID)

    return teardown


def setup_settrace():
    """Set up sys.settrace for comparison. Returns teardown function."""
    def trace_func(frame, event, arg):
        return trace_func
    sys.settrace(trace_func)
    return lambda: sys.settrace(None)


def count_events(workload_name):
    """Run a workload with counting callbacks to report event volumes."""
    E = sys.monitoring.events
    counter = EventCounter()

    sys.monitoring.use_tool_id(TOOL_ID, "exp1_count")
    sys.monitoring.register_callback(TOOL_ID, E.PY_START, counter.py_start)
    sys.monitoring.register_callback(TOOL_ID, E.PY_RETURN, counter.py_return)
    sys.monitoring.register_callback(TOOL_ID, E.LINE, counter.line)
    sys.monitoring.set_events(TOOL_ID, E.PY_START | E.PY_RETURN | E.LINE)

    counter.reset()
    WORKLOADS[workload_name]()
    total = counter.reset()

    sys.monitoring.set_events(TOOL_ID, 0)
    sys.monitoring.free_tool_id(TOOL_ID)
    return total


def main():
    configs = [
        ('none', 'No monitoring'),
        ('call_return', 'PY_START + PY_RETURN'),
        ('line', 'LINE only'),
        ('line_call_return', 'LINE + call/return'),
        ('line_call_return_exc', 'LINE + call/return + RAISE'),
        ('instruction', 'INSTRUCTION only'),
        ('full', 'All events'),
    ]

    use_settrace = True

    # First, count events per workload
    print("=== Event Counts (per single iteration) ===")
    print(f"{'Workload':<15} {'Events (LINE+call/ret)':>22}")
    print("-" * 39)
    for name in WORKLOADS:
        count = count_events(name)
        print(f"{name:<15} {count:>22,}")
    print()

    # Collect baseline
    print("=== Performance: Absolute Times (us/iter) ===")
    header = f"{'Workload':<15}"
    for _, label in configs:
        header += f" {label:>14}"
    if use_settrace:
        header += f" {'settrace':>14}"
    print(header)
    print("-" * len(header))

    # results[workload][config] = time_ns
    results = {}

    for name in WORKLOADS:
        results[name] = {}
        row = f"{name:<15}"

        for config_key, config_label in configs:
            if config_key == 'none':
                teardown = lambda: None
            else:
                teardown = setup_monitoring(config_key)

            _, per_iter = run_workload(name, iterations=ITERATIONS)
            teardown()
            results[name][config_key] = per_iter
            row += f" {per_iter/1000:>14.1f}"

        if use_settrace:
            teardown = setup_settrace()
            _, per_iter = run_workload(name, iterations=ITERATIONS)
            teardown()
            results[name]['settrace'] = per_iter
            row += f" {per_iter/1000:>14.1f}"

        print(row)

    # Print overhead ratios
    print()
    print("=== Overhead Ratios (vs no monitoring) ===")
    header = f"{'Workload':<15}"
    ratio_configs = [c for c in configs if c[0] != 'none']
    for _, label in ratio_configs:
        header += f" {label:>14}"
    if use_settrace:
        header += f" {'settrace':>14}"
    print(header)
    print("-" * len(header))

    for name in WORKLOADS:
        baseline = results[name]['none']
        row = f"{name:<15}"
        for config_key, _ in ratio_configs:
            ratio = results[name][config_key] / baseline
            row += f" {ratio:>13.2f}x"
        if use_settrace:
            ratio = results[name]['settrace'] / baseline
            row += f" {ratio:>13.2f}x"
        print(row)


if __name__ == '__main__':
    main()
