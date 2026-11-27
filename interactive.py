"""interactive.py - the market report as a web page: hover (or tap) any item name to see its hour-by-hour
price for the last 7 days, with the number of listings; move along the line to read each hour. Click to pin it.
Horde AH 113 (WoW Forever, US, PvP) data only - the same data as the one-page report."""
import json
import os
import re

import report as R

JS = r"""
const D = JSON.parse(document.getElementById('series').textContent);
const RC = JSON.parse((document.getElementById('rcinfo')||{}).textContent||'{}');
function rcBtn(iid){const r=RC[String(iid).replace(/^lk/,'')]; if(!r)return '';
  const row=(k,v)=>v?`<div><span class="mut">${k}</span> ${v}</div>`:'';
  return `<div class="rcw"><button type="button" class="rcb">${r.w}</button><div class="rct"><b>${r.n}</b>`+
    row('Profession',r.prof+(r.sk?' ('+r.sk+')':''))+row('Where',r.src)+row('On AH',r.ah)+row('Needs',r.mats)+row('Mats cost now',r.cost)+row('Cooldown',r.cd)+(r.bop?'<div class="mut">Binds on pickup - you must get it yourself</div>':'')+`</div></div>`}
const TT = JSON.parse((document.getElementById('ttinfo')||{}).textContent||'{}');
const QC={POOR:'#9d9d9d',COMMON:'#e6e9ee',UNCOMMON:'#1eff00',RARE:'#0070dd',EPIC:'#a335ee',LEGENDARY:'#ff8000'};
function ttBox(iid){const x=TT[String(iid).replace(/^lk/,'')]; if(!x||!x[1].length)return '';
  return '<div class="ttb">'+x[1].map(([s,c])=>`<div class="${c}">${s.replace(/</g,'&lt;')}</div>`).join('')+'</div>'}
function qName(iid,name){const x=TT[String(iid).replace(/^lk/,'')]; return x?`<b style="color:${QC[x[0]]||'#e6e9ee'}">${name}</b>`:`<b>${name}</b>`}
function rcWire(el){const w=el.querySelector('.rcw'); if(!w)return;
  w.querySelector('.rcb').addEventListener('click',e=>{e.stopPropagation(); w.classList.toggle('open')})}
const pop = document.getElementById('pop');
let pinned = null;
function money(c){c=Math.round(c);const g=Math.floor(c/10000),s=Math.floor(c%10000/100),k=c%100;
  return g?`${g}g ${String(s).padStart(2,'0')}s`:s?`${s}s ${String(k).padStart(2,'0')}c`:`${k}c`}
function fmt(t){return new Date(t*1000).toLocaleString('en-US', {timeZone:'America/Phoenix',weekday:'short',hour:'numeric',minute:'2-digit',hour12:true})+' PST'}
function draw(iid, name, el=pop){
  const d = D[iid]; if(!d || !d.p.length){el.innerHTML=`${rcBtn(iid)}<div class="ph${RC[String(iid).replace(/^lk/,'')]?' hasrc':''}">${qName(iid,name)}</div>${ttBox(iid)}<div class="mut">never listed on the Horde PvP ruleset AH since we started tracking it</div>`;rcWire(el);return}
  const W=560,H=210,L=52,Rt=12,T=12,B=30, t0=d.t0, t1=d.t[d.t.length-1];
  const lo=Math.min(...d.p), hi=Math.max(...d.p), pad=(hi-lo)*.08||hi*.05;
  const y0=lo-pad, y1=hi+pad, X=t=>L+(t-t0)/(Math.max(1,t1-t0))*(W-L-Rt), Y=p=>T+(1-(p-y0)/(y1-y0))*(H-T-B);
  let grid='', days='';
  for(let k=0;k<=3;k++){const v=y0+(y1-y0)*k/3; grid+=`<line x1="${L}" x2="${W-Rt}" y1="${Y(v)}" y2="${Y(v)}" class="gl"/><text x="${L-5}" y="${Y(v)+3}" class="ax" text-anchor="end">${money(v)}</text>`}
  const dd=new Date(t0*1000); dd.setHours(24,0,0,0);
  const nd=Math.ceil((t1-t0)/86400), st=Math.max(1,Math.ceil(nd/7)); let di=0;
  for(let t=dd/1000;t<t1;t+=86400,di++){if(di%st)continue; days+=`<line x1="${X(t)}" x2="${X(t)}" y1="${T}" y2="${H-B}" class="dl"/><text x="${X(t)+3}" y="${H-B+13}" class="ax">${new Date(t*1000).toLocaleDateString('en-US', nd>10?{timeZone:'America/Phoenix',month:'numeric',day:'numeric'}:{timeZone:'America/Phoenix',weekday:'short',month:'numeric',day:'numeric'})}</text>`}
  const med=d.med, path=d.t.map((t,i)=>`${i&&d.t[i-1]<t-43200?'M':i?'L':'M'}${X(t).toFixed(1)},${Y(d.p[i]).toFixed(1)}`).join('');
  el.innerHTML=`${rcBtn(iid)}<div class="ph${RC[String(iid).replace(/^lk/,'')]?' hasrc':''}">${qName(iid,name)}<span>now <b>${money(d.now||d.p[d.p.length-1])}</b> &middot; usual ${money(med)} &middot; low <span class="dn">${money(lo)}</span> &middot; high <span class="up">${money(hi)}</span></span></div>${ttBox(iid)}
  <svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}">${grid}${days}
   <line x1="${L}" x2="${W-Rt}" y1="${Y(med)}" y2="${Y(med)}" class="md"/><text x="${W-Rt}" y="${Y(med)-3}" class="ax" text-anchor="end">usual</text>
   <path d="${path}" class="ln"/><g id="hv" style="display:none"><line id="hx" y1="${T}" y2="${H-B}" class="hx"/><circle id="hc" r="4" class="hc"/></g>
   <rect x="${L}" y="${T}" width="${W-L-Rt}" height="${H-T-B}" fill="transparent" id="hit"/></svg>
  <div class="tip" id="tip">move along the line to read each hour${el!==pop?'':pinned?' &middot; click the name again or press Esc to close':' &middot; click the name to pin'}</div>`;
  rcWire(el);
  const hit=el.querySelector('#hit'), hv=el.querySelector('#hv'), tip=el.querySelector('#tip'), svg=el.querySelector('svg');
  hit.addEventListener('mousemove',e=>{const r=svg.getBoundingClientRect(), x=(e.clientX-r.left)*W/r.width;
    let i=0,b=1e18; d.t.forEach((t,j)=>{const q=Math.abs(X(t)-x); if(q<b){b=q;i=j}});
    hv.style.display=''; el.querySelector('#hx').setAttribute('x1',X(d.t[i])); el.querySelector('#hx').setAttribute('x2',X(d.t[i]));
    const c=el.querySelector('#hc'); c.setAttribute('cx',X(d.t[i])); c.setAttribute('cy',Y(d.p[i]));
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
// each section becomes a beveled picture tile; tap it to open that section's tables
const ART={'AH Deviation Watchlist':['🔄','#2f81f7','#0b2a5e'],'AH Most Active':['📈','#2ea043','#0b3318'],
  'AH Manipulation':['🕵️','#a371f7','#2a1556'],'Cooldowns':['⏳','#e3b341','#4a3205'],
  'Raid consumables':['🐉','#f85149','#4a0d0c'],'Cooking':['🍖','#f0883e','#4a2208'],'Top crafts':['⚒️','#8b949e','#1c2128'],'1-150, 150-300 Profession Kits':['🧰','#39c5cf','#0b3a3e'],"Likely all you’ll use this for":['🔍','#58a6ff','#102a4c'],'New in Forever':['🧨','#d2a8ff','#3a1d5c'],'New Forever Crafts':['⚗️','#56d4dd','#0b3a3e'],'Durotar Supply and Logistics':['📦','#e3b341','#4a3205'],"Hammy's Plays":['🎯','#3fb950','#0d3a1a']};
// button order (search stays first)
const ORDER=['AH Most Active','AH Deviation Watchlist','AH Manipulation','Raid consumables','Durotar Supply and Logistics','Top crafts','Cooldowns','1-150, 150-300 Profession Kits','Cooking',
  'New in Forever',"Hammy's Plays",'New Forever Crafts'];
(()=>{const secs=[...document.querySelectorAll('.wrap > section')], nm=s=>((s.querySelector(':scope > .sh h2')||{}).textContent||'').trim();
  let at=secs.find(s=>nm(s).startsWith('Likely all')); if(!at)return;
  ORDER.forEach(n=>{const s=secs.find(x=>nm(x)===n); if(s){at.after(s); at=s}})})();
let openSet=[]; try{openSet=JSON.parse(localStorage.getItem('open')||'[]')}catch(e){}
document.querySelectorAll('.wrap > section').forEach(sec=>{
  const sh=sec.querySelector(':scope > .sh'); if(!sh)return;
  const name=sh.querySelector('h2').textContent.trim(), tag=(sh.querySelector('.tag')||{}).textContent||'';
  const [icon,c1,c2]=ART[name]||['📊','#58a6ff','#0d2d5e'];
  const body=document.createElement('div'); body.className='sb';
  while(sh.nextSibling)body.appendChild(sh.nextSibling);
  const t=document.createElement('button'); t.className='tile'; t.type='button';
  t.style.setProperty('--c1',c1); t.style.setProperty('--c2',c2);
  t.innerHTML=`<span class="bg">${icon}</span><span class="ic">${icon}</span><span class="tt">${name}</span>${name==='Raid consumables'&&tag?`<span class="tg">${tag}</span>`:''}<span class="chev">&#9662;</span>`;
  sh.replaceWith(t); sec.appendChild(body); sec.classList.add('card');
  // group labels (Drops, Quests, a cooldown's name...) stay on screen when the table is swiped sideways
  body.querySelectorAll('tr.grp > td').forEach(td=>{const g=document.createElement('span');g.className='grl';while(td.firstChild)g.appendChild(td.firstChild);td.appendChild(g)});
  if(openSet.includes(name)){sec.classList.add('open');body.style.height='auto'}
  t.addEventListener('click',()=>{
    t.classList.remove('flash'); void t.offsetWidth; t.classList.add('flash');   // quick flash on press
    const opening=!sec.classList.contains('open');
    body.style.height=(opening?0:body.scrollHeight)+'px';
    if(opening)sec.classList.add('open');
    requestAnimationFrame(()=>requestAnimationFrame(()=>{body.style.height=(opening?body.scrollHeight:0)+'px'}));
    body.addEventListener('transitionend',function done(e){if(e.target!==body||e.propertyName!=='height')return;body.removeEventListener('transitionend',done);
      if(opening)body.style.height='auto';else sec.classList.remove('open')});
    setTimeout(()=>{try{const o=[...document.querySelectorAll('section.card.open .tt')].map(x=>x.textContent);localStorage.setItem('open',JSON.stringify(o))}catch(e){}},450)});
});
// Item Lookup: search every named AH 113 item and draw its full price history (data loads on first use)
let LK=null, LKP=null;
function loadLK(){ if(!LKP) LKP=fetch('lookup.json?v='+Date.now()).then(r=>r.json()).then(j=>{LK=j;
    LK.list=Object.entries(j.n).map(([id,n])=>[id,n,n.toLowerCase()]); const st=document.getElementById('lkst'); if(st)st.textContent=LK.list.length.toLocaleString()+' items ('+Object.keys(j.s).length.toLocaleString()+' seen listed)';});
  return LKP }
function lkShow(id,name){
  if(LK.rc&&LK.rc[id])RC[id]=LK.rc[id]; if(LK.tt&&LK.tt[id])TT[id]=LK.tt[id];
  if(!LK.s[id]){const el=document.getElementById('lkchart'); el.innerHTML=rcBtn(id)+'<div class="sub'+(RC[id]?' hasrc':'')+'">'+qName(id,name)+'</div>'+ttBox(id)+'<div class="mut">Not listed on the Horde PvP ruleset AH since we started tracking it (gear from Nov 23, 2026). It can be sold on the AH - nobody has posted one yet.</div>'; rcWire(el);
    document.getElementById('lkres').innerHTML=''; document.getElementById('lkq').value=name; return}
  const s=LK.s[id], h0=LK.h0, t=s.h.map(x=>(h0+x)*3600+((LK.mi||{})[x]||0)), last=t[t.length-1];
  const wk=s.p.filter((p,i)=>t[i]>=last-7*86400).sort((a,b)=>a-b);
  D['lk'+id]={t0:t[0],t,p:s.p,q:s.q,med:wk[Math.floor(wk.length/2)]||s.p[0],now:s.p[s.p.length-1]};
  draw('lk'+id, name+(LK.g.includes(+id)?' <span class="mut">(gear - lowest buyout)</span>':''), document.getElementById('lkchart'));
  document.getElementById('lkres').innerHTML=''; document.getElementById('lkq').value=name;
}
const lkq=document.getElementById('lkq');
if(lkq){
  lkq.addEventListener('focus',loadLK);
  lkq.addEventListener('input',async()=>{await loadLK(); const q=lkq.value.trim().toLowerCase(), box=document.getElementById('lkres');
    if(q.length<2){box.innerHTML='';return}
    const ws=q.replace(/[^a-z0-9' ]/g,' ').split(' ').filter(Boolean), sc=x=>ws.filter(w=>x[2].includes(w)).length;
    let hits=LK.list.filter(x=>sc(x)==ws.length);   // every word, any order, punctuation ignored
    if(!hits.length&&ws.length>1)hits=LK.list.filter(x=>sc(x)>=ws.length-1);   // forgive one extra/misspelled word
    hits=hits.sort((a,b)=>(b[2].includes(q)-a[2].includes(q))||(b[2].startsWith(q)-a[2].startsWith(q))||(sc(b)-sc(a))||a[1].length-b[1].length).slice(0,12);
    box.innerHTML=hits.length?hits.map(([id,n])=>`<button type="button" data-lk="${id}">${n}</button>`).join(''):'<span class="mut">no item by that name</span>'});
  document.getElementById('lkres').addEventListener('click',e=>{const b=e.target.closest('[data-lk]'); if(b)lkShow(b.dataset.lk,b.textContent)});
  lkq.addEventListener('keydown',e=>{if(e.key==='Enter'){const b=document.querySelector('#lkres [data-lk]'); if(b)lkShow(b.dataset.lk,b.textContent)}});
}
"""

