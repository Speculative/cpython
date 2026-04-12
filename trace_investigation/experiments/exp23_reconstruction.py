"""
Experiment 23: Object State Reconstruction Analysis

Explores what it takes to reconstruct full object state from trace data.

Key questions:
  1. Can we reconstruct object state from captured mutations (WAL-style)?
  2. How do we handle object destruction?
  3. How do we handle object references (graphs)?
  4. What capture strategies are feasible?

This is an analysis experiment — we test different capture depths and
see what can and can't be reconstructed.
"""
import sys
import os
import copy
import weakref

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'exp7_c_extension'))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'exp10_c_extension'))


# ============================================================================
# Tracer that captures at different depths
# ============================================================================

class ReconstructionTracer:
    """Traces with configurable capture depth to test reconstruction."""

    DEPTH_POINTER = 0      # id(obj) only
    DEPTH_SHALLOW = 1      # type + id + len + top-level value for primitives
    DEPTH_SNAPSHOT = 2     # shallow copy of containers, repr of primitives
    DEPTH_DEEP = 3         # deep copy (full reconstruction possible)

    def __init__(self, depth=1):
        self.depth = depth
        self.events = []
        self.prev_locals = {}
        self.obj_registry = {}    # id -> weakref or snapshot
        self.call_depth = 0

    def _capture_value(self, value):
        """Capture a value at the configured depth."""
        if self.depth == self.DEPTH_POINTER:
            return {'type': type(value).__name__, 'id': id(value)}

        if self.depth >= self.DEPTH_SHALLOW:
            info = {'type': type(value).__name__, 'id': id(value)}

            if value is None or isinstance(value, bool):
                info['value'] = value
            elif isinstance(value, (int, float)):
                info['value'] = value
            elif isinstance(value, str):
                info['value'] = value if len(value) <= 200 else value[:200] + '...'
            elif isinstance(value, bytes):
                info['value'] = value[:200] if len(value) <= 200 else value[:200]
                info['len'] = len(value)
            elif isinstance(value, (list, tuple)):
                info['len'] = len(value)
                if self.depth >= self.DEPTH_SNAPSHOT:
                    # Shallow: capture element ids and primitive values
                    info['elements'] = [self._capture_element(v) for v in value[:50]]
                if self.depth >= self.DEPTH_DEEP:
                    try:
                        info['deep_copy'] = copy.deepcopy(value)
                    except Exception:
                        pass
            elif isinstance(value, dict):
                info['len'] = len(value)
                if self.depth >= self.DEPTH_SNAPSHOT:
                    info['keys'] = list(value.keys())[:50]
                    info['snapshot'] = {
                        k: self._capture_element(v)
                        for k, v in list(value.items())[:50]
                    }
                if self.depth >= self.DEPTH_DEEP:
                    try:
                        info['deep_copy'] = copy.deepcopy(value)
                    except Exception:
                        pass
            elif isinstance(value, set):
                info['len'] = len(value)
                if self.depth >= self.DEPTH_SNAPSHOT:
                    info['elements'] = [self._capture_element(v) for v in list(value)[:50]]
            else:
                # Generic object
                if hasattr(value, '__dict__'):
                    info['attrs'] = list(value.__dict__.keys()) if self.depth >= self.DEPTH_SHALLOW else []
                    if self.depth >= self.DEPTH_SNAPSHOT:
                        info['attr_values'] = {
                            k: self._capture_element(v)
                            for k, v in list(value.__dict__.items())[:20]
                        }

            return info

        return {'type': type(value).__name__, 'id': id(value)}

    def _capture_element(self, value):
        """Capture a single element (for container contents)."""
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return value if len(value) <= 50 else value[:50] + '...'
        return {'type': type(value).__name__, 'id': id(value)}

    def trace_func(self, frame, event, arg):
        code = frame.f_code
        if code.co_qualname.startswith('ReconstructionTracer'):
            return self.trace_func
        if '/lib/' in code.co_filename.replace('\\', '/'):
            return self.trace_func

        if event == 'call':
            self.call_depth += 1
            fid = id(frame)
            current = dict(frame.f_locals)
            changes = {k: self._capture_value(v) for k, v in current.items()
                      if not k.startswith('__')}
            self.prev_locals[fid] = {k: id(v) for k, v in current.items()}
            self.events.append({
                'event': 'call',
                'func': code.co_qualname,
                'line': frame.f_lineno,
                'depth': self.call_depth,
                'changes': changes,
            })

        elif event == 'line':
            fid = id(frame)
            current = dict(frame.f_locals)
            prev_ids = self.prev_locals.get(fid, {})

            changes = {}
            for k, v in current.items():
                if k.startswith('__'):
                    continue
                cur_id = id(v)
                if k not in prev_ids or prev_ids[k] != cur_id:
                    changes[k] = self._capture_value(v)

            if changes:
                self.events.append({
                    'event': 'line',
                    'func': code.co_qualname,
                    'line': frame.f_lineno,
                    'depth': self.call_depth,
                    'changes': changes,
                })

            self.prev_locals[fid] = {k: id(v) for k, v in current.items()}

        elif event == 'return':
            self.events.append({
                'event': 'return',
                'func': code.co_qualname,
                'line': frame.f_lineno,
                'depth': self.call_depth,
                'retval': self._capture_value(arg) if arg is not None else None,
            })
            self.prev_locals.pop(id(frame), None)
            self.call_depth -= 1

        return self.trace_func

    def start(self):
        self.events.clear()
        self.prev_locals.clear()
        self.call_depth = 0
        sys.settrace(self.trace_func)

    def stop(self):
        sys.settrace(None)


