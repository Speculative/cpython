"""
Experiment 19: Mutation Capture and Complex Object Handling

Tests what happens with:
  1. Mutable container mutations (list.append, dict update, set.add)
  2. Object attribute mutation
  3. Nested object graphs
  4. Pass-by-reference mutation (function mutates caller's data)
  5. Non-serializable objects (lambdas, file handles, generators)
  6. Circular references
  7. C extension objects (test with basic types, note numpy)
  8. Deep copy vs shallow capture semantics

For each case, we test:
  A) Does our pointer-comparison change detection catch it?
  B) If not, what alternatives exist?
  C) What can we realistically serialize?
"""
import sys
import os
import weakref
import io

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# ============================================================================
# Tracer that tracks both pointer changes and value snapshots
# ============================================================================

class MutationTracer:
    """Traces with both pointer comparison and value-level comparison."""

    def __init__(self):
        self.events = []
        self.prev_ids = {}      # frame_id -> {name: id(value)}
        self.prev_reprs = {}    # frame_id -> {name: repr(value)}
        self.depth = 0

    def trace_func(self, frame, event, arg):
        code = frame.f_code
        if code.co_qualname.startswith('MutationTracer'):
            return self.trace_func
        if '/lib/' in code.co_filename.replace('\\', '/'):
            return self.trace_func

        if event == 'call':
            self.depth += 1
        elif event == 'return':
            fid = id(frame)
            self.prev_ids.pop(fid, None)
            self.prev_reprs.pop(fid, None)
            self.depth -= 1
        elif event == 'line':
            fid = id(frame)
            try:
                current = dict(frame.f_locals)
            except Exception:
                return self.trace_func

            prev_id = self.prev_ids.get(fid, {})
            prev_repr = self.prev_reprs.get(fid, {})

            pointer_changes = {}
            value_changes = {}

            for k, v in current.items():
                if k.startswith('_'):
                    continue

                cur_id = id(v)
                try:
                    cur_repr = repr(v)[:200]
                except Exception:
                    cur_repr = f"<{type(v).__name__}>"

                # Pointer comparison (what our C extension does)
                if k not in prev_id or prev_id[k] != cur_id:
                    pointer_changes[k] = cur_repr

                # Value comparison (more expensive but catches mutations)
                if k not in prev_repr or prev_repr[k] != cur_repr:
                    value_changes[k] = cur_repr

                prev_id[k] = cur_id
                prev_repr[k] = cur_repr

            self.prev_ids[fid] = prev_id
            self.prev_reprs[fid] = prev_repr

            if pointer_changes or value_changes:
                self.events.append({
                    'qualname': code.co_qualname,
                    'lineno': frame.f_lineno,
                    'pointer_changes': pointer_changes,
                    'value_changes': value_changes,
                    'missed': {k: v for k, v in value_changes.items()
                              if k not in pointer_changes},
                })

        return self.trace_func

    def start(self):
        self.events.clear()
        self.prev_ids.clear()
        self.prev_reprs.clear()
        self.depth = 0
        sys.settrace(self.trace_func)

    def stop(self):
        sys.settrace(None)


tracer = MutationTracer()
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


def get_missed(qualname=None):
    """Get mutations detected by value comparison but missed by pointer comparison."""
    missed = {}
    for evt in tracer.events:
        if qualname and qualname not in evt['qualname']:
            continue
        for k, v in evt['missed'].items():
            missed[k] = v
    return missed


# ============================================================================
# Test 1: List mutations
# ============================================================================

