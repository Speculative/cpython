/*
 * tracewal.c — Inline WAL tracing for CPython eval loop
 *
 * Produces an object-centric Write-Ahead Log (WAL) with entries emitted
 * directly from bytecode handlers. Uses internal frame APIs for direct
 * localsplus access — no settrace, no PyFrame_GetVar.
 *
 * WAL entry format is identical to ctrace_wal_compact.c:
 *   Header: event(1) + seq(4) + oid(4) + line(4) + code_idx(2) = 15 bytes
 *   Values: tag(1) + variable payload
 *
 * Ported from trace_investigation/experiments/exp27_c_extension/ctrace_wal_compact.c
 * with these key changes:
 *   - Uses _PyFrame_GetCode() (borrowed ref) instead of PyFrame_GetCode()
 *   - Uses frame->localsplus[i] + PyStackRef_AsPyObjectBorrow() instead of PyFrame_GetVar()
 *   - Hooks called from bytecode handlers, not from settrace callback
 *   - No pending_mask/deferred reads needed (we intercept stores directly)
 */

#define Py_BUILD_CORE
#include "Python.h"
#include "pycore_interp.h"
#include "pycore_interpframe.h"
#include "pycore_code.h"
#include "pycore_stackref.h"
#include "pycore_tracewal.h"
#include "pycore_opcode_utils.h"   /* RESUME_AT_FUNC_START */
#include "pycore_traceback.h"      /* PyTracebackObject */

#include <string.h>
#include <unistd.h>
#include <fcntl.h>

/* ========================================================================
 * Configuration
 * ======================================================================== */

#define MAX_LOCALS          64
#define MAX_CODE_ENTRIES    4096
#define MAX_CACHED_FRAMES   256
#define CODE_HASH_SIZE      8191
/* Initial capacity of the oid map. Doubles on demand (grow at 75%
 * load). The map's size grows with objects-ever-tracked because we
 * have no destruction signal — see exp29 perf-optimization notes
 * for the full memory story and the planned tp_dealloc-hook fix. */
#define OID_MAP_INITIAL     16384  /* power of two for cheap modulo via mask */
#define STRING_INTERN_SIZE  4093

typedef uint32_t oid_t;
#define OID_NONE 0

/* WAL event types */
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
    WAL_RAISE,
    WAL_EXCEPT,
    WAL_SNAPSHOT,
    WAL_OBJ_SNAPSHOT,  /* user-object __dict__ snapshot */
} WALEventType;

/* Global enable flag */
int _PyWAL_enabled = 0;
int _PyWAL_line_mode = 1;  /* default: full LINE tracking */

/* ========================================================================
 * Object ID tracker
 * ======================================================================== */

typedef struct {
    uintptr_t cpython_id;
    oid_t oid;
    uint8_t type_tag;
    uint8_t occupied;
    /* Set whenever a SNAPSHOT was just emitted for this oid (initial or
     * refresh or post-call). Cleared by any event that mutates the
     * underlying object (SETITEM/SETATTR/MUTATE/DELITEM/DELATTR). When
     * the refresh path sees this set, it skips emitting — the existing
     * snapshot still reflects current contents, so the loader's state
     * is up to date. Catches the common BUILD_LIST → STORE_FAST case
     * where a fresh container is bound immediately after creation. */
    uint8_t snapshot_fresh;
    /* For type_tag = 10 (user object), the index in g_string_table of
     * Py_TYPE(obj)->tp_name. The loader uses this to render `__obj__`
     * snapshots with the actual class name (Counter, function, etc)
     * rather than a generic "object" placeholder. 0 means unset. */
    uint16_t type_name_idx;
} OidMapEntry;

static OidMapEntry *g_oid_map = NULL;
static size_t g_oid_map_capacity = 0;  /* always a power of two */
static size_t g_oid_map_count = 0;     /* live (occupied) entries */
static oid_t g_next_oid = 1;

/* Probe-step constant. The original 32-step linear probe is preserved
 * so collision behavior matches the static-array version. With a
 * resizable map at <= 75% load we should rarely exhaust 32 steps. */
#define OID_MAP_PROBE_STEPS 32

static void oid_map_init(void) {
    if (g_oid_map) {
        PyMem_Free(g_oid_map);
    }
    g_oid_map_capacity = OID_MAP_INITIAL;
    g_oid_map = (OidMapEntry *)PyMem_Calloc(g_oid_map_capacity, sizeof(OidMapEntry));
    g_oid_map_count = 0;
    g_next_oid = 1;
}

/* Grow the oid map to 2× capacity and rehash. Called from oid_create
 * when load passes 75%. All existing lookup/insert sites use the same
 * (h + p) & mask probe pattern — they pick up the new capacity
 * automatically once g_oid_map / g_oid_map_capacity are updated. No
 * outstanding pointers into the old array exist across function
 * boundaries, so the realloc is safe. */
