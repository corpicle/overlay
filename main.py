import ctypes
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

import os
import queue
import threading
import time
import tkinter as tk
import traceback
from ctypes import wintypes

import keyboard
import mss
MSS = getattr(mss, "MSS", None) or mss.mss
from PIL import Image, ImageEnhance, ImageTk

try:
    RESAMPLE = Image.Resampling.BILINEAR
except AttributeError:
    RESAMPLE = Image.BILINEAR

try:
    from windows_capture import WindowsCapture
    HAVE_WGC = True
except Exception as _e:
    HAVE_WGC = False
    print("windows-capture not available (", _e, ") - run: pip install windows-capture numpy")

HOTKEY_SELECT = "ctrl+shift+s"
HOTKEY_CLICKTHROUGH = "ctrl+shift+t"
HOTKEY_CLOSE_ALL = "ctrl+shift+x"
HOTKEY_QUIT = "ctrl+shift+q"

DEFAULT_ALPHA = 0.6
ALPHA_STEP = 0.03
DEFAULT_FPS = 20
WGC_TIMEOUT = 1.5

user32 = ctypes.windll.user32
dwmapi = ctypes.windll.dwmapi

user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long
user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
user32.SetWindowLongW.restype = ctypes.c_long
user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD,
                                         ctypes.c_void_p, wintypes.DWORD]

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
WS_EX_NOACTIVATE = 0x08000000
IGNORE_TITLES = {"Shell Handwriting Canvas", "Windows Input Experience", "Program Manager",
                 "Microsoft Text Input Application", "NVIDIA GeForce Overlay",
                 "Task Switching", "Task View", "Windows Shell Experience Host"}
WDA_EXCLUDEFROMCAPTURE = 0x00000011
DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14

MY_PID = os.getpid()
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

gdi32 = ctypes.windll.gdi32
_V = ctypes.c_void_p
user32.GetWindowDC.argtypes = [_V]
user32.GetWindowDC.restype = _V
user32.ReleaseDC.argtypes = [_V, _V]
user32.PrintWindow.argtypes = [_V, _V, wintypes.UINT]
gdi32.CreateCompatibleDC.argtypes = [_V]
gdi32.CreateCompatibleDC.restype = _V
gdi32.CreateCompatibleBitmap.argtypes = [_V, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = _V
gdi32.SelectObject.argtypes = [_V, _V]
gdi32.SelectObject.restype = _V
gdi32.DeleteObject.argtypes = [_V]
gdi32.DeleteDC.argtypes = [_V]
gdi32.GetDIBits.argtypes = [_V, _V, wintypes.UINT, wintypes.UINT, _V, _V, wintypes.UINT]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


def print_window(hwnd):
    r = full_rect(hwnd)
    w, h = r.right - r.left, r.bottom - r.top
    if w <= 0 or h <= 0:
        return None
    hdc = user32.GetWindowDC(hwnd)
    mdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    old = gdi32.SelectObject(mdc, bmp)
    try:
        user32.PrintWindow(hwnd, mdc, 2)
        bi = BITMAPINFOHEADER()
        bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.biWidth, bi.biHeight = w, -h
        bi.biPlanes, bi.biBitCount = 1, 32
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
        return Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)
    finally:
        gdi32.SelectObject(mdc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(hwnd, hdc)



def get_hwnd(win):
    return user32.GetAncestor(win.winfo_id(), 2)


def exclude_from_capture(win):
    try:
        user32.SetWindowDisplayAffinity(get_hwnd(win), WDA_EXCLUDEFROMCAPTURE)
    except Exception:
        pass


def set_click_through(win, enabled):
    hwnd = get_hwnd(win)
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    style |= WS_EX_LAYERED | WS_EX_TOOLWINDOW
    if enabled:
        style |= WS_EX_TRANSPARENT
    else:
        style &= ~WS_EX_TRANSPARENT
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)


def text_of(hwnd, fn):
    buf = ctypes.create_unicode_buffer(512)
    fn(hwnd, buf, 512)
    return buf.value


def visible_bounds(hwnd):
    r = wintypes.RECT()
    hr = dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS,
                                      ctypes.byref(r), ctypes.sizeof(r))
    if hr != 0:
        user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r


def full_rect(hwnd):
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r


