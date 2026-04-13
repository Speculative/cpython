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
# 5. REAL-WORLD PATTERNS
# ============================================================================

import re
import functools

def pattern_exceptions(n=5000):
    """Exception handling in hot paths — common in web frameworks, parsers."""
    data = [{'key': i} if i % 3 != 0 else {'other': i} for i in range(n)]
    total = 0
    for item in data:
        try:
            total += item['key']
        except KeyError:
            try:
                total += item['other'] * 2
            except KeyError:
                pass
    return total


def pattern_string_processing(n=2000):
    """String manipulation: regex, formatting, parsing — very common."""
    pattern = re.compile(r'(\w+)=(\d+)')
    lines = [f'item_{i}={i * 7 % 1000} status={"ok" if i % 5 else "err"} count={i}'
             for i in range(n)]

    results = []
    for line in lines:
        matches = pattern.findall(line)
        record = {}
        for key, val in matches:
            record[key] = int(val)
        name = line.split(' ')[0]
        record['name'] = name.upper().replace('_', '-')
        record['summary'] = f"{record.get('name', '?')}: {sum(record.get(k, 0) for k in record if k != 'name' and k != 'summary')}"
        results.append(record)
    return len(results)


def pattern_comprehensions(n=5000):
    """List/dict/set comprehensions — extremely common Python idiom."""
    data = list(range(n))

    # List comprehension with filter
    evens = [x * 2 for x in data if x % 2 == 0]

    # Nested comprehension
    matrix = [[i * j for j in range(20)] for i in range(20)]

    # Dict comprehension
    index = {f'k{x}': x ** 2 for x in data if x % 7 == 0}

    # Set comprehension
    unique_mods = {x % 97 for x in data}

    # Generator expression consumed by sum
    total = sum(x * x for x in evens if x < n)

    return total + len(index) + len(unique_mods) + sum(sum(row) for row in matrix)


def pattern_closures_decorators(n=2000):
    """Closures, decorators, higher-order functions — framework patterns."""
    def make_validator(min_val, max_val):
        def validator(x):
            if x < min_val:
                return min_val
            if x > max_val:
                return max_val
            return x
        return validator

    def memoize(func):
        cache = {}
        @functools.wraps(func)
        def wrapper(*args):
            if args not in cache:
                cache[args] = func(*args)
            return cache[args]
        return wrapper

    @memoize
    def fib(n):
        if n < 2:
            return n
        return fib(n - 1) + fib(n - 2)

    def apply_pipeline(value, *transforms):
        for fn in transforms:
            value = fn(value)
        return value

    clamp = make_validator(0, 100)
    double = lambda x: x * 2
    offset = lambda x: x + 10

    total = 0
    for i in range(n):
        total += apply_pipeline(i % 200, clamp, double, offset)
        if i < 80:
            total += fib(i)
    return total


def pattern_class_hierarchy(n=3000):
    """Inheritance, super(), method resolution — OOP-heavy code."""
    class Base:
        def __init__(self, value):
            self.value = value
            self._cache = None

        def process(self):
            return self.value * 2

        @property
        def cached_result(self):
            if self._cache is None:
                self._cache = self.process()
            return self._cache

    class Middle(Base):
        def __init__(self, value, factor):
            super().__init__(value)
            self.factor = factor

        def process(self):
            base = super().process()
            return base * self.factor

    class Leaf(Middle):
        def __init__(self, value, factor, label):
            super().__init__(value, factor)
            self.label = label
            self.history = []

        def process(self):
            result = super().process()
            self.history.append(result)
            return result + len(self.label)

    total = 0
    objects = []
    for i in range(n):
        if i % 3 == 0:
            obj = Base(i)
        elif i % 3 == 1:
            obj = Middle(i, 1.5)
        else:
            obj = Leaf(i, 1.5, f'item_{i}')
        objects.append(obj)
        total += obj.cached_result

    # Access properties again (should hit cache)
    for obj in objects:
        total += obj.cached_result

    return total


def pattern_context_managers(n=3000):
    """Context managers — with statements, __enter__/__exit__."""
    class Counter:
        def __init__(self):
            self.count = 0
            self.depth = 0
            self.max_depth = 0

        def __enter__(self):
            self.depth += 1
            if self.depth > self.max_depth:
                self.max_depth = self.depth
            return self

        def __exit__(self, *args):
            self.depth -= 1
            self.count += 1
            return False

    class Accumulator:
        def __init__(self):
            self.total = 0
            self.items = []

        def __enter__(self):
            self.items = []
            return self

        def __exit__(self, *args):
            self.total += sum(self.items)
            return False

    counter = Counter()
    acc = Accumulator()
    total = 0

    for i in range(n):
        with counter:
            with acc:
                acc.items.append(i)
                if i % 10 == 0:
                    with counter:
                        acc.items.append(i * 2)
            total += acc.total

    return total + counter.count + counter.max_depth


