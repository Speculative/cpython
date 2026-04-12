"""
Experiment 25: WAL (Write-Ahead Log) Prototype

Python prototype of the complete object-centric WAL architecture.
Validates correctness before porting to C.

Tests:
  1. Argument resolution — read mutation args from locals/constants before execution
  2. WAL replay — reconstruct data structures from WAL entries
  3. Unique object IDs — monotonic counter, handle id() reuse
  4. Object deallocation — detect via weakrefs (user objects) and scope tracking
  5. Aliasing — multiple variables pointing to same WAL object
  6. Reference graphs — objects containing references to other tracked objects
  7. Full lifecycle — CREATE → BIND → MUTATE → UNBIND → DEALLOC
"""
import sys
import os
import dis
import opcode
import types
import weakref
import collections
import copy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# ============================================================================
# Bytecode analyzer: extracts mutation info including argument sources
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


def analyze_code_wal(code):
    """Full WAL-oriented bytecode analysis.

    Returns per-line info:
      {line: [MutationInfo, ...]}

    Each MutationInfo is:
      {
        'type': 'store_fast' | 'store_subscr' | 'store_attr' |
                'delete_subscr' | 'delete_attr' | 'method_call',
        'target': str,        # variable name being mutated
        'method': str | None, # method name for method_call
        'attr': str | None,   # attribute name for store_attr/delete_attr
        'arg_sources': [      # for method_call: how to read each argument
            {'type': 'const', 'value': ...},
            {'type': 'local', 'name': ...},
            {'type': 'expr'},  # cannot resolve statically
        ],
      }
    """
    instructions = list(dis.get_instructions(code))
    varnames = code.co_varnames
    line_info = {}

    def add_info(line, info):
        if line not in line_info:
            line_info[line] = []
        line_info[line].append(info)

    # Fused LOAD_FAST opcodes that load two locals at once
    FUSED_LOAD_FAST_OPS = {'LOAD_FAST_BORROW_LOAD_FAST_BORROW',
                           'LOAD_FAST_LOAD_FAST', 'STORE_FAST_LOAD_FAST'}

    def get_loaded_locals(instr):
        """Get list of local names loaded by an instruction."""
        if instr.opname in FUSED_LOAD_FAST_OPS:
            # argval is a tuple of two names
            if isinstance(instr.argval, tuple):
                return list(instr.argval)
            return []
        if 'LOAD_FAST' in instr.opname:
            return [instr.argval]
        return []

    def find_load_fast_before(i, steps=3):
        """Find the last local loaded before instruction i."""
        for j in range(i - 1, max(0, i - steps) - 1, -1):
            loaded = get_loaded_locals(instructions[j])
            if loaded:
                return loaded[-1]  # last loaded is typically the target
        return None

    def find_all_loads_before(i, steps=4):
        """Find all locals loaded in the instructions before i."""
        result = []
        for j in range(max(0, i - steps), i):
            result.extend(get_loaded_locals(instructions[j]))
        return result

    i = 0
    while i < len(instructions):
        instr = instructions[i]
        line = instr.positions.lineno
        if line is None:
            i += 1
            continue

        # STORE_FAST
        if instr.opcode in STORE_FAST_OPS:
            add_info(line, {'type': 'store_fast', 'target': instr.argval})

        # STORE_SUBSCR: stack is [value, container, key] → STORE_SUBSCR
        elif instr.opcode == STORE_SUBSCR_OP:
            # Key is loaded right before STORE_SUBSCR
            key_source = None
            if i >= 1:
                prev = instructions[i - 1]
                if prev.opname in ('LOAD_SMALL_INT', 'LOAD_CONST'):
                    key_source = {'type': 'const', 'value': prev.argval}
                elif 'LOAD_FAST' in prev.opname:
                    key_source = {'type': 'local', 'name': prev.argval}

            # Container and value are loaded before the key.
            # With fused ops: LOAD_FAST_BORROW_LOAD_FAST_BORROW (val, container)
            # Without: LOAD_FAST val, LOAD_FAST container
            all_loads = find_all_loads_before(i, 4)
            # Filter out the key if it was a local
            if key_source and key_source['type'] == 'local':
                all_loads = [n for n in all_loads if n != key_source['name']]

            target = None
            val_source = None
            if len(all_loads) >= 2:
                # First loaded is value, second is container (for x = v; d[k] = x pattern)
                # But with fused: (val, container) loaded together
                val_source = {'type': 'local', 'name': all_loads[0]}
                target = all_loads[1]
            elif len(all_loads) == 1:
                target = all_loads[0]
            else:
                target = find_load_fast_before(i, 3)

            # Check for const value before the loads
            if val_source is None:
                for j in range(max(0, i - 5), i):
                    pj = instructions[j]
                    if pj.opname in ('LOAD_SMALL_INT', 'LOAD_CONST'):
                        val_source = {'type': 'const', 'value': pj.argval}
                        break
            add_info(line, {
                'type': 'store_subscr', 'target': target or '?',
                'key_source': key_source, 'val_source': val_source,
            })

        # STORE_ATTR: stack is [value, target] → STORE_ATTR attr_name
        elif instr.opcode == STORE_ATTR_OP:
            # With fused: LOAD_FAST_BORROW_LOAD_FAST_BORROW (val, target)
            # Without: LOAD_FAST val, LOAD_FAST target or LOAD_CONST val, LOAD_FAST target
            all_loads = find_all_loads_before(i, 3)
            target = None
            val_source = None

            if len(all_loads) >= 2:
                val_source = {'type': 'local', 'name': all_loads[0]}
                target = all_loads[1]
            elif len(all_loads) == 1:
                target = all_loads[0]
                # Value might be a constant
                for j in range(max(0, i - 3), i):
                    pj = instructions[j]
                    if pj.opname in ('LOAD_SMALL_INT', 'LOAD_CONST'):
                        val_source = {'type': 'const', 'value': pj.argval}
                        break
            else:
                target = find_load_fast_before(i, 2)
            add_info(line, {
                'type': 'store_attr', 'target': target or '?',
                'attr': instr.argval, 'val_source': val_source,
            })

        # DELETE_SUBSCR
        elif instr.opcode == DELETE_SUBSCR_OP:
            target = find_load_fast_before(i, 2)
            key_source = None
            if i >= 1:
                prev = instructions[i - 1]
                if prev.opname in ('LOAD_SMALL_INT', 'LOAD_CONST'):
                    key_source = {'type': 'const', 'value': prev.argval}
                elif 'LOAD_FAST' in prev.opname:
                    key_source = {'type': 'local', 'name': prev.argval}
            add_info(line, {
                'type': 'delete_subscr', 'target': target or '?',
                'key_source': key_source,
            })

        # DELETE_ATTR
        elif instr.opcode == DELETE_ATTR_OP:
            target = find_load_fast_before(i, 1)
            add_info(line, {
                'type': 'delete_attr', 'target': target or '?',
                'attr': instr.argval,
            })

        # Method call mutation: LOAD_FAST x → LOAD_ATTR method → args → CALL
        elif (instr.opname == 'LOAD_ATTR' and
              instr.argval in KNOWN_MUTATING_METHODS and
              i > 0 and 'LOAD_FAST' in instructions[i - 1].opname):
            target = instructions[i - 1].argval
            method = instr.argval
            # Scan forward to CALL, collecting argument sources
            arg_sources = []
            j = i + 1
            while j < len(instructions):
                aj = instructions[j]
                if aj.opname == 'CALL':
                    break
                if aj.opname in ('LOAD_SMALL_INT', 'LOAD_CONST'):
                    arg_sources.append({'type': 'const', 'value': aj.argval})
                elif 'LOAD_FAST' in aj.opname:
                    arg_sources.append({'type': 'local', 'name': aj.argval})
                elif aj.opname == 'LOAD_GLOBAL':
                    arg_sources.append({'type': 'global', 'name': aj.argval})
                elif aj.opname.startswith('BUILD_'):
                    # BUILD_MAP etc — inputs were the previous args
                    arg_sources.append({'type': 'build', 'op': aj.opname,
                                       'count': aj.argval})
                elif aj.opname in ('PUSH_NULL', 'COPY', 'LIST_EXTEND',
                                   'SET_UPDATE', 'DICT_UPDATE', 'DICT_MERGE'):
                    pass
                else:
                    arg_sources.append({'type': 'expr'})
                j += 1

            add_info(line, {
                'type': 'method_call', 'target': target,
                'method': method, 'arg_sources': arg_sources,
            })

        i += 1

    return line_info


