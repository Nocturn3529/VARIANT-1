"""Exercise the packaged CPU DLLs at one/four threads without an LLM model."""
import argparse
import ctypes as c
import json
import os
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--runtime', type=Path, default=Path(__file__).resolve().parents[1] / 'dist/win-unpacked/resources/bin')
args = parser.parse_args()
folder = args.runtime.resolve()
guard = os.add_dll_directory(str(folder))
base = c.CDLL(str(folder / 'ggml-base.dll'))
cpu = c.CDLL(str(folder / 'ggml-cpu-x64.dll'))


class Params(c.Structure):
    _fields_ = [('mem_size', c.c_size_t), ('mem_buffer', c.c_void_p), ('no_alloc', c.c_bool)]


def bind(lib, name, result, arguments):
    function = getattr(lib, name)
    function.restype, function.argtypes = result, arguments
    return function


ptr = c.c_void_p
init = bind(base, 'ggml_init', ptr, [Params])
free = bind(base, 'ggml_free', None, [ptr])
tensor = bind(base, 'ggml_new_tensor_2d', ptr, [ptr, c.c_int, c.c_int64, c.c_int64])
data = bind(base, 'ggml_get_data', ptr, [ptr])
multiply = bind(base, 'ggml_mul_mat', ptr, [ptr, ptr, ptr])
graph = bind(base, 'ggml_new_graph', ptr, [ptr])
expand = bind(base, 'ggml_build_forward_expand', None, [ptr, ptr])
compute = bind(cpu, 'ggml_graph_compute_with_ctx', c.c_int, [ptr, ptr, c.c_int])
for threads in [1, 4]:
    context = init(Params(16 * 1024 * 1024, None, False))
    assert context
    try:
        a, b = tensor(context, 0, 8, 3), tensor(context, 0, 8, 4)
        for item, count, value in [(a, 24, 1), (b, 32, 2)]:
            values = (c.c_float * count).from_address(data(item))
            for i in range(count):
                values[i] = value
        result = multiply(context, a, b)
        plan = graph(context)
        expand(plan, result)
        status = compute(context, plan, threads)
        values = list((c.c_float * 12).from_address(data(result)))
        assert status == 0 and values == [16.0] * 12, (status, values)
        print(json.dumps({'backend': 'ggml-cpu-x64', 'threads': threads, 'status': 'pass'}))
    finally:
        free(context)
