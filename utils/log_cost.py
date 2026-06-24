#!/usr/bin/env python3
"""Cost + wall-clock analysis of crustify agent logs.

Each agent log ends in a box-footer `╰─ tokens=N,NNN  cost=$X.XXXX ─╯`.
Two views:
  * per agent KIND  (port / wrap / merge / scaffold / analyze) — kind from the
    session-log filename prefix (reliable; symbol-wrap heads don't say
    "Crustify"); cost = Σ footers per file (restarts emit >1 → summed);
    wall-clock = file birth(%W)→mtime(%Y).
  * per WAVE        — session dirs mapped to the wave whose commit immediately
    follows the dir's merge mtime; cost split by agent kind.

DETACHED logs (`w{N}_detached.log`) are EXCLUDED: they are redundant aggregate
re-captures of the same session-logged waves (parallel-interleaved, so footer↔
kind is unrecoverable anyway) and would double-count (~$570).

Usage:  python3 log_cost.py <repo_root>        # e.g. /root/git/libgit2
"""
import sys, os, re, glob, subprocess
from collections import defaultdict

REPO = sys.argv[1] if len(sys.argv) > 1 else "/root/git/libgit2"
LOG  = f"{REPO}/crustify/targets/src/libgit2/logs"
FOOT = re.compile(r'tokens=[\d,]+\s+cost=\$([0-9.]+)')

def kind(fn):
    for p,k in (("port_","port"),("wrap_","wrap"),("merge","merge"),
                ("scaffolder","scaffold"),("type_analyzer","analyze"),("buffer","analyze")):
        if fn.startswith(p): return k
    return "other"

def stat(path, fmt):  # %W birth, %Y mtime
    return int(subprocess.run(["stat","-c",fmt,path],capture_output=True,text=True).stdout or 0)

def footers(path):
    return [float(m.group(1)) for l in open(path,errors='replace') for m in [FOOT.search(l)] if m]

def hm(s): s=int(s); return f"{s//3600}h{(s%3600)//60:02d}m"

# ---- per-kind (session logs only) ----
kc=defaultdict(float); kr=defaultdict(int); krs=defaultdict(int); kw=defaultdict(int)
for p in glob.glob(f"{LOG}/**/*.log", recursive=True):
    if p.endswith("_detached.log"): continue
    fs=footers(p)
    if not fs: continue
    k=kind(os.path.basename(p))
    kc[k]+=sum(fs); kr[k]+=1; krs[k]+= (len(fs)>1)
    kw[k]+=max(stat(p,"%Y")-stat(p,"%W"),0)
print("=== PER AGENT KIND (session logs; detached excluded) ===")
print(f"{'kind':<10}{'runs':>5}{'restart':>8}{'$ total':>10}{'$/run':>8}{'Σwall':>9}")
tc=tr=0; tw=0
for k in ["port","wrap","merge","scaffold","analyze","other"]:
    if not kr.get(k): continue
    print(f"{k:<10}{kr[k]:>5}{krs[k]:>8}{kc[k]:>10.2f}{kc[k]/kr[k]:>8.2f}{hm(kw[k]):>9}")
    tc+=kc[k]; tr+=kr[k]; tw+=kw[k]
print(f"{'Σ':<10}{tr:>5}{'':>8}{tc:>10.2f}{'':>8}{hm(tw):>9}")

# ---- per-wave (map session dirs between consecutive wave commits) ----
out=subprocess.run(["git","-C",REPO,"log","--all","--format=%ct %s"],capture_output=True,text=True).stdout
waves={}
for l in out.splitlines():
    m=re.search(r'crustify: L(\d+) ',l)
    if m and ('wave' in l or 'pack-reader' in l): waves.setdefault(int(m.group(1)),int(l.split()[0]))
ct=sorted((waves[l],l) for l in waves)
B=defaultdict(lambda: defaultdict(float))
for d in glob.glob(f"{LOG}/2026-06-*"):
    if not os.path.isdir(d): continue
    if not (glob.glob(f"{d}/port_*.log") or glob.glob(f"{d}/wrap_*.log")): continue
    mg=glob.glob(f"{d}/merge*.log"); mt=stat(mg[0] if mg else d,"%Y")
    cand=[l for t,l in ct if t>=mt-60]
    if not cand: continue
    layer=min(cand,key=lambda l:waves[l])
    for p in glob.glob(f"{d}/*.log"):
        k=kind(os.path.basename(p))
        if k in ("port","wrap","merge"): B[layer][k]+=sum(footers(p))
print("\n=== PER WAVE (port/wrap/merge $) ===")
g=0
for l in sorted(B):
    b=B[l]; t=b['wrap']+b['port']+b['merge']; g+=t
    print(f"  L{l:<3} wrap={b['wrap']:6.1f} port={b['port']:6.1f} merge={b['merge']:5.1f}  total={t:6.1f}")
print(f"  WAVE Σ = ${g:.2f}  | + scaffold ${kc['scaffold']:.2f} + analyze ${kc['analyze']:.2f} = ${g+kc['scaffold']+kc['analyze']:.2f}")