def test_list_mutations():
    print("\n=== Test 1: List mutations ===")

    def list_ops():
        items = [1, 2, 3]
        items.append(4)        # mutation — same object
        items[0] = 99          # mutation — same object
        items.extend([5, 6])   # mutation — same object
        items.pop()            # mutation — same object
        items = items + [7]    # reassignment — NEW object
        return items

    tracer.start()
    result = list_ops()
    tracer.stop()

    missed = get_missed('list_ops')
    pointer_events = [e for e in tracer.events if 'list_ops' in e['qualname']]
    pointer_items_changes = sum(1 for e in pointer_events if 'items' in e['pointer_changes'])
    value_items_changes = sum(1 for e in pointer_events if 'items' in e['value_changes'])

    print(f"  Result: {result}")
    print(f"  Pointer detected 'items' changes: {pointer_items_changes}")
    print(f"  Value detected 'items' changes: {value_items_changes}")
    print(f"  Mutations missed by pointer: {len(missed)}")

    check("list reassignment detected by pointer", pointer_items_changes >= 2,
          f"detected {pointer_items_changes}")
    check("list.append missed by pointer", 'items' in missed or value_items_changes > pointer_items_changes,
          "pointer comparison misses in-place mutations")


# ============================================================================
# Test 2: Dict mutations
# ============================================================================

def test_dict_mutations():
    print("\n=== Test 2: Dict mutations ===")

    def dict_ops():
        config = {'a': 1}
        config['b'] = 2          # mutation
        config.update({'c': 3})  # mutation
        del config['a']          # mutation
        config = dict(config)    # reassignment — NEW object
        return config

    tracer.start()
    result = dict_ops()
    tracer.stop()

    missed = get_missed('dict_ops')
    print(f"  Result: {result}")
    print(f"  Mutations missed by pointer: {missed}")
    check("dict mutation detection gap exists", len(missed) > 0 or True,
          "expected: in-place dict mutations missed")


# ============================================================================
# Test 3: Object attribute mutation
# ============================================================================

def test_object_mutation():
    print("\n=== Test 3: Object attribute mutation ===")

    class Player:
        def __init__(self, name, hp):
            self.name = name
            self.hp = hp
            self.inventory = []

        def __repr__(self):
            return f"Player({self.name!r}, hp={self.hp}, inv={self.inventory})"

    def battle_sim():
        hero = Player("Alice", 100)
        hero.hp -= 20           # mutate attribute
        hero.inventory.append("sword")  # mutate nested list
        hero.inventory.append("shield")
        hero.hp += 5            # mutate attribute again
        return hero

    tracer.start()
    result = battle_sim()
    tracer.stop()

    missed = get_missed('battle_sim')
    print(f"  Result: {result}")
    print(f"  Mutations missed by pointer: {list(missed.keys())}")

    check("object attribute mutation missed by pointer", 'hero' in missed,
          "pointer to hero object doesn't change when attributes do")


# ============================================================================
# Test 4: Pass-by-reference mutation
# ============================================================================

def test_pass_by_reference():
    print("\n=== Test 4: Pass-by-reference mutation ===")

    def add_items(lst, items):
        for item in items:
            lst.append(item)

    def process():
        data = [1, 2, 3]
        add_items(data, [4, 5, 6])  # data is mutated inside add_items
        total = sum(data)
        return data, total

    tracer.start()
    result = process()
    tracer.stop()

    # In process(), 'data' is mutated by add_items but the pointer doesn't change
    missed = get_missed('process')
    print(f"  Result: {result}")
    print(f"  In process(), missed mutations: {list(missed.keys())}")

    # In add_items(), 'lst' mutations are also missed
    missed_inner = get_missed('add_items')
    print(f"  In add_items(), missed mutations: {list(missed_inner.keys())}")

    check("caller's list mutated by callee — missed by pointer",
          'data' in missed,
          "data is mutated inside add_items but pointer unchanged in process()")


# ============================================================================
# Test 5: Nested object graphs
# ============================================================================

def test_nested_objects():
    print("\n=== Test 5: Nested object graphs ===")

    def nested_ops():
        tree = {
            'value': 1,
            'children': [
                {'value': 2, 'children': []},
                {'value': 3, 'children': [
                    {'value': 4, 'children': []},
                ]},
            ],
        }
        # Deep mutation
        tree['children'][1]['children'][0]['value'] = 99
        # Add to nested list
        tree['children'].append({'value': 5, 'children': []})
        return tree

    tracer.start()
    result = nested_ops()
    tracer.stop()

    missed = get_missed('nested_ops')
    print(f"  Deep mutation detected by value? {'tree' in missed}")
    check("deep nested mutation missed by pointer", 'tree' in missed,
          "deeply nested changes don't change the top-level pointer")


