"""
Experiment 26: WAL Edge Cases

Tests solutions for remaining WAL issues:

1. BUILD_* values not in locals (deferred capture)
   - self.items = []
   - items.append([1, 2])
   - d['key'] = [nested]
   - Same problem for BUILD_MAP, BUILD_SET, BUILD_TUPLE, BUILD_STRING

2. id() reuse for built-in types (scope-based invalidation)
   - del a; b = [...] where b gets same id as a
   - Variable reassignment: a = [...]; a = [...] (new object, maybe same id)

3. Global variables (LOAD_GLOBAL, STORE_GLOBAL)
   - g.append(4)
   - g = [new_value]

4. Nonlocal variables (LOAD_DEREF, STORE_DEREF)
   - nonlocal x; x.append(...)
   - nonlocal x; x = [new_value]

Uses the WALTracer from exp25, extended to handle these cases.
"""
import sys
import os
import dis
import opcode
import types
import weakref
import copy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# ============================================================================
# Extended WAL Tracer with deferred reads and global/nonlocal support
# ============================================================================

STORE_FAST_OPS = set()
STORE_GLOBAL_OP = None
STORE_DEREF_OP = None
for name, op in opcode.opmap.items():
    if 'STORE_FAST' in name or name == 'STORE_NAME':
        STORE_FAST_OPS.add(op)
    elif name == 'STORE_GLOBAL':
        STORE_GLOBAL_OP = op
    elif name == 'STORE_DEREF':
        STORE_DEREF_OP = op

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

FUSED_LOAD_FAST_OPS = {'LOAD_FAST_BORROW_LOAD_FAST_BORROW',
                       'LOAD_FAST_LOAD_FAST', 'STORE_FAST_LOAD_FAST'}


class WALEntry:
    __slots__ = ('seq', 'event', 'oid', 'data')
    def __init__(self, seq, event, oid, data):
        self.seq = seq; self.event = event; self.oid = oid; self.data = data
    def __repr__(self):
        return f"WAL#{self.seq} {self.event} oid={self.oid} {self.data}"


