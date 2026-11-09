# -*- coding: utf-8 -*-
"""
โปรแกรมตีบวก ดาบ / เท้า / เกาะ  (เสริมพลังยุทธภัณฑ์ - NPC โล้น)
หาไอเทมในกระเป๋าอัตโนมัติจากรูปไอคอนในโฟลเดอร์ icons/
"""
import ctypes
import json
import os
import queue
import subprocess
import tempfile
import threading
import time
import tkinter as tk
from tkinter import ttk

try:  # ให้พิกัดเมาส์/ภาพหน้าจอตรงกันเมื่อ Windows scale > 100%
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

import cv2
import numpy as np
from PIL import ImageGrab
from pynput import keyboard, mouse

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE, "upgrade_config.json")
ICON_DIR = os.path.join(BASE, "icons")

ITEM_TYPES = [  # key, ชื่อ, ไฟล์ไอคอน
    ("sword", "ดาบ", "sword.png"),
    ("boots", "เท้า", "boots.png"),
    ("armor", "เกาะ", "armor.png"),
]

POINTS = [  # key, ชื่อ, ปุ่มลัด
    ("npc", "NPC โล้น", "F1"),
    ("stone", "หินแดง", "F2"),
    ("charm", "ยันต์", "F3"),
    ("ok", "ปุ่มตกลง", "F4"),
    ("reg1", "มุมซ้ายบนกระเป๋า", "F5"),
    ("reg2", "มุมขวาล่างกระเป๋า", "F6"),
    ("menu", "เมนู เสริมพลัง(ใหม่)", "F7"),
    ("bag_target", "จุดวางกระเป๋า (หัวข้อ)", "F8"),
]
HOTKEY_TO_POINT = {getattr(keyboard.Key, hk.lower()): k for k, _, hk in POINTS}

DEFAULT = {
    "npc": None, "stone": None, "charm": None, "ok": None, "reg1": None, "reg2": None, "menu": None, "bag_target": None,
    "types": {k: {"enabled": True, "times": 1} for k, _, _ in ITEM_TYPES},
    "use_charm": True,
    "click_npc_every_time": False,
    "delay_click": 0.25,
    "delay_after_npc": 1.0,
    "delay_after_ok": 2.0,
    "threshold": 0.85,
}

mc = mouse.Controller()
kc = keyboard.Controller()

# ---- SendInput ตรงๆ ผ่าน ctypes
_u32 = ctypes.windll.user32
_PUL = ctypes.POINTER(ctypes.c_ulong)


class _KI(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", _PUL)]


class _MI(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", ctypes.c_ulong),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong), ("dwExtraInfo", _PUL)]


class _II(ctypes.Union):
    _fields_ = [("ki", _KI), ("mi", _MI)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("ii", _II)]


def _send(inp):
    _u32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))


def _move_abs(x, y):
    w, h = _u32.GetSystemMetrics(0), _u32.GetSystemMetrics(1)
    i = _INPUT(0, _II(mi=_MI(int(x * 65535 / (w - 1)), int(y * 65535 / (h - 1)), 0,
                              0x0001 | 0x8000, 0, None)))  # MOVE | ABSOLUTE
    _send(i)


def _mouse_btn(down):
    _send(_INPUT(0, _II(mi=_MI(0, 0, 0, 0x0002 if down else 0x0004, 0, None))))


def _unicode_char(ch, down):
    _send(_INPUT(1, _II(ki=_KI(0, ord(ch), 0x0004 | (0 if down else 0x0002), 0, None))))  # KEYEVENTF_UNICODE


def focus_game(point=None):
    """หาหน้าต่างที่ชื่อมี YGOnline แล้วดึงขึ้นมาหน้าสุด"""
    found = []
    cb_t = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def cb(hwnd, _):
        if _u32.IsWindowVisible(hwnd):
            n = _u32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            _u32.GetWindowTextW(hwnd, buf, n + 1)
            if "ygonline" in buf.value.lower():
                found.append(hwnd)
        return True
    _u32.EnumWindows(cb_t(cb), 0)
    if not found:
        return False
    hwnd = found[0]
    if point:  # มีหลายไคลเอนต์: เลือกหน้าต่างที่ครอบคลุมจุดที่จะคลิก
        for h in found:
            r = (ctypes.c_long * 4)()
            _u32.GetWindowRect(h, r)
            if r[0] <= point[0] <= r[2] and r[1] <= point[1] <= r[3]:
                hwnd = h
                break
    _u32.ShowWindow(hwnd, 9)  # SW_RESTORE
    _key(0x38, True); _key(0x38, False)  # แตะ Alt ให้ Windows ยอมสลับหน้าต่าง
    _u32.SetForegroundWindow(hwnd)
    return True


_GAME_HWND = [None]  # หน้าต่างเกมที่ล็อกไว้ตอนเริ่ม (มีหลายไคลเอนต์ เปิดอยู่หลายจอ)


def _win_rect(hwnd):
    r = (ctypes.c_long * 4)()
    _u32.GetWindowRect(ctypes.c_void_p(hwnd), r)
    return list(r)


def lock_game(point=None):
    """เลือกหน้าต่างเกมที่ครอบจุด point (เช่น NPC/กระเป๋า) แล้วจำไว้ใช้ตลอดการรัน"""
    _GAME_HWND[0] = None
    hs = _game_windows()
    pick = None
    for h in hs:
        r = _win_rect(h)
        if point and r[0] <= point[0] <= r[2] and r[1] <= point[1] <= r[3]:
            pick = h
            break
    if pick is None and hs:
        fg = _u32.GetForegroundWindow()
        pick = fg if fg in hs else hs[0]
    _GAME_HWND[0] = pick
    return pick


def _game_windows():
    found = []
    cb_t = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def cb(hwnd, _):
        if _u32.IsWindowVisible(hwnd):
            n = _u32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            _u32.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value.strip().lower().startswith("ygonline"):
                found.append(hwnd)
        return True
    _u32.EnumWindows(cb_t(cb), 0)
    return found


def game_rect():
    """กรอบหน้าต่างเกม YGOnline (ใช้หน้าต่างที่ล็อกไว้ตอนเริ่ม) -> (x1, y1, x2, y2) หรือ None"""
    h = _GAME_HWND[0]
    if h and _u32.IsWindow(ctypes.c_void_p(h)):
        r = _win_rect(h)
        x1, y1, x2, y2 = max(0, r[0]), max(0, r[1]), r[2], r[3]
        if x2 - x1 >= 200 and y2 - y1 >= 200:
            return (x1, y1, x2, y2)
    found = []
    cb_t = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def cb(hwnd, _):
        if _u32.IsWindowVisible(hwnd):
            n = _u32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            _u32.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value.strip().lower().startswith("ygonline"):
                found.append(hwnd)
        return True
    _u32.EnumWindows(cb_t(cb), 0)
    if not found:
        return None
    fg = _u32.GetForegroundWindow()
    hwnd = fg if fg in found else found[0]
    r = (ctypes.c_long * 4)()
    _u32.GetWindowRect(ctypes.c_void_p(hwnd), r)
    x1, y1, x2, y2 = max(0, r[0]), max(0, r[1]), r[2], r[3]
    if x2 - x1 < 200 or y2 - y1 < 200:  # ย่อหน้าต่างอยู่
        return None
    return (x1, y1, x2, y2)


def _key(scan, down):
    _send(_INPUT(1, _II(ki=_KI(0, scan, 0x0008 | (0 if down else 0x0002), 0, None))))  # SCANCODE


