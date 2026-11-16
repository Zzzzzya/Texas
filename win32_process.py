"""Read-only access to the Windows Arknights process."""

from __future__ import annotations

import ctypes
import sys
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path

_PROCESS_NAME = "Arknights.exe"
_MODULE_NAME = "GameAssembly.dll"
_PROCESS_VM_READ = 0x0010
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TH32CS_SNAPPROCESS = 0x00000002
_TH32CS_SNAPMODULE = 0x00000008
_ERROR_ACCESS_DENIED = 5
_ERROR_BAD_LENGTH = 24
_ERROR_NO_MORE_FILES = 18
_STILL_ACTIVE = 259
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_MAX_PATH = 260
_ADDRESS_LIMIT = 1 << 64


class ProcessError(RuntimeError):
    """The game's process or memory could not be accessed."""

    def __init__(self, message: str, *, winerror: int | None = None):
        super().__init__(message)
        self.winerror = winerror


class _ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * _MAX_PATH),
    ]


class _ModuleEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("th32ModuleID", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("GlblcntUsage", wintypes.DWORD),
        ("ProccntUsage", wintypes.DWORD),
        ("modBaseAddr", ctypes.c_void_p),
        ("modBaseSize", wintypes.DWORD),
        ("hModule", wintypes.HMODULE),
        ("szModule", wintypes.WCHAR * 256),
        ("szExePath", wintypes.WCHAR * _MAX_PATH),
    ]


def _kernel32():
    if sys.platform != "win32" or ctypes.sizeof(ctypes.c_void_p) != 8:
        raise ProcessError("A 64-bit Python interpreter on Windows is required.")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    dword, handle, boolean = wintypes.DWORD, wintypes.HANDLE, wintypes.BOOL
    pointer = ctypes.c_void_p
    size = ctypes.c_size_t
    functions = [
        ("CreateToolhelp32Snapshot", handle, [dword, dword]),
        ("Process32FirstW", boolean, [handle, ctypes.POINTER(_ProcessEntry)]),
        ("Process32NextW", boolean, [handle, ctypes.POINTER(_ProcessEntry)]),
        ("Module32FirstW", boolean, [handle, ctypes.POINTER(_ModuleEntry)]),
        ("Module32NextW", boolean, [handle, ctypes.POINTER(_ModuleEntry)]),
        ("OpenProcess", handle, [dword, boolean, dword]),
        ("QueryFullProcessImageNameW", boolean, [handle, dword, wintypes.LPWSTR, wintypes.LPDWORD]),
        ("ReadProcessMemory", boolean, [handle, pointer, pointer, size, ctypes.POINTER(size)]),
        ("GetExitCodeProcess", boolean, [handle, wintypes.LPDWORD]),
        ("CloseHandle", boolean, [handle]),
    ]
    for name, result, arguments in functions:
        function = getattr(kernel, name)
        function.restype = result
        function.argtypes = arguments
    return kernel


def _process_error(operation: str, code: int) -> ProcessError:
    if code == _ERROR_ACCESS_DENIED:
        detail = "access denied"
    else:
        detail = ctypes.FormatError(code).strip() if code else "unknown system error"
    return ProcessError(f"{operation}: {detail} (Win32 error {code}).", winerror=code)


@contextmanager
def _snapshot(kernel, flags: int, pid: int = 0):
    # The loader can change the module list while Toolhelp is taking a snapshot.
    for _ in range(3):
        handle = kernel.CreateToolhelp32Snapshot(flags, pid)
        if handle != _INVALID_HANDLE_VALUE:
            break
        code = ctypes.get_last_error()
        if code != _ERROR_BAD_LENGTH or flags != _TH32CS_SNAPMODULE:
            raise _process_error(f"CreateToolhelp32Snapshot for PID {pid}", code)
    else:
        raise _process_error(f"CreateToolhelp32Snapshot for PID {pid}", code)
    try:
        yield handle
    finally:
        kernel.CloseHandle(handle)


def _find_game_pid(kernel) -> int:
    pids = []
    entry = _ProcessEntry()
    entry.dwSize = ctypes.sizeof(entry)
    with _snapshot(kernel, _TH32CS_SNAPPROCESS) as snapshot:
        found = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while found:
            if entry.szExeFile.casefold() == _PROCESS_NAME.casefold():
                pids.append(entry.th32ProcessID)
            found = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        code = ctypes.get_last_error()
        if code != _ERROR_NO_MORE_FILES:
            raise _process_error("Enumerating Windows processes", code)
    if not pids:
        raise ProcessError(f"{_PROCESS_NAME} is not running.")
    if len(pids) > 1:
        matches = ", ".join(str(pid) for pid in sorted(pids))
        raise ProcessError(f"Multiple {_PROCESS_NAME} processes found (PIDs {matches}). Specify a PID.")
    return pids[0]