CSS = """
.ttb{background:#0b0e12;border:1px solid #3a424d;border-radius:6px;padding:5px 8px;margin:5px 0;font-size:11px;line-height:1.4;max-width:540px}
.ttb .g{color:#1eff00}.ttb .m{color:#8b949e}.ttb .y{color:#ffd100}
.lk{padding:4px 2px}
.lk input{width:100%;box-sizing:border-box;padding:7px 9px;border-radius:7px;border:1px solid #3a424d;background:#0b0e12;color:#e6e9ee;font:inherit;font-size:12px}
.lk input:focus{outline:none;border-color:#58a6ff;box-shadow:0 0 0 2px rgba(88,166,255,.25)}
#lkres{display:flex;flex-wrap:wrap;gap:4px;margin:6px 0}
#lkres button{background:#1a1f26;color:#e6e9ee;border:1px solid #2a3038;border-radius:12px;padding:2px 8px;font:inherit;font-size:10px;cursor:pointer}
#lkres button:hover{border-color:#58a6ff;color:#58a6ff}
#lkchart svg{display:block;max-width:100%;height:auto;margin-top:6px}
#lkchart .ph{display:flex;flex-direction:column;gap:2px}

section.card{margin:0 0 8px}
.tile{position:relative;display:flex;flex-direction:column;align-items:center;justify-content:center;width:100%;min-height:62px;
 padding:6px 34px;border:0;border-radius:10px;cursor:pointer;overflow:hidden;color:#fff;font:inherit;text-align:center;
 background:linear-gradient(150deg,var(--c1),var(--c2) 75%);
 box-shadow:inset 0 1px 0 rgba(255,255,255,.35),inset 0 -2px 0 rgba(0,0,0,.35),inset 1px 0 0 rgba(255,255,255,.12),0 3px 8px rgba(0,0,0,.5);
 transition:transform .08s,box-shadow .08s,filter .15s}
.tile:hover{filter:brightness(1.12)}
.tile:active{transform:translateY(1px);box-shadow:inset 0 2px 5px rgba(0,0,0,.45),0 1px 2px rgba(0,0,0,.4)}
.tile .bg{position:absolute;right:-6px;bottom:-14px;font-size:64px;opacity:.18;transform:rotate(-12deg);pointer-events:none}
.tile .ic{font-size:20px;line-height:1.1}
.tile .tt{font-size:15px;font-weight:800;letter-spacing:.02em;text-shadow:0 1px 2px rgba(0,0,0,.6)}
.tile .tg{font-size:9px;opacity:.85;margin-top:1px}
.tile .chev{position:absolute;right:12px;top:50%;transform:translateY(-50%);font-size:12px;opacity:.8;transition:transform .2s}
section.card.open .tile .chev{transform:translateY(-50%) rotate(180deg)}
section.card .sb{height:0;overflow-x:auto;overflow-y:hidden;-webkit-overflow-scrolling:touch;overscroll-behavior-x:contain;opacity:0;transition:height .35s ease,opacity .3s ease}
section.card.open .sb{opacity:1}
section.card .cols{display:block}
section.card .g.lv{table-layout:auto;width:100%}section.card .g.lv th{width:auto!important}
section.card .g.lv td{white-space:nowrap;vertical-align:top;padding:3px 4px}section.card .g.lv td:nth-child(2){white-space:normal;width:100%}
section.card .g .mats{white-space:nowrap}
section.card .sb .g td.nm{position:sticky;left:0;z-index:1;background:var(--bg);box-shadow:4px 0 6px -4px rgba(0,0,0,.8)}
section.card .sb>*:first-child{margin-top:6px}
section.card .sb section{display:contents}
section.card .sb tr.grp td .grl{position:sticky;left:6px;display:inline-block;max-width:calc(100vw - 48px);white-space:normal}
section.card .sb h3,section.card .sb .cgn,section.card .sb .fn,section.card .sb .sh{position:sticky;left:0;max-width:calc(100vw - 32px);box-sizing:border-box}
.tile::after{content:"";position:absolute;inset:0;background:#fff;opacity:0;pointer-events:none}
.tile.flash::after{animation:flash .45s ease-out}
@keyframes flash{0%{opacity:.55}100%{opacity:0}}

body{padding:18px}
.wrap{max-width:1500px;margin:0 auto}
[data-iid]{cursor:pointer;text-decoration:underline dotted #7d8692;text-underline-offset:2px}
[data-iid]:hover{color:#58a6ff}
#lkchart{position:relative}.ph.hasrc{padding-right:84px}
.rcw{position:absolute;top:8px;right:10px;z-index:3}
.rcb{font:700 11px/1 "Segoe UI",system-ui,sans-serif;letter-spacing:.03em;color:#0b1a2e;cursor:pointer;padding:5px 11px;border-radius:5px;
 border:1px solid #2f81f7;background:linear-gradient(180deg,#8cc4ff 0%,#58a6ff 55%,#3b8ae6 100%);
 box-shadow:inset 0 1px 0 rgba(255,255,255,.55),inset 0 -2px 0 rgba(0,0,0,.25),0 2px 4px rgba(0,0,0,.45);text-shadow:0 1px 0 rgba(255,255,255,.35)}
.rcb:active{transform:translateY(1px);box-shadow:inset 0 2px 4px rgba(0,0,0,.4)}
.rct{position:absolute;right:0;top:calc(100% + 6px);width:290px;max-width:calc(100vw - 40px);background:#0f1d33;border:1px solid #58a6ff;
 border-radius:6px;padding:8px 10px;font:12px/1.45 "Segoe UI",system-ui,sans-serif;color:#e6e9ee;box-shadow:0 8px 24px rgba(0,0,0,.6);
 opacity:0;transform:translateY(-8px);max-height:0;overflow:hidden;pointer-events:none;transition:opacity .2s ease,transform .25s ease,max-height .3s ease}
.rct div{margin-top:3px}.rcw:hover .rct,.rcw.open .rct{opacity:1;transform:translateY(0);max-height:420px;pointer-events:auto}
#pop{position:fixed;display:none;z-index:9;background:#161b22;border:1px solid #3a424d;color:#e6e9ee;border-radius:8px;
 box-shadow:0 8px 28px rgba(0,0,0,.6);padding:10px 12px;font:12px/1.4 "Segoe UI",system-ui,sans-serif;max-width:calc(100vw - 16px)}
#pop svg,#lkchart svg{display:block;max-width:100%;height:auto;margin-top:6px}
#pop .ph,#lkchart .ph{display:flex;flex-direction:column;gap:2px}#pop .ph b:first-child,#lkchart .ph b:first-child{font-size:14px}
#pop .ph span,#lkchart .ph span{color:#aab2bd}
#pop .gl,#lkchart .gl{stroke:#232a33}#pop .dl,#lkchart .dl{stroke:#2e3640;stroke-dasharray:2 3}#pop .md,#lkchart .md{stroke:#7d8692;stroke-dasharray:4 3}
#pop .ax,#lkchart .ax{font-size:10px;fill:#7d8692}
#pop .ln,#lkchart .ln{fill:none;stroke:#58a6ff;stroke-width:2;stroke-linejoin:round}
#pop .hx,#lkchart .hx{stroke:#e6e9ee;stroke-width:1}#pop .hc,#lkchart .hc{fill:#58a6ff;stroke:#161b22;stroke-width:2}
#pop .tip,#lkchart .tip{margin-top:4px;color:#aab2bd;min-height:17px}
.hint{color:#aab2bd;margin:0 0 6px;font:12px "Segoe UI",system-ui,sans-serif}
"""