class ObjectTracker:
    def __init__(self):
        self.next_oid = 1
        self.cpython_id_to_oid = {}
        self.oid_to_type = {}
        self.oid_binding_count = {}  # oid -> number of active bindings
        self.wal = []
        self.seq = 0

    def _next_seq(self):
        self.seq += 1
        return self.seq

    def get_or_create_oid(self, obj, force_new=False):
        cid = id(obj)
        if not force_new and cid in self.cpython_id_to_oid:
            oid = self.cpython_id_to_oid[cid]
            if self.oid_to_type.get(oid) != type(obj).__name__:
                self._invalidate_cpython_id(cid)
            else:
                return oid

        oid = self.next_oid
        self.next_oid += 1
        self.cpython_id_to_oid[cid] = oid
        self.oid_to_type[oid] = type(obj).__name__
        self.oid_binding_count[oid] = 0

        self.wal.append(WALEntry(
            self._next_seq(), 'CREATE', oid,
            {'type': type(obj).__name__, 'initial': self._serialize_shallow(obj)}
        ))

        # Weakref for deallocation (user objects only)
        try:
            def on_dealloc(ref, _oid=oid, _cid=cid):
                self.wal.append(WALEntry(self._next_seq(), 'DEALLOC', _oid, {}))
                self.cpython_id_to_oid.pop(_cid, None)
            weakref.ref(obj, on_dealloc)
        except TypeError:
            pass
        return oid

    def _invalidate_cpython_id(self, cid):
        old_oid = self.cpython_id_to_oid.pop(cid, None)
        if old_oid is not None:
            self.wal.append(WALEntry(
                self._next_seq(), 'DEALLOC', old_oid,
                {'reason': 'id_reuse_detected'}
            ))

    def record_bind(self, scope, name, obj, is_new_assignment=False):
        oid = self.get_or_create_oid(obj, force_new=is_new_assignment)
        self.oid_binding_count[oid] = self.oid_binding_count.get(oid, 0) + 1
        self.wal.append(WALEntry(
            self._next_seq(), 'BIND', oid,
            {'scope': scope, 'name': name}
        ))
        return oid

    def record_unbind(self, scope, name, oid):
        self.wal.append(WALEntry(
            self._next_seq(), 'UNBIND', oid,
            {'scope': scope, 'name': name}
        ))
        count = self.oid_binding_count.get(oid, 1) - 1
        self.oid_binding_count[oid] = count
        if count <= 0:
            # No more bindings — object may be deallocated
            # Invalidate cpython_id mapping so id reuse is detected
            cid_to_remove = None
            for cid, mapped_oid in self.cpython_id_to_oid.items():
                if mapped_oid == oid:
                    cid_to_remove = cid
                    break
            if cid_to_remove is not None:
                self.cpython_id_to_oid.pop(cid_to_remove, None)
                # Don't emit DEALLOC here — we might be wrong
                # (object could still be alive via non-tracked references)
                # But invalidating the id mapping means next time we see
                # this id, we'll create a new oid.

    def record_mutate(self, oid, operation, args=None):
        self.wal.append(WALEntry(
            self._next_seq(), 'MUTATE', oid,
            {'op': operation, 'args': args or []}
        ))

    def record_setattr(self, oid, attr, value):
        self.wal.append(WALEntry(
            self._next_seq(), 'SETATTR', oid,
            {'attr': attr, 'value': value}
        ))

    def record_setitem(self, oid, key, value):
        self.wal.append(WALEntry(
            self._next_seq(), 'SETITEM', oid,
            {'key': key, 'value': value}
        ))

    def record_delitem(self, oid, key):
        self.wal.append(WALEntry(self._next_seq(), 'DELITEM', oid, {'key': key}))

    def serialize_value(self, v):
        if v is None or isinstance(v, (bool, int, float)):
            return v
        if isinstance(v, str):
            return v if len(v) <= 200 else v[:200] + '...'
        oid = self.get_or_create_oid(v)
        return {'ref': oid}

    def _serialize_shallow(self, obj):
        if obj is None or isinstance(obj, (bool, int, float)):
            return obj
        if isinstance(obj, str):
            return obj if len(obj) <= 200 else obj[:200] + '...'
        if isinstance(obj, list):
            return {'list': [self._ser_elem(v) for v in obj[:50]]}
        if isinstance(obj, dict):
            return {'dict': {repr(k): self._ser_elem(v)
                           for k, v in list(obj.items())[:50]}}
        if isinstance(obj, (set, frozenset)):
            return {'set': [self._ser_elem(v) for v in list(obj)[:50]]}
        if isinstance(obj, tuple):
            return {'tuple': [self._ser_elem(v) for v in obj[:50]]}
        if hasattr(obj, '__dict__'):
            return {'object': type(obj).__name__,
                    'attrs': {k: self._ser_elem(v)
                             for k, v in list(obj.__dict__.items())[:20]}}
        return {'type': type(obj).__name__}

    def _ser_elem(self, v):
        if v is None or isinstance(v, (bool, int, float)):
            return v
        if isinstance(v, str):
            return v if len(v) <= 50 else v[:50] + '...'
        return {'type': type(v).__name__, 'id': id(v)}


