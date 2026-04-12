"""
Larger, more realistic workloads for trace capture experiments.

Categories:
  1. Compute-intensive: matrix operations, sorting, hashing
  2. IO-intensive: file read/write, directory traversal
  3. Memory-intensive: large data structure manipulation
  4. Async/yield-heavy: simulated network calls with long waits
"""
import time
import os
import tempfile
import hashlib
import json
import random
import asyncio


# ============================================================================
# 1. COMPUTE-INTENSIVE
# ============================================================================

def compute_matrix_multiply(size=50):
    """Naive matrix multiplication — lots of arithmetic, deep loops."""
    A = [[random.random() for _ in range(size)] for _ in range(size)]
    B = [[random.random() for _ in range(size)] for _ in range(size)]
    C = [[0.0] * size for _ in range(size)]
    for i in range(size):
        for j in range(size):
            s = 0.0
            for k in range(size):
                s += A[i][k] * B[k][j]
            C[i][j] = s
    return C[0][0]


def compute_mergesort(n=5000):
    """Recursive mergesort — deep call tree with moderate work per call."""
    data = [random.randint(0, 1_000_000) for _ in range(n)]

    def merge(left, right):
        result = []
        i = j = 0
        while i < len(left) and j < len(right):
            if left[i] <= right[j]:
                result.append(left[i])
                i += 1
            else:
                result.append(right[j])
                j += 1
        result.extend(left[i:])
        result.extend(right[j:])
        return result

    def mergesort(arr):
        if len(arr) <= 1:
            return arr
        mid = len(arr) // 2
        left = mergesort(arr[:mid])
        right = mergesort(arr[mid:])
        return merge(left, right)

    sorted_data = mergesort(data)
    return len(sorted_data)


def compute_hash_chain(n=10000):
    """Chain of SHA-256 hashes — moderate compute, small state."""
    h = b'seed'
    for i in range(n):
        h = hashlib.sha256(h + str(i).encode()).digest()
    return h.hex()[:16]


def compute_prime_sieve(limit=10000):
    """Sieve of Eratosthenes — array manipulation with conditional writes."""
    sieve = [True] * (limit + 1)
    sieve[0] = sieve[1] = False
    for i in range(2, int(limit**0.5) + 1):
        if sieve[i]:
            for j in range(i*i, limit + 1, i):
                sieve[j] = False
    primes = [i for i, is_prime in enumerate(sieve) if is_prime]
    return len(primes)


# ============================================================================
# 2. IO-INTENSIVE
# ============================================================================

