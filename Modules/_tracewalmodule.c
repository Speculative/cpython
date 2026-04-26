/*
 * _tracewalmodule.c — Python module for inline WAL tracing
 *
 * Exposes start/stop/stats/get_wal/clear/register_code to Python.
 * Same API as _ctrace_wal so the same test harness works.
 */

#define Py_BUILD_CORE_BUILTIN
#include "Python.h"
#include "pycore_tracewal.h"

static PyObject *
tracewal_start(PyObject *self, PyObject *args, PyObject *kwargs)
{
    static char *kwlist[] = {"buf_size", "output_file", "line_mode", NULL};
    int buf_size = 0;  /* 0 = default (64MB) */
    const char *output_file = NULL;
    int line_mode = -1;  /* -1 = don't change */

    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "|izi", kwlist,
                                     &buf_size, &output_file, &line_mode))
        return NULL;

    if (line_mode >= 0) {
        _PyWAL_line_mode = line_mode;
    }

    if (_PyWAL_Start(buf_size, output_file) < 0) {
        if (output_file) {
            PyErr_SetFromErrnoWithFilename(PyExc_OSError, output_file);
        } else {
            PyErr_NoMemory();
        }
        return NULL;
    }
    Py_RETURN_NONE;
}

static PyObject *
tracewal_stop(PyObject *self, PyObject *Py_UNUSED(ignored))
{
    _PyWAL_Stop();
    Py_RETURN_NONE;
}

static PyObject *
tracewal_stats(PyObject *self, PyObject *Py_UNUSED(ignored))
{
    return _PyWAL_GetStats();
}

static PyObject *
tracewal_get_wal(PyObject *self, PyObject *args)
{
    int max_count = 100;
    if (!PyArg_ParseTuple(args, "|i", &max_count))
        return NULL;
    return _PyWAL_GetWAL(max_count);
}

static PyObject *
tracewal_clear(PyObject *self, PyObject *Py_UNUSED(ignored))
{
    _PyWAL_Clear();
    Py_RETURN_NONE;
}

static PyObject *
tracewal_get_string_table(PyObject *self, PyObject *Py_UNUSED(ignored))
{
    return _PyWAL_GetStringTable();
}

static PyObject *
tracewal_register_code(PyObject *self, PyObject *args)
{
    /* Compatibility with _ctrace_wal's register_code(code, first_line, line_data, strings).
     * The fork auto-registers code, so we accept the args and ignore the analysis data.
     * We still register the code object so it gets a stable code_idx. */
    PyObject *code_obj;
    int first_line;
    PyObject *line_data, *string_list;

    if (!PyArg_ParseTuple(args, "OiOO", &code_obj, &first_line, &line_data, &string_list))
        return NULL;

    return _PyWAL_RegisterCode(code_obj);
}

static PyMethodDef methods[] = {
    {"register_code", tracewal_register_code, METH_VARARGS,
     "Register code object (compatibility — fork auto-registers)."},
    {"start", (PyCFunction)tracewal_start, METH_VARARGS | METH_KEYWORDS,
     "Start WAL tracing. Args: buf_size=64MB, output_file=None."},
    {"stop", tracewal_stop, METH_NOARGS,
     "Stop tracing."},
    {"stats", tracewal_stats, METH_NOARGS,
     "Get tracing statistics."},
    {"get_wal", tracewal_get_wal, METH_VARARGS,
     "Get WAL entries as list of dicts."},
    {"get_string_table", tracewal_get_string_table, METH_NOARGS,
     "Return interned strings (method/attr names) as a list."},
    {"clear", tracewal_clear, METH_NOARGS,
     "Clear all tracing state."},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef _tracewalmodule = {
    PyModuleDef_HEAD_INIT,
    "_tracewal",
    "Inline WAL tracing module",
    -1,
    methods
};

PyMODINIT_FUNC
PyInit__tracewal(void)
{
    return PyModule_Create(&_tracewalmodule);
}
