# Volumetric Sheet Compressor (disco is dead)

Gets JSON with Tabletop Simulator objects, compresses decksheets (`FaceURL` / `BackURL`) without visible quality loss 
(at least tries to), and then rewrites JSON at the re-uploaded files.

## Setup

Needs 64-bit Python 3.12 or newer (Windows, Linux or macOS).

```
python -m venv .venv
.venv\Scripts\activate            # Linux/macOS: source .venv/bin/activate
pip install -e ".[gui]"
vsc-gui
```

Next time, run `.venv\Scripts\vsc-gui` (Linux/macOS: `.venv/bin/vsc-gui`).

## From JSON to JSON with new decksheets

The numbers match the step buttons in the toolbar.

1. **New project.** Projects are stored in `projects/<name>/`.
2. **Scan.** Add your JSON files or folders with JSON files, containing TTS objects. The tool finds every
   decksheet and checks its size online.
3. **Pick sheets.** Set *Threshold (KB)* above the table (default 2048 = 2 MB), then click *Check over
   threshold* or tick sheets yourself. *Download checked* is optional; it fetches the originals so you
   can preview them first.
4. **Compress checked.** The defaults (Target Quality 80, JPEG then WebP) give no visible difference
   side by side. Sheets that meet the target are approved automatically. Then review in the Compare
   tab: flip or slide between the original and the result at 100% scale. Use **A** to approve and
   **R** to reject. Re-encode a sheet with a different format or quality if needed.
5. **Export.** Approved files go to `projects/<name>/out/`.
6. **Map uploads.** First upload those files in TTS: *Modding → Cloud Manager → Upload* (don't edit or
   re-save them before uploading). Then confirm the path to TTS's upload list (`CloudInfo.bson`, found
   automatically when possible; the dialog shows where it lives) or paste the new URLs instead. The
   tool finds each file's new URL by its content and checks that it works.
7. **Apply.** Preview the changes, then confirm. Old URLs are replaced in every text file under
   the scanned folders, and backups are kept. *File → Undo last apply* reverts the change.

`projects/<name>/reports/final_report.csv` lists every sheet with its status, old and new URL,
and sizes. `references.csv` lists where each sheet is used.

> Apply edits your files in place. Each changed file is backed up first, and *File → Undo last apply*
> restores it (edits you make after Apply are lost by Undo).

## Command line

The same stuff without the GUI. `vsc <command> -h` for details. More of an afterthought, never tested it live, better use GUI version

```
vsc scan -p myproject path/to/folder
vsc compress -p myproject
vsc map -p myproject --apply
vsc undo -p myproject
```
