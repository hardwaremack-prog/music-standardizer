#!/usr/bin/env python3
"""
standardize_music_gui.py — desktop GUI for cleaning up a messy music library.

Requires: standardize_music.py to be in the SAME FOLDER as this file.

Install once:
    pip install mutagen tkinterdnd2 pygame pillow

    (tkinterdnd2 enables drag & drop, pygame enables in-app mp3 playback, and
    pillow enables the album art preview. The app still runs without any of
    them — those features just quietly disable themselves and tell you why.)

Run:
    python standardize_music_gui.py

Workflow:
  1. Click "Choose Folder…" (or drag files/folders straight onto the window).
  2. Every row is a proposed Artist / Title / Track / new filename.
  3. Double-click any cell in the Artist / Title / Track column to fix a bad guess.
     Rows in yellow are low-confidence guesses worth a look.
  4. Click a row to preview its album art and play it back on the right.
  5. Click "Preview (dry run)" to see exactly what would happen, or
     "Apply" to actually rename the files and write the tags.
  6. "Clear" wipes the current list (doesn't touch your files).
"""

import io
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk, filedialog, messagebox

try:
    import standardize_music as core
except ImportError:
    print("Could not find standardize_music.py — keep both files in the same folder.", file=sys.stderr)
    sys.exit(1)

DND_AVAILABLE = False
_BaseWindow = tk.Tk
try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    # Importing the python module can succeed even if the native tkdnd Tcl
    # extension it depends on isn't actually available (e.g. missing data
    # files in a frozen/bundled build) — that failure only shows up when Tcl
    # tries to load it. So actually construct a throwaway window to confirm
    # it really works before committing to use it as our base class.
    _probe = TkinterDnD.Tk()
    _probe.withdraw()
    _probe.destroy()
    DND_AVAILABLE = True
    _BaseWindow = TkinterDnD.Tk
except Exception:
    DND_AVAILABLE = False
    _BaseWindow = tk.Tk

PLAYBACK_AVAILABLE = True
try:
    import pygame
    pygame.mixer.init()
except Exception:
    PLAYBACK_AVAILABLE = False

PIL_AVAILABLE = True
try:
    from PIL import Image, ImageTk
except ImportError:
    PIL_AVAILABLE = False

EDITABLE_COLS = {"artist", "title", "track"}
COLUMNS = [
    ("filename", "Original Filename", 230),
    ("artist", "Artist", 130),
    ("title", "Title", 170),
    ("track", "Track", 50),
    ("has_art", "Has Art", 60),
    ("confidence", "Confidence", 120),
    ("proposed_filename", "New Filename", 230),
]

