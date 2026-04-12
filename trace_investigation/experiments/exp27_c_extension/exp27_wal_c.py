"""
Experiment 27: WAL C Extension — Correctness + Performance

Tests the C WAL extension against the same scenarios as exp25/exp26,
then measures performance against ctrace2 selective and baseline.
"""
import sys
import os
import dis
import opcode
import types
import time
import statistics
import collections

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '..', 'exp7_c_extension'))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '..', 'exp10_c_extension'))

import _ctrace_wal
import _ctrace   # for comparison (noop, GetLocals)
import _ctrace2  # for comparison (selective)

# Mutation type constants (must match C enum)
MUT_STORE_FAST = 1
MUT_STORE_SUBSCR = 2
MUT_STORE_ATTR = 3
MUT_DELETE_SUBSCR = 4
MUT_DELETE_ATTR = 5
MUT_METHOD_CALL = 6

# Arg source constants (must match C enum)
ARG_NONE = 0
ARG_CONST_INT = 1
ARG_CONST_FLOAT = 2
ARG_CONST_STR = 3
ARG_CONST_NONE = 4
ARG_CONST_BOOL = 5
ARG_LOCAL = 6
ARG_BUILD = 7
ARG_EXPR = 8


# ============================================================================
# Bytecode analyzer — produces mutation info for the C extension
# ============================================================================

STORE_FAST_OPS = set()
for name, op in opcode.opmap.items():
    if 'STORE_FAST' in name or name == 'STORE_NAME' or name == 'STORE_DEREF':
        STORE_FAST_OPS.add(op)

STORE_SUBSCR_OP = opcode.opmap.get('STORE_SUBSCR')
STORE_ATTR_OP = opcode.opmap.get('STORE_ATTR')
DELETE_SUBSCR_OP = opcode.opmap.get('DELETE_SUBSCR')
DELETE_ATTR_OP = opcode.opmap.get('DELETE_ATTR')

KNOWN_MUTATING_METHODS = {
    'append', 'clear', 'extend', 'insert', 'pop', 'remove', 'reverse', 'sort',
    'add', 'discard', 'difference_update', 'intersection_update',
    'symmetric_difference_update', 'update',
    'appendleft', 'extendleft', 'popleft', 'rotate',
    'setdefault', 'popitem',
}

FUSED_LOAD_OPS = {'LOAD_FAST_BORROW_LOAD_FAST_BORROW',
                  'LOAD_FAST_LOAD_FAST', 'STORE_FAST_LOAD_FAST'}


