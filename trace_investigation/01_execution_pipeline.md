# How Python Code Is Executed

From source file to running program, CPython transforms code through a multi-stage pipeline. Understanding this pipeline is essential for knowing where and how to intercept execution for tracing.

## Pipeline Overview

```
Source Code (.py)
    |
    v
[1] Tokenizer  (Parser/lexer/, Parser/tokenizer/)
    |
    v
Token Stream
    |
    v
[2] PEG Parser  (Parser/parser.c, Grammar/python.gram)
    |
    v
Abstract Syntax Tree (AST)
    |
    v
[3] Symbol Table Builder  (Python/symtable.c)
    |
    v
Symbol Table
    |
    v
[4] Compiler  (Python/compile.c)
    |
    v
Instruction Sequence
    |
    v
[5] CFG Construction & Optimization  (Python/flowgraph.c)
    |
    v
Optimized Control Flow Graph
    |
    v
[6] Assembler  (Python/assemble.c)
    |
    v
PyCodeObject  (bytecode + metadata)
    |
    v
[7] Eval Loop  (Python/ceval.c)
    |
    v
Execution
```

## Stage 1: Tokenization

**Files:** `Parser/lexer/`, `Parser/tokenizer/`

The lexer converts raw source text into a stream of tokens (keywords, identifiers, operators, literals, etc.). Token types are defined in `Grammar/Tokens`. This is a standard lexing phase.

## Stage 2: Parsing (Source -> AST)

**Files:** `Parser/parser.c` (generated), `Grammar/python.gram` (source grammar)

Since Python 3.9, CPython uses a PEG (Parsing Expression Grammar) parser. Unusually, it operates on a token stream rather than raw characters. The grammar in `python.gram` is used to auto-generate `parser.c`.

**Entry point:** `_PyParser_ASTFromString()` / `_PyParser_ASTFromFile()` in `Parser/peg_api.c`

**Output:** An AST defined in `Parser/Python.asdl`, realized as C structs in `Include/internal/pycore_ast.h`. The AST is a tree of nodes like `FunctionDef`, `Assign`, `BinOp`, `Call`, etc. Each node carries source location info (line, column, end_line, end_column).

## Stage 3: Symbol Table

**Files:** `Python/symtable.c`, `Include/internal/pycore_symtable.h`

Before compilation, the symbol table builder walks the AST to determine variable scopes. For each block (module, function, class, comprehension), it creates a `PySTEntryObject` that records:

- Which names are local, global, free, or cell variables
- Whether the block is a generator, coroutine, etc.
- Nested scope relationships

This information is critical for the compiler to emit the correct `LOAD_FAST` / `LOAD_GLOBAL` / `LOAD_DEREF` instructions and to set up closure cells.

**Scope categories:** `LOCAL`, `GLOBAL_EXPLICIT`, `GLOBAL_IMPLICIT`, `FREE`, `CELL`

## Stage 4: Compilation (AST -> Instructions)

**Files:** `Python/compile.c` (~1776 lines)

**Entry point:** `_PyAST_Compile()` at `Python/compile.c:1509`

The compiler walks the AST and emits a flat instruction sequence. Each instruction has an opcode, argument, and source location. The compiler handles:

- Control flow (`if`, `while`, `for`, `try`)
- Function/class definitions
- Comprehensions (inlined or as separate code objects)
- Default argument values, decorators, annotations
- `yield`/`await` for generators and coroutines

Each function body, class body, module, and comprehension gets its own code object (and therefore its own compilation unit).

## Stage 5: CFG Construction & Optimization

**Files:** `Python/flowgraph.c`

The instruction sequence is converted to a Control Flow Graph (CFG). Optimizations include:

- Constant folding
- Dead code elimination
- Jump optimization (jump-to-jump elimination)
- Peephole optimizations

## Stage 6: Assembly (Instructions -> Bytecode)

**Files:** `Python/assemble.c`

The assembler converts the optimized instruction sequence into a `PyCodeObject`:

- Packs instructions into 2-byte `_Py_CODEUNIT` values (1 byte opcode + 1 byte arg)
- Generates `EXTENDED_ARG` instructions for arguments > 255
- Builds the `co_linetable` (source location mapping)
- Builds the `co_exceptiontable` (exception handler mapping)
- Collects constants, names, and variable names into tuples

**Key function:** `_PyAssemble_MakeCodeObject()` in `Python/assemble.c`

## Stage 7: Execution (Eval Loop)

**Files:** `Python/ceval.c` (~3812 lines), `Python/generated_cases.c.h`

**Entry point:** `_PyEval_EvalFrameDefault()`

### Bytecode Format

Each instruction is a `_Py_CODEUNIT` (defined in `Include/internal/pycore_structs.h`):
```c
typedef union {
    uint16_t cache;
    struct {
        uint8_t code;   // opcode
        uint8_t arg;    // argument
    } op;
} _Py_CODEUNIT;
```

### Frame Setup

When a function is called, CPython creates an `_PyInterpreterFrame` (see [Object and Value Storage](02_object_storage.md#frames)). Frames are allocated on a per-thread stack for performance, forming a linked list via the `previous` pointer.

### Dispatch Loop

The eval loop is a giant dispatch over opcodes. When `USE_COMPUTED_GOTOS` is enabled (most platforms), it uses GCC's computed goto extension for efficient dispatch. Otherwise, it falls back to a switch statement.

The opcode handlers are auto-generated from `Python/bytecodes.c` into `Python/generated_cases.c.h`. Each handler:
1. Pops operands from the evaluation stack
2. Performs the operation
3. Pushes results back onto the stack
4. Advances the instruction pointer

### Inline Caching & Specialization

Since Python 3.11, CPython includes an adaptive specialization system. "Generic" instructions like `LOAD_ATTR` are replaced with specialized versions (e.g., `LOAD_ATTR_INSTANCE_VALUE`) after observing runtime types. Inline cache entries follow the instruction in the bytecode stream. This is managed by `Python/specialize.c`.

### Tier 2 Execution (JIT)

CPython 3.13+ includes an experimental Tier 2 optimizer that can compile hot code paths into "traces" — sequences of micro-ops that bypass the normal dispatch overhead. These are stored in `co_executors`. This is relevant because **our tracing must work with both Tier 1 and Tier 2 execution paths**.

## Implications for Tracing

| Pipeline Stage | Tracing Relevance |
|---|---|
| Tokenizer/Parser | Not relevant — we trace at execution time |
| Symbol Table | Useful for understanding variable scopes in captured state |
| Code Object | **Critical** — contains bytecode, variable names, line mappings |
| Frame | **Critical** — contains local variables, stack state, instruction pointer |
| Eval Loop | **Primary intercept point** — where we hook in to capture events |
| Specialization | Must handle specialized opcodes transparently |
| Tier 2 JIT | Must either instrument or disable for traced code |

The key insight is that the code object + frame together contain everything we need to reconstruct execution state at any point:
- **What code is running:** `frame->f_executable` -> code object -> bytecode + source mapping
- **Where we are:** `frame->instr_ptr` -> current instruction offset
- **Variable values:** `frame->localsplus[]` -> local/cell/free variables
- **Call stack:** `frame->previous` -> caller's frame
- **Globals/builtins:** `frame->f_globals`, `frame->f_builtins`

See also: [Existing Tracing Infrastructure](05_tracing_infrastructure.md) for how CPython provides hooks into the eval loop.