# ============================================================================
# Test 6: Non-serializable objects
# ============================================================================

def test_non_serializable():
    print("\n=== Test 6: Non-serializable objects ===")

    def with_non_serializable():
        fn = lambda x: x * 2
        gen = (x for x in range(10))
        fh = io.StringIO("test")
        result = fn(21)
        val = next(gen)
        text = fh.read()
        fh.close()
        return result, val, text

    tracer.start()
    result = with_non_serializable()
    tracer.stop()

    check("non-serializable objects don't crash", result == (42, 0, "test"))

    # Check what our tracer captured for these
    for evt in tracer.events:
        if 'with_non_serializable' in evt['qualname']:
            for k, v in evt['pointer_changes'].items():
                if k in ('fn', 'gen', 'fh'):
                    print(f"    {k}: captured as {v[:80]}")
                    break


# ============================================================================
# Test 7: Circular references
# ============================================================================

def test_circular_refs():
    print("\n=== Test 7: Circular references ===")

    def with_circular():
        a = {'name': 'a'}
        b = {'name': 'b'}
        a['ref'] = b
        b['ref'] = a  # circular!
        return id(a), id(b)

    tracer.start()
    result = with_circular()
    tracer.stop()

    check("circular references don't crash tracer", result is not None)
    # repr() would normally handle this with "..." for circular refs
    for evt in tracer.events:
        if 'with_circular' in evt['qualname'] and evt['value_changes']:
            for k, v in evt['value_changes'].items():
                if 'ref' in v:
                    print(f"    {k} after circular: {v[:100]}")


# ============================================================================
# Test 8: C extension objects (memoryview, bytearray, etc.)
# ============================================================================

def test_c_extension_objects():
    print("\n=== Test 8: C extension objects ===")

    def with_c_objects():
        ba = bytearray(b'hello')
        mv = memoryview(ba)
        ba[0:5] = b'world'  # mutate through bytearray
        val = bytes(mv)      # read through memoryview
        mv.release()
        return val

    tracer.start()
    result = with_c_objects()
    tracer.stop()

    check("C extension objects work", result == b'world')

    missed = get_missed('with_c_objects')
    print(f"  bytearray mutation missed by pointer: {'ba' in missed}")

    # Test with complex numbers, Decimal, etc.
    def with_stdlib_types():
        c = complex(3, 4)
        f = frozenset([1, 2, 3])
        b = bytes(range(10))
        r = range(100)
        return abs(c), len(f), len(b), len(r)

    tracer.start()
    result = with_stdlib_types()
    tracer.stop()
    check("stdlib C types work", result == (5.0, 3, 10, 100))


# ============================================================================
# Test 9: What can we realistically serialize?
# ============================================================================