def analyze_for_wal(code):
    """Analyze code object and produce data in the format register_code expects.

    Returns: (line_data_list, string_list)
      line_data_list: [(line, bitmask, mutations_list), ...]
      string_list: [str, ...]
    """
    instructions = list(dis.get_instructions(code))
    varnames = code.co_varnames
    varname_to_idx = {name: i for i, name in enumerate(varnames) if i < 64}

    # String table
    string_table = []
    string_index = {}

    def intern_str(s):
        if s in string_index:
            return string_index[s]
        idx = len(string_table)
        string_table.append(s)
        string_index[s] = idx
        return idx

    # Pre-intern all varnames
    for name in varnames:
        intern_str(name)

    line_bitmasks = {}
    line_mutations = {}  # line -> [(type, target_idx, attr_str_idx, deferred, n_chain, chain, args)]

    def set_bit(line, varname):
        idx = varname_to_idx.get(varname)
        if idx is not None:
            line_bitmasks[line] = line_bitmasks.get(line, 0) | (1 << idx)

    def get_loaded_locals(instr):
        if instr.opname in FUSED_LOAD_OPS:
            return list(instr.argval) if isinstance(instr.argval, tuple) else []
        if 'LOAD_FAST' in instr.opname:
            return [instr.argval]
        return []

    def find_target_with_chain(idx):
        """Walk back from idx to find LOAD_FAST + optional LOAD_ATTR chain."""
        chain = []
        j = idx - 1
        while j >= 0:
            instr = instructions[j]
            if instr.opname == 'LOAD_ATTR':
                chain.insert(0, instr.argval)
                j -= 1
            elif 'LOAD_FAST' in instr.opname or instr.opname in ('LOAD_GLOBAL', 'LOAD_DEREF'):
                name = instr.argval
                if instr.opname in FUSED_LOAD_OPS and isinstance(instr.argval, tuple):
                    name = instr.argval[-1]
                return name, chain
            else:
                break
        return None, []

    def make_arg(instr):
        """Convert an instruction to an ArgSource tuple: (type, ival, fval, idx)."""
        if instr.opname in ('LOAD_SMALL_INT',):
            return (ARG_CONST_INT, instr.argval, 0.0, 0)
        if instr.opname == 'LOAD_CONST':
            v = instr.argval
            if v is None:
                return (ARG_CONST_NONE, 0, 0.0, 0)
            if isinstance(v, bool):
                return (ARG_CONST_BOOL, 1 if v else 0, 0.0, 0)
            if isinstance(v, int):
                return (ARG_CONST_INT, v, 0.0, 0)
            if isinstance(v, float):
                return (ARG_CONST_FLOAT, 0, v, 0)
            if isinstance(v, str):
                return (ARG_CONST_STR, 0, 0.0, intern_str(v))
            return (ARG_EXPR, 0, 0.0, 0)
        if 'LOAD_FAST' in instr.opname:
            name = instr.argval
            if instr.opname in FUSED_LOAD_OPS and isinstance(instr.argval, tuple):
                # Take first for value, second is target
                name = instr.argval[0]
            idx = varname_to_idx.get(name, 0)
            return (ARG_LOCAL, 0, 0.0, idx)
        if instr.opname.startswith('BUILD_'):
            return (ARG_BUILD, 0, 0.0, 0)
        return (ARG_EXPR, 0, 0.0, 0)

    def add_mutation(line, mut):
        if line not in line_mutations:
            line_mutations[line] = []
        line_mutations[line].append(mut)

    i = 0
    while i < len(instructions):
        instr = instructions[i]
        line = instr.positions.lineno
        if line is None:
            i += 1
            continue

        # STORE_FAST
        if instr.opcode in STORE_FAST_OPS:
            set_bit(line, instr.argval)

        # STORE_SUBSCR: stack is [value, container, key] → STORE_SUBSCR
        # Bytecode patterns:
        #   LOAD_FAST/CONST val → LOAD_FAST container → LOAD_CONST/FAST key → STORE_SUBSCR
        #   LOAD_FAST_BORROW_LOAD_FAST_BORROW (val, container) → LOAD_CONST key → STORE_SUBSCR
        elif instr.opcode == STORE_SUBSCR_OP:
            # Key is loaded right before STORE_SUBSCR
            key_arg = (ARG_EXPR, 0, 0.0, 0)
            if i >= 1:
                prev = instructions[i - 1]
                if prev.opname in ('LOAD_SMALL_INT', 'LOAD_CONST'):
                    key_arg = make_arg(prev)
                elif 'LOAD_FAST' in prev.opname:
                    name = prev.argval
                    if prev.opname in FUSED_LOAD_OPS and isinstance(prev.argval, tuple):
                        name = prev.argval[-1]
                    if name in varname_to_idx:
                        key_arg = (ARG_LOCAL, 0, 0.0, varname_to_idx[name])

            # Container is loaded before key — find via chain analysis
            # Start searching before the key instruction
            target_name = None
            chain = []
            for j in range(i - 2, max(0, i - 5) - 1, -1):
                pj = instructions[j]
                if pj.positions.lineno != line:
                    break
                if pj.opname == 'LOAD_ATTR':
                    chain.insert(0, pj.argval)
                elif 'LOAD_FAST' in pj.opname:
                    name = pj.argval
                    if pj.opname in FUSED_LOAD_OPS and isinstance(pj.argval, tuple):
                        name = pj.argval[-1]
                    target_name = name
                    break
                elif pj.opname in ('LOAD_GLOBAL', 'LOAD_DEREF'):
                    target_name = pj.argval
                    break

            if target_name:
                set_bit(line, target_name)
                tidx = varname_to_idx.get(target_name, 0)
                chain_idx = tuple(intern_str(a) for a in chain)

                # Value is loaded before container. Scan backwards from container load.
                val_arg = (ARG_EXPR, 0, 0.0, 0)
                # Find where the container was loaded
                container_instr_idx = i - 2 - len(chain)
                for j in range(container_instr_idx - 1, max(0, container_instr_idx - 3) - 1, -1):
                    pj = instructions[j]
                    if pj.positions.lineno != line:
                        break
                    if pj.opname in ('LOAD_SMALL_INT', 'LOAD_CONST'):
                        val_arg = make_arg(pj)
                        break
                    if 'LOAD_FAST' in pj.opname:
                        name = pj.argval
                        if pj.opname in FUSED_LOAD_OPS and isinstance(pj.argval, tuple):
                            # Fused: first is value, second is container
                            name = pj.argval[0]
                        if name != target_name and name in varname_to_idx:
                            val_arg = (ARG_LOCAL, 0, 0.0, varname_to_idx[name])
                            break
                    if pj.opname.startswith('BUILD_'):
                        val_arg = (ARG_BUILD, 0, 0.0, 0)
                        break

                needs_deferred = 1 if val_arg[0] in (ARG_BUILD, ARG_EXPR) else 0
                add_mutation(line, (MUT_STORE_SUBSCR, tidx, 0, needs_deferred,
                                   len(chain_idx), chain_idx, [key_arg, val_arg]))

        # STORE_ATTR: stack is [value, target] → STORE_ATTR attr
        # Bytecode patterns:
        #   LOAD_FAST_BORROW_LOAD_FAST_BORROW (val, target) → STORE_ATTR  (fused)
        #   LOAD_SMALL_INT val → LOAD_FAST target → STORE_ATTR             (const value)
        #   LOAD_CONST val → LOAD_FAST target → STORE_ATTR                 (const value)
        #   LOAD_FAST val → LOAD_FAST target → STORE_ATTR                  (local value)
        elif instr.opcode == STORE_ATTR_OP:
            target_name, chain = find_target_with_chain(i)
            if target_name:
                set_bit(line, target_name)
                tidx = varname_to_idx.get(target_name, 0)
                attr_idx = intern_str(instr.argval)

                # The value is loaded BEFORE the target. Only look at instructions
                # on the SAME line, and stop at the target load.
                val_arg = (ARG_EXPR, 0, 0.0, 0)

                # Check for fused instruction (value + target in one op)
                if i >= 1:
                    prev = instructions[i - 1]
                    if prev.opname in FUSED_LOAD_OPS and isinstance(prev.argval, tuple):
                        # First element is value, second is target
                        val_name = prev.argval[0]
                        if val_name != target_name and val_name in varname_to_idx:
                            val_arg = (ARG_LOCAL, 0, 0.0, varname_to_idx[val_name])

                # If not fused, look for value instruction before the target load
                if val_arg[0] == ARG_EXPR:
                    # Target is loaded at i-1 (LOAD_FAST target).
                    # Value is at i-2 (or earlier, same line only).
                    for j in range(i - 2, max(0, i - 4) - 1, -1):
                        pj = instructions[j]
                        # Don't cross line boundaries
                        if pj.positions.lineno != line:
                            break
                        # Don't cross other STORE_ATTR/STORE_SUBSCR
                        if pj.opcode in (STORE_ATTR_OP, STORE_SUBSCR_OP):
                            break
                        if pj.opname in ('LOAD_SMALL_INT', 'LOAD_CONST'):
                            val_arg = make_arg(pj)
                            break
                        if 'LOAD_FAST' in pj.opname:
                            name = pj.argval
                            if pj.opname in FUSED_LOAD_OPS and isinstance(pj.argval, tuple):
                                name = pj.argval[0]
                            if name != target_name and name in varname_to_idx:
                                val_arg = (ARG_LOCAL, 0, 0.0, varname_to_idx[name])
                                break
                        if pj.opname.startswith('BUILD_'):
                            val_arg = (ARG_BUILD, 0, 0.0, 0)
                            break

                needs_deferred = 1 if val_arg[0] in (ARG_BUILD, ARG_EXPR) else 0
                add_mutation(line, (MUT_STORE_ATTR, tidx, attr_idx, needs_deferred,
                                   0, (), [val_arg]))

        # DELETE_SUBSCR
        elif instr.opcode == DELETE_SUBSCR_OP:
            key_arg = make_arg(instructions[i-1]) if i >= 1 else (ARG_EXPR, 0, 0.0, 0)
            target_name, chain = find_target_with_chain(i - 1)
            if target_name:
                set_bit(line, target_name)
                tidx = varname_to_idx.get(target_name, 0)
                chain_idx = tuple(intern_str(a) for a in chain)
                add_mutation(line, (MUT_DELETE_SUBSCR, tidx, 0, 0,
                                   len(chain_idx), chain_idx, [key_arg]))

        # DELETE_ATTR
        elif instr.opcode == DELETE_ATTR_OP:
            target_name, _ = find_target_with_chain(i)
            if target_name:
                set_bit(line, target_name)
                tidx = varname_to_idx.get(target_name, 0)
                attr_idx = intern_str(instr.argval)
                add_mutation(line, (MUT_DELETE_ATTR, tidx, attr_idx, 0, 0, (), []))

        # Method mutation
        elif (instr.opname == 'LOAD_ATTR' and instr.argval in KNOWN_MUTATING_METHODS):
            target_name, chain = find_target_with_chain(i)
            if target_name:
                set_bit(line, target_name)
                tidx = varname_to_idx.get(target_name, 0)
                method_idx = intern_str(instr.argval)
                chain_idx = tuple(intern_str(a) for a in chain)
                # Collect args between LOAD_ATTR and CALL
                args = []
                j = i + 1
                while j < len(instructions):
                    aj = instructions[j]
                    if aj.opname == 'CALL':
                        break
                    if aj.opname in ('LOAD_SMALL_INT', 'LOAD_CONST'):
                        args.append(make_arg(aj))
                    elif 'LOAD_FAST' in aj.opname:
                        args.append(make_arg(aj))
                    elif aj.opname.startswith('BUILD_'):
                        args.append((ARG_BUILD, 0, 0.0, 0))
                    elif aj.opname in ('PUSH_NULL', 'COPY', 'LIST_EXTEND',
                                      'SET_UPDATE', 'DICT_UPDATE', 'DICT_MERGE', 'POP_TOP'):
                        pass
                    else:
                        args.append((ARG_EXPR, 0, 0.0, 0))
                    j += 1
                add_mutation(line, (MUT_METHOD_CALL, tidx, method_idx, 0,
                                   len(chain_idx), chain_idx, args[:4]))

        i += 1

    # Build line_data_list
    all_lines = set(line_bitmasks.keys()) | set(line_mutations.keys())
    line_data = []
    for ln in sorted(all_lines):
        bitmask = line_bitmasks.get(ln, 0)
        muts = line_mutations.get(ln, [])
        line_data.append((ln, bitmask, muts))

    return line_data, string_table