class EdgeCaseTracer:
    """Tracer with deferred reads and global/nonlocal support."""

    def __init__(self):
        self.tracker = ObjectTracker()
        self.frame_bindings = {}
        self.pending_attr_reads = []  # [(frame_id, target_name, attr_name)]
        self.pending_subscr_reads = [] # [(frame_id, target_name, key)]

    def _scope(self, frame):
        return f"{frame.f_code.co_qualname}@{id(frame):#x}"

    def _read_local(self, frame, name):
        """Read from locals, globals, or closure."""
        try:
            return frame.f_locals[name]
        except KeyError:
            pass
        try:
            return frame.f_globals[name]
        except KeyError:
            pass
        return None

    def trace_func(self, frame, event, arg):
        code = frame.f_code
        if code.co_qualname.startswith(('EdgeCaseTracer', 'ObjectTracker', 'WALEntry')):
            return self.trace_func
        if '/lib/' in code.co_filename.replace('\\', '/'):
            return self.trace_func

        fid = id(frame)
        scope = self._scope(frame)

        if event == 'call':
            if fid not in self.frame_bindings:
                self.frame_bindings[fid] = {}
            # Capture argument bindings
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
            # Process deferred reads from previous line
            self._process_deferred_reads(frame)

            # Detect new/changed bindings (STORE_FAST from previous line)
            try:
                current = frame.f_locals
            except Exception:
                current = {}

            prev = self.frame_bindings.get(fid, {})
            for k, v in current.items():
                if k.startswith('__'):
                    continue
                cur_cid = id(v)
                prev_oid = prev.get(k)
                mapped_oid = self.tracker.cpython_id_to_oid.get(cur_cid)
                if prev_oid is None or mapped_oid != prev_oid:
                    is_new = prev_oid is not None
                    oid = self.tracker.record_bind(scope, k, v, is_new_assignment=is_new)
                    prev[k] = oid
            self.frame_bindings[fid] = prev

            # Analyze current line for mutations
            # Simple inline analysis (instead of pre-computed maps for this prototype)
            instructions = list(dis.get_instructions(code))
            line = frame.f_lineno
            for i, instr in enumerate(instructions):
                if instr.positions.lineno != line:
                    continue

                # STORE_ATTR — schedule deferred read of the attribute
                if instr.opcode == STORE_ATTR_OP:
                    target = self._find_target(instructions, i)
                    if target:
                        self.pending_attr_reads.append((fid, target, instr.argval))

                # STORE_SUBSCR — schedule deferred read
                # Could be direct (d[k] = v) or chained (o.config[k] = v)
                elif instr.opcode == STORE_SUBSCR_OP:
                    key = self._find_key_before(instructions, i)
                    target, attr_chain = self._find_subscr_target_with_chain(instructions, i)
                    if target:
                        self.pending_subscr_reads.append((fid, target, attr_chain, key))

                # Method mutation — record with args read from locals NOW
                elif (instr.opname == 'LOAD_ATTR' and
                      instr.argval in KNOWN_MUTATING_METHODS):
                    # Check for attribute chain: LOAD_FAST x → LOAD_ATTR a → LOAD_ATTR mutator
                    target, attr_chain = self._find_target_with_chain(instructions, i)
                    if target:
                        if attr_chain:
                            # Attribute chain: x.a.mutator()
                            # Read x.a to get the actual object being mutated
                            obj = self._read_local(frame, target)
                            if obj is not None:
                                for attr in attr_chain:
                                    try:
                                        obj = getattr(obj, attr)
                                    except AttributeError:
                                        obj = None
                                        break
                            if obj is not None:
                                oid = self.tracker.get_or_create_oid(obj)
                                args = self._resolve_call_args(instructions, i, frame)
                                self.tracker.record_mutate(oid, instr.argval, args)
                        else:
                            # Direct: x.mutator()
                            obj = self._read_local(frame, target)
                            if obj is not None:
                                oid = self.tracker.get_or_create_oid(obj)
                                args = self._resolve_call_args(instructions, i, frame)
                                self.tracker.record_mutate(oid, instr.argval, args)

                # STORE_GLOBAL
                elif instr.opcode == STORE_GLOBAL_OP:
                    # Will be captured on next line via f_globals check
                    pass

                # STORE_DEREF (nonlocal)
                elif instr.opcode == STORE_DEREF_OP:
                    # Will be captured on next line via f_locals check
                    pass

        elif event == 'return':
            self._process_deferred_reads(frame)
            bindings = self.frame_bindings.pop(fid, {})
            for name, oid in bindings.items():
                self.tracker.record_unbind(scope, name, oid)

        return self.trace_func

    def _process_deferred_reads(self, frame):
        """Read attribute/subscript values that were set by the previous line."""
        fid = id(frame)

        # Deferred attr reads
        remaining = []
        for pfid, target_name, attr_name in self.pending_attr_reads:
            if pfid != fid:
                remaining.append((pfid, target_name, attr_name))
                continue
            obj = self._read_local(frame, target_name)
            if obj is not None:
                oid = self.tracker.get_or_create_oid(obj)
                try:
                    attr_val = getattr(obj, attr_name)
                    val = self.tracker.serialize_value(attr_val)
                except AttributeError:
                    val = '<missing>'
                self.tracker.record_setattr(oid, attr_name, val)
        self.pending_attr_reads = remaining

        # Deferred subscr reads
        remaining = []
        for pfid, target_name, attr_chain, key in self.pending_subscr_reads:
            if pfid != fid:
                remaining.append((pfid, target_name, attr_chain, key))
                continue
            obj = self._read_local(frame, target_name)
            if obj is not None:
                # Follow attribute chain to get the actual container
                for attr in attr_chain:
                    try:
                        obj = getattr(obj, attr)
                    except AttributeError:
                        obj = None
                        break
            if obj is not None:
                oid = self.tracker.get_or_create_oid(obj)
                try:
                    if key is not None:
                        subscr_val = obj[key]
                        val = self.tracker.serialize_value(subscr_val)
                    else:
                        val = '<unknown_key>'
                except (KeyError, IndexError, TypeError):
                    val = '<error>'
                self.tracker.record_setitem(oid, key, val)
        self.pending_subscr_reads = remaining

    def _find_target(self, instructions, store_idx):
        """Find the LOAD_FAST target before a STORE_ATTR/STORE_SUBSCR."""
        for j in range(store_idx - 1, max(0, store_idx - 4), -1):
            instr = instructions[j]
            if instr.opname in FUSED_LOAD_FAST_OPS:
                if isinstance(instr.argval, tuple):
                    return instr.argval[-1]
            if 'LOAD_FAST' in instr.opname:
                return instr.argval
            if instr.opname == 'LOAD_GLOBAL':
                return instr.argval
            if instr.opname == 'LOAD_DEREF':
                return instr.argval
        return None

    def _find_target_before_attr(self, instructions, attr_idx):
        """Find LOAD_FAST/GLOBAL/DEREF before a LOAD_ATTR."""
        if attr_idx > 0:
            prev = instructions[attr_idx - 1]
            if 'LOAD_FAST' in prev.opname:
                return prev.argval
            if prev.opname == 'LOAD_GLOBAL':
                return prev.argval
            if prev.opname == 'LOAD_DEREF':
                return prev.argval
            if prev.opname in FUSED_LOAD_FAST_OPS and isinstance(prev.argval, tuple):
                return prev.argval[-1]
        return None

    def _find_target_with_chain(self, instructions, mutator_attr_idx):
        """Find the target and attribute chain for a mutating method call.

        For LOAD_FAST x → LOAD_ATTR a → LOAD_ATTR mutator:
          returns ('x', ['a'])

        For LOAD_FAST x → LOAD_ATTR mutator:
          returns ('x', [])

        For LOAD_FAST x → LOAD_ATTR a → LOAD_ATTR b → LOAD_ATTR mutator:
          returns ('x', ['a', 'b'])
        """
        # Walk backwards from the mutator LOAD_ATTR, collecting LOAD_ATTRs
        chain = []
        j = mutator_attr_idx - 1
        while j >= 0:
            prev = instructions[j]
            if prev.opname == 'LOAD_ATTR':
                chain.insert(0, prev.argval)
                j -= 1
            elif 'LOAD_FAST' in prev.opname:
                return prev.argval, chain
            elif prev.opname == 'LOAD_GLOBAL':
                return prev.argval, chain
            elif prev.opname == 'LOAD_DEREF':
                return prev.argval, chain
            elif prev.opname in FUSED_LOAD_FAST_OPS and isinstance(prev.argval, tuple):
                return prev.argval[-1], chain
            else:
                break
        return None, []

    def _find_subscr_target_with_chain(self, instructions, store_subscr_idx):
        """Find target + attr chain for STORE_SUBSCR.

        For LOAD_FAST d → ... → STORE_SUBSCR:
          returns ('d', [])

        For LOAD_FAST o → LOAD_ATTR config → ... → STORE_SUBSCR:
          returns ('o', ['config'])
        """
        # Walk backwards from STORE_SUBSCR, skipping key and value loads
        # Pattern: ... value ... target [LOAD_ATTR chain] key STORE_SUBSCR
        # Key is right before STORE_SUBSCR, target/chain is before that
        chain = []
        j = store_subscr_idx - 2  # skip key (at -1)
        while j >= 0:
            prev = instructions[j]
            if prev.opname == 'LOAD_ATTR':
                chain.insert(0, prev.argval)
                j -= 1
            elif 'LOAD_FAST' in prev.opname:
                return prev.argval, chain
            elif prev.opname == 'LOAD_GLOBAL':
                return prev.argval, chain
            elif prev.opname == 'LOAD_DEREF':
                return prev.argval, chain
            elif prev.opname in FUSED_LOAD_FAST_OPS and isinstance(prev.argval, tuple):
                return prev.argval[-1], chain
            else:
                break
        return None, []

    def _find_key_before(self, instructions, store_idx):
        """Find the key loaded before STORE_SUBSCR."""
        if store_idx > 0:
            prev = instructions[store_idx - 1]
            if prev.opname in ('LOAD_SMALL_INT', 'LOAD_CONST'):
                return prev.argval
            if 'LOAD_FAST' in prev.opname:
                return prev.argval  # will need runtime resolution
        return None

    def _resolve_call_args(self, instructions, attr_idx, frame):
        """Resolve arguments between LOAD_ATTR and CALL."""
        args = []
        j = attr_idx + 1
        while j < len(instructions):
            instr = instructions[j]
            if instr.opname == 'CALL':
                break
            if instr.opname in ('LOAD_SMALL_INT', 'LOAD_CONST'):
                args.append(instr.argval)
            elif 'LOAD_FAST' in instr.opname:
                val = self._read_local(frame, instr.argval)
                args.append(self.tracker.serialize_value(val) if val is not None else f'<{instr.argval}>')
            elif instr.opname == 'LOAD_GLOBAL':
                val = frame.f_globals.get(instr.argval)
                if val is not None:
                    args.append(self.tracker.serialize_value(val))
            elif instr.opname == 'LOAD_DEREF':
                val = self._read_local(frame, instr.argval)
                args.append(self.tracker.serialize_value(val) if val is not None else f'<{instr.argval}>')
            elif instr.opname.startswith('BUILD_'):
                args.append(f'<{instr.opname}>')
            elif instr.opname in ('PUSH_NULL', 'COPY', 'LIST_EXTEND',
                                  'SET_UPDATE', 'DICT_UPDATE', 'DICT_MERGE',
                                  'POP_TOP'):
                pass
            j += 1
        return args

    def start(self):
        self.tracker.wal.clear()
        self.tracker.seq = 0
        self.tracker.next_oid = 1
        self.tracker.cpython_id_to_oid.clear()
        self.tracker.oid_to_type.clear()
        self.tracker.oid_binding_count.clear()
        self.frame_bindings.clear()
        self.pending_attr_reads.clear()
        self.pending_subscr_reads.clear()
        sys.settrace(self.trace_func)

    def stop(self):
        sys.settrace(None)


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


