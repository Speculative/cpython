"""
Experiment 18: Long-Running Stress Tests with C Extension

Same workloads as Experiment 17, but using the C extension (ctrace2
selective mode) instead of Python settrace. No disk write yet — just
measures the capture overhead at scale, then compares against:
  - Baseline (no tracing)
  - C noop (settrace floor)
  - C selective (pre-computed maps + targeted GetVar)
  - C GetLocals (read all locals as dict)
  - Python settrace + f_locals + disk write (from exp17)

Also runs with disk write using C capture + Python-side serialization
to measure the full pipeline.
"""
import sys
import os
import time
import struct

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'exp7_c_extension'))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'exp10_c_extension'))

import _ctrace
import _ctrace2
import dis
import types
import opcode
import tempfile

TIME_LIMIT_S = 10


# ============================================================================
# Code registration (reused)
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


def register_module_code(module_dict):
    registered = set()
    for name, obj in module_dict.items():
        if callable(obj) and hasattr(obj, '__code__'):
            if id(obj.__code__) not in registered:
                analyze_and_register(obj.__code__)
                registered.add(id(obj.__code__))


# ============================================================================
# Python settrace with disk write (reference from exp17)
# ============================================================================

class PythonDiskTracer:
    def __init__(self, filepath, flush_interval=5000):
        self.fd = os.open(filepath, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        self.buf = bytearray()
        self.flush_interval = flush_interval
        self.events = 0
        self.bytes_written = 0
        self.events_since_flush = 0
        self.prev_locals = {}
        self.start_time = 0
        self.stopped = False

    def _write_event(self, etype, lineno, code_hash, changes):
        n = min(len(changes), 255)
        self.buf.extend(struct.pack('<BiiB', etype, lineno, code_hash, n))
        for i, (name, value) in enumerate(changes.items()):
            if i >= n:
                break
            nb = name.encode('utf-8')[:255]
            self.buf.append(len(nb))
            self.buf.extend(nb)
            if value is None:
                self.buf.append(0)
            elif isinstance(value, bool):
                self.buf.append(1)
                self.buf.append(1 if value else 0)
            elif isinstance(value, int):
                self.buf.append(2)
                try:
                    self.buf.extend(struct.pack('<q', value))
                except struct.error:
                    self.buf.extend(b'\x00' * 8)
            elif isinstance(value, float):
                self.buf.append(3)
                self.buf.extend(struct.pack('<d', value))
            elif isinstance(value, str):
                self.buf.append(4)
                s = value.encode('utf-8')[:256]
                self.buf.extend(struct.pack('<H', len(s)))
                self.buf.extend(s)
            else:
                self.buf.append(5)
                t = type(value).__name__.encode('utf-8')[:64]
                self.buf.extend(struct.pack('<H', len(t)))
                self.buf.extend(t)
        self.events += 1
        self.events_since_flush += 1
        if self.events_since_flush >= self.flush_interval:
            self._flush()

    def _flush(self):
        if self.buf:
            os.write(self.fd, bytes(self.buf))
            self.bytes_written += len(self.buf)
            self.buf.clear()
            self.events_since_flush = 0

    def trace_func(self, frame, event, arg):
        if self.stopped:
            return None
        code = frame.f_code
        if '/lib/' in code.co_filename.replace('\\', '/'):
            return self.trace_func
        if code.co_qualname.startswith('PythonDiskTracer'):
            return self.trace_func
        ch = hash(id(code)) & 0x7FFFFFFF
        if event == 'call':
            fid = id(frame)
            try:
                cur = dict(frame.f_locals)
            except Exception:
                cur = {}
            changes = {k: v for k, v in cur.items() if not k.startswith('__')}
            self.prev_locals[fid] = cur
            self._write_event(1, frame.f_lineno, ch, changes)
        elif event == 'line':
            fid = id(frame)
            try:
                cur = dict(frame.f_locals)
            except Exception:
                cur = {}
            prev = self.prev_locals.get(fid, {})
            changes = {}
            for k, v in cur.items():
                if k.startswith('__'):
                    continue
                if k not in prev or prev[k] is not v:
                    changes[k] = v
            self.prev_locals[fid] = cur
            self._write_event(2, frame.f_lineno, ch, changes)
        elif event == 'return':
            fid = id(frame)
            self.prev_locals.pop(fid, None)
            self._write_event(3, frame.f_lineno, ch, {})
        if self.events % 10000 == 0:
            if time.monotonic() - self.start_time > TIME_LIMIT_S:
                self.stopped = True
                return None
        return self.trace_func

    def start(self):
        self.start_time = time.monotonic()
        sys.settrace(self.trace_func)

    def stop(self):
        sys.settrace(None)
        self._flush()
        os.close(self.fd)


# ============================================================================
# Workloads (same as exp17)
# ============================================================================

def stress_compute():
    def collatz_length(n):
        steps = 0
        while n != 1:
            if n % 2 == 0:
                n = n // 2
            else:
                n = 3 * n + 1
            steps += 1
        return steps

    def prime_factors(n):
        factors = []
        d = 2
        while d * d <= n:
            while n % d == 0:
                factors.append(d)
                n //= d
            d += 1
        if n > 1:
            factors.append(n)
        return factors

    def gcd(a, b):
        while b:
            a, b = b, a % b
        return a

    total = 0
    max_collatz = 0
    for i in range(1, 500_000):
        cl = collatz_length(i)
        if cl > max_collatz:
            max_collatz = cl
        if i % 100 == 0:
            pf = prime_factors(i)
            total += len(pf)
        if i % 50 == 0:
            g = gcd(i, i + 37)
            total += g
    return total, max_collatz


def stress_large_objects():
    def build_dataset(n):
        data = []
        for i in range(n):
            record = {
                'id': i,
                'name': f'item_{i:06d}',
                'values': [j * i for j in range(20)],
                'tags': {f'tag_{k}': k * i for k in range(5)},
                'description': f'This is item number {i} with various attributes ' * 3,
            }
            data.append(record)
        return data

    def filter_dataset(data, predicate):
        result = []
        for record in data:
            if predicate(record):
                result.append(record)
        return result

    def transform_dataset(data):
        transformed = []
        for record in data:
            new_record = {
                'id': record['id'],
                'total': sum(record['values']),
                'tag_sum': sum(record['tags'].values()),
                'name_upper': record['name'].upper(),
            }
            transformed.append(new_record)
        return transformed

    def aggregate(data, key):
        buckets = {}
        for record in data:
            bucket = record[key] % 10
            if bucket not in buckets:
                buckets[bucket] = []
            buckets[bucket].append(record)
        return buckets

    def sort_and_rank(data, key):
        sorted_data = sorted(data, key=lambda r: r[key], reverse=True)
        ranked = []
        for rank, record in enumerate(sorted_data, 1):
            record_copy = dict(record)
            record_copy['rank'] = rank
            ranked.append(record_copy)
        return ranked

    total_ranked = 0
    for iteration in range(5):
        data = build_dataset(10000)
        even_ids = filter_dataset(data, lambda r: r['id'] % 2 == 0)
        transformed = transform_dataset(even_ids)
        buckets = aggregate(transformed, 'id')
        for bucket_id, items in buckets.items():
            ranked = sort_and_rank(items, 'total')
            total_ranked += len(ranked)

    return len(data), len(even_ids), len(transformed), len(buckets), total_ranked


# ============================================================================
# Runner
# ============================================================================

def timed_run(label, workload_fn, setup_fn=None, teardown_fn=None):
    """Run workload with optional setup/teardown, return elapsed seconds."""
    if setup_fn:
        setup_fn()
    start = time.monotonic()
    result = workload_fn()
    elapsed = time.monotonic() - start
    if teardown_fn:
        teardown_fn()
    return elapsed, result


def run_stress(name, workload_fn):
    print(f"\n{'='*80}")
    print(f"STRESS: {name}")
    print(f"{'='*80}")

    tmpdir = tempfile.mkdtemp(prefix='trace_stress_')

    # Register code objects for C selective
    _ctrace2.clear()
    # We need to register code from the nested functions too
    analyze_and_register(workload_fn.__code__)

    # --- Baseline ---
    print("  Baseline...", end="", flush=True)
    baseline_t, baseline_r = timed_run("baseline", workload_fn)
    print(f" {baseline_t:.2f}s")

    configs = []

    # --- C noop ---
    configs.append(('C noop', 0,
        lambda: _ctrace.start(0),
        lambda: _ctrace.stop()))

    # --- C selective (no disk) ---
    configs.append(('C selective', 0,
        lambda: _ctrace2.start(1),
        lambda: _ctrace2.stop()))

    # --- C GetLocals (no disk) ---
    configs.append(('C GetLocals', 0,
        lambda: _ctrace.start(4),
        lambda: _ctrace.stop()))

    # --- Python settrace + disk write ---
    py_disk_path = os.path.join(tmpdir, f'{name}_py_disk.trace')
    py_tracer = [None]  # mutable ref

    def setup_py_disk():
        py_tracer[0] = PythonDiskTracer(py_disk_path)
        py_tracer[0].start()

    def teardown_py_disk():
        py_tracer[0].stop()

    configs.append(('Py st+disk', 1, setup_py_disk, teardown_py_disk))

    # Run all
    results = {}
    for label, has_disk, setup_fn, teardown_fn in configs:
        print(f"  {label}...", end="", flush=True)
        elapsed, result = timed_run(label, workload_fn, setup_fn, teardown_fn)
        correct = (result == baseline_r)

        entry = {
            'time': elapsed,
            'correct': correct,
            'overhead': elapsed / baseline_t if baseline_t > 0 else 0,
        }

        if has_disk and py_tracer[0]:
            entry['events'] = py_tracer[0].events
            entry['bytes'] = py_tracer[0].bytes_written
            entry['events_per_sec'] = py_tracer[0].events / elapsed if elapsed > 0 else 0
            entry['mb_per_sec'] = py_tracer[0].bytes_written / elapsed / 1024 / 1024 if elapsed > 0 else 0
            entry['bytes_per_event'] = py_tracer[0].bytes_written / py_tracer[0].events if py_tracer[0].events > 0 else 0
        elif label == 'C selective':
            stats = _ctrace2.stats()
            entry['c_stats'] = stats

        results[label] = entry
        print(f" {elapsed:.2f}s ({entry['overhead']:.1f}x) {'OK' if correct else 'WRONG'}")

    # Print table
    print(f"\n  {'Config':<16} {'Time':>8} {'Overhead':>9} {'Events/s':>12} {'MB/s':>8} {'B/evt':>7} {'File':>10}")
    print(f"  {'-'*72}")
    print(f"  {'Baseline':<16} {baseline_t:>7.2f}s {'1.0x':>9}")
    for label, _, _, _ in configs:
        r = results[label]
        row = f"  {label:<16} {r['time']:>7.2f}s {r['overhead']:>8.1f}x"
        if 'events_per_sec' in r:
            row += f" {r['events_per_sec']:>12,.0f} {r['mb_per_sec']:>7.1f} {r['bytes_per_event']:>6.1f} {r['bytes']/1024/1024:>8.1f}MB"
        elif 'c_stats' in r:
            s = r['c_stats']
            row += f" {s['events']/r['time']:>12,.0f}  (capture only, no disk)"
        print(row)

    if 'C selective' in results and results['C selective'].get('c_stats'):
        s = results['C selective']['c_stats']
        print(f"\n  C selective capture stats:")
        print(f"    Events: {s['events']:,}, Line events: {s['line_events']:,}")
        print(f"    Lines with writes: {s['lines_with_writes']:,}")
        print(f"    Vars checked: {s['vars_checked']:,}, Changed: {s['vars_changed']:,}")
        if s['vars_checked'] > 0:
            print(f"    Change hit rate: {100*s['vars_changed']/s['vars_checked']:.1f}%")

    # Extrapolation
    if 'Py st+disk' in results and 'events_per_sec' in results['Py st+disk']:
        r = results['Py st+disk']
        print(f"\n  Hourly extrapolation (Py st+disk):")
        print(f"    Events: {r['events_per_sec']*3600:,.0f}")
        print(f"    Data: {r['mb_per_sec']*3600/1024:.1f} GB")

    # Cleanup
    for f in os.listdir(tmpdir):
        os.unlink(os.path.join(tmpdir, f))
    os.rmdir(tmpdir)

    return results


def main():
    r_compute = run_stress('compute_heavy', stress_compute)
    r_large = run_stress('large_objects', stress_large_objects)

    # Final summary
    print(f"\n{'='*80}")
    print("FINAL SUMMARY: C Extension vs Python Settrace")
    print(f"{'='*80}")

    print(f"\n  {'Workload':<16} {'C noop':>8} {'C select':>9} {'C GetLoc':>9} {'Py+disk':>9}")
    print(f"  {'-'*54}")
    for name, results in [('compute_heavy', r_compute), ('large_objects', r_large)]:
        cn = results['C noop']['overhead']
        cs = results['C selective']['overhead']
        cg = results['C GetLocals']['overhead']
        pd = results['Py st+disk']['overhead']
        print(f"  {name:<16} {cn:>7.1f}x {cs:>8.1f}x {cg:>8.1f}x {pd:>8.1f}x")

    print(f"\n  C selective captures variable values with no disk write.")
    print(f"  Adding C-level serialization + disk would add ~0.5-1x (estimated).")


if __name__ == '__main__':
    main()
