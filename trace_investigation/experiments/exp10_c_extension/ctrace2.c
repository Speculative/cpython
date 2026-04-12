/*
 * Experiment 10: C Trace with Pre-computed Variable Maps
 *
 * The key optimization: Python pre-analyzes bytecode once per code object
 * and passes a packed "line -> written variable indices" map to C.
 * The C trace function uses this map to only read variables that changed.
 *
 * Architecture:
 *   Python (once per code object):
 *     analyze_code(code) -> {line: [var_idx, ...]}
 *     ctrace2.register_code(code_id, line_map)
 *
 *   C (every trace event):
 *     lookup line_map[code_id][line_number] -> bitmask of var indices
 *     for each set bit: compare localsplus[i] pointer to cached value
 *     if changed: read value via PyFrame_GetVar, record it
 */

#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <frameobject.h>
#include <time.h>
#include <string.h>

/* ========================================================================
 * Code object analysis cache (passed from Python)
 * ======================================================================== */

/* Per-line write info: bitmask of which locals are written */
/* We support up to 64 locals per function (covers vast majority of code) */
typedef uint64_t var_bitmask_t;

/* Per-code-object cached analysis */
typedef struct {
    PyObject *code_ref;      /* weak-ish ref to identify the code object */
    int n_locals;
    PyObject *varnames;      /* borrowed ref to tuple of var names (held via code obj) */

    /* Line -> bitmask mapping.
     * Indexed by (line_number - first_line).
     * If line_number is out of range, treat as "no writes". */
    int first_line;
    int n_lines;             /* size of line_map array */
    var_bitmask_t *line_map; /* array: line_map[line - first_line] = bitmask */
} CodeAnalysis;

#define MAX_CODE_ENTRIES 4096
static CodeAnalysis g_code_cache[MAX_CODE_ENTRIES];
static int g_n_codes = 0;

/* Hash map: code object pointer -> index in g_code_cache */
#define CODE_HASH_SIZE 8191
static int g_code_hash[CODE_HASH_SIZE]; /* -1 = empty, otherwise index */

static void init_code_hash(void) {
    memset(g_code_hash, -1, sizeof(g_code_hash));
}

static int code_hash_lookup(PyObject *code) {
    uintptr_t h = ((uintptr_t)code >> 4) % CODE_HASH_SIZE;
    for (int probe = 0; probe < 32; probe++) {
        int idx = g_code_hash[(h + probe) % CODE_HASH_SIZE];
        if (idx == -1) return -1;
        if (g_code_cache[idx].code_ref == code) return idx;
    }
    return -1;
}

static int code_hash_insert(PyObject *code, int cache_idx) {
    uintptr_t h = ((uintptr_t)code >> 4) % CODE_HASH_SIZE;
    for (int probe = 0; probe < 32; probe++) {
        int slot = (h + probe) % CODE_HASH_SIZE;
        if (g_code_hash[slot] == -1) {
            g_code_hash[slot] = cache_idx;
            return 0;
        }
    }
    return -1; /* hash table full */
}

/* ========================================================================
 * Per-frame variable cache (for change detection)
 * ======================================================================== */

#define MAX_LOCALS 64
#define MAX_CACHED_FRAMES 256

typedef struct {
    PyFrameObject *frame;    /* identity of the frame */
    int code_idx;            /* index into g_code_cache */
    var_bitmask_t pending_mask; /* write mask from previous line, to read on next LINE */
    PyObject *prev_values[MAX_LOCALS]; /* previous value pointers (borrowed) */
} FrameCache;

static FrameCache g_frame_cache[MAX_CACHED_FRAMES];
static int g_n_frames = 0;

/* Simple frame lookup — for typical call depths this is fine */
static FrameCache* find_frame_cache(PyFrameObject *frame) {
    for (int i = g_n_frames - 1; i >= 0; i--) {
        if (g_frame_cache[i].frame == frame)
            return &g_frame_cache[i];
    }
    return NULL;
}

