"""Windows current-user DPAPI credential pools, with secret-free failures."""
import ctypes
import json
import os
from pathlib import Path


def _decrypt(ciphertext):
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    if not isinstance(ciphertext, str) or not ciphertext or any(c not in "0123456789abcdefABCDEF" for c in ciphertext):
        raise ValueError()
    encrypted = bytes.fromhex(ciphertext)
    source_buffer = (ctypes.c_ubyte * len(encrypted)).from_buffer_copy(encrypted)
    source = Blob(len(encrypted), source_buffer)
    target = Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    unprotect = crypt32.CryptUnprotectData
    unprotect.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    unprotect.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    try:
        # UI forbidden; decrypt in the current Windows user's security context.
        if not unprotect(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
            raise ValueError()
        plaintext = ctypes.string_at(target.data, target.size).decode("utf-16-le")
        if not plaintext.strip():
            raise ValueError()
        return plaintext.strip()
    finally:
        if target.data:
            ctypes.memset(target.data, 0, target.size)
            kernel32.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))


def load_encrypted_key_pool(path):
    """Return distinct keys from PowerShell ConvertFrom-SecureString hex blobs.

    No alternate cipher, machine credential context, network access, logging,
    plaintext persistence or credential hashing is used.
    """
    result = None
    try:
        if os.name != "nt":
            raise ValueError()
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        ciphertexts = value["ciphertexts"]
        if not isinstance(ciphertexts, list) or not ciphertexts:
            raise ValueError()
        keys = []
        for ciphertext in ciphertexts:
            key = _decrypt(ciphertext)
            if key not in keys:
                keys.append(key)
        result = keys
    except Exception:
        # Raise outside the except suite: even exception context cannot expose
        # ciphertext/plaintext from JSON, Unicode or platform failures.
        pass
    if result is None:
        raise RuntimeError("Unable to load encrypted credential pool")
    return result