def test_build_deferred():
    print("\n=== Test 1: BUILD_* with deferred reads ===")

    class Obj:
        pass

    def target():
        o = Obj()
        o.items = []              # BUILD_LIST -> STORE_ATTR
        o.config = {'key': 'val'} # BUILD_MAP -> STORE_ATTR
        o.tags = {1, 2}           # BUILD_SET -> STORE_ATTR
        o.items.append(1)
        o.items.append(2)
        return o

    tracer = EdgeCaseTracer()
    tracer.start()
    result = target()
    tracer.stop()

    wal = tracer.tracker.wal
    print(f"  WAL entries: {len(wal)}")
    for e in wal:
        if e.event in ('CREATE', 'SETATTR', 'MUTATE'):
            print(f"    {e}")

    # Check that o.items = [] created a list oid
    setattrs = [e for e in wal if e.event == 'SETATTR']
    items_setattr = [e for e in setattrs if e.data.get('attr') == 'items']
    check("o.items = [] captured via deferred read",
          len(items_setattr) >= 1,
          f"setattrs: {[e.data for e in items_setattr]}")

    if items_setattr:
        val = items_setattr[0].data.get('value')
        check("o.items value is an oid reference",
              isinstance(val, dict) and 'ref' in val,
              f"value: {val}")

    config_setattr = [e for e in setattrs if e.data.get('attr') == 'config']
    check("o.config = {'key': 'val'} captured",
          len(config_setattr) >= 1)

    tags_setattr = [e for e in setattrs if e.data.get('attr') == 'tags']
    check("o.tags = {1, 2} captured",
          len(tags_setattr) >= 1)

    # Check list mutations on o.items
    mutates = [e for e in wal if e.event == 'MUTATE' and e.data.get('op') == 'append']
    check("o.items.append() mutations captured", len(mutates) >= 2,
          f"mutates: {len(mutates)}")


