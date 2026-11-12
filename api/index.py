"""Financev - ứng dụng quản lý tài chính cá nhân.
Flask + PostgreSQL, chạy trên Vercel. Backend và giao diện nằm trong file này.
Chạy thử trên máy:  python api/index.py  ->  http://127.0.0.1:5000
"""
import os
import re
from datetime import date, timedelta
from functools import wraps

import psycopg
from flask import Flask, Response, g, jsonify, request, session
from psycopg.rows import dict_row
from werkzeug.security import check_password_hash, generate_password_hash

# Vercel/Neon tự tạo POSTGRES_URL hoặc DATABASE_URL. Ta dùng cái nào có.
DB_URL = os.environ.get("POSTGRES_URL") or os.environ.get("DATABASE_URL")
# BẮT BUỘC đặt SECRET_KEY trên Vercel (chuỗi ngẫu nhiên dài)
SECRET = os.environ.get("SECRET_KEY") or "doi-chuoi-nay-khi-deploy"

app = Flask(__name__)
app.secret_key = SECRET
app.permanent_session_lifetime = timedelta(days=365)  # đăng nhập 1 lần, nhớ 1 năm
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("VERCEL")),  # Vercel dùng https
)

_ready = False


# ---------------------------------------------------------------- CSDL
def db():
    """Mỗi request mở một kết nối, cuối request tự đóng."""
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
    """Chạy một câu SQL và commit. Nếu fetch=True thì trả về các dòng."""
    conn = db()
    cur = conn.execute(sql, params)
    rows = cur.fetchall() if fetch else None
    conn.commit()
    return rows


