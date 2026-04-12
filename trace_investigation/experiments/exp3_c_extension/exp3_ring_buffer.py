"""
Experiment 3: C Extension Ring Buffer vs Python Callbacks

Compares performance of:
  - Python noop callbacks (from exp1)
  - C extension callbacks writing to a ring buffer
  - C extension callbacks (noop, just to measure call overhead)

The C extension (_tracebuf) must be built first:
  cd exp3_c_extension && ../../../build-opt/python setup.py build_ext --inplace
"""
import sys
import os
import time

# Add parent dir for workloads
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Also try current dir for workloads
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

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


def run_config(label, setup_fn, teardown_fn, workloads=None):
    """Run all workloads with a given monitoring config."""
    if workloads is None:
        workloads = WORKLOADS
    results = {}
    setup_fn()
    for name in workloads:
        _, per_iter = run_workload(name, iterations=ITERATIONS)
        results[name] = per_iter
    teardown_fn()
    return results


def main():
    E = sys.monitoring.events

    # Try to import C extension
    try:
        import _tracebuf
        has_c_ext = True
        print("C extension _tracebuf loaded successfully")
    except ImportError as e:
        has_c_ext = False
        print(f"WARNING: C extension not available: {e}")
        print("Build it with: cd exp3_c_extension && <python> setup.py build_ext --inplace")
        print("Continuing with Python-only tests...\n")

    print("=" * 70)
    print("Experiment 3: C Extension Ring Buffer Performance")
    print("=" * 70)

    # --- Baseline: no monitoring ---
    def noop(): pass
    baseline = run_config("baseline", noop, noop)

    # --- Python noop callbacks: LINE + call/return ---
    def setup_py_line_cr():
        sys.monitoring.use_tool_id(TOOL_ID, "exp3")
        sys.monitoring.register_callback(TOOL_ID, E.PY_START, noop_py_start)
        sys.monitoring.register_callback(TOOL_ID, E.PY_RETURN, noop_py_return)
        sys.monitoring.register_callback(TOOL_ID, E.LINE, noop_line)
        sys.monitoring.set_events(TOOL_ID, E.PY_START | E.PY_RETURN | E.LINE)

    def teardown_mon():
        sys.monitoring.set_events(TOOL_ID, 0)
        sys.monitoring.free_tool_id(TOOL_ID)

    py_line_cr = run_config("py_line_cr", setup_py_line_cr, teardown_mon)

    # --- C extension callbacks: LINE + call/return (with ring buffer) ---
    if has_c_ext:
        def setup_c_line_cr():
            _tracebuf.start()
            sys.monitoring.use_tool_id(TOOL_ID, "exp3")
            sys.monitoring.register_callback(TOOL_ID, E.PY_START, _tracebuf.cb_py_start)
            sys.monitoring.register_callback(TOOL_ID, E.PY_RETURN, _tracebuf.cb_py_return)
            sys.monitoring.register_callback(TOOL_ID, E.LINE, _tracebuf.cb_line)
            sys.monitoring.set_events(TOOL_ID, E.PY_START | E.PY_RETURN | E.LINE)

        def teardown_c():
            sys.monitoring.set_events(TOOL_ID, 0)
            sys.monitoring.free_tool_id(TOOL_ID)
            _tracebuf.stop()

        c_line_cr = run_config("c_line_cr", setup_c_line_cr, teardown_c)

        # Check that events were actually recorded
        stats = _tracebuf.stats()
        print(f"\nRing buffer stats: {stats}")
        _tracebuf.reset()

        # --- C extension: INSTRUCTION level ---
        def setup_c_instruction():
            _tracebuf.start()
            sys.monitoring.use_tool_id(TOOL_ID, "exp3")
            sys.monitoring.register_callback(TOOL_ID, E.INSTRUCTION, _tracebuf.cb_instruction)
            sys.monitoring.set_events(TOOL_ID, E.INSTRUCTION)

        c_instruction = run_config("c_instruction", setup_c_instruction, teardown_c)
        _tracebuf.reset()

    # --- Python noop: INSTRUCTION level ---
    def setup_py_instruction():
        sys.monitoring.use_tool_id(TOOL_ID, "exp3")
        sys.monitoring.register_callback(TOOL_ID, E.INSTRUCTION, noop_instruction)
        sys.monitoring.set_events(TOOL_ID, E.INSTRUCTION)

    py_instruction = run_config("py_instruction", setup_py_instruction, teardown_mon)

    # --- Print results ---
    print(f"\n{'':=<90}")
    print("Results: Overhead Ratios (vs no monitoring)")
    print(f"{'':=<90}")

    header = f"{'Workload':<15} {'Py LINE+cr':>12} {'Py INSTR':>12}"
    if has_c_ext:
        header += f" {'C LINE+cr':>12} {'C INSTR':>12} {'C/Py LINE':>12} {'C/Py INSTR':>12}"
    print(header)
    print("-" * len(header))

    for name in WORKLOADS:
        b = baseline[name]
        py_lcr_ratio = py_line_cr[name] / b
        py_ins_ratio = py_instruction[name] / b
        row = f"{name:<15} {py_lcr_ratio:>11.2f}x {py_ins_ratio:>11.2f}x"

        if has_c_ext:
            c_lcr_ratio = c_line_cr[name] / b
            c_ins_ratio = c_instruction[name] / b
            # C as fraction of Python overhead (lower = better)
            c_vs_py_lcr = (c_line_cr[name] - b) / max(py_line_cr[name] - b, 1)
            c_vs_py_ins = (c_instruction[name] - b) / max(py_instruction[name] - b, 1)
            row += f" {c_lcr_ratio:>11.2f}x {c_ins_ratio:>11.2f}x {c_vs_py_lcr:>11.0%} {c_vs_py_ins:>11.0%}"

        print(row)

    if has_c_ext:
        print(f"\nC/Py columns show C overhead as a percentage of Python overhead (lower = C is faster)")

    # --- Verify correctness: inspect some captured events ---
    if has_c_ext:
        print(f"\n{'':=<90}")
        print("Correctness check: sample captured events")
        print(f"{'':=<90}")

        _tracebuf.start()
        sys.monitoring.use_tool_id(TOOL_ID, "exp3")
        sys.monitoring.register_callback(TOOL_ID, E.PY_START, _tracebuf.cb_py_start)
        sys.monitoring.register_callback(TOOL_ID, E.PY_RETURN, _tracebuf.cb_py_return)
        sys.monitoring.register_callback(TOOL_ID, E.LINE, _tracebuf.cb_line)
        sys.monitoring.set_events(TOOL_ID, E.PY_START | E.PY_RETURN | E.LINE)

        # Run a simple function
        def sample_func():
            x = 1
            y = 2
            return x + y

        sample_func()

        sys.monitoring.set_events(TOOL_ID, 0)
        sys.monitoring.free_tool_id(TOOL_ID)
        _tracebuf.stop()

        events = _tracebuf.get_events(20)
        event_names = {0: 'PY_START', 1: 'PY_RETURN', 2: 'LINE', 3: 'INSTRUCTION'}
        print(f"Captured {len(events)} events from sample_func():")
        for ts, code_id, offset, evt_type in events:
            print(f"  {event_names.get(evt_type, '?'):<12} code=0x{code_id:x} offset/line={offset}")


if __name__ == '__main__':
    main()
