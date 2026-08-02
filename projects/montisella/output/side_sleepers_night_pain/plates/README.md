# Plates

Image-model output ONLY: backgrounds, product shots, scene plates. No text — the
compositor draws every word.

Name them `<concept_id>_<what>.png` (e.g. `C01_pillow_dark.png`) and reference them
relatively from `concepts.json`:

    "image": "plates/C01_pillow_dark.png"

`qa.py` warns on any plate it cannot find.