def _game_module(kernel, pid: int) -> tuple[int, Path]:
    entry = _ModuleEntry()
    entry.dwSize = ctypes.sizeof(entry)
    with _snapshot(kernel, _TH32CS_SNAPMODULE, pid) as snapshot:
        found = kernel.Module32FirstW(snapshot, ctypes.byref(entry))
        while found:
            if entry.szModule.casefold() == _MODULE_NAME.casefold():
                if entry.modBaseAddr is None or not entry.szExePath:
                    raise ProcessError(f"{_MODULE_NAME} in PID {pid} has incomplete module information.")
                return entry.modBaseAddr, Path(entry.szExePath)
            found = kernel.Module32NextW(snapshot, ctypes.byref(entry))
        code = ctypes.get_last_error()
        if code != _ERROR_NO_MORE_FILES:
            raise _process_error(f"Enumerating modules of PID {pid}", code)
    raise ProcessError(f"{_MODULE_NAME} is not loaded in PID {pid}.")


class ProcessMemory:
    """An open process handle. Use as a context manager to release it."""

    def __init__(self, kernel, handle: int, pid: int, module_base: int, module_path: Path):
        self._kernel = kernel
        self._handle: int | None = handle
        self.pid = pid
        self.module_base = module_base
        self.module_path = module_path

    @classmethod
    def open(cls, pid: int | None = None) -> ProcessMemory:
        if pid is not None and (type(pid) is not int or not 0 < pid <= 0xFFFFFFFF):
            raise ProcessError("PID must be a positive Windows process ID.")
        kernel = _kernel32()
        if pid is None:
            pid = _find_game_pid(kernel)

        rights = _PROCESS_VM_READ | _PROCESS_QUERY_LIMITED_INFORMATION
        handle = kernel.OpenProcess(rights, False, pid)
        if not handle:
            raise _process_error(f"OpenProcess for PID {pid}", ctypes.get_last_error())
        try:
            path = ctypes.create_unicode_buffer(32768)
            length = wintypes.DWORD(len(path))
            if not kernel.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(length)):
                raise _process_error(f"QueryFullProcessImageNameW for PID {pid}", ctypes.get_last_error())
            image = Path(path.value).name
            if image.casefold() != _PROCESS_NAME.casefold():
                raise ProcessError(f"PID {pid} runs {image}, expected {_PROCESS_NAME}.")

            module_base, module_path = _game_module(kernel, pid)
            return cls(kernel, handle, pid, module_base, module_path)
        except BaseException:
            kernel.CloseHandle(handle)
            raise

    def __enter__(self) -> ProcessMemory:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def close(self) -> None:
        if self._handle is not None:
            self._kernel.CloseHandle(self._handle)
            self._handle = None

    def is_running(self) -> bool:
        if self._handle is None:
            return False
        exit_code = wintypes.DWORD()
        if not self._kernel.GetExitCodeProcess(self._handle, ctypes.byref(exit_code)):
            raise _process_error(f"GetExitCodeProcess for PID {self.pid}", ctypes.get_last_error())
        return exit_code.value == _STILL_ACTIVE

    def read(self, address: int, size: int) -> bytes:
        if self._handle is None:
            raise ProcessError("The process handle is closed.")
        if not 0 <= address < _ADDRESS_LIMIT:
            raise ProcessError("Address must fit in a 64-bit pointer.")
        if not 0 <= size <= sys.maxsize or address + size > _ADDRESS_LIMIT:
            raise ProcessError("Read size or address range is invalid.")
        if size == 0:
            return b""

        buffer = ctypes.create_string_buffer(size)
        bytes_read = ctypes.c_size_t()
        if not self._kernel.ReadProcessMemory(
            self._handle, address, buffer, size, ctypes.byref(bytes_read)
        ):
            code = ctypes.get_last_error()
            raise _process_error(
                f"ReadProcessMemory for PID {self.pid} at {address:#x} "
                f"({bytes_read.value}/{size} bytes read)",
                code,
            )
        if bytes_read.value != size:
            raise ProcessError(
                f"Short read from PID {self.pid} at {address:#x}: {bytes_read.value}/{size} bytes."
            )
        return buffer.raw