@app.before_request
def setup():
    """Tạo bảng lần đầu tiên (chỉ chạy một lần mỗi instance)."""
    global _ready
    if _ready:
        return
    schema = [
        """CREATE TABLE IF NOT EXISTS users (
               id SERIAL PRIMARY KEY,
               name TEXT UNIQUE NOT NULL,
               pw TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS tx (
               id SERIAL PRIMARY KEY,
               uid INT NOT NULL,
               d DATE NOT NULL,
               kind TEXT NOT NULL,
               amt BIGINT NOT NULL,
               cat TEXT,
               note TEXT)""",
        """CREATE TABLE IF NOT EXISTS sv (
               id SERIAL PRIMARY KEY,
               uid INT NOT NULL,
               d DATE NOT NULL,
               kind TEXT NOT NULL,
               amt BIGINT NOT NULL,
               cat TEXT,
               note TEXT)""",
        """CREATE TABLE IF NOT EXISTS budget (
               uid INT NOT NULL,
               ym TEXT NOT NULL,
               amt BIGINT NOT NULL,
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
    """Kiểm tra dữ liệu một giao dịch / ghi nhận tiết kiệm."""
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
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Financev</title>
<style>
body{margin:0;background:#f4f6f8;color:#1d2433;font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header{display:flex;justify-content:space-between;align-items:center;padding:14px 18px;background:#fff;border-bottom:1px solid #e6e9ef}
nav{display:flex;gap:6px;justify-content:center;background:#fff;padding:8px;border-bottom:1px solid #e6e9ef;overflow-x:auto}
nav button{border:0;background:none;padding:8px 12px;border-radius:8px;color:#7a8496;cursor:pointer;font:inherit;white-space:nowrap}
nav button.on{background:#e8efff;color:#2563eb;font-weight:600}
main{max-width:720px;margin:0 auto;padding:16px;display:grid;gap:14px}
.card{background:#fff;border:1px solid #e6e9ef;border-radius:14px;padding:16px}
.card h3{margin:0 0 10px;font-size:16px}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.stats .card{padding:12px}
.stats b{display:block;font-size:17px}
small{color:#7a8496}
.in{color:#16a34a}.out{color:#dc2626}.mut{color:#7a8496}
.center{text-align:center}
.big{margin:4px 0 0;font-size:30px}
input,select{width:100%;padding:10px;border:1px solid #d5dae3;border-radius:10px;font:inherit;background:#fff;box-sizing:border-box}
label{display:grid;gap:4px;font-size:13px;color:#4a5365}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.full{grid-column:1/-1}
.btn{border:1px solid #d5dae3;background:#fff;border-radius:10px;padding:10px 14px;cursor:pointer;font:inherit}
.btn.pri{background:#2563eb;border-color:#2563eb;color:#fff;font-weight:600}
.btn.sm{padding:6px 12px}
.wide{width:100%}
.link{background:none;border:0;color:#2563eb;cursor:pointer;font:inherit;padding:0}
.danger{color:#dc2626;margin-left:10px}
.row-i{display:flex;justify-content:space-between;gap:10px;padding:10px 0;border-top:1px solid #eef0f4}
.row-i:first-child{border-top:0}
.row-i .right{display:flex;align-items:center;white-space:nowrap}
.cat{display:flex;justify-content:space-between;margin-top:8px}
.bar{height:8px;background:#eef0f4;border-radius:99px;overflow:hidden;margin:8px 0}
.bar span{display:block;height:100%;background:#2563eb;border-radius:99px}
.bar.thin{height:6px;margin:4px 0 10px}
.monthbar{display:flex;gap:8px;justify-content:center;align-items:center}
.monthbar input{width:auto}
.auth{max-width:380px;margin:60px auto}
.err{color:#dc2626;min-height:1.5em}
.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:#1d2433;color:#fff;padding:10px 16px;border-radius:10px;z-index:9}
.toast.bad{background:#dc2626}
@media(max-width:480px){.stats b{font-size:14px}.grid{grid-template-columns:1fr}}
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
 document.body.appendChild(t);setTimeout(()=>t.remove(),2600);
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
 $('#app').innerHTML=`<div class="card auth">
  <h2 style="margin-top:0">${reg?'Tạo tài khoản':'Đăng nhập'}</h2>
  <form id="f" style="display:grid;gap:10px">
   <input name="name" placeholder="Tên đăng nhập (a-z, 0-9)" required autocomplete="username">
   <input name="pw" type="password" placeholder="Mật khẩu" required autocomplete="${reg?'new-password':'current-password'}">
   <button class="btn pri wide">${reg?'Đăng ký':'Đăng nhập'}</button>
  </form>
  <p class="err" id="em"></p>
  <p class="center"><a href="#" id="sw">${reg?'Đã có tài khoản? Đăng nhập':'Chưa có tài khoản? Đăng ký'}</a></p>
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
 $('#app').innerHTML=`<header><b>Financev</b><span><span class="mut">${esc(S.user)}</span> <button class="link" id="lo">Đăng xuất</button></span></header>
 <nav>${tabs.map(([k,t])=>`<button data-t="${k}" class="${S.tab===k?'on':''}">${t}</button>`).join('')}</nav>
 <main id="m"></main>`;
 document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{S.tab=b.dataset.t;paint()});
 $('#lo').onclick=async()=>{await api('/api/logout','POST');S.data=null;S.user=null;authView('login')};
 ({home:vHome,tx:vTx,sv:vSv,set:vSet})[S.tab]();
 bindDel();
}

function monthBar(){
 return `<div class="monthbar card">
  <button class="btn sm" id="pv">‹</button>
  <input type="month" id="ym" value="${S.ym}">
  <button class="btn sm" id="nx">›</button></div>`;
}
function bindMonth(){
 const go=n=>{const [y,m]=S.ym.split('-').map(Number);S.ym=ymOf(new Date(y,m-1+n,1));paint()};
 $('#pv').onclick=()=>go(-1);
 $('#nx').onclick=()=>go(1);
 $('#ym').onchange=e=>{if(e.target.value){S.ym=e.target.value;paint()}};
}

function txRow(t,tb){
 const sign=t.kind==='in'?'+':'-';
 return `<div class="row-i">
  <div><b>${esc(t.cat||'Khác')}</b>${t.note?` <span class="mut">· ${esc(t.note)}</span>`:''}<br><small>${dmy(t.d)}</small></div>
  <div class="right"><b class="${t.kind==='in'?'in':'out'}">${sign}${fmt(t.amt)}</b>
  ${tb?`<button class="link danger" data-tb="${tb}" data-id="${t.id}">Xoá</button>`:''}</div></div>`;
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
 let budHtml='<p class="mut">Chưa đặt ngân sách tháng này. Vào tab "Ngân sách" để đặt.</p>';
 if(bud){
  const p=Math.min(100,Math.round(out/bud*100));
  budHtml=`<div class="bar"><span style="width:${p}%;background:${p>=100?'#dc2626':'#2563eb'}"></span></div>
  <p class="mut">Đã chi ${p}% ngân sách ${fmt(bud)}. ${bud-out>=0?'Còn lại '+fmt(bud-out):'Vượt '+fmt(out-bud)}</p>`;
 }
 $('#m').innerHTML=`${monthBar()}
 <section class="stats">
  <div class="card"><small>Thu</small><b class="in">${fmt(inc)}</b></div>
  <div class="card"><small>Chi</small><b class="out">${fmt(out)}</b></div>
  <div class="card"><small>Còn lại</small><b class="${left>=0?'in':'out'}">${fmt(left)}</b></div>
 </section>
 <section class="card"><h3>Ngân sách tháng</h3>${budHtml}</section>
 <section class="card"><h3>Chi theo danh mục</h3>
  ${cats.length?cats.map(([c,v])=>`<div class="cat"><span>${esc(c)}</span><b>${fmt(v)}</b></div><div class="bar thin"><span style="width:${v/max*100}%"></span></div>`).join(''):'<p class="mut">Chưa có khoản chi nào.</p>'}
 </section>
 <section class="card"><h3>Giao dịch gần đây</h3>
  ${tx.slice(0,5).map(t=>txRow(t,null)).join('')||'<p class="mut">Chưa có giao dịch.</p>'}
 </section>`;
 bindMonth();
}

function vTx(){
 const tx=S.data.tx.filter(inMonth);
 $('#m').innerHTML=`${monthBar()}
 <section class="card"><h3>Thêm giao dịch</h3>
  <form id="fx" class="grid">
   <label>Loại<select name="kind" id="kind"><option value="out">Chi tiêu</option><option value="in">Thu nhập</option></select></label>
   <label>Danh mục<select name="cat" id="cat"></select></label>
   <label>Số tiền (đ)<input name="amt" type="number" min="1" required inputmode="numeric"></label>
   <label>Ngày<input name="d" type="date" value="${today()}" required></label>
   <label class="full">Ghi chú<input name="note" maxlength="200" placeholder="Tuỳ chọn"></label>
   <button class="btn pri full">Thêm</button>
  </form></section>
 <section class="card"><h3>Giao dịch tháng ${S.ym}</h3>
  ${tx.map(t=>txRow(t,'tx')).join('')||'<p class="mut">Chưa có giao dịch.</p>'}
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
 $('#m').innerHTML=`<section class="card center"><small>Số dư tiết kiệm</small><h2 class="big">${fmt(bal)}</h2></section>
 <section class="card"><h3>Ghi nhận</h3>
  <form id="fs" class="grid">
   <label>Loại<select name="kind"><option value="in">Gửi thêm</option><option value="out">Rút ra</option></select></label>
   <label>Số tiền (đ)<input name="amt" type="number" min="1" required inputmode="numeric"></label>
   <label>Ngày<input name="d" type="date" value="${today()}" required></label>
   <label>Ghi chú<input name="note" maxlength="200"></label>
   <input type="hidden" name="cat" value="Tiết kiệm">
   <button class="btn pri full">Lưu</button>
  </form></section>
 <section class="card"><h3>Lịch sử</h3>
  ${S.data.sv.map(t=>txRow(t,'sv')).join('')||'<p class="mut">Chưa có ghi nhận.</p>'}
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
 <section class="card"><h3>Ngân sách chi tháng ${S.ym}</h3>
  <p class="mut">Tổng số tiền được phép chi trong tháng. Để trống hoặc 0 để xoá.</p>
  <form id="fb" class="grid">
   <label class="full">Số tiền (đ)<input name="amt" type="number" min="0" value="${b}" inputmode="numeric"></label>
   <button class="btn pri full">Lưu ngân sách</button>
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
