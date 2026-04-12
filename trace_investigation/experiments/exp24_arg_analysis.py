"""
Experiment 24: Static Analysis of Mutation Method Arguments

For each mutation call pattern (LOAD_FAST target → LOAD_ATTR method → args → CALL),
analyze the argument-loading instructions to determine if we can resolve the
argument values at trace time.

Categories of arguments:
  A) LOAD_CONST / LOAD_SMALL_INT — literal, known at analysis time
  B) LOAD_FAST — local variable, readable via PyFrame_GetVar at trace time
  C) LOAD_GLOBAL — global variable, readable from frame.f_globals
  D) BINARY_OP / BUILD_* — computed expression, needs stack simulation
  E) CALL (nested) — function call result, cannot resolve statically

For A and B (and often C), we can record in our pre-computed map:
  "On this line, method X is called on variable Y with arg from local Z"
Then the C extension reads both Y and Z after execution.

Tests this against real-world code patterns and measures coverage.
"""
import sys
import os
import dis
import opcode
import collections

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# ============================================================================
# Argument resolution analyzer
# ============================================================================

KNOWN_MUTATING_METHODS = {
    'append', 'clear', 'extend', 'insert', 'pop', 'remove', 'reverse', 'sort',
    'add', 'discard', 'difference_update', 'intersection_update',
    'symmetric_difference_update', 'update',
    'appendleft', 'extendleft', 'popleft', 'rotate',
    'resize', 'setdefault', 'popitem',
}


def analyze_mutation_args(code):
    """Analyze mutation call patterns and determine if arguments are resolvable.

    Returns list of:
      {
        'line': int,
        'target_var': str,       # the local being mutated
        'method': str,           # method name
        'args': [                # list of argument descriptors
          {'type': 'const', 'value': ...},
          {'type': 'local', 'name': ...},
          {'type': 'global', 'name': ...},
          {'type': 'expr', 'desc': ...},     # computed, can't resolve statically
          {'type': 'call', 'desc': ...},     # nested call result
        ],
        'resolvable': bool,      # can we read all args at trace time?
      }
    """
    instructions = list(dis.get_instructions(code))
    results = []

    i = 0
    while i < len(instructions):
        instr = instructions[i]

        # Look for: LOAD_FAST target → LOAD_ATTR known_method
        if ('LOAD_FAST' in instr.opname and
            i + 1 < len(instructions) and
            instructions[i + 1].opname == 'LOAD_ATTR' and
            instructions[i + 1].argval in KNOWN_MUTATING_METHODS):

            target_var = instr.argval
            method = instructions[i + 1].argval
            line = instr.positions.lineno

            # Find the CALL instruction that ends this method call
            # Walk forward from LOAD_ATTR, collecting arg-loading instructions
            j = i + 2
            args = []
            call_found = False
            stack_depth = 0  # track nested calls

            while j < len(instructions):
                arg_instr = instructions[j]

                if arg_instr.opname == 'CALL' and stack_depth == 0:
                    call_found = True
                    n_args = arg_instr.argval  # number of positional args
                    break

                # Track nested CALLs
                if arg_instr.opname == 'CALL':
                    stack_depth -= 1
                    args.append({'type': 'call', 'desc': 'nested call result'})
                    j += 1
                    continue

                # Categorize the instruction
                if arg_instr.opname in ('LOAD_SMALL_INT',):
                    args.append({'type': 'const', 'value': arg_instr.argval})
                elif arg_instr.opname == 'LOAD_CONST':
                    args.append({'type': 'const', 'value': arg_instr.argval})
                elif 'LOAD_FAST' in arg_instr.opname:
                    args.append({'type': 'local', 'name': arg_instr.argval})
                elif arg_instr.opname in ('LOAD_GLOBAL',):
                    args.append({'type': 'global', 'name': arg_instr.argval})
                elif arg_instr.opname in ('LOAD_DEREF',):
                    args.append({'type': 'closure', 'name': arg_instr.argval})
                elif arg_instr.opname.startswith('BUILD_'):
                    # BUILD_MAP, BUILD_LIST, BUILD_SET, BUILD_TUPLE
                    # These consume the previous N stack entries
                    # The args we collected are inputs to this BUILD
                    args.append({'type': 'build', 'op': arg_instr.opname,
                                'count': arg_instr.argval})
                elif arg_instr.opname in ('BINARY_OP', 'BINARY_SUBSCR',
                                          'COMPARE_OP', 'CONTAINS_OP'):
                    args.append({'type': 'expr', 'desc': arg_instr.opname})
                elif arg_instr.opname in ('LIST_EXTEND', 'SET_UPDATE',
                                          'DICT_UPDATE', 'DICT_MERGE'):
                    # These are used to unpack into BUILD_* results
                    pass  # Don't add another arg
                elif arg_instr.opname.startswith('LOAD_ATTR'):
                    # Attribute access — could be method lookup for nested call
                    if (j + 1 < len(instructions) and
                        instructions[j + 1].opname == 'CALL'):
                        stack_depth += 1
                    else:
                        args.append({'type': 'attr', 'name': arg_instr.argval})
                elif arg_instr.opname in ('PUSH_NULL', 'COPY'):
                    pass  # Internal ops, skip
                else:
                    args.append({'type': 'other', 'op': arg_instr.opname})

                j += 1

            if call_found:
                # Determine if all args are resolvable at trace time
                resolvable = all(
                    a['type'] in ('const', 'local', 'global', 'closure')
                    for a in args
                    if a['type'] not in ('build',)  # BUILD with const inputs is ok
                )

                # For BUILD_* with all-const inputs, it's resolvable
                # For BUILD_* with local/global inputs, it's resolvable at runtime

                results.append({
                    'line': line,
                    'target_var': target_var,
                    'method': method,
                    'args': args,
                    'resolvable': resolvable,
                })

        i += 1

    return results