static void oid_map_grow(void) {
    size_t old_cap = g_oid_map_capacity;
    OidMapEntry *old_map = g_oid_map;
    size_t new_cap = old_cap * 2;
    OidMapEntry *new_map = (OidMapEntry *)PyMem_Calloc(new_cap, sizeof(OidMapEntry));
    if (!new_map) {
        /* Out of memory — limp along with the existing map. Inserts
         * past capacity will fail to record but lookups still work. */
        return;
    }
    size_t mask = new_cap - 1;
    size_t live = 0;
    for (size_t i = 0; i < old_cap; i++) {
        OidMapEntry *src = &old_map[i];
        if (!src->occupied) continue;
        uintptr_t h = (src->cpython_id >> 4) & mask;
        for (int p = 0; p < (int)new_cap; p++) {
            OidMapEntry *dst = &new_map[(h + p) & mask];
            if (!dst->occupied) {
                *dst = *src;
                live++;
                break;
            }
        }
    }
    g_oid_map = new_map;
    g_oid_map_capacity = new_cap;
    g_oid_map_count = live;
    PyMem_Free(old_map);
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

static int is_primitive(PyObject *obj) {
    return (obj == Py_None || PyBool_Check(obj) || PyLong_Check(obj) ||
            PyFloat_Check(obj) || PyUnicode_Check(obj) || PyBytes_Check(obj));
}

static oid_t oid_lookup(uintptr_t cid) {
    uintptr_t h = (cid >> 4) & (g_oid_map_capacity - 1);
    for (int p = 0; p < 32; p++) {
        OidMapEntry *e = &g_oid_map[(h + p) & (g_oid_map_capacity - 1)];
        if (!e->occupied) return OID_NONE;
        if (e->cpython_id == cid) return e->oid;
    }
    return OID_NONE;
}

/* Forward decl */
static uint16_t string_intern(PyObject *name);

static oid_t oid_create(uintptr_t cid, uint8_t type_tag, PyObject *obj_for_typename) {
    /* Grow the map if it's getting full before we probe for a free slot.
     * 75% load keeps probe chains short; growth doubles capacity. */
    if (g_oid_map_count * 4 >= g_oid_map_capacity * 3) {
        oid_map_grow();
    }
    oid_t oid = g_next_oid++;
    uintptr_t h = (cid >> 4) & (g_oid_map_capacity - 1);
    for (int p = 0; p < 32; p++) {
        OidMapEntry *e = &g_oid_map[(h + p) & (g_oid_map_capacity - 1)];
        if (!e->occupied) {
            e->cpython_id = cid;
            e->oid = oid;
            e->type_tag = type_tag;
            e->occupied = 1;
            e->snapshot_fresh = 0;  /* set to 1 by oid_mark_snapshot_fresh */
            e->type_name_idx = UINT16_MAX;  /* "no name recorded" */
            /* For user objects, record Py_TYPE(obj)->tp_name so the loader
             * can render `__obj__` with the actual class name instead of
             * generic "object". Folded in here to avoid a second hash
             * lookup; obj_for_typename is NULL for callers that don't have
             * the PyObject available (e.g. wal_write_value's value-tagging
             * path). */
            if (type_tag == 10 && obj_for_typename) {
                PyObject *name_obj = PyUnicode_InternFromString(
                    Py_TYPE(obj_for_typename)->tp_name);
                if (name_obj) {
                    e->type_name_idx = string_intern(name_obj);
                    Py_DECREF(name_obj);
                } else {
                    PyErr_Clear();
                }
            }
            g_oid_map_count++;
            return oid;
        }
    }
    return oid; /* map full */
}


/* Find the oid_map entry for cid and set/clear its snapshot_fresh bit. */
static void oid_mark_snapshot_fresh(uintptr_t cid, uint8_t value) {
    uintptr_t h = (cid >> 4) & (g_oid_map_capacity - 1);
    for (int p = 0; p < 32; p++) {
        OidMapEntry *e = &g_oid_map[(h + p) & (g_oid_map_capacity - 1)];
        if (!e->occupied) return;
        if (e->cpython_id == cid) {
            e->snapshot_fresh = value;
            return;
        }
    }
}

static oid_t oid_get_or_create(PyObject *obj, int32_t line);

/* Like oid_get_or_create, but also clears snapshot_fresh on the matched
 * entry. Use at mutation sites (SETITEM, SETATTR, DELITEM, DELATTR,
 * MUTATE) so the existing lookup pulls double duty — no extra hash probe
 * for the bit clear. After the mutation event the loader's reconstructed
 * state diverges from any prior SNAPSHOT, so a future binding-site
 * refresh must re-snapshot rather than skip. */
static oid_t oid_get_or_create_for_mutation(PyObject *obj, int32_t line) {
    uintptr_t cid = (uintptr_t)obj;
    uint8_t type_tag = classify_type(obj);
    uintptr_t h = (cid >> 4) & (g_oid_map_capacity - 1);
    for (int p = 0; p < 32; p++) {
        OidMapEntry *e = &g_oid_map[(h + p) & (g_oid_map_capacity - 1)];
        if (!e->occupied) break;
        if (e->cpython_id == cid) {
            if (e->type_tag == type_tag) {
                /* Tuples are immutable — for_mutation is never called on
                 * them in practice, but invalidate defensively for symmetry
                 * with oid_get_or_create's freelist-window guard. */
                if (type_tag == 4 /* tuple */) {
                    e->occupied = 0;
                    if (g_oid_map_count) g_oid_map_count--;
                    break;
                }
                e->snapshot_fresh = 0;
                return e->oid;
            }
            e->occupied = 0;
            if (g_oid_map_count) g_oid_map_count--;
            break;
        }
    }
    return oid_get_or_create(obj, line);
}

static void oid_invalidate(uintptr_t cid) {
    if (!g_oid_map) return;
    uintptr_t h = (cid >> 4) & (g_oid_map_capacity - 1);
    for (int p = 0; p < 32; p++) {
        OidMapEntry *e = &g_oid_map[(h + p) & (g_oid_map_capacity - 1)];
        if (!e->occupied) return;
        if (e->cpython_id == cid) {
            e->occupied = 0;
            if (g_oid_map_count) g_oid_map_count--;
            return;
        }
    }
}

/* Forward declarations */
static void wal_emit_create(oid_t oid, uint8_t type_tag, int32_t line);
static void wal_emit_initial_snapshot(oid_t oid, uint8_t type_tag, PyObject *obj, int32_t line);

/* Set while wal_emit_initial_snapshot is iterating a container's items and
 * pre-creating oids for them. While set, the *_refresh variant skips the
 * existing-oid refresh path — otherwise we'd recursively re-snapshot every
 * nested mutable container, blowing the stack on cyclic or deep graphs. */
static int g_in_snapshot = 0;

/* Standard lookup-or-create. Used at mutation sites (SETITEM, SETATTR,
 * MUTATE method-call self) and value-tagging sites where the caller already
 * has a known oid. No refresh — the loader's reconstructed state is kept
 * current via SETITEM/SETATTR/MUTATE events. */
static oid_t oid_get_or_create(PyObject *obj, int32_t line) {
    uintptr_t cid = (uintptr_t)obj;
    uint8_t type_tag = classify_type(obj);

    uintptr_t h = (cid >> 4) & (g_oid_map_capacity - 1);
    for (int p = 0; p < 32; p++) {
        OidMapEntry *e = &g_oid_map[(h + p) & (g_oid_map_capacity - 1)];
        if (!e->occupied) break;
        if (e->cpython_id == cid) {
            if (e->type_tag == type_tag) {
                /* Tuples: still invalidate-on-lookup. The _Py_Dealloc hook
                 * catches most freed-tuple cases up front, but there's a
                 * timing window where a tuple is freed via the small-tuple
                 * freelist (memory cached, not actually returned to the
                 * allocator) and a *new* tuple at the same address is
                 * looked up before the original dealloc completes. Treating
                 * tuple-on-tuple address matches as fresh covers that
                 * window for free; correctness is unaffected for genuine
                 * same-object lookups since tuple contents are immutable. */
                if (type_tag == 4 /* tuple */) {
                    e->occupied = 0;
                    if (g_oid_map_count) g_oid_map_count--;
                    break;
                }
                return e->oid;
            }
            e->occupied = 0;
            if (g_oid_map_count) g_oid_map_count--;
            break;
        }
    }

    oid_t oid = oid_create(cid, type_tag, obj);
    wal_emit_create(oid, type_tag, line);
    wal_emit_initial_snapshot(oid, type_tag, obj, line);
    oid_mark_snapshot_fresh(cid, 1);
    return oid;
}

/* Like oid_get_or_create, but for binding sites — STORE_FAST, function arg
 * binding, RETURN value. The Python object being bound here might be at a
 * memory address recently freed by a now-dead earlier object, in which case
 * the cached oid_map entry has stale state from that earlier object. Emit a
 * SNAPSHOT for mutable containers so the loader resets state to the current
 * (post-rebirth) contents. Read/mutation sites use the plain
 * oid_get_or_create — there the caller already knows the oid so no
 * refresh is needed.
 *
 * If snapshot_fresh is set, skip — the existing snapshot is still
 * authoritative (no event has mutated this oid since we last emitted
 * SNAPSHOT). Catches the very common BUILD_LIST → STORE_FAST case
 * where a fresh container is bound immediately after creation. */
static oid_t oid_get_or_create_refresh(PyObject *obj, int32_t line) {
    if (g_in_snapshot) {
        return oid_get_or_create(obj, line);
    }
    uintptr_t cid = (uintptr_t)obj;
    uint8_t type_tag = classify_type(obj);
    uintptr_t h = (cid >> 4) & (g_oid_map_capacity - 1);
    for (int p = 0; p < 32; p++) {
        OidMapEntry *e = &g_oid_map[(h + p) & (g_oid_map_capacity - 1)];
        if (!e->occupied) break;
        if (e->cpython_id == cid && e->type_tag == type_tag) {
            if (type_tag == 1 || type_tag == 2 || type_tag == 3) {
                if (e->snapshot_fresh) {
                    return e->oid;
                }
                wal_emit_initial_snapshot(e->oid, type_tag, obj, line);
                e->snapshot_fresh = 1;
                return e->oid;
            }
            if (type_tag == 10) {
                /* No special handling needed — when the address was
                 * recycled, _Py_Dealloc's invalidation hook cleared the
                 * map entry, so we'd never reach this branch with a
                 * stale class. Mutations that don't change identity
                 * are already captured incrementally via SETATTR
                 * events; full OBJ_SNAPSHOT refresh isn't required. */
                return e->oid;
            }
            /* Other matchable types (notably tuple, type_tag = 4): the
             * cached entry is valid because the dealloc hook invalidates
             * on free, so an address reuse always lands as a miss above.
             * Fall through anyway — this branch is kept for defensive
             * symmetry with oid_get_or_create's same-type return. */
            break;
        }
    }
    return oid_get_or_create(obj, line);
}

/* ========================================================================
 * Statistics
 * ======================================================================== */

static uint64_t g_stat_events = 0;
static uint64_t g_stat_store_fast = 0;
static uint64_t g_stat_store_subscr = 0;
static uint64_t g_stat_store_attr = 0;
static uint64_t g_stat_calls = 0;
static uint64_t g_stat_returns = 0;
static uint64_t g_stat_mutations = 0;

/* ========================================================================
 * Compact WAL byte-stream buffer (identical format to ctrace_wal_compact.c)
 * ======================================================================== */

/* WALEventType defined above in forward declarations */

#define WAL_BUF_DEFAULT (64 * 1024 * 1024)  /* 64 MB */
#define WAL_MAX_ENTRY_SIZE 300

static uint8_t *g_wal_buf = NULL;
static size_t   g_wal_buf_capacity = 0;
static size_t   g_wal_buf_pos = 0;
static uint32_t g_wal_seq = 0;
static uint64_t g_wal_total = 0;

/* Disk flush */
static int      g_wal_fd = -1;
static uint64_t g_wal_bytes_flushed = 0;
static uint64_t g_wal_flush_count = 0;

static void wal_flush_to_disk(void) {
    if (g_wal_fd < 0 || g_wal_buf_pos == 0) return;
    ssize_t written = write(g_wal_fd, g_wal_buf, g_wal_buf_pos);
    if (written > 0) {
        g_wal_bytes_flushed += written;
    }
    g_wal_flush_count++;
    g_wal_buf_pos = 0;
}

static inline uint8_t* wal_reserve(size_t n) {
    if (!g_wal_buf) return NULL;
    if (g_wal_buf_pos + n > g_wal_buf_capacity) {
        if (g_wal_fd >= 0) {
            wal_flush_to_disk();
            if (g_wal_buf_pos + n > g_wal_buf_capacity) return NULL;
        } else {
            return NULL;
        }
    }
    return g_wal_buf + g_wal_buf_pos;
}

static inline void wal_finish(uint8_t *end) {
    g_wal_buf_pos = end - g_wal_buf;
}

/* Write helpers */
static inline void wal_write_u8(uint8_t **p, uint8_t v) { **p = v; (*p)++; }
static inline void wal_write_u16(uint8_t **p, uint16_t v) { memcpy(*p, &v, 2); *p += 2; }
static inline void wal_write_u32(uint8_t **p, uint32_t v) { memcpy(*p, &v, 4); *p += 4; }
static inline void wal_write_i32(uint8_t **p, int32_t v) { memcpy(*p, &v, 4); *p += 4; }
static inline void wal_write_i64(uint8_t **p, int64_t v) { memcpy(*p, &v, 8); *p += 8; }
static inline void wal_write_f64(uint8_t **p, double v) { memcpy(*p, &v, 8); *p += 8; }

static inline void wal_write_header(uint8_t **p, uint8_t event, oid_t oid,
                                     int32_t line, uint16_t code_idx) {
    wal_write_u8(p, event);
    wal_write_u32(p, ++g_wal_seq);
    wal_write_u32(p, oid);
    wal_write_i32(p, line);
    wal_write_u16(p, code_idx);
    g_wal_total++;
}

/* Write a tagged value from a PyObject* (borrowed ref) */
static inline void wal_write_value(uint8_t **p, PyObject *obj, int32_t line) {
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
            wal_write_u8(p, 6);
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
        wal_write_u8(p, 4);
        Py_ssize_t size;
        char *buf;
        PyBytes_AsStringAndSize(obj, &buf, &size);
        uint8_t len = size < 63 ? (uint8_t)size : 63;
        wal_write_u8(p, len);
        memcpy(*p, buf, len); *p += len;
    } else {
        /* Mutable/complex object — oid reference */
        oid_t ref = oid_lookup((uintptr_t)obj);
        if (ref == OID_NONE) {
            ref = oid_create((uintptr_t)obj, classify_type(obj), obj);
        }
        wal_write_u8(p, 5);
        wal_write_u32(p, ref);
    }
}

static void wal_emit_create(oid_t oid, uint8_t type_tag, int32_t line) {
    uint8_t *p = wal_reserve(15 + 1);
    if (!p) return;
    wal_write_header(&p, WAL_CREATE, oid, line, 0);
    wal_write_u8(&p, type_tag);
    wal_finish(p);
}

