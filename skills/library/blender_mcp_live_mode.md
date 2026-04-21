---
name: blender_mcp_live_mode
description: Interactive Blender MCP workflow with viewport-first edits
tags: [blender, mcp, viewport, 3d, design]
triggers: [blender mcp, blender live, viewport, scene edit, material, camera, lighting, low poly, 3d scene]
---

# Blender MCP live workflow

When MCP blender tools are available (`mcp_blender_*` in tool list), use them for interactive work:

1) Inspect scene first (scene/object list tool).
2) Create or modify objects/material/camera incrementally.
3) Ask for viewport snapshot after major changes.
4) Keep steps small and reversible.
5) For final deterministic export/render, use local `blender` tool if needed.

## Safety
- Prefer tool calls over arbitrary code execution.
- If code execution tool is required, keep the code minimal and scoped to the requested scene change.
