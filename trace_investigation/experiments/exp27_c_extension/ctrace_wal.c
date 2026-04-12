/*
 * ctrace_wal.c — WAL-based execution tracer C extension
 *
 * Produces an object-centric Write-Ahead Log:
 *   CREATE, BIND, UNBIND, MUTATE, SETATTR, SETITEM, DELITEM, DELATTR, DEALLOC
 *
 * Architecture:
 *   Python pre-analyzes bytecode → passes per-line mutation info to C
 *   C trace function reads args from locals/consts, emits WAL entries
 *   WAL is a ring buffer of fixed-size entries
 */

#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <frameobject.h>
#include <string.h>

/* ========================================================================
 * Configuration
 * ======================================================================== */

#define MAX_LOCALS          64
#define MAX_ARGS            4
#define MAX_MUTATIONS_LINE  8
#define MAX_CODE_ENTRIES    4096
#define MAX_CACHED_FRAMES   256
#define MAX_DEFERRED        8
#define MAX_STRINGS         256
#define CODE_HASH_SIZE      8191
#define OID_MAP_SIZE        16381
#define WAL_BUFFER_DEFAULT  (256 * 1024)

typedef uint32_t oid_t;
#define OID_NONE 0
typedef uint64_t var_bitmask_t;

/* ========================================================================
 * Argument source (how to read a mutation argument at trace time)
 * ======================================================================== */

typedef enum {
    ARG_NONE = 0,
    ARG_CONST_INT,
    ARG_CONST_FLOAT,
    ARG_CONST_STR,    /* string index into code's string table */
    ARG_CONST_NONE,
    ARG_CONST_BOOL,
    ARG_LOCAL,        /* local variable index */
    ARG_BUILD,        /* BUILD_* result — not directly resolvable */
    ARG_EXPR,         /* complex expression */
} ArgSourceType;

typedef struct {
    uint8_t type;     /* ArgSourceType */
    uint8_t _pad;
    uint16_t idx;     /* local_idx or str_idx */
    int64_t  ival;    /* const int/bool value, or 0 */
    double   fval;    /* const float value */
} ArgSource;

/* ========================================================================
 * Mutation info (per line, pre-computed)
 * ======================================================================== */

typedef enum {
    MUT_STORE_FAST = 1,
    MUT_STORE_SUBSCR,
    MUT_STORE_ATTR,
    MUT_DELETE_SUBSCR,
    MUT_DELETE_ATTR,
    MUT_METHOD_CALL,
} MutationType;

typedef struct {
    uint8_t  type;             /* MutationType */
    uint8_t  needs_deferred;   /* 1 = read value on next LINE */
    uint8_t  n_chain;          /* attr chain length (0 = direct target) */
    uint8_t  n_args;
    uint16_t target_idx;       /* local var index of target */
    uint16_t attr_str_idx;     /* attr/method name (string table index) */
    uint16_t chain[4];         /* attr chain string indices */
    ArgSource args[MAX_ARGS];
} MutationInfo;

typedef struct {
    uint8_t n_mutations;
    MutationInfo mutations[MAX_MUTATIONS_LINE];
} LineMutationInfo;

/* ========================================================================
 * Code analysis cache
 * ======================================================================== */

typedef struct {
    PyObject *code_ref;
    int n_locals;
    PyObject *varnames;       /* strong ref */
    int first_line;
    int n_lines;
    var_bitmask_t *line_write_mask;    /* fast check: any writes on this line? */
    LineMutationInfo **line_mutations; /* array[n_lines], NULL or allocated */

    /* String table: attr names, method names, etc. */
    PyObject **strings;   /* array of strong refs */
    int n_strings;
} CodeAnalysis;

static CodeAnalysis g_code_cache[MAX_CODE_ENTRIES];
static int g_n_codes = 0;

static int g_code_hash[CODE_HASH_SIZE];

static void init_code_hash(void) {
    memset(g_code_hash, -1, sizeof(g_code_hash));
}

static int code_hash_lookup(PyObject *code) {
    uintptr_t h = ((uintptr_t)code >> 4) % CODE_HASH_SIZE;
    for (int p = 0; p < 32; p++) {
        int idx = g_code_hash[(h + p) % CODE_HASH_SIZE];
        if (idx == -1) return -1;
        if (g_code_cache[idx].code_ref == code) return idx;
    }
    return -1;
}

static int code_hash_insert(PyObject *code, int cache_idx) {
    uintptr_t h = ((uintptr_t)code >> 4) % CODE_HASH_SIZE;
    for (int p = 0; p < 32; p++) {
        int slot = (h + p) % CODE_HASH_SIZE;
        if (g_code_hash[slot] == -1) {
            g_code_hash[slot] = cache_idx;
            return 0;
        }
    }
    return -1;
}

/* ========================================================================
 * Object ID tracker
 * ======================================================================== */

typedef struct {
    uintptr_t cpython_id;
    oid_t oid;
    uint8_t type_tag;  /* simple type classification */
    uint8_t occupied;
} OidMapEntry;

static OidMapEntry g_oid_map[OID_MAP_SIZE];
static oid_t g_next_oid = 1;

static void oid_map_init(void) {
    memset(g_oid_map, 0, sizeof(g_oid_map));
    g_next_oid = 1;
}

static uint8_t classify_type(PyObject *obj) {
    if (PyList_Check(obj)) return 1;
    if (PyDict_Check(obj)) return 2;
    if (PySet_Check(obj)) return 3;
    if (PyTuple_Check(obj)) return 4;
    if (PyLong_Check(obj)) return 5;
    if (PyFloat_Check(obj)) return 6;
    if (PyUnicode_Check(obj)) return 7;
    if (PyBool_Check(obj)) return 8;
    if (obj == Py_None) return 9;
    return 10; /* user object */
}

/* Check if an object is an immutable primitive that doesn't need oid tracking.
 * These can be serialized inline. Variables holding them can still be
 * reassigned to mutable objects later, so this is a RUNTIME check. */
static int is_primitive(PyObject *obj) {
    return (obj == Py_None || PyBool_Check(obj) || PyLong_Check(obj) ||
            PyFloat_Check(obj) || PyUnicode_Check(obj) || PyBytes_Check(obj));
}