# ====================================================================== core
class Bot:
    def __init__(self, log):
        self.log = log
        self.cfg = json.loads(json.dumps(DEFAULT))
        self.running = threading.Event()
        self.templates = {}
        self.load()
        self.load_templates()
        self.load_aux()
        self.cache = {}
        self.win_open = False
        self.stone_est = 0
        self.track_stone = False
        self.done = {}

    # ---------- config
    def load(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, encoding="utf-8") as f:
                    data = json.load(f)
                types = data.pop("types", {})
                self.cfg.update(data)
                for k, v in types.items():
                    if k in self.cfg["types"]:
                        self.cfg["types"][k].update(v)
            except Exception as e:
                self.log(f"โหลด config ไม่ได้: {e}")

    def save(self):
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(self.cfg, f, ensure_ascii=False, indent=2)
        self.log("บันทึกการตั้งค่าแล้ว")

    def load_templates(self):
        for key, name, fn in ITEM_TYPES:
            path = os.path.join(ICON_DIR, fn)
            img = cv2.imdecode(np.fromfile(path, np.uint8), cv2.IMREAD_COLOR) if os.path.exists(path) else None
            if img is None:
                self.log(f"ไม่พบไอคอน {name}: {path}")
                continue
            # ตัดขอบช่องออก ใช้เฉพาะกลางไอคอน (กันกรอบช่อง/เลข + ทำให้ match พลาด)
            h, w = img.shape[:2]
            m = max(3, min(h, w) // 8)
            self.templates[key] = img[m:h - m, m:w - m]

    # ---------- screen search
    def ensure_bag(self):
        """หากระเป๋า ถ้าไม่เจอกด Tab เปิด แล้วลากมาวางที่ bag_target (ถ้าตั้งไว้) คืนตำแหน่งกลางหัวข้อ"""
        p = self.find_one("bag")
        for _ in range(2):
            if p:
                break
            self.log("ไม่เจอกระเป๋า -> กด Tab")
            _key(0x0F, True)
            time.sleep(0.05)
            _key(0x0F, False)
            time.sleep(0.7)
            p = self.find_one("bag")
        if not p:
            return None
        tgt = self.cfg.get("bag_target")
        if tgt and (abs(p[0] - tgt[0]) > 6 or abs(p[1] - tgt[1]) > 6):
            self.log(f"ลากกระเป๋า {p} -> {tgt}")
            self.drag(p, tgt)
            time.sleep(0.4)
            q = self.find_one("bag")
            p = q or p
        return p

    def drag(self, a, b):
        _move_abs(a[0], a[1])
        time.sleep(0.15)
        _mouse_btn(True)
        time.sleep(0.15)
        steps = 12
        for i in range(1, steps + 1):
            _move_abs(a[0] + (b[0] - a[0]) * i / steps, a[1] + (b[1] - a[1]) * i / steps)
            time.sleep(0.02)
        time.sleep(0.15)
        _mouse_btn(False)

    def region(self):
        # โหมดอัตโนมัติ: หาหัวข้อ "ช่องสัมภาระ" แล้วคำนวณพื้นที่ช่องไอเทม (ตามตำแหน่งหน้าต่างปัจจุบัน)
        if self.cfg.get("auto", True) and "bag" in getattr(self, "aux", {}):
            if "_reg" not in self.cache:
                p = self.ensure_bag()
                if p:
                    th, tw = self.aux["bag"].shape[:2]
                    x0, y0 = p[0] - tw // 2, p[1] - th // 2
                    self.cache["_reg"] = (x0, y0 + 255, x0 + 215, y0 + 470)
                    self.log(f"เจอกระเป๋า พื้นที่ค้นหา = {self.cache['_reg']}")
                else:
                    self.cache["_reg"] = None
            if self.cache["_reg"]:
                return self.cache["_reg"]
        a, b = self.cfg["reg1"], self.cfg["reg2"]
        if a and b:
            return (min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))
        return None

    def find(self, key):
        tpl = self.templates.get(key)
        if tpl is None:
            return []
        reg = self.region()
        shot = ImageGrab.grab(bbox=reg) if reg else ImageGrab.grab()
        scr = cv2.cvtColor(np.array(shot), cv2.COLOR_RGB2BGR)
        th, tw = tpl.shape[:2]
        if scr.shape[0] < th or scr.shape[1] < tw:
            return []
        res = cv2.matchTemplate(scr, tpl, cv2.TM_CCOEFF_NORMED)
        ys, xs = np.where(res >= self.cfg["threshold"])
        cands = sorted(zip(res[ys, xs], xs, ys), reverse=True)
        found = []
        for score, x, y in cands:  # ตัดจุดซ้ำที่อยู่ใกล้กัน
            cx, cy = int(x + tw / 2), int(y + th / 2)
            if all(abs(cx - fx) > tw * 0.6 or abs(cy - fy) > th * 0.6 for fx, fy in found):
                found.append((cx, cy))
        ox, oy = (reg[0], reg[1]) if reg else (0, 0)
        found = [(x + ox, y + oy) for x, y in found]
        found.sort(key=lambda p: (p[1] // 20, p[0]))  # เรียงแถวบน->ล่าง ซ้าย->ขวา
        return found

    # ---------- หาตำแหน่งอัตโนมัติ (npc / menu / ok / stone / charm)
    AUX_THR = {"npc": 0.6, "menu": 0.7, "ok": 0.75, "stone": 0.75, "charm": 0.75, "bag": 0.7,
               "shop_menu": 0.7, "shop_title": 0.7, "shop_stone": 0.75, "shop_charm": 0.75,
               "shop_armor": 0.75, "shop_boots": 0.75, "shop_sword": 0.75}
    REQUIRED = ("npc", "ok", "stone")

    def load_aux(self):
        self.aux = {}
        for n in self.AUX_THR:
            path = os.path.join(ICON_DIR, n + ".png")
            if os.path.exists(path):
                self.aux[n] = cv2.imdecode(np.fromfile(path, np.uint8), cv2.IMREAD_COLOR)

    @staticmethod
    def _gmask(img):  # เก็บเฉพาะพิกเซลสีเขียวสว่าง (ตัวอักษรชื่อ NPC)
        b, g, r = cv2.split(img)
        return (((g > 140) & (r < 150) & (b < 130)).astype(np.uint8)) * 255

    def find_one(self, name, region=None):
        tpl = getattr(self, "aux", {}).get(name)
        if tpl is None:
            return None
        if not region:  # หาเฉพาะในหน้าต่างเกม (กันไปเจอของคล้ายๆ บนหน้าต่างบอทเอง)
            region = game_rect()
        shot = ImageGrab.grab(bbox=region) if region else ImageGrab.grab()
        scr = cv2.cvtColor(np.array(shot), cv2.COLOR_RGB2BGR)
        a, b = (self._gmask(scr), self._gmask(tpl)) if name == "npc" else (scr, tpl)
        th, tw = b.shape[:2]
        if a.shape[0] < th or a.shape[1] < tw:
            return None
        res = np.nan_to_num(cv2.matchTemplate(a, b, cv2.TM_CCOEFF_NORMED))
        _, mx, _, loc = cv2.minMaxLoc(res)
        if mx < self.AUX_THR[name]:
            return None
        ox, oy = (region[0], region[1]) if region else (0, 0)
        return [int(loc[0] + tw / 2 + ox), int(loc[1] + th / 2 + oy)]

    def resolve(self, name, retries=1):
        """คืนตำแหน่งของ name: หาเองก่อน (ถ้าเปิดโหมดอัตโนมัติ) ไม่เจอใช้ค่าที่ตั้งมือ"""
        if name in self.cache:  # เคยหาเจอแล้วในรอบนี้ ใช้ซ้ำ ไม่ต้องสแกนจอใหม่
            return self.cache[name]
        if self.cfg.get("auto", True):
            reg = self.region() if name in ("stone", "charm") else None
            for i in range(retries):
                p = self.find_one(name, reg)
                if p is None and reg:
                    p = self.find_one(name)
                if p:
                    if name == "npc":
                        p = [p[0], p[1] + 35]  # ชื่ออยู่เหนือหัว คลิกลงมาที่ตัวละคร
                    if name in ("stone", "charm", "ok"):
                        self.cache[name] = p  # stone/charm ถูกล้างทุกครั้งที่ตี (เผื่อของหมด)
                    return p
                if i < retries - 1 and not self.wait(0.4):
                    return None
        manual = self.cfg.get(name)
        if not manual:
            self.log(f"หา '{name}' ไม่เจอ และยังไม่ได้ตั้งตำแหน่งมือ")
        return manual

    def item_at(self, key, p):
        """เช็คว่ายังมีไอเทมชนิดนี้อยู่ที่ช่อง p ไหม (หายไป = แตก)"""
        tpl = self.templates.get(key)
        if tpl is None:
            return True
        th, tw = tpl.shape[:2]
        r = max(th, tw)
        time.sleep(0.12)
        shot = ImageGrab.grab(bbox=(p[0] - r, p[1] - r, p[0] + r, p[1] + r))
        scr = cv2.cvtColor(np.array(shot), cv2.COLOR_RGB2BGR)
        if scr.shape[0] < th or scr.shape[1] < tw:
            return True
        best = cv2.matchTemplate(scr, tpl, cv2.TM_CCOEFF_NORMED).max()
        return best >= max(0.5, self.cfg["threshold"] - 0.3)  # ผ่อนเกณฑ์ เผื่อมีเลข +N ทับ

    # ---------- input
    def wait(self, sec):
        end = time.time() + sec
        while time.time() < end:
            if not self.running.is_set():
                return False
            time.sleep(0.02)
        return True

    def click(self, p, ctrl=False):
        x, y = int(p[0]), int(p[1])
        m = int(self.cfg.get("method", 1))
        k = float(self.cfg.get("click_scale", 0.35))  # 1 = ช้า/ชัวร์, ยิ่งน้อยยิ่งเร็ว

        def nap(t):
            time.sleep(t * k)
        if m == 1:    # SendInput
            _move_abs(x - 3, y - 3)
            nap(0.08)
            _move_abs(x, y)
        elif m == 2:  # SetCursorPos + mouse_event
            _u32.SetCursorPos(x - 3, y - 3)
            nap(0.08)
            _u32.SetCursorPos(x, y)
        else:         # pynput
            mc.position = (x, y)
        nap(0.15)
        if ctrl:
            if m == 3:
                kc.press(keyboard.Key.ctrl_l)
            elif m == 2:
                _u32.keybd_event(0x11, 0x1D, 0, 0)
            else:
                _key(0x1D, True)
            nap(0.12)
        if m == 2:
            _u32.mouse_event(0x0002, 0, 0, 0, 0)
            nap(0.12)
            _u32.mouse_event(0x0004, 0, 0, 0, 0)
        elif m == 3:
            mc.press(mouse.Button.left)
            nap(0.12)
            mc.release(mouse.Button.left)
        else:
            _mouse_btn(True)
            nap(0.12)
            _mouse_btn(False)
        if ctrl:
            nap(0.12)
            if m == 3:
                kc.release(keyboard.Key.ctrl_l)
            elif m == 2:
                _u32.keybd_event(0x11, 0x1D, 2, 0)
            else:
                _key(0x1D, False)
        return self.wait(self.cfg["delay_click"])

    def open_npc(self):
        self.win_open = False
        self.cache.clear()  # หน้าต่างเปิดใหม่ อาจอยู่ที่ใหม่ ให้หาตำแหน่งหิน/ยันต์/ตกลงอีกครั้ง
        p = self.resolve("npc")
        if not p:
            return False
        self.log(f"  คลิก NPC ที่ {p}")
        if not (self.click(p) and self.wait(self.cfg["delay_after_npc"])):
            return False
        m = None
        if self.cfg.get("auto", True) and "menu" in self.aux:
            m = self.wait_find("menu", tries=5)
            if not m and self.find_one("bag"):
                # กระเป๋าบังเมนูของโล้น -> กด Tab ปิดกระเป๋าก่อน (เดี๋ยวตอนหาไอเทมจะเปิดคืนเอง)
                self.log("  หาเมนูเสริมพลังไม่เจอ (กระเป๋าน่าจะบัง) -> กด Tab ปิดกระเป๋า")
                _key(0x0F, True)
                time.sleep(0.05)
                _key(0x0F, False)
                time.sleep(0.6)
                m = self.wait_find("menu", tries=6)
        if not m:
            m = self.resolve("menu", retries=3) if (self.cfg.get("auto", True) or self.cfg.get("menu")) else None
        if m:  # เลือกเมนู "เสริมพลัง(ใหม่)" ในกล่องคุยของ NPC
            self.log(f"  คลิกเมนูเสริมพลัง ที่ {m}")
            ok = self.click(m) and self.wait(self.cfg["delay_after_npc"])
            self.win_open = bool(ok)
            return ok
        self.win_open = True
        return True

    # ---------- ซื้อหิน/ยันต์เมื่อหมด
    def wait_find(self, name, tries=10):
        for _ in range(tries):
            if not self.running.is_set():
                return None
            p = self.find_one(name)
            if p:
                return p
            time.sleep(0.3)
        return None

    def snap(self, name):
        """เก็บภาพหน้าจอไว้ดูตอนแก้ปัญหา (บันทึกในโฟลเดอร์โปรแกรม)"""
        try:
            ImageGrab.grab().save(os.path.join(BASE, name))
        except Exception:
            pass

    def type_number(self, n):
        # ส่งเป็นตัวอักษร Unicode ตรงๆ ไม่ขึ้นกับภาษาคีย์บอร์ด (ไทย/อังกฤษ) และไม่ผ่าน IME
        for ch in str(int(n)):
            _unicode_char(ch, True)
            time.sleep(0.05)
            _unicode_char(ch, False)
            time.sleep(0.08)
        time.sleep(0.2)
        _key(0x1C, True)  # Enter
        time.sleep(0.04)
        _key(0x1C, False)

    def restock(self, name):
        """คลิกโล้น > ซื้อมา/ขายไป > Ctrl+คลิกของ > พิมพ์จำนวน > Enter"""
        label = {"stone": "หินแดง", "charm": "ยันต์", "armor": "เกาะ", "boots": "เท้า", "sword": "ดาบ"}[name]
        qty = int(self.cfg.get("buy_" + name, self.cfg.get("buy_armor", 20)))  # ดาบ/เท้า ใช้จำนวนเดียวกับเกาะ
        self.log(f"ต้องซื้อ{label} -> ไปซื้อ " + (f"{qty} ชิ้น" if name == "stone" else f"{qty} ครั้ง (Ctrl+คลิกซ้ำ)"))
        if self.win_open:  # หน้าต่างเสริมพลังยังเปิดอยู่ กด Esc ปิดก่อน
            self.log("  กด Esc ปิดหน้าต่างเสริมพลัง")
            _key(0x01, True)
            time.sleep(0.05)
            _key(0x01, False)
            self.win_open = False
            self.wait(0.8)
        title = None
        for shop_try in range(1, 3):
            # คลิกโล้นทุกครั้ง ไม่เชื่อว่าเมนูเปิดค้างอยู่ (เคยหาเมนูเจอผิดที่ เลยไม่ได้คลิกโล้นจริง)
            m = None
            for attempt in range(1, 4):
                p = self.resolve("npc")
                if not p:
                    return False
                self.log(f"  คลิกโล้น ที่ {p} (ครั้งที่ {attempt})")
                if not (self.click(p) and self.wait(self.cfg["delay_after_npc"])):
                    return False
                m = self.wait_find("shop_menu", tries=6)
                if not m and self.find_one("bag"):
                    # เมนูของโล้นโดนกระเป๋าบัง -> Tab ปิดกระเป๋าแล้วหาใหม่ (ไม่ต้องคลิกโล้นซ้ำ)
                    self.log("  หาเมนู 'ซื้อมา/ขายไป' ไม่เจอ (กระเป๋าน่าจะบัง) -> กด Tab ปิดกระเป๋า")
                    _key(0x0F, True)
                    time.sleep(0.05)
                    _key(0x0F, False)
                    time.sleep(0.6)
                    self.cache.pop("_reg", None)
                    m = self.wait_find("shop_menu", tries=6)
                if m:
                    break
            if not m:
                self.snap("debug_shop_menu_not_found.png")
                self.log("  หาเมนู 'ซื้อมา/ขายไป' ไม่เจอ (ลองคลิกโล้น 3 ครั้งแล้ว) ดูภาพ debug_shop_menu_not_found.png")
                return False
            self.log(f"  คลิก 'ซื้อมา/ขายไป' ที่ {m}")
            self.click(m)
            title = self.wait_find("shop_title")
            if title:
                break
            self.snap("debug_shop_not_found.png")
            self.log(f"  หาหน้าต่างร้านค้าไม่เจอ (ครั้งที่ {shop_try}) ดูภาพ debug_shop_not_found.png")
            _key(0x01, True)  # Esc ปิดสิ่งที่เปิดค้าง แล้วลองใหม่
            time.sleep(0.05)
            _key(0x01, False)
            self.wait(0.8)
        if not title:
            return False
        self.wait(0.4)
        # หาของเฉพาะ "ในกรอบร้านค้า" เท่านั้น ห้ามไปเจอของในกระเป๋า (Ctrl+คลิกของในกระเป๋าตอนเปิดร้าน = ขายทิ้ง!)
        title = self.find_one("shop_title") or title
        shop_reg = (max(0, title[0] - 70), title[1] + 25, title[0] + 200, title[1] + 430)
        item = self.find_one("shop_" + name, shop_reg)
        if not item:
            self.snap("debug_shop_item_not_found.png")
            self.log(f"  ไม่เจอ{label}ในร้านค้า (ค้นในกรอบ {shop_reg})")
            return False
        reg = self.cache.get("_reg")
        if reg and reg[0] - 20 <= item[0] <= reg[2] + 20 and reg[1] - 20 <= item[1] <= reg[3] + 20:
            self.log(f"  ตำแหน่ง {item} อยู่ในกระเป๋า ไม่ใช่ร้านค้า -> ยกเลิก (กันขายของ)")
            return False
        if name == "stone":  # หินใส่จำนวนได้: Ctrl+คลิก > พิมพ์จำนวน > Enter
            self.log(f"  Ctrl+คลิกหินในร้านที่ {item}")
            self.click(item, ctrl=True)
            self.wait(1.0)  # รอช่องใส่จำนวนขึ้น
            self.snap("debug_1_after_ctrlclick.png")
            self.log(f"  พิมพ์จำนวน {qty} + Enter")
            self.type_number(qty)
            self.wait(0.3)
            self.snap("debug_2_after_typing.png")
            self.wait(0.7)
        else:  # เกาะ/ยันต์ใส่จำนวนไม่ได้: Ctrl+คลิกซ้ำทีละชิ้น
            # กด Ctrl ค้างไว้ แล้วคลิกรัวที่จุดเดิม (เร็วกว่าคลิกทีละครั้งแบบเต็มขั้นตอนมาก)
            gap = float(self.cfg.get("buy_click_gap", 0.08))
            _move_abs(item[0], item[1])
            time.sleep(0.15)
            _key(0x1D, True)
            time.sleep(0.08)
            try:
                for i in range(qty):
                    if not self.running.is_set():
                        return False
                    _mouse_btn(True)
                    time.sleep(0.02)
                    _mouse_btn(False)
                    time.sleep(gap)
                    if (i + 1) % 50 == 0:
                        self.log(f"  ซื้อ{label}ไปแล้ว {i + 1}/{qty}")
            finally:
                _key(0x1D, False)
            self.wait(0.5)
        self.click([title[0] + 185, title[1]])  # ปุ่ม X ปิดร้านค้า
        self.wait(0.5)
        self.log(f"  ซื้อ{label}เสร็จ")
        return True

    def charm_at(self, n):
        """ครั้งที่ n ของชิ้นนี้ต้องใช้ยันต์ไหม (ติ๊กใช้ยันต์ + ถึงครั้งที่ตั้งไว้)"""
        if not self.cfg["use_charm"]:
            return False
        start = int(self.cfg.get("charm_from", 0) or 0)
        return start <= 1 or n >= start

    def ensure_supplies(self, charm=None):
        """เช็คว่ามีหิน(/ยันต์)ในกระเป๋า ถ้าหมดและเปิดโหมดซื้อ จะไปซื้อแล้วเปิดหน้าต่างเสริมพลังใหม่"""
        if charm is None:
            charm = self.cfg["use_charm"]
        need = ["stone"] + (["charm"] if charm else [])
        bought = False
        auto = self.cfg.get("auto", True)
        # หินเหลือน้อยกว่าเกณฑ์ (นับจากจำนวนที่ใส่ไว้ - จำนวนครั้งที่ตีไปแล้ว) -> ไปซื้อเพิ่ม
        low = self.cfg.get("stone_low", 100)
        if self.cfg.get("auto_buy", True) and auto and self.track_stone and self.stone_est < low:
            real = self.read_stone_count()  # เช็คตัวเลขจริงจากแถบลัดก่อนตัดสินใจซื้อ
            if real is not None:
                self.stone_est = real
        if (self.cfg.get("auto_buy", True) and auto and self.track_stone and self.stone_est < low):
            self.log(f"หินเหลือ {self.stone_est} (ต่ำกว่า {low})")
            if not self.restock("stone"):
                return False
            self.stone_est += int(self.cfg.get("buy_stone", 100))
            bought = True
        for n in need:
            if self.has_supply(n):
                continue
            if not (self.cfg.get("auto_buy", True) and auto):
                self.log(f"{n} หมด (ไม่ได้เปิดโหมดซื้อเอง)")
                return False
            if n == "stone" and self.track_stone:
                real = self.read_stone_count()
                # ไม่เจอหินทั้งในกระเป๋าและในแถบลัด = หินหมดจริง (ไม่เชื่อตัวเลขที่กรอกไว้)
                self.stone_est = real if real is not None else 0
            if n == "stone" and self.track_stone and self.stone_est > 0:
                # ตามที่นับไว้ยังมีหินอยู่ แต่หาไอคอนไม่เจอ -> อาจมี popup ประกาศบังช่องหิน รอให้หายไปก่อน (สูงสุด ~15 วิ)
                self.log("  หาไอคอนหินไม่เจอ แต่ยังมีหิน -> รอ popup/เอฟเฟกต์บังหาย แล้วหาซ้ำ")
                if self.has_supply("stone", tries=38):
                    continue
                self.snap("debug_stone_not_found.png")
                self.log(f"  หาไอคอนหินในกระเป๋าไม่เจอ (นับไว้ยังเหลือ {self.stone_est}) -> หยุด"
                         " ดูภาพ debug_stone_not_found.png")
                return False
            if not self.restock(n):
                return False
            if n == "stone":
                self.stone_est += int(self.cfg.get("buy_stone", 100))
                self.track_stone = True
            bought = True
        if bought:
            if not self.open_npc():
                return False
            for n in need:
                self.wait(0.3)
                if not self.has_supply(n):
                    self.log(f"ซื้อแล้วแต่ยังหา {n} ในกระเป๋าไม่เจอ")
                    return False
        return True

    # ---------- แถวล่างสุดของกระเป๋า = ที่เก็บชิ้นที่ตีเสร็จแล้ว (บอทไม่ตีแถวนี้)
    BAG_ROWS, BAG_COLS = 6, 6

    def done_rows(self):
        return max(1, min(5, int(self.cfg.get("done_rows", 4))))  # จำนวนแถวล่างที่ใช้เก็บชิ้นที่ตีเสร็จ

    def cell(self, r, c):
        reg = self.region()
        if not reg:
            return None
        return (reg[0] + 24 + 33 * c, reg[1] + 19 + 35 * r)

    def cell_empty(self, p):
        shot = ImageGrab.grab(bbox=(p[0] - 11, p[1] - 11, p[0] + 11, p[1] + 11))
        a = np.array(shot)
        return a.mean() < 45 and a.std() < 15

    def not_done(self, key, spots):
        """ตัดชิ้นที่ตีเสร็จแล้วออก: ชิ้นที่อยู่แถวล่างสุดของกระเป๋า + ช่องที่จำไว้ (กรณีย้ายลงแถวล่างไม่ได้)"""
        done = self.done.get(key, [])
        first = self.cell(self.BAG_ROWS - self.done_rows(), 0)  # แถวบนสุดของโซนเก็บของ
        out = []
        for p in spots:
            if first and p[1] >= first[1] - 17:
                continue
            if any(abs(p[0] - d[0]) <= 12 and abs(p[1] - d[1]) <= 12 for d in done):
                continue
            out.append(p)
        return out

    def survived(self, key, p, sec=2.0):
        """ครั้งสุดท้าย: รอผลตีให้ชัวร์ก่อน (เกมใช้เวลาลบของที่แตก) ถ้าหายไปช่วงนี้ = แตก"""
        end = time.time() + sec
        while time.time() < end:
            if not self.item_at(key, p):
                return False
            if not self.wait(0.2):
                return False
        return True

    def move_to_done_row(self, key, p):
        """ย้ายชิ้นที่ตีเสร็จไปช่องว่างในแถวล่างสุด (คลิกหยิบ -> คลิกวาง) คืน True ถ้าย้ายสำเร็จ"""
        if self.win_open:  # กล่องแจ้งเตือน/หน้าต่างเสริมพลังเปิดอยู่ เกมไม่ให้หยิบของ -> Esc ปิดก่อน
            self.log("  กด Esc ปิดหน้าต่างเสริมพลังก่อนย้าย")
            _key(0x01, True)
            time.sleep(0.05)
            _key(0x01, False)
            self.win_open = False
            time.sleep(0.8)
            self.cache.pop("_reg", None)  # หากระเป๋าใหม่ (เผื่อถูกปิด/ขยับ)
        target = None
        for r in range(self.BAG_ROWS - 1, self.BAG_ROWS - 1 - self.done_rows(), -1):  # แถวล่างสุดก่อน
            for c in range(self.BAG_COLS - 1, -1, -1):  # ใส่จากขวาไปซ้าย
                q = self.cell(r, c)
                if q and self.cell_empty(q):
                    target = q
                    break
            if target:
                break
        if not target:
            self.log(f"  {self.done_rows()} แถวล่างของกระเป๋าเต็มแล้ว -> หยุดทันที")
            self.done_full = True
            return False
        self.log(f"  ย้ายชิ้นที่ตีเสร็จ {p} -> โซนเก็บ {target}")

        def slow_click(q):  # คลิกช้าๆ ชัวร์ๆ (คลิกเร็วเกม์อาจไม่รับการหยิบ/วาง)
            _move_abs(q[0] - 4, q[1] - 4)
            time.sleep(0.1)
            _move_abs(q[0], q[1])
            time.sleep(0.25)
            _mouse_btn(True)
            time.sleep(0.1)
            _mouse_btn(False)

        for attempt in range(1, 3):
            _move_abs(1, 1)
            time.sleep(0.2)
            if self.cell_empty(p):  # ของหายไปแล้ว (แตกตอนตีครั้งสุดท้าย) ไม่ต้องย้าย
                self.log("  ช่องนี้ว่างแล้ว (ของแตกไปตอนตีครั้งสุดท้าย) -> ไม่ต้องย้าย")
                return True
            slow_click(p)  # หยิบ
            time.sleep(0.6)
            self.snap("debug_move_1_pick.png")
            slow_click(target)  # วาง
            time.sleep(0.7)
            self.snap("debug_move_2_place.png")
            _move_abs(1, 1)
            time.sleep(0.3)
            if not self.cell_empty(target):
                self.log("  ย้ายสำเร็จ")
                return True
            # ยังไม่ย้าย: ถ้าของติดเมาส์อยู่ คลิกช่องเดิมเพื่อวางคืน
            if self.cell_empty(p):
                slow_click(p)
                time.sleep(0.5)
                _move_abs(1, 1)
            self.log(f"  ย้ายไม่สำเร็จ (ครั้งที่ {attempt})")
        self.log("  ย้ายไม่ได้ -> จำตำแหน่งไว้แทน ดูภาพ debug_move_1_pick.png / debug_move_2_place.png")
        return False

    def read_stone_count(self):
        """อ่านจำนวนหินจากแถบลัด (เช่น F1) ด้วย Windows OCR -> int หรือ None ถ้าอ่านไม่ได้
        ต้องลากหินไปไว้ในแถบลัดด้านล่างจอ (ช่องไหนก็ได้)"""
        tpl = getattr(self, "aux", {}).get("stone")
        if tpl is None:
            return None
        try:
            scr = cv2.cvtColor(np.array(ImageGrab.grab()), cv2.COLOR_RGB2BGR)
            res = cv2.matchTemplate(scr, tpl, cv2.TM_CCOEFF_NORMED)
            reg = self.cache.get("_reg")
            best = None
            th, tw = tpl.shape[:2]
            for _ in range(6):  # หาไอคอนหินที่อยู่ "นอกกระเป๋า" และต่ำที่สุดในจอ = ช่องแถบลัด
                _, mx, _, loc = cv2.minMaxLoc(res)
                if mx < 0.58:
                    break
                cx, cy = loc[0] + tw // 2, loc[1] + th // 2
                res[max(0, loc[1] - th):loc[1] + th, max(0, loc[0] - tw):loc[0] + tw] = -1
                if reg and reg[0] - 5 <= cx <= reg[2] + 5 and reg[1] - 5 <= cy <= reg[3] + 5:
                    continue
                if best is None or cy > best[1]:
                    best = (cx, cy)
            if not best:
                return None
            cx, cy = best
            crop = scr[max(0, cy - 2):cy + 18, max(0, cx - 12):cx + 22].astype(int)
            b, g, r = crop[..., 0], crop[..., 1], crop[..., 2]
            m = (g > 150) & (b > 150) & (g - r > 20)  # ตัวเลขสีฟ้าอ่อน
            ys, xs = np.where(m)
            if len(xs) < 5:
                return None
            m = m[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
            img = np.where(m, 0, 255).astype(np.uint8)
            img = cv2.resize(img, None, fx=6, fy=6, interpolation=cv2.INTER_NEAREST)
            img = cv2.GaussianBlur(img, (0, 0), 2)
            img = cv2.copyMakeBorder(img, 50, 50, 50, 50, cv2.BORDER_CONSTANT, value=255)
            path = os.path.join(tempfile.gettempdir(), "yg_stone_count.png")
            cv2.imwrite(path, img)
            out = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                 os.path.join(BASE, "ocr_digits.ps1"), "-Path", path],
                capture_output=True, text=True, timeout=15, creationflags=0x08000000)  # ไม่โชว์หน้าต่าง
            digits = "".join(ch for ch in out.stdout if ch.isdigit())
            return int(digits) if digits else None
        except Exception as e:
            self.log(f"  อ่านจำนวนหินไม่ได้: {e}")
            return None

    def has_supply(self, n, tries=6):
        """หาหิน/ยันต์ในกระเป๋าจริงๆ (ไม่ใช้ตำแหน่งที่ตั้งมือ เพราะของหมดแล้วช่องก็ว่าง)
        หาไม่เจอครั้งแรกอาจเพราะ tooltip/เอฟเฟกต์แตกบังอยู่ -> เลื่อนเมาส์ออกแล้วลองใหม่หลายครั้ง"""
        if not self.cfg.get("auto", True):
            return bool(self.resolve(n))
        for i in range(tries):
            reg = self.region()
            p = self.find_one(n, reg) or (self.find_one(n) if reg else None)
            if p:
                self.cache[n] = p
                return True
            if i == 0:
                _move_abs(1, 1)  # เอาเมาส์ออกจากกระเป๋า กัน tooltip บังไอคอน
            self.cache.pop("_reg", None)  # หากระเป๋าใหม่ เผื่อหน้าต่างขยับ
            if not self.wait(0.4):
                return False
        return False

    def upgrade_once(self, item, n=1):
        c = self.cfg
        charm = self.charm_at(n)
        self.cache.pop("stone", None)
        self.cache.pop("charm", None)
        if not self.ensure_supplies(charm):
            return False
        if not self.click(item, True):
            return False
        if charm:
            self.log("    + ใช้ยันต์")
        names = ["stone"] + (["charm"] if charm else []) + ["ok"]
        for n in names:
            p = self.resolve(n, retries=5 if n == "ok" else 1)
            if not p:
                self.log(f"  หา {n} ไม่เจอ -> หยุด")
                return False
            if not self.click(p, ctrl=(n != "ok")):
                return False
            if n == "stone":
                self.stone_est -= int(self.cfg.get("stone_per", 1))  # หินใช้ไป
        return self.wait(c["delay_after_ok"])

    # ---------- main loop
    def check(self):
        c = self.cfg
        if c.get("auto", True):  # โหมดอัตโนมัติ: ไม่ต้องตั้งตำแหน่งมือ ขอแค่มีรูปต้นแบบครบ
            req = list(self.REQUIRED) + (["charm"] if c["use_charm"] else [])
            if c.get("auto_buy", True):
                req += ["shop_menu", "shop_title", "shop_stone"] + (["shop_charm"] if c["use_charm"] else [])
                req += ["shop_" + k for k, t in c["types"].items() if t["enabled"]]
            miss = [n for n in req if n not in self.aux]
            if miss:
                self.log("ไม่พบรูปต้นแบบใน icons/: " + ", ".join(miss))
                return False
            miss = []
        else:
            need = ["npc", "stone", "ok"] + (["charm"] if c["use_charm"] else [])
            miss = [n for k, n, _ in POINTS if k in need and not c[k]]
        if miss:
            self.log("ยังไม่ได้ตั้งตำแหน่ง: " + ", ".join(miss))
            return False
        if not any(t["enabled"] for t in c["types"].values()):
            self.log("ยังไม่ได้เลือกไอเทมที่จะตี")
            return False
        return True

    def run(self, on_done):
        try:
            if not self.check():
                return
            self.log("=== เริ่มตีบวก ===")
            lock_game(self.cfg.get("npc") or self.cfg.get("bag_target"))
            self.log("สลับไปหน้าต่างเกม: " + ("สำเร็จ" if focus_game(self.cfg.get("npc") or self.cfg.get("stone")) else "ไม่พบหน้าต่าง YGOnline"))
            time.sleep(0.5)
            total = 0
            broken = 0
            if not self.open_npc():
                return
            rounds = 0
            self.stone_est = int(self.cfg.get("stone_have", 0))
            self.track_stone = self.stone_est > 0
            self.done_full = False
            self.done = {}  # ช่องที่ตีครบแล้วไม่แตก (จำเฉพาะรอบการกด F9 นี้)
            real = self.read_stone_count()
            if real is not None:
                self.stone_est = real
                self.track_stone = True
                self.log(f"อ่านจำนวนหินจากแถบลัดได้: {real}")
            else:
                self.log("อ่านจำนวนหินจากแถบลัดไม่ได้ (ไม่มีหินในช่อง F1 / หินหมด) -> ใช้ค่าที่กรอกไว้: "
                         + str(self.stone_est) + " (ถ้าหาหินในกระเป๋าไม่เจอจะไปซื้อ)")
            while self.running.is_set():
                rounds += 1
                before = total
                for key, name, _ in ITEM_TYPES:
                    t = self.cfg["types"][key]
                    if not t["enabled"]:
                        continue
                    spots = self.not_done(key, self.find(key))
                    if not spots and self.cfg.get("auto") and self.cfg.get("auto_buy", True):
                        for buy_try in range(1, 5):  # เอฟเฟกต์ตีสำเร็จบังโล้นอยู่ -> รอให้หายแล้วลองซื้อใหม่
                            if not self.running.is_set():
                                return
                            if self.restock(key) and self.open_npc():
                                spots = self.not_done(key, self.find(key))
                                break
                            self.log(f"  ซื้อไม่สำเร็จ (ครั้งที่ {buy_try}/4) -> รอ 6 วิ แล้วลองใหม่")
                            if not self.wait(6.0):
                                return
                    self.log(f"[รอบ {rounds}] {name}: เจอ {len(spots)} ชิ้น (ที่ยังไม่ได้ตี)")
                    for i, p in enumerate(spots, 1):
                        for n in range(1, t["times"] + 1):
                            if not self.running.is_set():
                                return
                            if n > 1 and not self.item_at(key, p):
                                self.log(f"  {name} ชิ้น {i}: ช่องว่างแล้ว (แตก/หาย) -> ข้าม")
                                broken += 1
                                break
                            if self.cfg["click_npc_every_time"] and total > 0 and not self.open_npc():
                                return
                            self.log(f"  {name} ชิ้น {i}/{len(spots)} ครั้งที่ {n}/{t['times']}")
                            if not self.upgrade_once(p, n):
                                return
                            total += 1
                            if n == t["times"]:
                                if not self.survived(key, p):
                                    self.log(f"  {name} ชิ้น {i}: หายไปหลังตี (แตก)")
                                    broken += 1
                                else:  # ตีครบแล้วไม่แตก -> ย้ายลงแถวล่าง (ย้ายไม่ได้ก็จำช่องไว้) รอบหน้าข้าม
                                    self.log(f"  {name} ชิ้น {i}: ตีครบ {t['times']} ครั้งไม่แตก")
                                    if not self.move_to_done_row(key, p):
                                        if getattr(self, "done_full", False):
                                            self.log("=== หยุด: โซนเก็บของเต็ม ===")
                                            return
                                        self.done.setdefault(key, []).append(p)
                                    if not self.win_open and not self.open_npc():  # เปิดหน้าต่างเสริมพลังคืน
                                        return
                if not self.cfg.get("loop", True):
                    break
                if total == before:  # รอบนี้ไม่ได้ตีเลย (หาไม่เจอ/ซื้อไม่สำเร็จ) หยุด กันวนไม่รู้จบ
                    self.log("รอบนี้ไม่มีอะไรให้ตี -> หยุด")
                    break
            self.log(f"=== เสร็จ ตีไปทั้งหมด {total} ครั้ง / แตก {broken} ชิ้น ===")
        except Exception as e:
            self.log(f"ผิดพลาด: {e}")
        finally:
            if not self.running.is_set():
                self.log("=== หยุดแล้ว ===")
            self.running.clear()
            on_done()


# ====================================================================== UI
class App:
    def __init__(self, root):
        self.root = root
        self.ui_q = queue.Queue()
        root.title("ตีบวก ดาบ / เท้า / เกาะ")
        root.attributes("-topmost", True)
        root.resizable(False, False)
        self.bot = Bot(self.log)
        c = self.bot.cfg

        pad = {"padx": 6, "pady": 3}
        main = ttk.Frame(root, padding=8)
        main.pack(fill="both", expand=True)

        # ---- ไอเทม
        f_items = ttk.LabelFrame(main, text="ไอเทมที่จะตี")
        f_items.grid(row=0, column=0, sticky="nsew", **pad)
        self.icons, self.v_en, self.v_times = {}, {}, {}
        for i, (key, name, fn) in enumerate(ITEM_TYPES):
            path = os.path.join(ICON_DIR, fn)
            if os.path.exists(path):
                self.icons[key] = tk.PhotoImage(file=path)
                ttk.Label(f_items, image=self.icons[key]).grid(row=i, column=0, **pad)
            self.v_en[key] = tk.BooleanVar(value=c["types"][key]["enabled"])
            ttk.Checkbutton(f_items, text=name, variable=self.v_en[key]).grid(row=i, column=1, sticky="w", **pad)
            ttk.Label(f_items, text="ครั้ง/ชิ้น").grid(row=i, column=2, **pad)
            self.v_times[key] = tk.IntVar(value=c["types"][key]["times"])
            ttk.Spinbox(f_items, from_=1, to=99, width=4, textvariable=self.v_times[key]).grid(row=i, column=3, **pad)
            ttk.Button(f_items, text="ทดสอบหา", command=lambda k=key: self.test_find(k)).grid(row=i, column=4, **pad)

        # ---- ตำแหน่ง
        f_pos = ttk.LabelFrame(main, text="ตำแหน่ง (เอาเมาส์ชี้ในเกมแล้วกด Ctrl + ปุ่ม F)")
        f_pos.grid(row=0, column=1, sticky="nsew", **pad)
        self.pos_lbl = {}
        for i, (key, name, hk) in enumerate(POINTS):
            ttk.Label(f_pos, text=f"Ctrl+{hk}", width=8, foreground="#0a5").grid(row=i, column=0, **pad)
            ttk.Label(f_pos, text=name).grid(row=i, column=1, sticky="w", **pad)
            self.pos_lbl[key] = ttk.Label(f_pos, width=12)
            self.pos_lbl[key].grid(row=i, column=2, sticky="w", **pad)
        ttk.Label(f_pos, text="(F5/F6 ไม่ตั้งก็ได้ = หาทั้งจอ | F7 ไม่ตั้ง = ไม่คลิกเมนู | F8 = ชี้ที่กลางหัวข้อ 'ช่องสัมภาระ' ในที่ที่อยากให้วาง)", foreground="#888").grid(
            row=len(POINTS), column=0, columnspan=3, sticky="w", **pad)
        self.refresh_pos()

        # ---- ตัวเลือก
        f_opt = ttk.LabelFrame(main, text="ตัวเลือก")
        f_opt.grid(row=1, column=0, columnspan=2, sticky="nsew", **pad)
        self.v_charm = tk.BooleanVar(value=c["use_charm"])
        self.v_npc_each = tk.BooleanVar(value=c["click_npc_every_time"])
        self.v_buy = tk.BooleanVar(value=c.get("auto_buy", True))
        ttk.Checkbutton(f_opt, text="ซื้อเองเมื่อหมด  หิน", variable=self.v_buy).grid(
            row=4, column=0, sticky="w", **pad)
        self.v_buy_stone = tk.StringVar(value=str(c.get("buy_stone", 100)))
        self.v_buy_charm = tk.StringVar(value=str(c.get("buy_charm", 50)))
        self.v_buy_armor = tk.StringVar(value=str(c.get("buy_armor", 10)))
        ttk.Entry(f_opt, width=6, textvariable=self.v_buy_stone).grid(row=4, column=1, sticky="w", **pad)
        ttk.Label(f_opt, text="ยันต์(ครั้ง)").grid(row=4, column=2, sticky="e", **pad)
        f_b = ttk.Frame(f_opt)
        f_b.grid(row=4, column=3, sticky="w")
        ttk.Entry(f_b, width=5, textvariable=self.v_buy_charm).pack(side="left", padx=3)
        ttk.Label(f_b, text="ของที่ตี(ชิ้น)").pack(side="left")
        ttk.Entry(f_b, width=5, textvariable=self.v_buy_armor).pack(side="left", padx=3)
        self.v_stone_have = tk.StringVar(value=str(c.get("stone_have", 0)))
        self.v_stone_low = tk.StringVar(value=str(c.get("stone_low", 100)))
        f_s = ttk.Frame(f_opt)
        f_s.grid(row=6, column=0, columnspan=4, sticky="w", padx=6)
        ttk.Label(f_s, text="หินที่มีตอนนี้ (ไม่ต้องกรอก ถ้ามีหินในช่อง F1 - อ่านจากจอเอง)").pack(side="left")
        ttk.Entry(f_s, width=6, textvariable=self.v_stone_have).pack(side="left", padx=4)
        ttk.Label(f_s, text="ซื้อเมื่อเหลือต่ำกว่า").pack(side="left")
        ttk.Entry(f_s, width=6, textvariable=self.v_stone_low).pack(side="left", padx=4)
        self.v_loop = tk.BooleanVar(value=c.get("loop", True))
        ttk.Checkbutton(f_opt, text="ตีต่อเนื่อง (หมดแล้วซื้อใหม่ วนไปเรื่อยๆ จนกด F9)", variable=self.v_loop).grid(
            row=5, column=0, columnspan=4, sticky="w", **pad)
        self.v_auto = tk.BooleanVar(value=c.get("auto", True))
        ttk.Checkbutton(f_opt, text="หาตำแหน่งเอง (NPC/เมนู/หิน/ยันต์/ตกลง)", variable=self.v_auto).grid(
            row=3, column=0, columnspan=4, sticky="w", **pad)
        f_c = ttk.Frame(f_opt)
        f_c.grid(row=0, column=0, sticky="w", **pad)
        ttk.Checkbutton(f_c, text="ใช้ยันต์ ตั้งแต่ครั้งที่", variable=self.v_charm).pack(side="left")
        self.v_charm_from = tk.StringVar(value=str(c.get("charm_from", 0)))
        ttk.Entry(f_c, width=4, textvariable=self.v_charm_from).pack(side="left", padx=3)
        ttk.Label(f_c, text="(0 = ทุกครั้ง)").pack(side="left")
        ttk.Checkbutton(f_opt, text="คลิก NPC ใหม่ทุกครั้ง", variable=self.v_npc_each).grid(
            row=0, column=1, columnspan=2, sticky="w", **pad)
        self.v_num = {}
        nums = [("delay_click", "หน่วงคลิก (วิ)"), ("delay_after_npc", "รอหลังคลิก NPC"),
                ("delay_after_ok", "รอผลหลังกดตกลง"), ("threshold", "ความแม่นหาไอเทม (0-1)")]
        for i, (key, label) in enumerate(nums):
            r, col = 1 + i // 2, (i % 2) * 2
            ttk.Label(f_opt, text=label).grid(row=r, column=col, sticky="w", **pad)
            self.v_num[key] = tk.StringVar(value=str(c[key]))
            ttk.Entry(f_opt, width=6, textvariable=self.v_num[key]).grid(row=r, column=col + 1, sticky="w", **pad)

        # ---- ปุ่ม
        f_btn = ttk.Frame(main)
        f_btn.grid(row=2, column=0, columnspan=2, sticky="ew", **pad)
        self.btn_start = ttk.Button(f_btn, text="▶ เริ่ม (F9)", command=self.toggle)
        self.btn_start.pack(side="left", padx=4)
        ttk.Button(f_btn, text="บันทึกตั้งค่า", command=self.save).pack(side="left", padx=4)
        ttk.Label(f_btn, text="วิธีคลิก").pack(side="left", padx=(10, 2))
        self.v_method = tk.StringVar(value=str(c.get("method", 1)))
        ttk.Combobox(f_btn, width=3, state="readonly", values=["1", "2", "3"],
                     textvariable=self.v_method).pack(side="left")
        ttk.Button(f_btn, text="ทดสอบคลิก", command=self.test_click).pack(side="left", padx=4)
        ttk.Button(f_btn, text="ทดสอบหาตำแหน่ง", command=self.test_auto).pack(side="left", padx=4)
        self.status = ttk.Label(f_btn, text="พร้อม", foreground="#555")
        self.status.pack(side="left", padx=10)

        # ---- log
        self.txt = tk.Text(main, height=12, width=80, state="disabled", font=("Tahoma", 9))
        self.txt.grid(row=3, column=0, columnspan=2, sticky="nsew", **pad)

        self.log("ตั้งตำแหน่งด้วย Ctrl+F1..F8 แล้วกด F9 เพื่อเริ่ม/หยุด  (รันโปรแกรมแบบ Administrator)")
        self.ctrl_down = False
        self.listener = keyboard.Listener(on_press=self.on_key, on_release=self.on_key_up)
        self.listener.daemon = True
        self.listener.start()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(50, self.pump)

    # ---------- thread-safe ui
    def log(self, msg):
        line = time.strftime("%H:%M:%S ") + msg
        self.ui_q.put(lambda: self._append(line))

    def _append(self, line):
        self.txt.configure(state="normal")
        self.txt.insert("end", line + "\n")
        self.txt.see("end")
        self.txt.configure(state="disabled")

    def pump(self):
        try:
            while True:
                self.ui_q.get_nowait()()
        except queue.Empty:
            pass
        self.root.after(50, self.pump)

    def refresh_pos(self):
        for key, lbl in self.pos_lbl.items():
            p = self.bot.cfg[key]
            lbl.configure(text=f"{p[0]}, {p[1]}" if p else "— ยังไม่ตั้ง",
                          foreground="#000" if p else "#c00")

    # ---------- actions
    def sync(self):
        c = self.bot.cfg
        for key in self.v_en:
            c["types"][key]["enabled"] = self.v_en[key].get()
            try:
                c["types"][key]["times"] = max(1, int(self.v_times[key].get()))
            except (tk.TclError, ValueError):
                pass
        self.sync_stone()
        c["method"] = int(self.v_method.get())
        c["auto"] = self.v_auto.get()
        c["auto_buy"] = self.v_buy.get()
        c["loop"] = self.v_loop.get()
        for k, v in (("buy_stone", self.v_buy_stone), ("buy_charm", self.v_buy_charm),
                     ("buy_armor", self.v_buy_armor)):
            try:
                c[k] = max(1, int(v.get()))
            except ValueError:
                v.set(str(c.get(k, 100)))
        c["use_charm"] = self.v_charm.get()
        c["click_npc_every_time"] = self.v_npc_each.get()
        for key, v in self.v_num.items():
            try:
                c[key] = float(v.get())
            except ValueError:
                v.set(str(c[key]))

    def test_auto(self):
        """หา NPC/เมนู/หิน/ยันต์/ตกลง จากหน้าจอตอนนี้ แล้วรายงานตำแหน่ง (เมาส์ชี้ให้ดูทีละอัน)"""
        self.sync()

        def job():
            for n in ("npc", "menu", "stone", "charm", "ok"):
                reg = self.bot.region() if n in ("stone", "charm") else None
                p = self.bot.find_one(n, reg) or (self.bot.find_one(n) if reg else None)
                self.log(f"หา {n}: " + (f"เจอที่ {p}" if p else "ไม่เจอ (ต้องเปิดหน้าต่างนั้นอยู่บนจอ)"))
                if p:
                    mc.position = tuple(p)
                    time.sleep(0.8)
        threading.Thread(target=job, daemon=True).start()

    def test_click(self):
        """นับถอยหลัง 4 วิ ให้เอาเมาส์ไปวางบนของในเกม แล้วคลิก 1 ครั้งด้วยวิธีที่เลือก"""
        self.sync()

        def job():
            for s in (4, 3, 2, 1):
                self.log(f"ทดสอบคลิกใน {s} วิ - เอาเมาส์ไปวางบนของในเกม (เช่น หินแดงในกระเป๋า)")
                time.sleep(1)
            x, y = mc.position
            self.log(f"สลับเกม: {focus_game([x, y])}  คลิกที่ {int(x)},{int(y)} ด้วยวิธี {self.bot.cfg['method']}")
            time.sleep(0.5)
            self.bot.running.set()
            self.bot.click([int(x), int(y)], ctrl=True)
            self.bot.running.clear()
            self.log("คลิก (Ctrl+คลิก) แล้ว - ดูในเกมว่าของถูกยกขึ้น/ใส่ช่องหรือไม่")
        threading.Thread(target=job, daemon=True).start()

    def sync_stone(self):
        for k, v in (("stone_have", self.v_stone_have), ("stone_low", self.v_stone_low),
                     ("charm_from", self.v_charm_from)):
            try:
                self.bot.cfg[k] = max(0, int(v.get()))  # 0 ได้ (stone_have=0 = ไม่เช็คจำนวน)
            except ValueError:
                v.set(str(self.bot.cfg.get(k, 0)))

    def save(self):
        self.sync()
        self.bot.save()

    def toggle(self):
        if self.bot.running.is_set():
            self.bot.running.clear()
            self.status.configure(text="กำลังหยุด...")
            return
        self.sync()
        self.bot.running.set()
        self.btn_start.configure(text="■ หยุด (F9)")
        self.status.configure(text="กำลังตี...", foreground="#0a5")
        threading.Thread(target=self.bot.run, args=(lambda: self.ui_q.put(self.done),), daemon=True).start()

    def done(self):
        self.btn_start.configure(text="▶ เริ่ม (F9)")
        self.status.configure(text="พร้อม", foreground="#555")

    def test_find(self, key):
        self.sync()
        name = dict((k, n) for k, n, _ in ITEM_TYPES)[key]

        def job():
            spots = self.bot.find(key)
            self.log(f"ทดสอบหา {name}: เจอ {len(spots)} ชิ้น {spots[:8]}{' ...' if len(spots) > 8 else ''}")
            for p in spots[:20]:  # ชี้เมาส์ให้ดูทีละชิ้น
                mc.position = p
                time.sleep(0.25)
        threading.Thread(target=job, daemon=True).start()

    CTRL_KEYS = (keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r)

    def on_key_up(self, key):
        if key in self.CTRL_KEYS:
            self.ctrl_down = False

    def on_key(self, key):
        if key in self.CTRL_KEYS:
            self.ctrl_down = True
            return
        if key in HOTKEY_TO_POINT:
            # ต้องกด Ctrl ค้างไว้ และบอทไม่ได้ทำงานอยู่ (กัน F1-F8 ในเกมไปทับตำแหน่ง)
            if not self.ctrl_down or self.bot.running.is_set():
                return
            k = HOTKEY_TO_POINT[key]
            x, y = mc.position
            self.bot.cfg[k] = [int(x), int(y)]
            name = dict((a, b) for a, b, _ in POINTS)[k]
            self.log(f"ตั้ง {name} = {int(x)}, {int(y)}")
            self.ui_q.put(self.refresh_pos)
        elif key == keyboard.Key.f9:
            self.ui_q.put(self.toggle)

    def on_close(self):
        self.bot.running.clear()
        try:
            self.sync()
            self.bot.save()
        except Exception:
            pass
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()