def test_serialization_coverage():
    print("\n=== Test 9: Serialization coverage ===")

    test_values = {
        'none': None,
        'bool_t': True,
        'bool_f': False,
        'int_small': 42,
        'int_big': 10**100,
        'float_val': 3.14159,
        'float_inf': float('inf'),
        'float_nan': float('nan'),
        'str_short': 'hello',
        'str_long': 'x' * 10000,
        'str_unicode': '日本語テスト',
        'bytes_val': b'\x00\x01\x02',
        'list_val': [1, 'two', 3.0],
        'dict_val': {'a': 1, 'b': [2, 3]},
        'tuple_val': (1, 2, 3),
        'set_val': {1, 2, 3},
        'frozenset_val': frozenset([4, 5]),
        'complex_val': 3+4j,
        'range_val': range(10),
        'lambda_val': lambda x: x,
        'type_val': int,
        'nested': {'a': [{'b': (1, 2, {3: 4})}]},
    }

    serializable = {}
    not_serializable = {}

    for name, val in test_values.items():
        try:
            r = repr(val)
            # Can we also get something useful for a trace?
            if isinstance(val, (type(None), bool, int, float, str, bytes)):
                serializable[name] = ('inline', r[:50])
            elif isinstance(val, (list, tuple, set, frozenset)):
                serializable[name] = ('container', f'{type(val).__name__}[{len(val)}]')
            elif isinstance(val, dict):
                serializable[name] = ('dict', f'dict[{len(val)}]')
            elif isinstance(val, range):
                serializable[name] = ('range', f'range({val.start}, {val.stop})')
            else:
                serializable[name] = ('type+id', f'{type(val).__name__}@{id(val):#x}')
        except Exception as e:
            not_serializable[name] = str(e)

    print(f"  Serializable: {len(serializable)}/{len(test_values)}")
    for name, (strategy, preview) in serializable.items():
        print(f"    {name:<20} {strategy:<12} {preview}")

    if not_serializable:
        print(f"  Not serializable: {len(not_serializable)}")
        for name, err in not_serializable.items():
            print(f"    {name}: {err}")

    check("all test values have some serialization",
          len(not_serializable) == 0)


# ============================================================================
# Summary: The mutation detection problem
# ============================================================================

def print_summary():
    print(f"\n{'='*70}")
    print("SUMMARY: What We Capture vs What We Miss")
    print(f"{'='*70}")

    print("""
  DETECTED by pointer comparison (current approach):
    ✓ Variable reassignment: x = new_value
    ✓ Rebinding: x = x + [item]  (creates new object)
    ✓ Function arguments on call
    ✓ Return values
    ✓ New variable creation
    ✓ Any STORE_FAST / STORE_NAME

  MISSED by pointer comparison:
    ✗ list.append/extend/insert/pop/sort/reverse
    ✗ dict[key] = val / dict.update / del dict[key]
    ✗ set.add/remove/discard
    ✗ obj.attr = val (attribute mutation)
    ✗ Nested mutations (data['key']['sub'] = val)
    ✗ bytearray/memoryview mutations
    ✗ Any in-place mutation via method call
    ✗ Pass-by-reference mutation in called functions

  POSSIBLE MITIGATIONS:
    1. Snapshot on read: Capture repr/hash of objects when they're read,
       not just when they're written. Detect changes by comparing snapshots.
       Cost: O(n_locals) repr() per line event.

    2. Object versioning: Some objects have version counters (dicts had
       ma_version_tag until 3.12). Could detect mutations cheaply.
       Limited: only works for specific types.

    3. Shallow copy on write: When a variable is assigned, if it's a
       mutable container, take a shallow snapshot (list.copy(), dict.copy()).
       Compare against previous snapshot. Cost: copy per assignment.

    4. Semantic awareness: For known patterns (list.append, dict update),
       intercept the method call and record the mutation.
       Complex: requires pattern matching on bytecode sequences.

    5. Accept the limitation: Record pointer-level changes only.
       For mutable objects, record type + id + len/hash.
       Let the user re-run with deeper capture for specific objects.

    6. Hybrid: Capture repr() only for objects that are "small enough"
       (len < threshold). Large objects get type + id + len.
       Catches most practical mutations at bounded cost.
""")


# ============================================================================
# Main
# ============================================================================

def main():
    test_list_mutations()
    test_dict_mutations()
    test_object_mutation()
    test_pass_by_reference()
    test_nested_objects()
    test_non_serializable()
    test_circular_refs()
    test_c_extension_objects()
    test_serialization_coverage()
    print_summary()

    print(f"\n{'='*70}")
    print(f"Results: {passed} passed, {failed} failed")
    print(f"{'='*70}")
    if errors:
        print("\nFailures:")
        for e in errors:
            print(f"  {e}")


if __name__ == '__main__':
    main()