static oid_t oid_lookup(uintptr_t cid) {
    uintptr_t h = (cid >> 4) % OID_MAP_SIZE;
    for (int p = 0; p < 32; p++) {
        OidMapEntry *e = &g_oid_map[(h + p) % OID_MAP_SIZE];
        if (!e->occupied) return OID_NONE;
        if (e->cpython_id == cid) return e->oid;
    }
    return OID_NONE;
}

static oid_t oid_create(uintptr_t cid, uint8_t type_tag) {
    oid_t oid = g_next_oid++;
    uintptr_t h = (cid >> 4) % OID_MAP_SIZE;
    for (int p = 0; p < 32; p++) {
        OidMapEntry *e = &g_oid_map[(h + p) % OID_MAP_SIZE];
        if (!e->occupied) {
            e->cpython_id = cid;
            e->oid = oid;
            e->type_tag = type_tag;
            e->occupied = 1;
            return oid;
        }
    }
    return oid; /* map full, oid created but not stored */
}

static void oid_invalidate(uintptr_t cid) {
    uintptr_t h = (cid >> 4) % OID_MAP_SIZE;
    for (int p = 0; p < 32; p++) {
        OidMapEntry *e = &g_oid_map[(h + p) % OID_MAP_SIZE];
        if (!e->occupied) return;
        if (e->cpython_id == cid) {
            e->occupied = 0;
            return;
        }
    }
}

/* Forward declaration */
static void wal_emit_create(oid_t oid, uint8_t type_tag, int32_t line);

static oid_t oid_get_or_create(PyObject *obj, int32_t line) {
    uintptr_t cid = (uintptr_t)obj;
    uint8_t type_tag = classify_type(obj);

    /* Lookup existing */
    uintptr_t h = (cid >> 4) % OID_MAP_SIZE;
    for (int p = 0; p < 32; p++) {
        OidMapEntry *e = &g_oid_map[(h + p) % OID_MAP_SIZE];
        if (!e->occupied) break;
        if (e->cpython_id == cid) {
            if (e->type_tag == type_tag) {
                return e->oid;
            }
            /* Type mismatch — id reuse detected */
            e->occupied = 0;
            break;
        }
    }

    /* Create new */
    oid_t oid = oid_create(cid, type_tag);
    wal_emit_create(oid, type_tag, line);
    return oid;
}

/* ========================================================================
 * WAL buffer
 * ======================================================================== */

typedef enum {
    WAL_CREATE = 1,
    WAL_BIND,
    WAL_UNBIND,
    WAL_MUTATE,
    WAL_SETATTR,
    WAL_SETITEM,
    WAL_DELITEM,
    WAL_DELATTR,
    WAL_DEALLOC,
    WAL_LINE,
    WAL_CALL,
    WAL_RETURN,
    WAL_EXCEPTION,
} WALEventType;

/* Inline value in WAL entries */
typedef struct {
    uint8_t tag;  /* 0=none, 1=int, 2=float, 3=bool, 4=str, 5=oid_ref, 6=unknown */
    int64_t ival;
    double  fval;
    oid_t   oid_ref;
    char    sval[64]; /* short string, truncated */
} WALValue;

typedef struct {
    uint32_t seq;
    uint8_t  event;   /* WALEventType */
    oid_t    oid;
    int32_t  line;
    uint16_t code_idx;

    /* Payload varies by event type */
    uint8_t  type_tag;        /* CREATE: type classification */
    uint16_t name_str_idx;    /* BIND/UNBIND: var name in string table */
    uint16_t attr_str_idx;    /* SETATTR/DELATTR/MUTATE: attr/method name */
    uint8_t  n_args;          /* MUTATE: number of args */
    WALValue args[MAX_ARGS];  /* MUTATE: resolved args */
    WALValue key;             /* SETITEM/DELITEM: key */
    WALValue value;           /* SETITEM/SETATTR: value */
} WALEntry;

static WALEntry *g_wal = NULL;
static size_t g_wal_capacity = 0;
static size_t g_wal_pos = 0;
static uint32_t g_wal_seq = 0;
static uint64_t g_wal_total = 0;

/* wal_next_full: memset entire entry (for complex entries: MUTATE, SETATTR, etc.) */
static WALEntry* wal_next_full(void) {
    if (!g_wal) return NULL;
    WALEntry *e = &g_wal[g_wal_pos];
    memset(e, 0, sizeof(WALEntry));
    e->seq = ++g_wal_seq;
    g_wal_pos = (g_wal_pos + 1) % g_wal_capacity;
    g_wal_total++;
    return e;
}

/* wal_next_small: only zero the header (for flow events: LINE, CALL, RETURN, etc.) */
static WALEntry* wal_next_small(void) {
    if (!g_wal) return NULL;
    WALEntry *e = &g_wal[g_wal_pos];
    /* Only zero the first 16 bytes (seq + event + oid + line + code_idx) */
    e->seq = ++g_wal_seq;
    e->event = 0;
    e->oid = 0;
    e->line = 0;
    e->code_idx = 0;
    g_wal_pos = (g_wal_pos + 1) % g_wal_capacity;
    g_wal_total++;
    return e;
}

static void wal_emit_create(oid_t oid, uint8_t type_tag, int32_t line) {
    WALEntry *e = wal_next_full();
    if (!e) return;
    e->event = WAL_CREATE;
    e->oid = oid;
    e->type_tag = type_tag;
    e->line = line;
}

/* ========================================================================
 * Frame cache
 * ======================================================================== */

typedef struct {
    PyFrameObject *frame;
    int code_idx;
    var_bitmask_t pending_mask;
    PyObject *prev_values[MAX_LOCALS];
    oid_t bound_oids[MAX_LOCALS];

    /* Deferred reads */
    uint8_t n_deferred;
    struct {
        uint16_t target_idx;
        uint16_t attr_str_idx; /* 0xFFFF = read target itself */
        uint8_t  n_chain;
        uint16_t chain[4];
    } deferred[MAX_DEFERRED];
} FrameCache;

static FrameCache g_frames[MAX_CACHED_FRAMES];
static int g_n_frames = 0;

