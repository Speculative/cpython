/*
 * pycore_tracewal.h — Inline WAL tracing for the eval loop
 *
 * Provides hook functions called from bytecode handlers (STORE_FAST,
 * STORE_SUBSCR, STORE_ATTR, RETURN_VALUE, YIELD_VALUE, RESUME) to emit
 * WAL entries with direct localsplus access — no settrace, no PyFrame_GetVar.
 */

#ifndef Py_INTERNAL_TRACEWAL_H
#define Py_INTERNAL_TRACEWAL_H
#ifndef Py_BUILD_CORE
#  error "this header requires Py_BUILD_CORE define"
#endif

#include "pycore_interpframe_structs.h"  /* _PyInterpreterFrame */
#include "pycore_stackref.h"             /* _PyStackRef */

/* Global enable flag — branch-predicted to 0 when tracing is off */
extern int _PyWAL_enabled;

/* LINE tracking mode:
 * 0 = no per-dispatch LINE (store hooks only)
 * 1 = full per-dispatch LINE (every source line) */
extern int _PyWAL_line_mode;

/* Eval loop hooks */
extern void _PyWAL_OnResume(_PyInterpreterFrame *frame, int oparg);
extern void _PyWAL_OnStoreFast(_PyInterpreterFrame *frame, int local_idx,
                                _PyStackRef old_val, _PyStackRef new_val);
extern void _PyWAL_OnStoreSubscr(_PyInterpreterFrame *frame,
                                  PyObject *container, PyObject *sub,
                                  PyObject *value);
extern void _PyWAL_OnStoreAttr(_PyInterpreterFrame *frame,
                                PyObject *owner, PyObject *name,
                                PyObject *value);
extern void _PyWAL_OnDeleteSubscr(_PyInterpreterFrame *frame,
                                   PyObject *container, PyObject *sub);
extern void _PyWAL_OnDeleteAttr(_PyInterpreterFrame *frame,
                                 PyObject *owner, PyObject *name);
extern void _PyWAL_OnReturn(_PyInterpreterFrame *frame, PyObject *retval);
extern void _PyWAL_OnYield(_PyInterpreterFrame *frame, PyObject *retval);
extern void _PyWAL_OnCall(_PyInterpreterFrame *frame,
                           PyObject *callable, PyObject *self_or_null,
                           _PyStackRef *args, int oparg);
extern void _PyWAL_OnRaise(_PyInterpreterFrame *frame, PyObject *exc);
extern void _PyWAL_OnExceptStart(_PyInterpreterFrame *frame, PyObject *exc);
extern void _PyWAL_OnStoreGlobal(_PyInterpreterFrame *frame,
                                  PyObject *name, PyObject *value);
extern void _PyWAL_OnStoreDeref(_PyInterpreterFrame *frame,
                                 int cell_idx, PyObject *value);
extern void _PyWAL_FlushPendingSnapshots(_PyInterpreterFrame *frame);
extern int _PyWAL_pending_snapshots;  /* nonzero if snapshots need flushing */
extern void _PyWAL_CheckLine(_PyInterpreterFrame *frame);

/* Python module API (called from _tracewalmodule.c) */
extern int     _PyWAL_Start(int buf_size, const char *output_file);
extern void    _PyWAL_Stop(void);
extern PyObject *_PyWAL_GetStats(void);
extern PyObject *_PyWAL_GetWAL(int max_count);
extern PyObject *_PyWAL_GetStringTable(void);
extern PyObject *_PyWAL_GetOidTypeNames(void);
extern void    _PyWAL_Clear(void);
extern PyObject *_PyWAL_RegisterCode(PyObject *code_obj);

#endif /* !Py_INTERNAL_TRACEWAL_H */