# ============================================================================
# Test scenarios
# ============================================================================

passed = 0
failed = 0
errors = []

def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS: {name}")
    else:
        failed += 1
        msg = f"  FAIL: {name}"
        if detail:
            msg += f" — {detail}"
        print(msg)
        errors.append(msg)


# --- Scenario 1: Can we reconstruct from snapshots? ---
def test_snapshot_reconstruction():
    print("\n=== Scenario 1: Snapshot-based reconstruction ===")
    print("  Strategy: capture shallow snapshot at each change point")

    tracer = ReconstructionTracer(depth=ReconstructionTracer.DEPTH_SNAPSHOT)

    def target():
        items = [1, 2, 3]
        items.append(4)
        items[0] = 99
        d = {'a': 1}
        d['b'] = 2
        d.update({'c': 3})
        return items, d

    tracer.start()
    result = target()
    tracer.stop()

    # Can we reconstruct the state at each line?
    # With pointer-based change detection, we only capture when the pointer changes.
    # For mutations (append, update), the pointer stays the same — we DON'T capture.
    pointer_changes = [e for e in tracer.events
                      if e['event'] == 'line' and e.get('changes')]

    print(f"  Events: {len(tracer.events)}")
    print(f"  Lines with pointer changes: {len(pointer_changes)}")
    for e in pointer_changes:
        for var, info in e['changes'].items():
            snap = info.get('elements') or info.get('snapshot') or info.get('value')
            print(f"    Line {e['line']}: {var} = {snap}")

    # The gap: mutations without pointer change aren't captured
    # items.append(4) and d.update({'c': 3}) are invisible
    check("snapshot captures initial assignment", len(pointer_changes) >= 2)

    print(f"\n  LIMITATION: Mutations via method calls (append, update) are not")
    print(f"  captured because the pointer doesn't change. To reconstruct,")
    print(f"  we need to capture the object state on mutation-flagged lines too.")