def register_code_wal(code, visited=None):
    if visited is None:
        visited = set()
    if id(code) in visited:
        return
    visited.add(id(code))

    line_data, strings = analyze_for_wal(code)
    _ctrace_wal.register_code(code, code.co_firstlineno, line_data, strings)

    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            register_code_wal(const, visited)


# Also register for ctrace2 (for comparison)
STORE_OPS_C2 = set()
for name, op in opcode.opmap.items():
    if 'STORE_FAST' in name or name == 'STORE_NAME' or name == 'STORE_DEREF':
        STORE_OPS_C2.add(op)

def register_code_c2(code, visited=None):
    if visited is None:
        visited = set()
    if id(code) in visited:
        return
    visited.add(id(code))
    varname_to_idx = {name: i for i, name in enumerate(code.co_varnames) if i < 64}
    line_bitmasks = {}
    for instr in dis.get_instructions(code):
        if instr.opcode in STORE_OPS_C2 and instr.positions.lineno is not None:
            idx = varname_to_idx.get(instr.argval)
            if idx is not None:
                ln = instr.positions.lineno
                line_bitmasks[ln] = line_bitmasks.get(ln, 0) | (1 << idx)
    packed = [(ln, mask) for ln, mask in line_bitmasks.items()]
    _ctrace2.register_code(code, code.co_firstlineno, packed)
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            register_code_c2(const, visited)


