"""
Experiment 12: Serialization and Disk I/O Costs

Measures the cost of getting captured trace data to disk.
Tests multiple strategies:

1. Inline synchronous write (serialize + write in the trace callback)
2. Batched synchronous write (accumulate N events, flush periodically)
3. Background writer thread with bounded queue (backpressure via queue.put with maxsize)
4. Background writer with ring buffer + memory-mapped file
5. Binary format vs JSON vs pickle

Also measures:
- Raw serialization throughput (bytes/sec for different formats)
- Raw disk write throughput
- Queue put/get overhead
- Impact of backpressure (what happens when writer falls behind)
"""
import sys
import os
import time
import struct
import json
import pickle
import queue
import threading
import tempfile
import mmap
import statistics

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# ============================================================================
# Synthetic trace event data
# ============================================================================

def make_event_batch(n):
    """Create a batch of realistic trace events."""
    events = []
    for i in range(n):
        events.append({
            'type': 'line',
            'code_id': 0x7f0000000000 + (i % 50) * 0x1000,
            'line': 100 + (i % 200),
            'timestamp': 1000000000 + i * 500,
            'changes': [
                (0, 'int', i * 3),
                (2, 'str', f'value_{i % 100}'),
            ] if i % 3 == 0 else [],
        })
    return events


# ============================================================================
# Serialization formats
# ============================================================================

def serialize_binary(events):
    """Pack events into a compact binary format.

    Header per event: type(1) + code_id(8) + line(4) + timestamp(8) + n_changes(1) = 22 bytes
    Per change: var_idx(2) + type_tag(1) + value(8) = 11 bytes
    """
    parts = []
    for evt in events:
        n_changes = len(evt['changes'])
        parts.append(struct.pack('<BQiQB',
            0x01,  # event type
            evt['code_id'],
            evt['line'],
            evt['timestamp'],
            n_changes,
        ))
        for var_idx, type_tag, value in evt['changes']:
            tag = 1 if type_tag == 'int' else 4
            if isinstance(value, int):
                parts.append(struct.pack('<HBq', var_idx, tag, value))
            else:
                # For strings, just store length + truncated hash
                parts.append(struct.pack('<HBq', var_idx, tag, hash(value) & 0x7FFFFFFFFFFFFFFF))
    return b''.join(parts)


def serialize_binary_prealloc(events):
    """Binary format with pre-allocated buffer (avoids repeated allocation)."""
    # Pre-calculate size
    size = 0
    for evt in events:
        size += 22 + 11 * len(evt['changes'])

    buf = bytearray(size)
    offset = 0
    for evt in events:
        n_changes = len(evt['changes'])
        struct.pack_into('<BQiQB', buf, offset,
            0x01, evt['code_id'], evt['line'], evt['timestamp'], n_changes)
        offset += 22
        for var_idx, type_tag, value in evt['changes']:
            tag = 1 if type_tag == 'int' else 4
            val = value if isinstance(value, int) else (hash(value) & 0x7FFFFFFFFFFFFFFF)
            struct.pack_into('<HBq', buf, offset, var_idx, tag, val)
            offset += 11
    return bytes(buf)


def serialize_json(events):
    """JSON serialization."""
    return json.dumps(events).encode()


def serialize_pickle(events):
    """Pickle serialization."""
    return pickle.dumps(events, protocol=pickle.HIGHEST_PROTOCOL)


def serialize_json_lines(events):
    """JSON Lines format (one JSON object per line)."""
    return b'\n'.join(json.dumps(e).encode() for e in events)


# ============================================================================
# Throughput measurements
# ============================================================================