static FrameCache* find_frame(PyFrameObject *frame) {
    for (int i = g_n_frames - 1; i >= 0; i--)
        if (g_frames[i].frame == frame)
            return &g_frames[i];
    return NULL;
}

static FrameCache* push_frame(PyFrameObject *frame, int code_idx) {
    if (g_n_frames >= MAX_CACHED_FRAMES)
        g_n_frames = MAX_CACHED_FRAMES / 2;
    FrameCache *fc = &g_frames[g_n_frames++];
    memset(fc, 0, sizeof(FrameCache));
    fc->frame = frame;
    fc->code_idx = code_idx;
    return fc;
}

static void pop_frame(PyFrameObject *frame) {
    if (g_n_frames > 0 && g_frames[g_n_frames - 1].frame == frame)
        g_n_frames--;
}

/* ========================================================================
 * Statistics (must be declared before resolve_arg uses them)
 * ======================================================================== */

static uint64_t g_stat_events = 0;
static uint64_t g_stat_line_events = 0;
static uint64_t g_stat_mutations_recorded = 0;
static uint64_t g_stat_getvar_calls = 0;

/* ========================================================================
 * Value resolution
 * ======================================================================== */

static WALValue make_value_from_obj(PyObject *obj, int32_t line) {
    WALValue v;
    memset(&v, 0, sizeof(v));
    if (obj == NULL || obj == Py_None) {
        v.tag = 0;
    } else if (PyBool_Check(obj)) {
        v.tag = 3;
        v.ival = (obj == Py_True) ? 1 : 0;
    } else if (PyLong_Check(obj)) {
        v.tag = 1;
        int overflow;
        long long val = PyLong_AsLongLongAndOverflow(obj, &overflow);
        if (overflow || PyErr_Occurred()) {
            PyErr_Clear();
            v.tag = 6;
        } else {
            v.ival = (int64_t)val;
        }
    } else if (PyFloat_Check(obj)) {
        v.tag = 2;
        v.fval = PyFloat_AS_DOUBLE(obj);
    } else if (PyUnicode_Check(obj)) {
        v.tag = 4;
        Py_ssize_t size;
        const char *s = PyUnicode_AsUTF8AndSize(obj, &size);
        if (s) {
            size_t copy = size < 63 ? size : 63;
            memcpy(v.sval, s, copy);
            v.sval[copy] = '\0';
        }
    } else if (PyBytes_Check(obj)) {
        v.tag = 4; /* reuse string tag for bytes preview */
        Py_ssize_t size;
        char *buf;
        PyBytes_AsStringAndSize(obj, &buf, &size);
        size_t copy = size < 63 ? size : 63;
        memcpy(v.sval, buf, copy);
        v.sval[copy] = '\0';
    } else {
        /* Mutable/complex object — store as oid reference */
        v.tag = 5;
        v.oid_ref = oid_get_or_create(obj, line);
    }
    return v;
}

static WALValue resolve_arg(ArgSource *src, FrameCache *fc, PyFrameObject *frame,
                            CodeAnalysis *ca, int32_t line) {
    WALValue v;
    memset(&v, 0, sizeof(v));

    switch (src->type) {
    case ARG_CONST_INT:
        v.tag = 1; v.ival = src->ival; break;
    case ARG_CONST_FLOAT:
        v.tag = 2; v.fval = src->fval; break;
    case ARG_CONST_BOOL:
        v.tag = 3; v.ival = src->ival; break;
    case ARG_CONST_NONE:
        v.tag = 0; break;
    case ARG_CONST_STR:
        v.tag = 4;
        if (src->idx < ca->n_strings && ca->strings[src->idx]) {
            Py_ssize_t size;
            const char *s = PyUnicode_AsUTF8AndSize(ca->strings[src->idx], &size);
            if (s) {
                size_t copy = size < 63 ? size : 63;
                memcpy(v.sval, s, copy);
            }
        }
        break;
    case ARG_LOCAL:
        if (src->idx < ca->n_locals) {
            /* Try frame cache first to avoid PyFrame_GetVar */
            PyObject *val = NULL;
            if (fc && src->idx < MAX_LOCALS && fc->prev_values[src->idx]) {
                val = fc->prev_values[src->idx];
                v = make_value_from_obj(val, line);
                /* Don't DECREF — prev_values is borrowed */
                break;
            }
            PyObject *name = PyTuple_GET_ITEM(ca->varnames, src->idx);
            val = PyFrame_GetVar(frame, name);
            g_stat_getvar_calls++;
            if (val) {
                v = make_value_from_obj(val, line);
                Py_DECREF(val);
            } else {
                PyErr_Clear();
                v.tag = 6;
            }
        }
        break;
    case ARG_BUILD:
        v.tag = 6; /* can't resolve BUILD_* values */
        break;
    default:
        v.tag = 6;
    }
    return v;
}

static PyObject* follow_attr_chain(PyFrameObject *frame, CodeAnalysis *ca,
                                   uint16_t target_idx, uint16_t *chain, uint8_t n_chain) {
    /* Read target local, then follow attr chain */
    if (target_idx >= ca->n_locals) return NULL;
    PyObject *name = PyTuple_GET_ITEM(ca->varnames, target_idx);
    PyObject *obj = PyFrame_GetVar(frame, name);
    if (!obj) { PyErr_Clear(); return NULL; }

    for (int i = 0; i < n_chain; i++) {
        if (chain[i] >= ca->n_strings || !ca->strings[chain[i]]) {
            Py_DECREF(obj);
            return NULL;
        }
        PyObject *attr = PyObject_GetAttr(obj, ca->strings[chain[i]]);
        Py_DECREF(obj);
        if (!attr) { PyErr_Clear(); return NULL; }
        obj = attr;
    }
    return obj; /* caller must DECREF */
}

/* ========================================================================
 * Trace function
 * ======================================================================== */

