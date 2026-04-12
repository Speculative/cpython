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
#include <unistd.h>
#include <fcntl.h>

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
 * Statistics
 * ======================================================================== */

static uint64_t g_stat_events = 0;
static uint64_t g_stat_line_events = 0;
static uint64_t g_stat_mutations_recorded = 0;
static uint64_t g_stat_getvar_calls = 0;

/* ========================================================================
 * Compact WAL byte-stream buffer
 *
 * Instead of fixed-size WALEntry structs (~530 bytes each), entries are
 * variable-length byte sequences in a flat buffer. Write is just memcpy +
 * pointer bump. Typical entry: 11-30 bytes.
 *
 * Format:
 *   Header (every entry): event_type(1) + seq(4) + oid(4) + line(4) + code_idx(2) = 15 bytes
 *   Value encoding: tag(1) + payload
 *     tag 0 (None): 0 bytes
 *     tag 1 (int):  8 bytes
 *     tag 2 (float):8 bytes
 *     tag 3 (bool): 1 byte
 *     tag 4 (str):  1 byte len + data (max 63 bytes)
 *     tag 5 (oid):  4 bytes
 *     tag 6 (unknown): 0 bytes
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

/* Byte stream buffer */
#define WAL_BUF_DEFAULT (64 * 1024 * 1024)  /* 64 MB */

static uint8_t *g_wal_buf = NULL;
static size_t   g_wal_buf_capacity = 0;
static size_t   g_wal_buf_pos = 0;
static uint32_t g_wal_seq = 0;
static uint64_t g_wal_total = 0;

/* Disk flush */
static int      g_wal_fd = -1;           /* file descriptor for disk output, -1 = no disk */
static uint64_t g_wal_bytes_flushed = 0; /* total bytes written to disk */
static uint64_t g_wal_flush_count = 0;   /* number of flush operations */

static void wal_flush_to_disk(void) {
    if (g_wal_fd < 0 || g_wal_buf_pos == 0) return;
    /* Write entire buffer in one syscall */
    ssize_t written = write(g_wal_fd, g_wal_buf, g_wal_buf_pos);
    if (written > 0) {
        g_wal_bytes_flushed += written;
    }
    g_wal_flush_count++;
    g_wal_buf_pos = 0;  /* reset buffer */
}

/* Reserve up to N bytes. If buffer would overflow, flush to disk first.
 * Returns write pointer, or NULL if no buffer / no disk and buffer full. */
static inline uint8_t* wal_reserve(size_t n) {
    if (!g_wal_buf) return NULL;
    if (g_wal_buf_pos + n > g_wal_buf_capacity) {
        /* Buffer full — flush to disk if configured */
        if (g_wal_fd >= 0) {
            wal_flush_to_disk();
            /* After flush, buffer is empty — should have space now */
            if (g_wal_buf_pos + n > g_wal_buf_capacity) return NULL; /* entry too large */
        } else {
            return NULL; /* no disk, buffer full, drop entry */
        }
    }
    return g_wal_buf + g_wal_buf_pos;
}
static inline void wal_finish(uint8_t *end) {
    g_wal_buf_pos = end - g_wal_buf;
}

/* Write helpers — all inline, advance *p */
static inline void wal_write_u8(uint8_t **p, uint8_t v) { **p = v; (*p)++; }
static inline void wal_write_u16(uint8_t **p, uint16_t v) { memcpy(*p, &v, 2); *p += 2; }
static inline void wal_write_u32(uint8_t **p, uint32_t v) { memcpy(*p, &v, 4); *p += 4; }
static inline void wal_write_i32(uint8_t **p, int32_t v) { memcpy(*p, &v, 4); *p += 4; }
static inline void wal_write_i64(uint8_t **p, int64_t v) { memcpy(*p, &v, 8); *p += 8; }
static inline void wal_write_f64(uint8_t **p, double v) { memcpy(*p, &v, 8); *p += 8; }