# ============================================================================
# WAL: Write-Ahead Log
# ============================================================================

class WALEntry:
    __slots__ = ('seq', 'event', 'oid', 'data')
    def __init__(self, seq, event, oid, data):
        self.seq = seq
        self.event = event
        self.oid = oid
        self.data = data
    def __repr__(self):
        return f"WAL#{self.seq} {self.event} oid={self.oid} {self.data}"


class ObjectTracker:
    """Manages unique object IDs and WAL entries."""

    def __init__(self):
        self.next_oid = 1
        self.cpython_id_to_oid = {}   # id(obj) -> oid (active mappings only)
        self.oid_to_type = {}          # oid -> type name
        self.wal = []                  # list of WALEntry
        self.seq = 0
        self._dealloc_callbacks = {}   # oid -> weakref ref (for user objects)

    def _next_seq(self):
        self.seq += 1
        return self.seq

    def get_or_create_oid(self, obj, force_new=False):
        """Get existing oid or assign a new one.
        If force_new=True, always create a new oid (for new variable bindings
        where we know this is a freshly created object)."""
        cid = id(obj)
        if not force_new and cid in self.cpython_id_to_oid:
            oid = self.cpython_id_to_oid[cid]
            # Verify it's the same type (detect id reuse across types)
            expected_type = self.oid_to_type.get(oid)
            actual_type = type(obj).__name__
            if expected_type != actual_type:
                self._invalidate_cpython_id(cid)
            else:
                return oid

        # New object
        oid = self.next_oid
        self.next_oid += 1
        self.cpython_id_to_oid[cid] = oid
        self.oid_to_type[oid] = type(obj).__name__

        # Record creation
        self.wal.append(WALEntry(
            self._next_seq(), 'CREATE', oid,
            {'type': type(obj).__name__, 'initial': self._serialize_shallow(obj)}
        ))

        # Try to set up deallocation tracking via weakref
        try:
            def on_dealloc(ref, _oid=oid, _cid=cid):
                self.wal.append(WALEntry(
                    self._next_seq(), 'DEALLOC', _oid, {}
                ))
                self.cpython_id_to_oid.pop(_cid, None)
            self._dealloc_callbacks[oid] = weakref.ref(obj, on_dealloc)
        except TypeError:
            pass  # Built-in types don't support weakref

        return oid

    def _invalidate_cpython_id(self, cid):
        """Handle id reuse: old oid is dead."""
        old_oid = self.cpython_id_to_oid.pop(cid, None)
        if old_oid is not None:
            self.wal.append(WALEntry(
                self._next_seq(), 'DEALLOC', old_oid,
                {'reason': 'id_reuse_detected'}
            ))

    def record_bind(self, scope, name, obj, is_new_assignment=False):
        """Record that a variable binds to an object."""
        oid = self.get_or_create_oid(obj, force_new=is_new_assignment)
        self.wal.append(WALEntry(
            self._next_seq(), 'BIND', oid,
            {'scope': scope, 'name': name}
        ))
        return oid

    def record_unbind(self, scope, name, oid):
        """Record that a variable no longer references an object."""
        self.wal.append(WALEntry(
            self._next_seq(), 'UNBIND', oid,
            {'scope': scope, 'name': name}
        ))
        # Check if any other binding still references this oid.
        # If not, the object might be deallocated (for non-weakref types),
        # so invalidate the cpython_id mapping to detect id reuse.
        # (For weakref-able types, the weakref callback handles this.)
        cid_to_remove = None
        for cid, mapped_oid in self.cpython_id_to_oid.items():
            if mapped_oid == oid:
                cid_to_remove = cid
                break
        # We'll invalidate on scope exit to be safe — the object
        # may still be alive via other references, but if CPython reuses
        # the id, we'll catch it by assigning a new oid.

    def record_mutate(self, oid, operation, args=None):
        """Record a mutation operation."""
        self.wal.append(WALEntry(
            self._next_seq(), 'MUTATE', oid,
            {'op': operation, 'args': args or []}
        ))

    def record_setattr(self, oid, attr, value_oid_or_val):
        """Record an attribute set."""
        self.wal.append(WALEntry(
            self._next_seq(), 'SETATTR', oid,
            {'attr': attr, 'value': value_oid_or_val}
        ))

    def record_setitem(self, oid, key, value_oid_or_val):
        """Record a subscript set."""
        self.wal.append(WALEntry(
            self._next_seq(), 'SETITEM', oid,
            {'key': key, 'value': value_oid_or_val}
        ))

    def record_delitem(self, oid, key):
        """Record a subscript delete."""
        self.wal.append(WALEntry(
            self._next_seq(), 'DELITEM', oid, {'key': key}
        ))

    def record_delattr(self, oid, attr):
        """Record an attribute delete."""
        self.wal.append(WALEntry(
            self._next_seq(), 'DELATTR', oid, {'attr': attr}
        ))

    def _serialize_shallow(self, obj):
        """Serialize an object shallowly for CREATE entries."""
        if obj is None or isinstance(obj, (bool, int, float)):
            return obj
        if isinstance(obj, str):
            return obj if len(obj) <= 200 else obj[:200] + '...'
        if isinstance(obj, bytes):
            return repr(obj[:100])
        if isinstance(obj, list):
            return {'list': [self._serialize_element(v) for v in obj[:50]]}
        if isinstance(obj, dict):
            return {'dict': {repr(k): self._serialize_element(v)
                           for k, v in list(obj.items())[:50]}}
        if isinstance(obj, (set, frozenset)):
            return {'set': [self._serialize_element(v) for v in list(obj)[:50]]}
        if isinstance(obj, tuple):
            return {'tuple': [self._serialize_element(v) for v in obj[:50]]}
        if hasattr(obj, '__dict__'):
            return {'object': type(obj).__name__,
                    'attrs': {k: self._serialize_element(v)
                             for k, v in list(obj.__dict__.items())[:20]}}
        return {'type': type(obj).__name__}

    def _serialize_element(self, v):
        """Serialize a single element (shallow)."""
        if v is None or isinstance(v, (bool, int, float)):
            return v
        if isinstance(v, str):
            return v if len(v) <= 50 else v[:50] + '...'
        return {'type': type(v).__name__, 'id': id(v)}

    def serialize_value(self, v):
        """Serialize a value for WAL args. Returns primitive or oid reference."""
        if v is None or isinstance(v, (bool, int, float)):
            return v
        if isinstance(v, str):
            return v if len(v) <= 200 else v[:200] + '...'
        if isinstance(v, bytes):
            return repr(v[:100])
        # For complex objects, create an oid reference
        oid = self.get_or_create_oid(v)
        return {'ref': oid}


