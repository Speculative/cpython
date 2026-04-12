/*
 * Experiment 7: C-Level Trace Function via PyEval_SetTrace
 *
 * Registers a pure C trace function directly using PyEval_SetTrace().
 * This bypasses the Python callback overhead entirely — CPython calls our
 * C function directly with (PyFrameObject*, event, arg).
 *
 * We test several configurations:
 *   1. Noop C trace (measure minimum overhead of settrace path)
 *   2. C trace with line counting (minimal work)
 *   3. C trace reading localsplus directly (variable capture in C)
 *   4. C trace with change detection + ring buffer (full capture)
 */

#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <frameobject.h>
#include <time.h>
#include <string.h>

/* ========================================================================
 * Ring buffer for trace events
 * ======================================================================== */

typedef struct {
    uint64_t timestamp;
    uint64_t code_id;
    int32_t  line_number;
    uint8_t  event_type;
    uint8_t  n_changes;
    uint8_t  _pad[2];
} TraceEventHeader;

/* Variable change record */
typedef struct {
    uint16_t var_index;
    uint64_t value_id;        /* pointer identity of the value */
    uint8_t  value_type_tag;  /* 0=none, 1=int, 2=float, 3=bool, 4=str, 5=other */
    uint8_t  _pad;
    int64_t  inline_value;    /* for int/float/bool: the actual value */
} VarChange;

#define MAX_CHANGES_PER_EVENT 32
#define DEFAULT_BUFFER_EVENTS (1024 * 1024)

typedef struct {
    TraceEventHeader header;
    VarChange changes[MAX_CHANGES_PER_EVENT];
} TraceEvent;

static TraceEvent *g_event_buffer = NULL;
static size_t g_buffer_capacity = 0;
static size_t g_write_pos = 0;
static uint64_t g_total_events = 0;
static int g_active = 0;

/* Previous local values for change detection, per-frame.
 * Simplified: we only track the most recent frame. */
#define MAX_LOCALS 256
static PyObject *g_prev_locals[MAX_LOCALS];
static PyCodeObject *g_prev_code = NULL;

static inline uint64_t fast_timestamp(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}

/* ========================================================================
 * Trace functions (Py_tracefunc signature)
 * ======================================================================== */

/*
 * Mode 1: Pure noop — measures minimum overhead of the settrace path
 */
static int
trace_noop(PyObject *self, PyFrameObject *frame, int what, PyObject *arg)
{
    return 0;
}

/*
 * Mode 2: Count events — minimal work per event
 */
static uint64_t g_event_count = 0;

static int
trace_count(PyObject *self, PyFrameObject *frame, int what, PyObject *arg)
{
    g_event_count++;
    return 0;
}

/*
 * Mode 3: Count + read line number from frame
 */
static int
trace_count_line(PyObject *self, PyFrameObject *frame, int what, PyObject *arg)
{
    if (what == PyTrace_LINE) {
        g_event_count++;
        /* Access line number — this is what a minimal tracer needs */
        volatile int line = PyFrame_GetLineNumber(frame);
        (void)line;
    }
    else if (what == PyTrace_CALL || what == PyTrace_RETURN) {
        g_event_count++;
    }
    return 0;
}

/*
 * Mode 4: Read locals via PyFrame_GetVar (public API, creates PyObject)
 */
static int
trace_read_locals_api(PyObject *self, PyFrameObject *frame, int what, PyObject *arg)
{
    if (what != PyTrace_LINE) return 0;

    g_event_count++;
    PyCodeObject *code = PyFrame_GetCode(frame);

    /* Read each local variable via the public API */
    PyObject *varnames = PyCode_GetVarnames(code);
    if (varnames) {
        Py_ssize_t n = PyTuple_GET_SIZE(varnames);
        if (n > MAX_LOCALS) n = MAX_LOCALS;

        for (Py_ssize_t i = 0; i < n; i++) {
            PyObject *name = PyTuple_GET_ITEM(varnames, i);
            PyObject *value = PyFrame_GetVar(frame, name);
            if (value != NULL) {
                Py_DECREF(value);  /* GetVar returns new ref */
            }
            else {
                PyErr_Clear();  /* Variable might not be set yet */
            }
        }
        Py_DECREF(varnames);
    }

    Py_DECREF(code);
    return 0;
}

/*
 * Mode 5: Read locals via f_locals dict (what Python settrace does)
 */
static int
trace_read_flocals(PyObject *self, PyFrameObject *frame, int what, PyObject *arg)
{
    if (what != PyTrace_LINE) return 0;

    g_event_count++;
    PyObject *locals = PyFrame_GetLocals(frame);
    if (locals) {
        Py_DECREF(locals);
    }
    else {
        PyErr_Clear();
    }
    return 0;
}