# ============================================================================
# Correctness tests
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


def test_list_mutations():
    print("\n=== Test 1: List mutations ===")
    def target():
        items = [1, 2, 3]
        items.append(4)
        items.insert(0, 0)
        items[2] = 99
        del items[1]
        items.clear()
        return items

    _ctrace_wal.clear()
    register_code_wal(target.__code__)
    _ctrace_wal.start()
    result = target()
    _ctrace_wal.stop()

    wal = _ctrace_wal.get_wal(100)
    creates = [e for e in wal if e['event'] == 'CREATE']
    mutates = [e for e in wal if e['event'] == 'MUTATE']
    setitems = [e for e in wal if e['event'] == 'SETITEM']
    delitems = [e for e in wal if e['event'] == 'DELITEM']

    print(f"  Result: {result}, WAL entries: {len(wal)}")
    for e in wal:
        if e['event'] in ('CREATE', 'MUTATE', 'SETITEM', 'DELITEM'):
            print(f"    {e}")

    appends = [e for e in mutates if e.get('method') == 'append']
    check("append(4) captured", any(4 in (e.get('args') or []) for e in appends),
          f"appends: {appends}")
    inserts = [e for e in mutates if e.get('method') == 'insert']
    check("insert(0,0) captured", len(inserts) >= 1)
    check("SETITEM captured", len(setitems) >= 1)
    check("DELITEM captured", len(delitems) >= 1)
    clears = [e for e in mutates if e.get('method') == 'clear']
    check("clear() captured", len(clears) >= 1)