def test_id_reuse_scope_invalidation():
    print("\n=== Test 2: id() reuse with scope-based invalidation ===")

    def target():
        a = [1, 2, 3]
        a_id = id(a)
        # Unbind a — after this, the list may be deallocated and id reused
        a = None
        # Now create a new list — may get same id
        b = [4, 5, 6]
        b_id = id(b)
        return a_id, b_id, a_id == b_id

    tracer = EdgeCaseTracer()
    tracer.start()
    a_id, b_id, reused = target()
    tracer.stop()

    wal = tracer.tracker.wal
    list_creates = [e for e in wal if e.event == 'CREATE' and e.data.get('type') == 'list']

    print(f"  id reused: {reused}")
    print(f"  List CREATEs: {len(list_creates)}")
    for e in list_creates:
        print(f"    {e}")

    check("two distinct list CREATEs despite id reuse",
          len(list_creates) >= 2,
          f"got {len(list_creates)} creates, reused={reused}")

    if len(list_creates) >= 2:
        check("different oids",
              list_creates[0].oid != list_creates[1].oid)


def test_global_variable():
    print("\n=== Test 3: Global variable mutations ===")

    global _test_global_list
    _test_global_list = [1, 2, 3]

    def target():
        global _test_global_list
        _test_global_list.append(4)
        _test_global_list[0] = 99
        _test_global_list = [10, 20]
        return _test_global_list

    tracer = EdgeCaseTracer()
    tracer.start()
    result = target()
    tracer.stop()

    wal = tracer.tracker.wal
    print(f"  WAL entries: {len(wal)}")
    for e in wal:
        if e.event in ('CREATE', 'MUTATE', 'SETITEM', 'BIND'):
            print(f"    {e}")

    mutates = [e for e in wal if e.event == 'MUTATE' and e.data.get('op') == 'append']
    check("global list.append captured", len(mutates) >= 1,
          f"mutates: {len(mutates)}")

    check("result correct", result == [10, 20])