static void wal_emit_initial_snapshot(oid_t oid, uint8_t type_tag,
                                       PyObject *obj, int32_t line) {
    /* Emit SNAPSHOT of the current container contents. Used both at CREATE
     * time (initial state) and on existing-oid return for mutable containers
     * (refresh, since the underlying object may have been reused after GC).
     * Empty containers still emit an empty SNAPSHOT — the refresh path needs
     * that to clear stale state.
     *
     * Sets g_in_snapshot while iterating items so the recursive
     * oid_get_or_create calls below don't re-snapshot nested containers. */
    int saved_in_snapshot = g_in_snapshot;
    g_in_snapshot = 1;
    if (type_tag == 1 /* list */) {
        Py_ssize_t n = PyList_GET_SIZE(obj);
        if (n > 256) n = 256;
        /* Pre-create oids for non-primitive elements so wal_write_value
         * finds them via oid_lookup (avoids nested wal_reserve). */
        for (Py_ssize_t i = 0; i < n; i++) {
            PyObject *item = PyList_GET_ITEM(obj, i);
            if (item && !is_primitive(item)) {
                oid_get_or_create(item, line);
            }
        }
        uint8_t *p = wal_reserve(15 + 2 + n * 14);
        if (p) {
            wal_write_header(&p, WAL_SNAPSHOT, oid, line, 0);
            wal_write_u16(&p, (uint16_t)n);
            for (Py_ssize_t i = 0; i < n; i++) {
                wal_write_value(&p, PyList_GET_ITEM(obj, i), line);
            }
            wal_finish(p);
        }
    } else if (type_tag == 2 /* dict */) {
        PyObject *key, *val;
        Py_ssize_t pos = 0;
        Py_ssize_t pairs = PyDict_GET_SIZE(obj);
        if (pairs > 256) pairs = 256;
        /* Pre-create oids for non-primitive keys and values */
        pos = 0;
        while (PyDict_Next(obj, &pos, &key, &val)) {
            if (key && !is_primitive(key)) oid_get_or_create(key, line);
            if (val && !is_primitive(val)) oid_get_or_create(val, line);
        }
        /* Each key-value pair can be up to 2*65 bytes (two strings) + 2 tags */
        uint8_t *p = wal_reserve(15 + 2 + pairs * 140);
        if (p) {
            wal_write_header(&p, WAL_SNAPSHOT, oid, line, 0);
            wal_write_u16(&p, (uint16_t)(pairs * 2));
            Py_ssize_t n = 0;
            pos = 0;
            while (PyDict_Next(obj, &pos, &key, &val) && n < pairs) {
                wal_write_value(&p, key, line);
                wal_write_value(&p, val, line);
                n++;
            }
            wal_finish(p);
        }
    } else if (type_tag == 3 /* set */) {
        PyObject *iter = PyObject_GetIter(obj);
        if (iter) {
            PyObject *items[256];
            Py_ssize_t n = 0;
            PyObject *item;
            while (n < 256 && (item = PyIter_Next(iter)) != NULL) {
                items[n++] = item;
            }
            Py_DECREF(iter);
            uint8_t *p = wal_reserve(15 + 2 + n * 14);
            if (p) {
                wal_write_header(&p, WAL_SNAPSHOT, oid, line, 0);
                wal_write_u16(&p, (uint16_t)n);
                for (Py_ssize_t i = 0; i < n; i++) {
                    wal_write_value(&p, items[i], line);
                    Py_DECREF(items[i]);
                }
                wal_finish(p);
            } else {
                for (Py_ssize_t i = 0; i < n; i++) Py_DECREF(items[i]);
            }
        }
    } else if (type_tag == 4 /* tuple */) {
        Py_ssize_t n = PyTuple_GET_SIZE(obj);
        if (n > 256) n = 256;
        for (Py_ssize_t i = 0; i < n; i++) {
            PyObject *item = PyTuple_GET_ITEM(obj, i);
            if (item && !is_primitive(item)) oid_get_or_create(item, line);
        }
        uint8_t *p = wal_reserve(15 + 2 + n * 14);
        if (p) {
            wal_write_header(&p, WAL_SNAPSHOT, oid, line, 0);
            wal_write_u16(&p, (uint16_t)n);
            for (Py_ssize_t i = 0; i < n; i++) {
                wal_write_value(&p, PyTuple_GET_ITEM(obj, i), line);
            }
            wal_finish(p);
        }
    } else if (type_tag == 10 /* user object */) {
        /* Walk the object's instance dict if it has one. Objects without
         * __dict__ (slots-using classes, builtins, functions, ...) emit
         * an empty OBJ_SNAPSHOT — same effect as no snapshot for the
         * loader (clears any stale attrs). */
        PyObject **dictptr = _PyObject_GetDictPtr(obj);
        PyObject *dict = (dictptr && *dictptr) ? *dictptr : NULL;
        Py_ssize_t n = (dict && PyDict_Check(dict)) ? PyDict_GET_SIZE(dict) : 0;
        if (n > 256) n = 256;
        if (n > 0) {
            PyObject *key, *val;
            Py_ssize_t pos = 0, count = 0;
            while (PyDict_Next(dict, &pos, &key, &val) && count < n) {
                if (val && !is_primitive(val)) oid_get_or_create(val, line);
                count++;
            }
        }
        /* Capture the live class name in the event itself so the loader
         * uses the type at THIS moment rather than whatever ends up in
         * the bundle's static oid_type_names map (which captures only
         * the final type per oid — wrong if the same address is bound
         * to objects of different classes across the trace). */
        uint16_t type_name_idx = 0;
        PyObject *type_name_obj = PyUnicode_InternFromString(
            Py_TYPE(obj)->tp_name);
        if (type_name_obj) {
            type_name_idx = string_intern(type_name_obj);
            Py_DECREF(type_name_obj);
        } else {
            PyErr_Clear();
        }
        /* Header + type_idx + n + n × (attr_idx u16 + value tag+payload). */
        uint8_t *p = wal_reserve(15 + 2 + 2 + n * (2 + 82));
        if (p) {
            wal_write_header(&p, WAL_OBJ_SNAPSHOT, oid, line, 0);
            wal_write_u16(&p, type_name_idx);
            wal_write_u16(&p, (uint16_t)n);
            if (n > 0) {
                PyObject *key, *val;
                Py_ssize_t pos = 0, count = 0;
                while (PyDict_Next(dict, &pos, &key, &val) && count < n) {
                    if (PyUnicode_Check(key)) {
                        wal_write_u16(&p, string_intern(key));
                    } else {
                        wal_write_u16(&p, 0);
                    }
                    wal_write_value(&p, val, line);
                    count++;
                }
            }
            wal_finish(p);
        }
    }
    g_in_snapshot = saved_in_snapshot;
}

/* ========================================================================
 * Code analysis cache (simplified — no bytecode mutation info needed)
 * ======================================================================== */

