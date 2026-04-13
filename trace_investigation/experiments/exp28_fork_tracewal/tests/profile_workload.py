"""Profile a workload under different tracing modes.

Usage:
    perf record -g ./python profile_workload.py baseline comp_primes 200
    perf record -g ./python profile_workload.py m0 comp_primes 200
    perf record -g ./python profile_workload.py m1 comp_primes 200
    perf report
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))

import _tracewal
from workloads_large import LARGE_WORKLOADS

mode = sys.argv[1] if len(sys.argv) > 1 else 'baseline'
workload = sys.argv[2] if len(sys.argv) > 2 else 'comp_primes'
iterations = int(sys.argv[3]) if len(sys.argv) > 3 else 200

fn = LARGE_WORKLOADS[workload]

if mode == 'm0':
    _tracewal.start(line_mode=0)
elif mode == 'm1':
    _tracewal.start(line_mode=1)

for _ in range(iterations):
    fn()

if mode != 'baseline':
    _tracewal.stop()
