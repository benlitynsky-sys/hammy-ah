"""interactive.py - the market report as a web page: hover (or tap) any item name to see its hour-by-hour
price for the last 7 days, with the number of listings; move along the line to read each hour. Click to pin it.
Horde AH 113 (WoW Forever, US, PvP) data only - the same data as the one-page report."""
import json
import os
import re

import report as R

JS = r"""
const D = JSON.parse(document.getElementById('series').textContent);
const pop = document.getElementById('pop');
let pinned = null;
function money(c){c=Math.round(c);const g=Math.floor(c/10000),s=Math.floor(c%10000/100),k=c%100;
  return g?`${g}g ${String(s).padStart(2,'0')}s`:s?`${s}s ${String(k).padStart(2,'0')}c`:`${k}c`}
function fmt(t){return new Date(t*1000).toLocaleString([], {weekday:'short',hour:'numeric',minute:'2-digit'})}
function draw(iid, name){
  const d = D[iid]; if(!d || !d.p.length){pop.innerHTML=`<b>${name}</b><div class="mut">no hourly data yet</div>`;return}
  const W=560,H=210,L=52,Rt=12,T=12,B=30, t0=d.t0, t1=d.t[d.t.length-1];
  const lo=Math.min(...d.p), hi=Math.max(...d.p), pad=(hi-lo)*.08||hi*.05;
  const y0=lo-pad, y1=hi+pad, X=t=>L+(t-t0)/(Math.max(1,t1-t0))*(W-L-Rt), Y=p=>T+(1-(p-y0)/(y1-y0))*(H-T-B);
  let grid='', days='';
  for(let k=0;k<=3;k++){const v=y0+(y1-y0)*k/3; grid+=`<line x1="${L}" x2="${W-Rt}" y1="${Y(v)}" y2="${Y(v)}" class="gl"/><text x="${L-5}" y="${Y(v)+3}" class="ax" text-anchor="end">${money(v)}</text>`}
  const dd=new Date(t0*1000); dd.setHours(24,0,0,0);
  for(let t=dd/1000;t<t1;t+=86400){days+=`<line x1="${X(t)}" x2="${X(t)}" y1="${T}" y2="${H-B}" class="dl"/><text x="${X(t)+3}" y="${H-B+13}" class="ax">${new Date(t*1000).toLocaleDateString([], {weekday:'short',month:'numeric',day:'numeric'})}</text>`}
  const med=d.med, path=d.t.map((t,i)=>`${i&&d.t[i-1]<t-5400?'M':i?'L':'M'}${X(t).toFixed(1)},${Y(d.p[i]).toFixed(1)}`).join('');
  pop.innerHTML=`<div class="ph"><b>${name}</b><span>now <b>${money(d.p[d.p.length-1])}</b> &middot; usual ${money(med)} &middot; 7-day low <span class="dn">${money(lo)}</span> &middot; high <span class="up">${money(hi)}</span></span></div>
  <svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}">${grid}${days}
   <line x1="${L}" x2="${W-Rt}" y1="${Y(med)}" y2="${Y(med)}" class="md"/><text x="${W-Rt}" y="${Y(med)-3}" class="ax" text-anchor="end">usual</text>
   <path d="${path}" class="ln"/><g id="hv" style="display:none"><line id="hx" y1="${T}" y2="${H-B}" class="hx"/><circle id="hc" r="4" class="hc"/></g>
   <rect x="${L}" y="${T}" width="${W-L-Rt}" height="${H-T-B}" fill="transparent" id="hit"/></svg>
  <div class="tip" id="tip">move along the line to read each hour${pinned?' &middot; click the name again or press Esc to close':' &middot; click the name to pin'}</div>`;
  const hit=pop.querySelector('#hit'), hv=pop.querySelector('#hv'), tip=pop.querySelector('#tip'), svg=pop.querySelector('svg');
  hit.addEventListener('mousemove',e=>{const r=svg.getBoundingClientRect(), x=(e.clientX-r.left)*W/r.width;
    let i=0,b=1e18; d.t.forEach((t,j)=>{const q=Math.abs(X(t)-x); if(q<b){b=q;i=j}});
    hv.style.display=''; pop.querySelector('#hx').setAttribute('x1',X(d.t[i])); pop.querySelector('#hx').setAttribute('x2',X(d.t[i]));
    const c=pop.querySelector('#hc'); c.setAttribute('cx',X(d.t[i])); c.setAttribute('cy',Y(d.p[i]));
    const vs=d.p[i]/med-1;
    tip.innerHTML=`<b>${fmt(d.t[i])}</b> &middot; ${money(d.p[i])} <span class="${vs>=.03?'up':vs<=-.03?'dn':'mut'}">(${vs>=0?'+':''}${Math.round(vs*100)}% vs usual)</span> &middot; ${d.q[i].toLocaleString()} listed`});
  hit.addEventListener('mouseleave',()=>{hv.style.display='none'});
}
function place(td){const r=td.getBoundingClientRect(), w=pop.offsetWidth, h=pop.offsetHeight;
  let x=r.right+10, y=r.top-20; if(x+w>innerWidth-8)x=Math.max(8,r.left-w-10); if(x<8)x=8;
  if(y+h>innerHeight-8)y=innerHeight-h-8; if(y<8)y=8; pop.style.left=x+'px'; pop.style.top=y+'px'}
function show(td){draw(td.dataset.iid, td.childNodes[0].textContent.trim()); pop.style.display='block'; place(td)}
document.querySelectorAll('[data-iid]').forEach(td=>{
  td.addEventListener('mouseenter',()=>{if(!pinned)show(td)});
  td.addEventListener('mouseleave',e=>{if(!pinned&&!pop.contains(e.relatedTarget))pop.style.display='none'});
  td.addEventListener('click',()=>{if(pinned===td){pinned=null;pop.style.display='none'}else{pinned=td;show(td)}});
});
pop.addEventListener('mouseleave',e=>{if(!pinned&&!(e.relatedTarget&&e.relatedTarget.dataset&&e.relatedTarget.dataset.iid))pop.style.display='none'});
document.addEventListener('keydown',e=>{if(e.key==='Escape'){pinned=null;pop.style.display='none'}});
document.addEventListener('click',e=>{if(pinned&&!pop.contains(e.target)&&!e.target.closest('[data-iid]')){pinned=null;pop.style.display='none'}});
"""

