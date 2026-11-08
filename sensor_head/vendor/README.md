# Vendored: primesense 2.2.0.30 (OpenNI2 Python binding)

The PyPI package `primesense` 2.2.0.30.post5 (OpenNI2/NiTE2 ctypes bindings
by PrimeSense Inc, MIT licence), copied here unchanged except for one fix in
`utils.py`:
`CEnumMeta` created the enum members inside the metaclass's `__new__`, which
Python 3.13+ rejects (`TypeError: abstract class`: ctypes now finishes a
type's setup in `__init__`). The members are now created in `__init__`,
which works on every Python from 3.6 up. `xtion_head.py` puts this directory
first on `sys.path`, so no pip install is needed on the tablet.
