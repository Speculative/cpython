"""
Experiment 21: Mutation Detection Through the C Extension

Tests the full mutation detection pipeline using ctrace2:
  1. Python bytecode analysis (once per code object) identifies all mutation
     patterns: STORE_FAST, STORE_SUBSCR, STORE_ATTR, DELETE_SUBSCR,
     DELETE_ATTR, and known-mutating method calls
  2. Analysis is packed into per-line bitmasks and passed to C via register_code()
  3. C extension reads only flagged variables via PyFrame_GetVar after each line

Tests every mutating operation on list, dict, set, deque, plus user-defined
classes with primitive and object attributes.
"""
import sys
import os
import dis
import opcode
import types
import collections

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'exp7_c_extension'))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'exp10_c_extension'))

import _ctrace2


# ============================================================================
# Extended bytecode analysis: STORE_FAST + STORE_SUBSCR + STORE_ATTR +
#   DELETE_SUBSCR + DELETE_ATTR + known-mutating method calls
# ============================================================================

STORE_FAST_OPS = set()
STORE_SUBSCR_OP = None
STORE_ATTR_OP = None
DELETE_SUBSCR_OP = None
DELETE_ATTR_OP = None

for name, op in opcode.opmap.items():
    if 'STORE_FAST' in name or name == 'STORE_NAME' or name == 'STORE_DEREF':
        STORE_FAST_OPS.add(op)
    elif name == 'STORE_SUBSCR':
        STORE_SUBSCR_OP = op
    elif name == 'STORE_ATTR':
        STORE_ATTR_OP = op
    elif name == 'DELETE_SUBSCR':
        DELETE_SUBSCR_OP = op
    elif name == 'DELETE_ATTR':
        DELETE_ATTR_OP = op

KNOWN_MUTATING_METHODS = {
    # list
    'append', 'clear', 'extend', 'insert', 'pop', 'remove', 'reverse', 'sort',
    # set
    'add', 'discard', 'difference_update', 'intersection_update',
    'symmetric_difference_update', 'update',
    # deque
    'appendleft', 'extendleft', 'popleft', 'rotate',
    # bytearray
    'resize',
    # dict (also covered by watchers, but good to have in bytecode analysis)
    'setdefault', 'popitem',
}


def analyze_mutations_full(code):
    """Full mutation analysis. Returns {line: bitmask} where bits correspond
    to variable indices in co_varnames that may be mutated on that line."""

    instructions = list(dis.get_instructions(code))
    varnames = code.co_varnames
    varname_to_idx = {name: i for i, name in enumerate(varnames) if i < 64}

    line_bitmasks = {}

    def set_bit(line, varname):
        idx = varname_to_idx.get(varname)
        if idx is not None:
            line_bitmasks[line] = line_bitmasks.get(line, 0) | (1 << idx)

    def find_load_fast_before(i, steps=3):
        for j in range(max(0, i - steps), i):
            if 'LOAD_FAST' in instructions[j].opname:
                return instructions[j].argval
        return None

    for i, instr in enumerate(instructions):
        line = instr.positions.lineno
        if line is None:
            continue

        # STORE_FAST / STORE_NAME / STORE_DEREF
        if instr.opcode in STORE_FAST_OPS:
            set_bit(line, instr.argval)

        # STORE_SUBSCR: target is a LOAD_FAST a few instructions before
        elif instr.opcode == STORE_SUBSCR_OP:
            target = find_load_fast_before(i, steps=3)
            if target:
                set_bit(line, target)

        # DELETE_SUBSCR
        elif instr.opcode == DELETE_SUBSCR_OP:
            target = find_load_fast_before(i, steps=2)
            if target:
                set_bit(line, target)

        # STORE_ATTR: target is a LOAD_FAST right before
        elif instr.opcode == STORE_ATTR_OP:
            target = find_load_fast_before(i, steps=2)
            if target:
                set_bit(line, target)

        # DELETE_ATTR
        elif instr.opcode == DELETE_ATTR_OP:
            target = find_load_fast_before(i, steps=1)
            if target:
                set_bit(line, target)

        # Known mutating method: LOAD_FAST x → LOAD_ATTR <mutator> → ... → CALL
        elif instr.opname == 'LOAD_ATTR' and instr.argval in KNOWN_MUTATING_METHODS:
            if i > 0 and 'LOAD_FAST' in instructions[i - 1].opname:
                target = instructions[i - 1].argval
                set_bit(line, target)

    return line_bitmasks


