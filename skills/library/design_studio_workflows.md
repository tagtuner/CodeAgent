---
name: design_studio_workflows
description: Designer uploads, ImageMagick/Blender patterns, and multi-variant exports in CodeAgent workspace
tags: [design, imagemagick, blender, studio, graphics]
triggers: [design, designer, mockup, banner, logo, pattern, variation, upload, uploads, attachment, imagemagick, convert, blender, png, svg, thumbnail, layout]
---

# Design studio (CodeAgent + workspace)

## When the user attached image/files
- Files are stored under **`uploads/`** in the **current session workspace** (same tree as worker `w1`, `w2`, …).
- From bash in a worker: **`WS="$(dirname "$PWD")"`** then paths like **`$WS/uploads/<file>.png`**.
- Inspect images with **`identify`**, **`file`**, **`ls -lh`** — never `read_file` on binary images.

## Multi-pattern / multi-variant (e.g. “5 different patterns”)
1. Confirm input path(s) under `$WS/uploads/`.
2. Plan distinct variants (color palette, border style, texture overlay, crop, duotone, grid layout) — keep names deterministic: `variant_01.png` … `variant_05.png` under **`$WS/`** (workspace root) so Studio preview finds them.
3. Use **ImageMagick** for 2D variants; use **Blender** (`blender -b ... -o ...`) only when 3D or precise mesh/material is required.
4. After each generation: **`ls -lh`** + **`file`** on outputs; echo **`$OUT`** paths.

## ImageMagick defaults (this host)
- Font: **`-font Helvetica`** unless user asked otherwise and font was verified (`convert -list font`).
- Safe timestamps: **`TS="$(date '+%Y-%m-%d %H:%M:%S')"`** and quote in `-annotate`.

## Blender (batch)
- Prefer **headless**: `blender -b <file.blend> -P <script.py> -o //out_#### -F PNG -x 1 -a` or scripted single frame — keep outputs under **`$WS/`**.

## Quality checklist
- Resolution and aspect ratio match user request.
- No accidental writes outside `$WS` (no absolute paths outside workspace without user approval).