/*
 * Mode 6: Full capture — read locals + change detection + ring buffer
 */
static int
trace_full_capture(PyObject *self, PyFrameObject *frame, int what, PyObject *arg)
{
    if (!g_active || !g_event_buffer) return 0;
    if (what != PyTrace_LINE && what != PyTrace_CALL && what != PyTrace_RETURN) return 0;

    PyCodeObject *code = PyFrame_GetCode(frame);
    size_t pos = g_write_pos;
    TraceEvent *evt = &g_event_buffer[pos];

    evt->header.timestamp = fast_timestamp();
    evt->header.code_id = (uint64_t)(uintptr_t)code;
    evt->header.line_number = PyFrame_GetLineNumber(frame);
    evt->header.event_type = (uint8_t)what;
    evt->header.n_changes = 0;

    if (what == PyTrace_LINE) {
        /* Read locals and detect changes */
        PyObject *varnames = PyCode_GetVarnames(code);
        if (!varnames) { Py_DECREF(code); return 0; }
        Py_ssize_t n = PyTuple_GET_SIZE(varnames);
        if (n > MAX_LOCALS) n = MAX_LOCALS;

        int n_changes = 0;
        for (Py_ssize_t i = 0; i < n && n_changes < MAX_CHANGES_PER_EVENT; i++) {
            PyObject *name = PyTuple_GET_ITEM(varnames, i);
            PyObject *value = PyFrame_GetVar(frame, name);
            if (value == NULL) {
                PyErr_Clear();
                value = NULL;
            }

            /* Change detection: pointer comparison */
            if (value != g_prev_locals[i] || code != g_prev_code) {
                VarChange *chg = &evt->changes[n_changes];
                chg->var_index = (uint16_t)i;
                chg->value_id = (uint64_t)(uintptr_t)value;

                /* Inline serialization for primitives */
                if (value == NULL || value == Py_None) {
                    chg->value_type_tag = 0;
                    chg->inline_value = 0;
                }
                else if (PyBool_Check(value)) {
                    chg->value_type_tag = 3;
                    chg->inline_value = (value == Py_True) ? 1 : 0;
                }
                else if (PyLong_Check(value)) {
                    int overflow;
                    long long v = PyLong_AsLongLongAndOverflow(value, &overflow);
                    if (!overflow && !PyErr_Occurred()) {
                        chg->value_type_tag = 1;
                        chg->inline_value = (int64_t)v;
                    }
                    else {
                        PyErr_Clear();
                        chg->value_type_tag = 5;
                        chg->inline_value = 0;
                    }
                }
                else if (PyFloat_Check(value)) {
                    chg->value_type_tag = 2;
                    double d = PyFloat_AS_DOUBLE(value);
                    memcpy(&chg->inline_value, &d, sizeof(double));
                }
                else {
                    chg->value_type_tag = 5;  /* other */
                    chg->inline_value = 0;
                }

                n_changes++;
                g_prev_locals[i] = value;
            }

            if (value) Py_DECREF(value);
        }
        evt->header.n_changes = (uint8_t)n_changes;
        g_prev_code = code;
        Py_DECREF(varnames);
    }

    g_write_pos = (pos + 1) % g_buffer_capacity;
    g_total_events++;
    Py_DECREF(code);
    return 0;
}

/* ========================================================================
 * Python API
 * ======================================================================== */

static int g_current_mode = 0;

static Py_tracefunc trace_funcs[] = {
    trace_noop,            /* 0 */
    trace_count,           /* 1 */
    trace_count_line,      /* 2 */
    trace_read_locals_api, /* 3 */
    trace_read_flocals,    /* 4 */
    trace_full_capture,    /* 5 */
};
static const char *mode_names[] = {
    "noop", "count", "count+line", "locals_api", "f_locals", "full_capture"
};
#define NUM_MODES 6

static PyObject *
ctrace_start(PyObject *self, PyObject *args)
{
    int mode = 0;
    int buffer_size = DEFAULT_BUFFER_EVENTS;
    if (!PyArg_ParseTuple(args, "|ii", &mode, &buffer_size))
        return NULL;

    if (mode < 0 || mode >= NUM_MODES) {
        PyErr_SetString(PyExc_ValueError, "Invalid mode");
        return NULL;
    }

    /* Allocate buffer for full capture mode */
    if (mode == 5) {
        if (g_event_buffer) PyMem_Free(g_event_buffer);
        g_event_buffer = (TraceEvent *)PyMem_Calloc(buffer_size, sizeof(TraceEvent));
        if (!g_event_buffer) return PyErr_NoMemory();
        g_buffer_capacity = buffer_size;
        g_write_pos = 0;
        g_total_events = 0;
        memset(g_prev_locals, 0, sizeof(g_prev_locals));
        g_prev_code = NULL;
    }

    g_event_count = 0;
    g_active = 1;
    g_current_mode = mode;

    PyEval_SetTrace(trace_funcs[mode], Py_None);
    Py_RETURN_NONE;
}