def test_nonlocal_variable():
    print("\n=== Test 4: Nonlocal variable mutations ===")

    def target():
        items = [1, 2, 3]
        def inner():
            nonlocal items
            items.append(4)
            items = [10, 20]
            items.append(30)
            return items
        result = inner()
        return items, result

    tracer = EdgeCaseTracer()
    tracer.start()
    outer_items, inner_result = target()
    tracer.stop()

    wal = tracer.tracker.wal
    print(f"  WAL entries: {len(wal)}")
    for e in wal:
        if e.event in ('CREATE', 'MUTATE', 'BIND') and 'items' in str(e.data):
            print(f"    {e}")

    check("nonlocal result correct",
          outer_items == [10, 20, 30] and inner_result == [10, 20, 30])

    mutates = [e for e in wal if e.event == 'MUTATE' and e.data.get('op') == 'append']
    check("nonlocal appends captured", len(mutates) >= 2,
          f"got {len(mutates)}")


def test_build_as_method_arg():
    print("\n=== Test 5: BUILD_* as method argument ===")

    def target():
        items = []
        items.append([1, 2])       # arg is BUILD_LIST
        items.append({'a': 1})     # arg is BUILD_MAP
        items.append((3, 4))       # arg is LOAD_CONST (tuple is a const)
        return items

    tracer = EdgeCaseTracer()
    tracer.start()
    result = target()
    tracer.stop()

    wal = tracer.tracker.wal
    mutates = [e for e in wal if e.event == 'MUTATE' and e.data.get('op') == 'append']
    print(f"  Append mutations: {len(mutates)}")
    for e in mutates:
        print(f"    args: {e.data['args']}")

    check("append([1,2]) captured (even if arg is BUILD_LIST)",
          len(mutates) >= 3)

    check("result correct", result == [[1, 2], {'a': 1}, (3, 4)])


