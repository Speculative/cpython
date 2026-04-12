from setuptools import setup, Extension

setup(
    name='tracebuf',
    ext_modules=[
        Extension(
            '_tracebuf',
            sources=['tracebuf.c'],
            extra_compile_args=['-O2'],
        ),
    ],
)
