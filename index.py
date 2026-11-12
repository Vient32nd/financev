"""Financev - quản lý tài chính cá nhân.
Flask + PostgreSQL, chạy trên Vercel. File này nằm ở thư mục gốc.
Chạy thử trên máy:  python index.py  ->  http://127.0.0.1:5000
"""
import os
import re
from datetime import date, timedelta
from functools import wraps

import psycopg
from flask import Flask, Response, g, jsonify, request, session
from psycopg.rows import dict_row
from werkzeug.security import check_password_hash, generate_password_hash

DB_URL = os.environ.get("POSTGRES_URL") or os.environ.get("DATABASE_URL")
SECRET = os.environ.get("SECRET_KEY") or "doi-chuoi-nay-khi-deploy"

app = Flask(__name__)
app.secret_key = SECRET
app.permanent_session_lifetime = timedelta(days=365)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("VERCEL")),
)

_ready = False


# ---------------------------------------------------------------- CSDL
def db():
    if "db" not in g:
        if not DB_URL:
            raise RuntimeError("Chưa cấu hình POSTGRES_URL hoặc DATABASE_URL")
        g.db = psycopg.connect(DB_URL, row_factory=dict_row)
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def q(sql, params=(), fetch=False):
    conn = db()
    cur = conn.execute(sql, params)
    rows = cur.fetchall() if fetch else None
    conn.commit()
    return rows


@app.before_request
def setup():
    global _ready
    if _ready:
        return
    schema = [
        """CREATE TABLE IF NOT EXISTS users (
               id SERIAL PRIMARY KEY, name TEXT UNIQUE NOT NULL, pw TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS tx (
               id SERIAL PRIMARY KEY, uid INT NOT NULL, d DATE NOT NULL,
               kind TEXT NOT NULL, amt BIGINT NOT NULL, cat TEXT, note TEXT)""",
        """CREATE TABLE IF NOT EXISTS sv (
               id SERIAL PRIMARY KEY, uid INT NOT NULL, d DATE NOT NULL,
               kind TEXT NOT NULL, amt BIGINT NOT NULL, cat TEXT, note TEXT)""",
        """CREATE TABLE IF NOT EXISTS budget (
               uid INT NOT NULL, ym TEXT NOT NULL, amt BIGINT NOT NULL,
               PRIMARY KEY (uid, ym))""",
    ]
    for stmt in schema:
        q(stmt)
    _ready = True


# ---------------------------------------------------------------- Tiện ích
def err(msg, code=400):
    return jsonify(error=msg), code


def body():
    return request.get_json(silent=True) or {}


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("uid"):
            return err("Chưa đăng nhập", 401)
        return view(*args, **kwargs)
    return wrapper


def start_session(uid, name):
    session.permanent = True
    session["uid"] = uid
    return jsonify(name=name)


def parse_entry(j):
    try:
        d = date.fromisoformat(str(j.get("d", "")))
        amt = int(j.get("amt"))
    except (TypeError, ValueError):
        raise ValueError("Ngày hoặc số tiền không hợp lệ")
    if amt <= 0:
        raise ValueError("Số tiền phải lớn hơn 0")
    if j.get("kind") not in ("in", "out"):
        raise ValueError("Loại không hợp lệ")
    cat = str(j.get("cat") or "Khác")[:40]
    note = str(j.get("note") or "").strip()[:200]
    return d, j["kind"], amt, cat, note


def serialize(rows):
    out = []
    for r in rows:
        r = dict(r)
        r["d"] = r["d"].isoformat()
        out.append(r)
    return out


TABLES = ("tx", "sv")


# ---------------------------------------------------------------- Tài khoản
@app.post("/api/register")
def register():
    j = body()
    name = str(j.get("name") or "").strip().lower()
    pw = str(j.get("pw") or "")
    if not re.fullmatch(r"[a-z0-9_.]{3,20}", name):
        return err("Tên đăng nhập 3-20 ký tự (a-z, 0-9, _ .)")
    if len(pw) < 6:
        return err("Mật khẩu tối thiểu 6 ký tự")
    try:
        row = q("INSERT INTO users (name, pw) VALUES (%s, %s) RETURNING id",
                (name, generate_password_hash(pw)), fetch=True)
    except psycopg.errors.UniqueViolation:
        db().rollback()
        return err("Tên đăng nhập đã tồn tại")
    return start_session(row[0]["id"], name)


