"""Read dependency inputs once, refusing links before reading any bytes."""

from __future__ import annotations

import errno
import os
import stat
import sys
from contextlib import ExitStack
from pathlib import Path

DEPENDENCY_NAMES = (
    ".python-version",
    "environment.yml",
    "environment.yaml",
    "uv.lock",
    "pyproject.toml",
    "requirements.txt",
)

MAX_DEPENDENCY_FILE_BYTES = 10 * 1024 * 1024  # 10 MiB


if sys.platform == "win32":

    def _windows_open(path: Path, root: Path) -> int:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel.CreateFileW
        create.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create.restype = wintypes.HANDLE
        # OPEN_REPARSE_POINT opens the link itself; it never opens its target.
        handle = create(str(path), 0x80000000, 7, None, 3, 0x00200000, None)
        if handle == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:

            class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("dwFileAttributes", wintypes.DWORD),
                    ("ftCreationTime", wintypes.FILETIME),
                    ("ftLastAccessTime", wintypes.FILETIME),
                    ("ftLastWriteTime", wintypes.FILETIME),
                    ("dwVolumeSerialNumber", wintypes.DWORD),
                    ("nFileSizeHigh", wintypes.DWORD),
                    ("nFileSizeLow", wintypes.DWORD),
                    ("nNumberOfLinks", wintypes.DWORD),
                    ("nFileIndexHigh", wintypes.DWORD),
                    ("nFileIndexLow", wintypes.DWORD),
                ]

            info = _BY_HANDLE_FILE_INFORMATION()
            get_info = kernel.GetFileInformationByHandle
            get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION)]
            get_info.restype = wintypes.BOOL
            if get_info(handle, ctypes.byref(info)) and (info.dwFileAttributes & 0x0400):
                raise OSError(
                    errno.ELOOP, f"Dependency path '{path.name}' is a symlink or reparse point"
                )

            final_path = kernel.GetFinalPathNameByHandleW
            final_path.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
            final_path.restype = wintypes.DWORD
            buffer = ctypes.create_unicode_buffer(32768)
            length = final_path(handle, buffer, len(buffer), 0)
            if not length or length >= len(buffer):
                raise ctypes.WinError(ctypes.get_last_error())
            final_name = buffer.value
            if final_name.startswith("\\\\?\\UNC\\"):
                final_name = "\\\\" + final_name[8:]
            else:
                final_name = final_name.removeprefix("\\\\?\\")
            resolved = Path(final_name)
            if not resolved.is_relative_to(root):
                raise OSError(errno.ELOOP, "Dependency path escapes the worktree")
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            close = kernel.CloseHandle
            close.argtypes = [wintypes.HANDLE]
            close.restype = wintypes.BOOL
            close(handle)
            raise
        return descriptor


def read_dependency_files(root: Path) -> dict[str, bytes]:
    """Snapshot only direct regular children, using the opened handles for validation and reads."""
    root = root.resolve(strict=True)
    files: dict[str, bytes] = {}
    with ExitStack() as handles:
        directory: int | None = None
        if sys.platform != "win32":
            directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            handles.callback(os.close, directory)
        for name in DEPENDENCY_NAMES:
            try:
                if sys.platform == "win32":
                    descriptor = _windows_open(root / name, root)
                else:
                    descriptor = os.open(
                        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
                    )
            except FileNotFoundError:
                continue
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise OSError(
                        f"Dependency file '{name}' is a symlink, which is refused"
                    ) from exc
                raise
            with os.fdopen(descriptor, "rb") as stream:
                metadata = os.fstat(stream.fileno())
                if not stat.S_ISREG(metadata.st_mode):
                    raise OSError(f"Dependency file '{name}' is not a regular file")
                if (
                    sys.platform == "win32"
                    and metadata.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
                ):
                    raise OSError(
                        f"Dependency file '{name}' is a symlink or reparse point, which is refused"
                    )
                if metadata.st_size > MAX_DEPENDENCY_FILE_BYTES:
                    raise OSError(
                        f"Dependency file '{name}' exceeds size limit of {MAX_DEPENDENCY_FILE_BYTES} bytes"
                    )
                data = stream.read(MAX_DEPENDENCY_FILE_BYTES + 1)
                if len(data) > MAX_DEPENDENCY_FILE_BYTES:
                    raise OSError(
                        f"Dependency file '{name}' exceeds size limit of {MAX_DEPENDENCY_FILE_BYTES} bytes"
                    )
                files[name] = data
    return files
