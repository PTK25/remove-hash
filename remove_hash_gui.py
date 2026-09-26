#!/usr/bin/env python3
"""
Interface gráfica — Removedor de Metadados & Alterador de Hash
macOS · tkinter (biblioteca padrão) · sem dependências pip

Uso:
  python3 remove_hash_gui.py [arquivo_ou_pasta ...]
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, font as tkfont

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import remove_hash_core as core  # noqa: E402

APP_NAME = "Removedor de Metadados"
GITHUB_URL = "https://github.com/PTK25/remove-hash"

# ─────────────────────────────────────────────────────────
# PALETA
# ─────────────────────────────────────────────────────────

P = {
    "bg": "#151517",
    "card": "#1e1e22",
    "card2": "#28282e",
    "border": "#35353c",
    "accent": "#0a84ff",
    "accent_hover": "#3d9bff",
    "accent_down": "#0060d0",
    "text": "#f2f2f7",
    "dim": "#8e8e93",
    "green": "#30d158",
    "red": "#ff453a",
    "yellow": "#ffd60a",
    "orange": "#ff9f0a",
    "select": "#2a4a7a",
}

KIND_LABEL = {"video": "Vídeo", "image": "Foto", "audio": "Áudio", "unknown": "?"}


def open_in_finder(path: str, reveal: bool = False) -> None:
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path] if reveal else ["open", path])
        elif sys.platform.startswith("linux"):
            subprocess.Popen(["xdg-open", os.path.dirname(path) if reveal else path])
        else:
            os.startfile(path)  # type: ignore[attr-defined]
    except Exception:
        pass


def fmt_time(sec: float) -> str:
    m, s = divmod(max(0.0, sec), 60)
    return f"{int(m)}:{s:04.1f}"


def parse_time(text: str) -> float:
    """Aceita "75", "75.5", "1:15" ou "1:15.5" (vírgula também vale como decimal)."""
    parts = text.strip().replace(",", ".").split(":")
    if not 1 <= len(parts) <= 3 or any(p.strip() == "" for p in parts):
        raise ValueError(text)
    total = 0.0
    for p in parts:
        total = total * 60 + float(p)
    if total < 0:
        raise ValueError(text)
    return total


# ─────────────────────────────────────────────────────────
# CORTE RÁPIDO
# ─────────────────────────────────────────────────────────

class TrimDialog:
    """Escolhe início/fim de um vídeo, com prévia do quadro em cada ponto."""

    PREVIEW_W = 360

    def __init__(self, app: "App", path: str, duration: float):
        self.app, self.path, self.duration = app, path, duration
        start, end = app.trims.get(path, (0.0, 0.0))
        self.start = tk.DoubleVar(value=start)
        self.end = tk.DoubleVar(value=end or duration)
        self._after: dict[str, str] = {}
        self._photos: dict[str, tk.PhotoImage] = {}
        self._tmp: list[str] = []
        self._seq = {"start": 0, "end": 0}          # descarta prévias que chegam fora de ordem
        self._ready: queue.Queue = queue.Queue()     # (chave, seq, png) vindos das threads do ffmpeg
        pw, ph = self.PREVIEW_W, self.PREVIEW_W * 9 // 16
        self._blank = tk.PhotoImage(width=pw, height=ph)  # fixa o tamanho em pixels antes da 1ª prévia

        win = self.win = tk.Toplevel(app.root)
        win.title(f"Cortar — {os.path.basename(path)}")
        win.configure(bg=P["bg"])
        win.transient(app.root)
        win.resizable(False, False)
        frame = ttk.Frame(win, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=f"Duração: {fmt_time(duration)}  ·  arraste ou digite (ex.: 1:15.5)",
                  style="Dim.TLabel").pack(anchor="w")

        cols = ttk.Frame(frame)
        cols.pack(fill="x", pady=(12, 0))
        self.widgets = {}
        for col, (key, label, var) in enumerate((("start", "Início", self.start), ("end", "Fim", self.end))):
            box = ttk.Frame(cols)
            box.grid(row=0, column=col, padx=(0 if col == 0 else 16, 0), sticky="n")
            ttk.Label(box, text=label).pack(anchor="w")
            preview = tk.Label(box, bg=P["card"], image=self._blank, text="carregando…", fg=P["dim"],
                               compound="center", bd=0, width=pw, height=ph)
            preview.pack(pady=(6, 6))
            scale = ttk.Scale(box, from_=0, to=duration, variable=var, orient="horizontal",
                              length=self.PREVIEW_W, command=lambda _v, k=key: self._on_scale(k))
            scale.pack()
            entry = ttk.Entry(box, width=10, justify="center")
            entry.pack(pady=(6, 0))
            entry.bind("<Return>", lambda _e, k=key: self._on_entry(k))
            entry.bind("<FocusOut>", lambda _e, k=key: self._on_entry(k))
            self.widgets[key] = (preview, entry)
            self._sync_entry(key)
            self._schedule_preview(key, 0)

        self.lbl_len = ttk.Label(frame, style="Dim.TLabel")
        self.lbl_len.pack(anchor="w", pady=(12, 0))
        self._update_len()

        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(12, 0))
        ttk.Button(row, text="Aplicar", style="Accent.TButton", command=self._apply).pack(side="right")
        ttk.Button(row, text="Cancelar", command=self._close).pack(side="right", padx=(0, 8))
        ttk.Button(row, text="Remover corte", command=self._clear).pack(side="left")
        win.bind("<Escape>", lambda e: self._close())
        win.protocol("WM_DELETE_WINDOW", self._close)
        win.grab_set()
        self._poll()

    def _var(self, key: str) -> tk.DoubleVar:
        return self.start if key == "start" else self.end

    def _sync_entry(self, key: str):
        entry = self.widgets[key][1]
        entry.delete(0, "end")
        entry.insert(0, fmt_time(self._var(key).get()))

    def _update_len(self):
        length = self.end.get() - self.start.get()
        self.lbl_len.configure(text=f"Trecho final: {fmt_time(max(0.0, length))}",
                               style="Dim.TLabel" if length > 0 else "Err.TLabel")

    def _on_scale(self, key: str):
        self._sync_entry(key)
        self._update_len()
        self._schedule_preview(key)

    def _on_entry(self, key: str):
        try:
            value = min(self.duration, parse_time(self.widgets[key][1].get()))
        except ValueError:
            value = self._var(key).get()
        self._var(key).set(value)
        self._on_scale(key)

    def _schedule_preview(self, key: str, delay: int = 250):
        if key in self._after:
            self.win.after_cancel(self._after[key])
        self._after[key] = self.win.after(delay, lambda: self._render_preview(key))

    def _render_preview(self, key: str):
        self._after.pop(key, None)
        # último quadro: recua um pouco para o ffmpeg ainda achar imagem
        t = min(self._var(key).get(), max(0.0, self.duration - 0.1))
        ffmpeg = core.find_ffmpeg()
        if not ffmpeg:
            return
        fd, png = tempfile.mkstemp(suffix=".png", prefix="remove-hash-trim-")
        os.close(fd)
        self._tmp.append(png)
        self._seq[key] += 1
        seq = self._seq[key]
        size = f"{self.PREVIEW_W}:{self.PREVIEW_W * 9 // 16}"

        def work():
            subprocess.run([ffmpeg, "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", self.path, "-frames:v", "1",
                            "-vf", f"scale={size}:force_original_aspect_ratio=decrease", png],
                           capture_output=True, timeout=30)
            self._ready.put((key, seq, png))  # Tk só é tocado na thread principal (_poll)

        threading.Thread(target=work, daemon=True).start()

    def _poll(self):
        if not self.win.winfo_exists():
            return
        try:
            while True:
                key, seq, png = self._ready.get_nowait()
                if seq == self._seq[key]:
                    self._show_preview(key, png)
        except queue.Empty:
            pass
        self._after["poll"] = self.win.after(60, self._poll)

    def _show_preview(self, key: str, png: str):
        preview = self.widgets[key][0]
        try:
            photo = tk.PhotoImage(file=png)
        except tk.TclError:
            preview.configure(image=self._blank, text="sem prévia")
            return
        self._photos[key] = photo  # mantém a referência viva
        preview.configure(image=photo, text="")

    def _apply(self):
        for key in ("start", "end"):
            self._on_entry(key)
        start, end = self.start.get(), self.end.get()
        if end - start < 0.1:
            messagebox.showwarning("Cortar vídeo", "O fim precisa ser depois do início.", parent=self.win)
            return
        start = 0.0 if start < 0.05 else round(start, 3)
        end = 0.0 if end > self.duration - 0.05 else round(end, 3)  # 0 = até o final
        self.app.set_trim(self.path, (start, end) if (start or end) else None)
        self._close()

    def _clear(self):
        self.app.set_trim(self.path, None)
        self._close()

    def _close(self):
        for a in self._after.values():
            self.win.after_cancel(a)
        self.win.grab_release()
        self.win.destroy()
        for f in self._tmp:
            try:
                os.remove(f)
            except OSError:
                pass


# ─────────────────────────────────────────────────────────
# APLICAÇÃO
# ─────────────────────────────────────────────────────────

class App:
    def __init__(self, root: tk.Tk, initial_paths: list[str] | None = None):
        self.root = root
        self.root.title(APP_NAME)
        self.root.configure(bg=P["bg"])
        self.root.minsize(760, 560)

        w, h = 900, 660
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        self.root.geometry(f"{w}x{h}+{(sw - w) // 2}+{max(0, (sh - h) // 2 - 40)}")

        # estado
        self.files: list[str] = []
        self.rows: dict[str, str] = {}          # caminho → item id da tabela
        self.results: dict[str, core.FileResult] = {}
        self.output_dir: str | None = None
        self.is_processing = False
        self.cancel_event = threading.Event()
        self.queue: queue.Queue = queue.Queue()
        self.start_time = 0.0
        self.done_count = 0

        self.output_path = tk.StringVar(value=os.path.expanduser("~/Desktop"))
        self.keep_icc = tk.BooleanVar(value=True)
        self.reencode = tk.BooleanVar(value=True)
        self.open_after = tk.BooleanVar(value=True)
        self.watermark_text = tk.StringVar(value="")
        self.watermark_opacity = tk.DoubleVar(value=0.5)
        self.watermark_position = tk.StringVar(value="Centro")
        self.watermark_size = tk.IntVar(value=36)
        self.remove_audio = tk.BooleanVar(value=False)
        self.trims: dict[str, tuple[float, float]] = {}  # caminho → (início, fim) em s; fim 0 = até o final

        self._setup_fonts()
        self._setup_style()
        self._build_menu()
        self._build_ui()
        self._set_icon()
        self._refresh_ffmpeg_status()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        try:
            self.root.createcommand("tk::mac::Quit", self._on_close)
        except tk.TclError:
            pass

        if initial_paths:
            self.add_paths(initial_paths)
        self.root.after(80, self._poll_queue)

    # ── aparência ──

    def _setup_fonts(self):
        base = tkfont.nametofont("TkDefaultFont")
        family = base.actual("family")
        mono = tkfont.nametofont("TkFixedFont").actual("family")
        self.f_title = tkfont.Font(family=family, size=21, weight="bold")
        self.f_body = tkfont.Font(family=family, size=13)
        self.f_bold = tkfont.Font(family=family, size=13, weight="bold")
        self.f_small = tkfont.Font(family=family, size=11)
        self.f_button = tkfont.Font(family=family, size=13, weight="bold")
        self.f_mono = tkfont.Font(family=mono, size=11)
        self.f_mono_small = tkfont.Font(family=mono, size=10)

    def _setup_style(self):
        s = ttk.Style()
        try:
            s.theme_use("clam")
        except tk.TclError:
            pass
        s.configure(".", background=P["bg"], foreground=P["text"], font=self.f_body, borderwidth=0)
        s.configure("TFrame", background=P["bg"])
        s.configure("Card.TFrame", background=P["card"])
        s.configure("TLabel", background=P["bg"], foreground=P["text"], font=self.f_body)
        s.configure("Title.TLabel", font=self.f_title, foreground="#ffffff")
        s.configure("Dim.TLabel", foreground=P["dim"], font=self.f_small)
        s.configure("Card.TLabel", background=P["card"], foreground=P["text"])
        s.configure("CardDim.TLabel", background=P["card"], foreground=P["dim"], font=self.f_small)
        s.configure("Status.TLabel", foreground=P["dim"], font=self.f_small)
        s.configure("Ok.TLabel", foreground=P["green"], font=self.f_small)
        s.configure("Err.TLabel", foreground=P["red"], font=self.f_small)
        s.configure("Warn.TLabel", foreground=P["yellow"], font=self.f_small)

        s.configure("TButton", background=P["card2"], foreground=P["text"], font=self.f_body,
                    padding=(14, 7), borderwidth=0, focusthickness=0, relief="flat")
        s.map("TButton",
              background=[("disabled", P["card"]), ("pressed", P["border"]), ("active", "#33333a")],
              foreground=[("disabled", "#5c5c62")])
        s.configure("Accent.TButton", background=P["accent"], foreground="#ffffff", font=self.f_button,
                    padding=(22, 10))
        s.map("Accent.TButton",
              background=[("disabled", "#2b3a4f"), ("pressed", P["accent_down"]), ("active", P["accent_hover"])],
              foreground=[("disabled", "#7d8797")])
        s.configure("Danger.TButton", background=P["card2"], foreground=P["red"])
        s.map("Danger.TButton", foreground=[("disabled", "#5c5c62")])

        s.configure("TCheckbutton", background=P["bg"], foreground=P["text"], font=self.f_small,
                    indicatorbackground=P["card2"], indicatorforeground=P["text"], focusthickness=0, padding=(2, 2))
        s.map("TCheckbutton",
              background=[("active", P["bg"])],
              indicatorbackground=[("selected", P["accent"]), ("active", P["border"])],
              indicatorforeground=[("selected", "#ffffff")])

        s.configure("TEntry", fieldbackground=P["card2"], foreground=P["text"], insertcolor=P["text"],
                    bordercolor=P["border"], lightcolor=P["border"], darkcolor=P["border"], padding=(8, 6))
        s.map("TEntry", bordercolor=[("focus", P["accent"])], lightcolor=[("focus", P["accent"])],
              darkcolor=[("focus", P["accent"])])

        s.configure("Treeview", background=P["card"], fieldbackground=P["card"], foreground=P["text"],
                    rowheight=28, borderwidth=0, font=self.f_body)
        s.map("Treeview", background=[("selected", P["select"])], foreground=[("selected", "#ffffff")])
        s.configure("Treeview.Heading", background=P["card2"], foreground=P["dim"], font=self.f_small,
                    relief="flat", padding=(8, 6))
        s.map("Treeview.Heading", background=[("active", P["card2"])])
        s.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])

        s.configure("Vertical.TScrollbar", background=P["card2"], troughcolor=P["card"], bordercolor=P["card"],
                    arrowcolor=P["dim"], relief="flat")
        s.map("Vertical.TScrollbar", background=[("active", P["border"])])

        s.configure("Accent.Horizontal.TProgressbar", troughcolor=P["card2"], background=P["accent"],
                    bordercolor=P["card2"], lightcolor=P["accent"], darkcolor=P["accent"], thickness=8)

    def _set_icon(self):
        for cand in ("icon.png", os.path.join("assets", "icon.png")):
            path = os.path.join(os.path.dirname(os.path.realpath(__file__)), cand)
            if os.path.exists(path):
                try:
                    self._icon_img = tk.PhotoImage(file=path)
                    self.root.iconphoto(True, self._icon_img)
                except tk.TclError:
                    pass
                break

    # ── menu ──

    def _build_menu(self):
        menubar = tk.Menu(self.root)
        if sys.platform == "darwin":
            apple = tk.Menu(menubar, name="apple")
            apple.add_command(label=f"Sobre {APP_NAME}", command=self._about)
            apple.add_separator()
            menubar.add_cascade(menu=apple)
        m_file = tk.Menu(menubar, tearoff=0)
        m_file.add_command(label="Adicionar arquivos…", accelerator="⌘O", command=self.pick_files)
        m_file.add_command(label="Adicionar pasta…", accelerator="⇧⌘O", command=self.pick_folder)
        m_file.add_separator()
        m_file.add_command(label="Inspecionar metadados de um arquivo…", accelerator="⌘I", command=self.inspect_dialog)
        m_file.add_separator()
        m_file.add_command(label="Limpar lista", command=self.clear_list)
        menubar.add_cascade(label="Arquivo", menu=m_file)
        m_help = tk.Menu(menubar, tearoff=0)
        m_help.add_command(label="Como funciona", command=self._how_it_works)
        m_help.add_command(label="Página no GitHub", command=lambda: webbrowser.open(GITHUB_URL))
        if sys.platform != "darwin":
            m_help.add_command(label="Sobre", command=self._about)
        menubar.add_cascade(label="Ajuda", menu=m_help)
        self.root.config(menu=menubar)

        mod = "Command" if sys.platform == "darwin" else "Control"
        self.root.bind_all(f"<{mod}-o>", lambda e: self.pick_files())
        self.root.bind_all(f"<{mod}-O>", lambda e: self.pick_folder())
        self.root.bind_all(f"<{mod}-i>", lambda e: self.inspect_dialog())
        self.root.bind_all(f"<{mod}-Return>", lambda e: self.start())
        self.root.bind_all("<BackSpace>", self._remove_selected)
        self.root.bind_all("<Delete>", self._remove_selected)

    # ── layout ──

    def _build_ui(self):
        outer = ttk.Frame(self.root, padding=(24, 18, 24, 18))
        outer.pack(fill="both", expand=True)

        # Cabeçalho
        head = ttk.Frame(outer)
        head.pack(fill="x")
        left = ttk.Frame(head)
        left.pack(side="left", fill="x", expand=True)
        ttk.Label(left, text=APP_NAME, style="Title.TLabel").pack(anchor="w")
        ttk.Label(left, text="Remove EXIF, GPS, XMP, datas e tags de fotos, vídeos e áudios · "
                             "altera o hash · 100% offline", style="Dim.TLabel").pack(anchor="w", pady=(2, 0))
        right = ttk.Frame(head)
        right.pack(side="right", anchor="n")
        self.lbl_ffmpeg = ttk.Label(right, text="", style="Dim.TLabel")
        self.lbl_ffmpeg.pack(anchor="e")
        self.btn_ffmpeg_help = ttk.Button(right, text="Como instalar o FFmpeg", command=self._ffmpeg_help)

        # Barra de ferramentas
        tools = ttk.Frame(outer)
        tools.pack(fill="x", pady=(16, 8))
        self.btn_add_files = ttk.Button(tools, text="＋ Arquivos", command=self.pick_files)
        self.btn_add_files.pack(side="left")
        self.btn_add_folder = ttk.Button(tools, text="＋ Pasta", command=self.pick_folder)
        self.btn_add_folder.pack(side="left", padx=(8, 0))
        self.btn_clear = ttk.Button(tools, text="Limpar", command=self.clear_list)
        self.btn_clear.pack(side="left", padx=(8, 0))
        self.btn_trim = ttk.Button(tools, text="✂ Cortar vídeo", command=self.open_trimmer, state="disabled")
        self.btn_trim.pack(side="left", padx=(8, 0))
        self.lbl_count = ttk.Label(tools, text="Nenhum arquivo", style="Dim.TLabel")
        self.lbl_count.pack(side="right")

        # Tabela
        table_wrap = tk.Frame(outer, bg=P["border"], padx=1, pady=1)
        table_wrap.pack(fill="both", expand=True)
        table = ttk.Frame(table_wrap, style="Card.TFrame")
        table.pack(fill="both", expand=True)
        cols = ("name", "kind", "size", "status", "hash")
        self.tree = ttk.Treeview(table, columns=cols, show="headings", selectmode="extended")
        self.tree.heading("name", text="Arquivo", anchor="w")
        self.tree.heading("kind", text="Tipo", anchor="w")
        self.tree.heading("size", text="Tamanho", anchor="e")
        self.tree.heading("status", text="Status", anchor="w")
        self.tree.heading("hash", text="Novo SHA-256", anchor="w")
        self.tree.column("name", width=320, minwidth=160, anchor="w", stretch=True)
        self.tree.column("kind", width=70, minwidth=60, anchor="w", stretch=False)
        self.tree.column("size", width=90, minwidth=80, anchor="e", stretch=False)
        self.tree.column("status", width=170, minwidth=120, anchor="w", stretch=False)
        self.tree.column("hash", width=150, minwidth=110, anchor="w", stretch=False)
        vsb = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.tag_configure("pending", foreground=P["dim"])
        self.tree.tag_configure("working", foreground=P["yellow"])
        self.tree.tag_configure("ok", foreground=P["green"])
        self.tree.tag_configure("warn", foreground=P["orange"])
        self.tree.tag_configure("error", foreground=P["red"])
        self.tree.bind("<Double-1>", self._show_details)
        self.tree.bind("<Return>", self._show_details)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        self.lbl_empty = tk.Label(
            table, text="Adicione arquivos ou uma pasta para começar\n\n"
                        "Fotos: JPG · PNG · WebP · GIF · HEIC · TIFF · RAW\n"
                        "Vídeos: MP4 · MOV · MKV · WebM · AVI …\n"
                        "Áudios: MP3 · M4A · FLAC · WAV …",
            bg=P["card"], fg=P["dim"], font=self.f_small, justify="center")
        self.lbl_empty.place(relx=0.5, rely=0.5, anchor="center")

        # Destino
        dest = ttk.Frame(outer)
        dest.pack(fill="x", pady=(14, 0))
        ttk.Label(dest, text="Destino", style="Dim.TLabel").pack(side="left")
        self.entry_out = ttk.Entry(dest, textvariable=self.output_path, font=self.f_mono)
        self.entry_out.pack(side="left", fill="x", expand=True, padx=(10, 8))
        self.btn_out = ttk.Button(dest, text="Escolher…", command=self.pick_output)
        self.btn_out.pack(side="left")

        # Opções
        opts = ttk.Frame(outer)
        opts.pack(fill="x", pady=(8, 0))
        self.chk_icc = ttk.Checkbutton(opts, text="Manter perfil de cor (ICC)", variable=self.keep_icc)
        self.chk_icc.pack(side="left")
        self.chk_reenc = ttk.Checkbutton(opts, text="Recodificar vídeo se a cópia direta falhar", variable=self.reencode)
        self.chk_reenc.pack(side="left", padx=(18, 0))
        self.chk_open = ttk.Checkbutton(opts, text="Abrir pasta ao concluir", variable=self.open_after)
        self.chk_open.pack(side="left", padx=(18, 0))
        self.chk_audio = ttk.Checkbutton(opts, text="Remover áudio (vídeos)", variable=self.remove_audio)
        self.chk_audio.pack(side="left", padx=(18, 0))

        # Marca d'água
        wm_frame = ttk.Frame(outer)
        wm_frame.pack(fill="x", pady=(8, 0))
        ttk.Label(wm_frame, text="Marca d'água (vídeos):", style="Dim.TLabel").pack(side="left")
        self.entry_wm = ttk.Entry(wm_frame, textvariable=self.watermark_text, width=15)
        self.entry_wm.pack(side="left", padx=(8, 8))
        
        ttk.Label(wm_frame, text="Tamanho:", style="Dim.TLabel").pack(side="left")
        self.spin_wm_size = ttk.Spinbox(wm_frame, from_=10, to=200, increment=2, textvariable=self.watermark_size, width=4)
        self.spin_wm_size.pack(side="left", padx=(4, 8))
        
        ttk.Label(wm_frame, text="Transp.:", style="Dim.TLabel").pack(side="left")
        self.scale_wm_opacity = ttk.Scale(wm_frame, from_=0.1, to=1.0, variable=self.watermark_opacity, orient="horizontal", length=80)
        self.scale_wm_opacity.pack(side="left", padx=(4, 8))
        
        ttk.Label(wm_frame, text="Posição:", style="Dim.TLabel").pack(side="left")
        self.combo_wm_pos = ttk.Combobox(wm_frame, textvariable=self.watermark_position, values=["Centro", "Repetir"], state="readonly", width=8)
        self.combo_wm_pos.pack(side="left", padx=(4, 0))

        # Progresso
        prog = ttk.Frame(outer)
        prog.pack(fill="x", pady=(16, 0))
        self.progress_var = tk.DoubleVar(value=0)
        self.progress = ttk.Progressbar(prog, variable=self.progress_var, maximum=100,
                                        style="Accent.Horizontal.TProgressbar")
        self.progress.pack(fill="x")
        status_row = ttk.Frame(prog)
        status_row.pack(fill="x", pady=(6, 0))
        self.lbl_status = ttk.Label(status_row, text="Pronto", style="Status.TLabel")
        self.lbl_status.pack(side="left")
        self.lbl_time = ttk.Label(status_row, text="", style="Dim.TLabel")
        self.lbl_time.pack(side="right")

        # Ações
        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(14, 0))
        self.btn_process = ttk.Button(actions, text="Processar", style="Accent.TButton", command=self.start)
        self.btn_process.pack(side="left")
        self.btn_cancel = ttk.Button(actions, text="Cancelar", style="Danger.TButton", command=self.cancel, state="disabled")
        self.btn_cancel.pack(side="left", padx=(10, 0))
        self.btn_report = ttk.Button(actions, text="Relatório", command=self.open_report, state="disabled")
        self.btn_report.pack(side="right")
        self.btn_open = ttk.Button(actions, text="Abrir pasta", command=self.open_output, state="disabled")
        self.btn_open.pack(side="right", padx=(0, 10))

    # ── FFmpeg ──

    def _refresh_ffmpeg_status(self):
        ver = core.ffmpeg_version()
        if ver:
            self.lbl_ffmpeg.configure(text=f"● FFmpeg {ver}", style="Ok.TLabel")
            self.btn_ffmpeg_help.pack_forget()
            self.ffmpeg_ok = True
        else:
            self.lbl_ffmpeg.configure(text="● FFmpeg não encontrado — vídeos e áudios indisponíveis", style="Err.TLabel")
            self.btn_ffmpeg_help.pack(anchor="e", pady=(6, 0))
            self.ffmpeg_ok = False

    def _ffmpeg_help(self):
        msg = ("O FFmpeg é necessário para processar vídeos e áudios (fotos funcionam sem ele).\n\n"
               "Instale pelo Terminal:\n\n    brew install ffmpeg\n\n"
               "Se não tiver o Homebrew: https://brew.sh\n\n"
               "Depois clique em OK para verificar novamente.")
        if messagebox.askokcancel("Instalar FFmpeg", msg):
            self._refresh_ffmpeg_status()

    # ── lista de arquivos ──

    def pick_files(self):
        if self.is_processing:
            return
        exts = " ".join(f"*{e}" for e in sorted(core.ALL_SUPPORTED))
        paths = filedialog.askopenfilenames(
            title="Selecionar arquivos",
            filetypes=[("Mídia suportada", exts),
                       ("Fotos", " ".join(f"*{e}" for e in sorted(core.IMAGE_EXTENSIONS))),
                       ("Vídeos", " ".join(f"*{e}" for e in sorted(core.VIDEO_EXTENSIONS))),
                       ("Áudios", " ".join(f"*{e}" for e in sorted(core.AUDIO_EXTENSIONS))),
                       ("Todos", "*.*")])
        if paths:
            self.add_paths(list(paths))

    def pick_folder(self):
        if self.is_processing:
            return
        path = filedialog.askdirectory(title="Selecionar pasta (inclui subpastas)")
        if path:
            self.add_paths([path])

    def pick_output(self):
        if self.is_processing:
            return
        path = filedialog.askdirectory(title="Pasta de destino", initialdir=self.output_path.get() or None)
        if path:
            self.output_path.set(path)

    def add_paths(self, paths: list[str]):
        found = core.collect_files(paths)
        skipped = 0
        added = 0
        for p in paths:
            if os.path.isfile(p) and core.get_file_type(p) == "unknown":
                skipped += 1
        for f in found:
            if f in self.rows:
                continue
            kind = core.get_file_type(f)
            try:
                size = core.human_size(os.path.getsize(f))
            except OSError:
                size = "?"
            iid = self.tree.insert("", "end", values=(os.path.basename(f), KIND_LABEL[kind], size, "Aguardando", ""),
                                   tags=("pending",))
            self.rows[f] = iid
            self.files.append(f)
            added += 1
        self._update_count()
        if added == 0 and not found:
            messagebox.showinfo("Nada adicionado", "Nenhum arquivo com formato suportado foi encontrado."
                                + (f"\n\n{skipped} arquivo(s) ignorado(s) por extensão não suportada." if skipped else ""))
        elif skipped:
            self.set_status(f"{added} adicionado(s) · {skipped} ignorado(s) (formato não suportado)")

    def clear_list(self):
        if self.is_processing:
            return
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.files.clear()
        self.rows.clear()
        self.results.clear()
        self.trims.clear()
        self.progress_var.set(0)
        self.set_status("Pronto")
        self.lbl_time.configure(text="")
        self._update_count()
        self._on_tree_select()

    def _remove_selected(self, event=None):
        if self.is_processing or not self.tree.selection():
            return
        focus = self.root.focus_get()
        if isinstance(focus, (ttk.Entry, tk.Entry, tk.Text)):
            return
        for iid in self.tree.selection():
            path = next((p for p, i in self.rows.items() if i == iid), None)
            if path:
                self.files.remove(path)
                del self.rows[path]
                self.results.pop(path, None)
                self.trims.pop(path, None)
            self.tree.delete(iid)
        self._update_count()
        self._on_tree_select()

    def _update_count(self):
        n = len(self.files)
        has = n > 0
        if has:
            self.lbl_empty.place_forget()
        else:
            self.lbl_empty.place(relx=0.5, rely=0.5, anchor="center")
        if not has:
            self.lbl_count.configure(text="Nenhum arquivo")
            return
        kinds = {"video": 0, "image": 0, "audio": 0}
        total = 0
        for f in self.files:
            kinds[core.get_file_type(f)] += 1
            try:
                total += os.path.getsize(f)
            except OSError:
                pass
        parts = []
        if kinds["image"]:
            parts.append(f"{kinds['image']} foto{'s' if kinds['image'] > 1 else ''}")
        if kinds["video"]:
            parts.append(f"{kinds['video']} vídeo{'s' if kinds['video'] > 1 else ''}")
        if kinds["audio"]:
            parts.append(f"{kinds['audio']} áudio{'s' if kinds['audio'] > 1 else ''}")
        self.lbl_count.configure(text=f"{n} arquivo{'s' if n > 1 else ''} · " + " · ".join(parts) + f" · {core.human_size(total)}")

    # ── status ──

    def set_status(self, msg: str, style: str = "Status.TLabel"):
        self.lbl_status.configure(text=msg, style=style)

    def _set_row(self, path: str, status: str, tag: str, hash_text: str | None = None, name: str | None = None):
        iid = self.rows.get(path)
        if not iid:
            return
        vals = list(self.tree.item(iid, "values"))
        if name is not None:
            vals[0] = name
        vals[3] = status
        if hash_text is not None:
            vals[4] = hash_text
        self.tree.item(iid, values=vals, tags=(tag,))

    def _set_controls(self, processing: bool):
        state = "disabled" if processing else "normal"
        for w in (self.btn_add_files, self.btn_add_folder, self.btn_clear, self.btn_out,
                  self.chk_icc, self.chk_reenc, self.btn_process, self.entry_wm,
                  self.spin_wm_size, self.scale_wm_opacity, self.chk_audio):
            w.configure(state=state)
        self.combo_wm_pos.configure(state="disabled" if processing else "readonly")
        self.entry_out.configure(state="disabled" if processing else "normal")
        self.btn_cancel.configure(state="normal" if processing else "disabled")
        self._on_tree_select()

    def _selected_path(self) -> str | None:
        sel = self.tree.selection()
        if len(sel) != 1:
            return None
        return next((p for p, i in self.rows.items() if i == sel[0]), None)

    def _on_tree_select(self, event=None):
        path = self._selected_path()
        ok = (not self.is_processing and getattr(self, "ffmpeg_ok", False) and path is not None
              and core.get_file_type(path) == "video")
        self.btn_trim.configure(state="normal" if ok else "disabled")

    def _pending_label(self, path: str) -> str:
        trim = self.trims.get(path)
        if not trim:
            return "Aguardando"
        return f"Aguardando · ✂ {fmt_time(trim[0])}–{fmt_time(trim[1]) if trim[1] else 'fim'}"

    # ── corte rápido ──

    def open_trimmer(self):
        path = self._selected_path()
        if self.is_processing or not path or core.get_file_type(path) != "video":
            return
        duration = core.media_duration(path)
        if duration <= 0:
            messagebox.showerror("Cortar vídeo", "Não foi possível ler a duração deste vídeo.")
            return
        TrimDialog(self, path, duration)

    def set_trim(self, path: str, trim: tuple[float, float] | None):
        if trim:
            self.trims[path] = trim
        else:
            self.trims.pop(path, None)
        if path in self.rows and not self.is_processing:
            self._set_row(path, self._pending_label(path), "pending")

    # ── processamento ──

    def start(self):
        if self.is_processing:
            return
        if not self.files:
            messagebox.showwarning("Nenhum arquivo", "Adicione arquivos ou uma pasta antes de processar.")
            return
        out = self.output_path.get().strip()
        if not out:
            messagebox.showwarning("Destino", "Escolha uma pasta de destino.")
            return
        if not os.path.isdir(out):
            try:
                os.makedirs(out, exist_ok=True)
            except OSError as e:
                messagebox.showerror("Destino", f"Não foi possível usar a pasta de destino:\n{e}")
                return
        try:
            wm_size = int(self.watermark_size.get())
        except (tk.TclError, ValueError):
            wm_size = 0
        if not 10 <= wm_size <= 200:
            messagebox.showwarning("Marca d'água", "O tamanho da marca d'água deve ser um número entre 10 e 200.")
            return
        needs_ffmpeg = any(core.get_file_type(f) != "image" for f in self.files)
        if needs_ffmpeg and not core.check_ffmpeg():
            self._refresh_ffmpeg_status()
            messagebox.showerror("FFmpeg não encontrado",
                                 "Há vídeos/áudios na lista, e o FFmpeg é necessário para eles.\n\n"
                                 "Instale com:  brew install ffmpeg\n\nOu remova os vídeos/áudios da lista.")
            return

        self.is_processing = True
        self.cancel_event.clear()
        self.results.clear()
        self.output_dir = None
        self.done_count = 0
        self.start_time = time.time()
        self.progress_var.set(0)
        self.btn_open.configure(state="disabled")
        self.btn_report.configure(state="disabled")
        self._set_controls(True)
        for f in self.files:
            self._set_row(f, self._pending_label(f), "pending", "")
        self.set_status("Iniciando…")

        options = core.Options(
            keep_icc=self.keep_icc.get(),
            reencode_fallback=self.reencode.get(),
            watermark_text=self.watermark_text.get().strip(),
            watermark_opacity=self.watermark_opacity.get(),
            watermark_position="repeat" if self.watermark_position.get() == "Repetir" else "center",
            watermark_size=wm_size,
            remove_audio=self.remove_audio.get(),
            trims=dict(self.trims),
        )
        files = list(self.files)
        threading.Thread(target=self._worker, args=(files, out, options), daemon=True).start()
        self._tick_time()

    def _worker(self, files: list[str], out: str, options: core.Options):
        q = self.queue
        try:
            output_dir, results = core.process_batch(
                files, out, options,
                on_start=lambda i, n, path: q.put(("start", i, n, path)),
                on_progress=lambda i, frac: q.put(("progress", i, frac)),
                on_done=lambda i, r: q.put(("done", i, r)),
                cancel=self.cancel_event,
            )
            q.put(("finished", output_dir, results))
        except Exception as e:  # noqa: BLE001
            q.put(("fatal", str(e)))

    def _poll_queue(self):
        try:
            while True:
                msg = self.queue.get_nowait()
                self._handle(msg)
        except queue.Empty:
            pass
        self.root.after(80, self._poll_queue)

    def _handle(self, msg):
        kind = msg[0]
        total = max(1, len(self.files))
        if kind == "start":
            _, i, n, path = msg
            self._set_row(path, "Processando…", "working")
            self.set_status(f"[{i + 1}/{n}] {os.path.basename(path)}")
            self.tree.see(self.rows[path])
        elif kind == "progress":
            _, i, frac = msg
            self.progress_var.set(100 * (i + frac) / total)
            if i < len(self.files):
                self._set_row(self.files[i], f"Processando {int(frac * 100)}%", "working")
        elif kind == "done":
            _, i, r = msg
            self.results[r.input] = r
            self.done_count += 1
            self.progress_var.set(100 * self.done_count / total)
            new_name = os.path.basename(r.output) if r.output else None
            if r.success:
                if r.clean:
                    status = "✓ Limpo"
                    tag = "ok"
                else:
                    status = "✓ OK · ver detalhes"
                    tag = "warn"
                if r.method == "convert":
                    status += f" · {os.path.splitext(r.output)[1][1:].upper()}"
                elif r.method == "reencode":
                    status += " · recodificado"
                self._set_row(r.input, status, tag, r.hash_after[:12] + "…", new_name)
            elif r.error == "cancelado":
                self._set_row(r.input, "Cancelado", "warn", "")
            else:
                self._set_row(r.input, "✗ Erro · ver detalhes", "error", "")
        elif kind == "finished":
            _, output_dir, results = msg
            self.output_dir = output_dir
            self._finish(results)
        elif kind == "fatal":
            self.is_processing = False
            self._set_controls(False)
            self.set_status(f"Erro: {msg[1]}", "Err.TLabel")
            messagebox.showerror("Erro", msg[1])

    def _finish(self, results: list[core.FileResult]):
        self.is_processing = False
        self._set_controls(False)
        ok = sum(1 for r in results if r.success)
        fail = sum(1 for r in results if not r.success and r.error != "cancelado")
        cancelled = self.cancel_event.is_set()
        n_cancelled = sum(1 for r in results if r.error == "cancelado") + (len(self.files) - len(results))
        for f in self.files[len(results):]:
            self._set_row(f, "Cancelado", "warn", "")
        elapsed = time.time() - self.start_time
        self.lbl_time.configure(text=f"{elapsed:.1f} s")
        if cancelled:
            self.set_status(f"Cancelado — {ok} concluído(s), {n_cancelled} não processado(s)"
                            + (f", {fail} erro(s)" if fail else ""), "Warn.TLabel")
        elif fail:
            self.set_status(f"Concluído com falhas — {ok} ok, {fail} erro(s)", "Err.TLabel")
            self.progress_var.set(100)
        else:
            self.set_status(f"Concluído — {ok} arquivo(s) limpos e com novo hash", "Ok.TLabel")
            self.progress_var.set(100)
        if self.output_dir:
            self.btn_open.configure(state="normal")
            self.btn_report.configure(state="normal")
            if self.open_after.get() and not cancelled:
                open_in_finder(self.output_dir)

    def _tick_time(self):
        if not self.is_processing:
            return
        self.lbl_time.configure(text=f"{time.time() - self.start_time:.0f} s")
        self.root.after(1000, self._tick_time)

    def cancel(self):
        if not self.is_processing:
            return
        self.cancel_event.set()
        self.btn_cancel.configure(state="disabled")
        self.set_status("Cancelando…", "Warn.TLabel")

    def open_output(self):
        if self.output_dir and os.path.isdir(self.output_dir):
            open_in_finder(self.output_dir)

    def open_report(self):
        if self.output_dir:
            path = os.path.join(self.output_dir, "relatorio.txt")
            if os.path.exists(path):
                open_in_finder(path)

    # ── janelas auxiliares ──

    def _show_details(self, event=None):
        sel = self.tree.selection()
        if not sel:
            return
        iid = sel[0]
        path = next((p for p, i in self.rows.items() if i == iid), None)
        if not path:
            return
        r = self.results.get(path)
        if r is None:
            self._text_window(f"Detalhes — {os.path.basename(path)}", f"Arquivo: {path}\n\nAinda não processado.")
            return
        lines = [f"Original:  {r.input}", f"Saída:     {r.output or '—'}", ""]
        lines.append(f"Tipo: {KIND_LABEL.get(r.kind, r.kind)}   Método: {r.method or '—'}   Tempo: {r.seconds}s")
        lines.append(f"Tamanho: {core.human_size(r.size_before)} → {core.human_size(r.size_after) if r.size_after else '—'}")
        lines.append("")
        if r.error:
            lines.append(f"ERRO: {r.error}")
            lines.append("")
        lines.append("SHA-256 original:")
        lines.append(f"  {r.hash_before}")
        lines.append("SHA-256 novo:")
        lines.append(f"  {r.hash_after or '—'}")
        lines.append("MD5 original / novo:")
        lines.append(f"  {r.md5_before}  /  {r.md5_after or '—'}")
        lines.append("")
        lines.append("Metadados removidos:")
        lines.extend(f"  • {x}" for x in r.removed) if r.removed else lines.append("  (nenhum metadado encontrado no original)")
        lines.append("")
        lines.append("Restante após a limpeza (tags técnicas, sem dados pessoais):")
        lines.extend(f"  • {x}" for x in r.remaining) if r.remaining else lines.append("  nenhum")
        if r.notes:
            lines.append("")
            lines.append("Observações:")
            lines.extend(f"  • {x}" for x in r.notes)
        lines.append("")
        lines.append("Verificação: " + ("LIMPO ✓" if r.clean else ("ATENÇÃO — confira as tags restantes" if r.success else "não processado")))
        self._text_window(f"Detalhes — {os.path.basename(path)}", "\n".join(lines),
                          reveal=r.output if r.success else None)

    def _text_window(self, title: str, text: str, reveal: str | None = None):
        win = tk.Toplevel(self.root)
        win.title(title)
        win.configure(bg=P["bg"])
        win.geometry("720x520")
        win.transient(self.root)
        frame = ttk.Frame(win, padding=16)
        frame.pack(fill="both", expand=True)
        txt = tk.Text(frame, font=self.f_mono_small, bg=P["card"], fg=P["text"], insertbackground=P["text"],
                      relief="flat", bd=0, padx=12, pady=12, wrap="word", highlightthickness=1,
                      highlightbackground=P["border"], highlightcolor=P["border"])
        txt.insert("1.0", text)
        txt.configure(state="disabled")
        txt.pack(fill="both", expand=True)
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(12, 0))
        ttk.Button(row, text="Fechar", command=win.destroy).pack(side="right")
        if reveal and os.path.exists(reveal):
            ttk.Button(row, text="Mostrar no Finder", command=lambda: open_in_finder(reveal, reveal=True)).pack(side="left")

        def copy():
            self.root.clipboard_clear()
            self.root.clipboard_append(text)

        ttk.Button(row, text="Copiar", command=copy).pack(side="left", padx=(8, 0))
        win.bind("<Escape>", lambda e: win.destroy())

    def inspect_dialog(self):
        exts = " ".join(f"*{e}" for e in sorted(core.ALL_SUPPORTED))
        path = filedialog.askopenfilename(title="Inspecionar metadados", filetypes=[("Mídia", exts), ("Todos", "*.*")])
        if not path:
            return
        info = core.inspect_file(path)
        lines = [f"Arquivo: {info['arquivo']}", f"Tipo: {KIND_LABEL.get(info['tipo'], info['tipo'])}   Tamanho: {info['tamanho']}",
                 f"SHA-256: {info['sha256']}", ""]
        if info["erro"]:
            lines.append(f"Erro: {info['erro']}")
        elif info["metadados"]:
            lines.append(f"Metadados encontrados ({len(info['metadados'])}):")
            lines.extend(f"  • {m}" for m in info["metadados"])
        else:
            lines.append("Nenhum metadado encontrado — arquivo já está limpo.")
        self._text_window(f"Inspeção — {os.path.basename(path)}", "\n".join(lines))

    def _about(self):
        messagebox.showinfo(f"Sobre {APP_NAME}",
                            f"{APP_NAME} v{core.__version__}\n\n"
                            "Remove metadados (EXIF, GPS, XMP, IPTC, datas, tags) de fotos, vídeos e áudios "
                            "e altera o hash de cada arquivo, sem recodificar quando possível.\n\n"
                            "100% offline. Código aberto:\n" + GITHUB_URL)

    def _how_it_works(self):
        self._text_window("Como funciona", (
            "FOTOS (JPG, PNG, WebP, GIF)\n"
            "  Os pixels são copiados byte a byte; só os blocos de metadados são removidos\n"
            "  (EXIF, GPS, XMP, IPTC, comentários, datas, miniaturas, dados ocultos após o fim\n"
            "  da imagem). A orientação da foto é preservada em uma tag mínima. O hash muda\n"
            "  por preenchimento permitido pela norma de cada formato — nenhum metadado novo\n"
            "  é adicionado.\n\n"
            "HEIC / HEIF / AVIF / RAW → JPEG  ·  TIFF / BMP → PNG\n"
            "  Esses formatos são convertidos (via sips do macOS ou FFmpeg) e depois limpos.\n\n"
            "VÍDEOS E ÁUDIOS\n"
            "  O FFmpeg copia os streams de vídeo/áudio para um container novo, sem\n"
            "  recodificar, descartando todas as tags, capítulos, capas e streams de dados\n"
            "  (sensores, GPS, rostos detectados pelo iPhone). Se a cópia direta não for\n"
            "  possível, o arquivo é recodificado. O hash muda por padding válido do\n"
            "  container (caixa 'free' no MP4/MOV, Void no MKV/WebM, JUNK no AVI/WAV…).\n\n"
            "VERIFICAÇÃO\n"
            "  Cada arquivo de saída é reanalisado: 'Limpo' significa que nenhuma tag pessoal\n"
            "  restou. Tags técnicas genéricas (ex.: 'handler_name=VideoHandler',\n"
            "  'encoder=Lavf') não identificam ninguém e são listadas nos detalhes.\n\n"
            "O QUE NÃO É REMOVIDO\n"
            "  • Conteúdo visível (placas, rostos, textos na imagem).\n"
            "  • Assinaturas do codificador dentro do próprio stream (ex.: 'x264'), que só\n"
            "    somem com recodificação.\n"
            "  • Marcas d'água invisíveis / fingerprint perceptual (o hash muda, mas a\n"
            "    imagem continua reconhecível por sistemas de comparação visual)."
        ))

    def _on_close(self):
        if self.is_processing:
            if not messagebox.askyesno("Sair", "Um processamento está em andamento. Cancelar e sair?"):
                return
            self.cancel_event.set()
        self.root.destroy()


# ─────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────

def main():
    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", root.tk.call("tk", "scaling"))
    except tk.TclError:
        pass
    paths = [p for p in sys.argv[1:] if os.path.exists(p)]
    App(root, paths)
    root.mainloop()


if __name__ == "__main__":
    main()