def measure_serialization_throughput():
    """Measure raw serialization speed for different formats."""
    print("=== Serialization throughput ===\n")

    batch_sizes = [100, 1000, 10000]
    formats = [
        ('binary', serialize_binary),
        ('binary_prealloc', serialize_binary_prealloc),
        ('json', serialize_json),
        ('json_lines', serialize_json_lines),
        ('pickle', serialize_pickle),
    ]

    for batch_size in batch_sizes:
        events = make_event_batch(batch_size)
        print(f"Batch size: {batch_size}")
        print(f"  {'Format':<20} {'Time (us)':>10} {'Size (bytes)':>12} "
              f"{'Events/sec':>12} {'MB/sec':>10}")
        print(f"  {'-'*66}")

        for name, serialize_fn in formats:
            # Warmup
            for _ in range(3):
                serialize_fn(events)

            times = []
            for _ in range(20):
                start = time.perf_counter_ns()
                data = serialize_fn(events)
                elapsed = time.perf_counter_ns() - start
                times.append(elapsed)

            med = statistics.median(times)
            size = len(data)
            events_per_sec = batch_size / (med / 1e9)
            mb_per_sec = size / (med / 1e9) / 1024 / 1024

            print(f"  {name:<20} {med/1000:>10.0f} {size:>12,} "
                  f"{events_per_sec:>12,.0f} {mb_per_sec:>10.1f}")
        print()