# --- Scenario 2: What about object destruction? ---
def test_object_destruction():
    print("\n=== Scenario 2: Object destruction ===")

    tracer = ReconstructionTracer(depth=ReconstructionTracer.DEPTH_SHALLOW)
    destruction_ids = []

    def target():
        # Object goes out of scope
        temp = [1, 2, 3]
        temp_id = id(temp)
        temp = None  # temp is destroyed (refcount -> 0)
        # temp_id now points to freed memory

        # Object replaced
        x = {'key': 'value1'}
        x_id1 = id(x)
        x = {'key': 'value2'}  # old dict destroyed
        x_id2 = id(x)

        # id reuse: CPython may reuse the same memory address
        a = [1]
        a_id = id(a)
        del a
        b = [2]
        b_id = id(b)

        return temp_id, x_id1, x_id2, a_id, b_id

    tracer.start()
    temp_id, x_id1, x_id2, a_id, b_id = target()
    tracer.stop()

    print(f"  temp_id: 0x{temp_id:x}")
    print(f"  x_id1 (old): 0x{x_id1:x}, x_id2 (new): 0x{x_id2:x}")
    print(f"  a_id: 0x{a_id:x}, b_id: 0x{b_id:x}")
    print(f"  id reused (a -> b): {a_id == b_id}")

    # The problem: if a_id == b_id, our id-based tracking thinks they're the same object
    id_reused = a_id == b_id

    if id_reused:
        print(f"  WARNING: CPython reused id 0x{a_id:x} — [1] and [2] have same id")
        print(f"  This means id-based object tracking can confuse different objects.")
    else:
        print(f"  IDs not reused in this run (but they can be in general)")

    check("destruction scenario runs", True)

    print(f"\n  IMPLICATIONS:")
    print(f"  1. Object ids can be reused after destruction (CPython allocator)")
    print(f"  2. Pure id-based tracking WILL confuse objects if ids are reused")
    print(f"  3. Solutions:")
    print(f"     a) Keep objects alive via Py_INCREF (prevents reuse, costs memory)")
    print(f"     b) Use (id, generation_counter) as key — increment on each new object")
    print(f"     c) Use (id, creation_timestamp) as key")
    print(f"     d) Accept id reuse — track by (id, type, birth_line) tuple")


# --- Scenario 3: Object reference graphs ---
def test_object_references():
    print("\n=== Scenario 3: Object reference graphs ===")

    tracer = ReconstructionTracer(depth=ReconstructionTracer.DEPTH_SNAPSHOT)

    class Node:
        def __init__(self, value):
            self.value = value
            self.children = []
        def add_child(self, child):
            self.children.append(child)
        def __repr__(self):
            return f"Node({self.value}, children={len(self.children)})"

    def target():
        root = Node("root")
        child1 = Node("a")
        child2 = Node("b")
        root.add_child(child1)
        root.add_child(child2)
        child1.add_child(Node("a1"))
        # Mutation via nested reference
        root.children[0].value = "A"
        return root

    tracer.start()
    result = target()
    tracer.stop()

    # What did we capture?
    print(f"  Final state: {result}")
    print(f"  root.children[0].value = {result.children[0].value}")
    print(f"  Events: {len(tracer.events)}")

    for e in tracer.events:
        if e['event'] == 'line' and e.get('changes'):
            for var, info in e['changes'].items():
                attrs = info.get('attrs', [])
                attr_vals = info.get('attr_values', {})
                print(f"    Line {e['line']}: {var} ({info['type']}) attrs={attrs}")
                if attr_vals:
                    for k, v in attr_vals.items():
                        print(f"      .{k} = {v}")

    check("reference graph traced", len(tracer.events) > 0)

    print(f"\n  CHALLENGE: When we capture 'root', we see its attributes at")
    print(f"  that moment. But root.children is a list — we'd need to also")
    print(f"  capture the list contents, and each child's attributes, etc.")
    print(f"  The graph can be arbitrarily deep.")
    print(f"\n  PRACTICAL STRATEGIES:")
    print(f"  1. Capture depth=1: record attr names + ids of attr values")
    print(f"     Reconstruct by linking ids across captures")
    print(f"  2. Capture depth=N: snapshot N levels deep (configurable)")
    print(f"  3. Lazy expansion: capture shallow, expand on demand during replay")