def register_code_full(code, visited=None):
    """Analyze and register a code object with the C extension."""
    if visited is None:
        visited = set()
    if id(code) in visited:
        return
    visited.add(id(code))

    line_bitmasks = analyze_mutations_full(code)
    first_line = code.co_firstlineno
    packed = [(line, mask) for line, mask in line_bitmasks.items()]
    _ctrace2.register_code(code, first_line, packed)

    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            register_code_full(const, visited)


# ============================================================================
# Test infrastructure
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


def run_traced(func):
    """Register code, run through C extension, return (result, stats)."""
    register_code_full(func.__code__)
    _ctrace2.start(1)
    result = func()
    _ctrace2.stop()
    stats = _ctrace2.stats()
    return result, stats


def show_analysis(func):
    """Print the bytecode analysis for a function."""
    bitmasks = analyze_mutations_full(func.__code__)
    varnames = func.__code__.co_varnames
    print(f"  Bytecode analysis of {func.__name__}:")
    for line in sorted(bitmasks):
        mask = bitmasks[line]
        vars_flagged = [varnames[i] for i in range(min(len(varnames), 64)) if mask & (1 << i)]
        print(f"    Line {line}: {vars_flagged}")


# ============================================================================
# Test A: List — all mutating methods
# ============================================================================

def test_list():
    print("\n=== Test A: List mutations (C extension) ===")

    def list_all():
        items = [3, 1, 4, 1, 5]
        items.append(9)
        items.extend([2, 6])
        items.insert(0, 0)
        items.remove(1)
        items.pop()
        items.pop(0)
        items.reverse()
        items.sort()
        items.clear()
        items = [1, 2, 3]
        items[0] = 99
        del items[1]
        items += [4, 5]
        return items

    show_analysis(list_all)
    result, stats = run_traced(list_all)
    print(f"  Result: {result}")
    print(f"  Stats: vars_checked={stats['vars_checked']}, changed={stats['vars_changed']}")
    check("list result correct", result == [99, 3, 4, 5])
    check("list vars detected", stats['vars_changed'] > 0,
          f"changed={stats['vars_changed']}")
    check("list lines with writes", stats['lines_with_writes'] >= 10,
          f"lines_w_writes={stats['lines_with_writes']}")


# ============================================================================
# Test B: Dict — all mutating methods
# ============================================================================

def test_dict():
    print("\n=== Test B: Dict mutations (C extension) ===")

    def dict_all():
        d = {'a': 1}
        d['b'] = 2
        d.update({'c': 3})
        d.setdefault('d', 4)
        d.pop('a')
        d.popitem()
        d.clear()
        d = {'x': 1, 'y': 2}
        del d['x']
        d |= {'z': 3}
        return d

    show_analysis(dict_all)
    result, stats = run_traced(dict_all)
    print(f"  Result: {result}")
    print(f"  Stats: vars_checked={stats['vars_checked']}, changed={stats['vars_changed']}")
    check("dict result correct", result == {'y': 2, 'z': 3})
    check("dict vars detected", stats['vars_changed'] > 0)


# ============================================================================
# Test C: Set — all mutating methods
# ============================================================================

def test_set():
    print("\n=== Test C: Set mutations (C extension) ===")

    def set_all():
        s = {1, 2, 3}
        s.add(4)
        s.update({5, 6})
        s.discard(1)
        s.remove(2)
        s.pop()
        s.difference_update({5})
        s.intersection_update({3, 6})
        s.symmetric_difference_update({7, 8})
        s.clear()
        s = {10, 20}
        s |= {30}
        return s

    show_analysis(set_all)
    result, stats = run_traced(set_all)
    print(f"  Result: {result}")
    print(f"  Stats: vars_checked={stats['vars_checked']}, changed={stats['vars_changed']}")
    check("set result correct", result == {10, 20, 30})
    check("set vars detected", stats['vars_changed'] > 0)


# ============================================================================
# Test D: Deque — all mutating methods
# ============================================================================

def test_deque():
    print("\n=== Test D: Deque mutations (C extension) ===")

    def deque_all():
        dq = collections.deque([1, 2, 3])
        dq.append(4)
        dq.appendleft(0)
        dq.extend([5, 6])
        dq.extendleft([-1, -2])
        dq.pop()
        dq.popleft()
        dq.remove(2)
        dq.insert(1, 99)
        dq.reverse()
        dq.rotate(2)
        dq.clear()
        dq = collections.deque([10])
        return dq

    show_analysis(deque_all)
    result, stats = run_traced(deque_all)
    print(f"  Result: {result}")
    print(f"  Stats: vars_checked={stats['vars_checked']}, changed={stats['vars_changed']}")
    check("deque result correct", result == collections.deque([10]))
    check("deque vars detected", stats['vars_changed'] > 0)