static FrameCache* push_frame_cache(PyFrameObject *frame, int code_idx) {
    if (g_n_frames >= MAX_CACHED_FRAMES) {
        /* Evict oldest */
        g_n_frames = MAX_CACHED_FRAMES / 2;
    }
    FrameCache *fc = &g_frame_cache[g_n_frames++];
    fc->frame = frame;
    fc->code_idx = code_idx;
    fc->pending_mask = 0;
    memset(fc->prev_values, 0, sizeof(fc->prev_values));
    return fc;
}

static void pop_frame_cache(PyFrameObject *frame) {
    /* Pop from top if it matches (common case) */
    if (g_n_frames > 0 && g_frame_cache[g_n_frames - 1].frame == frame) {
        g_n_frames--;
    }
}

/* ========================================================================
 * Statistics
 * ======================================================================== */

static uint64_t g_stat_events = 0;
static uint64_t g_stat_line_events = 0;
static uint64_t g_stat_lines_with_writes = 0;
static uint64_t g_stat_vars_checked = 0;
static uint64_t g_stat_vars_changed = 0;
static uint64_t g_stat_getvar_calls = 0;

/* ========================================================================
 * Trace function modes
 * ======================================================================== */

/*
 * Mode 0: noop (baseline)
 */
static int trace_noop(PyObject *self, PyFrameObject *frame, int what, PyObject *arg)
{
    return 0;
}

/*
 * Mode 1: Selective capture using pre-computed maps
 *
 * On LINE: look up which vars this line writes, pointer-compare cached values,
 *          only GetVar for actually changed vars.
 * On CALL: set up frame cache.
 * On RETURN: tear down frame cache.
 */
static int trace_selective(PyObject *self, PyFrameObject *frame, int what, PyObject *arg)
{
    g_stat_events++;

    if (what == PyTrace_CALL) {
        PyCodeObject *code = PyFrame_GetCode(frame);
        int idx = code_hash_lookup((PyObject *)code);
        if (idx >= 0) {
            FrameCache *fc = push_frame_cache(frame, idx);
            /* Capture function arguments on CALL.
             * Parameters are the first co_argcount + co_kwonlyargcount locals,
             * and they're already set in the frame when CALL fires. */
            CodeAnalysis *ca = &g_code_cache[idx];
            int n_args = code->co_argcount + code->co_kwonlyargcount;
            if (code->co_flags & CO_VARARGS) n_args++;
            if (code->co_flags & CO_VARKEYWORDS) n_args++;
            if (n_args > ca->n_locals) n_args = ca->n_locals;
            for (int i = 0; i < n_args && i < MAX_LOCALS; i++) {
                g_stat_vars_checked++;
                PyObject *name = PyTuple_GET_ITEM(ca->varnames, i);
                PyObject *value = PyFrame_GetVar(frame, name);
                if (value == NULL) { PyErr_Clear(); continue; }
                if (value != fc->prev_values[i]) {
                    g_stat_vars_changed++;
                    g_stat_getvar_calls++;
                    fc->prev_values[i] = value;
                }
                Py_DECREF(value);
            }
        }
        Py_DECREF(code);
        return 0;
    }

    if (what == PyTrace_RETURN) {
        /* Flush pending writes from the last line before the return */
        FrameCache *rfc = find_frame_cache(frame);
        if (rfc && rfc->pending_mask != 0) {
            CodeAnalysis *rca = &g_code_cache[rfc->code_idx];
            g_stat_lines_with_writes++;
            for (int i = 0; i < rca->n_locals && i < MAX_LOCALS; i++) {
                if (!(rfc->pending_mask & (1ULL << i))) continue;
                g_stat_vars_checked++;
                PyObject *name = PyTuple_GET_ITEM(rca->varnames, i);
                PyObject *value = PyFrame_GetVar(frame, name);
                if (value == NULL) { PyErr_Clear(); continue; }
                if (value != rfc->prev_values[i]) {
                    g_stat_vars_changed++;
                    g_stat_getvar_calls++;
                    rfc->prev_values[i] = value;
                }
                Py_DECREF(value);
            }
        }
        pop_frame_cache(frame);
        return 0;
    }

    if (what != PyTrace_LINE) return 0;

    g_stat_line_events++;

    FrameCache *fc = find_frame_cache(frame);
    if (!fc) {
        /* Code wasn't registered — skip */
        return 0;
    }

    CodeAnalysis *ca = &g_code_cache[fc->code_idx];

    /*
     * KEY INSIGHT: LINE fires BEFORE the line executes.
     * So we need to read variables written by the PREVIOUS line,
     * not the current one. On this LINE event, the previous line's
     * writes are now visible in the frame.
     *
     * Strategy:
     *   1. Read variables from the PREVIOUS line's write mask
     *      (these have now been written)
     *   2. Save the CURRENT line's write mask for the next event
     */

    /* Step 1: Read variables written by the PREVIOUS line.
     * Those writes have now completed, so values are visible. */
    if (fc->pending_mask != 0) {
        g_stat_lines_with_writes++;
        for (int i = 0; i < ca->n_locals && i < MAX_LOCALS; i++) {
            if (!(fc->pending_mask & (1ULL << i))) continue;

            g_stat_vars_checked++;

            PyObject *name = PyTuple_GET_ITEM(ca->varnames, i);
            PyObject *value = PyFrame_GetVar(frame, name);
            if (value == NULL) {
                PyErr_Clear();
                continue;
            }

            /* Pointer comparison for change detection */
            if (value != fc->prev_values[i]) {
                g_stat_vars_changed++;
                g_stat_getvar_calls++;
                fc->prev_values[i] = value;
            }

            Py_DECREF(value);
        }
    }

    /* Step 2: Save the CURRENT line's write mask.
     * We'll read these variables on the NEXT LINE event. */
    int line = PyFrame_GetLineNumber(frame);
    int line_idx = line - ca->first_line;
    fc->pending_mask = 0;
    if (line_idx >= 0 && line_idx < ca->n_lines) {
        fc->pending_mask = ca->line_map[line_idx];
    }

    return 0;
}