# ============================================================================
# Test against realistic code patterns
# ============================================================================

def real_world_patterns():
    """Representative mutation patterns from real Python code."""

    # Pattern 1: Literal arguments (most common)
    def literal_args():
        items = []
        items.append(1)
        items.append("hello")
        items.insert(0, "first")
        d = {}
        d.update({"key": "val"})
        s = set()
        s.add(42)

    # Pattern 2: Variable arguments (very common)
    def variable_args():
        items = []
        for i in range(10):
            items.append(i)
        name = "test"
        d = {}
        d.update({name: i})
        s = set()
        s.add(i)

    # Pattern 3: Expression arguments (common)
    def expression_args():
        items = []
        x = 5
        items.append(x + 1)
        items.append(x * 2)
        items.insert(0, len(items))
        d = {}
        d.update({f"key_{x}": x**2})

    # Pattern 4: Nested call arguments (occasional)
    def nested_call_args():
        items = []
        items.append(len(items))
        items.append(max(1, 2, 3))
        items.append(int("42"))
        d = {}
        d.update(dict(a=1, b=2))

    # Pattern 5: Attribute/subscript arguments
    def attr_subscript_args():
        items = []
        other = [10, 20, 30]
        items.append(other[0])
        items.append(other[-1])
        class Obj:
            val = 99
        o = Obj()
        items.append(o.val)

    # Pattern 6: Complex/mixed
    def complex_args():
        items = []
        d = {"a": 1}
        items.append(d.get("a", 0))
        items.extend([x*2 for x in range(5)])
        items.insert(len(items) // 2, "middle")

    return [literal_args, variable_args, expression_args,
            nested_call_args, attr_subscript_args, complex_args]


def main():
    print("=" * 70)
    print("Experiment 24: Mutation Argument Static Analysis")
    print("=" * 70)

    all_results = []
    functions = real_world_patterns()

    for fn in functions:
        print(f"\n--- {fn.__name__} ---")
        results = analyze_mutation_args(fn.__code__)
        all_results.extend(results)

        for r in results:
            arg_descs = []
            for a in r['args']:
                if a['type'] == 'const':
                    arg_descs.append(f"const({a['value']!r})")
                elif a['type'] == 'local':
                    arg_descs.append(f"local({a['name']})")
                elif a['type'] == 'global':
                    arg_descs.append(f"global({a['name']})")
                elif a['type'] == 'closure':
                    arg_descs.append(f"closure({a['name']})")
                elif a['type'] == 'build':
                    arg_descs.append(f"{a['op']}({a['count']})")
                elif a['type'] == 'expr':
                    arg_descs.append(f"expr({a['desc']})")
                elif a['type'] == 'call':
                    arg_descs.append(f"call()")
                elif a['type'] == 'attr':
                    arg_descs.append(f"attr({a['name']})")
                else:
                    arg_descs.append(f"{a['type']}({a.get('op', '?')})")

            status = "✓ resolvable" if r['resolvable'] else "✗ NOT resolvable"
            print(f"  L{r['line']}: {r['target_var']}.{r['method']}({', '.join(arg_descs)})  [{status}]")

    # Summary statistics
    print(f"\n{'='*70}")
    print("SUMMARY")
    print(f"{'='*70}")

    total = len(all_results)
    resolvable = sum(1 for r in all_results if r['resolvable'])
    not_resolvable = total - resolvable

    print(f"\n  Total mutation calls analyzed: {total}")
    print(f"  Resolvable at trace time:      {resolvable} ({100*resolvable/max(total,1):.0f}%)")
    print(f"  NOT resolvable:                {not_resolvable} ({100*not_resolvable/max(total,1):.0f}%)")

    # Break down by argument type
    arg_type_counts = collections.Counter()
    for r in all_results:
        for a in r['args']:
            arg_type_counts[a['type']] += 1

    print(f"\n  Argument types seen:")
    for atype, count in arg_type_counts.most_common():
        print(f"    {atype:<12} {count:>4}")

    # Break down non-resolvable reasons
    print(f"\n  Non-resolvable calls:")
    for r in all_results:
        if not r['resolvable']:
            reasons = [a['type'] for a in r['args']
                      if a['type'] not in ('const', 'local', 'global', 'closure', 'build')]
            print(f"    {r['target_var']}.{r['method']}() — has: {reasons}")

    # What would the C extension need to capture for resolvable args?
    print(f"\n  For resolvable mutations, the C extension needs to read:")
    local_args = set()
    for r in all_results:
        if r['resolvable']:
            for a in r['args']:
                if a['type'] == 'local':
                    local_args.add(a['name'])
    print(f"    Local variables used as mutation args: {sorted(local_args)}")
    print(f"    Plus: constant values are in bytecode (free)")
    print(f"    Plus: global values readable from frame.f_globals")

    print(f"""
  ARCHITECTURE:
    Pre-computed map per line (in addition to variable-write bitmask):
      mutation_info[line] = {{
          'target': var_index,           # which local is being mutated
          'method': 'append',            # method name
          'args': [                      # argument sources
              {{'type': 'const', 'value': 4}},
              {{'type': 'local', 'index': 3}},    # read via PyFrame_GetVar
          ]
      }}

    At trace time (C extension):
      1. Check if line has mutation_info
      2. Read target variable (already doing this)
      3. Read argument variables via PyFrame_GetVar (same mechanism)
      4. Record WAL entry: MUTATE oid=N method=append args=[4]

    This means: for the common case (literal + local variable args),
    we can capture EXACT mutation arguments with the same PyFrame_GetVar
    mechanism we already use for variable changes.
""")


if __name__ == '__main__':
    main()
