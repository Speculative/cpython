"""
WAL Replayer — reconstructs the complete settrace-equivalent execution
trace from a WAL (mode 1: control flow + stores) and the source code.

Strategy:
1. Walk the WAL events sequentially
2. Maintain reconstructed state of all objects (by OID)
3. Maintain current variable bindings per frame (from BIND/UNBIND)
4. At each LINE/CALL/RETURN event, emit a "step" with the current
   state of all local variables (by looking up their OIDs and
   reconstructing object contents)
5. For straight-line code between control flow events, use the source
   code to infer which lines executed and emit steps for those too

The output is a list of steps in the same format as reference_tracer.py.
"""
import dis
import collections


class ReplayObject:
    """Reconstructed state of a tracked mutable object."""

    def __init__(self, oid, type_tag):
        self.oid = oid
        self.type_tag = type_tag
        self.type_name = {
            1: 'list', 2: 'dict', 3: 'set', 4: 'tuple',
            10: 'object',
        }.get(type_tag, 'unknown')

        if self.type_name == 'list':
            self.data = []
        elif self.type_name == 'dict':
            self.data = {}
        elif self.type_name == 'set':
            self.data = set()
        elif self.type_name == 'tuple':
            self.data = []  # tuples are immutable, snapshot gives contents
        else:
            self.data = None
        self.attrs = {}

    def snapshot(self, objects):
        """Produce a deep_snapshot-compatible representation."""
        if self.type_name == 'list':
            return ['__list__', [_resolve(v, objects) for v in self.data]]
        elif self.type_name == 'tuple':
            return ['__tuple__', [_resolve(v, objects) for v in self.data]]
        elif self.type_name == 'dict':
            pairs = [[_resolve(k, objects), _resolve(v, objects)]
                     for k, v in self.data.items()]
            return ['__dict__', pairs]
        elif self.type_name == 'set':
            items = sorted([_resolve(v, objects) for v in self.data], key=repr)
            return ['__set__', items]
        elif self.type_name == 'object':
            snap_attrs = {}
            for k, v in self.attrs.items():
                snap_attrs[k] = _resolve(v, objects)
            return ['__obj__', 'object', snap_attrs]
        else:
            return ['__repr__', f'<oid {self.oid}>']

    def apply_mutation(self, method, args, objects=None):
        """Apply a known mutating method call. objects dict needed to resolve refs."""
        if self.type_name == 'list':
            if method == 'append' and len(args) >= 1:
                self.data.append(args[0])
            elif method == 'insert' and len(args) >= 2:
                idx = args[0] if isinstance(args[0], int) else 0
                self.data.insert(idx, args[1])
            elif method == 'extend' and len(args) >= 1:
                if isinstance(args[0], list):
                    self.data.extend(args[0])
            elif method == 'pop':
                if args and isinstance(args[0], int):
                    if 0 <= args[0] < len(self.data):
                        self.data.pop(args[0])
                elif self.data:
                    self.data.pop()
            elif method == 'remove' and len(args) >= 1:
                try:
                    self.data.remove(args[0])
                except ValueError:
                    pass
            elif method == 'clear':
                self.data.clear()
        elif self.type_name == 'dict':
            if method == 'pop' and len(args) >= 1:
                self.data.pop(args[0], None)
            elif method == 'popitem' and self.data:
                self.data.popitem()
            elif method == 'clear':
                self.data.clear()
            elif method == 'update' and len(args) >= 1:
                arg = args[0]
                if isinstance(arg, dict) and 'ref' in arg and objects:
                    # Resolve the oid reference to get the actual dict
                    ref_oid = arg['ref']
                    if ref_oid in objects and objects[ref_oid].type_name == 'dict':
                        self.data.update(objects[ref_oid].data)
                elif isinstance(arg, dict) and 'ref' not in arg:
                    self.data.update(arg)
            elif method == 'setdefault' and len(args) >= 1:
                if args[0] not in self.data:
                    self.data[args[0]] = args[1] if len(args) >= 2 else None
        elif self.type_name == 'set':
            if method == 'add' and len(args) >= 1:
                self.data.add(_make_hashable(args[0]))
            elif method == 'discard' and len(args) >= 1:
                self.data.discard(_make_hashable(args[0]))
            elif method == 'remove' and len(args) >= 1:
                self.data.discard(_make_hashable(args[0]))
            elif method == 'clear':
                self.data.clear()

    def apply_snapshot(self, items):
        """Replace contents with a SNAPSHOT.

        For lists/tuples/sets: items is [val, val, ...]
        For dicts: items is [key, val, key, val, ...] (alternating)
        """
        if self.type_name == 'list':
            self.data = list(items)
        elif self.type_name == 'tuple':
            self.data = list(items)  # stored as list, snapshotted as __tuple__
        elif self.type_name == 'dict':
            self.data = {}
            # Items come as alternating key, value
            it = iter(items)
            for k in it:
                try:
                    v = next(it)
                    self.data[k] = v
                except StopIteration:
                    break
        elif self.type_name == 'set':
            self.data = set(_make_hashable(v) for v in items)


