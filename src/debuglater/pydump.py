"""
The MIT License (MIT)
Copyright (C) 2012 Eli Finer <eli.finer@gmail.com>

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""
import builtins
from contextlib import contextmanager
from pathlib import Path
import os
import sys
import pdb
import gzip
import linecache
from traceback import format_exception
try:
    import cPickle as pickle
except ImportError:
    import pickle

from colorama import init
from colorama import Fore, Style

init()

try:
    import dill
except ImportError:
    dill = None

DUMP_VERSION = 1


def _print_not_dill():
    print("Using pickle: Only built-in objects will be serialized. "
          "To serialize everything: pip install 'debuglater[all]'\n")


def save_dump(filename, tb=None):
    """
    Saves a Python traceback in a pickled file. This function will usually be
    called from an except block to allow post-mortem debugging of a failed
    process.

    The saved file can be loaded with load_dump which creates a fake traceback
    object that can be passed to any reasonable Python debugger.
    """
    if not tb:
        tb = sys.exc_info()[2]
    fake_tb = FakeTraceback(tb)
    _remove_builtins(fake_tb)

    dump = {
        "traceback": fake_tb,
        "files": _get_traceback_files(fake_tb),
        "dump_version": DUMP_VERSION,
    }

    with gzip.open(filename, "wb") as f:
        if dill is not None:
            dill.dump(dump, f)
        else:
            _print_not_dill()
            pickle.dump(dump, f, protocol=pickle.HIGHEST_PROTOCOL)


def load_dump(filename):
    # NOTE: I think we can get rid of this
    # ugly hack to handle running non-install debuglater
    if "debuglater.pydump" not in sys.modules:
        sys.modules["debuglater.pydump"] = sys.modules[__name__]
    with gzip.open(filename, "rb") as f:
        if dill is not None:
            try:
                return dill.load(f)
            except IOError:
                try:
                    with open(filename, "rb") as f:
                        return dill.load(f)
                except Exception:
                    pass  # dill load failed, try pickle instead
        else:
            _print_not_dill()

        try:
            return pickle.load(f)
        except IOError:
            with open(filename, "rb") as f:
                return pickle.load(f)


def debug_dump(dump_filename, post_mortem_func=pdb.post_mortem):
    # monkey patching for pdb's longlist command
    import inspect
    import types

    inspect.isframe = lambda obj: isinstance(
        obj, types.FrameType) or obj.__class__.__name__ == "FakeFrame"
    inspect.iscode = lambda obj: isinstance(
        obj, types.CodeType) or obj.__class__.__name__ == "FakeCode"
    inspect.isclass = lambda obj: isinstance(
        obj, type) or obj.__class__.__name__ == "FakeClass"
    inspect.istraceback = lambda obj: isinstance(
        obj, types.TracebackType) or obj.__class__.__name__ == "FakeTraceback"

    with add_to_sys_path('.', chdir=False):
        dump = load_dump(dump_filename)

    _cache_files(dump["files"])
    tb = dump["traceback"]
    _inject_builtins(tb)
    _old_checkcache = linecache.checkcache
    linecache.checkcache = lambda filename=None: None
    post_mortem_func(tb)
    linecache.checkcache = _old_checkcache


class FakeClass(object):

    def __init__(self, repr, vars):
        self.__repr = repr
        self.__dict__.update(vars)

    def __repr__(self):
        return self.__repr


class FakeCode(object):

    def __init__(self, code):
        self.co_filename = os.path.abspath(code.co_filename)
        self.co_name = code.co_name
        self.co_argcount = code.co_argcount
        self.co_consts = tuple(
            FakeCode(c) if hasattr(c, "co_filename") else c
            for c in code.co_consts)
        self.co_firstlineno = code.co_firstlineno
        self.co_lnotab = code.co_lnotab
        self.co_varnames = code.co_varnames
        self.co_flags = code.co_flags
        self.co_code = code.co_code

        # co_lines was introduced in a recent version
        if hasattr(code, 'co_lines'):
            self.co_lines = FakeCoLines(code.co_lines)


class FakeCoLines:

    def __init__(self, co_lines) -> None:
        self._co_lines = list(co_lines())

    def __call__(self):
        return iter(self._co_lines)


class FakeFrame(object):

    def __init__(self, frame):
        self.f_code = FakeCode(frame.f_code)
        self.f_locals = _convert_dict(frame.f_locals)
        self.f_globals = _convert_dict(frame.f_globals)
        self.f_lineno = frame.f_lineno
        self.f_back = FakeFrame(frame.f_back) if frame.f_back else None

        if "self" in self.f_locals:
            self.f_locals["self"] = _convert_obj(frame.f_locals["self"])


class FakeTraceback(object):

    def __init__(self, traceback):
        self.tb_frame = FakeFrame(traceback.tb_frame)
        self.tb_lineno = traceback.tb_lineno
        self.tb_next = FakeTraceback(
            traceback.tb_next) if traceback.tb_next else None
        self.tb_lasti = 0


def _remove_builtins(fake_tb):
    traceback = fake_tb
    while traceback:
        frame = traceback.tb_frame
        while frame:
            frame.f_globals = dict((k, v) for k, v in frame.f_globals.items()
                                   if k not in dir(builtins))
            frame = frame.f_back
        traceback = traceback.tb_next


def _inject_builtins(fake_tb):
    traceback = fake_tb
    while traceback:
        frame = traceback.tb_frame
        while frame:
            frame.f_globals.update(builtins.__dict__)
            frame = frame.f_back
        traceback = traceback.tb_next


def _get_traceback_files(traceback):
    files = {}
    while traceback:
        frame = traceback.tb_frame
        while frame:
            filename = os.path.abspath(frame.f_code.co_filename)
            if filename not in files:
                try:
                    files[filename] = open(filename).read()
                except IOError:
                    files[filename] = ("couldn't locate '%s' "
                                       "during dump" %
                                       frame.f_code.co_filename)
            frame = frame.f_back
        traceback = traceback.tb_next
    return files


def _safe_repr(v):
    try:
        return repr(v)
    except Exception as e:
        return "repr error: " + str(e)


def _convert_obj(obj):
    try:
        return FakeClass(_safe_repr(obj), _convert_dict(obj.__dict__))
    except Exception:
        return _convert(obj)


def _convert_dict(v):
    return dict((_convert(k), _convert(i)) for (k, i) in v.items())


def _convert_seq(v):
    return (_convert(i) for i in v)


def _convert(v):
    if dill is not None:
        try:
            dill.dumps(v)
            return v
        except Exception:
            return _safe_repr(v)
    else:
        from datetime import date, time, datetime, timedelta

        BUILTIN = (str, int, float, date, time, datetime, timedelta)
        # XXX: what about bytes and bytearray?

        if v is None:
            return v

        if type(v) in BUILTIN:
            return v

        if type(v) is tuple:
            return tuple(_convert_seq(v))

        if type(v) is list:
            return list(_convert_seq(v))

        if type(v) is set:
            return set(_convert_seq(v))

        if type(v) is dict:
            return _convert_dict(v)

        return _safe_repr(v)


def _cache_files(files):
    for name, data in files.items():
        lines = [line + "\n" for line in data.splitlines()]
        linecache.cache[name] = (len(data), None, lines, name)


def run(filename, echo=True, tb=None):
    out = Path(filename).with_suffix('.dump')

    if echo:
        print(Fore.RED + f'Exception caught, writing {out}\n')

    save_dump(out, tb=tb)

    if echo:
        print(f'To debug, run:\n  dltr {out}')
        print(Style.RESET_ALL)


def excepthook_factory(filename):

    def excepthook(type, value, traceback):
        print(''.join(format_exception(type, value, traceback)),
              file=sys.stderr,
              end='')

        if type is not KeyboardInterrupt:
            run(filename, tb=traceback)

    return excepthook


@contextmanager
def add_to_sys_path(path, chdir=False):
    cwd_old = os.getcwd()

    if path is not None:
        path = os.path.abspath(path)
        sys.path.insert(0, path)

        if chdir:
            os.chdir(path)

    try:
        yield
    finally:
        if path is not None:
            sys.path.remove(path)
            os.chdir(cwd_old)