def test_object_attrs():
    print("\n=== Test 2: Object attribute mutations ===")
    class Player:
        def __init__(self, name):
            self.name = name
            self.hp = 100

    def target():
        p = Player("Alice")
        p.hp = 75
        p.hp = 50
        return p

    _ctrace_wal.clear()
    register_code_wal(target.__code__)
    register_code_wal(Player.__init__.__code__)
    _ctrace_wal.start()
    result = target()
    _ctrace_wal.stop()

    wal = _ctrace_wal.get_wal(100)
    setattrs = [e for e in wal if e['event'] == 'SETATTR']
    hp_sets = [e for e in setattrs if e.get('attr') == 'hp']

    print(f"  WAL entries: {len(wal)}")
    for e in wal:
        if e['event'] in ('CREATE', 'SETATTR'):
            print(f"    {e}")

    check("hp mutations captured", len(hp_sets) >= 2, f"got {len(hp_sets)}")
    name_sets = [e for e in setattrs if e.get('attr') == 'name']
    check("name attr captured", len(name_sets) >= 1)


def test_attr_chain():
    print("\n=== Test 3: Attribute chain mutations ===")
    class Container:
        pass

    def target():
        c = Container()
        c.items = []
        c.items.append(1)
        c.items.append(2)
        return c

    _ctrace_wal.clear()
    register_code_wal(target.__code__)
    _ctrace_wal.start()
    result = target()
    _ctrace_wal.stop()

    wal = _ctrace_wal.get_wal(100)
    mutates = [e for e in wal if e['event'] == 'MUTATE']

    print(f"  WAL entries: {len(wal)}")
    for e in wal:
        if e['event'] in ('CREATE', 'SETATTR', 'MUTATE'):
            print(f"    {e}")

    appends = [e for e in mutates if e.get('method') == 'append']
    check("c.items.append() captured via chain", len(appends) >= 2,
          f"got {len(appends)}")