static int trace_wal(PyObject *self, PyFrameObject *frame, int what, PyObject *arg)
{
    g_stat_events++;

    PyCodeObject *code = PyFrame_GetCode(frame);
    int code_idx = code_hash_lookup((PyObject *)code);

    if (what == PyTrace_CALL) {
        /* Emit CALL flow event */
        {
            int32_t ln = PyFrame_GetLineNumber(frame);
            WALEntry *e = wal_next_small();
            if (e) { e->event = WAL_CALL; e->line = ln;
                     e->code_idx = code_idx >= 0 ? code_idx : 0; }
        }

        if (code_idx < 0) { Py_DECREF(code); return 0; }
        CodeAnalysis *ca = &g_code_cache[code_idx];
        FrameCache *fc = push_frame(frame, code_idx);

        /* Capture argument bindings */
        int n_args = code->co_argcount + code->co_kwonlyargcount;
        if (code->co_flags & CO_VARARGS) n_args++;
        if (code->co_flags & CO_VARKEYWORDS) n_args++;
        if (n_args > ca->n_locals) n_args = ca->n_locals;

        int32_t line = PyFrame_GetLineNumber(frame);
        for (int i = 0; i < n_args && i < MAX_LOCALS; i++) {
            PyObject *name = PyTuple_GET_ITEM(ca->varnames, i);
            PyObject *val = PyFrame_GetVar(frame, name);
            g_stat_getvar_calls++;
            if (!val) { PyErr_Clear(); continue; }

            oid_t oid = oid_get_or_create(val, line);
            fc->prev_values[i] = val;
            fc->bound_oids[i] = oid;

            /* Emit BIND */
            WALEntry *e = wal_next_full();
            if (e) {
                e->event = WAL_BIND;
                e->oid = oid;
                e->line = line;
                e->code_idx = code_idx;
                e->name_str_idx = i; /* var index = string table index for varnames */
            }
            Py_DECREF(val);
        }
        Py_DECREF(code);
        return 0;
    }

    if (what == PyTrace_RETURN) {
        /* Emit RETURN flow event with return value */
        {
            int32_t ln = PyFrame_GetLineNumber(frame);
            WALEntry *e = wal_next_small();
            if (e) {
                e->event = WAL_RETURN; e->line = ln;
                e->code_idx = code_idx >= 0 ? code_idx : 0;
                memset(&e->value, 0, sizeof(WALValue));
                if (arg) e->value = make_value_from_obj(arg, ln);
            }
        }

        FrameCache *fc = find_frame(frame);
        if (fc) {
            CodeAnalysis *ca = &g_code_cache[fc->code_idx];
            int32_t line = PyFrame_GetLineNumber(frame);

            /* Flush pending writes from last line */
            if (fc->pending_mask != 0) {
                for (int i = 0; i < ca->n_locals && i < MAX_LOCALS; i++) {
                    if (!(fc->pending_mask & (1ULL << i))) continue;
                    g_stat_getvar_calls++;
                    PyObject *name = PyTuple_GET_ITEM(ca->varnames, i);
                    PyObject *val = PyFrame_GetVar(frame, name);
                    if (!val) { PyErr_Clear(); continue; }
                    if (val != fc->prev_values[i]) {
                        oid_t old_oid = fc->bound_oids[i];
                        oid_t new_oid = oid_get_or_create(val, line);
                        if (old_oid != OID_NONE && old_oid != new_oid) {
                            WALEntry *e = wal_next_full();
                            if (e) { e->event = WAL_UNBIND; e->oid = old_oid; e->line = line;
                                     e->code_idx = fc->code_idx; e->name_str_idx = i; }
                        }
                        WALEntry *e = wal_next_full();
                        if (e) { e->event = WAL_BIND; e->oid = new_oid; e->line = line;
                                 e->code_idx = fc->code_idx; e->name_str_idx = i; }
                        fc->bound_oids[i] = new_oid;
                        fc->prev_values[i] = val;
                    }
                    Py_DECREF(val);
                }
            }

            /* Process deferred reads */
            for (int d = 0; d < fc->n_deferred; d++) {
                uint16_t tidx = fc->deferred[d].target_idx;
                uint16_t aidx = fc->deferred[d].attr_str_idx;
                PyObject *obj = follow_attr_chain(frame, ca, tidx,
                    fc->deferred[d].chain, fc->deferred[d].n_chain);
                if (obj) {
                    oid_t obj_oid = oid_get_or_create(obj, line);
                    if (aidx != 0xFFFF && aidx < ca->n_strings) {
                        PyObject *attr_val = PyObject_GetAttr(obj, ca->strings[aidx]);
                        if (attr_val) {
                            WALEntry *e = wal_next_full();
                            if (e) {
                                e->event = WAL_SETATTR;
                                e->oid = obj_oid;
                                e->line = line;
                                e->code_idx = fc->code_idx;
                                e->attr_str_idx = aidx;
                                e->value = make_value_from_obj(attr_val, line);
                            }
                            Py_DECREF(attr_val);
                        } else { PyErr_Clear(); }
                    }
                    Py_DECREF(obj);
                }
            }
            fc->n_deferred = 0;

            /* Unbind all locals */
            for (int i = 0; i < ca->n_locals && i < MAX_LOCALS; i++) {
                if (fc->bound_oids[i] != OID_NONE) {
                    WALEntry *e = wal_next_full();
                    if (e) {
                        e->event = WAL_UNBIND;
                        e->oid = fc->bound_oids[i];
                        e->line = line;
                        e->code_idx = fc->code_idx;
                        e->name_str_idx = i;
                    }
                    /* Scope-based oid invalidation for non-weakref types */
                    oid_invalidate((uintptr_t)fc->prev_values[i]);
                }
            }
            pop_frame(frame);
        }
        Py_DECREF(code);
        return 0;
    }

    if (what == PyTrace_EXCEPTION) {
        /* Emit EXCEPTION flow event */
        int32_t ln = PyFrame_GetLineNumber(frame);
        WALEntry *e = wal_next_small();
        if (e) {
            e->event = WAL_EXCEPTION; e->line = ln;
            e->code_idx = code_idx >= 0 ? code_idx : 0;
        }
        Py_DECREF(code);
        return 0;
    }

    if (what != PyTrace_LINE) { Py_DECREF(code); return 0; }

    /* Emit LINE flow event */
    {
        int32_t ln = PyFrame_GetLineNumber(frame);
        WALEntry *e = wal_next_small();
        if (e) { e->event = WAL_LINE; e->line = ln;
                 e->code_idx = code_idx >= 0 ? code_idx : 0; }
    }

    g_stat_line_events++;
    FrameCache *fc = find_frame(frame);
    if (!fc) { Py_DECREF(code); return 0; }

    CodeAnalysis *ca = &g_code_cache[fc->code_idx];
    int32_t line = PyFrame_GetLineNumber(frame);

    /* Step 1: Process deferred reads from previous line */
    for (int d = 0; d < fc->n_deferred; d++) {
        uint16_t tidx = fc->deferred[d].target_idx;
        uint16_t aidx = fc->deferred[d].attr_str_idx;
        PyObject *obj = follow_attr_chain(frame, ca, tidx,
            fc->deferred[d].chain, fc->deferred[d].n_chain);
        if (obj) {
            oid_t obj_oid = oid_get_or_create(obj, line);
            if (aidx != 0xFFFF && aidx < ca->n_strings) {
                PyObject *attr_val = PyObject_GetAttr(obj, ca->strings[aidx]);
                if (attr_val) {
                    WALEntry *e = wal_next_full();
                    if (e) {
                        e->event = WAL_SETATTR;
                        e->oid = obj_oid;
                        e->line = line;
                        e->code_idx = fc->code_idx;
                        e->attr_str_idx = aidx;
                        e->value = make_value_from_obj(attr_val, line);
                    }
                    Py_DECREF(attr_val);
                } else { PyErr_Clear(); }
            }
            Py_DECREF(obj);
        }
    }
    fc->n_deferred = 0;

    /* Step 2: Read variables from PREVIOUS line's write mask */
    if (fc->pending_mask != 0) {
        for (int i = 0; i < ca->n_locals && i < MAX_LOCALS; i++) {
            if (!(fc->pending_mask & (1ULL << i))) continue;
            g_stat_getvar_calls++;
            PyObject *name = PyTuple_GET_ITEM(ca->varnames, i);
            PyObject *val = PyFrame_GetVar(frame, name);
            if (!val) { PyErr_Clear(); continue; }
            if (val != fc->prev_values[i]) {
                oid_t old_oid = fc->bound_oids[i];
                oid_t new_oid;

                if (is_primitive(val)) {
                    /* Primitives: emit BIND with inline value, no oid tracking */
                    new_oid = OID_NONE;
                    WALEntry *e = wal_next_full();
                    if (e) {
                        e->event = WAL_BIND; e->oid = OID_NONE;
                        e->line = line; e->code_idx = fc->code_idx;
                        e->name_str_idx = i;
                        e->value = make_value_from_obj(val, line);
                    }
                } else {
                    new_oid = oid_get_or_create(val, line);
                    if (old_oid != OID_NONE && old_oid != new_oid) {
                        WALEntry *e = wal_next_full();
                        if (e) { e->event = WAL_UNBIND; e->oid = old_oid; e->line = line;
                                 e->code_idx = fc->code_idx; e->name_str_idx = i; }
                    }
                    if (new_oid != old_oid) {
                        WALEntry *e = wal_next_full();
                        if (e) { e->event = WAL_BIND; e->oid = new_oid; e->line = line;
                                 e->code_idx = fc->code_idx; e->name_str_idx = i; }
                    }
                }
                fc->bound_oids[i] = new_oid;
                fc->prev_values[i] = val;
            }
            Py_DECREF(val);
        }
    }

    /* Step 3: Process mutation info for CURRENT line (before execution)
     * Fast path: if line has no mutations (only STORE_FAST), skip entirely */
    int line_idx = line - ca->first_line;
    if (line_idx >= 0 && line_idx < ca->n_lines && ca->line_mutations[line_idx]) {
        LineMutationInfo *lmi = ca->line_mutations[line_idx];
        for (int m = 0; m < lmi->n_mutations; m++) {
            MutationInfo *mi = &lmi->mutations[m];

            if (mi->type == MUT_STORE_FAST) continue; /* handled by pending_mask */

            /* Get target object — use frame cache if available, else GetVar */
            PyObject *target_obj;
            int target_from_cache = 0;
            if (mi->n_chain > 0) {
                target_obj = follow_attr_chain(frame, ca, mi->target_idx,
                                              mi->chain, mi->n_chain);
            } else if (mi->target_idx < MAX_LOCALS && fc->prev_values[mi->target_idx]) {
                /* Fast path: use cached value from frame */
                target_obj = fc->prev_values[mi->target_idx];
                Py_INCREF(target_obj); /* match the DECREF at end */
                target_from_cache = 1;
            } else {
                if (mi->target_idx >= ca->n_locals) continue;
                PyObject *tname = PyTuple_GET_ITEM(ca->varnames, mi->target_idx);
                target_obj = PyFrame_GetVar(frame, tname);
                g_stat_getvar_calls++;
                if (!target_obj) { PyErr_Clear(); continue; }
            }

            oid_t target_oid = oid_get_or_create(target_obj, line);

            switch (mi->type) {
            case MUT_METHOD_CALL: {
                g_stat_mutations_recorded++;
                WALEntry *e = wal_next_full();
                if (e) {
                    e->event = WAL_MUTATE;
                    e->oid = target_oid;
                    e->line = line;
                    e->code_idx = fc->code_idx;
                    e->attr_str_idx = mi->attr_str_idx;
                    e->n_args = mi->n_args;
                    for (int a = 0; a < mi->n_args && a < MAX_ARGS; a++) {
                        e->args[a] = resolve_arg(&mi->args[a], fc, frame, ca, line);
                    }
                }
                break;
            }
            case MUT_STORE_SUBSCR: {
                g_stat_mutations_recorded++;
                if (mi->needs_deferred) {
                    /* Schedule deferred read */
                    if (fc->n_deferred < MAX_DEFERRED) {
                        int d = fc->n_deferred++;
                        fc->deferred[d].target_idx = mi->target_idx;
                        fc->deferred[d].attr_str_idx = 0xFFFF;
                        fc->deferred[d].n_chain = mi->n_chain;
                        memcpy(fc->deferred[d].chain, mi->chain, mi->n_chain * sizeof(uint16_t));
                    }
                } else {
                    WALEntry *e = wal_next_full();
                    if (e) {
                        e->event = WAL_SETITEM;
                        e->oid = target_oid;
                        e->line = line;
                        e->code_idx = fc->code_idx;
                        if (mi->n_args >= 1) e->key = resolve_arg(&mi->args[0], fc, frame, ca, line);
                        if (mi->n_args >= 2) e->value = resolve_arg(&mi->args[1], fc, frame, ca, line);
                    }
                }
                break;
            }
            case MUT_STORE_ATTR: {
                g_stat_mutations_recorded++;
                if (mi->needs_deferred) {
                    if (fc->n_deferred < MAX_DEFERRED) {
                        int d = fc->n_deferred++;
                        fc->deferred[d].target_idx = mi->target_idx;
                        fc->deferred[d].attr_str_idx = mi->attr_str_idx;
                        fc->deferred[d].n_chain = 0;
                    }
                } else {
                    WALEntry *e = wal_next_full();
                    if (e) {
                        e->event = WAL_SETATTR;
                        e->oid = target_oid;
                        e->line = line;
                        e->code_idx = fc->code_idx;
                        e->attr_str_idx = mi->attr_str_idx;
                        if (mi->n_args >= 1) e->value = resolve_arg(&mi->args[0], fc, frame, ca, line);
                    }
                }
                break;
            }
            case MUT_DELETE_SUBSCR: {
                g_stat_mutations_recorded++;
                WALEntry *e = wal_next_full();
                if (e) {
                    e->event = WAL_DELITEM;
                    e->oid = target_oid;
                    e->line = line;
                    e->code_idx = fc->code_idx;
                    if (mi->n_args >= 1) e->key = resolve_arg(&mi->args[0], fc, frame, ca, line);
                }
                break;
            }
            case MUT_DELETE_ATTR: {
                g_stat_mutations_recorded++;
                WALEntry *e = wal_next_full();
                if (e) {
                    e->event = WAL_DELATTR;
                    e->oid = target_oid;
                    e->line = line;
                    e->code_idx = fc->code_idx;
                    e->attr_str_idx = mi->attr_str_idx;
                }
                break;
            }
            default:
                break;
            }
            Py_DECREF(target_obj);
        }
    }

    /* Step 4: Save CURRENT line's write mask for next event */
    fc->pending_mask = 0;
    if (line_idx >= 0 && line_idx < ca->n_lines) {
        fc->pending_mask = ca->line_write_mask[line_idx];
    }

    Py_DECREF(code);
    return 0;
}