/* Write entry header: event(1) + seq(4) + oid(4) + line(4) + code_idx(2) = 15 bytes */
static inline void wal_write_header(uint8_t **p, uint8_t event, oid_t oid, int32_t line, uint16_t code_idx) {
    wal_write_u8(p, event);
    wal_write_u32(p, ++g_wal_seq);
    wal_write_u32(p, oid);
    wal_write_i32(p, line);
    wal_write_u16(p, code_idx);
    g_wal_total++;
}

/* Write a value: tag(1) + variable payload.
 * Returns bytes written. Max = 1 + 64 = 65 bytes for a string. */
static inline void wal_write_value_from_obj(uint8_t **p, PyObject *obj, int32_t line) {
    if (obj == NULL || obj == Py_None) {
        wal_write_u8(p, 0);
    } else if (PyBool_Check(obj)) {
        wal_write_u8(p, 3);
        wal_write_u8(p, obj == Py_True ? 1 : 0);
    } else if (PyLong_Check(obj)) {
        int overflow;
        long long v = PyLong_AsLongLongAndOverflow(obj, &overflow);
        if (overflow || PyErr_Occurred()) {
            PyErr_Clear();
            wal_write_u8(p, 6); /* unknown */
        } else {
            wal_write_u8(p, 1);
            wal_write_i64(p, (int64_t)v);
        }
    } else if (PyFloat_Check(obj)) {
        wal_write_u8(p, 2);
        wal_write_f64(p, PyFloat_AS_DOUBLE(obj));
    } else if (PyUnicode_Check(obj)) {
        wal_write_u8(p, 4);
        Py_ssize_t size;
        const char *s = PyUnicode_AsUTF8AndSize(obj, &size);
        if (s) {
            uint8_t len = size < 63 ? (uint8_t)size : 63;
            wal_write_u8(p, len);
            memcpy(*p, s, len); *p += len;
        } else {
            wal_write_u8(p, 0);
        }
    } else if (PyBytes_Check(obj)) {
        wal_write_u8(p, 4); /* reuse str tag */
        Py_ssize_t size;
        char *buf;
        PyBytes_AsStringAndSize(obj, &buf, &size);
        uint8_t len = size < 63 ? (uint8_t)size : 63;
        wal_write_u8(p, len);
        memcpy(*p, buf, len); *p += len;
    } else {
        /* Mutable/complex object — oid reference.
         * Use oid_lookup only — caller must have pre-created the oid
         * before starting this write sequence (to avoid nested wal_reserve). */
        oid_t ref = oid_lookup((uintptr_t)obj);
        if (ref == OID_NONE) {
            /* Oid not pre-created. Create it now but DON'T emit CREATE entry
             * (it would conflict with the current write). The object will appear
             * as a forward reference; the CREATE can be emitted later or inferred. */
            ref = oid_create((uintptr_t)obj, classify_type(obj));
        }
        wal_write_u8(p, 5);
        wal_write_u32(p, ref);
    }
}

/* Max bytes any single WAL entry can use.
 * MUTATE with 4 string args: 15 + 2 + 1 + 4*(1+1+63) = 278
 * Use 300 as conservative max. */
#define WAL_MAX_ENTRY_SIZE 300

