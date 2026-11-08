#!/usr/bin/env python3
"""
Redmi Note 9 Pro (joyeuse) Flash Tool  (Fastboot + EDL + Backup)
- Fastboot: كتقرا flash_all.sh / flash_all.bat ديال ROM الرسمي وكتنفذ الأوامر
- EDL: كتغلّف أداة bkerler/edl  (pip install edl  أو git clone)
ما كاينش تجاوز ديال Xiaomi Auth: إلا الـ loader طلب تصريح، الأداة غادي توقف.
"""
import os, re, shlex, subprocess, threading, queue, sys
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# الأداة مقفولة على joyeuse فقط
ALLOWED_CODENAMES = {"joyeuse"}

# أوامر كتبقى ما كتتنفذش أبداً (قفل bootloader كيقدر يبريك الهاتف)
BLOCKED_PATTERNS = [r"\boem\s+lock\b", r"\bflashing\s+lock\b", r"\block\b"]

IS_WIN = os.name == "nt"
import time, hashlib, shutil

def base_dir():
    # مجلد الـ exe (إلا كانت الأداة مبنية بـ PyInstaller) أو مجلد السكريبت
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

def find_tool(name):
    """كيقلب أول على tools/ حدا الأداة، من بعد على PATH."""
    exe = name + (".exe" if IS_WIN else "")
    for d in (os.path.join(base_dir(), "tools"), base_dir()):
        cand = os.path.join(d, exe)
        if os.path.isfile(cand):
            return cand
    return shutil.which(name) or name

FASTBOOT = find_tool("fastboot")
EDL_CMD = [find_tool("edl")]

# partitions مهمة ما كتعوضش (IMEI, شبكة, معايرة...)
CRITICAL_PARTS = ["persist", "modemst1", "modemst2", "fsg", "modem",
                  "devinfo", "dtbo", "vbmeta", "boot"]


# ---------------------------------------------------------------- helpers
def run_cmd(args, log, cwd=None, stop_flag=None):
    """تنفيذ أمر وإرسال المخرجات للـ log. كيرجع (returncode, output)."""
    log("$ " + " ".join(shlex.quote(a) for a in args))
    try:
        p = subprocess.Popen(args, cwd=cwd, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True,
                             creationflags=0x08000000 if IS_WIN else 0)
    except FileNotFoundError:
        log(f"!! ما لقيتش الأداة: {args[0]} (تأكد أنها فـ PATH)")
        return 127, ""
    out = []
    for line in p.stdout:
        line = line.rstrip()
        out.append(line)
        log(line)
        if stop_flag and stop_flag.is_set():
            p.terminate()
            log("!! تم الإيقاف من طرف المستخدم")
            break
    return p.wait(), "\n".join(out)


def get_product(log):
    rc, out = run_cmd([FASTBOOT, "getvar", "product"], log)
    m = re.search(r"product:\s*(\S+)", out)
    return m.group(1).lower() if m else None


def parse_flash_script(rom_dir):
    """كيقرا flash_all.* وكيرجع قائمة ديال args (بدون كلمة fastboot)."""
    names = ["flash_all.bat", "flash_all.sh"] if IS_WIN else ["flash_all.sh", "flash_all.bat"]
    return _parse_first(rom_dir, names)


def parse_flash_script_keep_data(rom_dir):
    names = (["flash_all_except_storage.bat", "flash_all_except_data_storage.bat"] if IS_WIN
             else ["flash_all_except_storage.sh", "flash_all_except_data_storage.sh"])
    return _parse_first(rom_dir, names)


def _parse_first(rom_dir, names):
    for n in names:
        path = os.path.join(rom_dir, n)
        if os.path.isfile(path):
            return n, _parse_script(path)
    return None, []


def _parse_script(path):
    cmds = []
    with open(path, encoding="utf-8", errors="ignore") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith(("#", "::", "REM", "rem", "@echo")):
                continue
            m = re.search(r"fastboot(?:\.exe)?\"?\s+(.*)$", line)
            if not m:
                continue
            rest = m.group(1)
            rest = rest.replace("%*", "").replace("$*", "").replace("%~dp0", "")
            rest = re.sub(r'"?\$\(dirname "?\$0"?\)"?/?', "", rest)
            rest = re.sub(r"\$\{?\w+\}?", "", rest)
            rest = rest.replace("\\", "/")
            try:
                args = shlex.split(rest)
            except ValueError:
                continue
            if args:
                cmds.append(args)
    return cmds