def pattern_kwargs_unpacking(n=3000):
    """*args/**kwargs, dict unpacking — common in API/framework code."""
    def make_config(**kwargs):
        defaults = {'timeout': 30, 'retries': 3, 'verbose': False,
                    'cache_size': 100, 'mode': 'normal'}
        config = {**defaults, **kwargs}
        return config

    def process_request(method, url, *args, headers=None, **params):
        result = {'method': method, 'url': url, 'n_args': len(args)}
        if headers:
            result['headers'] = {**headers}
        result['params'] = {**params}
        return sum(len(str(v)) for v in result.values())

    total = 0
    for i in range(n):
        cfg = make_config(timeout=i % 60, retries=i % 5,
                         extra_key=f'val_{i}')
        total += cfg['timeout'] + cfg['retries'] + cfg['cache_size']

        headers = {'Authorization': f'Bearer token_{i}',
                   'Content-Type': 'application/json'}
        total += process_request('GET', f'/api/item/{i}',
                                 'extra_arg', headers=headers,
                                 page=i % 10, limit=50)
    return total


def pattern_global_state(n=5000):
    """Global/module-level state access — common in config, logging, singletons."""
    _registry = {}
    _counter = [0]   # mutable to allow modification from nested scope
    _log = []

    def register(name, value):
        _counter[0] += 1
        _registry[name] = {'value': value, 'id': _counter[0]}
        _log.append(f'registered {name}')

    def lookup(name):
        entry = _registry.get(name)
        if entry:
            _log.append(f'hit {name}')
            return entry['value']
        _log.append(f'miss {name}')
        return None

    def process_batch(items):
        results = []
        for name, value in items:
            register(name, value)
            looked_up = lookup(name)
            results.append(looked_up)
        return results

    batch = [(f'item_{i}', i * 3.14) for i in range(n)]
    results = process_batch(batch)

    # Re-lookup everything
    total = 0.0
    for i in range(0, n, 2):
        val = lookup(f'item_{i}')
        if val:
            total += val

    return total + _counter[0] + len(_log)


# ============================================================================
# 6. TRACER STRESS PATTERNS
# ============================================================================

def stress_large_container_creation(n=200):
    """Creates many large containers — stresses initial snapshot serialization."""
    results = []
    for i in range(n):
        big_list = list(range(100))
        big_dict = {f'k{j}': j for j in range(50)}
        big_set = set(range(80))
        results.append(len(big_list) + len(big_dict) + len(big_set))
    return sum(results)


def stress_tight_nonlocal(n=50000):
    """Tight loop mutating a nonlocal variable — stresses STORE_DEREF."""
    total = 0
    def accumulate(val):
        nonlocal total
        total += val
    for i in range(n):
        accumulate(i)
    return total


def stress_many_short_objects(n=20000):
    """Creates/discards many objects — stresses OID map churn."""
    total = 0
    for i in range(n):
        obj = {'value': i, 'items': [i, i+1, i+2]}
        total += obj['value'] + sum(obj['items'])
    return total


def stress_setitem_loop(n=5000):
    """Writes to list by index in a loop — stresses STORE_SUBSCR path."""
    items = [0] * n
    for i in range(n):
        items[i] = i * 3 + 1
    # Also dict by key
    d = {}
    for i in range(n):
        d[i] = i * 2
    return items[-1] + len(d)


def stress_deep_calls(depth=500):
    """Deep recursive calls — stresses frame cache stack."""
    def recurse(n, acc):
        if n <= 0:
            return acc
        return recurse(n - 1, acc + n)
    return recurse(depth, 0)


def stress_no_store_loop(n=50000):
    """Tight loop with no stores in body — stresses mode 1 LINE overhead."""
    items = list(range(100))
    total = 0
    for _ in range(n):
        total += len(items)  # len() is a C call, no store except total
    return total


def stress_many_small_calls(n=20000):
    """Many calls to trivial functions — stresses RESUME/RETURN overhead."""
    def add1(x):
        return x + 1
    def double(x):
        return x * 2
    def negate(x):
        return -x
    val = 0
    for i in range(n):
        val = add1(val)
        val = double(val)
        val = negate(val)
    return val


def stress_long_strings(n=5000):
    """Variables bound to long strings — stresses value serialization truncation."""
    results = []
    for i in range(n):
        s = f"item_{i}_" + "x" * 200
        t = s[:100] + s[100:]
        results.append(len(t))
    return sum(results)


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

    # Real-world patterns
    'pat_exceptions': lambda: pattern_exceptions(5000),
    'pat_strings': lambda: pattern_string_processing(2000),
    'pat_comprehensions': lambda: pattern_comprehensions(5000),
    'pat_closures': lambda: pattern_closures_decorators(2000),
    'pat_classes': lambda: pattern_class_hierarchy(3000),
    'pat_context': lambda: pattern_context_managers(3000),
    'pat_kwargs': lambda: pattern_kwargs_unpacking(3000),
    'pat_global': lambda: pattern_global_state(5000),

    # Tracer stress patterns
    'stress_snapshots': lambda: stress_large_container_creation(200),
    'stress_nonlocal': lambda: stress_tight_nonlocal(50000),
    'stress_oid_churn': lambda: stress_many_short_objects(20000),
    'stress_setitem': lambda: stress_setitem_loop(5000),
    'stress_deep_calls': lambda: stress_deep_calls(500),
    'stress_no_store': lambda: stress_no_store_loop(50000),
    'stress_small_calls': lambda: stress_many_small_calls(20000),
    'stress_strings': lambda: stress_long_strings(5000),
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