static void wal_emit_create(oid_t oid, uint8_t type_tag, int32_t line) {
    uint8_t *p = wal_reserve(15 + 1);
    if (!p) return;
    wal_write_header(&p, WAL_CREATE, oid, line, 0);
    wal_write_u8(&p, type_tag);
    wal_finish(p);
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

/* Write a resolved argument from ArgSource */
static void wal_write_arg(uint8_t **p, ArgSource *src, FrameCache *fc,
                          PyFrameObject *frame, CodeAnalysis *ca, int32_t line) {
    switch (src->type) {
    case ARG_CONST_INT:
        wal_write_u8(p, 1); wal_write_i64(p, src->ival); break;
    case ARG_CONST_FLOAT:
        wal_write_u8(p, 2); wal_write_f64(p, src->fval); break;
    case ARG_CONST_BOOL:
        wal_write_u8(p, 3); wal_write_u8(p, src->ival ? 1 : 0); break;
    case ARG_CONST_NONE:
        wal_write_u8(p, 0); break;
    case ARG_CONST_STR:
        wal_write_u8(p, 4);
        if (src->idx < ca->n_strings && ca->strings[src->idx]) {
            Py_ssize_t size;
            const char *s = PyUnicode_AsUTF8AndSize(ca->strings[src->idx], &size);
            if (s) {
                uint8_t len = size < 63 ? (uint8_t)size : 63;
                wal_write_u8(p, len);
                memcpy(*p, s, len); *p += len;
            } else { wal_write_u8(p, 0); }
        } else { wal_write_u8(p, 0); }
        break;
    case ARG_LOCAL:
        if (fc && src->idx < MAX_LOCALS && fc->prev_values[src->idx]) {
            wal_write_value_from_obj(p, fc->prev_values[src->idx], line);
        } else if (src->idx < ca->n_locals) {
            PyObject *name = PyTuple_GET_ITEM(ca->varnames, src->idx);
            PyObject *val = PyFrame_GetVar(frame, name);
            g_stat_getvar_calls++;
            if (val) {
                wal_write_value_from_obj(p, val, line);
                Py_DECREF(val);
            } else { PyErr_Clear(); wal_write_u8(p, 6); }
        } else { wal_write_u8(p, 6); }
        break;
    default:
        wal_write_u8(p, 6); /* unknown */
    }
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
            uint8_t *p = wal_reserve(15);
            if (p) { wal_write_header(&p, WAL_CALL, 0, ln, code_idx >= 0 ? code_idx : 0); wal_finish(p); }
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
            { uint8_t *p = wal_reserve(15 + 2);
              if (p) { wal_write_header(&p, WAL_BIND, oid, line, code_idx);
                       wal_write_u16(&p, i); wal_finish(p); } }
            Py_DECREF(val);
        }
        Py_DECREF(code);
        return 0;
    }

    if (what == PyTrace_RETURN) {
        /* Emit RETURN flow event with return value */
        {
            int32_t ln = PyFrame_GetLineNumber(frame);
            /* Pre-create oid for return value if complex object,
             * so CREATE entry is emitted BEFORE the RETURN entry */
            if (arg && !is_primitive(arg)) {
                oid_get_or_create(arg, ln);
            }
            uint8_t *p = wal_reserve(WAL_MAX_ENTRY_SIZE);
            if (p) {
                wal_write_header(&p, WAL_RETURN, 0, ln, code_idx >= 0 ? code_idx : 0);
                if (arg) wal_write_value_from_obj(&p, arg, ln);
                else wal_write_u8(&p, 0);
                wal_finish(p);
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
                            { uint8_t *p = wal_reserve(15 + 2);
                              if (p) { wal_write_header(&p, WAL_UNBIND, old_oid, line, fc->code_idx);
                                       wal_write_u16(&p, i); wal_finish(p); } }
                        }
                        { uint8_t *p = wal_reserve(15 + 2);
                          if (p) { wal_write_header(&p, WAL_BIND, new_oid, line, fc->code_idx);
                                   wal_write_u16(&p, i); wal_finish(p); } }
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
                            { uint8_t *_p = wal_reserve(15 + 2 + 82);
                              if (_p) { wal_write_header(&_p, WAL_SETATTR, obj_oid, line, fc->code_idx);
                                        wal_write_u16(&_p, aidx);
                                        wal_write_value_from_obj(&_p, attr_val, line); wal_finish(_p); } }
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
                    { uint8_t *p = wal_reserve(15 + 2);
                      if (p) { wal_write_header(&p, WAL_UNBIND, fc->bound_oids[i], line, fc->code_idx);
                               wal_write_u16(&p, i); wal_finish(p); } }
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
        uint8_t *p = wal_reserve(15);
        if (p) { wal_write_header(&p, WAL_EXCEPTION, 0, ln, code_idx >= 0 ? code_idx : 0); wal_finish(p); }
        Py_DECREF(code);
        return 0;
    }

    if (what != PyTrace_LINE) { Py_DECREF(code); return 0; }

    /* Emit LINE flow event */
    {
        int32_t ln = PyFrame_GetLineNumber(frame);
        uint8_t *p = wal_reserve(15);
        if (p) { wal_write_header(&p, WAL_LINE, 0, ln, code_idx >= 0 ? code_idx : 0); wal_finish(p); }
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
                    { uint8_t *_p = wal_reserve(15 + 2 + 82);
                      if (_p) { wal_write_header(&_p, WAL_SETATTR, obj_oid, line, fc->code_idx);
                                wal_write_u16(&_p, aidx);
                                wal_write_value_from_obj(&_p, attr_val, line); wal_finish(_p); } }
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
                    { uint8_t *_p = wal_reserve(82);
                      if (_p) { wal_write_header(&_p, WAL_BIND, OID_NONE, line, fc->code_idx);
                                wal_write_u16(&_p, i);
                                wal_write_value_from_obj(&_p, val, line); wal_finish(_p); } }
                } else {
                    new_oid = oid_get_or_create(val, line);
                    if (old_oid != OID_NONE && old_oid != new_oid) {
                        { uint8_t *p = wal_reserve(15 + 2);
                          if (p) { wal_write_header(&p, WAL_UNBIND, old_oid, line, fc->code_idx);
                                   wal_write_u16(&p, i); wal_finish(p); } }
                    }
                    if (new_oid != old_oid) {
                        { uint8_t *p = wal_reserve(15 + 2);
                          if (p) { wal_write_header(&p, WAL_BIND, new_oid, line, fc->code_idx);
                                   wal_write_u16(&p, i); wal_finish(p); } }
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
                { uint8_t *_p = wal_reserve(WAL_MAX_ENTRY_SIZE);
                  if (_p) { wal_write_header(&_p, WAL_MUTATE, target_oid, line, fc->code_idx);
                            wal_write_u16(&_p, mi->attr_str_idx);
                            wal_write_u8(&_p, mi->n_args);
                            for (int a = 0; a < mi->n_args && a < MAX_ARGS; a++) {
                                wal_write_arg(&_p, &mi->args[a], fc, frame, ca, line);
                            } wal_finish(_p); } }
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
                    { uint8_t *_p = wal_reserve(WAL_MAX_ENTRY_SIZE);
                      if (_p) { wal_write_header(&_p, WAL_SETITEM, target_oid, line, fc->code_idx);
                                if (mi->n_args >= 1) wal_write_arg(&_p, &mi->args[0], fc, frame, ca, line);
                                else wal_write_u8(&_p, 6);
                                if (mi->n_args >= 2) wal_write_arg(&_p, &mi->args[1], fc, frame, ca, line);
                                else wal_write_u8(&_p, 6); wal_finish(_p); } }
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
                    { uint8_t *_p = wal_reserve(WAL_MAX_ENTRY_SIZE);
                      if (_p) { wal_write_header(&_p, WAL_SETATTR, target_oid, line, fc->code_idx);
                                wal_write_u16(&_p, mi->attr_str_idx);
                                if (mi->n_args >= 1) wal_write_arg(&_p, &mi->args[0], fc, frame, ca, line);
                                else wal_write_u8(&_p, 6); wal_finish(_p); } }
                }
                break;
            }
            case MUT_DELETE_SUBSCR: {
                g_stat_mutations_recorded++;
                { uint8_t *_p = wal_reserve(WAL_MAX_ENTRY_SIZE);
                  if (_p) { wal_write_header(&_p, WAL_DELITEM, target_oid, line, fc->code_idx);
                            if (mi->n_args >= 1) wal_write_arg(&_p, &mi->args[0], fc, frame, ca, line);
                            else wal_write_u8(&_p, 6); wal_finish(_p); } }
                break;
            }
            case MUT_DELETE_ATTR: {
                g_stat_mutations_recorded++;
                { uint8_t *_p = wal_reserve(15 + 2);
                  if (_p) { wal_write_header(&_p, WAL_DELATTR, target_oid, line, fc->code_idx);
                            wal_write_u16(&_p, mi->attr_str_idx); wal_finish(_p); } }
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
ctrace_wal_start(PyObject *self, PyObject *args, PyObject *kwargs)
{
    static char *kwlist[] = {"buf_size", "output_file", NULL};
    int buf_size = WAL_BUF_DEFAULT;
    const char *output_file = NULL;

    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "|iz", kwlist, &buf_size, &output_file))
        return NULL;

    if (g_wal_buf) PyMem_Free(g_wal_buf);
    g_wal_buf = (uint8_t *)PyMem_Calloc(1, buf_size);
    if (!g_wal_buf) return PyErr_NoMemory();
    g_wal_buf_capacity = buf_size;
    g_wal_buf_pos = 0;
    g_wal_seq = 0;
    g_wal_total = 0;
    g_wal_bytes_flushed = 0;
    g_wal_flush_count = 0;
    g_stat_events = g_stat_line_events = 0;
    g_stat_mutations_recorded = g_stat_getvar_calls = 0;
    g_n_frames = 0;

    /* Open output file if specified */
    if (g_wal_fd >= 0) { close(g_wal_fd); g_wal_fd = -1; }
    if (output_file) {
        g_wal_fd = open(output_file, O_WRONLY | O_CREAT | O_TRUNC, 0644);
        if (g_wal_fd < 0) {
            PyErr_SetFromErrnoWithFilename(PyExc_OSError, output_file);
            return NULL;
        }
    }

    PyEval_SetTrace(trace_wal, Py_None);
    Py_RETURN_NONE;
}

