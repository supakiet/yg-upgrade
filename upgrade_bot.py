# -*- coding: utf-8 -*-
"""
บอทตีบวกของ (เสริมพลังยุทธภัณฑ์) - คลิก NPC โล้น > Ctrl+คลิก ไอเทม / หินแดง / ยันต์ > ตกลง

ปุ่มลัด (เอาเมาส์ไปชี้ตำแหน่งแล้วกด):
  F1  = บันทึกตำแหน่ง NPC โล้น (ตัว NPC)
  F2  = เพิ่มช่องไอเทมที่จะบวก (กดได้หลายช่อง)
  F3  = บันทึกช่องหินแดง
  F4  = บันทึกช่องยันต์
  F5  = บันทึกปุ่ม "ตกลง"
  F6  = ล้างรายการช่องไอเทมทั้งหมด
  F9  = เริ่ม / หยุด
  F10 = บันทึก config
  End = ออกจากโปรแกรม
"""
import json
import os
import sys
import threading
import time

from pynput import keyboard, mouse

# ---------------------------------------------------------------- settings
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "upgrade_config.json")

DEFAULT = {
    "npc": None,             # [x, y]
    "items": [],             # [[x, y], ...]
    "stone": None,           # หินแดง
    "charm": None,           # ยันต์
    "ok": None,              # ปุ่มตกลง
    "times_per_item": 1,     # ตีกี่ครั้งต่อ 1 ช่อง
    "use_charm": True,       # ใส่ยันต์ด้วยไหม
    "click_npc_every_time": False,  # คลิก NPC ใหม่ทุกครั้ง (ถ้าหน้าต่างปิดเองหลังตี)
    "delay_click": 0.25,     # หน่วงระหว่างคลิก (วินาที)
    "delay_after_npc": 1.0,  # รอหน้าต่างเปิดหลังคลิก NPC
    "delay_after_ok": 2.0,   # รอผลตีบวก
}

mc = mouse.Controller()
kc = keyboard.Controller()
cfg = dict(DEFAULT)
running = threading.Event()
quit_flag = threading.Event()


def load():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg.update(json.load(f))
        print(f"[โหลด config] {CONFIG_FILE}")


def save():
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print(f"[บันทึก config แล้ว] {CONFIG_FILE}")


def pos():
    x, y = mc.position
    return [int(x), int(y)]


# ---------------------------------------------------------------- actions
def wait(sec):
    """sleep ที่หยุดกลางทางได้"""
    end = time.time() + sec
    while time.time() < end:
        if not running.is_set() or quit_flag.is_set():
            return False
        time.sleep(0.02)
    return True


def click(p, ctrl=False, button=mouse.Button.left):
    mc.position = (p[0], p[1])
    time.sleep(0.08)
    if ctrl:
        kc.press(keyboard.Key.ctrl_l)
        time.sleep(0.05)
    mc.press(button)
    time.sleep(0.06)
    mc.release(button)
    if ctrl:
        time.sleep(0.05)
        kc.release(keyboard.Key.ctrl_l)
    return wait(cfg["delay_click"])


def open_npc():
    print("  - คลิก NPC")
    return click(cfg["npc"]) and wait(cfg["delay_after_npc"])


def upgrade_once(item):
    if not click(item, ctrl=True):        # ไอเทม
        return False
    if not click(cfg["stone"], ctrl=True):  # หินแดง
        return False
    if cfg["use_charm"] and cfg["charm"]:
        if not click(cfg["charm"], ctrl=True):  # ยันต์
            return False
    if not click(cfg["ok"]):               # ตกลง
        return False
    return wait(cfg["delay_after_ok"])


def check_ready():
    missing = [k for k in ("npc", "stone", "ok") if not cfg[k]]
    if cfg["use_charm"] and not cfg["charm"]:
        missing.append("charm")
    if not cfg["items"]:
        missing.append("items")
    if missing:
        print("[ยังตั้งค่าไม่ครบ]", ", ".join(missing))
        return False
    return True


def worker():
    while not quit_flag.is_set():
        running.wait(0.2)
        if not running.is_set():
            continue
        if not check_ready():
            running.clear()
            continue
        print("[เริ่มตีบวก]")
        ok = open_npc()
        total = 0
        for i, item in enumerate(cfg["items"], 1):
            for n in range(1, cfg["times_per_item"] + 1):
                if not ok or not running.is_set():
                    break
                if cfg["click_npc_every_time"] and total > 0:
                    ok = open_npc()
                    if not ok:
                        break
                total += 1
                print(f"  ช่อง {i}/{len(cfg['items'])} ครั้งที่ {n}/{cfg['times_per_item']}")
                ok = upgrade_once(item)
        print(f"[จบ] ตีไป {total} ครั้ง" if ok else "[หยุด]")
        running.clear()


# ---------------------------------------------------------------- hotkeys
def on_press(key):
    if key == keyboard.Key.f1:
        cfg["npc"] = pos(); print("NPC =", cfg["npc"])
    elif key == keyboard.Key.f2:
        cfg["items"].append(pos()); print(f"เพิ่มช่องไอเทม #{len(cfg['items'])} =", cfg["items"][-1])
    elif key == keyboard.Key.f3:
        cfg["stone"] = pos(); print("หินแดง =", cfg["stone"])
    elif key == keyboard.Key.f4:
        cfg["charm"] = pos(); print("ยันต์ =", cfg["charm"])
    elif key == keyboard.Key.f5:
        cfg["ok"] = pos(); print("ปุ่มตกลง =", cfg["ok"])
    elif key == keyboard.Key.f6:
        cfg["items"].clear(); print("ล้างช่องไอเทมแล้ว")
    elif key == keyboard.Key.f9:
        if running.is_set():
            running.clear(); print("[กำลังหยุด...]")
        else:
            running.set()
    elif key == keyboard.Key.f10:
        save()
    elif key == keyboard.Key.end:
        running.clear(); quit_flag.set()
        print("ออกจากโปรแกรม")
        return False


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    # ให้พิกัดเมาส์ตรงกับหน้าจอจริงเมื่อ Windows ตั้ง scale > 100%
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    load()
    print(__doc__)
    print("ตั้งค่าปัจจุบัน:", json.dumps(cfg, ensure_ascii=False))
    threading.Thread(target=worker, daemon=True).start()
    with keyboard.Listener(on_press=on_press) as listener:
        listener.join()


if __name__ == "__main__":
    main()
