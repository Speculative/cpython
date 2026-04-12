/*
 * Experiment 3: C Extension Ring Buffer for Trace Capture
 *
 * A minimal C extension that registers PEP 669 callbacks implemented in C,
 * writing events to a fixed-size ring buffer. This tests the hypothesis
 * that C-level callbacks are significantly faster than Python callbacks.
 *
 * The buffer stores compact event records:
 *   - 8 bytes: timestamp (perf counter)
 *   - 8 bytes: code object pointer (identity)
 *   - 4 bytes: instruction offset or line number
 *   - 1 byte:  event type
 *   = 21 bytes per event (padded to 24 for alignment)
 */

#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <time.h>
#include <string.h>

/* Event record - packed into 24 bytes */
typedef struct {
    uint64_t timestamp;
    uint64_t code_id;       /* pointer to code object, used as identity */
    int32_t  offset_or_line;
    uint8_t  event_type;
    uint8_t  _pad[3];
} TraceEvent;

/* Ring buffer */
#define DEFAULT_BUFFER_SIZE (1024 * 1024)  /* 1M events = 24MB */

static TraceEvent *g_buffer = NULL;
static size_t g_buffer_size = 0;
static size_t g_write_pos = 0;
static size_t g_total_events = 0;
static int g_active = 0;

/* Fast timestamp */
static inline uint64_t get_timestamp(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}

/* Write an event to the ring buffer */
static inline void record_event(uint8_t event_type, PyObject *code, int32_t offset_or_line) {
    if (!g_active || !g_buffer) return;

    size_t pos = g_write_pos;
    g_buffer[pos].timestamp = get_timestamp();
    g_buffer[pos].code_id = (uint64_t)(uintptr_t)code;
    g_buffer[pos].offset_or_line = offset_or_line;
    g_buffer[pos].event_type = event_type;

    g_write_pos = (pos + 1) % g_buffer_size;
    g_total_events++;
}

/* === PEP 669 Callbacks === */

#define EVT_PY_START  0
#define EVT_PY_RETURN 1
#define EVT_LINE      2
#define EVT_INSTRUCTION 3

static PyObject *
cb_py_start(PyObject *self, PyObject *const *args, Py_ssize_t nargs)
{
    /* args: (code, instruction_offset) */
    record_event(EVT_PY_START, args[0], (int32_t)PyLong_AsLong(args[1]));
    Py_RETURN_NONE;
}

static PyObject *
cb_py_return(PyObject *self, PyObject *const *args, Py_ssize_t nargs)
{
    /* args: (code, instruction_offset, retval) */
    record_event(EVT_PY_RETURN, args[0], (int32_t)PyLong_AsLong(args[1]));
    Py_RETURN_NONE;
}

static PyObject *
cb_line(PyObject *self, PyObject *const *args, Py_ssize_t nargs)
{
    /* args: (code, line_number) */
    record_event(EVT_LINE, args[0], (int32_t)PyLong_AsLong(args[1]));
    Py_RETURN_NONE;
}

static PyObject *
cb_instruction(PyObject *self, PyObject *const *args, Py_ssize_t nargs)
{
    /* args: (code, instruction_offset) */
    record_event(EVT_INSTRUCTION, args[0], (int32_t)PyLong_AsLong(args[1]));
    Py_RETURN_NONE;
}

/* === Control API === */

static PyObject *
tracebuf_start(PyObject *self, PyObject *args)
{
    int buffer_size = DEFAULT_BUFFER_SIZE;
    if (!PyArg_ParseTuple(args, "|i", &buffer_size))
        return NULL;

    if (g_buffer) {
        PyMem_Free(g_buffer);
    }
    g_buffer = (TraceEvent *)PyMem_Calloc(buffer_size, sizeof(TraceEvent));
    if (!g_buffer) {
        return PyErr_NoMemory();
    }
    g_buffer_size = buffer_size;
    g_write_pos = 0;
    g_total_events = 0;
    g_active = 1;

    Py_RETURN_NONE;
}

static PyObject *
tracebuf_stop(PyObject *self, PyObject *Py_UNUSED(ignored))
{
    g_active = 0;
    Py_RETURN_NONE;
}

static PyObject *
tracebuf_stats(PyObject *self, PyObject *Py_UNUSED(ignored))
{
    return Py_BuildValue("{s:K,s:n,s:n,s:i}",
        "total_events", (unsigned long long)g_total_events,
        "buffer_size", (Py_ssize_t)g_buffer_size,
        "write_pos", (Py_ssize_t)g_write_pos,
        "active", g_active
    );
}

static PyObject *
tracebuf_reset(PyObject *self, PyObject *Py_UNUSED(ignored))
{
    g_write_pos = 0;
    g_total_events = 0;
    if (g_buffer) {
        memset(g_buffer, 0, g_buffer_size * sizeof(TraceEvent));
    }
    Py_RETURN_NONE;
}

static PyObject *
tracebuf_get_events(PyObject *self, PyObject *args)
{
    /* Return last N events as a list of tuples */
    int count = 100;
    if (!PyArg_ParseTuple(args, "|i", &count))
        return NULL;

    size_t available = g_total_events < g_buffer_size ? g_total_events : g_buffer_size;
    if ((size_t)count > available) count = (int)available;

    PyObject *result = PyList_New(count);
    if (!result) return NULL;

    for (int i = 0; i < count; i++) {
        size_t idx;
        if (g_total_events <= g_buffer_size) {
            idx = i;
        } else {
            idx = (g_write_pos + g_buffer_size - count + i) % g_buffer_size;
        }
        TraceEvent *ev = &g_buffer[idx];
        PyObject *tuple = Py_BuildValue("(KKib)",
            (unsigned long long)ev->timestamp,
            (unsigned long long)ev->code_id,
            (int)ev->offset_or_line,
            (char)ev->event_type
        );
        if (!tuple) {
            Py_DECREF(result);
            return NULL;
        }
        PyList_SET_ITEM(result, i, tuple);
    }
    return result;
}

/* Module definition */

static PyMethodDef tracebuf_methods[] = {
    {"start", tracebuf_start, METH_VARARGS,
     "Start recording. Optional: buffer size (default 1M events)."},
    {"stop", tracebuf_stop, METH_NOARGS,
     "Stop recording."},
    {"stats", tracebuf_stats, METH_NOARGS,
     "Get buffer statistics."},
    {"reset", tracebuf_reset, METH_NOARGS,
     "Reset buffer and counters."},
    {"get_events", tracebuf_get_events, METH_VARARGS,
     "Get last N events as list of tuples."},

    /* Callbacks - exposed so Python code can register them with sys.monitoring */
    {"cb_py_start", (PyCFunction)cb_py_start, METH_FASTCALL,
     "PEP 669 PY_START callback."},
    {"cb_py_return", (PyCFunction)cb_py_return, METH_FASTCALL,
     "PEP 669 PY_RETURN callback."},
    {"cb_line", (PyCFunction)cb_line, METH_FASTCALL,
     "PEP 669 LINE callback."},
    {"cb_instruction", (PyCFunction)cb_instruction, METH_FASTCALL,
     "PEP 669 INSTRUCTION callback."},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef tracebuf_module = {
    PyModuleDef_HEAD_INIT,
    "_tracebuf",
    "C ring buffer for trace event recording",
    -1,
    tracebuf_methods
};

PyMODINIT_FUNC
PyInit__tracebuf(void)
{
    return PyModule_Create(&tracebuf_module);
}