# --- Scenario 4: Shared mutable state ---
def test_shared_state():
    print("\n=== Scenario 4: Shared mutable state (aliasing) ===")

    tracer = ReconstructionTracer(depth=ReconstructionTracer.DEPTH_SNAPSHOT)

    def target():
        shared_list = [1, 2, 3]
        a = shared_list
        b = shared_list  # a and b point to the same list
        a.append(4)      # mutates b too!
        b_len = len(b)   # b is now [1, 2, 3, 4]
        return a is b, b_len

    tracer.start()
    result = target()
    tracer.stop()

    print(f"  a is b: {result[0]}, len(b) after a.append: {result[1]}")

    # Check if our tracer sees the aliasing
    # When 'a' is assigned, we capture id(a).
    # When 'b' is assigned, we capture id(b).
    # If id(a) == id(b), we know they're the same object.
    a_id = b_id = None
    for e in tracer.events:
        if e['event'] == 'line' and e.get('changes'):
            if 'a' in e['changes']:
                a_id = e['changes']['a']['id']
            if 'b' in e['changes']:
                b_id = e['changes']['b']['id']

    check("aliasing detected via id", a_id is not None and a_id == b_id,
          f"a_id=0x{a_id or 0:x}, b_id=0x{b_id or 0:x}")

    print(f"\n  INSIGHT: Aliasing is detectable because id(a) == id(b).")
    print(f"  When we see a.append(4), we know b was also affected")
    print(f"  because they share the same id. The reconstruction viewer")
    print(f"  can show this: 'b is an alias of a — see a for current state.'")


# --- Scenario 5: What capture strategies enable reconstruction? ---
def test_reconstruction_strategies():
    print("\n=== Scenario 5: Reconstruction strategy comparison ===")

    def target():
        items = [1, 2, 3]           # initial state
        items.append(4)              # mutation 1
        items[0] = 99                # mutation 2 (STORE_SUBSCR)
        items.sort()                 # mutation 3
        d = {'x': items}            # reference to items
        d['y'] = len(items)          # STORE_SUBSCR on d
        return items, d

    # Strategy A: Pointer-only (current C extension approach)
    print("\n  Strategy A: Pointer-only")
    tracer_a = ReconstructionTracer(depth=ReconstructionTracer.DEPTH_POINTER)
    tracer_a.start()
    target()
    tracer_a.stop()
    events_a = [e for e in tracer_a.events if e['event'] == 'line' and e.get('changes')]
    print(f"    Captures: {len(events_a)} pointer changes")
    print(f"    Can reconstruct: variable identity (which object), NOT contents")

    # Strategy B: Shallow snapshot
    print("\n  Strategy B: Shallow snapshot (type + len + top-level values)")
    tracer_b = ReconstructionTracer(depth=ReconstructionTracer.DEPTH_SHALLOW)
    tracer_b.start()
    target()
    tracer_b.stop()
    events_b = [e for e in tracer_b.events if e['event'] == 'line' and e.get('changes')]
    print(f"    Captures: {len(events_b)} changes with type+len info")
    print(f"    Can reconstruct: object type and size at each point, NOT full contents")

    # Strategy C: Content snapshot
    print("\n  Strategy C: Content snapshot (shallow copy of containers)")
    tracer_c = ReconstructionTracer(depth=ReconstructionTracer.DEPTH_SNAPSHOT)
    tracer_c.start()
    target()
    tracer_c.stop()
    events_c = [e for e in tracer_c.events if e['event'] == 'line' and e.get('changes')]
    print(f"    Captures: {len(events_c)} changes with content snapshots")
    for e in events_c:
        for var, info in e['changes'].items():
            content = info.get('elements') or info.get('snapshot') or info.get('value')
            print(f"      Line {e['line']}: {var} = {str(content)[:80]}")
    print(f"    Can reconstruct: full state at points where pointer changed")
    print(f"    MISSING: mutations that don't change pointer (append, sort, etc.)")

    # Strategy D: Deep copy (gold standard, expensive)
    print("\n  Strategy D: Deep copy at every change")
    tracer_d = ReconstructionTracer(depth=ReconstructionTracer.DEPTH_DEEP)
    tracer_d.start()
    target()
    tracer_d.stop()
    events_d = [e for e in tracer_d.events if e['event'] == 'line' and e.get('changes')]
    deep_copies = 0
    for e in events_d:
        for var, info in e['changes'].items():
            if 'deep_copy' in info:
                deep_copies += 1
    print(f"    Deep copies made: {deep_copies}")
    print(f"    Can reconstruct: full state at pointer-change points")
    print(f"    Still MISSING: mutations without pointer change")