@app.post("/api/login")
def login():
    j = body()
    name = str(j.get("name") or "").strip().lower()
    rows = q("SELECT id, name, pw FROM users WHERE name = %s", (name,), fetch=True)
    if not rows or not check_password_hash(rows[0]["pw"], str(j.get("pw") or "")):
        return err("Sai tên đăng nhập hoặc mật khẩu", 401)
    return start_session(rows[0]["id"], rows[0]["name"])


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


@app.get("/api/me")
def me():
    uid = session.get("uid")
    if not uid:
        return jsonify(name=None)
    rows = q("SELECT name FROM users WHERE id = %s", (uid,), fetch=True)
    return jsonify(name=rows[0]["name"] if rows else None)


# ---------------------------------------------------------------- Dữ liệu
@app.get("/api/data")
@login_required
def data():
    uid = session["uid"]
    tx = q("SELECT id, d, kind, amt, cat, note FROM tx WHERE uid = %s ORDER BY d DESC, id DESC",
           (uid,), fetch=True)
    sv = q("SELECT id, d, kind, amt, cat, note FROM sv WHERE uid = %s ORDER BY d DESC, id DESC",
           (uid,), fetch=True)
    bud = q("SELECT ym, amt FROM budget WHERE uid = %s", (uid,), fetch=True)
    return jsonify(tx=serialize(tx), sv=serialize(sv),
                   budget={b["ym"]: b["amt"] for b in bud})


@app.post("/api/<table>")
@login_required
def add_entry(table):
    if table not in TABLES:
        return err("Không tồn tại", 404)
    try:
        d, kind, amt, cat, note = parse_entry(body())
    except ValueError as e:
        return err(str(e))
    q(f"INSERT INTO {table} (uid, d, kind, amt, cat, note) VALUES (%s, %s, %s, %s, %s, %s)",
      (session["uid"], d, kind, amt, cat, note))
    return jsonify(ok=True)


@app.delete("/api/<table>/<int:eid>")
@login_required
def delete_entry(table, eid):
    if table not in TABLES:
        return err("Không tồn tại", 404)
    q(f"DELETE FROM {table} WHERE id = %s AND uid = %s", (eid, session["uid"]))
    return jsonify(ok=True)


@app.post("/api/budget")
@login_required
def set_budget():
    j = body()
    ym = str(j.get("ym") or "")
    if not re.fullmatch(r"\d{4}-\d{2}", ym):
        return err("Tháng không hợp lệ")
    try:
        amt = int(j.get("amt") or 0)
    except (TypeError, ValueError):
        return err("Số tiền không hợp lệ")
    if amt < 0:
        return err("Ngân sách không được âm")
    if amt == 0:
        q("DELETE FROM budget WHERE uid = %s AND ym = %s", (session["uid"], ym))
    else:
        q("""INSERT INTO budget (uid, ym, amt) VALUES (%s, %s, %s)
             ON CONFLICT (uid, ym) DO UPDATE SET amt = EXCLUDED.amt""",
          (session["uid"], ym, amt))
    return jsonify(ok=True)