def _make_hashable(val):
    if isinstance(val, (int, float, str, bool, type(None))):
        return val
    if isinstance(val, dict) and 'ref' in val:
        return ('__ref__', val['ref'])
    return repr(val)


def _resolve(val, objects):
    """Resolve a WAL value to a deep_snapshot-compatible value."""
    if val is None:
        return None
    if isinstance(val, (bool, int, float, str)):
        return val
    if isinstance(val, dict) and 'ref' in val:
        oid = val['ref']
        if oid in objects:
            return objects[oid].snapshot(objects)
        return ['__repr__', f'<oid {oid}>']
    if isinstance(val, tuple) and len(val) == 2 and val[0] == '__ref__':
        oid = val[1]
        if oid in objects:
            return objects[oid].snapshot(objects)
        return ['__repr__', f'<oid {oid}>']
    return val


class Frame:
    """Reconstructed frame state."""

    def __init__(self, code_idx):
        self.code_idx = code_idx
        self.bindings = {}  # name -> ('val', primitive) | ('oid', oid_int)

    def snapshot_locals(self, objects):
        """Snapshot all current bindings."""
        result = {}
        for name, (kind, val) in self.bindings.items():
            if kind == 'val':
                result[name] = val
            elif kind == 'oid' and val in objects:
                result[name] = objects[val].snapshot(objects)
        return result