def test_build_as_subscr_value():
    print("\n=== Test 6: BUILD_* as STORE_SUBSCR value ===")

    def target():
        d = {}
        d['items'] = [1, 2, 3]    # BUILD_LIST -> STORE_SUBSCR
        d['config'] = {'nested': True}  # BUILD_MAP -> STORE_SUBSCR
        return d

    tracer = EdgeCaseTracer()
    tracer.start()
    result = target()
    tracer.stop()

    wal = tracer.tracker.wal
    setitems = [e for e in wal if e.event == 'SETITEM']
    print(f"  SETITEM entries: {len(setitems)}")
    for e in setitems:
        print(f"    {e}")

    items_entry = [e for e in setitems if e.data.get('key') == 'items']
    check("d['items'] = [1,2,3] captured via deferred read",
          len(items_entry) >= 1)

    if items_entry:
        val = items_entry[0].data.get('value')
        check("value is an oid reference to the list",
              isinstance(val, dict) and 'ref' in val,
              f"value: {val}")

    check("result correct", result == {'items': [1, 2, 3], 'config': {'nested': True}})


def test_attribute_chain_mutations():
    print("\n=== Test 7: Attribute chain mutations (o.items.append) ===")

    class Container:
        pass

    def target():
        c = Container()
        c.items = []
        c.config = {}
        c.items.append(1)
        c.items.append(2)
        c.items.extend([3, 4])
        c.config['key'] = 'val'
        c.config['num'] = 42
        return c

    tracer = EdgeCaseTracer()
    tracer.start()
    result = target()
    tracer.stop()

    wal = tracer.tracker.wal
    print(f"  WAL entries: {len(wal)}")
    for e in wal:
        if e.event in ('CREATE', 'SETATTR', 'MUTATE', 'SETITEM'):
            print(f"    {e}")

    mutates = [e for e in wal if e.event == 'MUTATE' and e.data.get('op') == 'append']
    check("c.items.append() mutations captured", len(mutates) >= 2,
          f"got {len(mutates)}")

    extends = [e for e in wal if e.event == 'MUTATE' and e.data.get('op') == 'extend']
    check("c.items.extend() captured", len(extends) >= 1)

    setitems = [e for e in wal if e.event == 'SETITEM']
    config_sets = [e for e in setitems if e.data.get('key') in ('key', 'num')]
    check("c.config['key'] and c.config['num'] captured",
          len(config_sets) >= 2,
          f"got {len(config_sets)}: {[e.data for e in config_sets]}")

    check("result correct",
          result.items == [1, 2, 3, 4] and result.config == {'key': 'val', 'num': 42})


def test_reassignment_same_id():
    print("\n=== Test 7: Variable reassignment with potential id reuse ===")

    def target():
        x = [1]
        x = [2]    # old [1] deallocated, [2] may get same id
        x = [3]    # old [2] deallocated, [3] may get same id
        return x

    tracer = EdgeCaseTracer()
    tracer.start()
    result = target()
    tracer.stop()

    wal = tracer.tracker.wal
    list_creates = [e for e in wal if e.event == 'CREATE' and e.data.get('type') == 'list']
    binds = [e for e in wal if e.event == 'BIND' and e.data.get('name') == 'x']

    print(f"  List CREATEs: {len(list_creates)}")
    print(f"  x BINDs: {len(binds)}")

    check("three distinct lists created",
          len(list_creates) >= 3,
          f"got {len(list_creates)}")

    if len(list_creates) >= 3:
        oids = [e.oid for e in list_creates]
        check("all different oids",
              len(set(oids)) == len(oids),
              f"oids: {oids}")


# ============================================================================
# Main
# ============================================================================

def main():
    test_build_deferred()
    test_id_reuse_scope_invalidation()
    test_global_variable()
    test_nonlocal_variable()
    test_build_as_method_arg()
    test_build_as_subscr_value()
    test_attribute_chain_mutations()
    test_reassignment_same_id()

    print(f"\n{'='*60}")
    print(f"Results: {passed} passed, {failed} failed")
    print(f"{'='*60}")
    if errors:
        print("\nFailures:")
        for e in errors:
            print(f"  {e}")


if __name__ == '__main__':
    main()