# --- Scenario 6: Practical recommendation ---
def test_practical_approach():
    print("\n=== Scenario 6: Practical approach — mutation-line snapshots ===")
    print("  Idea: read the object on MUTATION-FLAGGED lines (from bytecode analysis)")
    print("  even when the pointer hasn't changed. Capture shallow snapshot.")

    class MutationAwareTracer:
        """Simulates what the C extension would do if it captured object state
        on mutation-flagged lines (not just pointer changes)."""

        def __init__(self):
            self.snapshots = []  # (line, var, snapshot)
            self.prev_ids = {}

        def trace_func(self, frame, event, arg):
            code = frame.f_code
            if code.co_qualname.startswith('MutationAwareTracer'):
                return self.trace_func
            if code.co_qualname != 'test_practical_approach.<locals>.target':
                return self.trace_func

            if event == 'line':
                fid = id(frame)
                current = dict(frame.f_locals)
                prev = self.prev_ids.get(fid, {})

                for k, v in current.items():
                    if k.startswith('_'):
                        continue
                    # Capture on EVERY line (simulating mutation-aware flagging)
                    # In practice, the C extension would only read on flagged lines
                    cur_snapshot = self._snapshot(v)
                    prev_snapshot = prev.get(k)
                    if cur_snapshot != prev_snapshot:
                        self.snapshots.append((frame.f_lineno, k, cur_snapshot))

                self.prev_ids[fid] = {k: self._snapshot(v) for k, v in current.items()
                                      if not k.startswith('_')}

            elif event == 'return':
                self.prev_ids.pop(id(frame), None)

            return self.trace_func

        def _snapshot(self, v):
            """Cheap snapshot: type + len for containers, value for primitives."""
            if v is None or isinstance(v, (bool, int, float, str)):
                return ('prim', v)
            if isinstance(v, list):
                return ('list', len(v), tuple(v) if len(v) <= 20 else ('...', len(v)))
            if isinstance(v, dict):
                return ('dict', len(v), tuple(sorted(v.items())) if len(v) <= 20 else ('...', len(v)))
            if isinstance(v, set):
                return ('set', len(v), frozenset(v) if len(v) <= 20 else ('...', len(v)))
            if hasattr(v, '__dict__'):
                return ('obj', type(v).__name__,
                        tuple(sorted(v.__dict__.items())) if len(v.__dict__) <= 20 else ('...', len(v.__dict__)))
            return ('other', type(v).__name__, id(v))

    def target():
        items = [1, 2, 3]
        items.append(4)
        items[0] = 99
        items.sort()
        d = {'x': 1}
        d['y'] = 2
        d.update({'z': 3})
        return items, d

    tracer = MutationAwareTracer()
    sys.settrace(tracer.trace_func)
    result = target()
    sys.settrace(None)

    print(f"\n  Result: {result}")
    print(f"  Snapshots captured: {len(tracer.snapshots)}")
    print(f"\n  State reconstruction timeline:")
    for line, var, snap in tracer.snapshots:
        print(f"    Line {line}: {var} = {snap}")

    # Verify we can reconstruct the full history
    items_history = [(l, s) for l, v, s in tracer.snapshots if v == 'items']
    d_history = [(l, s) for l, v, s in tracer.snapshots if v == 'd']

    print(f"\n  items history: {len(items_history)} states")
    for line, snap in items_history:
        print(f"    Line {line}: {snap}")

    print(f"\n  d history: {len(d_history)} states")
    for line, snap in d_history:
        print(f"    Line {line}: {snap}")

    check("items history captures all mutations",
          len(items_history) >= 4,  # init + append + subscr + sort
          f"got {len(items_history)} states")
    check("d history captures all mutations",
          len(d_history) >= 3,  # init + subscr + update
          f"got {len(d_history)} states")

    # Verify final state
    if items_history:
        final_items = items_history[-1][1]
        check("items final state correct",
              final_items[2] == (3, 4, 99) or final_items[2] == tuple([3, 4, 99]),
              f"got {final_items}")
    if d_history:
        final_d = d_history[-1][1]
        check("d final state correct",
              'z' in str(final_d),
              f"got {final_d}")


