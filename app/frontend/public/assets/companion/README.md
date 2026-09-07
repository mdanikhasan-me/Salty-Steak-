# Otter companion assets

The companion uses the user's supplied otter and iPad/Pencil models. Their original distribution license was not supplied; these assets are for this local application pending rights verification.

`viewer-source.js` is the maintained renderer source. Bundle it with Three.js r180 and esbuild into `otter-viewer.js`. Three.js license is included in this folder.

The Blender source and authored motion are retained in `validation/companion-tablet-20260907/Otter-Tablet-Companion.blend`. The skin preserves the supplied mesh and UVs and adds forepaw/wrist controls. `Work_Enter`, `Work_Loop`, and `Work_Exit` are state-controlled; other clips are one-shot interactions.