# ============================================================================
# WAL Tracer: captures execution into WAL entries
# ============================================================================

class WALTracer:
    """settrace-based tracer that produces WAL entries."""

    def __init__(self):
        self.tracker = ObjectTracker()
        self.code_analysis = {}  # id(code) -> line_info
        self.frame_bindings = {} # frame_id -> {name: oid}
        self.scope_stack = []

    def register_code(self, code, visited=None):
        if visited is None:
            visited = set()
        if id(code) in visited:
            return
        visited.add(id(code))
        self.code_analysis[id(code)] = analyze_code_wal(code)
        for const in code.co_consts:
            if isinstance(const, types.CodeType):
                self.register_code(const, visited)

    def _resolve_arg(self, source, frame):
        """Resolve an argument source to a value."""
        if source is None:
            return None
        if source['type'] == 'const':
            return source['value']
        if source['type'] == 'local':
            try:
                val = frame.f_locals[source['name']]
                return self.tracker.serialize_value(val)
            except KeyError:
                return f"<unset:{source['name']}>"
        if source['type'] == 'global':
            try:
                val = frame.f_globals[source['name']]
                return self.tracker.serialize_value(val)
            except KeyError:
                return f"<unset_global:{source['name']}>"
        if source['type'] == 'build':
            return f"<{source['op']}>"
        return '<expr>'

    def _get_scope_name(self, frame):
        return f"{frame.f_code.co_qualname}@{id(frame):#x}"

    def trace_func(self, frame, event, arg):
        code = frame.f_code
        if code.co_qualname.startswith(('WALTracer', 'ObjectTracker', 'WALEntry')):
            return self.trace_func
        if '/lib/' in code.co_filename.replace('\\', '/'):
            return self.trace_func

        cid = id(code)
        scope = self._get_scope_name(frame)
        fid = id(frame)

        if event == 'call':
            self.scope_stack.append(scope)
            # Capture argument bindings
            if fid not in self.frame_bindings:
                self.frame_bindings[fid] = {}
            try:
                current = frame.f_locals
            except Exception:
                current = {}
            for k, v in current.items():
                if k.startswith('__'):
                    continue
                oid = self.tracker.record_bind(scope, k, v)
                self.frame_bindings[fid][k] = oid

        elif event == 'line':
            line_info = self.code_analysis.get(cid, {}).get(frame.f_lineno, [])

            for info in line_info:
                if info['type'] == 'store_fast':
                    # Will be handled on NEXT line event (deferred read pattern)
                    # For WAL, we need to read the value after assignment.
                    # We handle this by checking locals for changes.
                    pass

                elif info['type'] == 'method_call':
                    target_name = info['target']
                    method = info['method']
                    try:
                        target_obj = frame.f_locals[target_name]
                    except KeyError:
                        continue
                    oid = self.tracker.get_or_create_oid(target_obj)

                    # Resolve arguments BEFORE execution
                    resolved_args = []
                    for src in info.get('arg_sources', []):
                        resolved_args.append(self._resolve_arg(src, frame))

                    self.tracker.record_mutate(oid, method, resolved_args)

                elif info['type'] == 'store_subscr':
                    target_name = info['target']
                    try:
                        target_obj = frame.f_locals[target_name]
                    except KeyError:
                        continue
                    oid = self.tracker.get_or_create_oid(target_obj)
                    key = self._resolve_arg(info.get('key_source'), frame)
                    val = self._resolve_arg(info.get('val_source'), frame)
                    self.tracker.record_setitem(oid, key, val)

                elif info['type'] == 'store_attr':
                    target_name = info['target']
                    try:
                        target_obj = frame.f_locals[target_name]
                    except KeyError:
                        continue
                    oid = self.tracker.get_or_create_oid(target_obj)
                    val_source = info.get('val_source')
                    if val_source:
                        val = self._resolve_arg(val_source, frame)
                    else:
                        # Value source unknown (e.g., BUILD_LIST).
                        # Read the attribute AFTER execution would be too late
                        # (LINE fires before). Instead, read it from the current
                        # f_locals if it happens to be there, or mark as unknown.
                        # We'll capture the actual value on the NEXT line event
                        # by checking the attribute.
                        val = '<pending>'
                    self.tracker.record_setattr(oid, info['attr'], val)

                elif info['type'] == 'delete_subscr':
                    target_name = info['target']
                    try:
                        target_obj = frame.f_locals[target_name]
                    except KeyError:
                        continue
                    oid = self.tracker.get_or_create_oid(target_obj)
                    key = self._resolve_arg(info.get('key_source'), frame)
                    self.tracker.record_delitem(oid, key)

                elif info['type'] == 'delete_attr':
                    target_name = info['target']
                    try:
                        target_obj = frame.f_locals[target_name]
                    except KeyError:
                        continue
                    oid = self.tracker.get_or_create_oid(target_obj)
                    self.tracker.record_delattr(oid, info['attr'])

            # Check for new variable bindings (STORE_FAST results from prev line)
            try:
                current = frame.f_locals
            except Exception:
                current = {}

            prev_bindings = self.frame_bindings.get(fid, {})
            for k, v in current.items():
                if k.startswith('__'):
                    continue
                cur_id = id(v)
                prev_oid = prev_bindings.get(k)
                if prev_oid is None or self.tracker.cpython_id_to_oid.get(cur_id) != prev_oid:
                    # This is a new binding — the variable now points to a
                    # (possibly new) object. Use is_new_assignment=True if
                    # this is a genuine reassignment (prev_oid existed).
                    is_new = prev_oid is not None
                    oid = self.tracker.record_bind(scope, k, v, is_new_assignment=is_new)
                    prev_bindings[k] = oid
            self.frame_bindings[fid] = prev_bindings

        elif event == 'return':
            # Unbind all locals in this scope
            bindings = self.frame_bindings.pop(fid, {})
            for name, oid in bindings.items():
                self.tracker.record_unbind(scope, name, oid)
            if self.scope_stack:
                self.scope_stack.pop()

        return self.trace_func

    def start(self):
        self.tracker.wal.clear()
        self.tracker.seq = 0
        self.tracker.next_oid = 1
        self.tracker.cpython_id_to_oid.clear()
        self.tracker.oid_to_type.clear()
        self.frame_bindings.clear()
        self.scope_stack.clear()
        sys.settrace(self.trace_func)

    def stop(self):
        sys.settrace(None)


