"""
Microbenchmark workloads for trace capture experiments.
Each workload exercises different execution patterns.
"""
import time


# === Workload 1: Tight computational loop (many lines, few calls) ===
def fib_iterative(n):
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return a


# === Workload 2: Deep recursion (many calls, few lines per call) ===
def fib_recursive(n):
    if n <= 1:
        return n
    return fib_recursive(n - 1) + fib_recursive(n - 2)


# === Workload 3: Mixed - data processing with function calls ===
def process_data(data):
    results = []
    for item in data:
        transformed = transform(item)
        if should_include(transformed):
            results.append(summarize(transformed))
    return results


def transform(item):
    return {
        'value': item * 2 + 1,
        'squared': item ** 2,
        'label': f"item_{item}",
    }


def should_include(item):
    return item['value'] % 3 != 0


def summarize(item):
    return (item['label'], item['squared'])


# === Workload 4: Exception handling ===
def exception_workload(n):
    count = 0
    for i in range(n):
        try:
            result = risky_operation(i)
            count += result
        except ValueError:
            count -= 1
    return count


def risky_operation(x):
    if x % 7 == 0:
        raise ValueError("bad value")
    return x % 10


# === Workload 5: Generator-based pipeline ===
def gen_pipeline(n):
    source = range(n)
    doubled = (x * 2 for x in source)
    filtered = (x for x in doubled if x % 3 != 0)
    mapped = (x ** 0.5 for x in filtered)
    return sum(mapped)


# === Workload 6: Class-heavy OOP ===
class Counter:
    def __init__(self, start=0):
        self.value = start

    def increment(self, amount=1):
        self.value += amount
        return self.value

    def decrement(self, amount=1):
        self.value -= amount
        return self.value

    def reset(self):
        self.value = 0


def oop_workload(n):
    c = Counter()
    total = 0
    for i in range(n):
        if i % 3 == 0:
            total += c.increment(i)
        elif i % 3 == 1:
            total += c.decrement(1)
        else:
            c.reset()
    return total


# === Runner ===
WORKLOADS = {
    'fib_iter': lambda: fib_iterative(10000),
    'fib_rec': lambda: fib_recursive(25),
    'data_proc': lambda: process_data(range(5000)),
    'exceptions': lambda: exception_workload(5000),
    'generators': lambda: gen_pipeline(10000),
    'oop': lambda: oop_workload(10000),
}


def run_workload(name, iterations=10):
    """Run a workload and return (total_time_ns, per_iter_ns)."""
    workload = WORKLOADS[name]
    # Warmup
    for _ in range(3):
        workload()

    start = time.perf_counter_ns()
    for _ in range(iterations):
        workload()
    elapsed = time.perf_counter_ns() - start

    return elapsed, elapsed // iterations


def run_all(iterations=10):
    """Run all workloads and return timing dict."""
    results = {}
    for name in WORKLOADS:
        total, per_iter = run_workload(name, iterations)
        results[name] = per_iter
    return results


if __name__ == '__main__':
    print("=== Baseline Workload Timings ===")
    print(f"{'Workload':<15} {'Per-iter (us)':>15} {'Per-iter (ms)':>15}")
    print("-" * 47)
    for name in WORKLOADS:
        total, per_iter = run_workload(name, iterations=20)
        print(f"{name:<15} {per_iter/1000:>15.1f} {per_iter/1_000_000:>15.3f}")
