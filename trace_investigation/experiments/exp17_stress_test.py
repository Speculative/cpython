"""
Experiment 17: Long-Running Stress Tests with Disk Persistence

Two stress tests capped at 10 seconds or 10GB of trace data:

Test A: Heavy computation with small variables
  - Tight numerical loops, many function calls
  - Variables are ints/floats (cheap to serialize)
  - Generates maximum event rate

Test B: Computation over large objects
  - Manipulates large lists, dicts, strings
  - Variables are complex objects (expensive to repr, large to serialize)
  - Tests whether large objects blow up trace size

For each test, we:
  1. Run without tracing (baseline timing)
  2. Run with full Python settrace + capture to disk
  3. Measure: total events, data written, events/sec, MB/sec, overhead
"""
import sys
import os
import time
import struct
import inspect

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TIME_LIMIT_S = 10
SIZE_LIMIT_BYTES = 10 * 1024 * 1024 * 1024  # 10 GB


# ============================================================================
# Compact binary serializer
# ============================================================================

class BinaryTraceWriter:
    """Writes trace events to disk in a compact binary format.

    Event format:
      Header: event_type(1) + lineno(4) + code_hash(4) + n_changes(1) = 10 bytes
      Per change: name_len(1) + name(N) + type_tag(1) + value_data(variable)

    Type tags:
      0: None (0 bytes)
      1: bool (1 byte)
      2: int (8 bytes, or 0 if overflow)
      3: float (8 bytes)
      4: str (2 bytes len + data, truncated to 256)
      5: other (2 bytes len + type_name, truncated to 64)
    """

    def __init__(self, filepath, flush_interval=1000):
        self.filepath = filepath
        self.flush_interval = flush_interval
        self.fd = os.open(filepath, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        self.buf = bytearray()
        self.events_written = 0
        self.bytes_written = 0
        self.events_since_flush = 0
        self.limit_reached = False

    def write_event(self, event_type, lineno, code_hash, changes):
        if self.limit_reached:
            return

        n_changes = min(len(changes), 255)

        # Header
        self.buf.extend(struct.pack('<BiiB', event_type, lineno, code_hash, n_changes))

        # Changes
        for i, (name, value) in enumerate(changes.items()):
            if i >= n_changes:
                break
            # Name
            name_bytes = name.encode('utf-8')[:255]
            self.buf.append(len(name_bytes))
            self.buf.extend(name_bytes)

            # Value
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

        self.events_written += 1
        self.events_since_flush += 1

        if self.events_since_flush >= self.flush_interval:
            self._flush()

    def _flush(self):
        if self.buf:
            os.write(self.fd, bytes(self.buf))
            self.bytes_written += len(self.buf)
            self.buf.clear()
            self.events_since_flush = 0

            if self.bytes_written >= SIZE_LIMIT_BYTES:
                self.limit_reached = True

    def close(self):
        self._flush()
        os.close(self.fd)

    @property
    def total_bytes(self):
        return self.bytes_written + len(self.buf)


# ============================================================================
# Traced runner
# ============================================================================

class StressTracer:
    """settrace-based tracer that writes to disk via BinaryTraceWriter."""

    def __init__(self, writer):
        self.writer = writer
        self.prev_locals = {}
        self.start_time = 0
        self.time_limit_reached = False
        self._skip_qualnames = frozenset({
            'StressTracer.trace_func', 'StressTracer.__init__',
            'BinaryTraceWriter.write_event', 'BinaryTraceWriter._flush',
            'BinaryTraceWriter.close',
        })

    def trace_func(self, frame, event, arg):
        if self.time_limit_reached or self.writer.limit_reached:
            return None  # stop tracing

        code = frame.f_code

        # Skip tracer internals and stdlib
        if code.co_qualname in self._skip_qualnames:
            return self.trace_func
        if '/lib/' in code.co_filename.replace('\\', '/'):
            return self.trace_func

        code_hash = hash(id(code)) & 0x7FFFFFFF

        if event == 'call':
            fid = id(frame)
            try:
                current = dict(frame.f_locals)
            except Exception:
                current = {}
            # Filter dunders
            changes = {k: v for k, v in current.items() if not k.startswith('__')}
            self.prev_locals[fid] = current
            self.writer.write_event(1, frame.f_lineno, code_hash, changes)

        elif event == 'line':
            fid = id(frame)
            try:
                current = dict(frame.f_locals)
            except Exception:
                current = {}
            prev = self.prev_locals.get(fid, {})
            changes = {}
            for k, v in current.items():
                if k.startswith('__'):
                    continue
                if k not in prev or prev[k] is not v:
                    changes[k] = v
            self.prev_locals[fid] = current
            self.writer.write_event(2, frame.f_lineno, code_hash, changes)

        elif event == 'return':
            fid = id(frame)
            self.prev_locals.pop(fid, None)
            self.writer.write_event(3, frame.f_lineno, code_hash, {})

        # Check time limit periodically
        if self.writer.events_written % 10000 == 0:
            if time.monotonic() - self.start_time > TIME_LIMIT_S:
                self.time_limit_reached = True
                return None

        return self.trace_func

    def start(self):
        self.start_time = time.monotonic()
        self.time_limit_reached = False
        sys.settrace(self.trace_func)

    def stop(self):
        sys.settrace(None)


# ============================================================================
# Test A: Heavy computation, small variables
# ============================================================================

def stress_compute():
    """Tight numerical computation. Many lines, small variables."""

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


# ============================================================================
# Test B: Large objects
# ============================================================================

def stress_large_objects():
    """Manipulates large data structures. Fewer events but bigger variables."""

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

    # Build — large dataset, repeated to generate sustained load
    total_ranked = 0
    for iteration in range(5):
        data = build_dataset(10000)

        # Filter
        even_ids = filter_dataset(data, lambda r: r['id'] % 2 == 0)

        # Transform
        transformed = transform_dataset(even_ids)

        # Aggregate
        buckets = aggregate(transformed, 'id')

        # Sort each bucket
        for bucket_id, items in buckets.items():
            ranked = sort_and_rank(items, 'total')
            total_ranked += len(ranked)

    return len(data), len(even_ids), len(transformed), len(buckets), total_ranked


# ============================================================================
# Runner
# ============================================================================

def run_stress_test(name, workload_fn, tmpdir):
    print(f"\n{'='*70}")
    print(f"STRESS TEST: {name}")
    print(f"Limits: {TIME_LIMIT_S}s or {SIZE_LIMIT_BYTES/1024/1024/1024:.0f}GB")
    print(f"{'='*70}")

    # --- Baseline (no tracing) ---
    print("\n  Running baseline (no tracing)...")
    start = time.monotonic()
    baseline_result = workload_fn()
    baseline_time = time.monotonic() - start
    print(f"  Baseline: {baseline_time:.2f}s, result={baseline_result}")

    # --- Traced with disk write ---
    trace_path = os.path.join(tmpdir, f'{name}.trace')
    writer = BinaryTraceWriter(trace_path, flush_interval=5000)
    tracer = StressTracer(writer)

    print(f"\n  Running with tracing + disk write...")
    start = time.monotonic()
    tracer.start()
    traced_result = workload_fn()
    tracer.stop()
    traced_time = time.monotonic() - start
    writer.close()

    file_size = os.path.getsize(trace_path)
    events = writer.events_written

    # --- Report ---
    overhead = traced_time / baseline_time if baseline_time > 0 else float('inf')
    events_per_sec = events / traced_time if traced_time > 0 else 0
    mb_per_sec = file_size / traced_time / 1024 / 1024 if traced_time > 0 else 0
    bytes_per_event = file_size / events if events > 0 else 0

    stopped_reason = ""
    if tracer.time_limit_reached:
        stopped_reason = " (TIME LIMIT)"
    elif writer.limit_reached:
        stopped_reason = " (SIZE LIMIT)"

    print(f"\n  Results{stopped_reason}:")
    print(f"    Baseline time:   {baseline_time:>10.2f}s")
    print(f"    Traced time:     {traced_time:>10.2f}s")
    print(f"    Overhead:        {overhead:>10.1f}x")
    print(f"    Result correct:  {traced_result == baseline_result}")
    print(f"    Events captured: {events:>12,}")
    print(f"    Events/sec:      {events_per_sec:>12,.0f}")
    print(f"    Trace file size: {file_size:>12,} bytes ({file_size/1024/1024:.1f} MB)")
    print(f"    Data rate:       {mb_per_sec:>10.1f} MB/sec")
    print(f"    Bytes/event:     {bytes_per_event:>10.1f}")

    # Extrapolation
    if events_per_sec > 0:
        hour_events = events_per_sec * 3600
        hour_size_gb = mb_per_sec * 3600 / 1024
        print(f"\n  Extrapolation (1 hour):")
        print(f"    Events:          {hour_events:>12,.0f}")
        print(f"    Data:            {hour_size_gb:>10.1f} GB")

    # Clean up
    os.unlink(trace_path)

    return {
        'baseline_time': baseline_time,
        'traced_time': traced_time,
        'overhead': overhead,
        'events': events,
        'events_per_sec': events_per_sec,
        'file_size': file_size,
        'mb_per_sec': mb_per_sec,
        'bytes_per_event': bytes_per_event,
    }


def main():
    import tempfile
    tmpdir = tempfile.mkdtemp(prefix='trace_stress_')

    print(f"Trace output directory: {tmpdir}")

    results = {}
    results['compute'] = run_stress_test('compute_heavy', stress_compute, tmpdir)
    results['large_obj'] = run_stress_test('large_objects', stress_large_objects, tmpdir)

    # Summary
    print(f"\n{'='*70}")
    print("SUMMARY")
    print(f"{'='*70}")
    print(f"{'Test':<15} {'Overhead':>8} {'Events/s':>12} {'MB/s':>8} {'B/evt':>8} {'File':>10}")
    print("-" * 63)
    for name, r in results.items():
        print(f"{name:<15} {r['overhead']:>7.1f}x {r['events_per_sec']:>12,.0f} "
              f"{r['mb_per_sec']:>7.1f} {r['bytes_per_event']:>7.1f} "
              f"{r['file_size']/1024/1024:>8.1f}MB")

    os.rmdir(tmpdir)


if __name__ == '__main__':
    main()