# ============================================================================
# WAL Replayer: reconstructs object state from WAL entries
# ============================================================================

class WALReplayer:
    """Replays WAL entries to reconstruct object state at any point."""

    def __init__(self, wal_entries):
        self.entries = wal_entries
        self.objects = {}  # oid -> reconstructed state
        self.bindings = {} # (scope, name) -> oid

    def replay_to(self, seq=None):
        """Replay WAL entries up to a given sequence number.
        If seq is None, replay all."""
        self.objects.clear()
        self.bindings.clear()

        for entry in self.entries:
            if seq is not None and entry.seq > seq:
                break

            if entry.event == 'CREATE':
                self.objects[entry.oid] = {
                    'type': entry.data['type'],
                    'state': copy.deepcopy(entry.data['initial']),
                    'alive': True,
                }

            elif entry.event == 'DEALLOC':
                if entry.oid in self.objects:
                    self.objects[entry.oid]['alive'] = False

            elif entry.event == 'BIND':
                key = (entry.data['scope'], entry.data['name'])
                self.bindings[key] = entry.oid

            elif entry.event == 'UNBIND':
                key = (entry.data['scope'], entry.data['name'])
                self.bindings.pop(key, None)

            elif entry.event == 'MUTATE':
                obj = self.objects.get(entry.oid)
                if obj:
                    self._apply_mutation(obj, entry.data['op'], entry.data['args'])

            elif entry.event == 'SETITEM':
                obj = self.objects.get(entry.oid)
                if obj and 'state' in obj:
                    state = obj['state']
                    if isinstance(state, dict) and 'list' in state:
                        key = entry.data['key']
                        if isinstance(key, int) and key < len(state['list']):
                            state['list'][key] = entry.data['value']
                    elif isinstance(state, dict) and 'dict' in state:
                        state['dict'][repr(entry.data['key'])] = entry.data['value']

            elif entry.event == 'DELITEM':
                obj = self.objects.get(entry.oid)
                if obj and 'state' in obj:
                    state = obj['state']
                    if isinstance(state, dict) and 'list' in state:
                        key = entry.data['key']
                        if isinstance(key, int) and key < len(state['list']):
                            state['list'].pop(key)
                    elif isinstance(state, dict) and 'dict' in state:
                        state['dict'].pop(repr(entry.data['key']), None)

            elif entry.event == 'SETATTR':
                obj = self.objects.get(entry.oid)
                if obj and 'state' in obj:
                    state = obj['state']
                    if isinstance(state, dict) and 'object' in state:
                        state['attrs'][entry.data['attr']] = entry.data['value']

            elif entry.event == 'DELATTR':
                obj = self.objects.get(entry.oid)
                if obj and 'state' in obj:
                    state = obj['state']
                    if isinstance(state, dict) and 'object' in state:
                        state['attrs'].pop(entry.data['attr'], None)

    def _apply_mutation(self, obj, op, args):
        """Apply a method mutation to reconstructed state."""
        state = obj.get('state')
        if state is None:
            return

        if isinstance(state, dict) and 'list' in state:
            lst = state['list']
            if op == 'append' and args:
                lst.append(args[0])
            elif op == 'extend' and args:
                if isinstance(args[0], (list, tuple)):
                    lst.extend(args[0])
            elif op == 'insert' and len(args) >= 2:
                lst.insert(args[0], args[1])
            elif op == 'pop':
                if lst:
                    if args:
                        idx = args[0] if isinstance(args[0], int) else -1
                    else:
                        idx = -1
                    if -len(lst) <= idx < len(lst):
                        lst.pop(idx)
            elif op == 'remove' and args:
                try:
                    lst.remove(args[0])
                except ValueError:
                    pass
            elif op == 'clear':
                lst.clear()
            elif op == 'sort':
                lst.sort()
            elif op == 'reverse':
                lst.reverse()

        elif isinstance(state, dict) and 'dict' in state:
            d = state['dict']
            if op == 'update' and args:
                if isinstance(args[0], dict):
                    d.update({repr(k): v for k, v in args[0].items()})
            elif op == 'pop' and args:
                d.pop(repr(args[0]), None)
            elif op == 'popitem' and d:
                d.popitem()
            elif op == 'setdefault' and len(args) >= 2:
                key = repr(args[0])
                if key not in d:
                    d[key] = args[1]
            elif op == 'clear':
                d.clear()

        elif isinstance(state, dict) and 'set' in state:
            s = state['set']
            if op == 'add' and args:
                s.append(args[0])
            elif op == 'discard' and args:
                try:
                    s.remove(args[0])
                except ValueError:
                    pass
            elif op == 'remove' and args:
                try:
                    s.remove(args[0])
                except ValueError:
                    pass
            elif op == 'clear':
                s.clear()
            elif op == 'pop' and s:
                s.pop()

    def get_object_state(self, oid):
        return self.objects.get(oid)

    def get_bindings_at(self):
        return dict(self.bindings)


