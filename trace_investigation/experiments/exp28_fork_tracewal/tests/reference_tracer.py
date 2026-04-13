"""
Reference tracer — pure Python settrace that captures the ground truth
of program execution.

Output: a list of steps, one per settrace event, each with:
  - event type (line/call/return/exception)
  - file, line number, function name
  - snapshot of ALL local variables at that point

This is slow — it's the ground truth to validate against.
"""
import sys
import collections
import copy


def deep_snapshot(value, depth=0, seen=None):
    """Create a JSON-serializable deep snapshot of a Python value."""
    if depth > 20:
        return ['__truncated__']
    if seen is None:
        seen = set()

    obj_id = id(value)

    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if -2**53 <= value <= 2**53:
            return value
        return ['__bigint__', str(value)]
    if isinstance(value, float):
        return value
    if isinstance(value, str):
        return value[:500]
    if isinstance(value, bytes):
        return ['__bytes__', value[:200].hex()]

    if obj_id in seen:
        return ['__cycle__']
    seen = seen | {obj_id}

    if isinstance(value, list):
        return ['__list__', [deep_snapshot(v, depth + 1, seen) for v in value[:500]]]
    if isinstance(value, tuple):
        return ['__tuple__', [deep_snapshot(v, depth + 1, seen) for v in value[:500]]]
    if isinstance(value, dict):
        pairs = []
        for k, v in list(value.items())[:500]:
            pairs.append([deep_snapshot(k, depth + 1, seen),
                          deep_snapshot(v, depth + 1, seen)])
        return ['__dict__', pairs]
    if isinstance(value, set):
        return ['__set__', sorted([deep_snapshot(v, depth + 1, seen) for v in value], key=repr)]
    if isinstance(value, frozenset):
        return ['__frozenset__', sorted([deep_snapshot(v, depth + 1, seen) for v in value], key=repr)]
    if isinstance(value, collections.deque):
        return ['__deque__', [deep_snapshot(v, depth + 1, seen) for v in value]]
    if isinstance(value, type):
        return ['__type__', value.__name__]
    if callable(value):
        return ['__callable__', getattr(value, '__name__', repr(value))]
    if isinstance(value, type(sys)):
        return ['__module__', value.__name__]

    # User object
    if hasattr(value, '__dict__') and not isinstance(value.__dict__, type):
        attrs = {}
        for k, v in value.__dict__.items():
            if not k.startswith('_'):
                attrs[k] = deep_snapshot(v, depth + 1, seen)
        return ['__obj__', type(value).__name__, attrs]

    try:
        return ['__repr__', repr(value)[:200]]
    except Exception:
        return ['__unknown__']


def snapshot_locals(frame):
    """Snapshot all local variables from a frame.

    Filters out:
    - dunder names
    - modules
    - the class being defined (in class bodies)
    """
    result = {}
    for name, value in frame.f_locals.items():
        if name.startswith('__') and name.endswith('__'):
            continue
        if isinstance(value, type(sys)):
            continue
        result[name] = deep_snapshot(value)
    return result


class ReferenceTracer:
    """Captures ground truth via settrace: every line, call, return, exception."""

    def __init__(self, trace_file):
        """trace_file: only trace frames from this source file."""
        self.trace_file = trace_file
        self.steps = []

    def _trace(self, frame, event, arg):
        if frame.f_code.co_filename != self.trace_file:
            return self._trace

        step = {
            'event': event,
            'lineno': frame.f_lineno,
            'funcname': frame.f_code.co_name,
            'locals': snapshot_locals(frame),
        }

        if event == 'return':
            step['retval'] = deep_snapshot(arg)
        elif event == 'exception':
            exc_type, exc_value, exc_tb = arg
            step['exc_type'] = exc_type.__name__ if exc_type else None
            step['exc_msg'] = str(exc_value) if exc_value else None

        self.steps.append(step)
        return self._trace

    def trace(self, fn):
        """Run fn under settrace, return (steps, result)."""
        self.steps = []
        sys.settrace(self._trace)
        try:
            result = fn()
        finally:
            sys.settrace(None)
        return self.steps, result
