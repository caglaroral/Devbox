"""Minimal Linux PAM authentication via ctypes (no third-party dependency).

authenticate(user, password, service) -> (ok, message)

Note: when the console does not run as root, pam_unix can only verify the password of the
user the process runs as (it uses the setgid helper unix_chkpwd).
"""

from __future__ import annotations

import ctypes
import ctypes.util
from ctypes import CFUNCTYPE, POINTER, Structure, byref, c_char_p, c_int, c_size_t, c_void_p, cast, sizeof

PAM_PROMPT_ECHO_OFF = 1
PAM_PROMPT_ECHO_ON = 2
PAM_ERROR_MSG = 3
PAM_TEXT_INFO = 4
PAM_SUCCESS = 0
PAM_DISALLOW_NULL_AUTHTOK = 0x0001


class PamHandle(Structure):
    _fields_ = [("handle", c_void_p)]


class PamMessage(Structure):
    _fields_ = [("msg_style", c_int), ("msg", c_char_p)]


class PamResponse(Structure):
    _fields_ = [("resp", c_void_p), ("resp_retcode", c_int)]


CONV_FUNC = CFUNCTYPE(c_int, c_int, POINTER(POINTER(PamMessage)), POINTER(POINTER(PamResponse)), c_void_p)


class PamConv(Structure):
    _fields_ = [("conv", CONV_FUNC), ("appdata_ptr", c_void_p)]


_libc = ctypes.CDLL(ctypes.util.find_library("c"))
_libc.calloc.restype = c_void_p
_libc.calloc.argtypes = [c_size_t, c_size_t]
_libc.strdup.restype = c_void_p
_libc.strdup.argtypes = [c_char_p]

_pam = ctypes.CDLL(ctypes.util.find_library("pam") or "libpam.so.0")
_pam.pam_start.restype = c_int
_pam.pam_start.argtypes = [c_char_p, c_char_p, POINTER(PamConv), POINTER(PamHandle)]
_pam.pam_authenticate.restype = c_int
_pam.pam_authenticate.argtypes = [PamHandle, c_int]
_pam.pam_acct_mgmt.restype = c_int
_pam.pam_acct_mgmt.argtypes = [PamHandle, c_int]
_pam.pam_end.restype = c_int
_pam.pam_end.argtypes = [PamHandle, c_int]
_pam.pam_strerror.restype = c_char_p
_pam.pam_strerror.argtypes = [PamHandle, c_int]


def authenticate(user: str, password: str, service: str = "login") -> tuple[bool, str]:
    """Blocking. Run in a thread (pam_unix sleeps ~2 s on failure)."""
    pw = password.encode()

    @CONV_FUNC
    def conv(n, msgs, resp, _data):
        arr = _libc.calloc(n, sizeof(PamResponse))
        if not arr:
            return 5  # PAM_BUF_ERR
        resp[0] = cast(arr, POINTER(PamResponse))
        for i in range(n):
            style = msgs[i].contents.msg_style
            if style == PAM_PROMPT_ECHO_OFF:
                resp[0][i].resp = _libc.strdup(pw)
            elif style == PAM_PROMPT_ECHO_ON:
                resp[0][i].resp = _libc.strdup(user.encode())
        return PAM_SUCCESS

    handle = PamHandle()
    pconv = PamConv(conv, None)
    rc = _pam.pam_start(service.encode(), user.encode(), byref(pconv), byref(handle))
    if rc != PAM_SUCCESS:
        return False, "pam_start failed"
    try:
        rc = _pam.pam_authenticate(handle, PAM_DISALLOW_NULL_AUTHTOK)
        if rc == PAM_SUCCESS:
            rc = _pam.pam_acct_mgmt(handle, 0)
        msg = (_pam.pam_strerror(handle, rc) or b"").decode(errors="replace")
        return rc == PAM_SUCCESS, msg
    finally:
        _pam.pam_end(handle, rc)