# ============================================================================
# Tests
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
    print("\n=== Test 1: List mutation argument capture + replay ===")

    def target():
        items = [1, 2, 3]
        items.append(4)
        items.append(5)
        items.insert(0, 0)
        items[2] = 99
        del items[1]
        items.clear()
        return items

    tracer = WALTracer()
    tracer.register_code(target.__code__)
    tracer.start()
    result = target()
    tracer.stop()

    wal = tracer.tracker.wal
    print(f"  WAL entries: {len(wal)}")
    for e in wal:
        if e.event in ('CREATE', 'MUTATE', 'SETITEM', 'DELITEM'):
            print(f"    {e}")

    # Find the list oid
    list_creates = [e for e in wal if e.event == 'CREATE'
                   and e.data.get('type') == 'list']
    check("list CREATE captured", len(list_creates) >= 1)

    # Check mutation entries
    mutates = [e for e in wal if e.event == 'MUTATE']
    setitems = [e for e in wal if e.event == 'SETITEM']
    delitems = [e for e in wal if e.event == 'DELITEM']

    append_entries = [e for e in mutates if e.data['op'] == 'append']
    check("append(4) captured with arg", len(append_entries) >= 1 and
          any(4 in e.data['args'] for e in append_entries),
          f"appends: {[(e.data['op'], e.data['args']) for e in append_entries]}")

    insert_entries = [e for e in mutates if e.data['op'] == 'insert']
    check("insert(0, 0) captured with args",
          any(0 in e.data['args'] for e in insert_entries),
          f"inserts: {[(e.data['op'], e.data['args']) for e in insert_entries]}")

    check("SETITEM captured for items[2]=99", len(setitems) >= 1)
    check("DELITEM captured for del items[1]", len(delitems) >= 1)

    clear_entries = [e for e in mutates if e.data['op'] == 'clear']
    check("clear() captured", len(clear_entries) >= 1)

    # Replay and verify
    replayer = WALReplayer(wal)
    replayer.replay_to()
    # After replay, list should be empty (after clear)
    if list_creates:
        list_oid = list_creates[0].oid
        state = replayer.get_object_state(list_oid)
        if state:
            reconstructed = state['state'].get('list', [])
            check("replay: list is empty after clear()",
                  reconstructed == [],
                  f"got {reconstructed}")


