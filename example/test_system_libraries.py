"""Check that the system library requested by the self-test is usable.

The self-test passes FFTW in the reusable workflow's ``libraries`` input
(it is not part of the GitHub-hosted Ubuntu image), and the example config
runs this file alongside one of the example packages, so it executes inside
the same venv and environment as the real package tests. If the
``libraries`` input stops installing packages, this test fails.
"""

import ctypes


def test_fftw_is_loadable():
    # libfftw3.so.3 is the library provided by the libfftw3-double3 apt
    # package; loading it fails with OSError if it is not installed.
    fftw = ctypes.CDLL("libfftw3.so.3")
    # fftw_version is a char array, not a pointer, so read it in place.
    version = ctypes.string_at(ctypes.addressof(ctypes.c_char.in_dll(fftw, "fftw_version")))
    assert version.decode().startswith("fftw-3")