class App(_BaseWindow):
    def __init__(self):
        super().__init__()
        self.title("Music Library Standardizer")
        self.geometry("1350x620")

        self.folder = tk.StringVar(value="")
        self.pattern = tk.StringVar(value="{artist} - {title}")
        self.fetch_art = tk.BooleanVar(value=False)
        self.force_art = tk.BooleanVar(value=False)
        self.online_lookup = tk.BooleanVar(value=False)
        self.rows = []  # list of proposal dicts (same shape as core.scan_folder output)
        self.row_id_to_index = {}  # treeview item id -> index into self.rows

        self.selected_path = None
        self.currently_playing = None
        self.art_image_ref = None  # keep a reference so Tk doesn't garbage-collect the preview image

        self._build_top_bar()
        self._build_main_area()
        self._build_bottom_bar()

    # ---------- UI construction ----------

    def _build_top_bar(self):
        bar = ttk.Frame(self, padding=8)
        bar.pack(fill="x")

        ttk.Button(bar, text="Choose Folder…", command=self.choose_folder).pack(side="left")
        ttk.Button(bar, text="Clear", command=self.clear_all).pack(side="left", padx=(6, 0))
        self.folder_label = ttk.Label(bar, text="No folder selected", foreground="#555")
        self.folder_label.pack(side="left", padx=8)

        ttk.Label(bar, text="Filename pattern:").pack(side="left", padx=(20, 4))
        pattern_entry = ttk.Entry(bar, textvariable=self.pattern, width=28)
        pattern_entry.pack(side="left")
        ttk.Label(bar, text="(use {artist} {title} {track})", foreground="#777").pack(side="left", padx=4)

        self.scan_btn = ttk.Button(bar, text="Scan", command=self.start_scan, state="disabled")
        self.scan_btn.pack(side="right")

    def _build_main_area(self):
        container = ttk.Frame(self)
        container.pack(fill="both", expand=True, padx=8, pady=4)

        drop_text = ("🎵  Drag & drop mp3 / flac / m4a files or folders here"
                     if DND_AVAILABLE else
                     "Drag & drop unavailable (pip install tkinterdnd2 to enable it) — use Choose Folder instead")
        self.drop_label = ttk.Label(container, text=drop_text, anchor="center",
                                     relief="groove", padding=8, foreground="#555")
        self.drop_label.pack(fill="x", pady=(0, 6))

        table_row = ttk.Frame(container)
        table_row.pack(fill="both", expand=True)

        col_ids = [c[0] for c in COLUMNS]
        self.tree = ttk.Treeview(table_row, columns=col_ids, show="headings", selectmode="browse")
        for col_id, heading, width in COLUMNS:
            self.tree.heading(col_id, text=heading)
            self.tree.column(col_id, width=width, anchor="w")

        vsb = ttk.Scrollbar(table_row, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")

        self.tree.tag_configure("low", background="#fff3b0")
        self.tree.tag_configure("high", background="#e6f4ea")

        self.tree.bind("<Double-1>", self.on_double_click)
        self.tree.bind("<<TreeviewSelect>>", self.on_row_select)

        self._build_side_panel(table_row)

        if DND_AVAILABLE:
            for widget in (self.drop_label, self.tree):
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", self.on_drop)

    def _build_side_panel(self, parent):
        side = ttk.Frame(parent, width=210, padding=(10, 0))
        side.pack(side="left", fill="y")
        side.pack_propagate(False)

        ttk.Label(side, text="Album Art", font=("", 10, "bold")).pack(anchor="w")

        art_frame = ttk.Frame(side, width=190, height=190, relief="sunken")
        art_frame.pack(pady=6)
        art_frame.pack_propagate(False)
        self.art_label = ttk.Label(art_frame, text="No selection", anchor="center", justify="center", wraplength=170)
        self.art_label.pack(fill="both", expand=True)
        if not PIL_AVAILABLE:
            self.art_label.config(text="No selection\n\n(install pillow to preview art)")

        self.now_playing_label = ttk.Label(side, text="", wraplength=190, foreground="#333")
        self.now_playing_label.pack(fill="x", pady=(2, 6))

        self.play_btn = ttk.Button(side, text="▶ Play", command=self.toggle_play, state="disabled")
        self.play_btn.pack(fill="x")

        if not PLAYBACK_AVAILABLE:
            ttk.Label(side, text="(install pygame to enable playback)", foreground="#999",
                      wraplength=190, font=("", 8)).pack(pady=(4, 0))

    def _build_bottom_bar(self):
        bar = ttk.Frame(self, padding=8)
        bar.pack(fill="x")

        self.status = ttk.Label(bar, text="Pick a folder or drag & drop files to get started.")
        self.status.pack(side="left")

        self.apply_btn = ttk.Button(bar, text="Apply (rename + tag)", command=lambda: self.start_apply(dry_run=False), state="disabled")
        self.apply_btn.pack(side="right", padx=(6, 0))
        self.preview_btn = ttk.Button(bar, text="Preview (dry run)", command=lambda: self.start_apply(dry_run=True), state="disabled")
        self.preview_btn.pack(side="right")

        ttk.Checkbutton(bar, text="Re-fetch even if art exists", variable=self.force_art).pack(side="right", padx=(4, 16))
        ttk.Checkbutton(bar, text="Fetch album art (needs internet)", variable=self.fetch_art).pack(side="right", padx=(4, 4))
        ttk.Checkbutton(bar, text="Look up unclear tracks online", variable=self.online_lookup).pack(side="right", padx=(4, 16))

    # ---------- folder scan ----------

    def choose_folder(self):
        folder = filedialog.askdirectory(title="Choose your music folder")
        if folder:
            self.folder.set(folder)
            self.folder_label.config(text=folder)
            self.scan_btn.config(state="normal")

    def start_scan(self):
        folder = self.folder.get()
        if not folder:
            return
        pattern = self.pattern.get()
        self.scan_btn.config(state="disabled")
        self.status.config(text="Scanning…")

        thread = threading.Thread(target=self._scan_worker, args=(folder, pattern), daemon=True)
        thread.start()

    def _scan_worker(self, folder, pattern):
        def progress(i, total, path):
            if path is not None:
                self.after(0, lambda: self.status.config(text=f"Scanning {i + 1}/{total}: {path.name}"))

        try:
            rows = core.scan_folder(folder, pattern=pattern, progress_cb=progress)
        except Exception as e:
            self.after(0, lambda: self._scan_failed(e))
            return
        self.after(0, lambda: self._scan_done(rows))

    def _scan_failed(self, exc):
        self.status.config(text="Scan failed.")
        self.scan_btn.config(state="normal")
        messagebox.showerror("Scan failed", str(exc))

    def _scan_done(self, rows):
        self.rows = rows
        self.scan_btn.config(state="normal")
        self._rebuild_tree()

        if not rows:
            self.status.config(text="No audio files found in that folder.")
            self.apply_btn.config(state="disabled")
            self.preview_btn.config(state="disabled")
            return

        low_count = sum(1 for r in rows if r["confidence"] == "low")
        self.status.config(text=f"Found {len(rows)} file(s) — {low_count} need a manual check (highlighted).")
        self.apply_btn.config(state="normal")
        self.preview_btn.config(state="normal")

    # ---------- drag & drop ----------

    def on_drop(self, event):
        try:
            paths = [p for p in self.tk.splitlist(event.data) if p]
        except Exception:
            paths = [event.data] if event.data else []
        if not paths:
            return
        pattern = self.pattern.get()
        self.status.config(text=f"Scanning {len(paths)} dropped item(s)…")
        thread = threading.Thread(target=self._scan_paths_worker, args=(paths, pattern), daemon=True)
        thread.start()

    def _scan_paths_worker(self, paths, pattern):
        def progress(i, total, path):
            if path is not None:
                self.after(0, lambda: self.status.config(text=f"Scanning {i + 1}/{total}: {path.name}"))

        try:
            new_rows = core.scan_paths(paths, pattern=pattern, progress_cb=progress)
        except Exception as e:
            self.after(0, lambda: self._scan_failed(e))
            return
        self.after(0, lambda: self._merge_rows(new_rows))

    def _merge_rows(self, new_rows):
        if not new_rows:
            self.status.config(text="No audio files found in what you dropped.")
            return

        by_path = {r["original_path"]: i for i, r in enumerate(self.rows)}
        added = 0
        for row in new_rows:
            if row["original_path"] in by_path:
                self.rows[by_path[row["original_path"]]] = row
            else:
                self.rows.append(row)
                added += 1

        self._rebuild_tree()
        self.status.config(text=f"Added {added} new file(s) from drop (updated {len(new_rows) - added} existing). Total: {len(self.rows)}.")
        self.apply_btn.config(state="normal")
        self.preview_btn.config(state="normal")

    # ---------- table helpers ----------

    def _rebuild_tree(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.row_id_to_index = {}
        for idx, row in enumerate(self.rows):
            self._insert_row(idx, row)

    def _insert_row(self, idx, row):
        filename = Path(row["original_path"]).name
        tag = "low" if row["confidence"] == "low" else "high"
        values = (
            filename,
            row["artist"],
            row["title"],
            row["track"],
            row.get("has_art", ""),
            row["confidence"],
            row["proposed_filename"],
        )
        item_id = self.tree.insert("", "end", values=values, tags=(tag,))
        self.row_id_to_index[item_id] = idx

    def clear_all(self):
        self.stop_playback()
        self.rows = []
        self.row_id_to_index = {}
        for item in self.tree.get_children():
            self.tree.delete(item)
        self._clear_art_preview()
        self.apply_btn.config(state="disabled")
        self.preview_btn.config(state="disabled")
        self.status.config(text="Cleared. Pick a folder or drag & drop files to get started.")

    def on_double_click(self, event):
        region = self.tree.identify("region", event.x, event.y)
        if region != "cell":
            return
        item_id = self.tree.identify_row(event.y)
        col_id = self.tree.identify_column(event.x)  # like '#2'
        col_index = int(col_id.replace("#", "")) - 1
        col_name = COLUMNS[col_index][0]

        if item_id not in self.row_id_to_index:
            return

        if col_name == "filename":
            self.tree.selection_set(item_id)
            self.toggle_play()
            return

        if col_name not in EDITABLE_COLS:
            return

        x, y, width, height = self.tree.bbox(item_id, col_id)
        current_value = self.tree.set(item_id, col_name)

        editor = ttk.Entry(self.tree)
        editor.insert(0, current_value)
        editor.select_range(0, "end")
        editor.focus()
        editor.place(x=x, y=y, width=width, height=height)

        def save(_event=None):
            new_value = editor.get()
            editor.destroy()
            self.tree.set(item_id, col_name, new_value)
            idx = self.row_id_to_index[item_id]
            self.rows[idx][col_name] = new_value
            self._refresh_proposed_filename(item_id, idx)

        editor.bind("<Return>", save)
        editor.bind("<FocusOut>", save)
        editor.bind("<Escape>", lambda e: editor.destroy())

    def _refresh_proposed_filename(self, item_id, idx):
        row = self.rows[idx]
        ext = Path(row["original_path"]).suffix
        new_name = core.build_proposed_filename(row["artist"], row["title"], row["track"], ext, self.pattern.get())
        row["proposed_filename"] = new_name
        self.tree.set(item_id, "proposed_filename", new_name)

    # ---------- selection: album art preview + playback target ----------

    def on_row_select(self, event=None):
        selection = self.tree.selection()
        if not selection:
            self._clear_art_preview()
            return
        item_id = selection[0]
        idx = self.row_id_to_index.get(item_id)
        if idx is None:
            return
        row = self.rows[idx]
        self.selected_path = row["original_path"]
        self.now_playing_label.config(text=f"{row['artist']} — {row['title']}" if row["artist"] or row["title"] else Path(row["original_path"]).name)
        self._update_art_preview(self.selected_path)
        self.play_btn.config(state="normal" if PLAYBACK_AVAILABLE else "disabled")

    def _update_art_preview(self, path):
        if not PIL_AVAILABLE:
            return
        image_bytes, mime = core.get_album_art_bytes(path)
        if not image_bytes:
            self.art_label.config(image="", text="No album art")
            self.art_image_ref = None
            return
        try:
            img = Image.open(io.BytesIO(image_bytes))
            img.thumbnail((180, 180))
            photo = ImageTk.PhotoImage(img)
            self.art_label.config(image=photo, text="")
            self.art_image_ref = photo  # prevent garbage collection
        except Exception:
            self.art_label.config(image="", text="(couldn't preview art)")
            self.art_image_ref = None

    def _clear_art_preview(self):
        self.art_label.config(image="", text="No selection" if PIL_AVAILABLE else "No selection\n\n(install pillow to preview art)")
        self.art_image_ref = None
        self.now_playing_label.config(text="")
        self.selected_path = None
        self.play_btn.config(state="disabled")

    # ---------- playback ----------

    def toggle_play(self):
        if not PLAYBACK_AVAILABLE:
            return
        if self.currently_playing:
            self.stop_playback()
            return
        path = self.selected_path
        if not path:
            return
        try:
            pygame.mixer.music.load(path)
            pygame.mixer.music.play()
            self.currently_playing = path
            self.play_btn.config(text="■ Stop")
            self._poll_playback()
        except Exception as e:
            messagebox.showerror("Playback error", str(e))

    def _poll_playback(self):
        if not self.currently_playing:
            return
        if not pygame.mixer.music.get_busy():
            self.stop_playback()
            return
        self.after(500, self._poll_playback)

    def stop_playback(self):
        if PLAYBACK_AVAILABLE:
            try:
                pygame.mixer.music.stop()
            except Exception:
                pass
        self.currently_playing = None
        self.play_btn.config(text="▶ Play")

    # ---------- apply ----------

    def start_apply(self, dry_run):
        if not self.rows:
            return
        if not dry_run:
            notes = []
            if self.online_lookup.get():
                notes.append("look up unclear tracks online")
            if self.fetch_art.get():
                notes.append("fetch album art")
            note = f" and {', '.join(notes)}" if notes else ""
            confirmed = messagebox.askyesno(
                "Apply changes?",
                f"This will rename and re-tag {len(self.rows)} file(s) in place{note}.\n\n"
                "This can't be automatically undone. Continue?",
            )
            if not confirmed:
                return

        self.stop_playback()
        self.apply_btn.config(state="disabled")
        self.preview_btn.config(state="disabled")
        self.status.config(text="Working…")

        pattern = self.pattern.get()
        fetch_art = self.fetch_art.get()
        force_art = self.force_art.get()
        online_lookup = self.online_lookup.get()
        thread = threading.Thread(target=self._apply_worker, args=(dry_run, pattern, fetch_art, force_art, online_lookup), daemon=True)
        thread.start()

    def _apply_worker(self, dry_run, pattern, fetch_art, force_art, online_lookup):
        results = []
        art_added, art_skipped, art_not_found, art_errors = 0, 0, 0, 0
        meta_corrected, meta_not_found, meta_errors = 0, 0, 0
        n = len(self.rows)

        for i, row in enumerate(self.rows):
            label = f"{row['artist']} — {row['title']}".strip(" —") or Path(row["original_path"]).name
            is_low = row.get("confidence", "").startswith("low")

            if not dry_run:
                if online_lookup and is_low:
                    verb = "Looking up"
                elif fetch_art:
                    verb = "Fetching album art for"
                else:
                    verb = None
                text = f"({i + 1}/{n}) {verb} {label}…" if verb else f"({i + 1}/{n}) {label}"
                self.after(0, lambda text=text: self.status.config(text=text))

            result = core.apply_row(row, dry_run=dry_run, pattern=pattern, fetch_art=fetch_art,
                                     force_art=force_art, improve_metadata=online_lookup)
            results.append(result)

            tally_parts = []

            if online_lookup and not dry_run and result["metadata"]:
                if result["metadata"] == "corrected":
                    meta_corrected += 1
                elif result["metadata"] == "not found":
                    meta_not_found += 1
                elif result["metadata"].startswith("error"):
                    meta_errors += 1
                tally_parts.append(f"lookup: {meta_corrected} corrected, {meta_not_found} not found, {meta_errors} errors")

            if fetch_art and not dry_run and result["art"]:
                if result["art"] == "added":
                    art_added += 1
                elif result["art"].startswith("skipped"):
                    art_skipped += 1
                elif result["art"] == "not found":
                    art_not_found += 1
                elif result["art"].startswith("error"):
                    art_errors += 1
                tally_parts.append(f"art: {art_added} added, {art_skipped} skipped, {art_not_found} not found, {art_errors} errors")

            if tally_parts and not dry_run:
                tally = " — ".join(tally_parts)
                self.after(0, lambda i=i, n=n, label=label, tally=tally: self.status.config(
                    text=f"({i + 1}/{n}) {label} — {tally}"))

        self.after(0, lambda: self._apply_done(results, dry_run))

    def _apply_done(self, results, dry_run):
        self.apply_btn.config(state="normal")
        self.preview_btn.config(state="normal")

        renamed = sum(1 for r in results if r["renamed"])
        tagged = sum(1 for r in results if r["tagged"])
        errors = [r for r in results if r["error"]]
        art_added = sum(1 for r in results if r["art"] == "added")
        art_not_found = sum(1 for r in results if r["art"] == "not found")
        art_errors = [r for r in results if r["art"] and str(r["art"]).startswith("error")]
        meta_corrected = sum(1 for r in results if r["metadata"] == "corrected")
        meta_not_found = sum(1 for r in results if r["metadata"] == "not found")
        meta_errors = [r for r in results if r["metadata"] and str(r["metadata"]).startswith("error")]

        if dry_run:
            lines = [f"{Path(r['original_path']).name}  ->  {Path(r['dest']).name}" for r in results[:25]]
            more = f"\n…and {len(results) - 25} more." if len(results) > 25 else ""
            self.status.config(text=f"Preview: {len(results)} file(s) would be renamed/tagged.")
            messagebox.showinfo("Dry run preview", "\n".join(lines) + more)
            return

        meta_summary = ""
        if self.online_lookup.get():
            meta_summary = f" Online lookup: {meta_corrected} corrected"
            if meta_not_found:
                meta_summary += f", {meta_not_found} not found"
            if meta_errors:
                meta_summary += f", {len(meta_errors)} error(s)"
            meta_summary += "."

        art_summary = ""
        if self.fetch_art.get():
            art_summary = f" Album art: {art_added} added"
            if art_not_found:
                art_summary += f", {art_not_found} not found"
            if art_errors:
                art_summary += f", {len(art_errors)} error(s)"
            art_summary += "."

        self.status.config(text=f"Done. Renamed {renamed}, tagged {tagged} file(s)." +
                            (f" {len(errors)} error(s)." if errors else "") + meta_summary + art_summary)

        if errors:
            detail = "\n".join(f"{Path(e['original_path']).name}: {e['error']}" for e in errors[:20])
            messagebox.showwarning("Finished with some errors", detail)
        else:
            messagebox.showinfo("Done", f"Renamed {renamed} file(s) and updated tags on {tagged} file(s)." + meta_summary + art_summary)

        # rescan to reflect new filenames/tags on disk
        if self.folder.get():
            self.start_scan()
        else:
            # library was built from drops, not a folder — rescan those exact (now renamed) paths
            new_paths = [r["dest"] for r in results if r.get("dest")]
            if new_paths:
                pattern = self.pattern.get()
                thread = threading.Thread(target=self._scan_paths_worker_replace, args=(new_paths, pattern), daemon=True)
                thread.start()

    def _scan_paths_worker_replace(self, paths, pattern):
        try:
            rows = core.scan_paths(paths, pattern=pattern)
        except Exception:
            return
        self.after(0, lambda: self._scan_done(rows))


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