class WALReconstructor:
    """Reconstructs complete execution trace from WAL + source code.

    Given a WAL captured in mode 1 (control flow + stores), produces
    the same step sequence that a settrace tracer would produce.
    """

    def __init__(self, wal, source_code=None, code_objects=None):
        self.wal = wal
        self.objects = {}       # oid -> ReplayObject
        self.frames = []        # stack of Frame
        self.source_lines = {}  # code_idx -> {lineno: source_line_text}

    def reconstruct(self):
        """Walk the WAL and produce steps.

        Returns list of steps matching reference_tracer format:
        {
            'event': 'line'|'call'|'return'|'exception',
            'lineno': int,
            'funcname': str,
            'locals': {name: snapshot_value, ...},
            'retval': snapshot_value,   # for return
            'exc_type': str,            # for exception
            'exc_msg': str,
        }
        """
        steps = []
        last_line = -1

        for event in self.wal:
            etype = event.get('event')

            if etype == 'CREATE':
                self.objects[event['oid']] = ReplayObject(
                    event['oid'], event.get('type_tag', 10))

            elif etype == 'BIND':
                name = event.get('name', '?')
                oid = event.get('oid', 0)
                if self.frames:
                    if oid != 0:
                        self.frames[-1].bindings[name] = ('oid', oid)
                    elif 'value' in event:
                        self.frames[-1].bindings[name] = ('val', event['value'])

            elif etype == 'UNBIND':
                name = event.get('name', '?')
                if self.frames:
                    self.frames[-1].bindings.pop(name, None)

            elif etype == 'SETATTR':
                oid = event.get('oid', 0)
                attr = event.get('attr', '?')
                value = event.get('value')
                if oid in self.objects:
                    self.objects[oid].attrs[attr] = value

            elif etype == 'SETITEM':
                oid = event.get('oid', 0)
                key = event.get('key')
                value = event.get('value')
                if oid in self.objects:
                    obj = self.objects[oid]
                    if obj.type_name == 'list' and isinstance(key, int):
                        while len(obj.data) <= key:
                            obj.data.append(None)
                        obj.data[key] = value
                    elif obj.type_name == 'dict':
                        obj.data[key] = value

            elif etype == 'DELITEM':
                oid = event.get('oid', 0)
                key = event.get('key')
                if oid in self.objects:
                    obj = self.objects[oid]
                    if obj.type_name == 'list' and isinstance(key, int):
                        if 0 <= key < len(obj.data):
                            obj.data.pop(key)
                    elif obj.type_name == 'dict':
                        obj.data.pop(key, None)

            elif etype == 'DELATTR':
                oid = event.get('oid', 0)
                attr = event.get('attr', '?')
                if oid in self.objects:
                    self.objects[oid].attrs.pop(attr, None)

            elif etype == 'MUTATE':
                oid = event.get('oid', 0)
                method = event.get('method', '')
                args = event.get('args', [])
                if oid in self.objects:
                    self.objects[oid].apply_mutation(method, args, self.objects)

            elif etype == 'SNAPSHOT':
                oid = event.get('oid', 0)
                items = event.get('items', [])
                if oid in self.objects:
                    self.objects[oid].apply_snapshot(items)

            elif etype == 'CALL':
                line = event.get('line', -1)
                code_idx = event.get('code_idx', -1)
                self.frames.append(Frame(code_idx))
                steps.append({
                    'event': 'call',
                    'lineno': line,
                    'locals': {},
                })
                last_line = line

            elif etype == 'RETURN':
                line = event.get('line', -1)
                retval = event.get('retval')
                locals_snap = {}
                if self.frames:
                    locals_snap = self.frames[-1].snapshot_locals(self.objects)
                    self.frames.pop()
                steps.append({
                    'event': 'return',
                    'lineno': line,
                    'locals': locals_snap,
                    'retval': _resolve(retval, self.objects),
                })
                last_line = line

            elif etype == 'LINE':
                line = event.get('line', -1)
                locals_snap = {}
                if self.frames:
                    locals_snap = self.frames[-1].snapshot_locals(self.objects)
                steps.append({
                    'event': 'line',
                    'lineno': line,
                    'locals': locals_snap,
                })
                last_line = line

            elif etype == 'RAISE':
                line = event.get('line', -1)
                steps.append({
                    'event': 'exception',
                    'lineno': line,
                    'exc_type': event.get('exc_type'),
                    'exc_msg': event.get('exc_msg'),
                })

            elif etype == 'EXCEPT':
                pass  # EXCEPT is internal, the RAISE already captured the exception

        return steps

    def get_locals_at(self, seq):
        """Replay up to seq and return current locals snapshot."""
        self.objects = {}
        self.frames = []

        for event in self.wal:
            if event.get('seq', 0) > seq:
                break
            etype = event.get('event')
            if etype == 'CREATE':
                self.objects[event['oid']] = ReplayObject(event['oid'], event.get('type_tag', 10))
            elif etype == 'BIND':
                name = event.get('name', '?')
                oid = event.get('oid', 0)
                if self.frames:
                    if oid != 0:
                        self.frames[-1].bindings[name] = ('oid', oid)
                    elif 'value' in event:
                        self.frames[-1].bindings[name] = ('val', event['value'])
            elif etype == 'UNBIND':
                if self.frames:
                    self.frames[-1].bindings.pop(event.get('name', '?'), None)
            elif etype == 'SETATTR':
                oid = event.get('oid', 0)
                if oid in self.objects:
                    self.objects[oid].attrs[event.get('attr', '?')] = event.get('value')
            elif etype == 'SETITEM':
                oid = event.get('oid', 0)
                if oid in self.objects:
                    obj = self.objects[oid]
                    key = event.get('key')
                    if obj.type_name == 'list' and isinstance(key, int):
                        while len(obj.data) <= key:
                            obj.data.append(None)
                        obj.data[key] = event.get('value')
                    elif obj.type_name == 'dict':
                        obj.data[key] = event.get('value')
            elif etype == 'DELITEM':
                oid = event.get('oid', 0)
                if oid in self.objects:
                    obj = self.objects[oid]
                    key = event.get('key')
                    if obj.type_name == 'list' and isinstance(key, int) and 0 <= key < len(obj.data):
                        obj.data.pop(key)
                    elif obj.type_name == 'dict':
                        obj.data.pop(key, None)
            elif etype == 'DELATTR':
                oid = event.get('oid', 0)
                if oid in self.objects:
                    self.objects[oid].attrs.pop(event.get('attr', '?'), None)
            elif etype == 'MUTATE':
                oid = event.get('oid', 0)
                if oid in self.objects:
                    self.objects[oid].apply_mutation(event.get('method', ''), event.get('args', []), self.objects)
            elif etype == 'SNAPSHOT':
                oid = event.get('oid', 0)
                if oid in self.objects:
                    self.objects[oid].apply_snapshot(event.get('items', []))
            elif etype == 'CALL':
                self.frames.append(Frame(event.get('code_idx', -1)))
            elif etype == 'RETURN':
                if self.frames:
                    self.frames.pop()

        if self.frames:
            return self.frames[-1].snapshot_locals(self.objects)
        return {}
