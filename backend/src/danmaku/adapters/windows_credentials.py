import ctypes
from ctypes import wintypes as win
from uuid import uuid4

from pydantic import SecretStr

from danmaku.domain.workbench import WorkbenchError


class Credential(ctypes.Structure):
    _fields_ = [
        ("Flags", win.DWORD), ("Type", win.DWORD), ("TargetName", win.LPWSTR),
        ("Comment", win.LPWSTR), ("LastWritten", win.FILETIME),
        ("CredentialBlobSize", win.DWORD), ("CredentialBlob", ctypes.c_void_p),
        ("Persist", win.DWORD), ("AttributeCount", win.DWORD), ("Attributes", ctypes.c_void_p),
        ("TargetAlias", win.LPWSTR), ("UserName", win.LPWSTR),
    ]


GENERIC = 1
PERSIST_LOCAL_MACHINE = 2
NOT_FOUND = 1168


class WindowsCredentialStore:
    def __init__(self, scope: str = "LLM") -> None:
        self.scope = scope
        native = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        pointer = ctypes.POINTER(Credential)
        self._read = native.CredReadW
        self._read.argtypes = [win.LPCWSTR, win.DWORD, win.DWORD, ctypes.POINTER(pointer)]
        self._read.restype = win.BOOL
        self._write = native.CredWriteW
        self._write.argtypes = [pointer, win.DWORD]
        self._write.restype = win.BOOL
        self._delete = native.CredDeleteW
        self._delete.argtypes = [win.LPCWSTR, win.DWORD, win.DWORD]
        self._delete.restype = win.BOOL
        self._free = native.CredFree
        self._free.argtypes = [ctypes.c_void_p]
        self._free.restype = None

    def _target(self, reference: str) -> str:
        return f"DanmakuMigration/{self.scope}/{reference}"

    def _unavailable(self) -> WorkbenchError:
        return WorkbenchError(503, "credential_store_unavailable", "Windows 凭据库暂不可用", True)

    def put(self, secret: SecretStr) -> str:
        reference = uuid4().hex
        data = secret.get_secret_value().encode("utf-8")
        blob = ctypes.create_string_buffer(data)
        credential = Credential(
            Type=GENERIC, TargetName=self._target(reference), CredentialBlobSize=len(data),
            CredentialBlob=ctypes.addressof(blob), Persist=PERSIST_LOCAL_MACHINE,
        )
        try:
            if not self._write(ctypes.byref(credential), 0):
                raise self._unavailable()
        finally:
            ctypes.memset(ctypes.addressof(blob), 0, len(blob))
        return reference

    def get(self, reference: str) -> SecretStr | None:
        credential = ctypes.POINTER(Credential)()
        if not self._read(self._target(reference), GENERIC, 0, ctypes.byref(credential)):
            if ctypes.get_last_error() == NOT_FOUND:
                return None
            raise self._unavailable()
        try:
            value = credential.contents
            return SecretStr(ctypes.string_at(value.CredentialBlob, value.CredentialBlobSize).decode("utf-8"))
        except UnicodeError:
            raise self._unavailable() from None
        finally:
            self._free(credential)

    def delete(self, reference: str) -> None:
        if not self._delete(self._target(reference), GENERIC, 0) and ctypes.get_last_error() != NOT_FOUND:
            raise self._unavailable()