def archived_rows(iids, before):
    """Hourly rows from the permanent monthly archive files in archive/ (if downloaded), older than the working file."""
    import glob
    import sqlite3
    out = {}
    if not iids:
        return out
    q = ",".join("?" * len(iids))
    for db in sorted(glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)), "archive", "ah113-*.sqlite"))):
        try:
            con = sqlite3.connect(db)
            seen = {}
            for t in ("snap", "allsnap"):
                for ts, iid, fl, qty, sold in con.execute(
                        f"SELECT ts, item_id, CASE WHEN qty < 20 AND min_unit > 0 THEN min_unit ELSE floor END, qty, sold_est "
                        f"FROM {t} WHERE ts < ? AND item_id IN ({q})", (before, *iids)):
                    seen[(iid, ts)] = (ts, fl, qty, sold)
            con.close()
            for (iid, _), r in seen.items():
                out.setdefault(iid, []).append(r)
        except sqlite3.Error:
            pass
    for v in out.values():
        v.sort()
    return out


def _noname(m, i):
    n = m.name(i)
    return n.isdigit() or n.startswith("item ")


def write_lookup(m, path):
    """lookup.json for the search tile: every named item's hourly price (archive + working file).
    {"h0": first hour, "n": {id: name}, "g": [gear ids], "s": {id: {"h": hour offsets, "p": prices, "q": listed}}}"""
    named = {i for i in set(m.rows_long) if not _noname(m, i)}
    old = archived_rows(sorted(named), min((v[0][0] for v in m.rows_long.values() if v), default=m.last))
    series, real = {}, {}   # real: hour -> seconds past the hour of that hour's snapshot (shared by every item)
    for i in named:
        hrs = {}
        for ts, p, q, *_ in old.get(i, []) + m.rows_long.get(i, []):
            if p:
                hrs[ts // 3600] = (p, q)
                real[ts // 3600] = max(real.get(ts // 3600, 0), ts % 3600)
        if hrs:
            series[i] = hrs
    gear = {}
    try:   # gear / one-off items: lowest buyout each hour
        for ts, i, mn, n in m.con.execute("SELECT ts, item_id, min_buyout, auctions FROM gearsnap WHERE ts >= ?", (m.t0_long,)):
            if i in series or not mn or _noname(m, i):
                continue
            gear.setdefault(i, {})[ts // 3600] = (mn, n)
            real.setdefault(ts // 3600, ts % 3600)
    except Exception:
        pass
    series.update(gear)
    h0 = min(min(v) for v in series.values())
    out = {"h0": h0, "n": {i: m.name(i) for i in series}, "g": sorted(gear), "s": {},
           "mi": {h - h0: sec for h, sec in real.items() if h >= h0}}
    try:   # every tradeable weapon / armor / recipe in the game, so items nobody has listed yet still turn up
        for k, n in json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "catalog.json"), encoding="utf-8")).items():
            if int(k) not in series:
                out["n"][int(k)] = n
    except (OSError, ValueError):
        pass
    try:
        import tooltips
        gearish = [i for i in out["n"] if (m.items.get(int(i)) or {}).get("cls") in ("Armor", "Weapon") or int(i) in gear or i not in series]
        c = tooltips.fetch(gearish, 400)   # fills in ~400 more each run until every item is cached
        out["tt"] = {i: c[str(i)] for i in out["n"] if c.get(str(i)) and c[str(i)][1]}
    except Exception as e:
        print("lookup tooltips skipped:", e)
    try:
        info = recipe_info(m)
        out["rc"] = {i: info[i] for i in out["n"] if i in info}
    except Exception as e:
        print("lookup recipe buttons skipped:", e)
    for i, hrs in series.items():
        hs = sorted(hrs)
        out["s"][i] = {"h": [h - h0 for h in hs], "p": [int(hrs[h][0]) for h in hs], "q": [int(hrs[h][1] or 0) for h in hs]}
    json.dump(out, open(path, "w", encoding="utf-8"), separators=(",", ":"))
    return path


RECIPE_WORDS = ("Recipe", "Formula", "Pattern", "Plans", "Schematic", "Manual")


def recipe_info(m):
    """{crafted item id: recipe button data} - which recipe/formula teaches it, where to get it, its AH price.
    Classic recipes: recipe_sources.json; Forever recipes: forever_recipes.json (+ DSL camp vendor list)."""
    import csv
    here = os.path.dirname(os.path.abspath(__file__))
    ld = lambda n: json.load(open(os.path.join(here, n), encoding="utf-8"))
    out = {}

    def split(name):
        for w in RECIPE_WORDS:
            if name.startswith(w + ": "):
                return w, name[len(w) + 2:]
        return "Recipe", name

    def ah(rid):
        now = m.ah(rid)[0] or m.gear_now(rid)[0]
        if now:
            return f"{R.money(now) if hasattr(R, 'money') else now} listed now"
        hist = m.series(rid) or m.series(rid, gear=True)
        if hist:
            h = max(0, round((m.last - hist[-1][0]) / 3600))
            return f"last seen {h // 24}d ago" if h >= 48 else f"last seen {h}h ago"
        return "not seen on the Horde PvP ruleset AH yet"

    try:
        dslv = {r["recipe"].lower(): r for r in csv.DictReader(open(os.path.join(here, "dsl_vendor.csv"), encoding="utf-8"), delimiter="|")}
    except OSError:
        dslv = {}
    KIND = {"drop": "Drops from: ", "quest": "Quest reward: ", "far": "Vendor: ", "city": "Vendor: ", "rep": "Reputation vendor: ",
            "alliance": "Alliance only - buy it on the AH. "}
    for rid, r in ld("recipe_sources.json").items():
        w, nm = split(r["name"])
        oid = m.name_id.get(nm.lower())
        if oid and oid not in out:
            out[oid] = dict(w=w, n=R.esc(r["name"]), prof=r["prof"], sk=r.get("skill"),
                            src=R.esc(KIND.get(r["kind"], "") + r["where"]), ah=ah(int(rid)), bop=False)
    for rid, r in ld("forever_recipes.json").items():
        w, nm = split(r["name"])
        oid = r.get("out") or m.name_id.get(nm.lower())
        if not oid or oid in out:
            continue
        v = dslv.get(nm.lower())
        src = (f"Durotar Supply and Logistics camp (The Barrens, outside the Crossroads west gate): {v['standing']}, "
               f"{v['favor']} Merchant&rsquo;s Favor") if v else "New in Forever - source not documented yet"
        out[oid] = dict(w=w, n=R.esc(r["name"]), prof=r["prof"], sk=r.get("skill"), src=src, ah=ah(int(rid)), bop=bool(r.get("bop")))
    for oid, rs in m.by_out.items():   # any other craft taught by a recipe item we know by name
        if oid in out:
            continue
        nm = m.name(oid)
        for w in RECIPE_WORDS:
            rid = m.name_id.get(f"{w.lower()}: {nm.lower()}")
            if rid:
                out[oid] = dict(w=w, n=R.esc(f"{w}: {nm}"), prof=rs[0]["prof"], sk=None,
                                src="Where it comes from isn&rsquo;t in our database yet", ah=ah(rid), bop=False)
                break
    try:
        trainer = {int(k): v for k, v in ld("trainer_crafts.json").items()}   # crafted item id -> trainer skill
    except (OSError, ValueError):
        trainer = {}
    for oid, rs in m.by_out.items():   # no recipe item: trainer-taught, or a Forever recipe we can't place yet
        r = rs[0]
        if oid in out or str(r.get("category") or "").upper() == "SEASON OF DISCOVERY" or oid >= 190000:
            continue
        if oid in trainer:
            out[oid] = dict(w="Recipe", n=R.esc(r["name"]), prof=r["prof"], sk=trainer[oid] or None,
                            src=f"Taught by any {R.esc(r['prof'])} trainer - no recipe to buy", ah="", bop=False)
        else:
            out[oid] = dict(w="Recipe", n=R.esc(r["name"]), prof=r["prof"], sk=None,
                            src="Where to learn it isn&rsquo;t in our database yet", ah="", bop=False)
    cds = {}
    try:
        for r in csv.DictReader(open(os.path.join(here, "recipes.csv"), encoding="utf-8")):
            h = float(r.get("cooldown_hours") or 0)
            if h > 0:
                cds[r["output"].strip().lower()] = h
    except (OSError, KeyError, ValueError):
        pass
    for oid, x in out.items():   # what one craft needs, priced now
        rs = m.by_out.get(oid)
        if not rs:
            continue
        r = rs[0]
        parts = []
        for iid, n, q in r["reagents"]:
            c, _ = m.cost(iid, r["prof"])
            parts.append(f"{q:g} {R.esc(n)}" + (f" <span class=mut>({R.money(c * q)})</span>" if c else ""))
        x["mats"] = ", ".join(parts) + (f" &rarr; makes {r['qty']:g}" if (r.get("qty") or 1) > 1 else "")
        tot = m.recipe_cost(r)
        x["cost"] = R.money(tot) if tot else None
        h = cds.get(m.name(oid).lower())
        if h:
            x["cd"] = f"{h:g} hours (about {round(h / 24, 1):g} days)" if h >= 24 else f"{h:g} hours"
    return out


def make(m, title, body, path):
    """Write the interactive web page for this report body next to the PDF."""
    data = {}
    iids = sorted({int(x) for x in re.findall(r'data-iid="(\d+)"', body)})
    old = archived_rows(iids, min((v[0][0] for v in m.rows_long.values() if v), default=m.last))
    for iid in iids:   # charts show the full history: archive + working file
        rows = old.get(iid, []) + m.rows_long.get(iid, [])
        if not rows:   # gear / one-off items live in the gear table: lowest buyout each hour
            rows = [(ts, p, q) for ts, p, q, *_ in m.gear.get(iid, []) if p]
        hours = {}
        for ts, p, q, *_ in rows:
            hours.setdefault(ts // 3600, []).append((p, q, ts))
        if not hours:
            continue
        t = sorted(hours)
        ps = [sorted(v[0] for v in hours[h])[len(hours[h]) // 2] for h in t]
        wk = [p for h, p in zip(t, ps) if h * 3600 >= m.last - 7 * 86400] or ps
        med = sorted(wk)[len(wk) // 2]   # the 'usual' line = the 7-day median, same as the report
        # drop one-hour listing gaps, but always keep the latest scan so "now" matches the report (a fresh spike is real)
        # only a lone one-hour blip is dropped; a move that lasts 2+ hours (a real rise or fall) stays on the chart
        ok = [0.4 * med <= p <= 2.5 * med for p in ps]
        keep = [i for i in range(len(ps) - 1)
                if ok[i] or (i and not ok[i - 1]) or (i + 1 < len(ps) and not ok[i + 1])] + [len(ps) - 1]
        now = m.ah(iid)[0] or m.gear_now(iid)[0] or ps[-1]    # same price the report shows (a lone silly ask is replaced by the usual price)
        data[iid] = {"t0": t[0] * 3600, "t": [hours[t[i]][-1][2] for i in keep],
                     "p": [ps[i] for i in keep[:-1]] + [now],
                     "q": [hours[t[i]][-1][1] for i in keep], "med": med, "now": now}
    rc = {}
    try:
        info = recipe_info(m)
        rc = {i: info[i] for i in iids if i in info}
    except Exception as e:   # never break the page over the recipe button
        print("recipe button data skipped:", e)
    tt = {}
    try:   # weapon / armor tooltips (stats, abilities) for the items on the page
        import tooltips
        gear_ids = [i for i in iids if (m.items.get(i) or {}).get("cls") in ("Armor", "Weapon")]
        c = tooltips.fetch(gear_ids, 600)
        tt = {i: c[str(i)] for i in gear_ids if c.get(str(i)) and c[str(i)][1]}
    except Exception as e:
        print("tooltips skipped:", e)
    lookup = ('<section><div class="sh"><h2>Likely all you&rsquo;ll use this for</h2><span class="tag">search any item on the Horde PvP ruleset AH for its full price history</span></div>'
              '<div class="lk"><input id="lkq" type="search" placeholder="Type an item name, e.g. Black Lotus" autocomplete="off">'
              '<div class="mut" style="font-size:9px;margin-top:2px">every item ever listed on the Horde PvP ruleset AH, plus every tradeable weapon, armor piece and recipe (even ones that have never been listed) &middot; <span id="lkst">loads when you tap the box</span></div>'
              '<div id="lkres"></div><div id="lkchart"></div></div></section>')
    body = body.replace("</header>", "</header>" + lookup, 1)   # search tile at the top
    write_lookup(m, os.path.join(os.path.dirname(path), "lookup.json"))
    import onepage
    page = (f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{title}</title><style>{onepage.CSS.replace("__ZOOM__", "1.55")}{CSS}</style></head><body><div class="wrap">'
            f'<p class="hint">Tap a section to open it. Hover over (or tap) any item name for its full hour-by-hour price history; click to pin it open.</p>'
            f'{body}</div><div id="pop"></div>'
            f'<script type="application/json" id="series">{json.dumps(data, separators=(",", ":"))}</script>'
            f'<script type="application/json" id="rcinfo">{json.dumps(rc, separators=(",", ":")).replace("</", "<"+chr(92)+"/")}</script>'
            f'<script type="application/json" id="ttinfo">{json.dumps(tt, separators=(",", ":")).replace("</", "<"+chr(92)+"/")}</script>'
            f'<script>{JS}</script></body></html>')
    open(path, "w", encoding="utf-8").write(page)
    return path