# ---------------------------------------------------------------- Giao diện
HTML = """<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#f2f2f7">
<title>Financev</title>
<style>
/* ---------- Bảng màu: sáng / tối (phong cách Apple) ---------- */
:root{--bg:#f2f2f7;--card:#ffffff;--ink:#000000;--mut:#8e8e93;--line:rgba(60,60,67,.14);
 --fill:rgba(118,118,128,.12);--field:#e9e9ef;--accent:#0a84ff;--in:#248a3d;--out:#ff3b30;
 --glass:rgba(255,255,255,.55);--glass-edge:rgba(255,255,255,.85);--glass-sh:0 10px 30px rgba(0,0,0,.12);
 --pill:#ffffff;--topbg:rgba(242,242,247,.62)}
@media(prefers-color-scheme:dark){
 :root{color-scheme:dark;--bg:#000000;--card:#1c1c1e;--ink:#ffffff;--mut:#8e8e93;--line:rgba(84,84,88,.65);
 --fill:rgba(120,120,128,.28);--field:#2c2c2e;--accent:#0a84ff;--in:#30d158;--out:#ff453a;
 --glass:rgba(44,44,48,.45);--glass-edge:rgba(255,255,255,.18);--glass-sh:0 14px 40px rgba(0,0,0,.55);
 --pill:rgba(118,118,128,.5);--topbg:rgba(0,0,0,.45)}
}

/* ---------- Nền & chữ ---------- */
*{box-sizing:border-box}
body{margin:0;min-height:100vh;background:var(--bg);color:var(--ink);
 font:400 16px/1.4 -apple-system,BlinkMacSystemFont,"SF Pro Text","Helvetica Neue","Segoe UI",system-ui,sans-serif;
 letter-spacing:-.016em;font-variant-numeric:tabular-nums;-webkit-font-smoothing:antialiased;
 padding:env(safe-area-inset-top) 0 env(safe-area-inset-bottom)}
button,input,select{font:inherit;color:inherit}
button{cursor:pointer;border:0;background:none;-webkit-tap-highlight-color:transparent}
h1,h2,h3{margin:0;font-weight:600;letter-spacing:-.02em}
small{color:var(--mut)}
.in{color:var(--in)}.out{color:var(--out)}.mut{color:var(--mut)}
.wrap{max-width:720px;margin:0 auto;padding:0 16px 120px}

/* ---------- Thẻ & kính mờ ---------- */
.card{background:var(--card);border-radius:30px;padding:18px;margin-bottom:12px}
.glass{background:var(--glass);border:1px solid var(--glass-edge);box-shadow:var(--glass-sh);
 -webkit-backdrop-filter:blur(28px) saturate(180%);backdrop-filter:blur(28px) saturate(180%)}

/* ---------- Đầu trang ---------- */
header.top{position:sticky;top:0;z-index:30;display:flex;justify-content:space-between;align-items:center;gap:12px;
 margin:0 -16px;padding:12px 16px;background:var(--topbg);
 -webkit-backdrop-filter:blur(24px) saturate(180%);backdrop-filter:blur(24px) saturate(180%)}
.brand{display:flex;align-items:center;gap:10px;font-size:22px;font-weight:700;letter-spacing:-.03em}
.logo{width:40px;height:40px;border-radius:12px;display:grid;place-items:center;color:#fff;font-weight:700;font-size:20px;
 background:linear-gradient(135deg,#0a84ff,#5e5ce6);box-shadow:0 6px 16px rgba(10,132,255,.35)}
.who{display:flex;align-items:center;gap:8px;font-size:14px}

/* ---------- Thanh tab viên thuốc ---------- */
.tabs{display:flex;justify-content:space-between;padding:5px;border-radius:999px;margin:14px 0 16px;overflow-x:auto}
.tabs button{flex:1;padding:9px 10px;border-radius:999px;color:var(--mut);font-weight:600;font-size:15px;white-space:nowrap;transition:background .3s,color .3s}
.tabs button.on{background:var(--pill);color:var(--ink);box-shadow:0 2px 8px rgba(0,0,0,.14)}

/* ---------- Nút, phân đoạn, ô nhập ---------- */
.btn{padding:9px 18px;border-radius:999px;background:var(--fill);font-size:15px;font-weight:600;transition:opacity .2s,transform .15s;white-space:nowrap}
.btn:hover{opacity:.75}
.btn:active{transform:scale(.97)}
.btn.chev{width:38px;height:38px;padding:0;display:grid;place-items:center;font-size:22px;line-height:1}
.btn.pri{background:var(--accent);color:#fff}
.btn.sm{padding:7px 14px;font-size:14px}
.btn.wide{width:100%}
.link{color:var(--accent);font-size:14px;font-weight:600}
.link.danger{color:var(--out)}
input,select{width:100%;padding:13px 16px;border-radius:999px;border:0;background:var(--field);outline:none;
 transition:box-shadow .2s;-webkit-appearance:none;appearance:none;font-size:16px}
input:focus,select:focus{box-shadow:inset 0 0 0 2px var(--accent)}
input[type=month]{width:auto;padding:9px 14px;font-size:15px}
.fld{display:grid;gap:6px;margin:0;font-size:13px;color:var(--mut)}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.full{grid-column:1/-1}
.err{color:var(--out);min-height:18px;font-size:14px}
.row{display:flex;gap:8px;align-items:center;justify-content:center}
.row.between{justify-content:space-between}

/* ---------- Thống kê ---------- */
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:12px}
.stat{margin:0;padding:14px 14px 12px}
.stat small{display:block;font-size:13px}
.stat b{display:block;font-size:22px;font-weight:600;letter-spacing:-.025em;margin-top:4px;line-height:1.15}
.ch{display:flex;justify-content:space-between;align-items:baseline;gap:10px;margin-bottom:10px}
.ch h3{font-size:17px}
.bar{height:6px;border-radius:999px;background:var(--fill);overflow:hidden;margin:10px 0}
.bar span{display:block;height:100%;border-radius:999px;background:var(--accent);transition:width .6s cubic-bezier(.32,.72,0,1)}
.bar.thin{height:5px;margin:4px 0 12px}
.bar.over span{background:var(--out)}

/* ---------- Danh mục & danh sách bo tròn ---------- */
.cat{display:flex;justify-content:space-between;margin-top:10px;font-size:15px}
.list{background:var(--field);border-radius:26px;padding:0 16px;overflow:hidden}
.li{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:13px 0;border-bottom:1px solid var(--line)}
.li:last-child{border-bottom:0}
.li .nm{min-width:0}
.li .lv{display:flex;align-items:center;gap:10px;font-weight:600;white-space:nowrap}
.empty{color:var(--mut);text-align:center;padding:14px 0;font-size:15px}

/* ---------- Tháng ---------- */
.cnav{display:flex;gap:8px;align-items:center;justify-content:center;margin:6px 0 12px}
.phead{display:flex;justify-content:space-between;align-items:flex-end;margin:4px 2px 10px}
.ttl{font-size:32px;font-weight:700;letter-spacing:-.03em;line-height:1.1}
.phead small{font-size:12px;text-transform:uppercase;letter-spacing:.05em;font-weight:600}

/* ---------- Đăng nhập ---------- */
.auth{max-width:380px;margin:9vh auto 0;text-align:center}
.auth .logo{width:84px;height:84px;margin:0 auto 14px;border-radius:24px;font-size:40px;box-shadow:0 10px 26px rgba(10,132,255,.35)}
.auth .wm{font-size:34px;font-weight:700;letter-spacing:-.035em}
.auth .sub{margin:4px 0 22px;font-size:15px;color:var(--mut)}
.auth form{display:grid;gap:10px;text-align:left}
.auth .switch{margin-top:16px;font-size:14px}

/* ---------- Thông báo ---------- */
.toast{position:fixed;left:50%;bottom:28px;transform:translate(-50%,16px);opacity:0;transition:opacity .3s,transform .4s cubic-bezier(.32,.72,0,1);
 background:var(--ink);color:var(--bg);padding:11px 20px;border-radius:999px;font-size:14px;font-weight:600;z-index:99;
 box-shadow:0 8px 24px rgba(0,0,0,.25);max-width:92vw;text-align:center}
.toast.show{opacity:1;transform:translate(-50%,0)}
.toast.bad{background:var(--out);color:#fff}

/* ---------- Điện thoại ---------- */
@media(max-width:480px){
 .stats{grid-template-columns:1fr 1fr 1fr;gap:8px}
 .stat{padding:12px 10px}
 .stat b{font-size:17px}
 .grid{grid-template-columns:1fr}
 .ttl{font-size:28px}
}
@media(prefers-reduced-motion:reduce){*{transition:none!important}}
</style>
</head>
<body>
<div id="app"></div>
<script>
const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt=n=>Number(n||0).toLocaleString('vi-VN')+' đ';
const ymOf=d=>d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0');
const today=()=>{const d=new Date();return ymOf(d)+'-'+String(d.getDate()).padStart(2,'0')};
const dmy=d=>d.split('-').reverse().join('/');
const CATS={out:['Ăn uống','Mua sắm','Giải trí','Di chuyển','Hoá đơn','Khác'],in:['Lương','Thưởng','Khác']};
const S={user:null,data:null,tab:'home',ym:ymOf(new Date())};
const inMonth=t=>t.d.startsWith(S.ym);
const sumOf=(list,kind)=>list.filter(t=>t.kind===kind).reduce((a,t)=>a+t.amt,0);

async function api(path,method='GET',payload){
 const r=await fetch(path,{method,headers:{'Content-Type':'application/json'},body:payload?JSON.stringify(payload):undefined});
 const j=await r.json().catch(()=>({}));
 if(!r.ok)throw new Error(j.error||'Có lỗi xảy ra');
 return j;
}
function toast(msg,bad){
 const t=document.createElement('div');t.className='toast'+(bad?' bad':'');t.textContent=msg;
 document.body.appendChild(t);requestAnimationFrame(()=>t.classList.add('show'));
 setTimeout(()=>{t.classList.remove('show');setTimeout(()=>t.remove(),400)},2400);
}

async function boot(){
 try{
  const m=await api('/api/me');
  if(m.name){S.user=m.name;await load();return}
 }catch(e){}
 authView('login');
}
async function load(){S.data=await api('/api/data');paint()}

function authView(mode){
 const reg=mode==='reg';
 $('#app').innerHTML=`<div class="auth">
  <div class="logo">F</div>
  <div class="wm">Financev</div>
  <p class="sub">Quản lý tài chính cá nhân</p>
  <div class="card">
   <h2 style="margin-bottom:14px">${reg?'Tạo tài khoản':'Đăng nhập'}</h2>
   <form id="f">
    <input name="name" placeholder="Tên đăng nhập (a-z, 0-9)" required autocomplete="username">
    <input name="pw" type="password" placeholder="Mật khẩu" required autocomplete="${reg?'new-password':'current-password'}">
    <button class="btn pri wide">${reg?'Đăng ký':'Đăng nhập'}</button>
   </form>
   <p class="err" id="em"></p>
  </div>
  <p class="switch"><a class="link" href="#" id="sw">${reg?'Đã có tài khoản? Đăng nhập':'Chưa có tài khoản? Đăng ký'}</a></p>
 </div>`;
 $('#sw').onclick=e=>{e.preventDefault();authView(reg?'login':'reg')};
 $('#f').onsubmit=async e=>{
  e.preventDefault();
  const fd=new FormData(e.target);
  try{
   const r=await api(reg?'/api/register':'/api/login','POST',{name:fd.get('name'),pw:fd.get('pw')});
   S.user=r.name;await load();
  }catch(x){$('#em').textContent=x.message}
 };
}

function paint(){
 const tabs=[['home','Tổng quan'],['tx','Thu chi'],['sv','Tiết kiệm'],['set','Ngân sách']];
 $('#app').innerHTML=`<div class="wrap">
 <header class="top">
  <div class="brand"><div class="logo" style="width:34px;height:34px;font-size:17px;border-radius:10px">F</div>Financev</div>
  <div class="who"><span class="mut">${esc(S.user)}</span><button class="btn sm" id="lo">Đăng xuất</button></div>
 </header>
 <nav class="tabs glass">${tabs.map(([k,t])=>`<button data-t="${k}" class="${S.tab===k?'on':''}">${t}</button>`).join('')}</nav>
 <main id="m"></main></div>`;
 document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{S.tab=b.dataset.t;paint()});
 $('#lo').onclick=async()=>{await api('/api/logout','POST');S.data=null;S.user=null;authView('login')};
 ({home:vHome,tx:vTx,sv:vSv,set:vSet})[S.tab]();
 bindDel();
}

function monthBar(){
 return `<div class="phead"><div><small>Tháng</small><h2 class="ttl">${S.ym.split('-').reverse().join('/')}</h2></div>
 <div class="cnav"><button class="btn chev" id="pv">‹</button><input type="month" id="ym" value="${S.ym}"><button class="btn chev" id="nx">›</button></div></div>`;
}
function bindMonth(){
 const go=n=>{const [y,m]=S.ym.split('-').map(Number);S.ym=ymOf(new Date(y,m-1+n,1));paint()};
 $('#pv').onclick=()=>go(-1);
 $('#nx').onclick=()=>go(1);
 $('#ym').onchange=e=>{if(e.target.value){S.ym=e.target.value;paint()}};
}

function txRow(t,tb){
 const sign=t.kind==='in'?'+':'-';
 return `<div class="li">
  <div class="nm"><b>${esc(t.cat||'Khác')}</b>${t.note?` <small>· ${esc(t.note)}</small>`:''}<br><small>${dmy(t.d)}</small></div>
  <div class="lv"><span class="${t.kind==='in'?'in':'out'}">${sign}${fmt(t.amt)}</span>${tb?`<button class="link danger" data-tb="${tb}" data-id="${t.id}">Xoá</button>`:''}</div></div>`;
}
function bindDel(){
 document.querySelectorAll('button[data-tb]').forEach(b=>b.onclick=async()=>{
  if(!confirm('Xoá mục này?'))return;
  try{await api(`/api/${b.dataset.tb}/${b.dataset.id}`,'DELETE');toast('Đã xoá');await load()}
  catch(e){toast(e.message,true)}
 });
}

function vHome(){
 const tx=S.data.tx.filter(inMonth);
 const inc=sumOf(tx,'in'),out=sumOf(tx,'out'),left=inc-out;
 const bud=S.data.budget[S.ym];
 const byCat={};
 tx.filter(t=>t.kind==='out').forEach(t=>{const c=t.cat||'Khác';byCat[c]=(byCat[c]||0)+t.amt});
 const cats=Object.entries(byCat).sort((a,b)=>b[1]-a[1]);
 const max=cats.length?cats[0][1]:1;
 let budHtml='<p class="mut" style="margin:0">Chưa đặt ngân sách tháng này. Vào tab "Ngân sách" để đặt.</p>';
 if(bud){
  const p=Math.min(100,Math.round(out/bud*100));
  budHtml=`<div class="bar ${out>bud?'over':''}"><span style="width:${p}%"></span></div>
  <p class="mut" style="margin:0">Đã chi ${p}% ngân sách ${fmt(bud)}. ${bud-out>=0?'Còn lại '+fmt(bud-out):'Vượt '+fmt(out-bud)}</p>`;
 }
 $('#m').innerHTML=`${monthBar()}
 <section class="stats">
  <div class="card stat"><small>Thu</small><b class="in">${fmt(inc)}</b></div>
  <div class="card stat"><small>Chi</small><b class="out">${fmt(out)}</b></div>
  <div class="card stat"><small>Còn lại</small><b class="${left>=0?'in':'out'}">${fmt(left)}</b></div>
 </section>
 <section class="card"><div class="ch"><h3>Ngân sách tháng</h3></div>${budHtml}</section>
 <section class="card"><div class="ch"><h3>Chi theo danh mục</h3></div>
  ${cats.length?cats.map(([c,v])=>`<div class="cat"><span>${esc(c)}</span><b>${fmt(v)}</b></div><div class="bar thin"><span style="width:${v/max*100}%"></span></div>`).join(''):'<div class="empty">Chưa có khoản chi nào.</div>'}
 </section>
 <section class="card"><div class="ch"><h3>Giao dịch gần đây</h3></div>
  <div class="list">${tx.slice(0,5).map(t=>txRow(t,null)).join('')||'<div class="empty">Chưa có giao dịch.</div>'}</div>
 </section>`;
 bindMonth();
}

function vTx(){
 const tx=S.data.tx.filter(inMonth);
 $('#m').innerHTML=`${monthBar()}
 <section class="card"><div class="ch"><h3>Thêm giao dịch</h3></div>
  <form id="fx" class="grid">
   <label class="fld">Loại<select name="kind" id="kind"><option value="out">Chi tiêu</option><option value="in">Thu nhập</option></select></label>
   <label class="fld">Danh mục<select name="cat" id="cat"></select></label>
   <label class="fld">Số tiền (đ)<input name="amt" type="number" min="1" required inputmode="numeric"></label>
   <label class="fld">Ngày<input name="d" type="date" value="${today()}" required></label>
   <label class="fld full">Ghi chú<input name="note" maxlength="200" placeholder="Tuỳ chọn"></label>
   <button class="btn pri wide full">Thêm</button>
  </form></section>
 <section class="card"><div class="ch"><h3>Giao dịch tháng ${S.ym.split('-').reverse().join('/')}</h3></div>
  <div class="list">${tx.map(t=>txRow(t,'tx')).join('')||'<div class="empty">Chưa có giao dịch.</div>'}</div>
 </section>`;
 const fillCats=()=>{$('#cat').innerHTML=CATS[$('#kind').value].map(c=>`<option>${esc(c)}</option>`).join('')};
 fillCats();
 $('#kind').onchange=fillCats;
 bindMonth();
 $('#fx').onsubmit=async e=>{
  e.preventDefault();
  const f=Object.fromEntries(new FormData(e.target));
  try{
   await api('/api/tx','POST',f);
   S.ym=f.d.slice(0,7);
   toast('Đã thêm');await load();
  }catch(x){toast(x.message,true)}
 };
}

function vSv(){
 const bal=sumOf(S.data.sv,'in')-sumOf(S.data.sv,'out');
 $('#m').innerHTML=`<section class="card" style="text-align:center"><small>Số dư tiết kiệm</small>
  <h2 style="font-size:34px;margin-top:4px">${fmt(bal)}</h2></section>
 <section class="card"><div class="ch"><h3>Ghi nhận</h3></div>
  <form id="fs" class="grid">
   <label class="fld">Loại<select name="kind"><option value="in">Gửi thêm</option><option value="out">Rút ra</option></select></label>
   <label class="fld">Số tiền (đ)<input name="amt" type="number" min="1" required inputmode="numeric"></label>
   <label class="fld">Ngày<input name="d" type="date" value="${today()}" required></label>
   <label class="fld">Ghi chú<input name="note" maxlength="200"></label>
   <input type="hidden" name="cat" value="Tiết kiệm">
   <button class="btn pri wide full">Lưu</button>
  </form></section>
 <section class="card"><div class="ch"><h3>Lịch sử</h3></div>
  <div class="list">${S.data.sv.map(t=>txRow(t,'sv')).join('')||'<div class="empty">Chưa có ghi nhận.</div>'}</div>
 </section>`;
 $('#fs').onsubmit=async e=>{
  e.preventDefault();
  try{await api('/api/sv','POST',Object.fromEntries(new FormData(e.target)));toast('Đã lưu');await load()}
  catch(x){toast(x.message,true)}
 };
}

function vSet(){
 const b=S.data.budget[S.ym]||'';
 $('#m').innerHTML=`${monthBar()}
 <section class="card"><div class="ch"><h3>Ngân sách chi tháng</h3></div>
  <p class="mut" style="margin:0 0 12px">Tổng số tiền được phép chi trong tháng. Để trống hoặc 0 để xoá.</p>
  <form id="fb" class="grid">
   <label class="fld full">Số tiền (đ)<input name="amt" type="number" min="0" value="${b}" inputmode="numeric"></label>
   <button class="btn pri wide full">Lưu ngân sách</button>
  </form></section>`;
 bindMonth();
 $('#fb').onsubmit=async e=>{
  e.preventDefault();
  try{
   await api('/api/budget','POST',{ym:S.ym,amt:e.target.elements.amt.value||0});
   toast('Đã lưu');await load();
  }catch(x){toast(x.message,true)}
 };
}

boot();
</script>
</body>
</html>
"""


@app.get("/")
def index():
    return Response(HTML, mimetype="text/html")


if __name__ == "__main__":
    app.run(debug=True)