def test_aliasing():
    print("\n=== Test 4: Aliasing ===")
    def target():
        a = [1, 2, 3]
        b = a
        return a, b

    _ctrace_wal.clear()
    register_code_wal(target.__code__)
    _ctrace_wal.start()
    result = target()
    _ctrace_wal.stop()

    wal = _ctrace_wal.get_wal(100)
    binds = [e for e in wal if e['event'] == 'BIND']
    a_binds = [e for e in binds if e.get('name') == 'a']
    b_binds = [e for e in binds if e.get('name') == 'b']

    if a_binds and b_binds:
        check("a and b share same oid", a_binds[-1]['oid'] == b_binds[-1]['oid'],
              f"a_oid={a_binds[-1]['oid']}, b_oid={b_binds[-1]['oid']}")
    else:
        check("found binds", False)


def test_variable_args():
    print("\n=== Test 5: Variable arguments resolved from locals ===")
    def target():
        items = []
        x = 42
        items.append(x)
        name = "hello"
        items.append(name)
        return items

    _ctrace_wal.clear()
    register_code_wal(target.__code__)
    _ctrace_wal.start()
    result = target()
    _ctrace_wal.stop()

    wal = _ctrace_wal.get_wal(100)
    mutates = [e for e in wal if e['event'] == 'MUTATE']
    for e in mutates:
        print(f"    {e}")

    check("append(x=42) resolved",
          any(42 in (e.get('args') or []) for e in mutates),
          f"mutates: {[e.get('args') for e in mutates]}")
    check("append(name='hello') resolved",
          any('hello' in (e.get('args') or []) for e in mutates),
          f"mutates: {[e.get('args') for e in mutates]}")


def test_full_lifecycle():
    print("\n=== Test 6: Full WAL lifecycle ===")
    class Task:
        def __init__(self, name):
            self.name = name
            self.done = False

    def target():
        tasks = []
        t = Task("first")
        tasks.append(t)
        t.done = True
        return tasks

    _ctrace_wal.clear()
    register_code_wal(target.__code__)
    register_code_wal(Task.__init__.__code__)
    _ctrace_wal.start()
    result = target()
    _ctrace_wal.stop()

    wal = _ctrace_wal.get_wal(200)
    by_type = collections.Counter(e['event'] for e in wal)

    print(f"  WAL entries: {len(wal)}, by type: {dict(by_type)}")
    for e in wal:
        print(f"    {e}")

    check("has CREATE", by_type.get('CREATE', 0) >= 2)
    check("has BIND", by_type.get('BIND', 0) >= 1)
    check("has MUTATE", by_type.get('MUTATE', 0) >= 1)
    check("has SETATTR", by_type.get('SETATTR', 0) >= 1)
    check("has UNBIND", by_type.get('UNBIND', 0) >= 1)


# ============================================================================
# Performance tests
# ============================================================================