# ============================================================================
# Test E: STORE_SUBSCR + STORE_ATTR
# ============================================================================

def test_subscr_attr():
    print("\n=== Test E: Subscript and attribute writes (C extension) ===")

    class Obj:
        def __repr__(self):
            return f"Obj({self.__dict__})"

    def subscr_attr():
        lst = [0, 0, 0]
        lst[0] = 'a'
        lst[1] = 'b'
        d = {}
        d['x'] = 1
        d['y'] = 2
        obj = Obj()
        obj.name = "test"
        obj.value = 42
        del d['x']
        del lst[0]
        return lst, d, obj

    show_analysis(subscr_attr)
    result, stats = run_traced(subscr_attr)
    print(f"  Result: {result}")
    print(f"  Stats: vars_checked={stats['vars_checked']}, changed={stats['vars_changed']}")
    check("subscr/attr result correct",
          result[0] == ['b', 0] and result[1] == {'y': 2})
    check("subscr/attr vars detected", stats['vars_changed'] > 0)


# ============================================================================
# Test F: User-defined class with primitive + object attributes
# ============================================================================

def test_user_class():
    print("\n=== Test F: User-defined class (C extension) ===")

    class Inventory:
        def __init__(self):
            self.items = []
            self.counts = {}

        def add(self, item, count=1):
            self.items.append(item)
            self.counts[item] = count

        def remove(self, item):
            self.items.remove(item)
            del self.counts[item]

        def __repr__(self):
            return f"Inv({self.items})"

    class Player:
        def __init__(self, name):
            self.name = name
            self.hp = 100
            self.inventory = Inventory()
            self.status = "alive"

        def take_damage(self, amount):
            self.hp -= amount
            if self.hp <= 0:
                self.status = "dead"

        def pickup(self, item):
            self.inventory.add(item)

        def __repr__(self):
            return f"Player({self.name!r}, hp={self.hp}, {self.inventory})"

    def game_sim():
        player = Player("Alice")
        player.pickup("sword")
        player.pickup("shield")
        player.take_damage(30)
        player.inventory.add("potion", 3)
        player.take_damage(80)
        return player

    # Register all nested code objects
    for cls in [Inventory, Player]:
        for name, method in vars(cls).items():
            if callable(method) and hasattr(method, '__code__'):
                register_code_full(method.__code__)

    show_analysis(game_sim)
    result, stats = run_traced(game_sim)
    print(f"  Result: {result}")
    print(f"  Stats: events={stats['events']}, vars_checked={stats['vars_checked']}, "
          f"changed={stats['vars_changed']}")

    check("player created", result.name == "Alice")
    check("damage applied", result.hp == -10)
    check("status dead", result.status == "dead")
    check("inventory correct", result.inventory.items == ["sword", "shield", "potion"])

    # Key: did the C extension detect mutations inside method calls?
    check("C extension detected var changes across methods",
          stats['vars_changed'] > 5,
          f"changed={stats['vars_changed']}")


# ============================================================================
# Test G: Pass-by-reference mutation
# ============================================================================

def test_pass_by_ref():
    print("\n=== Test G: Pass-by-reference mutation (C extension) ===")

    def mutate_list(lst):
        lst.append(99)

    def mutate_dict(d):
        d['injected'] = True

    class Box:
        def __repr__(self):
            return f"Box({self.__dict__})"

    def mutate_obj(obj):
        obj.mutated = True

    def caller():
        my_list = [1, 2, 3]
        my_dict = {'a': 1}
        my_obj = Box()
        mutate_list(my_list)
        mutate_dict(my_dict)
        mutate_obj(my_obj)
        return my_list, my_dict, my_obj

    # Register all functions
    for fn in [caller, mutate_list, mutate_dict, mutate_obj]:
        register_code_full(fn.__code__)

    # Show analysis for callees
    for fn in [mutate_list, mutate_dict, mutate_obj]:
        show_analysis(fn)

    result, stats = run_traced(caller)
    print(f"  Result: {result}")
    print(f"  Stats: vars_checked={stats['vars_checked']}, changed={stats['vars_changed']}")

    check("pass-by-ref list mutated", result[0] == [1, 2, 3, 99])
    check("pass-by-ref dict mutated", result[1] == {'a': 1, 'injected': True})
    check("pass-by-ref obj mutated", result[2].mutated is True)
    check("C extension detected callee mutations", stats['vars_changed'] > 3,
          f"changed={stats['vars_changed']}")