def is_blocked(args):
    s = " ".join(args)
    return any(re.search(p, s) for p in BLOCKED_PATTERNS)


# ---------------------------------------------------------------- GUI
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Redmi Note 9 Pro (joyeuse) Flash Tool")
        self.geometry("760x640")
        self.q = queue.Queue()
        self.stop = threading.Event()
        self.worker = None

        nb = ttk.Notebook(self)
        nb.pack(fill="x", padx=8, pady=8)
        self._build_fastboot(nb)
        self._build_edl(nb)
        self._build_backup(nb)

        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8)
        ttk.Button(bar, text="إيقاف", command=self.stop.set).pack(side="right")
        ttk.Button(bar, text="فحص الجهاز (fastboot)", command=self.check_device).pack(side="left")

        self.logbox = tk.Text(self, height=20, state="disabled", bg="#111", fg="#ddd",
                              font=("Consolas", 10))
        self.logbox.pack(fill="both", expand=True, padx=8, pady=8)
        self.after(100, self._drain)

    # ---- tabs
    def _build_fastboot(self, nb):
        f = ttk.Frame(nb, padding=8)
        nb.add(f, text="Fastboot")
        self.rom_dir = tk.StringVar()
        self.keep_data = tk.BooleanVar(value=True)
        r = ttk.Frame(f); r.pack(fill="x")
        ttk.Label(r, text="مجلد ROM:").pack(side="left")
        ttk.Entry(r, textvariable=self.rom_dir).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(r, text="...", width=3,
                   command=lambda: self.rom_dir.set(filedialog.askdirectory() or self.rom_dir.get())
                   ).pack(side="left")
        ttk.Checkbutton(f, text="حافظ على البيانات (ما تمسحش userdata)",
                        variable=self.keep_data).pack(anchor="w", pady=4)
        ttk.Button(f, text="Flash ROM", command=self.start_fastboot).pack(anchor="w")
        ttk.Label(f, text="الهاتف خاصو يكون فـ Fastboot (Vol- + Power) والـ bootloader مفتوح.",
                  foreground="#888").pack(anchor="w", pady=(6, 0))

    def _build_edl(self, nb):
        f = ttk.Frame(nb, padding=8)
        nb.add(f, text="EDL (9008)")
        self.loader = tk.StringVar()
        self.img_dir = tk.StringVar()
        self.memory = tk.StringVar(value="auto")
        for label, var, is_dir in (("Loader (.elf/.mbn):", self.loader, False),
                                   ("مجلد الصور (rawprogram/patch):", self.img_dir, True)):
            r = ttk.Frame(f); r.pack(fill="x", pady=2)
            ttk.Label(r, text=label, width=28).pack(side="left")
            ttk.Entry(r, textvariable=var).pack(side="left", fill="x", expand=True, padx=4)
            ttk.Button(r, text="...", width=3,
                       command=lambda v=var, d=is_dir: v.set(
                           (filedialog.askdirectory() if d else filedialog.askopenfilename()) or v.get())
                       ).pack(side="left")
        r = ttk.Frame(f); r.pack(fill="x", pady=2)
        ttk.Label(r, text="نوع الذاكرة:", width=28).pack(side="left")
        ttk.Combobox(r, textvariable=self.memory, values=["auto", "ufs", "emmc"],
                     width=8, state="readonly").pack(side="left", padx=4)
        ttk.Button(f, text="Flash عبر EDL", command=self.start_edl).pack(anchor="w", pady=6)
        ttk.Label(f, text="خاصك loader مصرّح ليه. الأداة ما كتتجاوزش Xiaomi Auth.",
                  foreground="#888").pack(anchor="w")

    def _build_backup(self, nb):
        f = ttk.Frame(nb, padding=8)
        nb.add(f, text="Partitions / Backup")
        self.out_dir = tk.StringVar(value=os.path.join(base_dir(), "backup"))
        ttk.Label(f, text="كيستعمل الـ loader والذاكرة ديال تابة EDL. الهاتف فـ 9008.",
                  foreground="#888").pack(anchor="w")
        r = ttk.Frame(f); r.pack(fill="x", pady=3)
        ttk.Label(r, text="مجلد الحفظ:").pack(side="left")
        ttk.Entry(r, textvariable=self.out_dir).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(r, text="...", width=3,
                   command=lambda: self.out_dir.set(filedialog.askdirectory() or self.out_dir.get())
                   ).pack(side="left")
        b = ttk.Frame(f); b.pack(fill="x", pady=3)
        ttk.Button(b, text="قراءة partitions", command=self.read_gpt).pack(side="left", padx=2)
        ttk.Button(b, text="Backup المحدد", command=self.backup_selected).pack(side="left", padx=2)
        ttk.Button(b, text="Backup الأساسي", command=self.backup_critical).pack(side="left", padx=2)
        ttk.Button(b, text="Backup كامل (بلا userdata)", command=self.backup_full).pack(side="left", padx=2)
        self.part_list = tk.Listbox(f, selectmode="extended", height=6)
        self.part_list.pack(fill="x", pady=3)

    # ---- EDL helpers
    def _edl_args(self):
        loader = self.loader.get().strip()
        if not os.path.isfile(loader):
            messagebox.showerror("خطأ", "اختار loader فـ تابة EDL.")
            return None
        extra = [f"--loader={loader}"]
        if self.memory.get() != "auto":
            extra.append(f"--memory={self.memory.get()}")
        return extra

    def _new_backup_dir(self):
        d = os.path.join(self.out_dir.get().strip() or "backup",
                         "joyeuse_" + time.strftime("%Y%m%d_%H%M%S"))
        os.makedirs(d, exist_ok=True)
        return d

    def _write_manifest(self, d):
        lines = []
        for fn in sorted(os.listdir(d)):
            fp = os.path.join(d, fn)
            if os.path.isfile(fp) and fn != "SHA256SUMS.txt":
                h = hashlib.sha256()
                with open(fp, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        h.update(chunk)
                lines.append(f"{h.hexdigest()}  {fn}")
        with open(os.path.join(d, "SHA256SUMS.txt"), "w") as m:
            m.write("\n".join(lines) + "\n")
        self.log(f"تم حفظ SHA256SUMS.txt ({len(lines)} ملف) فـ {d}")

    def read_gpt(self):
        extra = self._edl_args()
        if extra is None:
            return

        def job():
            rc, out = run_cmd(EDL_CMD + ["printgpt"] + extra, self.log, stop_flag=self.stop)
            names = []
            for line in out.splitlines():
                m = re.match(r"^\s*([\w\-\.]+):\s+(?:Offset|Sector|Start)", line)
                if m and m.group(1) not in names:
                    names.append(m.group(1))
            self.after(0, lambda: self._fill_parts(names))
            self.log(f"لقيت {len(names)} partition." if names else
                     "!! ما قدرتش نقرا الأسماء. شوف الـ log (الصيغة كتتبدل بين إصدارات edl).")
        self._spawn(job)

    def _fill_parts(self, names):
        self.part_list.delete(0, "end")
        for n in names:
            self.part_list.insert("end", n)

    def _backup_parts(self, parts):
        extra = self._edl_args()
        if extra is None or not parts:
            return
        d = self._new_backup_dir()

        def job():
            for i, part in enumerate(parts, 1):
                if self.stop.is_set():
                    break
                self.log(f"[{i}/{len(parts)}] {part}")
                out_file = os.path.join(d, part + ".img")
                rc, _ = run_cmd(EDL_CMD + ["r", part, out_file] + extra,
                                self.log, stop_flag=self.stop)
                if rc != 0:
                    self.log(f"!! فشل backup ديال {part} (code {rc})")
            self._write_manifest(d)
            self.log("== سالا backup ==")
        self._spawn(job)

    def backup_selected(self):
        parts = [self.part_list.get(i) for i in self.part_list.curselection()]
        if not parts:
            return messagebox.showinfo("تنبيه", "حدد partitions من اللائحة (اقرا partitions أولاً).")
        self._backup_parts(parts)

    def backup_critical(self):
        self._backup_parts(CRITICAL_PARTS)

    def backup_full(self):
        extra = self._edl_args()
        if extra is None:
            return
        if not messagebox.askyesno("تأكيد", "Backup كامل غادي ياخد وقت وبزاف ديال المساحة "
                                            "(بلا userdata). تكمل؟"):
            return
        d = self._new_backup_dir()

        def job():
            rc, _ = run_cmd(EDL_CMD + ["rl", d, "--skip=userdata"] + extra,
                            self.log, stop_flag=self.stop)
            self._write_manifest(d)
            self.log("== سالا backup ==" if rc == 0 else f"!! فشل (code {rc})")
        self._spawn(job)

    # ---- logging
    def log(self, msg):
        self.q.put(msg)

    def _drain(self):
        try:
            while True:
                msg = self.q.get_nowait()
                self.logbox.config(state="normal")
                self.logbox.insert("end", msg + "\n")
                self.logbox.see("end")
                self.logbox.config(state="disabled")
        except queue.Empty:
            pass
        self.after(100, self._drain)

    def _spawn(self, fn):
        if self.worker and self.worker.is_alive():
            messagebox.showwarning("مشغول", "كاين عملية خدامة دابا.")
            return
        self.stop.clear()
        self.worker = threading.Thread(target=fn, daemon=True)
        self.worker.start()

    # ---- actions
    def check_device(self):
        def job():
            p = get_product(self.log)
            if p is None:
                self.log("!! ما تلقى حتى جهاز فـ Fastboot.")
            elif p in ALLOWED_CODENAMES:
                self.log(f"OK: الجهاز {p} مدعوم.")
            else:
                self.log(f"!! الجهاز '{p}' ماشي Redmi Note 9 Pro. الأداة غادي تمنع الفلاش.")
        self._spawn(job)

    def start_fastboot(self):
        rom = self.rom_dir.get().strip()
        if not os.path.isdir(rom):
            return messagebox.showerror("خطأ", "اختار مجلد ROM صحيح.")
        if self.keep_data.get():
            name, cmds = parse_flash_script_keep_data(rom)
            if not cmds:
                name, cmds = parse_flash_script(rom)
        else:
            name, cmds = parse_flash_script(rom)
        if not cmds:
            return messagebox.showerror("خطأ", "ما لقيتش flash_all.sh/.bat فـ المجلد.")
        todo = [c for c in cmds if not is_blocked(c)]
        skipped = len(cmds) - len(todo)
        wipes = [c for c in todo if c[0] == "erase" and "userdata" in c]
        msg = f"السكريبت: {name}\nعدد الأوامر: {len(todo)}"
        if skipped:
            msg += f"\n(تم تجاهل {skipped} أمر ديال القفل)"
        if wipes:
            msg += "\n\n⚠ هاد السكريبت غادي يمسح userdata!"
        if not messagebox.askyesno("تأكيد", msg + "\n\nواش تكمل؟"):
            return

        def job():
            prod = get_product(self.log)
            if prod not in ALLOWED_CODENAMES:
                self.log(f"!! الجهاز '{prod}' غير مدعوم. تم الإلغاء.")
                return
            # تأكد أن ROM كيوافق الجهاز (إلا كان كاين android-info.txt / ملفات)
            for i, args in enumerate(todo, 1):
                if self.stop.is_set():
                    break
                self.log(f"[{i}/{len(todo)}]")
                rc, _ = run_cmd([FASTBOOT] + args, self.log, cwd=rom, stop_flag=self.stop)
                if rc != 0:
                    self.log(f"!! فشل الأمر (code {rc}). تم التوقف.")
                    return
            self.log("== سالات العملية ==")
        self._spawn(job)

    def start_edl(self):
        loader, imgs = self.loader.get().strip(), self.img_dir.get().strip()
        if not os.path.isfile(loader) or not os.path.isdir(imgs):
            return messagebox.showerror("خطأ", "اختار loader ومجلد الصور.")
        raw = sorted(f for f in os.listdir(imgs) if re.match(r"rawprogram.*\.xml$", f))
        patch = sorted(f for f in os.listdir(imgs) if re.match(r"patch.*\.xml$", f))
        if not raw or not patch:
            return messagebox.showerror("خطأ", "ما لقيتش rawprogram*.xml / patch*.xml.")
        if not messagebox.askyesno("تأكيد", "الفلاش عبر EDL كيكتب مباشرة على الذاكرة.\n"
                                            "غلط واحد كيقدر يبريك الهاتف. تكمل؟"):
            return

        def job():
            cmd = EDL_CMD + ["qfil", ",".join(os.path.join(imgs, r) for r in raw),
                             ",".join(os.path.join(imgs, p) for p in patch),
                             imgs, f"--loader={loader}"]
            if self.memory.get() != "auto":
                cmd.append(f"--memory={self.memory.get()}")
            rc, out = run_cmd(cmd, self.log, stop_flag=self.stop)
            low = out.lower()
            if "auth" in low or "sign" in low:
                self.log("!! الـ loader كيطلب تصريح Xiaomi (Authorized Account). "
                         "خاصك استعمل حساب مصرّح ليه.")
            self.log("== سالات العملية ==" if rc == 0 else f"!! فشل (code {rc})")
        self._spawn(job)


if __name__ == "__main__":
    App().mainloop()