def io_file_write_read(n_files=100, size_per_file=1000):
    """Write and read many small files."""
    tmpdir = tempfile.mkdtemp(prefix='trace_exp_')
    try:
        # Write phase
        for i in range(n_files):
            path = os.path.join(tmpdir, f'file_{i:04d}.txt')
            with open(path, 'w') as f:
                for j in range(size_per_file // 50):
                    f.write(f'line {j}: data={j*i} hash={hash((i,j))}\n')

        # Read phase
        total_lines = 0
        for i in range(n_files):
            path = os.path.join(tmpdir, f'file_{i:04d}.txt')
            with open(path, 'r') as f:
                for line in f:
                    total_lines += 1
        return total_lines
    finally:
        for fname in os.listdir(tmpdir):
            os.remove(os.path.join(tmpdir, fname))
        os.rmdir(tmpdir)


def io_json_serialize(n_records=2000):
    """Serialize and deserialize JSON — mixed IO and compute."""
    records = []
    for i in range(n_records):
        records.append({
            'id': i,
            'name': f'user_{i}',
            'email': f'user_{i}@example.com',
            'scores': [random.randint(0, 100) for _ in range(10)],
            'metadata': {
                'created': '2024-01-01',
                'active': i % 3 != 0,
                'tags': [f'tag_{j}' for j in range(5)],
            }
        })

    # Serialize
    serialized = json.dumps(records)

    # Deserialize
    deserialized = json.loads(serialized)

    # Process
    active_count = sum(1 for r in deserialized if r['metadata']['active'])
    return active_count


def io_tempfile_ops(n_ops=200):
    """Create, write, read, delete temp files — syscall-heavy."""
    results = []
    for i in range(n_ops):
        fd, path = tempfile.mkstemp(prefix='trace_')
        try:
            os.write(fd, f'data_{i}\n'.encode())
            os.close(fd)
            with open(path, 'r') as f:
                content = f.read()
            results.append(len(content))
        finally:
            os.unlink(path)
    return sum(results)


# ============================================================================
# 3. MEMORY-INTENSIVE
# ============================================================================

def memory_dict_heavy(n=50000):
    """Build and query large dictionaries."""
    # Build
    data = {}
    for i in range(n):
        key = f'key_{i:06d}'
        data[key] = {
            'value': i * 3.14,
            'tags': [f't{j}' for j in range(i % 5)],
            'active': i % 2 == 0,
        }

    # Query
    total = 0.0
    for i in range(0, n, 3):
        key = f'key_{i:06d}'
        if key in data and data[key]['active']:
            total += data[key]['value']

    # Update
    for i in range(0, n, 7):
        key = f'key_{i:06d}'
        if key in data:
            data[key]['value'] *= 2
            data[key]['tags'].append('updated')

    return total


def memory_list_operations(n=100000):
    """Heavy list manipulation — appends, slicing, sorting."""
    items = []
    for i in range(n):
        items.append(i * 7 % 1000)

    # Sort
    items.sort()

    # Filter into new list
    filtered = [x for x in items if x > 500]

    # Chunk and process
    chunk_size = 100
    chunk_sums = []
    for i in range(0, len(filtered), chunk_size):
        chunk = filtered[i:i+chunk_size]
        chunk_sums.append(sum(chunk))

    return sum(chunk_sums)


def memory_nested_structures(depth=6, breadth=4):
    """Build deeply nested dicts/lists — lots of object creation."""
    def build_tree(d, b):
        if d == 0:
            return {'leaf': True, 'value': random.random()}
        children = {}
        for i in range(b):
            children[f'child_{i}'] = build_tree(d - 1, b)
        return {'leaf': False, 'depth': d, 'children': children}

    tree = build_tree(depth, breadth)

    # Walk the tree
    def count_leaves(node):
        if node['leaf']:
            return 1
        total = 0
        for child in node['children'].values():
            total += count_leaves(child)
        return total

    return count_leaves(tree)


def memory_class_instances(n=20000):
    """Create many class instances — tests object allocation patterns."""
    class Node:
        __slots__ = ('value', 'left', 'right', 'parent', 'depth')
        def __init__(self, value, parent=None, depth=0):
            self.value = value
            self.left = None
            self.right = None
            self.parent = parent
            self.depth = depth

    # Build BST
    root = Node(n // 2)
    for i in range(n):
        val = random.randint(0, n * 2)
        node = root
        depth = 0
        while True:
            depth += 1
            if val < node.value:
                if node.left is None:
                    node.left = Node(val, parent=node, depth=depth)
                    break
                node = node.left
            else:
                if node.right is None:
                    node.right = Node(val, parent=node, depth=depth)
                    break
                node = node.right

    # Walk and count
    def count(node):
        if node is None:
            return 0
        return 1 + count(node.left) + count(node.right)

    return count(root)


# ============================================================================
# 4. ASYNC / YIELD-HEAVY
# ============================================================================

def yield_pipeline(n=10000):
    """Generator pipeline — many yields with minimal compute between them."""
    def producer(n):
        for i in range(n):
            yield i

    def mapper(source):
        for item in source:
            yield item * 2 + 1

    def filterer(source):
        for item in source:
            if item % 3 != 0:
                yield item

    def batcher(source, size=10):
        batch = []
        for item in source:
            batch.append(item)
            if len(batch) >= size:
                yield batch
                batch = []
        if batch:
            yield batch

    pipeline = batcher(filterer(mapper(producer(n))))
    total = 0
    for batch in pipeline:
        total += sum(batch)
    return total


def yield_coroutine_sim(n_tasks=100, steps_per_task=20):
    """Simulate coroutine-style cooperative multitasking with generators."""
    def task(task_id, steps):
        state = 0
        for step in range(steps):
            state += task_id * step
            yield state  # "yield to scheduler"
        return state

    # Round-robin scheduler
    tasks = [task(i, steps_per_task) for i in range(n_tasks)]
    results = [None] * n_tasks
    active = list(range(n_tasks))

    while active:
        next_active = []
        for idx in active:
            try:
                results[idx] = next(tasks[idx])
                next_active.append(idx)
            except StopIteration as e:
                pass
        active = next_active

    return sum(r for r in results if r is not None)


async def async_simulated_network(n_requests=50, latency_ms=1):
    """Async tasks with simulated network latency (asyncio.sleep)."""
    async def fetch(url_id):
        # Simulate network latency
        await asyncio.sleep(latency_ms / 1000.0)
        # Simulate processing response
        data = {'id': url_id, 'payload': f'response_{url_id}' * 10}
        processed = len(json.dumps(data))
        return processed

    async def batch_fetch(ids):
        tasks = [fetch(i) for i in ids]
        return await asyncio.gather(*tasks)

    # Run in batches
    total = 0
    batch_size = 10
    for start in range(0, n_requests, batch_size):
        batch_ids = range(start, min(start + batch_size, n_requests))
        results = await batch_fetch(batch_ids)
        total += sum(results)
    return total


def async_network_sync_wrapper(n_requests=50, latency_ms=1):
    """Sync wrapper for the async workload."""
    return asyncio.run(async_simulated_network(n_requests, latency_ms))


async def async_producer_consumer(n_items=500, n_producers=3, n_consumers=3):
    """Async producer-consumer with queues."""
    queue = asyncio.Queue(maxsize=20)
    results = []

    async def producer(pid, count):
        for i in range(count):
            item = {'producer': pid, 'item': i, 'value': pid * 1000 + i}
            await queue.put(item)
            # Simulate variable production rate
            if i % 10 == 0:
                await asyncio.sleep(0.0001)

    async def consumer(cid):
        consumed = 0
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=0.05)
                # Process item
                result = item['value'] * 2
                consumed += 1
                queue.task_done()
            except asyncio.TimeoutError:
                break
        return consumed

    items_per_producer = n_items // n_producers

    producers = [asyncio.create_task(producer(i, items_per_producer))
                 for i in range(n_producers)]
    consumers = [asyncio.create_task(consumer(i))
                 for i in range(n_consumers)]

    await asyncio.gather(*producers)
    await queue.join()

    for c in consumers:
        c.cancel()

    consumed = sum(c.result() for c in consumers if not c.cancelled()
                   and c.done() and not c.exception())
    return consumed


def async_producer_consumer_wrapper(n_items=500):
    """Sync wrapper for async producer-consumer."""
    try:
        return asyncio.run(async_producer_consumer(n_items))
    except Exception:
        return 0


# ============================================================================
# WORKLOAD REGISTRY
# ============================================================================

LARGE_WORKLOADS = {
    # Compute-intensive
    'comp_matmul': lambda: compute_matrix_multiply(50),
    'comp_mergesort': lambda: compute_mergesort(5000),
    'comp_hash': lambda: compute_hash_chain(10000),
    'comp_primes': lambda: compute_prime_sieve(10000),

    # IO-intensive
    'io_files': lambda: io_file_write_read(100, 1000),
    'io_json': lambda: io_json_serialize(2000),
    'io_tempfiles': lambda: io_tempfile_ops(200),

    # Memory-intensive
    'mem_dict': lambda: memory_dict_heavy(50000),
    'mem_list': lambda: memory_list_operations(100000),
    'mem_nested': lambda: memory_nested_structures(6, 4),
    'mem_classes': lambda: memory_class_instances(20000),

    # Async / yield-heavy
    'yield_pipeline': lambda: yield_pipeline(10000),
    'yield_coroutines': lambda: yield_coroutine_sim(100, 20),
    'async_network': lambda: async_network_sync_wrapper(50, 1),
    'async_prodcons': lambda: async_producer_consumer_wrapper(500),
}

# Subset for quicker iteration
QUICK_WORKLOADS = {k: v for k, v in LARGE_WORKLOADS.items()
                   if k not in ('async_network',)}  # async_network is slow by design


def run_workload(name, iterations=1):
    """Run a workload and return (total_time_ns, per_iter_ns)."""
    workload = LARGE_WORKLOADS[name]
    # Warmup
    workload()

    start = time.perf_counter_ns()
    for _ in range(iterations):
        workload()
    elapsed = time.perf_counter_ns() - start

    return elapsed, elapsed // max(iterations, 1)


if __name__ == '__main__':
    print("=== Large Workload Baselines ===")
    print(f"{'Workload':<20} {'Time (ms)':>12} {'Category':<15}")
    print("-" * 50)

    categories = {
        'comp_': 'compute',
        'io_': 'io',
        'mem_': 'memory',
        'yield_': 'yield',
        'async_': 'async',
    }

    for name in LARGE_WORKLOADS:
        cat = next((v for k, v in categories.items() if name.startswith(k)), '?')
        total, per_iter = run_workload(name, iterations=1)
        print(f"{name:<20} {per_iter/1_000_000:>10.1f}ms {cat:<15}")
