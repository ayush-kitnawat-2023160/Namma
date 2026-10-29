#!/usr/bin/env python3
import importlib
import json
import sys
mods = ['torch','torchvision','xformers','hydra','omegaconf','PIL','numpy','cv2','moviepy']
out = {'python': sys.version.split()[0]}
for m in mods:
    mod = importlib.import_module(m)
    out[m] = getattr(mod, '__version__', 'ok')
out['pansplat_repo'] = __import__('pathlib').Path('/opt/PanSplat/src').is_dir()
print(json.dumps(out, indent=2))