static PyObject *
ctrace_wal_stop(PyObject *self, PyObject *Py_UNUSED(ignored))
{
    PyEval_SetTrace(NULL, NULL);
    /* Flush remaining buffer to disk */
    if (g_wal_fd >= 0) {
        wal_flush_to_disk();
        close(g_wal_fd);
        g_wal_fd = -1;
    }
    Py_RETURN_NONE;
}

static PyObject *
ctrace_wal_stats(PyObject *self, PyObject *Py_UNUSED(ignored))
{
    return Py_BuildValue("{s:K,s:K,s:K,s:K,s:K,s:i,s:K,s:K,s:K,s:K}",
        "events", (unsigned long long)g_stat_events,
        "line_events", (unsigned long long)g_stat_line_events,
        "mutations_recorded", (unsigned long long)g_stat_mutations_recorded,
        "getvar_calls", (unsigned long long)g_stat_getvar_calls,
        "wal_total", (unsigned long long)g_wal_total,
        "registered_codes", g_n_codes,
        "buf_used", (unsigned long long)g_wal_buf_pos,
        "buf_capacity", (unsigned long long)g_wal_buf_capacity,
        "bytes_flushed", (unsigned long long)g_wal_bytes_flushed,
        "flush_count", (unsigned long long)g_wal_flush_count
    );
}