static PyObject *
ctrace_stop(PyObject *self, PyObject *Py_UNUSED(ignored))
{
    PyEval_SetTrace(NULL, NULL);
    g_active = 0;
    Py_RETURN_NONE;
}

static PyObject *
ctrace_stats(PyObject *self, PyObject *Py_UNUSED(ignored))
{
    return Py_BuildValue("{s:K,s:K,s:s,s:i}",
        "event_count", (unsigned long long)g_event_count,
        "total_buffer_events", (unsigned long long)g_total_events,
        "mode", mode_names[g_current_mode],
        "active", g_active
    );
}

static PyObject *
ctrace_get_events(PyObject *self, PyObject *args)
{
    int count = 100;
    if (!PyArg_ParseTuple(args, "|i", &count))
        return NULL;

    if (!g_event_buffer) {
        return PyList_New(0);
    }

    size_t available = g_total_events < g_buffer_capacity ? g_total_events : g_buffer_capacity;
    if ((size_t)count > available) count = (int)available;

    PyObject *result = PyList_New(count);
    if (!result) return NULL;

    const char *evt_names[] = {
        [PyTrace_CALL] = "call",
        [PyTrace_EXCEPTION] = "exception",
        [PyTrace_LINE] = "line",
        [PyTrace_RETURN] = "return",
        [PyTrace_C_CALL] = "c_call",
        [PyTrace_C_EXCEPTION] = "c_exception",
        [PyTrace_C_RETURN] = "c_return",
        [PyTrace_OPCODE] = "opcode",
    };
    const char *type_names[] = {"none", "int", "float", "bool", "str", "other"};

    for (int i = 0; i < count; i++) {
        size_t idx;
        if (g_total_events <= g_buffer_capacity) {
            idx = i;
        } else {
            idx = (g_write_pos + g_buffer_capacity - count + i) % g_buffer_capacity;
        }
        TraceEvent *ev = &g_event_buffer[idx];

        /* Build changes list */
        PyObject *changes = PyList_New(ev->header.n_changes);
        for (int j = 0; j < ev->header.n_changes; j++) {
            VarChange *chg = &ev->changes[j];
            PyObject *val;
            if (chg->value_type_tag == 1) {
                val = PyLong_FromLongLong(chg->inline_value);
            } else if (chg->value_type_tag == 2) {
                double d;
                memcpy(&d, &chg->inline_value, sizeof(double));
                val = PyFloat_FromDouble(d);
            } else if (chg->value_type_tag == 3) {
                val = chg->inline_value ? Py_True : Py_False;
            } else {
                val = Py_None;
            }
            PyObject *entry = Py_BuildValue("(iOs)",
                (int)chg->var_index, val, type_names[chg->value_type_tag]);
            if (chg->value_type_tag == 1 || chg->value_type_tag == 2) {
                Py_DECREF(val);
            }
            PyList_SET_ITEM(changes, j, entry);
        }

        const char *ename = (ev->header.event_type < 8) ?
            evt_names[ev->header.event_type] : "?";

        PyObject *tuple = Py_BuildValue("(sKiN)",
            ename,
            (unsigned long long)ev->header.code_id,
            (int)ev->header.line_number,
            changes
        );
        PyList_SET_ITEM(result, i, tuple);
    }
    return result;
}

static PyObject *
ctrace_list_modes(PyObject *self, PyObject *Py_UNUSED(ignored))
{
    PyObject *result = PyList_New(NUM_MODES);
    for (int i = 0; i < NUM_MODES; i++) {
        PyList_SET_ITEM(result, i, Py_BuildValue("(is)", i, mode_names[i]));
    }
    return result;
}

static PyMethodDef ctrace_methods[] = {
    {"start", ctrace_start, METH_VARARGS,
     "Start tracing. Args: mode (0-5), buffer_size (for mode 5)."},
    {"stop", ctrace_stop, METH_NOARGS, "Stop tracing."},
    {"stats", ctrace_stats, METH_NOARGS, "Get stats."},
    {"get_events", ctrace_get_events, METH_VARARGS,
     "Get last N events (mode 5 only)."},
    {"list_modes", ctrace_list_modes, METH_NOARGS, "List available modes."},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef ctrace_module = {
    PyModuleDef_HEAD_INIT,
    "_ctrace",
    "C-level trace function via PyEval_SetTrace",
    -1,
    ctrace_methods
};

PyMODINIT_FUNC
PyInit__ctrace(void)
{
    return PyModule_Create(&ctrace_module);
}