/* ========================================================================
 * Python API
 * ======================================================================== */

static PyObject *
ctrace_wal_register_code(PyObject *self, PyObject *args)
{
    /*
     * register_code(code_obj, first_line, line_data_list, string_list)
     *
     * line_data_list: [(line_number, bitmask, mutations_list), ...]
     *   mutations_list: [(type, target_idx, attr_str_idx, needs_deferred,
     *                     n_chain, chain_tuple, args_list), ...]
     *     args_list: [(arg_type, int_val, float_val, str_idx), ...]
     *
     * string_list: [str, ...] — string table for this code object
     */
    PyObject *code_obj, *line_data, *string_list;
    int first_line;

    if (!PyArg_ParseTuple(args, "OiOO", &code_obj, &first_line, &line_data, &string_list))
        return NULL;

    if (!PyCode_Check(code_obj)) {
        PyErr_SetString(PyExc_TypeError, "First arg must be code object");
        return NULL;
    }
    if (g_n_codes >= MAX_CODE_ENTRIES) {
        PyErr_SetString(PyExc_RuntimeError, "Code cache full");
        return NULL;
    }

    PyCodeObject *code = (PyCodeObject *)code_obj;

    /* Build string table */
    Py_ssize_t n_strings = PyList_Size(string_list);
    if (n_strings > MAX_STRINGS) n_strings = MAX_STRINGS;
    PyObject **strings = (PyObject **)PyMem_Calloc(n_strings, sizeof(PyObject *));
    if (!strings) return PyErr_NoMemory();
    for (Py_ssize_t i = 0; i < n_strings; i++) {
        strings[i] = PyList_GET_ITEM(string_list, i);
        Py_INCREF(strings[i]);
    }

    /* Find line range */
    int max_line = first_line;
    Py_ssize_t n_entries = PyList_Size(line_data);
    for (Py_ssize_t i = 0; i < n_entries; i++) {
        PyObject *entry = PyList_GET_ITEM(line_data, i);
        int ln = (int)PyLong_AsLong(PyTuple_GET_ITEM(entry, 0));
        if (ln > max_line) max_line = ln;
    }
    int n_lines = max_line - first_line + 1;

    var_bitmask_t *write_mask = (var_bitmask_t *)PyMem_Calloc(n_lines, sizeof(var_bitmask_t));
    LineMutationInfo **mutations = (LineMutationInfo **)PyMem_Calloc(n_lines, sizeof(LineMutationInfo *));
    if (!write_mask || !mutations) {
        PyMem_Free(write_mask);
        PyMem_Free(mutations);
        return PyErr_NoMemory();
    }

    /* Parse line data */
    for (Py_ssize_t i = 0; i < n_entries; i++) {
        PyObject *entry = PyList_GET_ITEM(line_data, i);
        int ln = (int)PyLong_AsLong(PyTuple_GET_ITEM(entry, 0));
        uint64_t mask = PyLong_AsUnsignedLongLong(PyTuple_GET_ITEM(entry, 1));
        PyObject *muts = PyTuple_GET_ITEM(entry, 2);

        int idx = ln - first_line;
        if (idx < 0 || idx >= n_lines) continue;
        write_mask[idx] |= mask;

        Py_ssize_t n_muts = PyList_Size(muts);
        if (n_muts > 0) {
            LineMutationInfo *lmi = (LineMutationInfo *)PyMem_Calloc(1, sizeof(LineMutationInfo));
            if (!lmi) continue;
            lmi->n_mutations = n_muts > MAX_MUTATIONS_LINE ? MAX_MUTATIONS_LINE : (uint8_t)n_muts;

            for (Py_ssize_t j = 0; j < lmi->n_mutations; j++) {
                PyObject *mut = PyList_GET_ITEM(muts, j);
                MutationInfo *mi = &lmi->mutations[j];
                mi->type = (uint8_t)PyLong_AsLong(PyTuple_GET_ITEM(mut, 0));
                mi->target_idx = (uint16_t)PyLong_AsLong(PyTuple_GET_ITEM(mut, 1));
                mi->attr_str_idx = (uint16_t)PyLong_AsLong(PyTuple_GET_ITEM(mut, 2));
                mi->needs_deferred = (uint8_t)PyLong_AsLong(PyTuple_GET_ITEM(mut, 3));
                mi->n_chain = (uint8_t)PyLong_AsLong(PyTuple_GET_ITEM(mut, 4));

                /* Parse chain */
                PyObject *chain_tuple = PyTuple_GET_ITEM(mut, 5);
                for (int c = 0; c < mi->n_chain && c < 4; c++) {
                    mi->chain[c] = (uint16_t)PyLong_AsLong(PyTuple_GET_ITEM(chain_tuple, c));
                }

                /* Parse args */
                PyObject *args_list = PyTuple_GET_ITEM(mut, 6);
                mi->n_args = (uint8_t)PyList_Size(args_list);
                if (mi->n_args > MAX_ARGS) mi->n_args = MAX_ARGS;
                for (int a = 0; a < mi->n_args; a++) {
                    PyObject *arg_tuple = PyList_GET_ITEM(args_list, a);
                    mi->args[a].type = (uint8_t)PyLong_AsLong(PyTuple_GET_ITEM(arg_tuple, 0));
                    mi->args[a].ival = PyLong_AsLongLong(PyTuple_GET_ITEM(arg_tuple, 1));
                    mi->args[a].fval = PyFloat_AsDouble(PyTuple_GET_ITEM(arg_tuple, 2));
                    mi->args[a].idx = (uint16_t)PyLong_AsLong(PyTuple_GET_ITEM(arg_tuple, 3));
                }
            }
            mutations[idx] = lmi;
        }
    }

    /* Get varnames */
    PyObject *varnames = PyCode_GetVarnames(code);
    if (!varnames) {
        PyMem_Free(write_mask);
        PyMem_Free(mutations);
        return NULL;
    }

    /* Store */
    int cache_idx = g_n_codes++;
    CodeAnalysis *ca = &g_code_cache[cache_idx];
    ca->code_ref = code_obj;
    ca->n_locals = (int)PyTuple_GET_SIZE(varnames);
    ca->varnames = varnames;
    ca->first_line = first_line;
    ca->n_lines = n_lines;
    ca->line_write_mask = write_mask;
    ca->line_mutations = mutations;
    ca->strings = strings;
    ca->n_strings = (int)n_strings;

    code_hash_insert(code_obj, cache_idx);
    return PyLong_FromLong(cache_idx);
}