typedef struct {
    PyObject *code_ref;   /* borrowed ref during tracing session */
    int n_locals;
    int n_args;           /* co_argcount + co_kwonlyargcount + varargs/varkeywords */
    uint16_t code_idx;
    int32_t *offset_to_line;  /* pre-computed offset→line table, NULL if not built */
    int n_offsets;             /* size of offset_to_line array */
    /* Surgical classifier — gates BIND/UNBIND/LINE emission for stdlib code.
     * `classified` is 0 until code_is_traceable() runs the user-supplied
     * classifier callback; `is_traceable` is the cached result (1 = emit
     * everything, 0 = skip BIND/UNBIND/LINE for this code). When no
     * classifier is registered, both bits stay 0 and code_is_traceable()
     * short-circuits to 1, so nothing changes for callers that don't opt in. */
    uint8_t is_traceable;
    uint8_t classified;
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

/* Build a pre-computed offset→line lookup table for a code object.
 * This replaces per-call PyCode_Addr2Line (~78% of traced overhead). */
static void build_offset_to_line(CodeAnalysis *ca, PyCodeObject *code) {
    Py_ssize_t code_len = Py_SIZE(code);
    if (code_len <= 0 || code_len > 100000) {
        ca->offset_to_line = NULL;
        ca->n_offsets = 0;
        return;
    }

    ca->n_offsets = (int)code_len;
    ca->offset_to_line = (int32_t *)PyMem_Calloc(code_len, sizeof(int32_t));
    if (!ca->offset_to_line) {
        ca->n_offsets = 0;
        return;
    }

    /* Walk the line table once and fill in all offsets */
    for (int i = 0; i < code_len; i++) {
        ca->offset_to_line[i] = (int32_t)PyCode_Addr2Line(
            code, i * (int)sizeof(_Py_CODEUNIT));
    }
}

/* Auto-register a code object on first encounter */
static int code_auto_register(PyCodeObject *code) {
    if (g_n_codes >= MAX_CODE_ENTRIES) return -1;

    int cache_idx = g_n_codes++;
    CodeAnalysis *ca = &g_code_cache[cache_idx];
    ca->code_ref = (PyObject *)code;
    ca->n_locals = code->co_nlocals;

    /* Compute n_args */
    int n_args = code->co_argcount + code->co_kwonlyargcount;
    if (code->co_flags & CO_VARARGS) n_args++;
    if (code->co_flags & CO_VARKEYWORDS) n_args++;
    if (n_args > ca->n_locals) n_args = ca->n_locals;
    ca->n_args = n_args;
    ca->code_idx = (uint16_t)cache_idx;

    /* Build offset→line lookup table */
    build_offset_to_line(ca, code);

    code_hash_insert((PyObject *)code, cache_idx);
    return cache_idx;
}

/* Lookup or auto-register */
static inline int code_get_idx(_PyInterpreterFrame *frame) {
    PyCodeObject *code = _PyFrame_GetCode(frame);
    int idx = code_hash_lookup((PyObject *)code);
    if (idx < 0) {
        idx = code_auto_register(code);
    }
    return idx;
}

/* User-supplied classifier callable: takes a code object, returns truthy
 * for "user code" (full event emission) and falsy for "stdlib / non-user
 * code" (BIND/UNBIND/LINE skipped). Set via _PyWAL_SetClassifier; NULL
 * means everything is treated as traceable. */
static PyObject *g_classifier = NULL;

/* Lazy classification: returns 1 if `code_idx` is traceable, 0 otherwise.
 * The first call per code_idx invokes the registered classifier (if any)
 * and caches the result on the CodeAnalysis entry. Subsequent calls are
 * a single branch + load — cheap enough for the hot path. */
static inline int code_is_traceable(int code_idx) {
    if (code_idx < 0 || code_idx >= g_n_codes) return 1;
    CodeAnalysis *ca = &g_code_cache[code_idx];
    if (ca->classified) return ca->is_traceable;
    if (!g_classifier || !ca->code_ref) {
        ca->is_traceable = 1;
        ca->classified = 1;
        return 1;
    }
    /* Disable WAL during the callback so any Python code the classifier
     * runs doesn't recursively trigger our own hooks. The bytecode
     * handlers all gate on _PyWAL_enabled, so toggling it here is enough.
     * Save and restore any pending exception around the call. */
    int saved_enabled = _PyWAL_enabled;
    _PyWAL_enabled = 0;
    PyObject *exc_type, *exc_val, *exc_tb;
    PyErr_Fetch(&exc_type, &exc_val, &exc_tb);
    PyObject *result = PyObject_CallOneArg(g_classifier, ca->code_ref);
    int traceable = 1;
    if (result) {
        int truth = PyObject_IsTrue(result);
        traceable = (truth > 0) ? 1 : 0;
        Py_DECREF(result);
    } else {
        PyErr_Clear();  /* swallow classifier errors — fail open */
    }
    PyErr_Restore(exc_type, exc_val, exc_tb);
    _PyWAL_enabled = saved_enabled;
    ca->is_traceable = (uint8_t)traceable;
    ca->classified = 1;
    return traceable;
}

/* ========================================================================
 * Frame cache
 * ======================================================================== */

typedef struct {
    _PyInterpreterFrame *frame;
    int code_idx;
    int32_t last_line;
    oid_t bound_oids[MAX_LOCALS];
} FrameCache;

static FrameCache g_frames[MAX_CACHED_FRAMES];
static int g_n_frames = 0;

static FrameCache* find_frame(_PyInterpreterFrame *frame) {
    for (int i = g_n_frames - 1; i >= 0; i--)
        if (g_frames[i].frame == frame)
            return &g_frames[i];
    return NULL;
}

static FrameCache* push_frame(_PyInterpreterFrame *frame, int code_idx) {
    if (g_n_frames >= MAX_CACHED_FRAMES)
        g_n_frames = MAX_CACHED_FRAMES / 2;
    FrameCache *fc = &g_frames[g_n_frames++];
    memset(fc, 0, sizeof(FrameCache));
    fc->frame = frame;
    fc->code_idx = code_idx;
    fc->last_line = -1;
    return fc;
}

static void pop_frame(_PyInterpreterFrame *frame) {
    if (g_n_frames > 0 && g_frames[g_n_frames - 1].frame == frame)
        g_n_frames--;
}

/* ========================================================================
 * Line number helper — cached to avoid repeated PyCode_Addr2Line calls
 *
 * PyCode_Addr2Line walks the line table on every call (~78% of traced
 * execution time before caching). Since consecutive bytecodes in the
 * same function are usually on the same source line, we cache the last
 * result per frame and only re-query when the instruction offset changes.
 * ======================================================================== */

/* Fast line lookup using code_idx from FrameCache — avoids hash lookup */
static inline int32_t get_line_fast(int code_idx, _PyInterpreterFrame *frame) {
    if (code_idx >= 0 && code_idx < g_n_codes) {
        CodeAnalysis *ca = &g_code_cache[code_idx];
        if (ca->offset_to_line) {
            int offset = (int)(frame->instr_ptr - _PyCode_CODE(_PyFrame_GetCode(frame)));
            if (offset >= 0 && offset < ca->n_offsets) {
                return ca->offset_to_line[offset];
            }
        }
    }
    /* Fallback */
    PyCodeObject *code = _PyFrame_GetCode(frame);
    int offset = (int)(frame->instr_ptr - _PyCode_CODE(code));
    return (int32_t)PyCode_Addr2Line(code, offset * (int)sizeof(_Py_CODEUNIT));
}

/* Slow path for when we don't have a code_idx */
static inline int32_t get_current_line(_PyInterpreterFrame *frame) {
    int code_idx = code_hash_lookup((PyObject *)_PyFrame_GetCode(frame));
    return get_line_fast(code_idx, frame);
}

/* Fast per-dispatch line tracking.
 * Avoids frame cache lookup by caching the last frame+line globally.
 * On every DISPATCH(), we check if the line changed. If so, emit WAL_LINE. */
static _PyInterpreterFrame *g_last_line_frame = NULL;
static int32_t g_last_line_num = -1;

/* Called from Objects/object.c::_Py_Dealloc before the type's tp_dealloc
 * runs. _Py_Dealloc is the universal funnel for every refcount-driven AND
 * GC-driven object death, so dropping a hash-probe-based invalidate here
 * gives us full coverage with no slot patching. Cost: one branch +
 * (on hit) one hash probe per dying PyObject. The oid_invalidate fast-
 * path (no g_oid_map → return) makes calls during shutdown / non-traced
 * runs effectively free. */
void
_PyWAL_OnObjectDealloc(PyObject *op)
{
    oid_invalidate((uintptr_t)op);
}

void
_PyWAL_CheckLine(_PyInterpreterFrame *frame)
{
    /* Get code_idx first so we can use the fast line lookup */
    int code_idx = code_get_idx(frame);
    if (code_idx < 0) return;

    /* LINE events for non-traceable code are dropped by the loader
     * unconditionally — skip emission entirely. */
    if (!code_is_traceable(code_idx)) return;

    int32_t line = get_line_fast(code_idx, frame);
    if (line == g_last_line_num && frame == g_last_line_frame) return;
    if (line <= 0) return;

    g_last_line_num = line;
    g_last_line_frame = frame;

    uint8_t *p = wal_reserve(15);
    if (p) {
        wal_write_header(&p, WAL_LINE, 0, line, (uint16_t)code_idx);
        wal_finish(p);
    }
}

/* Emit WAL_LINE if line changed since last emission for this frame */
static inline void maybe_emit_line(FrameCache *fc, _PyInterpreterFrame *frame,
                                    int32_t line) {
    if (line != fc->last_line && line > 0) {
        uint8_t *p = wal_reserve(15);
        if (p) {
            wal_write_header(&p, WAL_LINE, 0, line,
                             (uint16_t)fc->code_idx);
            wal_finish(p);
        }
        fc->last_line = line;
    }
}

/* ========================================================================
 * String intern table (for attr names in STORE_ATTR / DELETE_ATTR)
 * ======================================================================== */

typedef struct {
    PyObject *key;     /* interned string, borrowed */
    uint16_t idx;
    uint8_t  occupied;
} StringInternEntry;

static StringInternEntry g_strings[STRING_INTERN_SIZE];
static PyObject *g_string_table[STRING_INTERN_SIZE]; /* strong refs for WAL decoder */
static int g_n_strings = 0;

static void string_intern_init(void) {
    memset(g_strings, 0, sizeof(g_strings));
    g_n_strings = 0;
}

static uint16_t string_intern(PyObject *name) {
    uintptr_t h = ((uintptr_t)name >> 3) % STRING_INTERN_SIZE;
    for (int p = 0; p < 32; p++) {
        int slot = (h + p) % STRING_INTERN_SIZE;
        StringInternEntry *e = &g_strings[slot];
        if (!e->occupied) {
            if (g_n_strings >= STRING_INTERN_SIZE) return 0; /* full */
            e->key = name;
            e->idx = (uint16_t)g_n_strings;
            e->occupied = 1;
            g_string_table[g_n_strings] = name;
            Py_INCREF(name);
            return (uint16_t)g_n_strings++;
        }
        if (e->key == name) return e->idx;
    }
    return 0; /* hash table full in this chain */
}

/* ========================================================================
 * Hook implementations — called from bytecode handlers
 * ======================================================================== */

void
_PyWAL_OnResume(_PyInterpreterFrame *frame, int oparg)
{
    g_stat_events++;

    int code_idx = code_get_idx(frame);
    if (code_idx < 0) return;

    if ((oparg & RESUME_OPARG_LOCATION_MASK) == RESUME_AT_FUNC_START) {
        /* Function entry — emit CALL and bind arguments */
        g_stat_calls++;
        CodeAnalysis *ca = &g_code_cache[code_idx];
        FrameCache *fc = push_frame(frame, code_idx);

        int32_t line = get_line_fast(code_idx, frame);
        fc->last_line = line;

        /* Emit CALL */
        {
            uint8_t *p = wal_reserve(15);
            if (p) {
                wal_write_header(&p, WAL_CALL, 0, line, (uint16_t)code_idx);
                wal_finish(p);
            }
        }

        /* Skip arg-binding for non-traceable code — the loader drops these
         * BINDs anyway (frame.frame_id == -1). CALL above still fires so the
         * frame stack stays balanced and any nested user-code calls have
         * the right parent frame chain. */
        if (!code_is_traceable(code_idx)) {
            return;
        }

        /* Capture argument bindings */
        int n_args = ca->n_args;
        for (int i = 0; i < n_args && i < MAX_LOCALS; i++) {
            _PyStackRef ref = frame->localsplus[i];
            if (PyStackRef_IsNull(ref)) continue;
            PyObject *val = PyStackRef_AsPyObjectBorrow(ref);

            if (is_primitive(val)) {
                /* Primitive: emit BIND with inline value */
                uint8_t *p = wal_reserve(15 + 2 + 82);
                if (p) {
                    wal_write_header(&p, WAL_BIND, OID_NONE, line, (uint16_t)code_idx);
                    wal_write_u16(&p, (uint16_t)i);
                    wal_write_value(&p, val, line);
                    wal_finish(p);
                }
            } else {
                oid_t oid = oid_get_or_create_refresh(val, line);
                fc->bound_oids[i] = oid;
                uint8_t *p = wal_reserve(15 + 2);
                if (p) {
                    wal_write_header(&p, WAL_BIND, oid, line, (uint16_t)code_idx);
                    wal_write_u16(&p, (uint16_t)i);
                    wal_finish(p);
                }
            }
        }
    } else if ((oparg & RESUME_OPARG_LOCATION_MASK) == RESUME_AFTER_YIELD) {
        /* Generator resume — update frame's last_line */
        FrameCache *fc = find_frame(frame);
        if (fc) {
            int32_t line = get_line_fast(fc->code_idx, frame);
        }
    }
}

void
_PyWAL_OnStoreFast(_PyInterpreterFrame *frame, int local_idx,
                    _PyStackRef old_val, _PyStackRef new_val)
{
    g_stat_events++;
    g_stat_store_fast++;

    if (local_idx >= MAX_LOCALS) return;

    FrameCache *fc = find_frame(frame);
    if (!fc) return;

    /* Skip BIND/UNBIND emission for non-traceable code (the loader drops
     * them by frame.frame_id check). Also skip the bound_oids bookkeeping —
     * the FrameCache for this frame stays at its initial zeros, OnReturn
     * has nothing to unbind, and we never read bound_oids back from a
     * non-traceable frame. */
    if (!code_is_traceable(fc->code_idx)) return;

    int32_t line = get_line_fast(fc->code_idx, frame);

    PyObject *new_obj = PyStackRef_IsNull(new_val) ? NULL :
                        PyStackRef_AsPyObjectBorrow(new_val);
    if (!new_obj) return;

    PyObject *old_obj = PyStackRef_IsNull(old_val) ? NULL :
                        PyStackRef_AsPyObjectBorrow(old_val);

    oid_t old_oid = fc->bound_oids[local_idx];

    if (is_primitive(new_obj)) {
        /* Unbind old if present */
        if (old_oid != OID_NONE) {
            uint8_t *p = wal_reserve(15 + 2);
            if (p) {
                wal_write_header(&p, WAL_UNBIND, old_oid, line,
                                 (uint16_t)fc->code_idx);
                wal_write_u16(&p, (uint16_t)local_idx);
                wal_finish(p);
            }
        }
        /* Primitive bind with inline value */
        uint8_t *p = wal_reserve(15 + 2 + 82);
        if (p) {
            wal_write_header(&p, WAL_BIND, OID_NONE, line,
                             (uint16_t)fc->code_idx);
            wal_write_u16(&p, (uint16_t)local_idx);
            wal_write_value(&p, new_obj, line);
            wal_finish(p);
        }
        fc->bound_oids[local_idx] = OID_NONE;
    } else {
        oid_t new_oid = oid_get_or_create_refresh(new_obj, line);

        if (old_oid != OID_NONE && old_oid != new_oid) {
            uint8_t *p = wal_reserve(15 + 2);
            if (p) {
                wal_write_header(&p, WAL_UNBIND, old_oid, line,
                                 (uint16_t)fc->code_idx);
                wal_write_u16(&p, (uint16_t)local_idx);
                wal_finish(p);
            }
        }

        if (new_oid != old_oid) {
            uint8_t *p = wal_reserve(15 + 2);
            if (p) {
                wal_write_header(&p, WAL_BIND, new_oid, line,
                                 (uint16_t)fc->code_idx);
                wal_write_u16(&p, (uint16_t)local_idx);
                wal_finish(p);
            }
        }

        fc->bound_oids[local_idx] = new_oid;
    }
}

void
_PyWAL_OnStoreSubscr(_PyInterpreterFrame *frame,
                      PyObject *container, PyObject *sub, PyObject *value)
{
    g_stat_events++;
    g_stat_store_subscr++;

    FrameCache *fc = find_frame(frame);
    if (!fc) return;

    int32_t line = get_line_fast(fc->code_idx, frame);

    /* Pre-create oids for complex values before starting WAL entry */
    if (!is_primitive(container)) {
        oid_get_or_create(container, line);
    }
    if (value && !is_primitive(value)) {
        oid_get_or_create(value, line);
    }

    oid_t container_oid = is_primitive(container) ? OID_NONE :
                          oid_get_or_create_for_mutation(container, line);

    uint8_t *p = wal_reserve(WAL_MAX_ENTRY_SIZE);
    if (p) {
        wal_write_header(&p, WAL_SETITEM, container_oid, line,
                         (uint16_t)fc->code_idx);
        wal_write_value(&p, sub, line);
        wal_write_value(&p, value, line);
        wal_finish(p);
    }
}

void
_PyWAL_OnStoreAttr(_PyInterpreterFrame *frame,
                    PyObject *owner, PyObject *name, PyObject *value)
{
    g_stat_events++;
    g_stat_store_attr++;

    FrameCache *fc = find_frame(frame);
    if (!fc) return;

    int32_t line = get_line_fast(fc->code_idx, frame);

    /* Pre-create oids */
    if (!is_primitive(owner)) {
        oid_get_or_create(owner, line);
    }
    if (value && !is_primitive(value)) {
        oid_get_or_create(value, line);
    }

    oid_t owner_oid = is_primitive(owner) ? OID_NONE :
                      oid_get_or_create_for_mutation(owner, line);

    uint16_t name_idx = string_intern(name);

    uint8_t *p = wal_reserve(WAL_MAX_ENTRY_SIZE);
    if (p) {
        wal_write_header(&p, WAL_SETATTR, owner_oid, line,
                         (uint16_t)fc->code_idx);
        wal_write_u16(&p, name_idx);
        wal_write_value(&p, value, line);
        wal_finish(p);
    }
}

void
_PyWAL_OnDeleteSubscr(_PyInterpreterFrame *frame,
                       PyObject *container, PyObject *sub)
{
    g_stat_events++;

    FrameCache *fc = find_frame(frame);
    if (!fc) return;

    int32_t line = get_line_fast(fc->code_idx, frame);

    oid_t container_oid = is_primitive(container) ? OID_NONE :
                          oid_get_or_create_for_mutation(container, line);

    uint8_t *p = wal_reserve(WAL_MAX_ENTRY_SIZE);
    if (p) {
        wal_write_header(&p, WAL_DELITEM, container_oid, line,
                         (uint16_t)fc->code_idx);
        wal_write_value(&p, sub, line);
        wal_finish(p);
    }
}

void
_PyWAL_OnDeleteAttr(_PyInterpreterFrame *frame,
                     PyObject *owner, PyObject *name)
{
    g_stat_events++;

    FrameCache *fc = find_frame(frame);
    if (!fc) return;

    int32_t line = get_line_fast(fc->code_idx, frame);

    oid_t owner_oid = is_primitive(owner) ? OID_NONE :
                      oid_get_or_create_for_mutation(owner, line);

    uint16_t name_idx = string_intern(name);

    uint8_t *p = wal_reserve(15 + 2);
    if (p) {
        wal_write_header(&p, WAL_DELATTR, owner_oid, line,
                         (uint16_t)fc->code_idx);
        wal_write_u16(&p, name_idx);
        wal_finish(p);
    }
}

void
_PyWAL_OnReturn(_PyInterpreterFrame *frame, PyObject *retval)
{
    g_stat_events++;
    g_stat_returns++;

    FrameCache *fc = find_frame(frame);
    if (!fc) return;

    int32_t line = get_line_fast(fc->code_idx, frame);
    int code_idx = fc->code_idx;

    /* Unbind all locals */
    PyCodeObject *code = _PyFrame_GetCode(frame);
    int n_locals = code->co_nlocals;
    for (int i = 0; i < n_locals && i < MAX_LOCALS; i++) {
        if (fc->bound_oids[i] != OID_NONE) {
            uint8_t *p = wal_reserve(15 + 2);
            if (p) {
                wal_write_header(&p, WAL_UNBIND, fc->bound_oids[i], line,
                                 (uint16_t)code_idx);
                wal_write_u16(&p, (uint16_t)i);
                wal_finish(p);
            }
            /* Note: we do NOT invalidate oids here. The objects may still
             * be alive (referenced by caller, return value, or globals).
             * Oid invalidation for id() reuse is handled by type_tag
             * mismatch detection in oid_get_or_create. */
        }
    }

    /* Emit RETURN with return value. Use the refresh variant: a returned
     * value crosses a frame boundary and might land in a caller's slot
     * whose previous occupant was at the same address — a snapshot here
     * makes sure the loader sees the current contents. */
    if (retval && !is_primitive(retval)) {
        oid_get_or_create_refresh(retval, line);
    }
    uint8_t *p = wal_reserve(WAL_MAX_ENTRY_SIZE);
    if (p) {
        wal_write_header(&p, WAL_RETURN, 0, line, (uint16_t)code_idx);
        if (retval) {
            wal_write_value(&p, retval, line);
        } else {
            wal_write_u8(&p, 0);
        }
        wal_finish(p);
    }

    pop_frame(frame);
}

void
_PyWAL_OnYield(_PyInterpreterFrame *frame, PyObject *retval)
{
    g_stat_events++;

    FrameCache *fc = find_frame(frame);
    if (!fc) return;

    int32_t line = get_line_fast(fc->code_idx, frame);

    /* Emit RETURN event for yield (frame stays alive, no unbind) */
    if (retval && !is_primitive(retval)) {
        oid_get_or_create(retval, line);
    }
    uint8_t *p = wal_reserve(WAL_MAX_ENTRY_SIZE);
    if (p) {
        wal_write_header(&p, WAL_RETURN, 0, line, (uint16_t)fc->code_idx);
        if (retval) {
            wal_write_value(&p, retval, line);
        } else {
            wal_write_u8(&p, 0);
        }
        wal_finish(p);
    }
}

/* ========================================================================
 * Global / nonlocal (STORE_DEREF) variable hooks
 * ======================================================================== */

void
_PyWAL_OnStoreGlobal(_PyInterpreterFrame *frame,
                      PyObject *name, PyObject *value)
{
    g_stat_events++;

    FrameCache *fc = find_frame(frame);
    if (!fc) return;

    int32_t line = get_line_fast(fc->code_idx, frame);

    /* Globals are stored in a dict — we emit SETITEM on the globals dict.
     * But for debugger display, we use a BIND-like event with the
     * global name. We'll reuse SETATTR with the globals dict as the
     * "owner" since it's semantically setting an attribute of the module. */
    PyObject *globals_dict = frame->f_globals;
    if (!globals_dict) return;

    oid_t dict_oid = oid_get_or_create_for_mutation(globals_dict, line);
    if (value && !is_primitive(value)) {
        oid_get_or_create(value, line);
    }

    uint16_t name_idx = string_intern(name);

    uint8_t *p = wal_reserve(WAL_MAX_ENTRY_SIZE);
    if (p) {
        wal_write_header(&p, WAL_SETATTR, dict_oid, line,
                         (uint16_t)fc->code_idx);
        wal_write_u16(&p, name_idx);
        wal_write_value(&p, value, line);
        wal_finish(p);
    }
}

void
_PyWAL_OnStoreDeref(_PyInterpreterFrame *frame,
                     int cell_idx, PyObject *value)
{
    g_stat_events++;

    FrameCache *fc = find_frame(frame);
    if (!fc) return;

    /* Same gating as STORE_FAST — cell BIND/UNBIND are dropped by the
     * loader for non-traceable frames. */
    if (!code_is_traceable(fc->code_idx)) return;

    int32_t line = get_line_fast(fc->code_idx, frame);

    /* cell_idx is the localsplus index of the cell.
     * The variable name is in co_localsplusnames[cell_idx]. */
    PyCodeObject *code = _PyFrame_GetCode(frame);
    const char *varname = "?";
    if (cell_idx < code->co_nlocalsplus) {
        PyObject *names = code->co_localsplusnames;
        PyObject *nm = PyTuple_GET_ITEM(names, cell_idx);
        varname = PyUnicode_AsUTF8(nm);
        if (!varname) { PyErr_Clear(); varname = "?"; }
    }

    /* Emit as BIND — the cell variable is conceptually a variable binding */
    if (is_primitive(value)) {
        uint8_t *p = wal_reserve(15 + 2 + 82);
        if (p) {
            wal_write_header(&p, WAL_BIND, OID_NONE, line,
                             (uint16_t)fc->code_idx);
            /* Use cell_idx as the name index — but we need the name in the WAL.
             * We'll intern the name and write it as a u16. */
            PyObject *nm = PyTuple_GET_ITEM(code->co_localsplusnames, cell_idx);
            wal_write_u16(&p, (uint16_t)cell_idx);
            wal_write_value(&p, value, line);
            wal_finish(p);
        }
    } else {
        oid_t new_oid = oid_get_or_create(value, line);
        oid_t old_oid = (cell_idx < MAX_LOCALS) ? fc->bound_oids[cell_idx] : OID_NONE;

        if (old_oid != OID_NONE && old_oid != new_oid) {
            uint8_t *p = wal_reserve(15 + 2);
            if (p) {
                wal_write_header(&p, WAL_UNBIND, old_oid, line,
                                 (uint16_t)fc->code_idx);
                wal_write_u16(&p, (uint16_t)cell_idx);
                wal_finish(p);
            }
        }
        if (new_oid != old_oid) {
            uint8_t *p = wal_reserve(15 + 2);
            if (p) {
                wal_write_header(&p, WAL_BIND, new_oid, line,
                                 (uint16_t)fc->code_idx);
                wal_write_u16(&p, (uint16_t)cell_idx);
                wal_finish(p);
            }
        }
        if (cell_idx < MAX_LOCALS) {
            fc->bound_oids[cell_idx] = new_oid;
        }
    }
}

/* ========================================================================
 * Post-call snapshots for opaque C mutations (sort, reverse)
 * ======================================================================== */

#define MAX_PENDING_SNAPSHOTS 4

static struct {
    oid_t oid;
    PyObject *obj;      /* borrowed ref — only valid until _DO_CALL returns */
    int32_t line;
    uint16_t code_idx;
} g_pending_snapshots[MAX_PENDING_SNAPSHOTS];
int _PyWAL_pending_snapshots = 0;

static int needs_post_call_snapshot(const char *name, PyObject *self) {
    /* Methods whose result can't be reliably reconstructed from inputs:
     * - list.sort: reorders by comparison, key= is a callable we can't replay
     * - list.reverse / deque.reverse: reconstructable, but cheap insurance
     * - deque.rotate: reconstructable if n captured, but safer to snapshot
     * - set.pop: removes arbitrary element, order is implementation-defined
     *
     * list.pop, dict.pop, dict.popitem, deque.pop/popleft are all
     * reconstructable from the replayer's state, so no snapshot needed.
     */
    if (strcmp(name, "sort") == 0) return 1;
    if (strcmp(name, "reverse") == 0) return 1;
    if (strcmp(name, "rotate") == 0) return 1;
    if (strcmp(name, "pop") == 0 && PySet_Check(self)) return 1;
    return 0;
}

static void emit_snapshot(oid_t oid, PyObject *obj, int32_t line, uint16_t code_idx) {
    /* Serialize list/dict/set contents as a sequence of values.
     * Format: SNAPSHOT header + n_items(u16) + value[n_items] */
    if (PyList_Check(obj)) {
        Py_ssize_t n = PyList_GET_SIZE(obj);
        if (n > 256) n = 256; /* cap to avoid huge entries */

        /* Pre-create oids for non-primitive elements */
        for (Py_ssize_t i = 0; i < n; i++) {
            PyObject *item = PyList_GET_ITEM(obj, i);
            if (item && !is_primitive(item)) {
                oid_get_or_create(item, line);
            }
        }

        uint8_t *p = wal_reserve(15 + 2 + n * 14);
        if (p) {
            wal_write_header(&p, WAL_SNAPSHOT, oid, line, code_idx);
            wal_write_u16(&p, (uint16_t)n);
            for (Py_ssize_t i = 0; i < n; i++) {
                wal_write_value(&p, PyList_GET_ITEM(obj, i), line);
            }
            wal_finish(p);
        }
    } else if (PySet_Check(obj) || PyFrozenSet_Check(obj)) {
        Py_ssize_t n = PySet_GET_SIZE(obj);
        if (n > 256) n = 256;

        /* Iterate the set to get elements */
        PyObject *iter = PyObject_GetIter(obj);
        if (!iter) { PyErr_Clear(); return; }

        /* Pre-create oids */
        PyObject *items[256];
        Py_ssize_t count = 0;
        PyObject *item;
        while (count < n && (item = PyIter_Next(iter)) != NULL) {
            if (!is_primitive(item)) {
                oid_get_or_create(item, line);
            }
            items[count++] = item;
        }
        Py_DECREF(iter);

        uint8_t *p = wal_reserve(15 + 2 + count * 14);
        if (p) {
            wal_write_header(&p, WAL_SNAPSHOT, oid, line, code_idx);
            wal_write_u16(&p, (uint16_t)count);
            for (Py_ssize_t i = 0; i < count; i++) {
                wal_write_value(&p, items[i], line);
                Py_DECREF(items[i]);
            }
            wal_finish(p);
        } else {
            for (Py_ssize_t i = 0; i < count; i++) Py_DECREF(items[i]);
        }
    } else {
        /* Deque or other sequence — try generic iteration */
        PyObject *as_list = PySequence_List(obj);
        if (!as_list) { PyErr_Clear(); return; }

        Py_ssize_t n = PyList_GET_SIZE(as_list);
        if (n > 256) n = 256;

        for (Py_ssize_t i = 0; i < n; i++) {
            PyObject *item = PyList_GET_ITEM(as_list, i);
            if (item && !is_primitive(item)) {
                oid_get_or_create(item, line);
            }
        }

        uint8_t *p = wal_reserve(15 + 2 + n * 14);
        if (p) {
            wal_write_header(&p, WAL_SNAPSHOT, oid, line, code_idx);
            wal_write_u16(&p, (uint16_t)n);
            for (Py_ssize_t i = 0; i < n; i++) {
                wal_write_value(&p, PyList_GET_ITEM(as_list, i), line);
            }
            wal_finish(p);
        }
        Py_DECREF(as_list);
    }
}

void
_PyWAL_FlushPendingSnapshots(_PyInterpreterFrame *frame)
{
    for (int i = 0; i < _PyWAL_pending_snapshots; i++) {
        emit_snapshot(g_pending_snapshots[i].oid,
                      g_pending_snapshots[i].obj,
                      g_pending_snapshots[i].line,
                      g_pending_snapshots[i].code_idx);
        oid_mark_snapshot_fresh((uintptr_t)g_pending_snapshots[i].obj, 1);
    }
    _PyWAL_pending_snapshots = 0;
}

/* ========================================================================
 * Known mutating methods — checked on CALL to emit WAL_MUTATE
 * ======================================================================== */

static int is_known_mutating_method(const char *name) {
    /* These must match KNOWN_MUTATING_METHODS in the Python analyzer */
    static const char *methods[] = {
        "append", "clear", "extend", "insert", "pop", "remove",
        "reverse", "sort", "add", "discard",
        "difference_update", "intersection_update",
        "symmetric_difference_update", "update",
        "appendleft", "extendleft", "popleft", "rotate",
        "setdefault", "popitem", NULL
    };
    for (const char **m = methods; *m; m++) {
        if (strcmp(name, *m) == 0) return 1;
    }
    return 0;
}

void
_PyWAL_OnCall(_PyInterpreterFrame *frame,
              PyObject *callable, PyObject *self_or_null,
              _PyStackRef *args, int oparg)
{
    if (!self_or_null) return;

    /* Check if this is a method descriptor call on a mutable object */
    const char *method_name = NULL;

    if (Py_IS_TYPE(callable, &PyMethodDescr_Type)) {
        PyMethodDescrObject *descr = (PyMethodDescrObject *)callable;
        method_name = descr->d_method->ml_name;
    } else if (Py_IS_TYPE(callable, &PyCFunction_Type)) {
        PyCFunctionObject *cfunc = (PyCFunctionObject *)callable;
        method_name = cfunc->m_ml->ml_name;
    }

    if (!method_name || !is_known_mutating_method(method_name)) return;

    /* self_or_null is the object being mutated */
    if (is_primitive(self_or_null)) return;

    FrameCache *fc = find_frame(frame);
    if (!fc) return;

    g_stat_events++;
    g_stat_mutations++;

    int32_t line = get_line_fast(fc->code_idx, frame);

    /* Pre-create oids for all values. self uses the for_mutation variant
     * — the method is about to mutate self, so any existing snapshot is
     * stale and the snapshot_fresh bit is cleared as part of the lookup. */
    oid_t self_oid = oid_get_or_create_for_mutation(self_or_null, line);
    for (int i = 0; i < oparg; i++) {
        PyObject *arg_obj = PyStackRef_AsPyObjectBorrow(args[i]);
        if (arg_obj && !is_primitive(arg_obj)) {
            oid_get_or_create(arg_obj, line);
        }
    }

    /* Intern method name */
    uint16_t method_idx = string_intern(PyUnicode_InternFromString(method_name));

    /* Emit WAL_MUTATE */
    uint8_t *p = wal_reserve(WAL_MAX_ENTRY_SIZE);
    if (p) {
        wal_write_header(&p, WAL_MUTATE, self_oid, line,
                         (uint16_t)fc->code_idx);
        wal_write_u16(&p, method_idx);
        uint8_t n_args = oparg > 4 ? 4 : (uint8_t)oparg;
        wal_write_u8(&p, n_args);
        for (int i = 0; i < n_args; i++) {
            PyObject *arg_obj = PyStackRef_AsPyObjectBorrow(args[i]);
            wal_write_value(&p, arg_obj, line);
        }
        wal_finish(p);
    }

    /* Schedule post-call snapshot for opaque C mutations.
     * (For sort/reverse/rotate/set.pop the FlushPending code below sets
     * snapshot_fresh = 1 again; for other methods, oid_get_or_create_for_mutation
     * above already cleared it.) */
    if (needs_post_call_snapshot(method_name, self_or_null) &&
        _PyWAL_pending_snapshots < MAX_PENDING_SNAPSHOTS) {
        int i = _PyWAL_pending_snapshots++;
        g_pending_snapshots[i].oid = self_oid;
        g_pending_snapshots[i].obj = self_or_null;
        g_pending_snapshots[i].line = line;
        g_pending_snapshots[i].code_idx = (uint16_t)fc->code_idx;
    }
}

/* ========================================================================
 * Exception tracing hooks
 * ======================================================================== */

void
_PyWAL_OnRaise(_PyInterpreterFrame *frame, PyObject *exc)
{
    if (!exc) return;
    g_stat_events++;

    int code_idx = code_get_idx(frame);
    int32_t line = get_current_line(frame);

    /* Write exc type name as a string value */
    PyObject *type_name = NULL;
    PyObject *tp = (PyObject *)Py_TYPE(exc);
    if (tp) {
        type_name = PyObject_GetAttrString(tp, "__name__");
    }

    uint8_t *p = wal_reserve(WAL_MAX_ENTRY_SIZE);
    if (p) {
        wal_write_header(&p, WAL_RAISE, 0, line,
                         code_idx >= 0 ? (uint16_t)code_idx : 0);
        if (type_name) {
            wal_write_value(&p, type_name, line);
            Py_DECREF(type_name);
        } else {
            wal_write_u8(&p, 6); /* unknown */
        }
        /* Also write the str(exc) for the message */
        PyObject *exc_str = PyObject_Str(exc);
        if (exc_str) {
            wal_write_value(&p, exc_str, line);
            Py_DECREF(exc_str);
        } else {
            PyErr_Clear();
            wal_write_u8(&p, 0); /* None */
        }
        wal_finish(p);
    } else {
        Py_XDECREF(type_name);
    }
}

void
_PyWAL_OnExceptStart(_PyInterpreterFrame *frame, PyObject *exc)
{
    if (!exc) return;
    g_stat_events++;

    int code_idx = code_get_idx(frame);
    int32_t handler_line = get_current_line(frame);

    /* Get the origin line from the exception's traceback.
     * At PUSH_EXC_INFO time, the traceback may or may not be attached yet.
     * Also check tstate's current exception info. */
    int32_t origin_line = -1;
    PyObject *tb = PyException_GetTraceback(exc);
    if (tb) {
        origin_line = (int32_t)((PyTracebackObject *)tb)->tb_lineno;
        Py_DECREF(tb);
    }
    if (origin_line <= 0) {
        /* Fallback: try to get from the current traceback on tstate */
        PyObject *cur_exc = PyErr_GetRaisedException();
        if (cur_exc) {
            tb = PyException_GetTraceback(cur_exc);
            if (tb) {
                origin_line = (int32_t)((PyTracebackObject *)tb)->tb_lineno;
                Py_DECREF(tb);
            }
            PyErr_SetRaisedException(cur_exc);
        }
    }

    PyObject *type_name = NULL;
    PyObject *tp = (PyObject *)Py_TYPE(exc);
    if (tp) {
        type_name = PyObject_GetAttrString(tp, "__name__");
    }

    uint8_t *p = wal_reserve(WAL_MAX_ENTRY_SIZE);
    if (p) {
        /* Use origin_line as the line (where exception was raised) */
        wal_write_header(&p, WAL_EXCEPT, 0,
                         origin_line > 0 ? origin_line : handler_line,
                         code_idx >= 0 ? (uint16_t)code_idx : 0);
        if (type_name) {
            wal_write_value(&p, type_name, origin_line);
            Py_DECREF(type_name);
        } else {
            wal_write_u8(&p, 6);
        }
        wal_finish(p);
    } else {
        Py_XDECREF(type_name);
    }
}

/* ========================================================================
 * Python module API
 * ======================================================================== */

int
_PyWAL_Start(int buf_size, const char *output_file)
{
    if (buf_size <= 0) buf_size = WAL_BUF_DEFAULT;

    /* Ensure the oid map is allocated. _PyWAL_Clear inits it; if a caller
     * skips clear and goes straight to start, do it lazily here too. */
    if (!g_oid_map) {
        oid_map_init();
    }


    if (g_wal_buf) PyMem_Free(g_wal_buf);
    g_wal_buf = (uint8_t *)PyMem_Calloc(1, buf_size);
    if (!g_wal_buf) return -1;
    g_wal_buf_capacity = buf_size;
    g_wal_buf_pos = 0;
    g_wal_seq = 0;
    g_wal_total = 0;
    g_wal_bytes_flushed = 0;
    g_wal_flush_count = 0;
    g_stat_events = 0;
    g_stat_store_fast = 0;
    g_stat_store_subscr = 0;
    g_stat_store_attr = 0;
    g_stat_calls = 0;
    g_stat_returns = 0;
    g_stat_mutations = 0;
    g_n_frames = 0;
    g_last_line_frame = NULL;
    g_last_line_num = -1;

    /* Open output file if specified */
    if (g_wal_fd >= 0) { close(g_wal_fd); g_wal_fd = -1; }
    if (output_file) {
        g_wal_fd = open(output_file, O_WRONLY | O_CREAT | O_TRUNC, 0644);
        if (g_wal_fd < 0) return -1;
    }

    _PyWAL_enabled = 1;
    return 0;
}

void
_PyWAL_Stop(void)
{
    _PyWAL_enabled = 0;

    if (g_wal_fd >= 0) {
        wal_flush_to_disk();
        close(g_wal_fd);
        g_wal_fd = -1;
    }

}

PyObject *
_PyWAL_GetStats(void)
{
    return Py_BuildValue("{s:K,s:K,s:K,s:K,s:K,s:K,s:K,s:K,s:i,s:K,s:K,s:K,s:K}",
        "events", (unsigned long long)g_stat_events,
        "store_fast", (unsigned long long)g_stat_store_fast,
        "store_subscr", (unsigned long long)g_stat_store_subscr,
        "store_attr", (unsigned long long)g_stat_store_attr,
        "calls", (unsigned long long)g_stat_calls,
        "returns", (unsigned long long)g_stat_returns,
        "mutations", (unsigned long long)g_stat_mutations,
        "wal_total", (unsigned long long)g_wal_total,
        "registered_codes", g_n_codes,
        "buf_used", (unsigned long long)g_wal_buf_pos,
        "buf_capacity", (unsigned long long)g_wal_buf_capacity,
        "bytes_flushed", (unsigned long long)g_wal_bytes_flushed,
        "flush_count", (unsigned long long)g_wal_flush_count
    );
}

void
_PyWAL_Clear(void)
{
    _PyWAL_enabled = 0;
    for (int i = 0; i < g_n_codes; i++) {
        PyMem_Free(g_code_cache[i].offset_to_line);
        g_code_cache[i].offset_to_line = NULL;
        g_code_cache[i].classified = 0;
        g_code_cache[i].is_traceable = 0;
    }
    g_n_codes = 0;
    g_n_frames = 0;
    if (g_wal_buf) { PyMem_Free(g_wal_buf); g_wal_buf = NULL; }
    g_wal_buf_capacity = 0;
    g_wal_buf_pos = 0;
    init_code_hash();
    oid_map_init();

    /* Free interned strings */
    for (int i = 0; i < g_n_strings; i++) {
        Py_XDECREF(g_string_table[i]);
        g_string_table[i] = NULL;
    }
    string_intern_init();
}

/* Register a classifier callback. Pass None (or NULL) to clear. The
 * callback receives a code object and returns truthy for "user code"
 * (full event emission) or falsy for "skip BIND/UNBIND/LINE". Existing
 * CodeAnalysis classifications are reset so the new callback runs lazily
 * on the next event for each code object. */
void
_PyWAL_SetClassifier(PyObject *fn)
{
    PyObject *old = g_classifier;
    if (fn == NULL || fn == Py_None) {
        g_classifier = NULL;
    } else {
        Py_INCREF(fn);
        g_classifier = fn;
    }
    Py_XDECREF(old);
    /* Reset cached classifications so the new policy applies. */
    for (int i = 0; i < g_n_codes; i++) {
        g_code_cache[i].classified = 0;
        g_code_cache[i].is_traceable = 0;
    }
}

/* Return the interned string table as a Python list. The loader uses this
 * to resolve method_idx (in MUTATE events) and attr_idx (in SETATTR/DELATTR)
 * to actual names — without it, the loader can't apply container mutations. */
PyObject *
_PyWAL_GetStringTable(void)
{
    PyObject *result = PyList_New(g_n_strings);
    if (!result) return NULL;
    for (int i = 0; i < g_n_strings; i++) {
        PyObject *s = g_string_table[i];
        PyList_SET_ITEM(result, i, s ? Py_NewRef(s) : Py_NewRef(Py_None));
    }
    return result;
}

/* Return {oid: type_name} for every entry whose type_name was recorded.
 * Today we only record for type_tag = 10 (user objects / functions /
 * methods / etc) — for built-in containers the type_tag itself is
 * sufficient. The loader uses this to render `__obj__` snapshots with
 * the actual class name instead of a generic "object". */
PyObject *
_PyWAL_GetOidTypeNames(void)
{
    PyObject *result = PyDict_New();
    if (!result) return NULL;
    for (size_t i = 0; i < g_oid_map_capacity; i++) {
        OidMapEntry *e = &g_oid_map[i];
        if (!e->occupied) continue;
        if (e->type_name_idx == UINT16_MAX) continue;
        if (e->type_name_idx >= g_n_strings) continue;
        PyObject *name = g_string_table[e->type_name_idx];
        if (!name) continue;
        PyObject *oid_key = PyLong_FromUnsignedLong(e->oid);
        if (!oid_key) { Py_DECREF(result); return NULL; }
        if (PyDict_SetItem(result, oid_key, name) < 0) {
            Py_DECREF(oid_key);
            Py_DECREF(result);
            return NULL;
        }
        Py_DECREF(oid_key);
    }
    return result;
}

/* ---- WAL decoder (identical format to ctrace_wal_compact.c) ---- */

static const char *wal_event_names[] = {
    "?", "CREATE", "BIND", "UNBIND", "MUTATE", "SETATTR",
    "SETITEM", "DELITEM", "DELATTR", "DEALLOC",
    "LINE", "CALL", "RETURN", "EXCEPTION",
    "RAISE", "EXCEPT", "SNAPSHOT", "OBJ_SNAPSHOT"
};
#define WAL_EVENT_NAME_COUNT 18

static inline uint8_t  wal_read_u8 (const uint8_t **p) { uint8_t  v = **p; (*p)++; return v; }
static inline uint16_t wal_read_u16(const uint8_t **p) { uint16_t v; memcpy(&v, *p, 2); *p += 2; return v; }
static inline uint32_t wal_read_u32(const uint8_t **p) { uint32_t v; memcpy(&v, *p, 4); *p += 4; return v; }
static inline int32_t  wal_read_i32(const uint8_t **p) { int32_t  v; memcpy(&v, *p, 4); *p += 4; return v; }
static inline int64_t  wal_read_i64(const uint8_t **p) { int64_t  v; memcpy(&v, *p, 8); *p += 8; return v; }
static inline double   wal_read_f64(const uint8_t **p) { double   v; memcpy(&v, *p, 8); *p += 8; return v; }

static PyObject *wal_read_value_py(const uint8_t **p) {
    uint8_t tag = wal_read_u8(p);
    switch (tag) {
    case 0: Py_RETURN_NONE;
    case 1: return PyLong_FromLongLong(wal_read_i64(p));
    case 2: return PyFloat_FromDouble(wal_read_f64(p));
    case 3: return PyBool_FromLong(wal_read_u8(p));
    case 4: {
        uint8_t len = wal_read_u8(p);
        PyObject *s = PyUnicode_FromStringAndSize((const char *)*p, len);
        *p += len;
        return s;
    }
    case 5: {
        uint32_t ref = wal_read_u32(p);
        return Py_BuildValue("{s:I}", "ref", (unsigned int)ref);
    }
    default: return PyUnicode_FromString("<unknown>");
    }
}

/* Resolve a variable name from code_idx + local_idx */
static const char *resolve_varname(uint16_t code_idx, uint16_t local_idx) {
    if (code_idx >= (uint16_t)g_n_codes) return "?";
    CodeAnalysis *ca = &g_code_cache[code_idx];
    PyCodeObject *code = (PyCodeObject *)ca->code_ref;
    if (!code) return "?";
    PyObject *names = code->co_localsplusnames;
    if (!names || local_idx >= PyTuple_GET_SIZE(names)) return "?";
    PyObject *nm = PyTuple_GET_ITEM(names, local_idx);
    const char *s = PyUnicode_AsUTF8(nm);
    return s ? s : "?";
}

/* Resolve an interned string from string table */
static const char *resolve_string(uint16_t str_idx) {
    if (str_idx >= g_n_strings) return "?";
    PyObject *s = g_string_table[str_idx];
    if (!s) return "?";
    const char *r = PyUnicode_AsUTF8(s);
    return r ? r : "?";
}

PyObject *
_PyWAL_GetWAL(int max_count)
{
    if (!g_wal_buf || g_wal_buf_pos == 0)
        return PyList_New(0);

    PyObject *result = PyList_New(0);
    if (!result) return NULL;

    const uint8_t *p = g_wal_buf;
    const uint8_t *end = g_wal_buf + g_wal_buf_pos;
    int n = 0;

    while (p < end && n < max_count) {
        if (p + 15 > end) break;

        uint8_t event   = wal_read_u8(&p);
        uint32_t seq    = wal_read_u32(&p);
        uint32_t oid    = wal_read_u32(&p);
        int32_t line    = wal_read_i32(&p);
        uint16_t cidx   = wal_read_u16(&p);

        const char *evt_name = (event > 0 && event < WAL_EVENT_NAME_COUNT) ? wal_event_names[event] : "?";
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
            const char *name_str = resolve_varname(cidx, name_idx);
            if (oid == OID_NONE) {
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
            const char *name_str = resolve_varname(cidx, name_idx);
            entry = Py_BuildValue("{s:I,s:s,s:I,s:i,s:s}",
                "seq", seq, "event", evt_name, "oid", oid,
                "line", line, "name", name_str);
            break;
        }
        case WAL_MUTATE: {
            uint16_t attr_idx = wal_read_u16(&p);
            uint8_t n_args = wal_read_u8(&p);
            const char *attr_str = resolve_string(attr_idx);
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
            const char *attr_str = resolve_string(attr_idx);
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
            const char *attr_str = resolve_string(attr_idx);
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
        case WAL_RAISE: {
            PyObject *exc_type = wal_read_value_py(&p);
            PyObject *exc_msg = wal_read_value_py(&p);
            entry = Py_BuildValue("{s:I,s:s,s:i,s:i,s:N,s:N}",
                "seq", seq, "event", evt_name,
                "line", line, "code_idx", (int)cidx,
                "exc_type", exc_type, "exc_msg", exc_msg);
            break;
        }
        case WAL_EXCEPT: {
            PyObject *exc_type = wal_read_value_py(&p);
            entry = Py_BuildValue("{s:I,s:s,s:i,s:i,s:N}",
                "seq", seq, "event", evt_name,
                "line", line, "code_idx", (int)cidx,
                "exc_type", exc_type);
            break;
        }
        case WAL_SNAPSHOT: {
            uint16_t n_items = wal_read_u16(&p);
            PyObject *items = PyList_New(n_items);
            for (int i = 0; i < n_items; i++) {
                PyList_SET_ITEM(items, i, wal_read_value_py(&p));
            }
            entry = Py_BuildValue("{s:I,s:s,s:I,s:i,s:N}",
                "seq", seq, "event", evt_name, "oid", oid,
                "line", line, "items", items);
            break;
        }
        case WAL_OBJ_SNAPSHOT: {
            uint16_t type_name_idx = wal_read_u16(&p);
            uint16_t n_attrs = wal_read_u16(&p);
            const char *type_name = resolve_string(type_name_idx);
            PyObject *attrs = PyDict_New();
            for (int i = 0; i < n_attrs; i++) {
                uint16_t attr_idx = wal_read_u16(&p);
                const char *name_str = resolve_string(attr_idx);
                PyObject *val = wal_read_value_py(&p);
                PyDict_SetItemString(attrs, name_str, val);
                Py_DECREF(val);
            }
            entry = Py_BuildValue("{s:I,s:s,s:I,s:i,s:s,s:N}",
                "seq", seq, "event", evt_name, "oid", oid,
                "line", line, "type_name", type_name, "attrs", attrs);
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

PyObject *
_PyWAL_RegisterCode(PyObject *code_obj)
{
    /* Compatibility no-op — the fork auto-registers code objects.
     * Accept and ignore the call so the same Python harness works. */
    if (!PyCode_Check(code_obj)) {
        PyErr_SetString(PyExc_TypeError, "Expected code object");
        return NULL;
    }
    PyCodeObject *code = (PyCodeObject *)code_obj;
    int idx = code_hash_lookup(code_obj);
    if (idx < 0) {
        idx = code_auto_register(code);
    }
    if (idx < 0) {
        PyErr_SetString(PyExc_RuntimeError, "Code cache full");
        return NULL;
    }
    return PyLong_FromLong(idx);
}