# ============================================================================
# Test H: Read-only methods — no false positives
# ============================================================================

def test_no_false_positives():
    print("\n=== Test H: Read-only methods — no false positives ===")

    def readonly():
        items = [3, 1, 4, 1, 5]
        c = items.count(1)
        i = items.index(4)
        cp = items.copy()
        s = {1, 2, 3}
        d = s.difference({1})
        u = s.union({4})
        b = s.issubset({1, 2, 3, 4})
        return c, i, len(cp), len(d), len(u), b

    bitmasks = analyze_mutations_full(readonly.__code__)
    varnames = readonly.__code__.co_varnames

    # Check that lines with ONLY read-only method calls on items/s are NOT flagged.
    # Lines with STORE_FAST (initial assignments, result vars) ARE correctly flagged.
    # We want to verify: no line is flagged SOLELY because of a read-only method call.
    #
    # Strategy: look at lines that have method calls on items/s but no STORE_FAST
    # for items/s. These should have bitmask=0 for items/s.
    init_lines = set()  # lines that do STORE_FAST for items or s
    method_only_lines = {}  # lines that call methods on items/s but don't store to them

    for instr in dis.get_instructions(readonly):
        line = instr.positions.lineno
        if line is None:
            continue
        if instr.opcode in STORE_FAST_OPS and instr.argval in ('items', 's'):
            init_lines.add(line)

    items_idx = list(varnames).index('items') if 'items' in varnames else -1
    s_idx = list(varnames).index('s') if 's' in varnames else -1

    false_positives = []
    for line in sorted(bitmasks):
        if line in init_lines:
            continue  # STORE_FAST is correct
        mask = bitmasks[line]
        if items_idx >= 0 and (mask & (1 << items_idx)):
            false_positives.append((line, 'items'))
        if s_idx >= 0 and (mask & (1 << s_idx)):
            false_positives.append((line, 's'))

    print(f"  False positives (items/s flagged on non-STORE lines): {false_positives}")
    check("no false positives on read-only methods",
          len(false_positives) == 0,
          f"falsely flagged: {false_positives}")

    # Verify it runs correctly
    result, stats = run_traced(readonly)
    check("readonly result correct", result == (2, 2, 5, 2, 4, True))


# ============================================================================
# Test I: Augmented assignment
# ============================================================================

def test_augmented():
    print("\n=== Test I: Augmented assignment (C extension) ===")

    def augmented():
        x = 5
        x += 3
        items = [1, 2]
        items += [3, 4]
        d = {'a': 1}
        d |= {'b': 2}
        s = {1, 2}
        s |= {3}
        s -= {1}
        s &= {2, 3}
        return x, items, d, s

    show_analysis(augmented)
    result, stats = run_traced(augmented)
    print(f"  Result: {result}")
    print(f"  Stats: vars_checked={stats['vars_checked']}, changed={stats['vars_changed']}")
    check("augmented result correct",
          result == (8, [1, 2, 3, 4], {'a': 1, 'b': 2}, {2, 3}))
    check("augmented vars detected", stats['vars_changed'] > 0)


# ============================================================================
# Main
# ============================================================================

def main():
    _ctrace2.clear()

    test_list()
    test_dict()
    test_set()
    test_deque()
    test_subscr_attr()
    test_user_class()
    test_pass_by_ref()
    test_no_false_positives()
    test_augmented()

    final_stats = _ctrace2.stats()

    print(f"\n{'='*60}")
    print(f"Results: {passed} passed, {failed} failed")
    print(f"{'='*60}")
    print(f"\nC extension cumulative stats:")
    print(f"  Registered code objects: {final_stats['registered_codes']}")
    print(f"  Total events: {final_stats['events']:,}")
    print(f"  Line events: {final_stats['line_events']:,}")
    print(f"  Lines with writes: {final_stats['lines_with_writes']:,}")
    print(f"  Vars checked: {final_stats['vars_checked']:,}")
    print(f"  Vars changed: {final_stats['vars_changed']:,}")

    if errors:
        print("\nFailures:")
        for e in errors:
            print(f"  {e}")

    return failed == 0


if __name__ == '__main__':
    success = main()
    sys.exit(0 if success else 1)