static PyObject *
ctrace_wal_start(PyObject *self, PyObject *args)
{
    int buf_size = WAL_BUFFER_DEFAULT;
    if (!PyArg_ParseTuple(args, "|i", &buf_size))
        return NULL;

    if (g_wal) PyMem_Free(g_wal);
    g_wal = (WALEntry *)PyMem_Calloc(buf_size, sizeof(WALEntry));
    if (!g_wal) return PyErr_NoMemory();
    g_wal_capacity = buf_size;
    g_wal_pos = 0;
    g_wal_seq = 0;
    g_wal_total = 0;
    g_stat_events = g_stat_line_events = 0;
    g_stat_mutations_recorded = g_stat_getvar_calls = 0;
    g_n_frames = 0;

    PyEval_SetTrace(trace_wal, Py_None);
    Py_RETURN_NONE;
}

static PyObject *
ctrace_wal_stop(PyObject *self, PyObject *Py_UNUSED(ignored))
{
    PyEval_SetTrace(NULL, NULL);
    Py_RETURN_NONE;
}

static PyObject *
ctrace_wal_stats(PyObject *self, PyObject *Py_UNUSED(ignored))
{
    return Py_BuildValue("{s:K,s:K,s:K,s:K,s:K,s:i}",
        "events", (unsigned long long)g_stat_events,
        "line_events", (unsigned long long)g_stat_line_events,
        "mutations_recorded", (unsigned long long)g_stat_mutations_recorded,
        "getvar_calls", (unsigned long long)g_stat_getvar_calls,
        "wal_total", (unsigned long long)g_wal_total,
        "registered_codes", g_n_codes
    );
}