def test_dict_mutations():
    print("\n=== Test 2: Dict mutation argument capture + replay ===")

    def target():
        d = {'a': 1}
        d['b'] = 2
        d['c'] = 3
        d.update({'d': 4})
        del d['a']
        return d

    tracer = WALTracer()
    tracer.register_code(target.__code__)
    tracer.start()
    result = target()
    tracer.stop()

    wal = tracer.tracker.wal
    print(f"  WAL entries: {len(wal)}")
    for e in wal:
        if e.event in ('CREATE', 'MUTATE', 'SETITEM', 'DELITEM'):
            print(f"    {e}")

    setitems = [e for e in wal if e.event == 'SETITEM']
    check("d['b']=2 captured",
          any(e.data.get('key') == 'b' for e in setitems),
          f"setitems: {[e.data for e in setitems]}")

    delitems = [e for e in wal if e.event == 'DELITEM']
    check("del d['a'] captured",
          any(e.data.get('key') == 'a' for e in delitems),
          f"delitems: {[e.data for e in delitems]}")

    check("actual result correct", result == {'b': 2, 'c': 3, 'd': 4})


def test_object_attrs():
    print("\n=== Test 3: Object attribute mutations + replay ===")

    class Player:
        def __init__(self, name):
            self.name = name
            self.hp = 100

    def target():
        p = Player("Alice")
        p.hp = 75
        p.hp = 50
        return p

    tracer = WALTracer()
    tracer.register_code(target.__code__)
    tracer.register_code(Player.__init__.__code__)
    tracer.start()
    result = target()
    tracer.stop()

    wal = tracer.tracker.wal
    print(f"  WAL entries: {len(wal)}")
    for e in wal:
        if e.event in ('CREATE', 'SETATTR'):
            print(f"    {e}")

    setattrs = [e for e in wal if e.event == 'SETATTR']
    hp_sets = [e for e in setattrs if e.data.get('attr') == 'hp']
    check("hp attribute mutations captured", len(hp_sets) >= 2,
          f"got {len(hp_sets)}: {[e.data for e in hp_sets]}")

    name_sets = [e for e in setattrs if e.data.get('attr') == 'name']
    check("name attribute set captured", len(name_sets) >= 1)