def find_window_at(x, y):
    found = []

    def _cb(hwnd, _):
        try:
            if not hwnd or not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == MY_PID:
                return True
            cloaked = wintypes.DWORD()
            dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), 4)
            if cloaked.value:
                return True
            if text_of(hwnd, user32.GetClassNameW) in (
                    "Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
                return True
            title = text_of(hwnd, user32.GetWindowTextW)
            if not title or title in IGNORE_TITLES:
                return True
            ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if ex & (WS_EX_TRANSPARENT | WS_EX_NOACTIVATE):
                return True
            if (ex & WS_EX_TOOLWINDOW) and not (ex & WS_EX_APPWINDOW):
                return True
            r = visible_bounds(hwnd)
            if r.left <= x < r.right and r.top <= y < r.bottom:
                found.append(hwnd)
                return False
        except Exception:
            pass
        return True

    cb = WNDENUMPROC(_cb)
    user32.EnumWindows(cb, 0)
    return found[0] if found else None


class WGCGrabber:
    def __init__(self, title, hwnd=None):
        self.title = title
        self.hwnd = hwnd
        self.lock = threading.Lock()
        self.latest = None
        self.fresh = False
        self.ctl = None
        self.stopped = False
        self.closed = False
        self.error = None
        self.got_frame = False
        self.last_frame_time = None
        self.frame_count = 0
        self.name = title
        self._orig = None
        self._trick_tried = False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _option_sets(self):
        base = []
        targets = []
        if self.hwnd:
            targets.append({"window_hwnd": self.hwnd})
        targets.append({"window_name": self.title})
        for target in targets:
            for extra in ({"cursor_capture": False, "draw_border": False},
                          {"cursor_capture": False},
                          {"draw_border": False},
                          {}):
                d = dict(target)
                d.update(extra)
                base.append(d)
        return base

    def _apply_trick(self):
        if self._trick_tried or not self.hwnd:
            return
        self._trick_tried = True
        try:
            uniq = f"RegionPin-{MY_PID}-{int(time.time() * 1000)}"
            orig = text_of(self.hwnd, user32.GetWindowTextW)
            user32.SetWindowTextW(self.hwnd, uniq)
            if text_of(self.hwnd, user32.GetWindowTextW) == uniq:
                self._orig = orig
                self.name = uniq
                print("Targeting exact window via temporary unique title.")
            else:
                user32.SetWindowTextW(self.hwnd, orig)
        except Exception:
            pass

    def _restore_title(self):
        if self._orig is not None:
            try:
                user32.SetWindowTextW(self.hwnd, self._orig)
            except Exception:
                pass
            self._orig = None

    def _run(self):
        for opts in self._option_sets():
            if self.stopped:
                break
            if "window_name" in opts:
                self._apply_trick()
                opts["window_name"] = self.name
            try:
                cap = WindowsCapture(**opts)
            except TypeError:
                continue
            except Exception as e:
                self.error = e
                continue

            @cap.event
            def on_frame_arrived(frame, capture_control):
                if self.stopped:
                    capture_control.stop()
                    return
                arr = frame.frame_buffer.copy()
                if not self.got_frame:
                    self._restore_title()
                with self.lock:
                    self.latest = arr
                    self.fresh = True
                    self.got_frame = True
                    self.last_frame_time = time.time()
                    self.frame_count += 1

            @cap.event
            def on_closed():
                self.closed = True

            try:
                print(f"Starting capture with options: {opts}")
                cap.start()
                break
            except Exception as e:
                self.error = e
                print("Capture start failed:", repr(e))
                continue
        self._restore_title()

    def grab(self):
        with self.lock:
            if not self.fresh:
                return None
            self.fresh = False
            return self.latest

    def close(self):
        self.stopped = True
        self._restore_title()


class Pin:
    def __init__(self, app, region):
        self.app = app
        self.region = region
        self.scale = 1.0
        self.alpha = DEFAULT_ALPHA
        self.fps = DEFAULT_FPS
        self.click_through = False
        self.alive = True
        self._drag = (0, 0)
        self._photo = None
        self._size = None
        self._last = None
        self._redraw = False
        self._crop_ready = False
        self._black = 0
        self._t0 = time.time()
        self.last_capture_time = None
        self._last_status_log = 0.0
        self._last_logged_state = None

        self.grabber = None
        self.hwnd = None
        self.rel_vis = None
        self.rel_full = None

        hwnd = find_window_at(region["left"] + region["width"] // 2,
                              region["top"] + region["height"] // 2)
        title = text_of(hwnd, user32.GetWindowTextW) if hwnd else ""
        if hwnd and title and HAVE_WGC:
            self.hwnd = hwnd
            vb, fr = visible_bounds(hwnd), full_rect(hwnd)
            w, h = region["width"], region["height"]
            x, y = region["left"], region["top"]
            self.rel_vis = (x - vb.left, y - vb.top, x - vb.left + w, y - vb.top + h)
            self.rel_full = (x - fr.left, y - fr.top, x - fr.left + w, y - fr.top + h)
            self.vb_size = (vb.right - vb.left, vb.bottom - vb.top)
            self.fr_size = (fr.right - fr.left, fr.bottom - fr.top)
            self.rel = self.rel_vis
            self.grabber = WGCGrabber(title, hwnd)
            print(f"Pinned from window: {title!r} "
                  f"[class {text_of(hwnd, user32.GetClassNameW)!r}] (works while tabbed out)")
        else:
            print("Couldn't attach to a window - using plain screen capture "
                  "(it will NOT work while tabbed out).")
        self.mode = "window" if self.grabber else "screen"
        self.sct = MSS()

        self.win = tk.Toplevel(app.root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-alpha", self.alpha)
        self.win.configure(bg="black")

        self.label = tk.Label(self.win, bd=0, bg="black", cursor="fleur")
        self.label.pack()
        self.status = tk.Label(
            self.win, text="Waiting for first frame...", bd=0,
            bg="#171717", fg="#ffffff", font=("Consolas", 9),
            anchor="w", padx=5, pady=2
        )
        self.status.pack(fill="x")

        self.win.geometry(f"+{region['left'] + 40}+{region['top'] + 40}")
        self.win.update_idletasks()
        exclude_from_capture(self.win)
        set_click_through(self.win, False)

        for w in (self.win, self.label):
            w.bind("<ButtonPress-1>", self._press)
            w.bind("<B1-Motion>", self._motion)
            w.bind("<MouseWheel>", self._wheel)
            w.bind("<Button-3>", self._menu)
            w.bind("<Double-Button-1>", lambda e: self.close())

        self._tick()

    def _fallback(self):
        self.mode = "print" if (self.mode == "window" and self.hwnd) else "screen"
        self._black = 0
        self._redraw = True
        print(f"Switched capture mode to: {self.mode}")

    def _capture(self):
        if self.mode == "window" and self.grabber:
            arr = self.grabber.grab()
            if arr is None:
                now = time.time()
                if not self.grabber.got_frame and now - self._t0 > WGC_TIMEOUT:
                    print("No frames from window capture"
                          + (f" ({self.grabber.error!r})" if self.grabber.error else ""))
                    self._fallback()
                elif (self.grabber.last_frame_time is not None
                      and now - self.grabber.last_frame_time > WGC_TIMEOUT):
                    print(f"Window capture has delivered no new frame for {now - self.grabber.last_frame_time:.1f}s; trying fallback.")
                    self._fallback()
                return None
            h, w = arr.shape[:2]
            if not self._crop_ready:
                self._crop_ready = True
                if (w, h) == self.vb_size:
                    self.rel = self.rel_vis
                elif (w, h) == self.fr_size:
                    self.rel = self.rel_full
                else:
                    self.rel = self.rel_vis
                print(f"First frame: {w}x{h}, crop box {self.rel}, "
                      f"window size {self.vb_size} / {self.fr_size}")
                near = lambda a, b: abs(a - b) <= 16
                if not ((near(w, self.vb_size[0]) and near(h, self.vb_size[1])) or
                        (near(w, self.fr_size[0]) and near(h, self.fr_size[1]))):
                    print(f"Frame {w}x{h} doesn't match the window - probably the wrong "
                          "window was hooked.")
                    self._fallback()
                    return None
            x1, y1, x2, y2 = self.rel
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(w, x2), min(h, y2)
            if x2 <= x1 or y2 <= y1:
                return None
            crop = arr[y1:y2, x1:x2, :3]
            if crop.max() == 0:
                self._black += 1
                if self._black >= 8:
                    print("Window capture returns black frames.")
                    self._fallback()
                return None
            self._black = 0
            return Image.fromarray(crop[:, :, ::-1].copy(), "RGB")

        if self.mode == "print" and self.hwnd:
            img = print_window(self.hwnd)
            if img is None:
                return None
            x1, y1, x2, y2 = self.rel_full
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(img.width, x2), min(img.height, y2)
            if x2 <= x1 or y2 <= y1:
                return None
            img = img.crop((x1, y1, x2, y2))
            if not any(mx for _, mx in img.getextrema()):
                self._black += 1
                if self._black >= 8:
                    print("PrintWindow also returns black.")
                    self._fallback()
                return None
            self._black = 0
            return img

        shot = self.sct.grab(self.region)
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    def _tick(self):
        if not self.alive:
            return
        try:
            new = self._capture()
            if new is not None:
                self._last = new
                self.last_capture_time = time.time()
            if self._last is not None and (new is not None or self._redraw):
                self._redraw = False
                img = self._last
                if self.scale != 1.0:
                    size = (max(1, int(img.width * self.scale)),
                            max(1, int(img.height * self.scale)))
                    img = img.resize(size, RESAMPLE)
                self._photo = ImageTk.PhotoImage(img)
                self.label.configure(image=self._photo)
                if self._size != img.size:
                    self._size = img.size
                    self.win.geometry(f"{img.width}x{img.height + self.status.winfo_reqheight()}")
            self._update_status()
        except Exception:
            traceback.print_exc()
        if self.alive:
            self.win.after(max(1, int(1000 / self.fps)), self._tick)

    def _update_status(self):
        now = time.time()
        capture_age = (now - self.last_capture_time) if self.last_capture_time else None
        if self.mode == "window" and self.grabber:
            frame_age = (now - self.grabber.last_frame_time) if self.grabber.last_frame_time else None
            count = self.grabber.frame_count
            if frame_age is None:
                detail = "WGC waiting for first frame"
                stale = True
            elif frame_age > 1.5:
                detail = f"WGC STALE {frame_age:.1f}s | frames={count}"
                stale = True
            else:
                detail = f"WGC LIVE {frame_age:.1f}s | frames={count}"
                stale = False
        else:
            age_text = "never" if capture_age is None else f"{capture_age:.1f}s ago"
            detail = f"{self.mode.upper()} | last capture: {age_text}"
            stale = capture_age is None or capture_age > 1.5

        if stale:
            self.status.configure(text="[STALE] " + detail, fg="#ffb454")
        else:
            self.status.configure(text="[LIVE]  " + detail, fg="#8dff9b")

        state = (self.mode, stale, detail.split("|")[0])
        if state != self._last_logged_state or now - self._last_status_log >= 5:
            print(f"[RegionPin] {detail}")
            self._last_logged_state = state
            self._last_status_log = now

    def _press(self, e):
        self._drag = (e.x_root - self.win.winfo_x(), e.y_root - self.win.winfo_y())

    def _motion(self, e):
        self.win.geometry(f"+{e.x_root - self._drag[0]}+{e.y_root - self._drag[1]}")

    def _wheel(self, e):
        step = 1 if e.delta > 0 else -1
        if e.state & 0x4:
            self.set_scale(self.scale + 0.1 * step)
        else:
            self.set_alpha(self.alpha + ALPHA_STEP * step)

    def set_alpha(self, a):
        self.alpha = min(1.0, max(0.1, a))
        self.win.attributes("-alpha", self.alpha)

    def set_scale(self, s):
        self.scale = min(4.0, max(0.25, s))
        self._redraw = True

    def set_fps(self, f):
        self.fps = f

    def set_click_through(self, enabled):
        self.click_through = enabled
        set_click_through(self.win, enabled)

    def _menu(self, e):
        m = tk.Menu(self.win, tearoff=0)

        opacity = tk.Menu(m, tearoff=0)
        for pct in (20, 30, 40, 50, 60, 70, 80, 90, 100):
            opacity.add_command(label=f"{pct}%", command=lambda p=pct: self.set_alpha(p / 100))
        m.add_cascade(label="Opacity", menu=opacity)

        scale = tk.Menu(m, tearoff=0)
        for pct in (50, 75, 100, 125, 150, 200, 300):
            scale.add_command(label=f"{pct}%", command=lambda p=pct: self.set_scale(p / 100))
        m.add_cascade(label="Scale", menu=scale)

        fps = tk.Menu(m, tearoff=0)
        for f in (5, 10, 20, 30, 60):
            fps.add_command(label=f"{f} FPS", command=lambda f=f: self.set_fps(f))
        m.add_cascade(label="Refresh rate", menu=fps)

        m.add_separator()
        m.add_command(label="Enable click-through (toggle back with Ctrl+Shift+T)",
                      command=lambda: self.set_click_through(True))
        m.add_separator()
        m.add_command(label="Close pin", command=self.close)
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def close(self):
        if not self.alive:
            return
        self.alive = False
        try:
            self.sct.close()
        except Exception:
            pass
        try:
            if self.grabber:
                self.grabber.close()
        except Exception:
            pass
        try:
            self.win.destroy()
        except Exception:
            pass
        if self in self.app.pins:
            self.app.pins.remove(self)


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.pins = []
        self.q = queue.Queue()
        self.selecting = False

        keyboard.add_hotkey(HOTKEY_SELECT, lambda: self.q.put("select"))
        keyboard.add_hotkey(HOTKEY_CLICKTHROUGH, lambda: self.q.put("clickthrough"))
        keyboard.add_hotkey(HOTKEY_CLOSE_ALL, lambda: self.q.put("closeall"))
        keyboard.add_hotkey(HOTKEY_QUIT, lambda: self.q.put("quit"))

        self.root.after(50, self._poll)

    def _poll(self):
        try:
            while True:
                cmd = self.q.get_nowait()
                try:
                    if cmd == "select" and not self.selecting:
                        self.select_region()
                    elif cmd == "clickthrough":
                        new = not (self.pins and self.pins[0].click_through)
                        for p in self.pins:
                            p.set_click_through(new)
                    elif cmd == "closeall":
                        for p in list(self.pins):
                            p.close()
                    elif cmd == "quit":
                        for p in list(self.pins):
                            p.close()
                        self.root.quit()
                        return
                except Exception:
                    self.selecting = False
                    traceback.print_exc()
        except queue.Empty:
            pass
        self.root.after(50, self._poll)

    def _make_pin(self, region):
        try:
            self.pins.append(Pin(self, region))
        except Exception:
            traceback.print_exc()

    def select_region(self):
        self.selecting = True
        with MSS() as sct:
            mon = sct.monitors[0]
            shot = sct.grab(mon)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        dimmed = ImageEnhance.Brightness(img).enhance(0.45)

        top = tk.Toplevel(self.root)
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        top.geometry(f"{mon['width']}x{mon['height']}{mon['left']:+d}{mon['top']:+d}")

        canvas = tk.Canvas(top, highlightthickness=0, cursor="crosshair")
        canvas.pack(fill="both", expand=True)
        photo = ImageTk.PhotoImage(dimmed)
        canvas.create_image(0, 0, image=photo, anchor="nw")
        canvas.image = photo

        state = {"x": 0, "y": 0, "rect": None}

        def finish():
            try:
                top.destroy()
            finally:
                self.selecting = False

        def press(e):
            state["x"], state["y"] = e.x, e.y
            if state["rect"]:
                canvas.delete(state["rect"])
            state["rect"] = canvas.create_rectangle(
                e.x, e.y, e.x, e.y, outline="#5865F2", width=2)

        def drag(e):
            if state["rect"]:
                canvas.coords(state["rect"], state["x"], state["y"], e.x, e.y)

        def release(e):
            x1, x2 = sorted((state["x"], e.x))
            y1, y2 = sorted((state["y"], e.y))
            finish()
            if x2 - x1 < 5 or y2 - y1 < 5:
                return
            region = {"left": mon["left"] + x1, "top": mon["top"] + y1,
                      "width": x2 - x1, "height": y2 - y1}
            self.root.after(120, lambda: self._make_pin(region))

        canvas.bind("<ButtonPress-1>", press)
        canvas.bind("<B1-Motion>", drag)
        canvas.bind("<ButtonRelease-1>", release)
        top.bind("<Escape>", lambda e: finish())
        top.focus_force()

    def run(self):
        print("Region Pin running.")
        print(f"  {HOTKEY_SELECT}: select region | {HOTKEY_CLICKTHROUGH}: click-through "
              f"| {HOTKEY_CLOSE_ALL}: close all | {HOTKEY_QUIT}: quit")
        self.root.mainloop()
        keyboard.unhook_all()
        os._exit(0)


if __name__ == "__main__":
    App().run() 