static const char *wal_event_names[] = {
    "?", "CREATE", "BIND", "UNBIND", "MUTATE", "SETATTR",
    "SETITEM", "DELITEM", "DELATTR", "DEALLOC",
    "LINE", "CALL", "RETURN", "EXCEPTION"
};

static PyObject *val_to_py(WALValue *v) {
    switch (v->tag) {
    case 0: Py_RETURN_NONE;
    case 1: return PyLong_FromLongLong(v->ival);
    case 2: return PyFloat_FromDouble(v->fval);
    case 3: return PyBool_FromLong(v->ival);
    case 4: return PyUnicode_FromString(v->sval);
    case 5: return Py_BuildValue("{s:I}", "ref", (unsigned int)v->oid_ref);
    default: return PyUnicode_FromString("<unknown>");
    }
}

static PyObject *
ctrace_wal_get_wal(PyObject *self, PyObject *args)
{
    int count = 100;
    if (!PyArg_ParseTuple(args, "|i", &count))
        return NULL;

    size_t available = g_wal_total < g_wal_capacity ? g_wal_total : g_wal_capacity;
    if ((size_t)count > available) count = (int)available;

    PyObject *result = PyList_New(count);
    if (!result) return NULL;

    for (int i = 0; i < count; i++) {
        size_t idx;
        if (g_wal_total <= g_wal_capacity) {
            idx = i;
        } else {
            idx = (g_wal_pos + g_wal_capacity - count + i) % g_wal_capacity;
        }
        WALEntry *e = &g_wal[idx];
        const char *evt_name = (e->event > 0 && e->event <= 13) ? wal_event_names[e->event] : "?";

        /* Resolve string indices to actual strings using code's string table */
        const char *name_str = "?";
        const char *attr_str = "?";
        if (e->code_idx < g_n_codes) {
            CodeAnalysis *ca = &g_code_cache[e->code_idx];
            if (e->name_str_idx < ca->n_locals) {
                PyObject *n = PyTuple_GET_ITEM(ca->varnames, e->name_str_idx);
                name_str = PyUnicode_AsUTF8(n);
            }
            if (e->attr_str_idx < ca->n_strings) {
                PyObject *a = ca->strings[e->attr_str_idx];
                if (a) attr_str = PyUnicode_AsUTF8(a);
            }
        }

        PyObject *entry;
        switch (e->event) {
        case WAL_CREATE:
            entry = Py_BuildValue("{s:I,s:s,s:I,s:i,s:b}",
                "seq", e->seq, "event", evt_name, "oid", e->oid,
                "line", e->line, "type_tag", e->type_tag);
            break;
        case WAL_BIND:
            if (e->oid == OID_NONE) {
                /* Primitive bind — include inline value */
                entry = Py_BuildValue("{s:I,s:s,s:I,s:i,s:s,s:N}",
                    "seq", e->seq, "event", evt_name, "oid", e->oid,
                    "line", e->line, "name", name_str,
                    "value", val_to_py(&e->value));
            } else {
                entry = Py_BuildValue("{s:I,s:s,s:I,s:i,s:s}",
                    "seq", e->seq, "event", evt_name, "oid", e->oid,
                    "line", e->line, "name", name_str);
            }
            break;
        case WAL_UNBIND:
            entry = Py_BuildValue("{s:I,s:s,s:I,s:i,s:s}",
                "seq", e->seq, "event", evt_name, "oid", e->oid,
                "line", e->line, "name", name_str);
            break;
        case WAL_MUTATE: {
            PyObject *args_list = PyList_New(e->n_args);
            for (int a = 0; a < e->n_args; a++) {
                PyList_SET_ITEM(args_list, a, val_to_py(&e->args[a]));
            }
            entry = Py_BuildValue("{s:I,s:s,s:I,s:i,s:s,s:N}",
                "seq", e->seq, "event", evt_name, "oid", e->oid,
                "line", e->line, "method", attr_str, "args", args_list);
            break;
        }
        case WAL_SETATTR:
            entry = Py_BuildValue("{s:I,s:s,s:I,s:i,s:s,s:N}",
                "seq", e->seq, "event", evt_name, "oid", e->oid,
                "line", e->line, "attr", attr_str, "value", val_to_py(&e->value));
            break;
        case WAL_SETITEM:
            entry = Py_BuildValue("{s:I,s:s,s:I,s:i,s:N,s:N}",
                "seq", e->seq, "event", evt_name, "oid", e->oid,
                "line", e->line, "key", val_to_py(&e->key), "value", val_to_py(&e->value));
            break;
        case WAL_DELITEM:
            entry = Py_BuildValue("{s:I,s:s,s:I,s:i,s:N}",
                "seq", e->seq, "event", evt_name, "oid", e->oid,
                "line", e->line, "key", val_to_py(&e->key));
            break;
        case WAL_DELATTR:
            entry = Py_BuildValue("{s:I,s:s,s:I,s:i,s:s}",
                "seq", e->seq, "event", evt_name, "oid", e->oid,
                "line", e->line, "attr", attr_str);
            break;
        case WAL_DEALLOC:
            entry = Py_BuildValue("{s:I,s:s,s:I,s:i}",
                "seq", e->seq, "event", evt_name, "oid", e->oid, "line", e->line);
            break;
        case WAL_LINE:
        case WAL_CALL:
        case WAL_EXCEPTION:
            entry = Py_BuildValue("{s:I,s:s,s:i,s:i}",
                "seq", e->seq, "event", evt_name,
                "line", e->line, "code_idx", (int)e->code_idx);
            break;
        case WAL_RETURN:
            entry = Py_BuildValue("{s:I,s:s,s:i,s:i,s:N}",
                "seq", e->seq, "event", evt_name,
                "line", e->line, "code_idx", (int)e->code_idx,
                "retval", val_to_py(&e->value));
            break;
        default:
            entry = Py_BuildValue("{s:I,s:s,s:I,s:i}",
                "seq", e->seq, "event", "?", "oid", e->oid, "line", e->line);
        }
        PyList_SET_ITEM(result, i, entry);
    }
    return result;
}