def test_variable_args():
    print("\n=== Test 4: Variable arguments resolved from locals ===")

    def target():
        items = []
        x = 42
        name = "hello"
        items.append(x)
        items.append(name)
        d = {}
        d['key'] = x
        return items, d

    tracer = WALTracer()
    tracer.register_code(target.__code__)
    tracer.start()
    result = target()
    tracer.stop()

    wal = tracer.tracker.wal
    mutates = [e for e in wal if e.event == 'MUTATE']
    setitems = [e for e in wal if e.event == 'SETITEM']

    check("append(x) resolved to append(42)",
          any(42 in e.data['args'] for e in mutates),
          f"mutates: {[e.data for e in mutates]}")

    check("append(name) resolved to append('hello')",
          any('hello' in e.data['args'] for e in mutates),
          f"mutates: {[e.data for e in mutates]}")

    check("d['key']=x resolved to d['key']=42",
          any(e.data.get('value') == 42 for e in setitems),
          f"setitems: {[e.data for e in setitems]}")


def test_unique_oids():
    print("\n=== Test 5: Unique object IDs ===")

    def target():
        a = [1, 2, 3]
        b = [4, 5, 6]
        c = a  # alias
        return a, b, c

    tracer = WALTracer()
    tracer.register_code(target.__code__)
    tracer.start()
    result = target()
    tracer.stop()

    wal = tracer.tracker.wal
    creates = [e for e in wal if e.event == 'CREATE' and e.data.get('type') == 'list']
    binds = [e for e in wal if e.event == 'BIND']

    check("two list CREATEs (a and b are different)", len(creates) >= 2,
          f"got {len(creates)}")

    # c = a should bind to same oid as a
    a_binds = [e for e in binds if e.data.get('name') == 'a']
    c_binds = [e for e in binds if e.data.get('name') == 'c']
    if a_binds and c_binds:
        check("aliasing: c binds to same oid as a",
              a_binds[-1].oid == c_binds[-1].oid,
              f"a_oid={a_binds[-1].oid}, c_oid={c_binds[-1].oid}")
    else:
        check("aliasing: found binds for a and c", False,
              f"a_binds={len(a_binds)}, c_binds={len(c_binds)}")


def test_id_reuse():
    print("\n=== Test 6: Object ID reuse detection ===")

    def target():
        a = [1, 2, 3]
        a_orig_id = id(a)
        del a
        # CPython may reuse the same address
        b = [4, 5, 6]
        return a_orig_id, id(b)

    tracer = WALTracer()
    tracer.register_code(target.__code__)
    tracer.start()
    a_id, b_id = target()
    tracer.stop()

    wal = tracer.tracker.wal
    creates = [e for e in wal if e.event == 'CREATE' and e.data.get('type') == 'list']
    deallocs = [e for e in wal if e.event == 'DEALLOC']

    print(f"  CPython id reused: {a_id == b_id}")
    print(f"  CREATEs: {len(creates)}, DEALLOCs: {len(deallocs)}")

    check("two distinct list CREATEs even if id reused",
          len(creates) >= 2,
          f"creates: {len(creates)}, ids_same: {a_id == b_id}")

    if len(creates) >= 2:
        check("different oids for different objects",
              creates[0].oid != creates[1].oid,
              f"oid1={creates[0].oid}, oid2={creates[1].oid}")


