#!/usr/bin/env python3
from __future__ import annotations
import argparse,json
from common import load_graph,scene_root
p=argparse.ArgumentParser(); p.add_argument('scene',nargs='?',default='hindoor_unibs'); a=p.parse_args()
g=load_graph(a.scene); root=scene_root(a.scene)/'05_pansplat'/'transitions'
rows=[]
for e in g['edges']:
    names=[f"{e['a']}__{e['b']}",f"{e['b']}__{e['a']}"]
    hit=next((root/f'{n}.webm' for n in names if (root/f'{n}.webm').is_file()),None)
    rows.append({'a':e['a'],'b':e['b'],'video':str(hit) if hit else None,'ready':bool(hit and hit.stat().st_size>10000)})
summary={'scene':a.scene,'edges':len(rows),'ready':sum(x['ready'] for x in rows),'complete':all(x['ready'] for x in rows),'items':rows}
print(json.dumps(summary,indent=2))
raise SystemExit(0 if summary['complete'] else 2)