# ============================================================================
# Summary
# ============================================================================

def print_summary():
    print(f"\n{'='*70}")
    print("SUMMARY: State Reconstruction Architecture")
    print(f"{'='*70}")
    print("""
  WHAT WE CAPTURE (current C extension + mutation analysis):
    For each trace event:
      - Event type (call/line/return)
      - Code object identity + line number
      - Which variables were written (from bytecode bitmask)
      - Variable values via PyFrame_GetVar (pointer comparison for change)

  THE RECONSTRUCTION GAP:
    Pointer comparison misses in-place mutations (append, sort, etc.)
    But bytecode analysis KNOWS which lines mutate — we just need to
    read the object state on those lines too.

  PROPOSED ARCHITECTURE:

    On mutation-flagged lines, the C extension would:
      1. Read the variable via PyFrame_GetVar (same as now)
      2. If pointer unchanged but line is mutation-flagged:
         - For list/tuple: capture len() + first/last N elements
         - For dict: capture len() + keys
         - For set: capture len()
         - For objects: capture __dict__ keys + primitive values
         - For all: capture a shallow content hash for diff detection

    This adds O(1) work per mutation-flagged line (len is free,
    reading a few elements is cheap).

  OBJECT DESTRUCTION:
    - id() can be reused by CPython's allocator after object destruction
    - Solutions:
      a) Track objects by (id, type, first_seen_line) — cheap, handles 99%
      b) Py_INCREF tracked objects to prevent id reuse (costs memory)
      c) Use a generation counter per id slot

  OBJECT REFERENCES:
    - When we capture a container, we see element ids
    - When we capture an object, we see attribute value ids
    - The viewer can resolve id -> object state from other captures
    - Aliasing detected naturally: if id(a) == id(b), viewer shows the link
    - Deep graphs: capture only 1 level deep, expand lazily on demand

  RECONSTRUCTION ALGORITHM:
    For each variable at each point in time:
      1. Look up the latest capture for that variable
      2. If it's a primitive: value is directly available
      3. If it's a container: contents available from shallow snapshot
      4. If it's an object: attrs available from snapshot
      5. For references: resolve target id to its latest capture
      6. For aliases: display "same as <other variable>"
""")


# ============================================================================
# Main
# ============================================================================

def main():
    test_snapshot_reconstruction()
    test_object_destruction()
    test_object_references()
    test_shared_state()
    test_reconstruction_strategies()
    test_practical_approach()
    print_summary()

    print(f"\n{'='*60}")
    print(f"Results: {passed} passed, {failed} failed")
    print(f"{'='*60}")
    if errors:
        print("\nFailures:")
        for e in errors:
            print(f"  {e}")


if __name__ == '__main__':
    main()