/*
 * Mode 2: GetLocals + diff (the previous best approach, for comparison)
 */
static PyObject *g_prev_locals_dict = NULL;

static int trace_getlocals_diff(PyObject *self, PyFrameObject *frame, int what, PyObject *arg)
{
    g_stat_events++;
    if (what != PyTrace_LINE) return 0;
    g_stat_line_events++;

    PyObject *locals = PyFrame_GetLocals(frame);
    if (!locals) { PyErr_Clear(); return 0; }

    /* In a real implementation we'd diff against cached. Just access it. */
    Py_DECREF(locals);
    return 0;
}

/* ========================================================================
 * Python API
 * ======================================================================== */

static int g_mode = 0;
static Py_tracefunc g_trace_funcs[] = {
    trace_noop,
    trace_selective,
    trace_getlocals_diff,
};
static const char *g_mode_names[] = {"noop", "selective", "getlocals_diff"};
#define NUM_MODES 3

static PyObject *
ctrace2_register_code(PyObject *self, PyObject *args)
{
    /*
     * register_code(code_object, first_line, line_bitmasks)
     *
     * line_bitmasks: list of (line_number, bitmask) tuples
     * bitmask: int where bit i means local variable i is written on that line
     */
    PyObject *code_obj;
    int first_line;
    PyObject *line_bitmasks;

    if (!PyArg_ParseTuple(args, "OiO", &code_obj, &first_line, &line_bitmasks))
        return NULL;

    if (!PyCode_Check(code_obj)) {
        PyErr_SetString(PyExc_TypeError, "First argument must be a code object");
        return NULL;
    }

    if (g_n_codes >= MAX_CODE_ENTRIES) {
        PyErr_SetString(PyExc_RuntimeError, "Code cache full");
        return NULL;
    }

    PyCodeObject *code = (PyCodeObject *)code_obj;

    /* Find max line to size the array */
    int max_line = first_line;
    Py_ssize_t n_entries = PyList_Size(line_bitmasks);
    for (Py_ssize_t i = 0; i < n_entries; i++) {
        PyObject *entry = PyList_GET_ITEM(line_bitmasks, i);
        int line = (int)PyLong_AsLong(PyTuple_GET_ITEM(entry, 0));
        if (line > max_line) max_line = line;
    }

    int n_lines = max_line - first_line + 1;
    var_bitmask_t *line_map = (var_bitmask_t *)PyMem_Calloc(n_lines, sizeof(var_bitmask_t));
    if (!line_map) return PyErr_NoMemory();

    /* Fill in the bitmasks */
    for (Py_ssize_t i = 0; i < n_entries; i++) {
        PyObject *entry = PyList_GET_ITEM(line_bitmasks, i);
        int line = (int)PyLong_AsLong(PyTuple_GET_ITEM(entry, 0));
        uint64_t mask = PyLong_AsUnsignedLongLong(PyTuple_GET_ITEM(entry, 1));
        int idx = line - first_line;
        if (idx >= 0 && idx < n_lines) {
            line_map[idx] |= mask;
        }
    }

    /* Get varnames */
    PyObject *varnames = PyCode_GetVarnames(code);
    if (!varnames) {
        PyMem_Free(line_map);
        return NULL;
    }

    /* Store in cache */
    int cache_idx = g_n_codes++;
    CodeAnalysis *ca = &g_code_cache[cache_idx];
    ca->code_ref = code_obj;
    ca->n_locals = (int)PyTuple_GET_SIZE(varnames);
    ca->varnames = varnames;  /* we keep a strong reference */
    ca->first_line = first_line;
    ca->n_lines = n_lines;
    ca->line_map = line_map;

    code_hash_insert(code_obj, cache_idx);

    return PyLong_FromLong(cache_idx);
}