def measure_disk_write_throughput():
    """Measure raw disk write speed."""
    print("=== Disk write throughput ===\n")

    tmpdir = tempfile.mkdtemp(prefix='trace_io_')
    filepath = os.path.join(tmpdir, 'trace.bin')

    sizes = [1024, 10*1024, 100*1024, 1024*1024, 10*1024*1024]

    print(f"  {'Write size':<15} {'Time (us)':>10} {'MB/sec':>10} {'Method':<20}")
    print(f"  {'-'*57}")

    for size in sizes:
        data = b'\x00' * size

        # os.write (unbuffered)
        times = []
        for _ in range(10):
            fd = os.open(filepath, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
            start = time.perf_counter_ns()
            os.write(fd, data)
            os.close(fd)
            elapsed = time.perf_counter_ns() - start
            times.append(elapsed)
        med = statistics.median(times)
        mb = size / 1024 / 1024
        mb_per_sec = mb / (med / 1e9)
        print(f"  {size:>10,}B  {med/1000:>10.0f} {mb_per_sec:>10.1f} os.write")

        # Python file write (buffered)
        times = []
        for _ in range(10):
            start = time.perf_counter_ns()
            with open(filepath, 'wb') as f:
                f.write(data)
            elapsed = time.perf_counter_ns() - start
            times.append(elapsed)
        med = statistics.median(times)
        mb_per_sec = mb / (med / 1e9)
        print(f"  {size:>10,}B  {med/1000:>10.0f} {mb_per_sec:>10.1f} file.write")

    os.unlink(filepath)
    os.rmdir(tmpdir)
    print()


def measure_queue_overhead():
    """Measure queue.put/get overhead for background writer pattern."""
    print("=== Queue overhead ===\n")

    for maxsize in [0, 1000, 10000, 100000]:
        q = queue.Queue(maxsize=maxsize if maxsize > 0 else 0)
        label = f"maxsize={maxsize}" if maxsize > 0 else "unbounded"

        item = {'type': 'line', 'code_id': 123, 'line': 42, 'data': b'x' * 100}
        n = min(100000, maxsize if maxsize > 0 else 100000)

        # Measure put
        start = time.perf_counter_ns()
        for _ in range(n):
            q.put_nowait(item)
        put_elapsed = time.perf_counter_ns() - start

        # Measure get
        start = time.perf_counter_ns()
        for _ in range(n):
            q.get_nowait()
        get_elapsed = time.perf_counter_ns() - start

        put_ns = put_elapsed / n
        get_ns = get_elapsed / n
        print(f"  {label:<20} put: {put_ns:>6.0f}ns  get: {get_ns:>6.0f}ns  "
              f"total: {put_ns+get_ns:>7.0f}ns/event")
    print()


# ============================================================================
# End-to-end write strategies
# ============================================================================

def measure_write_strategies():
    """Measure end-to-end: generate events → serialize → write to disk."""
    print("=== End-to-end write strategies ===\n")

    tmpdir = tempfile.mkdtemp(prefix='trace_strat_')
    n_events = 100000
    events = make_event_batch(n_events)

    strategies = []

    # Strategy 1: Synchronous, serialize all then write
    def strategy_sync_bulk():
        filepath = os.path.join(tmpdir, 'sync_bulk.bin')
        data = serialize_binary_prealloc(events)
        with open(filepath, 'wb') as f:
            f.write(data)
        return filepath

    strategies.append(('sync_bulk', strategy_sync_bulk))

    # Strategy 2: Synchronous, batched (1000 events per flush)
    def strategy_sync_batched():
        filepath = os.path.join(tmpdir, 'sync_batch.bin')
        batch_size = 1000
        with open(filepath, 'wb') as f:
            for i in range(0, len(events), batch_size):
                batch = events[i:i+batch_size]
                data = serialize_binary_prealloc(batch)
                f.write(data)
        return filepath

    strategies.append(('sync_batch_1k', strategy_sync_batched))

    # Strategy 3: Synchronous, per-event (worst case)
    def strategy_sync_per_event():
        filepath = os.path.join(tmpdir, 'sync_per.bin')
        with open(filepath, 'wb') as f:
            for evt in events[:10000]:  # only 10k to avoid being too slow
                data = serialize_binary_prealloc([evt])
                f.write(data)
        return filepath

    strategies.append(('sync_per_event(10k)', strategy_sync_per_event))

    # Strategy 4: Background thread with bounded queue
    def strategy_bg_thread():
        filepath = os.path.join(tmpdir, 'bg_thread.bin')
        q = queue.Queue(maxsize=10000)
        done = threading.Event()

        def writer():
            with open(filepath, 'wb') as f:
                while not done.is_set() or not q.empty():
                    batch = []
                    try:
                        while len(batch) < 1000:
                            batch.append(q.get(timeout=0.001))
                    except queue.Empty:
                        pass
                    if batch:
                        data = serialize_binary_prealloc(batch)
                        f.write(data)

        t = threading.Thread(target=writer)
        t.start()

        # Producer: enqueue events
        for evt in events:
            q.put(evt)
        done.set()
        t.join()
        return filepath

    strategies.append(('bg_thread_q10k', strategy_bg_thread))

    # Strategy 5: Background thread, smaller queue (more backpressure)
    def strategy_bg_thread_small_q():
        filepath = os.path.join(tmpdir, 'bg_small.bin')
        q = queue.Queue(maxsize=1000)
        done = threading.Event()

        def writer():
            with open(filepath, 'wb') as f:
                while not done.is_set() or not q.empty():
                    batch = []
                    try:
                        while len(batch) < 500:
                            batch.append(q.get(timeout=0.001))
                    except queue.Empty:
                        pass
                    if batch:
                        data = serialize_binary_prealloc(batch)
                        f.write(data)

        t = threading.Thread(target=writer)
        t.start()
        for evt in events:
            q.put(evt)  # will block when queue is full (backpressure)
        done.set()
        t.join()
        return filepath

    strategies.append(('bg_thread_q1k', strategy_bg_thread_small_q))

    # Strategy 6: Pickle bulk (simple baseline)
    def strategy_pickle_bulk():
        filepath = os.path.join(tmpdir, 'pickle_bulk.pkl')
        with open(filepath, 'wb') as f:
            pickle.dump(events, f, protocol=pickle.HIGHEST_PROTOCOL)
        return filepath

    strategies.append(('pickle_bulk', strategy_pickle_bulk))

    print(f"Events: {n_events:,}")
    print(f"  {'Strategy':<25} {'Time (ms)':>10} {'Events/sec':>12} "
          f"{'File size':>12} {'Bytes/evt':>10}")
    print(f"  {'-'*71}")

    for name, fn in strategies:
        # Warmup
        fp = fn()
        if os.path.exists(fp):
            os.unlink(fp)

        times = []
        for _ in range(5):
            start = time.perf_counter_ns()
            fp = fn()
            elapsed = time.perf_counter_ns() - start
            fsize = os.path.getsize(fp)
            os.unlink(fp)
            times.append((elapsed, fsize))

        med_time = statistics.median([t for t, _ in times])
        fsize = times[0][1]
        n = n_events if '10k' not in name else 10000
        events_per_sec = n / (med_time / 1e9)
        bytes_per_evt = fsize / max(n, 1)

        print(f"  {name:<25} {med_time/1e6:>10.1f} {events_per_sec:>12,.0f} "
              f"{fsize:>12,} {bytes_per_evt:>10.1f}")

    # Cleanup
    for f in os.listdir(tmpdir):
        os.unlink(os.path.join(tmpdir, f))
    os.rmdir(tmpdir)
    print()


# ============================================================================
# Context: how does serialization compare to capture overhead?
# ============================================================================

def measure_capture_vs_io_budget():
    """Put serialization costs in context with capture overhead."""
    print("=== Capture vs I/O budget analysis ===\n")

    # From experiments: C selective captures ~4M events over all workloads
    # at 2.74x overhead. If baseline is ~100ms total, traced is ~274ms,
    # so capture overhead is ~174ms for ~4M events = ~43ns/event.
    #
    # What's the serialization budget?

    events = make_event_batch(10000)

    # Binary serialization cost per event
    times = []
    for _ in range(20):
        start = time.perf_counter_ns()
        serialize_binary_prealloc(events)
        elapsed = time.perf_counter_ns() - start
        times.append(elapsed)
    binary_ns_per_event = statistics.median(times) / len(events)

    # Queue put cost per event
    q = queue.Queue(maxsize=100000)
    start = time.perf_counter_ns()
    for _ in range(100000):
        q.put_nowait(None)
    q_put_ns = (time.perf_counter_ns() - start) / 100000

    print(f"  Per-event costs:")
    print(f"    C selective capture overhead:  ~40-100ns (from exp11)")
    print(f"    Binary serialization:          {binary_ns_per_event:>6.0f}ns")
    print(f"    Queue put (unbounded):         {q_put_ns:>6.0f}ns")
    print(f"    Total (capture + serialize + queue): ~{40 + binary_ns_per_event + q_put_ns:.0f}ns")
    print()
    print(f"  At 1M events/sec:")
    binary_size = len(serialize_binary_prealloc(events)) / len(events)
    print(f"    Data rate: {binary_size * 1e6 / 1024/1024:.1f} MB/sec (binary)")
    print(f"    Typical SSD write: 500-2000 MB/sec")
    print(f"    Headroom: {500 / (binary_size * 1e6 / 1024/1024):.0f}-{2000 / (binary_size * 1e6 / 1024/1024):.0f}x")
    print()

    # Background thread analysis
    print(f"  Background thread feasibility:")
    print(f"    If writer batches 1000 events:")
    batch = events[:1000]
    start = time.perf_counter_ns()
    for _ in range(100):
        data = serialize_binary_prealloc(batch)
    batch_serialize_ns = (time.perf_counter_ns() - start) / 100

    tmpdir = tempfile.mkdtemp()
    fp = os.path.join(tmpdir, 'test.bin')
    data = serialize_binary_prealloc(batch)
    start = time.perf_counter_ns()
    for _ in range(100):
        with open(fp, 'ab') as f:
            f.write(data)
    batch_write_ns = (time.perf_counter_ns() - start) / 100
    os.unlink(fp)
    os.rmdir(tmpdir)

    batch_total_ns = batch_serialize_ns + batch_write_ns
    max_events_per_sec = 1000 / (batch_total_ns / 1e9)

    print(f"      Serialize 1000 events: {batch_serialize_ns/1000:.0f}us")
    print(f"      Write to disk:         {batch_write_ns/1000:.0f}us")
    print(f"      Total per batch:       {batch_total_ns/1000:.0f}us")
    print(f"      Max throughput:        {max_events_per_sec:,.0f} events/sec")
    print(f"      At 1M events/sec:      writer {'CAN' if max_events_per_sec > 1e6 else 'CANNOT'} keep up")
    print(f"      At 4M events/sec:      writer {'CAN' if max_events_per_sec > 4e6 else 'CANNOT'} keep up")


def main():
    measure_serialization_throughput()
    measure_disk_write_throughput()
    measure_queue_overhead()
    measure_write_strategies()
    measure_capture_vs_io_budget()


if __name__ == '__main__':
    main()