static const char *wal_event_names[] = {
    "?", "CREATE", "BIND", "UNBIND", "MUTATE", "SETATTR",
    "SETITEM", "DELITEM", "DELATTR", "DEALLOC",
    "LINE", "CALL", "RETURN", "EXCEPTION"
};

/* ---- Byte-stream read helpers for decoding ---- */
static inline uint8_t  wal_read_u8 (const uint8_t **p) { uint8_t  v = **p; (*p)++; return v; }
static inline uint16_t wal_read_u16(const uint8_t **p) { uint16_t v; memcpy(&v, *p, 2); *p += 2; return v; }
static inline uint32_t wal_read_u32(const uint8_t **p) { uint32_t v; memcpy(&v, *p, 4); *p += 4; return v; }
static inline int32_t  wal_read_i32(const uint8_t **p) { int32_t  v; memcpy(&v, *p, 4); *p += 4; return v; }
static inline int64_t  wal_read_i64(const uint8_t **p) { int64_t  v; memcpy(&v, *p, 8); *p += 8; return v; }
static inline double   wal_read_f64(const uint8_t **p) { double   v; memcpy(&v, *p, 8); *p += 8; return v; }

/* Decode a tagged value from the byte stream and return a Python object.
 * Advances *p past the value. */
static PyObject *wal_read_value_py(const uint8_t **p) {
    uint8_t tag = wal_read_u8(p);
    switch (tag) {
    case 0: Py_RETURN_NONE;                              /* None */
    case 1: return PyLong_FromLongLong(wal_read_i64(p)); /* int */
    case 2: return PyFloat_FromDouble(wal_read_f64(p));  /* float */
    case 3: return PyBool_FromLong(wal_read_u8(p));      /* bool */
    case 4: {                                            /* str */
        uint8_t len = wal_read_u8(p);
        PyObject *s = PyUnicode_FromStringAndSize((const char *)*p, len);
        *p += len;
        return s;
    }
    case 5: {                                            /* oid ref */
        uint32_t ref = wal_read_u32(p);
        return Py_BuildValue("{s:I}", "ref", (unsigned int)ref);
    }
    default: return PyUnicode_FromString("<unknown>");    /* 6 = unknown */
    }
}