static PyObject *
ctrace2_start(PyObject *self, PyObject *args)
{
    int mode = 1;
    if (!PyArg_ParseTuple(args, "|i", &mode))
        return NULL;
    if (mode < 0 || mode >= NUM_MODES) {
        PyErr_SetString(PyExc_ValueError, "Invalid mode");
        return NULL;
    }
    g_mode = mode;
    g_stat_events = g_stat_line_events = 0;
    g_stat_lines_with_writes = g_stat_vars_checked = 0;
    g_stat_vars_changed = g_stat_getvar_calls = 0;
    g_n_frames = 0;

    PyEval_SetTrace(g_trace_funcs[mode], Py_None);
    Py_RETURN_NONE;
}

static PyObject *
ctrace2_stop(PyObject *self, PyObject *Py_UNUSED(ignored))
{
    PyEval_SetTrace(NULL, NULL);
    Py_RETURN_NONE;
}

static PyObject *
ctrace2_stats(PyObject *self, PyObject *Py_UNUSED(ignored))
{
    return Py_BuildValue("{s:K,s:K,s:K,s:K,s:K,s:K,s:i,s:s}",
        "events", (unsigned long long)g_stat_events,
        "line_events", (unsigned long long)g_stat_line_events,
        "lines_with_writes", (unsigned long long)g_stat_lines_with_writes,
        "vars_checked", (unsigned long long)g_stat_vars_checked,
        "vars_changed", (unsigned long long)g_stat_vars_changed,
        "getvar_calls", (unsigned long long)g_stat_getvar_calls,
        "registered_codes", g_n_codes,
        "mode", g_mode_names[g_mode]
    );
}

static PyObject *
ctrace2_clear(PyObject *self, PyObject *Py_UNUSED(ignored))
{
    for (int i = 0; i < g_n_codes; i++) {
        Py_XDECREF(g_code_cache[i].varnames);
        PyMem_Free(g_code_cache[i].line_map);
    }
    g_n_codes = 0;
    g_n_frames = 0;
    init_code_hash();
    Py_RETURN_NONE;
}

static PyMethodDef ctrace2_methods[] = {
    {"register_code", ctrace2_register_code, METH_VARARGS,
     "Register a code object's line->variable write map."},
    {"start", ctrace2_start, METH_VARARGS,
     "Start tracing. Mode: 0=noop, 1=selective, 2=getlocals_diff."},
    {"stop", ctrace2_stop, METH_NOARGS, "Stop tracing."},
    {"stats", ctrace2_stats, METH_NOARGS, "Get statistics."},
    {"clear", ctrace2_clear, METH_NOARGS, "Clear all registered codes."},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef ctrace2_module = {
    PyModuleDef_HEAD_INIT,
    "_ctrace2",
    "C trace with pre-computed variable maps",
    -1,
    ctrace2_methods
};

PyMODINIT_FUNC
PyInit__ctrace2(void)
{
    init_code_hash();
    return PyModule_Create(&ctrace2_module);
}
