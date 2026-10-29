#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from common import load_graph, scene_root


def main() -> None:
    p = argparse.ArgumentParser(description='Render PanoGraph edges one isolated GPU process at a time')
    p.add_argument('scene', nargs='?', default='hindoor_unibs')
    p.add_argument('--profile', choices=['512','4k'], default=os.environ.get('PS_PROFILE','512'))
    p.add_argument('--max-edges', type=int, default=0)
    p.add_argument('--continue-on-error', action='store_true')
    args = p.parse_args()
    graph = load_graph(args.scene)
    edges = graph['edges'][:args.max_edges or None]
    root = scene_root(args.scene) / '05_pansplat'
    root.mkdir(parents=True, exist_ok=True)
    results=[]
    for i,e in enumerate(edges,1):
        a,b=e['a'],e['b']
        print(f'\n=== edge {i}/{len(edges)}: {a} -> {b} ===', flush=True)
        cmd=[sys.executable,'/opt/namma-pansplat/scripts/render_edge.py',args.scene,a,b,'--profile',args.profile]
        run=subprocess.run(cmd)
        item={'a':a,'b':b,'returncode':run.returncode}
        results.append(item)
        if run.returncode != 0 and not args.continue_on_error:
            (root/'summary.json').write_text(json.dumps({'scene':args.scene,'profile':args.profile,'complete':False,'edges':results},indent=2))
            raise SystemExit(run.returncode)
    complete=all(r['returncode']==0 for r in results)
    summary={'scene':args.scene,'profile':args.profile,'complete':complete,'edges':results,'rendered':sum(r['returncode']==0 for r in results),'total':len(results)}
    (root/'summary.json').write_text(json.dumps(summary,indent=2))
    print('\n=== PANOSPLAT EDGE BATCH COMPLETE ===')
    print(json.dumps(summary,indent=2))

if __name__ == '__main__': main()