CSS = """
body{padding:18px}
.wrap{max-width:1500px;margin:0 auto}
[data-iid]{cursor:pointer;text-decoration:underline dotted #7d8692;text-underline-offset:2px}
[data-iid]:hover{color:#58a6ff}
#pop{position:fixed;display:none;z-index:9;background:#161b22;border:1px solid #3a424d;color:#e6e9ee;border-radius:8px;
 box-shadow:0 8px 28px rgba(0,0,0,.6);padding:10px 12px;font:12px/1.4 "Segoe UI",system-ui,sans-serif;max-width:calc(100vw - 16px)}
#pop svg{display:block;max-width:100%;height:auto;margin-top:6px}
#pop .ph{display:flex;flex-direction:column;gap:2px}#pop .ph b:first-child{font-size:14px}
#pop .ph span{color:#aab2bd}
#pop .gl{stroke:#232a33}#pop .dl{stroke:#2e3640;stroke-dasharray:2 3}#pop .md{stroke:#7d8692;stroke-dasharray:4 3}
#pop .ax{font-size:10px;fill:#7d8692}
#pop .ln{fill:none;stroke:#58a6ff;stroke-width:2;stroke-linejoin:round}
#pop .hx{stroke:#e6e9ee;stroke-width:1}#pop .hc{fill:#58a6ff;stroke:#161b22;stroke-width:2}
#pop .tip{margin-top:4px;color:#aab2bd;min-height:17px}
.hint{color:#aab2bd;margin:0 0 6px;font:12px "Segoe UI",system-ui,sans-serif}
"""


def make(m, title, body, path):
    """Write the interactive web page for this report body next to the PDF."""
    days = 7 * 86400
    data = {}
    for iid in sorted({int(x) for x in re.findall(r'data-iid="(\d+)"', body)}):
        rows = [r for r in m.rows.get(iid, []) if r[0] >= m.last - days]
        hours = {}
        for ts, p, q, *_ in rows:
            hours.setdefault(ts // 3600, []).append((p, q, ts))
        if not hours:
            continue
        t = sorted(hours)
        ps = [sorted(v[0] for v in hours[h])[len(hours[h]) // 2] for h in t]
        med = sorted(ps)[len(ps) // 2]
        keep = [i for i, p in enumerate(ps) if 0.4 * med <= p <= 2.5 * med]   # drop one-hour listing gaps
        data[iid] = {"t0": t[0] * 3600, "t": [hours[t[i]][-1][2] for i in keep], "p": [ps[i] for i in keep],
                     "q": [hours[t[i]][-1][1] for i in keep], "med": med}
    import onepage
    page = (f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{title}</title><style>{onepage.CSS.replace("__ZOOM__", "1.55")}{CSS}</style></head><body><div class="wrap">'
            f'<p class="hint">Hover over (or tap) any item name for its hour-by-hour price over the last 7 days. Click to pin it open.</p>'
            f'{body}</div><div id="pop"></div>'
            f'<script type="application/json" id="series">{json.dumps(data, separators=(",", ":"))}</script>'
            f'<script>{JS}</script></body></html>')
    open(path, "w", encoding="utf-8").write(page)
    return path