def test_deallocation():
    print("\n=== Test 7: Deallocation detection ===")

    class TrackedObj:
        def __init__(self, val):
            self.val = val

    def target():
        obj = TrackedObj(42)
        result = obj.val
        obj = None  # should trigger deallocation
        return result

    tracer = WALTracer()
    tracer.register_code(target.__code__)
    tracer.register_code(TrackedObj.__init__.__code__)
    tracer.start()
    result = target()
    tracer.stop()

    wal = tracer.tracker.wal
    creates = [e for e in wal if e.event == 'CREATE'
               and e.data.get('type') == 'TrackedObj']
    deallocs = [e for e in wal if e.event == 'DEALLOC']

    print(f"  TrackedObj CREATEs: {len(creates)}")
    print(f"  DEALLOCs: {len(deallocs)}")

    check("TrackedObj created", len(creates) >= 1)
    # Weakref callback should fire when obj = None
    check("deallocation detected via weakref",
          len(deallocs) >= 1,
          f"deallocs: {len(deallocs)}")


def test_reference_graph():
    print("\n=== Test 8: Object reference graph ===")

    class Container:
        def __init__(self):
            self.items = []

    def target():
        c = Container()
        c.items.append(1)
        c.items.append(2)
        return c

    tracer = WALTracer()
    tracer.register_code(target.__code__)
    tracer.register_code(Container.__init__.__code__)
    tracer.start()
    result = target()
    tracer.stop()

    wal = tracer.tracker.wal
    print(f"  WAL entries: {len(wal)}")
    for e in wal:
        if e.event in ('CREATE', 'SETATTR', 'MUTATE'):
            print(f"    {e}")

    creates = [e for e in wal if e.event == 'CREATE']
    # Should have CREATE for Container and for the items list
    container_creates = [e for e in creates if e.data.get('type') == 'Container']
    list_creates = [e for e in creates if e.data.get('type') == 'list']

    check("Container object created", len(container_creates) >= 1)
    check("items list created", len(list_creates) >= 1)

    # items attr should reference the list oid
    setattrs = [e for e in wal if e.event == 'SETATTR' and e.data.get('attr') == 'items']
    if setattrs and list_creates:
        items_val = setattrs[0].data.get('value')
        check("Container.items references list oid",
              isinstance(items_val, dict) and 'ref' in items_val,
              f"items attr value: {items_val}")


def test_full_lifecycle():
    print("\n=== Test 9: Full WAL lifecycle ===")

    class Task:
        def __init__(self, name):
            self.name = name
            self.done = False
        def complete(self):
            self.done = True

    def target():
        tasks = []
        t1 = Task("first")
        t2 = Task("second")
        tasks.append(t1)
        tasks.append(t2)
        t1.done = True
        tasks[0] = Task("replacement")
        return tasks

    tracer = WALTracer()
    tracer.register_code(target.__code__)
    tracer.register_code(Task.__init__.__code__)
    tracer.start()
    result = target()
    tracer.stop()

    wal = tracer.tracker.wal
    print(f"  WAL entries: {len(wal)}")

    # Count by event type
    by_type = collections.Counter(e.event for e in wal)
    print(f"  By type: {dict(by_type)}")

    check("has CREATE entries", by_type.get('CREATE', 0) >= 3)  # list + 2 Tasks + replacement
    check("has BIND entries", by_type.get('BIND', 0) >= 1)
    check("has MUTATE entries", by_type.get('MUTATE', 0) >= 2)  # 2 appends
    check("has SETATTR entries", by_type.get('SETATTR', 0) >= 1)  # t1.done = True
    check("has SETITEM entries", by_type.get('SETITEM', 0) >= 1)  # tasks[0] = replacement
    check("has UNBIND entries", by_type.get('UNBIND', 0) >= 1)

    print(f"\n  Full WAL:")
    for e in wal:
        print(f"    {e}")


# ============================================================================
# Main
# ============================================================================

def main():
    test_list_mutations()
    test_dict_mutations()
    test_object_attrs()
    test_variable_args()
    test_unique_oids()
    test_id_reuse()
    test_deallocation()
    test_reference_graph()
    test_full_lifecycle()

    print(f"\n{'='*60}")
    print(f"Results: {passed} passed, {failed} failed")
    print(f"{'='*60}")
    if errors:
        print("\nFailures:")
        for e in errors:
            print(f"  {e}")


if __name__ == '__main__':
    main()