static PyObject *
ctrace_wal_clear(PyObject *self, PyObject *Py_UNUSED(ignored))
{
    for (int i = 0; i < g_n_codes; i++) {
        Py_XDECREF(g_code_cache[i].varnames);
        PyMem_Free(g_code_cache[i].line_write_mask);
        for (int j = 0; j < g_code_cache[i].n_lines; j++) {
            PyMem_Free(g_code_cache[i].line_mutations[j]);
        }
        PyMem_Free(g_code_cache[i].line_mutations);
        for (int j = 0; j < g_code_cache[i].n_strings; j++) {
            Py_XDECREF(g_code_cache[i].strings[j]);
        }
        PyMem_Free(g_code_cache[i].strings);
    }
    g_n_codes = 0;
    g_n_frames = 0;
    init_code_hash();
    oid_map_init();
    Py_RETURN_NONE;
}

static PyMethodDef methods[] = {
    {"register_code", ctrace_wal_register_code, METH_VARARGS, "Register code with mutation info."},
    {"start", ctrace_wal_start, METH_VARARGS, "Start WAL tracing."},
    {"stop", ctrace_wal_stop, METH_NOARGS, "Stop tracing."},
    {"stats", ctrace_wal_stats, METH_NOARGS, "Get stats."},
    {"get_wal", ctrace_wal_get_wal, METH_VARARGS, "Get WAL entries."},
    {"clear", ctrace_wal_clear, METH_NOARGS, "Clear all state."},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef module = {
    PyModuleDef_HEAD_INIT, "_ctrace_wal",
    "WAL-based execution tracer", -1, methods
};

PyMODINIT_FUNC PyInit__ctrace_wal(void) {
    init_code_hash();
    oid_map_init();
    return PyModule_Create(&module);
}