def measure(workload_fn, n_warmup=3, n_rounds=10, iters_per_round=None):
    if iters_per_round is None:
        start = time.perf_counter_ns()
        workload_fn()
        single = time.perf_counter_ns() - start
        iters_per_round = max(1, 50_000_000 // max(single, 1))
        iters_per_round = min(iters_per_round, 50)
    for _ in range(n_warmup):
        for _ in range(iters_per_round):
            workload_fn()
    times = []
    for _ in range(n_rounds):
        start = time.perf_counter_ns()
        for _ in range(iters_per_round):
            workload_fn()
        times.append((time.perf_counter_ns() - start) // iters_per_round)
    return statistics.median(times)


def test_performance():
    print("\n=== Performance comparison ===")

    from workloads_large import LARGE_WORKLOADS

    workload_names = list(LARGE_WORKLOADS.keys())

    # Register all code for both extensions
    _ctrace_wal.clear()
    _ctrace2.clear()

    import workloads_large
    for name, fn in vars(workloads_large).items():
        if callable(fn) and hasattr(fn, '__code__'):
            register_code_wal(fn.__code__)
            register_code_c2(fn.__code__)
    for name, fn in LARGE_WORKLOADS.items():
        if hasattr(fn, '__code__'):
            register_code_wal(fn.__code__)
            register_code_c2(fn.__code__)

    configs = [
        ('baseline', 'Baseline', lambda: None, lambda: None),
        ('c_noop', 'C noop', lambda: _ctrace.start(0), lambda: _ctrace.stop()),
        ('c_selective', 'C selective', lambda: _ctrace2.start(1), lambda: _ctrace2.stop()),
        ('c_wal', 'C WAL', lambda: _ctrace_wal.start(), lambda: _ctrace_wal.stop()),
    ]

    results = {k: {} for k, _, _, _ in configs}

    for cfg_key, cfg_label, setup, teardown in configs:
        print(f"  Running {cfg_label}...")
        for name in workload_names:
            fn = LARGE_WORKLOADS[name]
            setup()
            med = measure(fn)
            teardown()
            results[cfg_key][name] = med

    # Print
    categories = {
        'comp_': 'compute', 'io_': 'io', 'mem_': 'memory',
        'yield_': 'yield', 'async_': 'async',
    }

    print(f"\n{'Workload':<20} {'Cat':<8} {'C noop':>10} {'C select':>10} {'C WAL':>10}")
    print("-" * 60)
    for name in workload_names:
        cat = next((v for k, v in categories.items() if name.startswith(k)), '?')
        b = results['baseline'][name]
        cn = results['c_noop'][name] / b
        cs = results['c_selective'][name] / b
        cw = results['c_wal'][name] / b
        print(f"{name:<20} {cat:<8} {cn:>9.2f}x {cs:>9.2f}x {cw:>9.2f}x")

    # Category averages
    print()
    cat_wl = {}
    for name in workload_names:
        cat = next((v for k, v in categories.items() if name.startswith(k)), '?')
        cat_wl.setdefault(cat, []).append(name)

    print(f"{'Category':<12} {'#':>3} {'C noop':>10} {'C select':>10} {'C WAL':>10}")
    print("-" * 48)
    for cat, names in cat_wl.items():
        n = len(names)
        cn = sum(results['c_noop'][nm] / results['baseline'][nm] for nm in names) / n
        cs = sum(results['c_selective'][nm] / results['baseline'][nm] for nm in names) / n
        cw = sum(results['c_wal'][nm] / results['baseline'][nm] for nm in names) / n
        print(f"{cat:<12} {n:>3} {cn:>9.2f}x {cs:>9.2f}x {cw:>9.2f}x")

    all_cn = sum(results['c_noop'][n] / results['baseline'][n] for n in workload_names) / len(workload_names)
    all_cs = sum(results['c_selective'][n] / results['baseline'][n] for n in workload_names) / len(workload_names)
    all_cw = sum(results['c_wal'][n] / results['baseline'][n] for n in workload_names) / len(workload_names)
    print(f"{'ALL':<12} {len(workload_names):>3} {all_cn:>9.2f}x {all_cs:>9.2f}x {all_cw:>9.2f}x")

    # WAL stats
    _ctrace_wal.start()
    for name in workload_names:
        LARGE_WORKLOADS[name]()
    _ctrace_wal.stop()
    stats = _ctrace_wal.stats()
    print(f"\n  WAL stats (all workloads, single iteration):")
    for k, v in stats.items():
        print(f"    {k}: {v:,}" if isinstance(v, int) else f"    {k}: {v}")


# ============================================================================
# Main
# ============================================================================

def main():
    test_list_mutations()
    test_object_attrs()
    test_attr_chain()
    test_aliasing()
    test_variable_args()
    test_full_lifecycle()

    print(f"\n{'='*60}")
    print(f"Correctness: {passed} passed, {failed} failed")
    print(f"{'='*60}")
    if errors:
        print("\nFailures:")
        for e in errors:
            print(f"  {e}")
        return

    test_performance()


if __name__ == '__main__':
    main()