static PyObject *
ctrace_wal_get_wal(PyObject *self, PyObject *args)
{
    int max_count = 100;
    if (!PyArg_ParseTuple(args, "|i", &max_count))
        return NULL;

    if (!g_wal_buf || g_wal_buf_pos == 0)
        return PyList_New(0);

    /* Walk the byte buffer from the beginning, decode each entry */
    PyObject *result = PyList_New(0);
    if (!result) return NULL;

    const uint8_t *p = g_wal_buf;
    const uint8_t *end = g_wal_buf + g_wal_buf_pos;
    int n = 0;

    while (p < end && n < max_count) {
        if (p + 15 > end) break; /* need at least a header */

        /* Read header: event(1) + seq(4) + oid(4) + line(4) + code_idx(2) */
        uint8_t event   = wal_read_u8(&p);
        uint32_t seq    = wal_read_u32(&p);
        uint32_t oid    = wal_read_u32(&p);
        int32_t line    = wal_read_i32(&p);
        uint16_t cidx   = wal_read_u16(&p);

        const char *evt_name = (event > 0 && event <= 13) ? wal_event_names[event] : "?";

        PyObject *entry = NULL;

        switch (event) {
        case WAL_CREATE: {
            uint8_t type_tag = wal_read_u8(&p);
            entry = Py_BuildValue("{s:I,s:s,s:I,s:i,s:b}",
                "seq", seq, "event", evt_name, "oid", oid,
                "line", line, "type_tag", type_tag);
            break;
        }
        case WAL_BIND: {
            uint16_t name_idx = wal_read_u16(&p);
            /* Resolve name string */
            const char *name_str = "?";
            if (cidx < g_n_codes) {
                CodeAnalysis *ca = &g_code_cache[cidx];
                if (name_idx < ca->n_locals) {
                    PyObject *nm = PyTuple_GET_ITEM(ca->varnames, name_idx);
                    name_str = PyUnicode_AsUTF8(nm);
                }
            }
            if (oid == OID_NONE) {
                /* Primitive bind — has inline value */
                PyObject *val = wal_read_value_py(&p);
                entry = Py_BuildValue("{s:I,s:s,s:I,s:i,s:s,s:N}",
                    "seq", seq, "event", evt_name, "oid", oid,
                    "line", line, "name", name_str, "value", val);
            } else {
                entry = Py_BuildValue("{s:I,s:s,s:I,s:i,s:s}",
                    "seq", seq, "event", evt_name, "oid", oid,
                    "line", line, "name", name_str);
            }
            break;
        }
        case WAL_UNBIND: {
            uint16_t name_idx = wal_read_u16(&p);
            const char *name_str = "?";
            if (cidx < g_n_codes) {
                CodeAnalysis *ca = &g_code_cache[cidx];
                if (name_idx < ca->n_locals) {
                    PyObject *nm = PyTuple_GET_ITEM(ca->varnames, name_idx);
                    name_str = PyUnicode_AsUTF8(nm);
                }
            }
            entry = Py_BuildValue("{s:I,s:s,s:I,s:i,s:s}",
                "seq", seq, "event", evt_name, "oid", oid,
                "line", line, "name", name_str);
            break;
        }
        case WAL_MUTATE: {
            uint16_t attr_idx = wal_read_u16(&p);
            uint8_t n_args = wal_read_u8(&p);
            const char *attr_str = "?";
            if (cidx < g_n_codes) {
                CodeAnalysis *ca = &g_code_cache[cidx];
                if (attr_idx < ca->n_strings && ca->strings[attr_idx])
                    attr_str = PyUnicode_AsUTF8(ca->strings[attr_idx]);
            }
            PyObject *args_list = PyList_New(n_args);
            for (int a = 0; a < n_args; a++) {
                PyList_SET_ITEM(args_list, a, wal_read_value_py(&p));
            }
            entry = Py_BuildValue("{s:I,s:s,s:I,s:i,s:s,s:N}",
                "seq", seq, "event", evt_name, "oid", oid,
                "line", line, "method", attr_str, "args", args_list);
            break;
        }
        case WAL_SETATTR: {
            uint16_t attr_idx = wal_read_u16(&p);
            const char *attr_str = "?";
            if (cidx < g_n_codes) {
                CodeAnalysis *ca = &g_code_cache[cidx];
                if (attr_idx < ca->n_strings && ca->strings[attr_idx])
                    attr_str = PyUnicode_AsUTF8(ca->strings[attr_idx]);
            }
            PyObject *val = wal_read_value_py(&p);
            entry = Py_BuildValue("{s:I,s:s,s:I,s:i,s:s,s:N}",
                "seq", seq, "event", evt_name, "oid", oid,
                "line", line, "attr", attr_str, "value", val);
            break;
        }
        case WAL_SETITEM: {
            PyObject *key = wal_read_value_py(&p);
            PyObject *val = wal_read_value_py(&p);
            entry = Py_BuildValue("{s:I,s:s,s:I,s:i,s:N,s:N}",
                "seq", seq, "event", evt_name, "oid", oid,
                "line", line, "key", key, "value", val);
            break;
        }
        case WAL_DELITEM: {
            PyObject *key = wal_read_value_py(&p);
            entry = Py_BuildValue("{s:I,s:s,s:I,s:i,s:N}",
                "seq", seq, "event", evt_name, "oid", oid,
                "line", line, "key", key);
            break;
        }
        case WAL_DELATTR: {
            uint16_t attr_idx = wal_read_u16(&p);
            const char *attr_str = "?";
            if (cidx < g_n_codes) {
                CodeAnalysis *ca = &g_code_cache[cidx];
                if (attr_idx < ca->n_strings && ca->strings[attr_idx])
                    attr_str = PyUnicode_AsUTF8(ca->strings[attr_idx]);
            }
            entry = Py_BuildValue("{s:I,s:s,s:I,s:i,s:s}",
                "seq", seq, "event", evt_name, "oid", oid,
                "line", line, "attr", attr_str);
            break;
        }
        case WAL_DEALLOC:
            entry = Py_BuildValue("{s:I,s:s,s:I,s:i}",
                "seq", seq, "event", evt_name, "oid", oid, "line", line);
            break;
        case WAL_LINE:
        case WAL_CALL:
        case WAL_EXCEPTION:
            entry = Py_BuildValue("{s:I,s:s,s:i,s:i}",
                "seq", seq, "event", evt_name,
                "line", line, "code_idx", (int)cidx);
            break;
        case WAL_RETURN: {
            PyObject *retval = wal_read_value_py(&p);
            entry = Py_BuildValue("{s:I,s:s,s:i,s:i,s:N}",
                "seq", seq, "event", evt_name,
                "line", line, "code_idx", (int)cidx, "retval", retval);
            break;
        }
        default:
            entry = Py_BuildValue("{s:I,s:s,s:I,s:i}",
                "seq", seq, "event", "?", "oid", oid, "line", line);
            break;
        }

        if (entry) {
            PyList_Append(result, entry);
            Py_DECREF(entry);
        }
        n++;
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
    if (g_wal_buf) { PyMem_Free(g_wal_buf); g_wal_buf = NULL; }
    g_wal_buf_capacity = 0;
    g_wal_buf_pos = 0;
    init_code_hash();
    oid_map_init();
    Py_RETURN_NONE;
}

static PyMethodDef methods[] = {
    {"register_code", ctrace_wal_register_code, METH_VARARGS, "Register code with mutation info."},
    {"start", (PyCFunction)ctrace_wal_start, METH_VARARGS | METH_KEYWORDS, "Start WAL tracing. Args: buf_size=64MB, output_file=None."},
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
