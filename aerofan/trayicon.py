"""
The Win32 side of the tray icon: a hidden window, a notification icon, a menu.

There is no framework here on purpose. pystray would bring Pillow, Pillow
brings a compiled wheel, and the whole point of the install script is that it
works on a friend's laptop with nothing on it. The shell API for this is small
enough to call directly.

THE HIDDEN WINDOW

A notification icon has no window of its own; it borrows one to deliver its
messages to. That window is created and never shown. It cannot be a
message-only window, tempting as that is - a message-only window cannot become
the foreground window, and a popup menu belonging to a window that is not
foreground stays on screen after you click away from it. So: a normal
overlapped window that is simply never made visible.

VERSION 4

NIM_SETVERSION with NOTIFYICON_VERSION_4 is what allows a tooltip longer than
64 characters, and we need about 100 for two temperatures and two fans. It also
changes how the callback packs its arguments, which is why the notification
code comes out of the low word of lParam here rather than out of wParam.

TASKBARCREATED

If Explorer restarts - it does - every notification icon on the machine is
gone and each application is expected to add its own back. Windows broadcasts
a registered message to say so, and the four lines that listen for it are the
difference between surviving that and quietly vanishing until the next login.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import threading

from .icons import destroy_icon, make_icon

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = wt.LPARAM
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_COMMAND = 0x0111
WM_APP = 0x8000
WM_CONTEXTMENU = 0x007B
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_USER = 0x0400
NIN_SELECT = WM_USER + 0
NIN_KEYSELECT = WM_USER + 1

WM_TRAYICON = WM_APP + 1
WM_REFRESH = WM_APP + 2
WM_QUIT_APP = WM_APP + 3

# Menu command identifiers. Assigned per menu from the item's position, so the
# same item always gets the same number within one display and no state has to
# survive between displays.
FIRST_COMMAND_ID = 1000

# Names for the notification codes, for the log. Reading "WM_RBUTTONUP then
# WM_CONTEXTMENU for one click" in a log file is the whole reason this exists.
NOTIFICATION_NAMES = {
    0x0200: "WM_MOUSEMOVE", 0x0201: "WM_LBUTTONDOWN", 0x0202: "WM_LBUTTONUP",
    0x0203: "WM_LBUTTONDBLCLK", 0x0204: "WM_RBUTTONDOWN",
    0x0205: "WM_RBUTTONUP", 0x0206: "WM_RBUTTONDBLCLK",
    0x007B: "WM_CONTEXTMENU", 0x0400: "NIN_SELECT", 0x0401: "NIN_KEYSELECT",
    0x0402: "NIN_BALLOONSHOW", 0x0406: "NIN_POPUPOPEN",
    0x0407: "NIN_POPUPCLOSE",
}

NIM_ADD = 0
NIM_MODIFY = 1
NIM_DELETE = 2
NIM_SETVERSION = 4

NIF_MESSAGE = 0x01
NIF_ICON = 0x02
NIF_TIP = 0x04
NIF_SHOWTIP = 0x80

NOTIFYICON_VERSION_4 = 4

MF_STRING = 0x0000
MF_GRAYED = 0x0001
MF_DISABLED = 0x0002
MF_CHECKED = 0x0008
MF_SEPARATOR = 0x0800

TPM_LEFTALIGN = 0x0000
TPM_RIGHTBUTTON = 0x0002
TPM_NONOTIFY = 0x0080
TPM_RETURNCMD = 0x0100

IDC_ARROW = 32512
CW_USEDEFAULT = -0x80000000
TIP_CHARS = 128


class WNDCLASSEX(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("style", wt.UINT),
                ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE),
                ("hIcon", wt.HICON), ("hCursor", wt.HANDLE),
                ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR), ("hIconSm", wt.HICON)]


class NOTIFYICONDATA(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT),
                ("uFlags", wt.UINT), ("uCallbackMessage", wt.UINT),
                ("hIcon", wt.HICON), ("szTip", wt.WCHAR * TIP_CHARS),
                ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD),
                ("szInfo", wt.WCHAR * 256), ("uVersion", wt.UINT),
                ("szInfoTitle", wt.WCHAR * 64), ("dwInfoFlags", wt.DWORD),
                ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wt.HICON)]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", wt.HWND), ("message", wt.UINT),
                ("wParam", wt.WPARAM), ("lParam", wt.LPARAM),
                ("time", wt.DWORD), ("pt", POINT)]


user32.CreateWindowExW.restype = wt.HWND
user32.CreateWindowExW.argtypes = [
    wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE,
    ctypes.c_void_p]
user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.CreatePopupMenu.restype = wt.HMENU
user32.AppendMenuW.argtypes = [wt.HMENU, wt.UINT, ctypes.c_void_p, wt.LPCWSTR]
user32.TrackPopupMenu.restype = ctypes.c_int
user32.TrackPopupMenu.argtypes = [
    wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND,
    ctypes.c_void_p]
user32.DestroyMenu.argtypes = [wt.HMENU]
user32.LoadCursorW.restype = wt.HANDLE
user32.LoadCursorW.argtypes = [wt.HINSTANCE, ctypes.c_void_p]
user32.RegisterWindowMessageW.restype = wt.UINT
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.DestroyWindow.argtypes = [wt.HWND]
shell32.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(NOTIFYICONDATA)]
kernel32.GetModuleHandleW.restype = wt.HMODULE
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]


class MenuItem:
    """One line of the popup. `identifier` of None makes it unclickable."""

    __slots__ = ("identifier", "text", "checked", "enabled", "separator")

    def __init__(self, identifier=None, text: str = "", checked: bool = False,
                 enabled: bool = True, separator: bool = False):
        self.identifier = identifier
        self.text = text
        self.checked = checked
        self.enabled = enabled
        self.separator = separator


def separator() -> MenuItem:
    return MenuItem(separator=True)


class TrayIcon:
    """
    A notification icon and its menu.

    ``run()`` blocks on the message loop and must be called from the main
    thread. Everything else here is safe to call from anywhere: state goes
    under the lock and the window is poked with PostMessage, so the shell API
    is only ever touched from the thread that owns the window.
    """

    CLASS_NAME = "AeroFanTrayWindow"

    def __init__(self, build_menu, on_command, tooltip: str = "aerofan",
                 colour: tuple[int, int, int] = (122, 132, 145), log=None):
        self.build_menu = build_menu
        self.on_command = on_command
        self.log = log or logging.getLogger("aerofan.tray")
        self._lock = threading.Lock()
        self._tooltip = tooltip
        self._colour = colour
        self._applied: tuple | None = None
        self.hwnd = None
        self._hicon = None
        self._data = None
        self._added = False
        self._menu_open = False
        self._wndproc = WNDPROC(self._on_message)
        self._taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")

    # -- called from other threads -------------------------------------------

    def update(self, colour: tuple[int, int, int], tooltip: str) -> None:
        with self._lock:
            self._tooltip = tooltip
            self._colour = colour
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_REFRESH, 0, 0)

    def quit(self) -> None:
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_QUIT_APP, 0, 0)

    # -- window --------------------------------------------------------------

    def _register_class(self) -> None:
        instance = kernel32.GetModuleHandleW(None)
        window_class = WNDCLASSEX()
        window_class.cbSize = ctypes.sizeof(WNDCLASSEX)
        window_class.style = 0
        window_class.lpfnWndProc = self._wndproc
        window_class.hInstance = instance
        window_class.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(IDC_ARROW))
        window_class.lpszClassName = self.CLASS_NAME
        if not user32.RegisterClassExW(ctypes.byref(window_class)):
            error = ctypes.get_last_error()
            if error != 1410:  # already registered by an earlier run
                raise ctypes.WinError(error)
        self._class = window_class  # keep the WNDPROC referenced
        self.hwnd = user32.CreateWindowExW(
            0, self.CLASS_NAME, "aerofan", 0, CW_USEDEFAULT, CW_USEDEFAULT,
            0, 0, None, None, instance, None)
        if not self.hwnd:
            raise ctypes.WinError(ctypes.get_last_error())

    def _notify_data(self) -> NOTIFYICONDATA:
        data = NOTIFYICONDATA()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATA)
        data.hWnd = self.hwnd
        data.uID = 1
        return data

    def _add_icon(self) -> None:
        with self._lock:
            colour, tooltip = self._colour, self._tooltip
        self._hicon = make_icon(colour)
        data = self._notify_data()
        data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP
        data.uCallbackMessage = WM_TRAYICON
        data.hIcon = self._hicon
        data.szTip = tooltip[:TIP_CHARS - 1]
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data))
        version = self._notify_data()
        version.uVersion = NOTIFYICON_VERSION_4
        shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(version))
        self._added = True
        self._applied = (colour, tooltip)

    def _refresh_icon(self) -> None:
        if not self._added:
            return
        with self._lock:
            colour, tooltip = self._colour, self._tooltip
        if self._applied == (colour, tooltip):
            return
        data = self._notify_data()
        data.uFlags = NIF_TIP | NIF_SHOWTIP
        data.szTip = tooltip[:TIP_CHARS - 1]
        if self._applied is None or colour != self._applied[0]:
            new_icon = make_icon(colour)
            data.uFlags |= NIF_ICON
            data.hIcon = new_icon
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data))
            destroy_icon(self._hicon)
            self._hicon = new_icon
        else:
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data))
        self._applied = (colour, tooltip)

    def _remove_icon(self) -> None:
        if self._added:
            data = self._notify_data()
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(data))
            self._added = False
        if self._hicon:
            destroy_icon(self._hicon)
            self._hicon = None

    # -- menu ----------------------------------------------------------------

    def _show_menu(self) -> None:
        """
        Show the menu, once.

        TrackPopupMenu is modal and pumps messages itself, so this window goes
        on receiving notifications - and this method goes on being called -
        for as long as a menu is on screen. Re-entering it while a menu is up
        is never wanted, and used to be actively harmful: the inner call reset
        the identifier-to-action map that the outer call was still going to
        look its result up in, so the outer click resolved to nothing and the
        menu appeared to do nothing at all.

        The map is a local now, so re-entry could not corrupt it even if it
        happened, and this guard means it does not happen.
        """
        if self._menu_open:
            self.log.debug("menu already open; ignoring the second request")
            return
        self._menu_open = True
        try:
            self._popup()
        finally:
            self._menu_open = False

    def _popup(self) -> None:
        items = self.build_menu()
        menu = user32.CreatePopupMenu()
        if not menu:
            self.log.error("CreatePopupMenu failed: %s",
                           ctypes.WinError(ctypes.get_last_error()))
            return

        # Local, and keyed on position: unique within this menu, reproducible,
        # and gone when it returns. Nothing to get out of step with anything.
        commands: dict[int, object] = {}
        chosen = 0
        try:
            for index, item in enumerate(items):
                if item.separator:
                    user32.AppendMenuW(menu, MF_SEPARATOR, None, None)
                    continue
                identifier = FIRST_COMMAND_ID + index
                flags = MF_STRING
                if item.checked:
                    flags |= MF_CHECKED
                if not item.enabled or item.identifier is None:
                    flags |= MF_GRAYED | MF_DISABLED
                if not user32.AppendMenuW(menu, flags,
                                          ctypes.c_void_p(identifier),
                                          item.text):
                    self.log.error("AppendMenuW failed for %r: %s", item.text,
                                   ctypes.WinError(ctypes.get_last_error()))
                commands[identifier] = item.identifier

            point = POINT()
            user32.GetCursorPos(ctypes.byref(point))
            # Required, and in this order: the menu belongs to a window nobody
            # can see, and without the foreground call it would not dismiss
            # when you click elsewhere. The trailing null message is the
            # documented companion to that.
            user32.SetForegroundWindow(self.hwnd)
            chosen = user32.TrackPopupMenu(
                menu, TPM_LEFTALIGN | TPM_RIGHTBUTTON | TPM_RETURNCMD
                | TPM_NONOTIFY, point.x, point.y, 0, self.hwnd, None)
            user32.PostMessageW(self.hwnd, 0, 0, 0)
        finally:
            user32.DestroyMenu(menu)

        action = commands.get(chosen)
        self.log.info("menu closed: id=%s -> %r", chosen, action)
        if action is not None:
            self.on_command(action)

    # -- message loop --------------------------------------------------------

    def _on_message(self, hwnd, message, wparam, lparam):
        try:
            return self._dispatch(hwnd, message, wparam, lparam)
        except Exception:
            # A window procedure is a C callback. If this raises, ctypes
            # writes the traceback to stderr - which under pythonw.exe is
            # None - and returns 0, so the failure is completely invisible and
            # the window carries on in a state nobody can account for. Log it
            # and keep the window alive.
            self.log.exception("window procedure failed on message 0x%04X",
                               message)
            return 0

    def _dispatch(self, hwnd, message, wparam, lparam):
        if message == WM_TRAYICON:
            event = lparam & 0xFFFF
            self.log.debug("tray notification %s (0x%04X)",
                           NOTIFICATION_NAMES.get(event, "?"), event)
            # Version 4 delivers WM_CONTEXTMENU for a right click and
            # NIN_SELECT for a left one - and ALSO delivers the raw button
            # messages for the same click. Acting on both halves of a pair
            # opened the menu twice per click, which is the bug the guard in
            # _show_menu describes. Listen only to the version 4 notifications.
            if event in (WM_CONTEXTMENU, NIN_SELECT, NIN_KEYSELECT):
                self.log.info("opening the menu (%s)",
                              NOTIFICATION_NAMES.get(event, event))
                self._show_menu()
            return 0
        if message == WM_REFRESH:
            try:
                self._refresh_icon()
            except OSError:
                pass
            return 0
        if message == WM_QUIT_APP:
            user32.DestroyWindow(hwnd)
            return 0
        if message == self._taskbar_created:
            # Explorer restarted and took every tray icon with it.
            self._added = False
            self._applied = None
            self._add_icon()
            return 0
        if message == WM_CLOSE:
            user32.DestroyWindow(hwnd)
            return 0
        if message == WM_DESTROY:
            self._remove_icon()
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def run(self) -> int:
        self._register_class()
        self._add_icon()
        message = MSG()
        while True:
            result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
            if result in (0, -1):
                break
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))
        self._remove_icon()
        return 0
