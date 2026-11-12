"""Financev - App quản lý tài chính (Flask + PostgreSQL, chạy trên Vercel).

Biến môi trường cần đặt trên Vercel (Settings -> Environment Variables):
  POSTGRES_URL  (hoặc DATABASE_URL) : do Vercel Storage (Neon) tự tạo
  SECRET_KEY    : chuỗi bí mật dài, bắt buộc
  ADMIN_PW      : mật khẩu tài khoản quản trị 'admin' (tuỳ chọn, không đặt thì không có admin)
"""
import os, re
from datetime import timedelta, datetime
from flask import Flask, request, session, jsonify, g, Response
from werkzeug.security import generate_password_hash, check_password_hash
import psycopg
from psycopg.rows import dict_row

DB_URL = os.environ.get("POSTGRES_URL") or os.environ.get("DATABASE_URL")
SECRET = os.environ.get("SECRET_KEY") or "doi-chuoi-nay-khi-deploy"

ADMIN_NAME = "admin"
ADMIN_PW = os.environ.get("ADMIN_PW", "")
ACTIVE_DAYS = 30          # đăng nhập trong 30 ngày gần nhất thì coi là đang hoạt động

app = Flask(__name__)
app.secret_key = SECRET
app.permanent_session_lifetime = timedelta(days=3650)
app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True)


def db():
    if "db" not in g:
        if not DB_URL:
            raise RuntimeError("Chưa cấu hình POSTGRES_URL trên Vercel (Storage -> Neon)")
        g.db = psycopg.connect(DB_URL, row_factory=dict_row)
    return g.db


@app.teardown_appcontext
def close(_):
    d = g.pop("db", None)
    if d:
        d.close()


def now():
    return datetime.now().isoformat(timespec="seconds")


def is_active(ls):
    if not ls:
        return False
    try:
        return datetime.now() - datetime.fromisoformat(ls) <= timedelta(days=ACTIVE_DAYS)
    except ValueError:
        return False


DEFAULT_CATS = {"out": ["Ăn uống", "Mua sắm", "Giải trí", "Di chuyển", "Hoá đơn", "Bất ngờ"],
                "in": ["Lương", "Thưởng", "Khác"]}


def seed_cats(c, owner):
    """Tạo danh mục mặc định cho một người dùng (chỉ làm một lần)."""
    for kind, names in DEFAULT_CATS.items():
        for nm in names:
            c.execute("INSERT INTO cats(uid,kind,name) VALUES(%s,%s,%s)", (owner, kind, nm))
    c.execute("UPDATE users SET cseed=1 WHERE id=%s", (owner,))


SCHEMA = [
    """CREATE TABLE IF NOT EXISTS users(
           id SERIAL PRIMARY KEY, name TEXT UNIQUE, pw TEXT,
           role TEXT NOT NULL DEFAULT 'user', last_seen TEXT, created TEXT,
           cseed INTEGER NOT NULL DEFAULT 0,
           pro_claimed INTEGER NOT NULL DEFAULT 0, pro_until TEXT)""",
    "CREATE TABLE IF NOT EXISTS tx(id SERIAL PRIMARY KEY, uid INT, d TEXT, kind TEXT, amt BIGINT, note TEXT, cat INT)",
    "CREATE TABLE IF NOT EXISTS sv(id SERIAL PRIMARY KEY, uid INT, d TEXT, kind TEXT, amt BIGINT, note TEXT)",
    "CREATE TABLE IF NOT EXISTS lim(uid INT, scope TEXT, period TEXT, amt BIGINT, PRIMARY KEY(uid, scope, period))",
    "CREATE TABLE IF NOT EXISTS cats(id SERIAL PRIMARY KEY, uid INT, kind TEXT, name TEXT, color TEXT)",
]


def migrate():
    """Tạo bảng (nếu chưa có) và tạo tài khoản admin nếu đã đặt ADMIN_PW."""
    with psycopg.connect(DB_URL, row_factory=dict_row) as c:
        for stmt in SCHEMA:
            c.execute(stmt)
        if ADMIN_PW and not c.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
            # Nếu từng có người dùng tên 'admin' thường, đổi tên người đó để nhường chỗ cho admin.
            c.execute("UPDATE users SET name='admin_old_'||id WHERE name=%s", (ADMIN_NAME,))
            c.execute("INSERT INTO users(name,pw,role,created,last_seen) VALUES(%s,%s,%s,%s,%s) RETURNING id",
                      (ADMIN_NAME, generate_password_hash(ADMIN_PW), "admin", now(), now()))
        for r in c.execute("SELECT id FROM users WHERE cseed=0 AND role<>'admin'").fetchall():
            seed_cats(c, r["id"])


if DB_URL:
    migrate()


def err(m, code=400):
    return jsonify(error=m), code


def uid():
    return session.get("uid")


def is_admin():
    if not uid():
        return False
    r = db().execute("SELECT role FROM users WHERE id=%s", (uid(),)).fetchone()
    return bool(r and r["role"] == "admin")


def touch():
    db().execute("UPDATE users SET last_seen=%s WHERE id=%s", (now(), uid()))
    db().commit()


def money(n):
    return f"{n:,}".replace(",", ".")


DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH = re.compile(r"^\d{4}-\d{2}$")
USERNAME = re.compile(r"^[a-z0-9_.]{3,20}$")


def limit_of(scope, period):
    """Hạn mức hiệu lực: ưu tiên mốc riêng (ngày / tháng), không có thì dùng mặc định."""
    rows = {r["period"]: r["amt"] for r in
            db().execute("SELECT period, amt FROM lim WHERE uid=%s AND scope=%s", (uid(), scope))}
    return rows.get(period, rows.get("default"))


def spent(prefix):
    """Tổng chi theo ngày 'YYYY-MM-DD' hoặc tháng 'YYYY-MM'."""
    return db().execute(
        "SELECT COALESCE(SUM(amt),0)::bigint AS s FROM tx WHERE uid=%s AND kind='out' AND substr(d,1,%s)=%s",
        (uid(), len(prefix), prefix)).fetchone()["s"]


def rows_of(owner):
    """Toàn bộ dữ liệu tài chính của một người dùng (dùng chung cho chính họ và admin xem)."""
    tx = [dict(r) for r in db().execute(
        "SELECT id,d,kind,amt,note,cat FROM tx WHERE uid=%s ORDER BY d,id", (owner,))]
    sv = [dict(r) for r in db().execute(
        "SELECT id,d,kind,amt,note FROM sv WHERE uid=%s ORDER BY d,id", (owner,))]
    lim = [dict(r) for r in db().execute(
        "SELECT scope, period, amt FROM lim WHERE uid=%s", (owner,))]
    return tx, sv, lim


def cats_of(owner):
    return [dict(r) for r in db().execute(
        "SELECT id,kind,name,color FROM cats WHERE uid=%s ORDER BY id", (owner,))]


@app.post("/api/register")
def register():
    j = request.get_json(force=True)
    n, p = (j.get("name") or "").strip().lower(), j.get("pw") or ""
    if not USERNAME.match(n):
        return err("Tên đăng nhập 3-20 ký tự (a-z, 0-9, _ .)")
    if len(p) < 4:
        return err("Mật khẩu tối thiểu 4 ký tự")
    try:
        cur = db().execute("INSERT INTO users(name,pw,role,created,last_seen) VALUES(%s,%s,%s,%s,%s) RETURNING id",
                           (n, generate_password_hash(p, method="pbkdf2:sha256"), "user", now(), now()))
        new_uid = cur.fetchone()["id"]
        seed_cats(db(), new_uid)
        db().commit()
    except psycopg.errors.UniqueViolation:
        db().rollback()
        return err("Tên đăng nhập đã tồn tại")
    session.permanent = True
    session["uid"] = new_uid
    return jsonify(name=n)


@app.post("/api/login")
def login():
    j = request.get_json(force=True)
    n = (j.get("name") or "").strip().lower()
    u = db().execute("SELECT * FROM users WHERE name=%s", (n,)).fetchone()
    if not u or not check_password_hash(u["pw"], j.get("pw") or ""):
        return err("Sai tên hoặc mật khẩu", 401)
    session.permanent = True
    session["uid"] = u["id"]
    db().execute("UPDATE users SET last_seen=%s WHERE id=%s", (now(), u["id"]))
    db().commit()
    return jsonify(name=u["name"])


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=1)


@app.get("/api/me")
def me():
    if not uid():
        return jsonify(name=None, role=None)
    u = db().execute("SELECT name, role FROM users WHERE id=%s", (uid(),)).fetchone()
    return jsonify(name=u["name"] if u else None, role=u["role"] if u else None)


@app.post("/api/account")
def account():
    """Đổi tên đăng nhập và/hoặc mật khẩu. Bắt buộc nhập mật khẩu hiện tại."""
    if not uid():
        return err("Chưa đăng nhập", 401)
    j = request.get_json(force=True)
    u = db().execute("SELECT * FROM users WHERE id=%s", (uid(),)).fetchone()
    if not u or not check_password_hash(u["pw"], j.get("pw") or ""):
        return err("Mật khẩu hiện tại không đúng", 401)
    name = (j.get("name") or "").strip().lower() or u["name"]
    if not USERNAME.match(name):
        return err("Tên đăng nhập 3-20 ký tự (a-z, 0-9, _ .)")
    if name != u["name"] and db().execute(
            "SELECT 1 FROM users WHERE name=%s AND id<>%s", (name, uid())).fetchone():
        return err("Tên đăng nhập đã tồn tại")
    new_pw = j.get("new_pw") or ""
    pw_hash = u["pw"]
    if new_pw:
        if len(new_pw) < 4:
            return err("Mật khẩu mới tối thiểu 4 ký tự")
        pw_hash = generate_password_hash(new_pw, method="pbkdf2:sha256")
    db().execute("UPDATE users SET name=%s, pw=%s WHERE id=%s", (name, pw_hash, uid()))
    db().commit()
    return jsonify(name=name)


@app.get("/api/data")
def data():
    if not uid():
        return err("Chưa đăng nhập", 401)
    if is_admin():
        return err("Tài khoản quản trị không có dữ liệu tài chính", 403)
    touch()
    tx, sv, lim = rows_of(uid())
    return jsonify(tx=tx, sv=sv, lim=lim, cats=cats_of(uid()), pro=pro_of(uid()))


@app.get("/api/admin/users")
def admin_users():
    if not is_admin():
        return err("Không có quyền truy cập", 403)
    rows = db().execute(
        "SELECT id,name,created,last_seen FROM users WHERE role<>'admin' ORDER BY name").fetchall()
    return jsonify(users=[{"id": r["id"], "name": r["name"], "created": r["created"],
                           "last_seen": r["last_seen"], "active": is_active(r["last_seen"])}
                          for r in rows], active_days=ACTIVE_DAYS)


@app.get("/api/admin/user/<int:i>")
def admin_user(i):
    """Xem lịch của một người dùng ở chế độ chỉ đọc."""
    if not is_admin():
        return err("Không có quyền truy cập", 403)
    u = db().execute("SELECT id,name,role FROM users WHERE id=%s", (i,)).fetchone()
    if not u or u["role"] == "admin":
        return err("Không tìm thấy tài khoản", 404)
    tx, sv, lim = rows_of(i)
    return jsonify(name=u["name"], tx=tx, sv=sv, lim=lim, cats=cats_of(i))


def add(table, kinds):
    if not uid():
        return err("Chưa đăng nhập", 401)
    if is_admin():
        return err("Tài khoản quản trị không quản lý tài chính", 403)
    j = request.get_json(force=True)
    try:
        amt = int(j.get("amt"))
    except (TypeError, ValueError):
        return err("Số tiền không hợp lệ")
    if amt <= 0 or amt > 10**13:
        return err("Số tiền phải lớn hơn 0")
    d = str(j.get("d") or "")
    if j.get("kind") not in kinds or not DATE.match(d):
        return err("Dữ liệu không hợp lệ")
    if table == "sv" and j["kind"] == "wd":
        bal = db().execute(
            "SELECT COALESCE(SUM(CASE kind WHEN 'dep' THEN amt ELSE -amt END),0)::bigint AS s FROM sv WHERE uid=%s",
            (uid(),)).fetchone()["s"]
        if amt > bal:
            return err("Số tiền rút vượt quá số dư heo đất")
    cat = j.get("cat")
    if table == "tx" and cat not in (None, ""):
        try:
            cat = int(cat)
        except (TypeError, ValueError):
            return err("Danh mục không hợp lệ")
        if not db().execute("SELECT 1 FROM cats WHERE id=%s AND uid=%s AND kind=%s",
                            (cat, uid(), j["kind"])).fetchone():
            return err("Danh mục không hợp lệ")
    else:
        cat = None
    if table == "tx":
        cur = db().execute("INSERT INTO tx(uid,d,kind,amt,note,cat) VALUES(%s,%s,%s,%s,%s,%s) RETURNING id",
                     (uid(), d, j["kind"], amt, str(j.get("note") or "")[:80], cat))
    else:
        cur = db().execute(f"INSERT INTO {table}(uid,d,kind,amt,note) VALUES(%s,%s,%s,%s,%s) RETURNING id",
                     (uid(), d, j["kind"], amt, str(j.get("note") or "")[:80]))
    db().commit()

    warn = []
    if table == "tx" and j["kind"] == "out":
        dl = limit_of("day", d)
        if dl is not None:
            s = spent(d)
            if s > dl:
                warn.append(f"Vượt hạn mức chi ngày: {money(s)} ₫ / {money(dl)} ₫")
        ym = d[:7]
        ml = limit_of("month", ym)
        if ml is not None:
            s = spent(ym)
            if s > ml:
                warn.append(f"Vượt hạn mức chi tháng: {money(s)} ₫ / {money(ml)} ₫")
    return jsonify(ok=1, warn=warn, id=cur.fetchone()["id"])


@app.post("/api/tx")
def add_tx():
    return add("tx", ("in", "out"))


@app.post("/api/sv")
def add_sv():
    return add("sv", ("dep", "wd"))


@app.post("/api/limit")
def set_limit():
    """Đặt hạn mức. amt = 0 nghĩa là xoá hạn mức đó.
    scope 'day': period 'default' hoặc 'YYYY-MM-DD' (riêng cho một ngày)
    scope 'month': period 'default' hoặc 'YYYY-MM' (riêng cho một tháng)."""
    if not uid():
        return err("Chưa đăng nhập", 401)
    if is_admin():
        return err("Tài khoản quản trị không quản lý tài chính", 403)
    j = request.get_json(force=True)
    scope = str(j.get("scope") or "")
    period = str(j.get("period") or "default")
    valid = (scope == "day" and (period == "default" or bool(DATE.match(period)))) or \
            (scope == "month" and (period == "default" or bool(MONTH.match(period))))
    if not valid:
        return err("Hạn mức không hợp lệ")
    try:
        amt = int(j.get("amt") or 0)
    except (TypeError, ValueError):
        return err("Số tiền không hợp lệ")
    if amt < 0 or amt > 10**13:
        return err("Số tiền không hợp lệ")
    if amt == 0:
        db().execute("DELETE FROM lim WHERE uid=%s AND scope=%s AND period=%s", (uid(), scope, period))
    else:
        db().execute("INSERT INTO lim(uid,scope,period,amt) VALUES(%s,%s,%s,%s) ON CONFLICT(uid,scope,period) DO UPDATE SET amt=EXCLUDED.amt",
                     (uid(), scope, period, amt))
    db().commit()
    return jsonify(ok=1)


@app.delete("/api/<t>/<int:i>")
def delete(t, i):
    if not uid() or t not in ("tx", "sv"):
        return err("Không hợp lệ", 401)
    if is_admin():
        return err("Tài khoản quản trị không quản lý tài chính", 403)
    db().execute(f"DELETE FROM {t} WHERE id=%s AND uid=%s", (i, uid()))
    db().commit()
    return jsonify(ok=1)


@app.post("/api/sv/undo")
def undo():
    if not uid():
        return err("Chưa đăng nhập", 401)
    if is_admin():
        return err("Tài khoản quản trị không quản lý tài chính", 403)
    r = db().execute("SELECT id FROM sv WHERE uid=%s AND kind='wd' ORDER BY id DESC LIMIT 1",
                     (uid(),)).fetchone()
    if not r:
        return err("Không có lần rút nào để hoàn tác")
    db().execute("DELETE FROM sv WHERE id=%s", (r["id"],))
    db().commit()
    return jsonify(ok=1)


@app.post("/api/cats")
def add_cat():
    if not uid():
        return err("Chưa đăng nhập", 401)
    if is_admin():
        return err("Tài khoản quản trị không quản lý tài chính", 403)
    j = request.get_json(force=True)
    kind = j.get("kind")
    name = str(j.get("name") or "").strip()[:24]
    if kind not in ("in", "out") or not name:
        return err("Dữ liệu không hợp lệ")
    if db().execute("SELECT 1 FROM cats WHERE uid=%s AND kind=%s AND lower(name)=lower(%s)",
                    (uid(), kind, name)).fetchone():
        return err("Danh mục đã tồn tại")
    cur = db().execute("INSERT INTO cats(uid,kind,name) VALUES(%s,%s,%s) RETURNING id", (uid(), kind, name))
    db().commit()
    return jsonify(id=cur.fetchone()["id"])


@app.delete("/api/cats/<int:i>")
def del_cat(i):
    if not uid():
        return err("Chưa đăng nhập", 401)
    if is_admin():
        return err("Tài khoản quản trị không quản lý tài chính", 403)
    if not db().execute("SELECT 1 FROM cats WHERE id=%s AND uid=%s", (i, uid())).fetchone():
        return err("Không tìm thấy danh mục", 404)
    db().execute("UPDATE tx SET cat=NULL WHERE cat=%s AND uid=%s", (i, uid()))
    db().execute("DELETE FROM cats WHERE id=%s AND uid=%s", (i, uid()))
    db().commit()
    return jsonify(ok=1)


@app.post("/api/tx/<int:i>")
def edit_tx(i):
    """Đổi danh mục của một giao dịch đã lưu. cat rỗng = Chưa phân loại."""
    if not uid():
        return err("Chưa đăng nhập", 401)
    if is_admin():
        return err("Tài khoản quản trị không quản lý tài chính", 403)
    r = db().execute("SELECT kind FROM tx WHERE id=%s AND uid=%s", (i, uid())).fetchone()
    if not r:
        return err("Không tìm thấy giao dịch", 404)
    j = request.get_json(force=True)
    cat = j.get("cat")
    if cat in (None, ""):
        cat = None
    else:
        try:
            cat = int(cat)
        except (TypeError, ValueError):
            return err("Danh mục không hợp lệ")
        if not db().execute("SELECT 1 FROM cats WHERE id=%s AND uid=%s AND kind=%s",
                            (cat, uid(), r["kind"])).fetchone():
            return err("Danh mục không hợp lệ")
    db().execute("UPDATE tx SET cat=%s WHERE id=%s AND uid=%s", (cat, i, uid()))
    db().commit()
    return jsonify(ok=1)


HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


@app.post("/api/cats/<int:i>")
def edit_cat(i):
    """Đổi màu của một danh mục."""
    if not uid():
        return err("Chưa đăng nhập", 401)
    if is_admin():
        return err("Tài khoản quản trị không quản lý tài chính", 403)
    if not db().execute("SELECT 1 FROM cats WHERE id=%s AND uid=%s", (i, uid())).fetchone():
        return err("Không tìm thấy danh mục", 404)
    j = request.get_json(force=True)
    col = str(j.get("color") or "")
    if not HEX_COLOR.match(col):
        return err("Màu không hợp lệ")
    db().execute("UPDATE cats SET color=%s WHERE id=%s AND uid=%s", (col.lower(), i, uid()))
    db().commit()
    return jsonify(ok=1)


@app.post("/api/restore/<t>")
def restore(t):
    """Khôi phục một giao dịch hoặc heo đất (dùng cho hoàn tác xoá và làm lại thêm)."""
    if not uid():
        return err("Chưa đăng nhập", 401)
    if is_admin():
        return err("Tài khoản quản trị không quản lý tài chính", 403)
    if t not in ("tx", "sv"):
        return err("Không hợp lệ")
    j = request.get_json(force=True)
    try:
        rid = int(j.get("id"))
        amt = int(j.get("amt"))
    except (TypeError, ValueError):
        return err("Dữ liệu không hợp lệ")
    kinds = ("in", "out") if t == "tx" else ("dep", "wd")
    d = str(j.get("d") or "")
    if j.get("kind") not in kinds or not DATE.match(d) or amt <= 0 or amt > 10**13:
        return err("Dữ liệu không hợp lệ")
    note = str(j.get("note") or "")[:80]
    cat = None
    if t == "tx" and j.get("cat") not in (None, ""):
        try:
            c = int(j.get("cat"))
            if db().execute("SELECT 1 FROM cats WHERE id=%s AND uid=%s AND kind=%s",
                            (c, uid(), j["kind"])).fetchone():
                cat = c
        except (TypeError, ValueError):
            pass  # danh mục cũ đã bị xoá: khôi phục thành Chưa phân loại
    try:
        if t == "tx":
            db().execute("INSERT INTO tx(id,uid,d,kind,amt,note,cat) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                         (rid, uid(), d, j["kind"], amt, note, cat))
        else:
            db().execute("INSERT INTO sv(id,uid,d,kind,amt,note) VALUES(%s,%s,%s,%s,%s,%s)",
                         (rid, uid(), d, j["kind"], amt, note))
        db().execute(f"SELECT setval(pg_get_serial_sequence('{t}','id'), GREATEST((SELECT MAX(id) FROM {t}),1))")
    except psycopg.errors.UniqueViolation:
        db().rollback()
        return err("Không thể hoàn tác vì dữ liệu đã thay đổi", 409)
    db().commit()
    return jsonify(ok=1)


PRO_DAYS = 30


def pro_of(owner):
    r = db().execute("SELECT pro_claimed, pro_until FROM users WHERE id=%s", (owner,)).fetchone()
    until = r["pro_until"] if r else None
    active = False
    if until:
        try:
            active = datetime.fromisoformat(until) > datetime.now()
        except ValueError:
            active = False
    return {"claimed": bool(r and r["pro_claimed"]), "until": until, "active": active}


@app.post("/api/pro/claim")
def pro_claim():
    """Nhận voucher PRO miễn phí: chỉ một lần cho mỗi tài khoản, có hiệu lực PRO_DAYS ngày."""
    if not uid():
        return err("Chưa đăng nhập", 401)
    if is_admin():
        return err("Tài khoản quản trị không dùng chức năng này", 403)
    r = db().execute("SELECT pro_claimed FROM users WHERE id=%s", (uid(),)).fetchone()
    if r and r["pro_claimed"]:
        return err("Voucher này đã được sử dụng")
    until = (datetime.now() + timedelta(days=PRO_DAYS)).isoformat(timespec="seconds")
    db().execute("UPDATE users SET pro_claimed=1, pro_until=%s WHERE id=%s", (until, uid()))
    db().commit()
    return jsonify(pro=pro_of(uid()))


@app.get("/")
def index():
    return Response(PAGE, mimetype="text/html")


PAGE = r"""<!doctype html><html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Financev">
<title>Financev</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Be+Vietnam+Pro:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
/* ---------- Bảng màu: sáng / tối (theo phong cách Apple) ---------- */
:root,[data-theme=light]{
 --bg:#f2f2f7;--card:#ffffff;--ink:#000000;--mut:#8e8e93;--mut2:#c7c7cc;
 --line:rgba(60,60,67,.14);--fill:rgba(118,118,128,.12);--field:#e9e9ef;--input:#e9e9ef;
 --accent:#0a84ff;--in:#248a3d;--out:#ff3b30;--today:#ff3b30;--sel:rgba(10,132,255,.12);
 --glass:rgba(255,255,255,.55);--glass-edge:rgba(255,255,255,.85);--glass-sh:0 10px 30px rgba(0,0,0,.12);
 --pill:rgba(255,255,255,.95);--ov:rgba(0,0,0,.25);--topbg:rgba(242,242,247,.62)}
[data-theme=dark]{color-scheme:dark;
 --bg:#000000;--card:#1c1c1e;--ink:#ffffff;--mut:#8e8e93;--mut2:#48484a;
 --line:rgba(84,84,88,.65);--fill:rgba(120,120,128,.28);--field:#2c2c2e;--input:#2c2b30;
 --accent:#0a84ff;--in:#30d158;--out:#ff453a;--today:#ff453a;--sel:rgba(10,132,255,.18);
 --glass:rgba(44,44,48,.45);--glass-edge:rgba(255,255,255,.18);--glass-sh:0 14px 40px rgba(0,0,0,.55);
 --pill:rgba(118,118,128,.5);--ov:rgba(0,0,0,.6);--topbg:rgba(0,0,0,.45)}
[data-theme=graphite]{color-scheme:dark;
 --bg:#18181a;--card:#2c2b30;--ink:#ffffff;--mut:#8e8e93;--mut2:#55545a;
 --line:#434246;--fill:#434246;--field:#2c2b30;--input:#2c2b30;
 --accent:#0a84ff;--in:#30d158;--out:#ff453a;--today:#ff453a;--sel:rgba(10,132,255,.18);
 --glass:rgba(44,43,48,.5);--glass-edge:rgba(255,255,255,.14);--glass-sh:0 14px 40px rgba(0,0,0,.5);
 --pill:rgba(118,118,128,.5);--ov:rgba(0,0,0,.6);--topbg:rgba(24,24,26,.5)}

/* ---------- Nền & chữ ---------- */
*{box-sizing:border-box}
body{margin:0;min-height:100vh;background:var(--bg);color:var(--ink);
 font:400 16px/1.4 -apple-system,BlinkMacSystemFont,"SF Pro Text","Helvetica Neue","Be Vietnam Pro",system-ui,sans-serif;
 letter-spacing:-.016em;font-variant-numeric:tabular-nums;-webkit-font-smoothing:antialiased;
 transition:background-color .4s,color .4s;padding:env(safe-area-inset-top) 0 env(safe-area-inset-bottom)}
button,input{font:inherit;color:inherit}
button{cursor:pointer;border:0;background:none;-webkit-tap-highlight-color:transparent}
h1,h2,h3{margin:0;font-weight:600;letter-spacing:-.02em}
small{color:var(--mut)}
.in{color:var(--in)}.out{color:var(--out)}
.wrap{max-width:980px;margin:0 auto;padding:0 20px 100px}

/* ---------- Thẻ ---------- */
.card{background:var(--card);border-radius:22px;padding:18px;margin-bottom:12px}

/* ---------- Kính lỏng (liquid glass) dùng chung ---------- */
.glass{background:var(--glass);border:1px solid var(--glass-edge);box-shadow:var(--glass-sh),inset 0 1px 0 rgba(255,255,255,.22);
 -webkit-backdrop-filter:blur(28px) saturate(180%);backdrop-filter:blur(28px) saturate(180%)}

/* ---------- Đầu trang ---------- */
header.top{position:sticky;top:0;z-index:30;display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;
 margin:0 -20px;padding:14px 20px;background:var(--topbg);
 -webkit-backdrop-filter:blur(24px) saturate(180%);backdrop-filter:blur(24px) saturate(180%)}
.brandrow{display:flex;align-items:center;gap:14px}
.brandrow .logo{width:58px;height:58px;border-radius:16px;flex:none;box-shadow:0 8px 20px rgba(0,0,0,.2)}
.logo{display:block}
.logo svg{width:100%;height:100%;display:block}
.wm{font-family:"SF Pro Display",-apple-system,BlinkMacSystemFont,"Be Vietnam Pro",system-ui,sans-serif;font-size:32px;font-weight:700;letter-spacing:-.035em;line-height:1.05}
.ver{display:inline-block;font-size:12px;font-weight:600;color:var(--mut);background:var(--fill);padding:3px 9px;border-radius:99px;margin-left:8px;vertical-align:middle;letter-spacing:0}
.credit{display:block;font-size:13px;color:var(--mut);margin-top:3px;letter-spacing:-.005em}
.who{display:flex;align-items:center;gap:8px;color:var(--mut);font-size:14px;flex-wrap:wrap}
.icon{width:40px;height:40px;border-radius:50%;display:grid;place-items:center;color:var(--ink)}
.icon:hover{opacity:.8}
.tk{overflow:hidden;white-space:nowrap;font-size:13px;color:var(--mut);margin:0 -20px 6px;padding:9px 0;border-bottom:1px solid var(--line)}
.tk>div{display:inline-block;padding-left:100%;animation:run 80s linear infinite}
.tk:hover>div{animation-play-state:paused}
.tk span{margin:0 24px}
.tk .in,.tk .out{font-weight:600}
@keyframes run{to{transform:translateX(-100%)}}

/* ---------- Thanh tab: kính lỏng dạng viên thuốc ---------- */
.tabs{position:relative;display:inline-flex;padding:5px;border-radius:999px;margin:14px 0 18px;max-width:100%}
.tabs button{position:relative;z-index:1;padding:9px 20px;border-radius:999px;color:var(--mut);font-weight:600;font-size:15px;transition:color .3s;white-space:nowrap}
.tabs button.on{color:var(--ink)}
#ind{position:absolute;top:5px;bottom:5px;left:0;width:0;border-radius:999px;background:var(--pill);box-shadow:0 2px 8px rgba(0,0,0,.18);transition:transform .5s cubic-bezier(.32,.72,0,1),width .5s cubic-bezier(.32,.72,0,1)}

/* ---------- Hiệu ứng chuyển tab ---------- */
#m{min-height:60vh}
.pg-r{animation:inR .45s cubic-bezier(.32,.72,0,1)}
.pg-l{animation:inL .45s cubic-bezier(.32,.72,0,1)}
@keyframes inR{from{opacity:0;transform:translate3d(28px,0,0)}to{opacity:1;transform:none}}
@keyframes inL{from{opacity:0;transform:translate3d(-28px,0,0)}to{opacity:1;transform:none}}

/* ---------- Nút, phân đoạn, ô nhập ---------- */
.btn{padding:9px 18px;border-radius:999px;background:var(--fill);font-size:15px;font-weight:600;transition:opacity .2s,transform .15s;white-space:nowrap}
.btn:hover{opacity:.75}
.btn:active{transform:scale(.97)}
.btn.chev{width:38px;height:38px;padding:0;display:grid;place-items:center;font-size:22px;line-height:1}
.btn.pri{background:var(--accent);color:#fff}
.btn.ghost{background:transparent;box-shadow:inset 0 0 0 1px var(--line)}
.btn.wide{min-width:150px}
.btn.sm{padding:7px 14px;font-size:14px}
.btn:disabled{opacity:.35;cursor:not-allowed;transform:none}
.seg{display:inline-flex;padding:3px;border-radius:999px;background:var(--fill)}
.seg button{padding:7px 16px;border-radius:999px;color:var(--mut);font-size:14px;font-weight:600;transition:background .25s,color .25s}
.seg button.on{background:var(--pill);color:var(--ink);box-shadow:0 2px 6px rgba(0,0,0,.16)}
.seg.full{display:flex}.seg.full button{flex:1}
input{width:100%;padding:14px 16px;border-radius:16px;border:0;background:var(--input);outline:none;transition:box-shadow .2s;-webkit-appearance:none;appearance:none;font-size:17px}
input:focus{box-shadow:inset 0 0 0 2px var(--accent)}
input[type=month],input[type=date]{width:auto;min-width:0;padding:9px 14px;font-size:15px;border-radius:12px}
.fld{display:grid;gap:6px;margin:0}
.fld span{font-size:13px;color:var(--mut)}
.err{color:var(--out);min-height:18px;font-size:14px}
.hint{font-size:13px;color:var(--mut);margin:4px 0 0}
.sub{font-size:13px;color:var(--mut);margin-top:8px}
.bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.bar.end{justify-content:flex-end;margin-top:16px}
.empty{color:var(--mut);text-align:center;padding:16px 0;font-size:15px}
.chip{display:inline-block;padding:3px 10px;border-radius:999px;font-size:12px;font-weight:600}
.chip.ok{background:rgba(48,209,88,.18);color:var(--in)}
.chip.off{background:var(--fill);color:var(--mut)}

/* ---------- Lịch kiểu Apple (không hiện âm lịch) ---------- */
.chead{display:flex;justify-content:space-between;align-items:flex-end;gap:14px;flex-wrap:wrap;margin:6px 2px 12px}
.ttl{font-family:"SF Pro Display",-apple-system,BlinkMacSystemFont,"Be Vietnam Pro",system-ui,sans-serif;font-size:32px;font-weight:700;letter-spacing:-.03em;line-height:1.1}
.chead small,.phead small{font-size:12px;text-transform:uppercase;letter-spacing:.05em;font-weight:600}
.cnav{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.calc{padding:4px 0 6px;overflow:hidden}
.dows{display:grid;grid-template-columns:repeat(7,1fr);padding:10px 0 6px;border-bottom:1px solid var(--line)}
.dows div{text-align:center;font-size:12px;font-weight:600;color:var(--ink)}
.dows div:nth-child(6),.dows div:nth-child(7){color:var(--mut)}
.cgrid{display:grid;grid-template-columns:repeat(7,1fr)}
.cell{all:unset;box-sizing:border-box;cursor:pointer;min-height:78px;padding:6px 0 5px;display:flex;flex-direction:column;align-items:center;gap:3px;border-bottom:1px solid var(--line);transition:background .2s}
.cell:hover{background:var(--fill)}
.cell:focus-visible{outline:2px solid var(--accent);outline-offset:-2px}
.dn{width:40px;height:40px;border-radius:50%;display:grid;place-items:center;font-size:22px;font-weight:500;letter-spacing:-.02em;transition:background .2s}
.cell.wk .dn{color:var(--mut)}
.cell.off .dn{color:var(--mut2)}
.cell.today .dn{background:var(--today);color:#fff;font-weight:600}
.cell.sel:not(.today) .dn{box-shadow:inset 0 0 0 2px var(--accent)}
.dots{display:flex;gap:3px;height:5px}
.dots i{width:5px;height:5px;border-radius:50%}
.dots .in{background:var(--in)}.dots .out{background:var(--out)}
.amt{font-size:11px;font-weight:600;color:var(--out);max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding:0 3px}
.cell.over .amt{background:var(--out);color:#fff;border-radius:999px}
.cgrid.week .cell{min-height:130px}

/* ---------- Thống kê ---------- */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-bottom:12px}
.stat{margin:0;padding:10px 16px 10px}
.stat small{display:block;font-size:13px;margin-bottom:4px}
.stat small:first-of-type{padding-bottom:5px;margin-bottom:4px;border-bottom:1px solid rgba(128,128,128,.22)}
.stat b{display:block;font-size:31px;font-weight:600;letter-spacing:-.025em;line-height:1.1}
.stat{position:relative;min-height:130px;overflow:hidden}
.si{position:absolute;right:-16px;top:46px;display:flex;align-items:center;justify-content:center;width:100px;height:100px;color:var(--ink);opacity:.9;pointer-events:none;-webkit-mask-image:radial-gradient(circle at 100% 100%,#000 0%,rgba(0,0,0,.5) 42%,rgba(0,0,0,0) 78%);mask-image:radial-gradient(circle at 100% 100%,#000 0%,rgba(0,0,0,.5) 42%,rgba(0,0,0,0) 78%)}
.si svg{width:100%;height:100%;display:block}
.stat .meter{margin-right:96px;margin-top:6px}
.stat b+small{display:block;margin-right:96px;margin-top:0;margin-bottom:0;font-size:12px;line-height:1.3}
.stat b.none{font-size:17px;color:var(--mut);font-weight:500}
.phead{display:flex;justify-content:space-between;align-items:flex-end;gap:14px;flex-wrap:wrap;margin:6px 2px 12px}
.ch{display:flex;justify-content:space-between;align-items:baseline;gap:10px;margin-bottom:8px;flex-wrap:wrap}
.ch h3{font-size:17px}
.ch b{font-size:17px;font-weight:600}
.meter{height:5px;border-radius:99px;background:var(--fill);overflow:hidden;margin:10px 0 0}
.meter i{display:block;height:100%;border-radius:99px;background:var(--in);transition:width .7s cubic-bezier(.32,.72,0,1)}
.meter.warn i{background:#ff9f0a}
.meter.over i{background:var(--out)}

/* ---------- Trung bình theo khoảng ngày ---------- */
.range{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:12px}
.range .to{color:var(--mut);font-size:14px}
.presets{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px}
.avg{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:10px}
.avg div{background:var(--field);border-radius:18px;padding:13px 15px}
.avg small{display:block;font-size:12px;margin-bottom:3px}
.avg b{font-size:20px;font-weight:600;letter-spacing:-.02em}
.sumline{font-size:13px;color:var(--mut);line-height:1.6}

/* ---------- Hạn mức ---------- */
.limc{padding:18px}
.limc h3{font-size:17px;margin-bottom:2px}
.fields{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:12px 0 2px}
.fields label{display:grid;gap:6px}
.fields label span{font-size:13px;color:var(--mut)}
.rowend{display:flex;justify-content:flex-end;margin-top:12px}
.dlrow{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;padding:12px 14px;border-radius:18px;background:var(--field);margin-bottom:12px}
.dlrow .lt{font-weight:600;display:block;font-size:15px}
.dlrow small{font-size:13px}
.dlin{display:flex;gap:6px;align-items:center;flex:1;justify-content:flex-end;min-width:220px}
.dlin input{max-width:160px;padding:10px 12px;font-size:16px}

/* ---------- Danh sách nhóm bo tròn ---------- */
.list{background:var(--field);border-radius:18px;padding:0 16px;overflow:hidden}
.li{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:13px 0;border-bottom:1px solid var(--line);font-size:16px}
.li:last-child{border-bottom:0}
.li .nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
.li .nm small{font-size:13px}
.li .lv{display:flex;align-items:center;gap:8px;font-weight:600;white-space:nowrap;font-variant-numeric:tabular-nums}
.li .lv small{font-size:13px;font-weight:400}
.row-click{cursor:pointer}
.row-click:hover{opacity:.7}
.del{width:22px;height:22px;border-radius:50%;background:var(--fill);color:var(--mut);font-size:13px;line-height:22px;text-align:center}
.del:hover{color:var(--out)}
.daysum{display:flex;justify-content:space-between;gap:10px;font-size:14px;color:var(--mut);margin:12px 2px 10px;flex-wrap:wrap}
.daysum b{font-weight:600;color:var(--ink)}
.daysum .in,.daysum .out{font-weight:600}

/* ---------- Biểu đồ ---------- */
.scroll{overflow-x:auto;margin:0 -4px;padding:0 4px 2px}
.chart{display:block;width:100%;min-width:500px;height:auto}
.chart text{fill:var(--mut);font-size:10px}
.chart .base{stroke:var(--line)}
.chart .lim{stroke:var(--mut);stroke-dasharray:4 4;stroke-width:1}
.chart .limt{font-size:10px}
.cb{transform-box:fill-box;transform-origin:50% 100%;animation:grow .8s cubic-bezier(.32,.72,0,1) both}
.cb.in{fill:var(--in)}.cb.out{fill:var(--out)}
@keyframes grow{from{transform:scaleY(0)}}

/* ---------- Heo đất ---------- */
.pig{display:flex;align-items:center;gap:20px;flex-wrap:wrap}
.pig-ic{width:92px;height:92px;border-radius:20px;background:var(--fill);color:var(--ink);display:flex;align-items:center;justify-content:center;flex:none}
html[data-layout=phone] .pig-ic{width:80px;height:80px}
.pig-ic svg{width:58%;height:58%;display:block}
input.ic{padding-left:44px!important;background-repeat:no-repeat;background-position:15px center;background-size:18px 18px}
input.ic-amt{background-image:url("data:image/svg+xml,%3Csvg%20xmlns%3D%27http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%27%20viewBox%3D%270%200%2024%2024%27%20fill%3D%27none%27%20stroke%3D%27%238e8e93%27%20stroke-width%3D%271.8%27%20stroke-linecap%3D%27round%27%20stroke-linejoin%3D%27round%27%3E%3Ccircle%20cx%3D%2712%27%20cy%3D%2712%27%20r%3D%279.5%27%2F%3E%3Ccircle%20cx%3D%2712%27%20cy%3D%2712%27%20r%3D%277%27%2F%3E%3Ctext%20x%3D%2712%27%20y%3D%2716%27%20text-anchor%3D%27middle%27%20font-family%3D%27Arial%2Csans-serif%27%20font-size%3D%2710%27%20font-weight%3D%27700%27%20fill%3D%27%238e8e93%27%20stroke%3D%27none%27%3E%C4%91%3C%2Ftext%3E%3C%2Fsvg%3E")}
input.ic-note{background-image:url("data:image/svg+xml,%3Csvg%20xmlns%3D%27http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%27%20viewBox%3D%270%200%2024%2024%27%20fill%3D%27none%27%20stroke%3D%27%238e8e93%27%20stroke-width%3D%271.8%27%20stroke-linecap%3D%27round%27%20stroke-linejoin%3D%27round%27%3E%3Cpath%20d%3D%27M4%2020h4L19%209l-4-4L4%2016z%27%2F%3E%3Cpath%20d%3D%27M13.5%206.5l4%204%27%2F%3E%3C%2Fsvg%3E")}
input.ic-dlv{background-image:url("data:image/svg+xml,%3Csvg%20xmlns%3D%27http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%27%20viewBox%3D%270%200%2024%2024%27%20fill%3D%27none%27%20stroke%3D%27%238e8e93%27%20stroke-width%3D%271.8%27%20stroke-linecap%3D%27round%27%20stroke-linejoin%3D%27round%27%3E%3Crect%20x%3D%273.5%27%20y%3D%275%27%20width%3D%2717%27%20height%3D%2715%27%20rx%3D%272.5%27%2F%3E%3Cpath%20d%3D%27M3.5%2010h17M8%203v4M16%203v4%27%2F%3E%3Ccircle%20cx%3D%2712%27%20cy%3D%2715%27%20r%3D%271.3%27%20fill%3D%27%238e8e93%27%20stroke%3D%27none%27%2F%3E%3C%2Fsvg%3E")}
input.ic-ld{background-image:url("data:image/svg+xml,%3Csvg%20xmlns%3D%27http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%27%20viewBox%3D%270%200%2024%2024%27%20fill%3D%27none%27%20stroke%3D%27%238e8e93%27%20stroke-width%3D%271.8%27%20stroke-linecap%3D%27round%27%20stroke-linejoin%3D%27round%27%3E%3Ccircle%20cx%3D%2712%27%20cy%3D%2712%27%20r%3D%279%27%2F%3E%3Cpath%20d%3D%27M12%207v5l3%202%27%2F%3E%3C%2Fsvg%3E")}
input.ic-lm{background-image:url("data:image/svg+xml,%3Csvg%20xmlns%3D%27http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%27%20viewBox%3D%270%200%2024%2024%27%20fill%3D%27none%27%20stroke%3D%27%238e8e93%27%20stroke-width%3D%271.8%27%20stroke-linecap%3D%27round%27%20stroke-linejoin%3D%27round%27%3E%3Crect%20x%3D%273.5%27%20y%3D%275%27%20width%3D%2717%27%20height%3D%2715%27%20rx%3D%272.5%27%2F%3E%3Cpath%20d%3D%27M3.5%2010h17M8%203v4M16%203v4%27%2F%3E%3C%2Fsvg%3E")}
input.ic-lx{background-image:url("data:image/svg+xml,%3Csvg%20xmlns%3D%27http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%27%20viewBox%3D%270%200%2024%2024%27%20fill%3D%27none%27%20stroke%3D%27%238e8e93%27%20stroke-width%3D%271.8%27%20stroke-linecap%3D%27round%27%20stroke-linejoin%3D%27round%27%3E%3Ccircle%20cx%3D%2712%27%20cy%3D%2712%27%20r%3D%278.5%27%2F%3E%3Ccircle%20cx%3D%2712%27%20cy%3D%2712%27%20r%3D%274.5%27%2F%3E%3Ccircle%20cx%3D%2712%27%20cy%3D%2712%27%20r%3D%271%27%2F%3E%3C%2Fsvg%3E")}
input.ic-a{background-image:url("data:image/svg+xml,%3Csvg%20xmlns%3D%27http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%27%20viewBox%3D%270%200%2024%2024%27%20fill%3D%27none%27%20stroke%3D%27%238e8e93%27%20stroke-width%3D%271.8%27%20stroke-linecap%3D%27round%27%20stroke-linejoin%3D%27round%27%3E%3Cpath%20d%3D%27M12%2015V4M7.5%208.5L12%204l4.5%204.5%27%2F%3E%3Cpath%20d%3D%27M4%2014v5a1%201%200%200%200%201%201h14a1%201%200%200%200%201-1v-5%27%2F%3E%3C%2Fsvg%3E")}
input.ic-n{background-image:url("data:image/svg+xml,%3Csvg%20xmlns%3D%27http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%27%20viewBox%3D%270%200%2024%2024%27%20fill%3D%27none%27%20stroke%3D%27%238e8e93%27%20stroke-width%3D%271.8%27%20stroke-linecap%3D%27round%27%20stroke-linejoin%3D%27round%27%3E%3Cpath%20d%3D%27M5%206h14M5%2012h10M5%2018h7%27%2F%3E%3C%2Fsvg%3E")}
input.ic-cn{background-image:url("data:image/svg+xml,%3Csvg%20xmlns%3D%27http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%27%20viewBox%3D%270%200%2024%2024%27%20fill%3D%27none%27%20stroke%3D%27%238e8e93%27%20stroke-width%3D%271.8%27%20stroke-linecap%3D%27round%27%20stroke-linejoin%3D%27round%27%3E%3Cpath%20d%3D%27M3%2012V4h8l10%2010-8%208z%27%2F%3E%3Ccircle%20cx%3D%277.5%27%20cy%3D%278.5%27%20r%3D%271.2%27%2F%3E%3C%2Fsvg%3E")}
input.ic-u{background-image:url("data:image/svg+xml,%3Csvg%20xmlns%3D%27http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%27%20viewBox%3D%270%200%2024%2024%27%20fill%3D%27none%27%20stroke%3D%27%238e8e93%27%20stroke-width%3D%271.8%27%20stroke-linecap%3D%27round%27%20stroke-linejoin%3D%27round%27%3E%3Ccircle%20cx%3D%2712%27%20cy%3D%278%27%20r%3D%274%27%2F%3E%3Cpath%20d%3D%27M4%2021a8%208%200%200%201%2016%200%27%2F%3E%3C%2Fsvg%3E")}
input.ic-p{background-image:url("data:image/svg+xml,%3Csvg%20xmlns%3D%27http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%27%20viewBox%3D%270%200%2024%2024%27%20fill%3D%27none%27%20stroke%3D%27%238e8e93%27%20stroke-width%3D%271.8%27%20stroke-linecap%3D%27round%27%20stroke-linejoin%3D%27round%27%3E%3Crect%20x%3D%275%27%20y%3D%2711%27%20width%3D%2714%27%20height%3D%279%27%20rx%3D%272.5%27%2F%3E%3Cpath%20d%3D%27M8%2011V8a4%204%200%200%201%208%200v3%27%2F%3E%3C%2Fsvg%3E")}
input.ic-nn{background-image:url("data:image/svg+xml,%3Csvg%20xmlns%3D%27http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%27%20viewBox%3D%270%200%2024%2024%27%20fill%3D%27none%27%20stroke%3D%27%238e8e93%27%20stroke-width%3D%271.8%27%20stroke-linecap%3D%27round%27%20stroke-linejoin%3D%27round%27%3E%3Crect%20x%3D%273%27%20y%3D%275%27%20width%3D%2718%27%20height%3D%2714%27%20rx%3D%272.5%27%2F%3E%3Ccircle%20cx%3D%279%27%20cy%3D%2711%27%20r%3D%272%27%2F%3E%3Cpath%20d%3D%27M6%2016a3%203%200%200%201%206%200M14.5%2010h4M14.5%2014h4%27%2F%3E%3C%2Fsvg%3E")}
input.ic-np{background-image:url("data:image/svg+xml,%3Csvg%20xmlns%3D%27http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%27%20viewBox%3D%270%200%2024%2024%27%20fill%3D%27none%27%20stroke%3D%27%238e8e93%27%20stroke-width%3D%271.8%27%20stroke-linecap%3D%27round%27%20stroke-linejoin%3D%27round%27%3E%3Ccircle%20cx%3D%278%27%20cy%3D%2712%27%20r%3D%274%27%2F%3E%3Cpath%20d%3D%27M12%2012h9M18%2012v3M21%2012v2%27%2F%3E%3C%2Fsvg%3E")}
input.ic-np2{background-image:url("data:image/svg+xml,%3Csvg%20xmlns%3D%27http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%27%20viewBox%3D%270%200%2024%2024%27%20fill%3D%27none%27%20stroke%3D%27%238e8e93%27%20stroke-width%3D%271.8%27%20stroke-linecap%3D%27round%27%20stroke-linejoin%3D%27round%27%3E%3Ccircle%20cx%3D%2712%27%20cy%3D%2712%27%20r%3D%279%27%2F%3E%3Cpath%20d%3D%27M8%2012.5l2.5%202.5L16%209.5%27%2F%3E%3C%2Fsvg%3E")}
input.ic-op{background-image:url("data:image/svg+xml,%3Csvg%20xmlns%3D%27http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%27%20viewBox%3D%270%200%2024%2024%27%20fill%3D%27none%27%20stroke%3D%27%238e8e93%27%20stroke-width%3D%271.8%27%20stroke-linecap%3D%27round%27%20stroke-linejoin%3D%27round%27%3E%3Cpath%20d%3D%27M12%203l7%203v5c0%204.5-3%208-7%2010-4-2-7-5.5-7-10V6z%27%2F%3E%3C%2Fsvg%3E")}
input.ic-rpn{background-image:url("data:image/svg+xml,%3Csvg%20xmlns%3D%27http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%27%20viewBox%3D%270%200%2024%2024%27%20fill%3D%27none%27%20stroke%3D%27%238e8e93%27%20stroke-width%3D%271.8%27%20stroke-linecap%3D%27round%27%20stroke-linejoin%3D%27round%27%3E%3Cpath%20d%3D%27M4%2020l1.5-4.5L16%205.5a2.1%202.1%200%200%201%203%203L8.5%2018.5z%27%2F%3E%3Cpath%20d%3D%27M4%2020h5%27%2F%3E%3C%2Fsvg%3E")}
.addrow2 input.ic{padding-left:44px}

.pig .sp{flex:1;min-width:200px}
.pig small{font-size:13px}
.pig h2{font-size:18px;margin:3px 0 2px}
.pig .big{font-size:38px;font-weight:700;letter-spacing:-.03em;color:var(--in);line-height:1.1}
.pbtn{display:flex;gap:8px;flex-wrap:wrap}

/* ---------- Banner chế độ xem ---------- */
.peek{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:14px 18px}
.peek small{display:block;font-size:12px}
.peek b{font-size:17px}

/* ---------- Popup: tấm trượt từ dưới lên (điện thoại) / hộp giữa màn hình (laptop) ---------- */
.ov{position:fixed;inset:0;display:flex;align-items:flex-end;justify-content:center;background:var(--ov);z-index:60;animation:fade .25s ease;transition:opacity .2s}
.ov.out{opacity:0}
.sheet{width:min(520px,100%);max-height:90vh;overflow:auto;background:var(--card);border-radius:34px 34px 0 0;padding:22px 18px calc(24px + env(safe-area-inset-bottom));box-shadow:0 -12px 40px rgba(0,0,0,.3);animation:up .42s cubic-bezier(.32,.72,0,1);transition:transform .25s}
.ov.out .sheet{transform:translateY(40px)}
.sheet h3{font-size:24px;margin:2px 0 6px;letter-spacing:-.03em}
.sheet .mh small{text-transform:capitalize;font-size:14px}
.stack{display:grid;gap:10px;margin:14px 0}
.addrow{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:16px}
.addrow .seg{grid-column:1 / -1}
.addrow input{padding:15px 16px}
@keyframes fade{from{opacity:0}}
@keyframes up{from{transform:translateY(100%)}to{transform:none}}

/* ---------- Thông báo ---------- */
.toast{position:fixed;left:50%;bottom:110px;transform:translate(-50%,16px);opacity:0;transition:opacity .3s,transform .4s cubic-bezier(.32,.72,0,1);background:var(--ink);color:var(--bg);padding:11px 20px;border-radius:999px;font-size:14px;font-weight:600;z-index:99;box-shadow:0 8px 24px rgba(0,0,0,.25);max-width:92vw;text-align:center}
.toast.show{opacity:1;transform:translate(-50%,0)}
.toast.warn{background:var(--out);color:#fff}

/* ---------- Đăng nhập ---------- */
.auth{max-width:360px;margin:10vh auto 0;text-align:center;padding:0 6px}
.auth .logo{width:84px;height:84px;margin:0 auto 14px;border-radius:22px;box-shadow:0 10px 26px rgba(0,0,0,.25)}
.auth .wm{font-size:38px}
.auth .credit{margin:6px 0 0;font-size:14px;color:var(--ink);opacity:.75}
.auth .sub{margin:6px 0 0;font-size:15px}
.auth .stack{margin:26px 0 6px}
.auth .bar{justify-content:center;margin-top:14px}

/* ---------- Bố cục: điện thoại (mặc định) ---------- */
.lay{display:block}
.lay-side{display:block;margin-top:12px}
.charts{display:block}
html[data-layout=phone] .wrap{max-width:460px;padding:0 16px 140px}
html[data-layout=phone] header.top{margin:0 -16px;padding:12px 16px}
html[data-layout=phone] .tk{margin:0 -16px 6px}
html[data-layout=phone] .wm{font-size:28px}
html[data-layout=phone] .brandrow .logo{width:50px;height:50px;border-radius:14px}
html[data-layout=phone] .cell{min-height:64px}
html[data-layout=phone] .dn{width:36px;height:36px;font-size:20px}
html[data-layout=phone] .amt{font-size:9.5px}
html[data-layout=phone] .cgrid.week .cell{min-height:100px}
html[data-layout=phone] .fields{grid-template-columns:1fr}
html[data-layout=phone] .avg{grid-template-columns:1fr}
html[data-layout=phone] .dlin{min-width:0;justify-content:flex-start;width:100%}
html[data-layout=phone] .pig .big{font-size:30px}
/* Thanh tab nổi phía dưới, kính lỏng */
html[data-layout=phone] .tabs{position:fixed;bottom:max(14px,env(safe-area-inset-bottom));left:50%;transform:translateX(-50%);z-index:40;margin:0;width:max-content;max-width:94vw;background:var(--glass);border:1px solid var(--glass-edge);box-shadow:var(--glass-sh),inset 0 1px 0 rgba(255,255,255,.22);-webkit-backdrop-filter:blur(28px) saturate(180%);backdrop-filter:blur(28px) saturate(180%)}
html[data-layout=phone] .tabs button{padding:9px 13px;font-size:14px}
html[data-layout=phone] .toast{bottom:96px}

/* ---------- Bố cục: laptop ---------- */
html[data-layout=laptop] .tabs{background:var(--glass);border:1px solid var(--glass-edge);box-shadow:var(--glass-sh),inset 0 1px 0 rgba(255,255,255,.22);-webkit-backdrop-filter:blur(28px) saturate(180%);backdrop-filter:blur(28px) saturate(180%)}
html[data-layout=laptop] .ov{align-items:center;padding:16px}
html[data-layout=laptop] .sheet{border-radius:34px;padding:24px}

/* ---------- Điện thoại nhỏ ---------- */
@media(max-width:420px){
 .wm{font-size:24px}
 .brandrow .logo{width:46px;height:46px}
 .card{padding:16px}
}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}

/* ---------- Kính lỏng (bản chỉnh): trong suốt, viền sáng phía trên và phía dưới ---------- */
:root,[data-theme=light]{--glass-base:rgba(255,255,255,.30);--rim-t:rgba(255,255,255,.98);--rim-b:rgba(255,255,255,.55);--pill-top:rgba(255,255,255,.98);--seg-on:rgba(255,255,255,.95)}
[data-theme=dark],[data-theme=graphite]{--glass-base:rgba(26,26,30,.30);--rim-t:rgba(255,255,255,.62);--rim-b:rgba(255,255,255,.28);--pill-top:rgba(120,120,128,.55);--seg-on:rgba(118,118,128,.55)}
.tabs,.seg,.icon,html[data-layout=phone] .tabs,html[data-layout=laptop] .tabs{
 background:linear-gradient(180deg,rgba(255,255,255,.14) 0%,rgba(255,255,255,.03) 45%,rgba(255,255,255,.08) 100%),var(--glass-base);
 border:0;position:relative;isolation:isolate;
 box-shadow:0 14px 34px rgba(0,0,0,.42),inset 0 1px 0 rgba(255,255,255,.10);
 -webkit-backdrop-filter:blur(26px) saturate(170%);backdrop-filter:blur(26px) saturate(170%)}
.tabs::before,.seg::before,.icon::before{content:"";position:absolute;inset:0;border-radius:inherit;padding:1.3px;pointer-events:none;z-index:0;
 background:linear-gradient(180deg,var(--rim-t) 0%,rgba(255,255,255,.08) 40%,rgba(255,255,255,.03) 60%,var(--rim-b) 100%);
 -webkit-mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);-webkit-mask-composite:xor;mask-composite:exclude}
.tabs>*,.seg>*,.icon>*{position:relative;z-index:1}
#ind{background:linear-gradient(180deg,var(--pill-top) 0%,rgba(255,255,255,.06) 100%),var(--pill);
 box-shadow:inset 0 1px 0 rgba(255,255,255,.35),inset 0 -1px 0 rgba(255,255,255,.08),0 4px 12px rgba(0,0,0,.32)}
.seg button.on{background:var(--seg-on);box-shadow:inset 0 1px 0 rgba(255,255,255,.3),0 2px 8px rgba(0,0,0,.3)}
.tabs button.on{color:var(--ink)}

/* ---------- Thanh kính: không viền, chỉ có sáng trên & dưới ---------- */
:root,[data-theme=light]{--glass-base:rgba(255,255,255,.42)}
[data-theme=dark],[data-theme=graphite]{--glass-base:rgba(30,30,34,.36)}
.tabs,.seg,.icon,html[data-layout=phone] .tabs,html[data-layout=laptop] .tabs{
 background:linear-gradient(180deg,rgba(255,255,255,.10),rgba(255,255,255,.02) 60%),var(--glass-base);
 border:0;box-shadow:0 10px 28px rgba(0,0,0,.30);
 -webkit-backdrop-filter:blur(26px) saturate(160%);backdrop-filter:blur(26px) saturate(160%)}

html[data-layout=phone] .tabs{max-width:calc(100vw - 20px)}
html[data-layout=phone] .tabs button{padding:8px 10px;font-size:13.5px}

/* ---------- Viền sáng trên & dưới: bám theo bo tròn, mờ dần ở hai đầu ---------- */
.tabs::before,.seg::before,.icon::before{display:block;content:"";position:absolute;inset:0;border-radius:inherit;padding:1.5px;pointer-events:none;z-index:0;
 background:linear-gradient(180deg,rgba(255,255,255,.80) 0%,rgba(255,255,255,.22) 22%,rgba(255,255,255,0) 42%,rgba(255,255,255,0) 58%,rgba(255,255,255,.12) 80%,rgba(255,255,255,.40) 100%);
 -webkit-mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);-webkit-mask-composite:xor;mask-composite:exclude}
[data-theme=light] .tabs::before,[data-theme=light] .seg::before,[data-theme=light] .icon::before{
 background:linear-gradient(180deg,rgba(255,255,255,1) 0%,rgba(255,255,255,.35) 22%,rgba(255,255,255,0) 42%,rgba(255,255,255,0) 58%,rgba(255,255,255,.25) 80%,rgba(255,255,255,.9) 100%)}

/* ---------- Điện thoại: header gọn trên một hàng ---------- */
html[data-layout=phone] #who{display:none}
html[data-layout=phone] .who{gap:6px}
html[data-layout=phone] .who .btn{padding:8px 12px;font-size:14px}
html[data-layout=phone] header.top{flex-wrap:nowrap}

/* ---------- Khai báo cuối cùng: thanh tab điện thoại luôn nổi ở đáy màn hình ---------- */
html[data-layout=phone] .tabs{position:fixed;top:auto;right:auto;bottom:max(14px,env(safe-area-inset-bottom));left:50%;transform:translateX(-50%);z-index:40;margin:0;width:max-content;max-width:calc(100vw - 20px)}

/* ---------- Bản chỉnh 3.1: viền sáng dịu hơn, bo góc mọi thẻ, lịch gọn ---------- */
.tabs::before,.seg::before,.icon::before{
 background:linear-gradient(180deg,rgba(255,255,255,.34) 0%,rgba(255,255,255,.08) 24%,rgba(255,255,255,0) 44%,rgba(255,255,255,0) 56%,rgba(255,255,255,.05) 80%,rgba(255,255,255,.16) 100%)}
[data-theme=light] .tabs::before,[data-theme=light] .seg::before,[data-theme=light] .icon::before{
 background:linear-gradient(180deg,rgba(255,255,255,.85) 0%,rgba(255,255,255,.2) 24%,rgba(255,255,255,0) 44%,rgba(255,255,255,0) 56%,rgba(255,255,255,.15) 80%,rgba(255,255,255,.5) 100%)}

/* Bo góc: thẻ, danh sách, ô nhập, popup */
.card,.stat,.limc,.peek,.calc{border-radius:30px}
.list,.dlrow,.avg div{border-radius:26px}
input,input[type=month],input[type=date]{border-radius:999px}
.dlin input{border-radius:999px}
.sheet{border-radius:40px 40px 0 0}
html[data-layout=laptop] .sheet{border-radius:40px}
.toast{border-radius:999px}
.meter{border-radius:999px}
.seg,.tabs,.icon{border-radius:999px}
.cell.over .dn{box-shadow:inset 0 0 0 2px var(--out)}

/* Bỏ vòng tròn đỏ khoanh ngày hôm nay (và viền đỏ ngày vượt hạn mức): chỉ đổi màu số */
.cell.today .dn{background:none;color:var(--today);font-weight:700}
.cell.over .dn{box-shadow:none}

/* ---------- Màn hình chờ khi mở web ---------- */
#splash{position:fixed;inset:0;z-index:999;display:flex;flex-direction:column;align-items:center;justify-content:center;background:var(--bg);transition:opacity .5s ease,visibility .5s;padding:24px;text-align:center}
#splash.hide{opacity:0;visibility:hidden}
#splash .spl-logo{width:104px;height:104px;border-radius:26px;overflow:hidden;box-shadow:0 14px 34px rgba(0,0,0,.3);animation:sp .6s cubic-bezier(.32,.72,0,1) both}
#splash .spl-logo svg{width:100%;height:100%;display:block}
#splash .spl-name{font-family:"SF Pro Display",-apple-system,BlinkMacSystemFont,"Be Vietnam Pro",system-ui,sans-serif;font-size:34px;font-weight:700;letter-spacing:-.035em;margin-top:18px;animation:sp .6s .1s cubic-bezier(.32,.72,0,1) both}
#splash .spl-sub{font-size:15px;color:var(--mut);margin-top:6px;animation:sp .6s .2s cubic-bezier(.32,.72,0,1) both}
#splash .spl-text{font-size:13px;color:var(--mut);margin-top:40px}
#splash .spl-bar{width:200px;height:4px;border-radius:999px;background:var(--fill);overflow:hidden;margin-top:10px}
#splash .spl-bar i{display:block;height:100%;width:0;border-radius:999px;background:var(--accent);animation:fillbar 2s linear forwards}
@keyframes fillbar{to{width:100%}}
@keyframes sp{from{opacity:0;transform:scale(.92)}to{opacity:1;transform:none}}

/* ---------- Danh mục & biểu đồ tròn (5.0.0) ---------- */
.cats{display:flex;flex-wrap:wrap;gap:8px;padding:4px 2px 12px;margin:0}
.cats::-webkit-scrollbar{display:none}
.cchip{flex:none;display:inline-flex;align-items:center;gap:7px;padding:9px 15px;border-radius:999px;background:var(--fill);font-size:14px;font-weight:600;color:var(--mut);transition:background .2s,color .2s;white-space:nowrap}
.cchip.on{background:var(--accent);color:#fff}
.cchip.static{color:var(--ink)}
.cx{width:20px;height:20px;border-radius:50%;background:var(--line);color:var(--mut);font-size:13px;line-height:20px;text-align:center}
.cx:hover{color:var(--out)}
.addrow2{display:grid;grid-template-columns:1fr auto;gap:10px;margin-top:10px}
.addrow2 input{padding:12px 16px}
.pie{display:block;width:min(260px,72%);height:auto;margin:6px auto 14px}
.pie .pt1{fill:var(--mut);font-size:13px;font-weight:500}
.pie .pt2{fill:var(--ink);font-size:22px;font-weight:700}
.tip{position:fixed;z-index:80;pointer-events:none;min-width:170px;max-width:260px;padding:10px 12px;border-radius:14px;background:var(--card);color:var(--ink);border:1px solid var(--line);box-shadow:var(--glass-sh);font-size:13px;line-height:1.45;opacity:0;transform:translateY(4px) scale(.98);transition:opacity .14s,transform .14s}
.tip.show{opacity:1;transform:none}
.tip b{display:block;margin-bottom:4px;font-weight:600}
.tip div{white-space:nowrap}
.tip .tp-sub{color:var(--mut);font-size:12px;margin-top:4px;white-space:normal}
.rcard{display:flex;align-items:center;gap:16px;flex-wrap:wrap;background:var(--card);color:var(--ink);border-radius:22px;padding:18px;margin-bottom:12px}
.rcard .rc-ico{width:52px;height:52px;border-radius:16px;background:var(--fill);color:var(--ink);display:grid;place-items:center;flex:none}
.rcard .rc-txt{flex:1;min-width:200px}
.rcard small{color:var(--mut)}
.rcard h3{font-size:20px;color:var(--ink);margin:2px 0 4px}
.rcard p{margin:0;color:var(--mut);font-size:14px}
.sw{display:inline-block;width:12px;height:12px;border-radius:50%;margin-right:9px;vertical-align:middle}
.cnav input[type=month]{padding:9px 14px}

/* ---------- Màn hình chờ: căn giữa đều, nút Cập nhật ---------- */
#splash{display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;width:100%;height:100%}
#splash>*{margin-left:auto;margin-right:auto}
#splash .spl-name,#splash .spl-sub{animation:none;width:100%;text-align:center;padding:0}
#splash .spl-name{margin-top:18px;letter-spacing:-.03em;font-size:34px;line-height:1.1}
#splash .spl-sub{margin-top:6px;font-size:15px}
#splash .spl-text{width:100%;text-align:center;margin-top:40px;font-size:14px;color:var(--mut)}
#splash .spl-bar{width:200px;margin-top:12px}
#splash .upd{margin-top:22px;padding:12px 44px;font-size:17px}
/* ---- Sửa chồng chữ: tên app / dòng credit / màn hình chờ ---- */
.wm{line-height:1.25;padding-bottom:3px}
.ver{line-height:1;vertical-align:middle;position:relative;top:-2px}
.credit{line-height:1.45;margin-top:10px;position:relative;z-index:1}
#splash .spl-name{line-height:1.25;padding-bottom:4px;white-space:nowrap}
#splash .spl-sub{display:block;line-height:1.45;margin-top:10px}
#splash .spl-text{line-height:1.45;margin-top:36px}
#splash .spl-bar{margin-top:14px}
.auth .credit{margin:10px 0 0;line-height:1.45}
/* ---- Đổi màu danh mục & đổi danh mục của giao dịch ---- */
.cdot{position:relative;width:18px;height:18px;border-radius:50%;flex:none;display:inline-block;border:1px solid var(--line);overflow:hidden;cursor:pointer;margin-right:2px}
.cdot input{position:absolute;left:-6px;top:-6px;width:calc(100% + 12px);height:calc(100% + 12px);opacity:0;cursor:pointer;border:0;padding:0}
.recat{font:inherit;font-size:13px;margin-left:6px;padding:3px 8px;border-radius:8px;border:1px solid var(--line);background:var(--fill);color:var(--ink);max-width:60%}
/* ---- Phân tích PRO ---- */
.protag{display:inline-block;font-size:10px;font-weight:700;letter-spacing:.04em;padding:2px 7px;border-radius:99px;background:linear-gradient(135deg,#ffe27a,#d69a1c);color:#1c1c1e;vertical-align:middle;margin-left:4px}
.pro-center{text-align:center}
.pro-badge{width:84px;height:84px;margin:0 auto 12px;border-radius:22px;overflow:hidden;box-shadow:0 10px 26px rgba(0,0,0,.25)}
.pro-badge svg{width:100%;height:100%;display:block}
.ticket{display:flex;align-items:stretch;margin:16px auto;max-width:340px;border-radius:16px;overflow:hidden;background:linear-gradient(135deg,#ffe27a,#d69a1c);color:#1c1c1e;text-align:left}
.tk-main{flex:1;padding:14px 16px}
.tk-main small{display:block;opacity:.7}
.tk-main b{display:block;font-size:19px;margin-top:2px}
.tk-cut{width:0;border-left:2px dashed rgba(0,0,0,.35);margin:10px 0}
.tk-side{width:76px;display:flex;align-items:center;justify-content:center;background:rgba(255,255,255,.25)}
.prices{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:8px 0;text-align:center}
.prices small{display:block;color:var(--mut)}
.prices s{font-size:18px;color:var(--mut)}
.prices b{font-size:24px}
.pro-prices{max-width:320px;margin:14px auto}
.ring{position:relative;width:150px;height:150px;margin:6px auto 4px}
.ring svg{width:100%;height:100%}
.ring .rv{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center}
.ring .rv b{font-size:38px;font-weight:700;line-height:1}
.grade{display:block;font-size:20px;font-weight:700;margin-top:6px}
.pro-line{padding:10px 0;border-bottom:1px solid var(--line);font-size:15px;line-height:1.5;text-align:left}
.pro-line:last-child{border-bottom:0}
/* ---- Điện thoại: chia đều các tab để không tràn ra ngoài; chi tiết điểm xuống dòng ---- */
html[data-layout=phone] .tabs{width:calc(100vw - 20px);max-width:calc(100vw - 20px);display:flex;justify-content:space-between;padding:4px}
html[data-layout=phone] .tabs button{flex:1 1 0;min-width:0;padding:7px 2px;font-size:12px;line-height:1.15;display:flex;flex-direction:column;align-items:center;justify-content:center;overflow:hidden;text-overflow:ellipsis}
html[data-layout=phone] .tabs button svg{margin:0 auto 2px!important;display:block}
.li.wrap{align-items:flex-start}
.li.wrap .nm{white-space:normal;overflow:visible;text-overflow:clip;line-height:1.35}
.li.wrap .nm small.nt{display:block;margin-top:3px}
.appinfo .lv small{font-size:12px;color:var(--mut);font-weight:500}
</style></head><body><div id="app"></div>
<script>
/* ---------- Logo FV ---------- */
const LOGO=`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><defs><linearGradient id="fvT" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#4b4e58"/><stop offset="1" stop-color="#121318"/></linearGradient></defs><rect width="64" height="64" rx="15" fill="url(#fvT)"/><text x="32" y="43" text-anchor="middle" font-family="-apple-system,BlinkMacSystemFont,Helvetica Neue,Arial,sans-serif" font-size="27" font-weight="700" letter-spacing="-1" fill="#ffffff">FV</text></svg>`;
(()=>{const l=document.createElement('link');l.rel='icon';l.type='image/svg+xml';l.href='data:image/svg+xml;charset=utf-8,'+encodeURIComponent(LOGO);document.head.appendChild(l)})();

/* Màn hình chờ: logo, tên, slogan và thanh tải 0 -> 100% trong 2 giây */
const APP_VER='6.0.0',APP_NAME='Office';let SEEN=null;try{SEEN=localStorage.getItem('fv_ver')}catch(e){}
const SPLASH_FIRST=SEEN!==APP_VER;
const SPLASH_MS=SPLASH_FIRST?5000:2000;
document.body.insertAdjacentHTML('afterbegin',`<div id="splash"><div class="spl-logo">${LOGO}</div><div class="spl-name">Financev</div><div class="spl-sub">Finance App by Vincent</div>${SPLASH_FIRST?`<div class="spl-text" id="slt">Cập nhật phiên bản ${APP_VER}</div><button class="btn pri upd" id="updbtn">Cập nhật</button>`:`<div class="spl-text" id="slt">Loading data…</div>`}<div class="spl-bar" id="slb"${SPLASH_FIRST?' style="display:none"':''}><i style="animation-duration:${SPLASH_MS}ms"></i></div></div>`);


/* ---------- Tiện ích ---------- */
const $=s=>document.querySelector(s),T=new Date();
const ymd=d=>d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0');
const TODAY=ymd(T);
const num=n=>(n||0).toLocaleString('vi-VN');
const fm=n=>num(n)+' ₫';
const shortN=n=>{n=Math.round(n||0);if(n>=1e6)return (n/1e6).toFixed(1).replace('.',',').replace(',0','')+'tr';if(n>=1e3)return Math.round(n/1e3)+'k';return String(n)};
const esc=s=>String(s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const vd=d=>d.split('-').reverse().join('/'),sh=d=>d.slice(8)+'/'+d.slice(5,7);
const longD=d=>{const [y,m,dd]=d.split('-').map(Number);return new Date(y,m-1,dd).toLocaleDateString('vi-VN',{weekday:'long',day:'numeric',month:'long',year:'numeric'})};
const dayNum=s=>{const [y,m,d]=s.split('-').map(Number);return Date.UTC(y,m-1,d)/864e5};
const digits=s=>String($(s)?.value||'').replace(/\D/g,'');
const sum=(a,k)=>a.filter(x=>x.kind===k).reduce((s,x)=>s+x.amt,0);
const bal=()=>sum(V().sv,'dep')-sum(V().sv,'wd');
const TABS_MAIN=[['cal','Lịch'],['sum','Tổng kết'],['sav','Tiết kiệm'],['pro','<svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true" style="vertical-align:-2px;margin-right:5px"><defs><linearGradient id="crG" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ffe27a"/><stop offset="1" stop-color="#d69a1c"/></linearGradient></defs><path d="M3 18 L2 7 L8 12 L12 4 L16 12 L22 7 L21 18 Z" fill="url(#crG)"/></svg>Phân tích'],['acc','Tài khoản']];
const SUN='<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>';
const MOON='<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>';
const firstOfMonth=TODAY.slice(0,8)+'01';
const S={user:null,role:null,pro:null,voucherSeen:false,tx:[],sv:[],lim:[],cats:[],cat:null,draft:null,catKind:'out',peek:null,tab:'cal',view:'month',cur:new Date(T.getFullYear(),T.getMonth(),T.getDate()),smo:TODAY.slice(0,7),sel:TODAY,kind:'out',rf:firstOfMonth,rt:TODAY};
let DAY=null,NAVKEY='';

/* Dữ liệu đang hiển thị: của chính mình, hoặc của người dùng đang được admin xem (chỉ đọc) */
const V=()=>S.peek||S;
const RO=()=>!!S.peek;

/* ---------- Hạn mức ---------- */
const exact=(scope,p)=>{const r=V().lim.find(x=>x.scope===scope&&x.period===p);return r?r.amt:null};
const dayLim=()=>exact('day','default');
const effDay=d=>exact('day',d)??dayLim();
const monLim=ym=>exact('month',ym)??exact('month','default');
function meter(v,mx){const p=mx?Math.min(100,v/mx*100):0,c=v>mx?'over':p>=85?'warn':'';return `<div class="meter ${c}"><i style="width:${p.toFixed(1)}%"></i></div>`}

/* ---------- Sáng / tối ---------- */
const THEMES=['light','dark','graphite'],THEME_NAME={light:'Sáng',dark:'Tối',graphite:'Xám than'};
const applyTheme=t=>document.documentElement.dataset.theme=t;
applyTheme(localStorage.th||(matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light'));
function detectLayout(){const ua=navigator.userAgent;return (/iPhone|Android.*Mobile|iPod/i.test(ua)||matchMedia('(max-width: 700px)').matches)?'phone':'laptop'}
function applyLayout(l){document.documentElement.dataset.layout=l}
applyLayout(detectLayout());
addEventListener('resize',()=>{const l=detectLayout();if(l!==document.documentElement.dataset.layout){applyLayout(l);if(S.user)render()}});
const GRAPH='<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 1 0 18z" fill="currentColor"/></svg>';
function paintTheme(){const t=document.documentElement.dataset.theme||'light',b=$('#th');if(b){b.innerHTML=t==='dark'?MOON:t==='graphite'?GRAPH:SUN;b.title='Giao diện: '+THEME_NAME[t]+' (bấm để đổi)'}}

/* ---------- API & thông báo ---------- */
async function api(p,m='GET',b){const r=await fetch(p,{method:m,headers:{'Content-Type':'application/json'},body:b?JSON.stringify(b):undefined});const j=await r.json().catch(()=>({}));if(!r.ok)throw new Error(j.error||('Lỗi máy chủ ('+r.status+'). Xem cửa sổ terminal chạy python để biết chi tiết'));return j}
function toast(t,isWarn){const e=document.createElement('div');e.className='toast'+(isWarn?' warn':'');e.textContent=t;document.body.append(e);requestAnimationFrame(()=>requestAnimationFrame(()=>e.classList.add('show')));setTimeout(()=>{e.classList.remove('show');setTimeout(()=>e.remove(),400)},2400)}

/* ---------- Popup ---------- */
function fmtIn(i){i.value=i.value.replace(/\D/g,'').replace(/\B(?=(\d{3})+(?!\d))/g,'.')}
function numInputs(root){if(root)root.querySelectorAll('input[inputmode=numeric]').forEach(i=>i.addEventListener('input',()=>fmtIn(i)))}
function modal(h,noFocus){const o=document.createElement('div');o.className='ov';o.innerHTML='<div class="sheet">'+h+'</div>';o.onclick=e=>{if(e.target===o)close(o)};document.body.append(o);numInputs(o);if(!noFocus)setTimeout(()=>{const f=o.querySelector('input');if(f)f.focus()},80);return o}
function close(o){if(!o||o.classList.contains('out'))return;o.classList.add('out');setTimeout(()=>o.remove(),240)}
addEventListener('keydown',e=>{if(e.key==='Escape'){const o=document.querySelector('.ov:not(.out)');if(o)close(o)}});

/* Popup chọn ngày: danh sách giao dịch, thêm giao dịch, hạn mức ngày (chỉ đọc nếu là admin đang xem) */
function openDay(d,k){S.sel=d;S.cat=null;S.draft=null;document.querySelectorAll('.cell').forEach(c=>c.classList.toggle('sel',c.dataset.d===d));
if(DAY&&DAY.o.isConnected)close(DAY.o);
DAY={o:modal('',true),d,k};paintDay()}

function paintDay(){if(!DAY||!DAY.o.isConnected)return;const {o,d,k}=DAY,fin=k==='fin',t=fin?'tx':'sv',X=V(),list=(fin?X.tx:X.sv).filter(x=>x.d===d),sb=sum(list,fin?'out':'wd'),ro=RO();
const cats=fin?X.cats.filter(c=>c.kind===S.kind):[];
o.querySelector('.sheet').innerHTML=`<div class="mh"><small>${longD(d)}</small><h3>${ro?'Chỉ xem · ':''}${fin?'Chi tiêu trong ngày':'Heo đất trong ngày'}</h3></div>
${fin&&!ro?dayLimitRow(d,sb):''}
${dayList(d,k)}
${ro?`<div class="bar end"><button class="btn" id="cl">Đóng</button></div>`:`
${cats.length?`<div class="cats">${cats.map(c=>`<button class="cchip ${S.cat===c.id?'on':''}" data-c="${c.id}">${esc(c.name)}</button>`).join('')}</div>`:''}
<div class="addrow">
 ${fin?`<div class="seg full" id="kd">${[['out','Chi tiêu'],['in','Thu nhập']].map(([v,l])=>`<button data-k="${v}" class="${S.kind===v?'on':''}">${l}</button>`).join('')}</div>`:''}
 <input class="ic ic-amt" id="amt" inputmode="numeric" autocomplete="off" placeholder="${fin?'Số tiền (₫)':'Số tiền gửi (₫)'}">
 <input class="ic ic-note" id="note" autocomplete="off" placeholder="Ghi chú (tuỳ chọn)">
</div>
<div class="err" id="e"></div>
<div class="bar end"><button class="btn" id="cl">Đóng</button><button class="btn pri" id="add">Thêm</button></div>`}`;
numInputs(o);
if(S.draft){const a=o.querySelector('#amt'),n=o.querySelector('#note');if(a)a.value=S.draft.amt;if(n)n.value=S.draft.note;S.draft=null}
o.querySelector('#cl').onclick=()=>close(o);
if(!ro){
 o.querySelector('#add').onclick=()=>addEntry(k,d);
 ['#amt','#note'].forEach(s=>o.querySelector(s).addEventListener('keydown',e=>{if(e.key==='Enter')addEntry(k,d)}));
 o.querySelectorAll('#kd button').forEach(b=>b.onclick=()=>{S.draft={amt:o.querySelector('#amt').value,note:o.querySelector('#note').value};S.kind=b.dataset.k;S.cat=null;paintDay()});
 o.querySelectorAll('.cchip[data-c]').forEach(b=>b.onclick=()=>{const id=+b.dataset.c;S.cat=S.cat===id?null:id;o.querySelectorAll('.cchip[data-c]').forEach(z=>z.classList.toggle('on',+z.dataset.c===S.cat))});
 o.querySelectorAll('.recat').forEach(s=>s.onchange=async()=>{try{const id=s.dataset.tx,old=(X.tx.find(z=>String(z.id)===id)||{}).cat||null,nw=s.value?+s.value:null;await api('/api/tx/'+id,'POST',{cat:nw});pushHist(()=>api('/api/tx/'+id,'POST',{cat:old}),()=>api('/api/tx/'+id,'POST',{cat:nw}));await refresh();toast('Đã đổi danh mục')}catch(e){showErr(e.message)}});
 o.querySelectorAll('.del').forEach(b=>b.onclick=async()=>{try{const row=(fin?X.tx:X.sv).find(z=>String(z.id)===b.dataset.i);await api(`/api/${t}/${b.dataset.i}`,'DELETE');if(row)pushHist(()=>api('/api/restore/'+t,'POST',row),()=>api(`/api/${t}/${row.id}`,'DELETE'));await refresh();toast('Đã xoá')}catch(e){showErr(e.message)}});
 const dls=o.querySelector('#dls'),dlx=o.querySelector('#dlx');
 if(dls)dls.onclick=()=>saveLimit('day',d,digits('#dlv')||0);
 if(dlx)dlx.onclick=()=>saveLimit('day',d,0);
}}

function catSelect(x,X){const opts=X.cats.filter(c=>c.kind===x.kind);return `<select class="recat" data-tx="${x.id}" aria-label="Đổi danh mục"><option value="">Chưa phân loại</option>${opts.map(c=>`<option value="${c.id}" ${x.cat===c.id?'selected':''}>${esc(c.name)}</option>`).join('')}</select>`}
function dayList(d,k){const fin=k==='fin',X=V(),list=(fin?X.tx:X.sv).filter(x=>x.d===d),A=fin?'in':'dep',a=sum(list,A),b=sum(list,fin?'out':'wd'),isA=x=>x.kind===A,ro=RO();
const rows=list.map(x=>{const cn=x.cat?((X.cats.find(c=>c.id===x.cat)||{}).name||''):'';return `<div class="li"><span class="nm">${esc(x.note)||(isA(x)?(fin?'Thu nhập':'Gửi heo'):(fin?'Chi tiêu':'Rút heo'))}${fin&&!ro?catSelect(x,X):(cn?`<small> · ${esc(cn)}</small>`:'')}</span><span class="lv ${isA(x)?'in':'out'}">${isA(x)?'+':'−'}${num(x.amt)} ₫${ro?'':`<button class="del" data-i="${x.id}" aria-label="Xoá">×</button>`}</span></div>`}).join('')||'<div class="empty">Chưa có giao dịch trong ngày</div>';
return `<div class="daysum"><span>${fin?'Thu':'Gửi'} <b class="in">${fm(a)}</b></span><span>${fin?'Chi':'Rút'} <b class="out">${fm(b)}</b></span><span>Còn lại <b>${fm(a-b)}</b></span></div><div class="list">${rows}</div>`}

function showErr(m){if(DAY&&DAY.o.isConnected){const e=DAY.o.querySelector('#e');if(e)e.textContent=m}else toast(m,true)}
async function refresh(){await load();paintDay()}

async function addEntry(k,d){const fin=k==='fin',t=fin?'tx':'sv';
try{const body={d,kind:fin?S.kind:'dep',amt:digits('#amt'),note:DAY.o.querySelector('#note').value};if(fin)body.cat=S.cat;
const r=await api('/api/'+t,'POST',body);S.cat=null;
const row={id:r.id,d,kind:body.kind,amt:body.amt,note:(body.note||'').slice(0,80),cat:body.cat||null};
pushHist(()=>api('/api/'+t+'/'+row.id,'DELETE'),()=>api('/api/restore/'+t,'POST',row));
await refresh();if(r.warn&&r.warn.length)r.warn.forEach(w=>toast(w,true));else toast('Đã lưu');
const a=DAY&&DAY.o.querySelector('#amt');if(a&&matchMedia('(pointer:fine)').matches)a.focus()}catch(e){showErr(e.message)}}

async function saveLimit(scope,period,amt){try{await api('/api/limit','POST',{scope,period,amt});await load();toast(Number(amt)===0?'Đã dùng hạn mức mặc định':'Đã lưu hạn mức ngày');paintDay()}catch(e){showErr(e.message)}}
async function saveLimits(ym){try{
await api('/api/limit','POST',{scope:'day',period:'default',amt:digits('#ld')||0});
await api('/api/limit','POST',{scope:'month',period:'default',amt:digits('#lm')||0});
await api('/api/limit','POST',{scope:'month',period:ym,amt:digits('#lx')||0});
await load();toast('Đã lưu hạn mức')}catch(e){const el=$('#le');if(el)el.textContent=e.message}}

function dayLimitRow(d,spent){const ex=exact('day',d),eff=effDay(d);
return `<div class="dlrow"><div><span class="lt">Hạn mức ngày</span><small>${ex!==null?'Riêng cho ngày này':eff!==null?'Theo mặc định '+fm(eff):'Không giới hạn'}</small></div>
<div class="dlin"><input class="ic ic-dlv" id="dlv" inputmode="numeric" autocomplete="off" placeholder="Không giới hạn" value="${ex!==null?num(ex):''}"><button class="btn pri" id="dls">Lưu</button>${ex!==null?'<button class="btn ghost" id="dlx">Mặc định</button>':''}</div></div>
${eff!==null?meter(spent,eff)+`<div class="sub">${spent>eff?'Vượt '+fm(spent-eff):'Đã chi '+fm(spent)+' / '+fm(eff)}</div>`:''}`}

/* ---------- Đăng nhập ---------- */
function authView(){NAVKEY='';$('#app').innerHTML=`<div class="auth"><div class="logo">${LOGO}</div><div class="wm">Financev<span class="ver">6.0.0 · Office</span></div><p class="credit">App's Designed, Created, Compiled by Vincent</p><p class="sub">Quản lý tài chính cá nhân</p>
<div class="stack"><input class="ic ic-u" id="u" placeholder="Tên đăng nhập" autocomplete="username"><input class="ic ic-p" id="p" type="password" placeholder="Mật khẩu (tối thiểu 4 ký tự)" autocomplete="current-password"></div>
<div class="err" id="e"></div><div class="bar"><button class="btn pri wide" id="li">Đăng nhập</button><button class="btn wide" id="rg">Tạo tài khoản</button></div></div>`;
const go=m=>async()=>{try{await api('/api/'+m,'POST',{name:$('#u').value,pw:$('#p').value});await boot()}catch(e){$('#e').textContent=e.message}};
$('#li').onclick=go('login');$('#rg').onclick=go('register');$('#p').onkeydown=e=>{if(e.key==='Enter')go('login')()};$('#u').focus()}

/* ---------- Khung chính ---------- */
function tabsFor(){
 if(S.peek)return [['cal','Lịch'],['sum','Tổng kết'],['sav','Tiết kiệm']];
 if(S.role==='admin')return [['adm','Người dùng'],['acc','Tài khoản']];
 return TABS_MAIN}

function shell(){if($('#shell'))return;NAVKEY='';
$('#app').innerHTML=`<div id="shell" class="wrap">
<div id="tick"></div>
<header class="top"><div class="brandrow"><span class="logo">${LOGO}</span><div><span class="wm">Financev</span><span class="ver">6.0.0 · Office</span><small class="credit">App's Designed, Created, Compiled by Vincent</small></div></div>
<div class="who"><span id="who"></span><button class="icon" id="th"></button><button class="btn" id="lo">Thoát</button></div></header>
<nav class="tabs" id="nav"></nav>
<main id="m"></main></div>`;
$('#nav').onclick=e=>{const b=e.target.closest('button[data-t]');if(!b||b.dataset.t===S.tab)return;const L=tabsFor(),old=L.findIndex(t=>t[0]===S.tab),nw=L.findIndex(t=>t[0]===b.dataset.t);S.tab=b.dataset.t;render(nw>old?1:-1);scrollTo({top:0,behavior:'smooth'})};
$('#th').onclick=()=>{const cur=document.documentElement.dataset.theme,n=THEMES[(THEMES.indexOf(cur)+1)%THEMES.length];applyTheme(n);localStorage.th=n;paintTheme()};
$('#lo').onclick=async()=>{await api('/api/logout','POST');S.user=null;S.role=null;S.peek=null;if(DAY&&DAY.o)DAY.o.remove();authView()};
addEventListener('resize',moveInd)}
function moveInd(){const b=document.querySelector('#nav button.on'),i=$('#ind');if(!b||!i)return;i.style.width=b.offsetWidth+'px';i.style.transform=`translateX(${b.offsetLeft}px)`}

const VIEWS={cal:()=>calView('fin'),sav:()=>calView('sav'),sum:sumView,pro:proView,acc:accView,adm:admView};

function render(dir=0){const fresh=!$('#shell');shell();if(fresh)dir=dir||1;
const y=scrollY;
const L=tabsFor();if(!L.some(t=>t[0]===S.tab))S.tab=L[0][0];
const key=L.map(t=>t[0]).join('|')+(S.peek?'|peek':'');
if(key!==NAVKEY){NAVKEY=key;$('#nav').innerHTML='<i id="ind"></i>'+L.map(([k,l])=>`<button data-t="${k}">${l}</button>`).join('')}
$('#who').textContent=(S.peek?'Đang xem: '+S.peek.name:S.user||'');paintTheme();
$('#tick').innerHTML='';
document.querySelectorAll('#nav button').forEach(b=>b.classList.toggle('on',b.dataset.t===S.tab));moveInd();
VIEWS[S.tab]();
if(S.peek){$('#m').insertAdjacentHTML('afterbegin',`<section class="card peek"><div><small>Chế độ chỉ xem</small><b>Lịch của ${esc(S.peek.name)}</b></div><button class="btn" id="exitp">Quay lại</button></section>`);$('#exitp').onclick=()=>{S.peek=null;S.tab='adm';render(-1)}}
const m=$('#m');m.classList.remove('pg-l','pg-r');
if(dir){void m.offsetWidth;m.classList.add(dir>0?'pg-r':'pg-l')}else scrollTo(0,y)}

/* ---------- Dòng chạy 30 ngày gần nhất ---------- */
function ticker(){const X=V(),from=new Date(T);from.setDate(from.getDate()-30);const f=ymd(from),m={};
X.tx.filter(x=>x.d>=f).forEach(x=>{(m[x.d]??={in:0,out:0,sv:0})[x.kind]+=x.amt});
X.sv.filter(x=>x.d>=f&&x.kind==='dep').forEach(x=>{(m[x.d]??={in:0,out:0,sv:0}).sv+=x.amt});
const it=Object.keys(m).sort().reverse().map(d=>{const v=m[d];let s=`<span>${vd(d)}`;
if(v.in||v.out)s+=` · thu <b class="in">${fm(v.in)}</b> · chi <b class="out">${fm(v.out)}</b>`;if(v.sv)s+=` · heo đất <b class="in">${fm(v.sv)}</b>`;return s+'</span>'}).join('');
return `<div class="tk"><div>${it||'<span>Chưa có giao dịch trong 30 ngày gần nhất</span>'}</div></div>`}

/* ---------- Lịch (chi tiêu & tiết kiệm) ---------- */
/* ---------- Icon đơn sắc cho thẻ thống kê (nét đường, theo màu chữ) ---------- */
const SI_PATHS={
 'in':'<path d="M12 5v14"/><path d="M6 13l6 6 6-6"/>',
 'out':'<path d="M12 19V5"/><path d="M6 11l6-6 6 6"/>',
 'wallet':'<rect x="3" y="6" width="18" height="13" rx="3"/><path d="M16 12.5h2"/><path d="M6 6V5a2 2 0 0 1 2-2h8"/>',
 'target':'<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="4.5"/><circle cx="12" cy="12" r="1"/>',
 'jar':'<rect x="6" y="8" width="12" height="12" rx="3"/><path d="M9 8V5h6v3"/><path d="M12 12v4"/>',
 'users':'<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0"/><circle cx="17" cy="9" r="2.5"/><path d="M16 14.5a5 5 0 0 1 5.5 4.5"/>',
 'check':'<circle cx="12" cy="12" r="9"/><path d="M8 12.5l2.5 2.5L16 9.5"/>',
 'minus':'<circle cx="12" cy="12" r="9"/><path d="M8 12h8"/>'
};
const si=k=>`<span class="si" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${SI_PATHS[k]}</svg></span>`;

function calView(k){
const X=V(),fin=k==='fin',list=fin?X.tx:X.sv,A=fin?'in':'dep',B=fin?'out':'wd',c=S.cur,ro=RO();
let days=[],title;
if(S.view==='week'){const s=new Date(c);s.setDate(c.getDate()-((c.getDay()+6)%7));for(let i=0;i<7;i++){const d=new Date(s);d.setDate(s.getDate()+i);days.push(d)}title=`${sh(ymd(days[0]))} – ${sh(ymd(days[6]))}/${days[6].getFullYear()}`}
else{const f=new Date(c.getFullYear(),c.getMonth(),1),s=new Date(f);s.setDate(1-((f.getDay()+6)%7));const n=Math.ceil((((f.getDay()+6)%7)+new Date(c.getFullYear(),c.getMonth()+1,0).getDate())/7)*7;for(let i=0;i<n;i++){const d=new Date(s);d.setDate(s.getDate()+i);days.push(d)}title=`Tháng ${c.getMonth()+1}, ${c.getFullYear()}`}
const ym=c.getFullYear()+'-'+String(c.getMonth()+1).padStart(2,'0');
const inR=d=>S.view==='week'||d.getMonth()===c.getMonth(),rng=days.filter(inR).map(ymd),per=list.filter(x=>rng.includes(x.d)),a=sum(per,A),b=sum(per,B);
const ml=fin?monLim(ym):null,mSpent=sum(X.tx.filter(x=>x.d.startsWith(ym)),'out');

let h='';
if(!fin)h+=pigBox();
h+=`<div class="chead"><div><small>${fin?'Chi tiêu':'Heo đất'}</small><h2 class="ttl">${title}</h2></div>
<div class="cnav"><div class="seg">${[['week','Tuần'],['month','Tháng']].map(([v,l])=>`<button data-v="${v}" class="${S.view===v?'on':''}">${l}</button>`).join('')}</div>
<button class="btn chev" id="pv" aria-label="Trước">‹</button><button class="btn" id="td">Hôm nay</button><button class="btn chev" id="nx" aria-label="Sau">›</button></div></div>`;

h+=`<div class="stats">`;
if(fin){h+=`<div class="card stat">${si('in')}<small>Thu trong kỳ</small><b class="in">${fm(a)}</b></div><div class="card stat">${si('out')}<small>Chi trong kỳ</small><b class="out">${fm(b)}</b></div>
<div class="card stat">${si('target')}<small>Hạn mức tháng ${c.getMonth()+1}</small>${ml!==null?`<b class="${mSpent>ml?'out':''}">${fm(mSpent)}</b><small>${mSpent>ml?'Vượt '+fm(mSpent-ml):'Còn '+fm(ml-mSpent)} · hạn mức ${fm(ml)}</small>${meter(mSpent,ml)}`:`<b class="none">Chưa đặt</b><small>Đặt ở mục Hạn mức bên dưới</small>`}</div>`}
else{h+=`<div class="card stat">${si('in')}<small>Đã gửi</small><b class="in">${fm(a)}</b></div><div class="card stat">${si('out')}<small>Đã rút</small><b class="out">${fm(b)}</b></div><div class="card stat">${si('jar')}<small>Số dư heo</small><b>${fm(bal())}</b></div>`}
h+=`</div>`;

h+=`<div class="lay"><div class="lay-main"><section class="card calc"><div class="dows">${['T2','T3','T4','T5','T6','T7','CN'].map(x=>`<div>${x}</div>`).join('')}</div><div class="cgrid ${S.view}">`;
days.forEach(d=>{const s=ymd(d),e=list.filter(x=>x.d===s),x=sum(e,A),y=sum(e,B),L=fin?effDay(s):null,over=L!==null&&y>L;
const cls=[inR(d)?'':'off',(d.getDay()===0||d.getDay()===6)?'wk':'',s===TODAY?'today':'',s===S.sel?'sel':'',over?'over':''].join(' ');
h+=`<button class="cell ${cls}" data-d="${s}"><span class="dn">${d.getDate()}</span><span class="dots">${x?'<i class="in"></i>':''}${y?'<i class="out"></i>':''}</span>${y?`<span class="amt">−${shortN(y)}</span>`:''}</button>`});
h+=`</div></section>`;

h+='</div>';if(fin&&!ro)h+=`<aside class="lay-side">${limitCard(ym)}</aside>`;h+='</div>';
$('#m').innerHTML=h;
numInputs($('#m'));

/* sự kiện */
document.querySelectorAll('[data-v]').forEach(e=>e.onclick=()=>{S.view=e.dataset.v;render()});
const mv=n=>{if(S.view==='week')c.setDate(c.getDate()+7*n);else c.setMonth(c.getMonth()+n,1);render()};
$('#pv').onclick=()=>mv(-1);$('#nx').onclick=()=>mv(1);
$('#td').onclick=()=>{S.cur=new Date(T.getFullYear(),T.getMonth(),T.getDate());S.sel=TODAY;render()};
document.querySelectorAll('.cell').forEach(e=>e.onclick=()=>openDay(e.dataset.d,k));
if(fin&&!ro)$('#lsave').onclick=()=>saveLimits(ym);
if(!fin&&!ro){$('#wd').onclick=withdraw;$('#un').onclick=async()=>{try{await api('/api/sv/undo','POST');toast('Đã hoàn tác lần rút gần nhất');await load()}catch(e){toast(e.message,true)}}}}

function limitCard(ym){const [y,m]=ym.split('-');const dl=exact('day','default'),mo=exact('month','default'),mx=exact('month',ym);
return `<section class="card limc"><h3>Hạn mức chi tiêu</h3>

<div class="fields"><label><span>Mỗi ngày</span><input class="ic ic-ld" id="ld" inputmode="numeric" autocomplete="off" value="${dl!==null?num(dl):''}" placeholder="Ví dụ 300.000"></label>
<label><span>Mỗi tháng (mặc định)</span><input class="ic ic-lm" id="lm" inputmode="numeric" autocomplete="off" value="${mo!==null?num(mo):''}" placeholder="Ví dụ 10.000.000"></label>
<label><span>Riêng tháng ${m}/${y}</span><input class="ic ic-lx" id="lx" inputmode="numeric" autocomplete="off" value="${mx!==null?num(mx):''}" placeholder="Để trống = mức mặc định"></label></div>
<div class="err" id="le"></div><div class="rowend"><button class="btn pri" id="lsave">Lưu hạn mức</button></div></section>`}

/* ---------- Heo đất ---------- */
const PIG=`<span class="pig-ic" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx='12' cy='12' r='9.5'/><circle cx='12' cy='12' r='7'/><text x='12' y='16' text-anchor='middle' font-family='Arial,sans-serif' font-size='10' font-weight='700' fill='currentColor' stroke='none'>đ</text></svg></span>`;
function pigBox(){const has=V().sv.some(x=>x.kind==='wd'),ro=RO();return `<section class="card pig">${PIG}<div class="sp"><small>Heo đất</small><h2>${ro?'Số tiền đã tiết kiệm':'Bạn đã tiết kiệm được'}</h2><div class="big">${fm(bal())}</div></div>${ro?'':`<div class="pbtn"><button class="btn pri" id="wd">Rút tiền</button><button class="btn" id="un" ${has?'':'disabled'}>Hoàn tác lần rút</button></div>`}</section>`}
function withdraw(){const o=modal(`<h3>Rút tiền heo đất</h3><p class="hint">Số dư hiện có: <b>${fm(bal())}</b></p><div class="stack"><input class="ic ic-a" id="a" type="text" inputmode="numeric" autocomplete="off" placeholder="Số tiền cần rút (₫)"><input class="ic ic-n" id="n" placeholder="Lý do (tuỳ chọn)"></div><div class="err" id="e"></div><div class="bar end"><button class="btn" id="cl">Huỷ</button><button class="btn pri" id="ok">Xác nhận rút</button></div>`);
o.querySelector('#cl').onclick=()=>close(o);
o.querySelector('#ok').onclick=async()=>{try{await api('/api/sv','POST',{d:TODAY,kind:'wd',amt:o.querySelector('#a').value.replace(/\D/g,''),note:o.querySelector('#n').value});close(o);toast('Đã rút tiền · có thể hoàn tác');await load()}catch(e){o.querySelector('#e').textContent=e.message}}}

/* ---------- Biểu đồ cột ---------- */
function chart(rows,key,cls,limit){
const W=620,H=170,n=rows.length,mx=Math.max(1,limit||0,...rows.map(r=>r[key])),slot=W/n,bw=Math.max(5,Math.min(14,slot*.56));
let s=`<svg class="chart" viewBox="0 -10 ${W} ${H+30}" role="img" aria-label="Biểu đồ ${cls==='in'?'thu':'chi'} theo ngày"><line class="base" x1="0" x2="${W}" y1="${H}" y2="${H}"/>`;
const tot=rows.reduce((q,z)=>q+z[key],0);
rows.forEach((r,i)=>{const v=r[key],h=v/mx*(H-10),x=i*slot+(slot-bw)/2,pc=tot?Math.round(v/tot*100):0;
const tipH=`<b>${vd(r.d)}</b><div>Thu: <span class="in">${fm(r.a)}</span></div><div>Chi: <span class="out">${fm(r.b)}</span></div>`+
 (cls==='out'&&limit?`<div class="tp-sub">Hạn mức ngày ${fm(limit)} · ${r.b>limit?'vượt '+fm(r.b-limit):'còn '+fm(limit-r.b)}</div>`:'')+
 `<div class="tp-sub">Chiếm ${pc}% tổng ${cls==='in'?'thu':'chi'} trong tháng</div>`;
s+=`<rect class="cb ${cls}" x="${x.toFixed(1)}" y="${(H-h).toFixed(1)}" width="${bw.toFixed(1)}" height="${h.toFixed(1)}" rx="${Math.min(4,bw/2).toFixed(1)}" style="animation-delay:${i*16}ms" data-tip="${esc(tipH)}"></rect>`;
if((i+1)%5===0||i===0)s+=`<text x="${(x+bw/2).toFixed(1)}" y="${H+18}" text-anchor="middle">${r.l}</text>`});
if(limit){const ly=(H-limit/mx*(H-10)).toFixed(1);s+=`<line class="lim" x1="0" x2="${W}" y1="${ly}" y2="${ly}"/><text class="limt" x="${W-2}" y="${ly-6}" text-anchor="end">Hạn mức ${num(limit)}</text>`}
return `<div class="scroll">${s}</svg></div>`}

/* ---------- Trung bình thu/chi theo khoảng ngày tuỳ chọn ---------- */
function rangeCard(){
 const f=S.rf,t=S.rt,X=V();let body;
 if(!f||!t)body='<div class="err">Chọn đủ ngày bắt đầu và ngày kết thúc</div>';
 else if(f>t)body='<div class="err">Ngày bắt đầu phải trước hoặc trùng ngày kết thúc</div>';
 else{
  const days=dayNum(t)-dayNum(f)+1,inR=X.tx.filter(x=>x.d>=f&&x.d<=t),a=sum(inR,'in'),b=sum(inR,'out'),net=a-b;
  const expDays=new Set(inR.filter(x=>x.kind==='out').map(x=>x.d)).size;
  const incDays=new Set(inR.filter(x=>x.kind==='in').map(x=>x.d)).size;
  const avgIn=Math.round(a/days),avgOut=Math.round(b/days),avgNet=Math.round(net/days);
  body=`<div class="avg">
   <div><small>Thu trung bình / ngày</small><b class="in">${fm(avgIn)}</b></div>
   <div><small>Chi trung bình / ngày</small><b class="out">${fm(avgOut)}</b></div>
   <div><small>Còn lại trung bình / ngày</small><b class="${avgNet<0?'out':''}">${fm(avgNet)}</b></div></div>
  <div class="sumline">${days} ngày từ ${vd(f)} đến ${vd(t)} · tổng thu ${fm(a)} (${incDays} ngày có thu) · tổng chi ${fm(b)} (${expDays} ngày có chi)${expDays?` · chi trung bình mỗi ngày có chi ${fm(Math.round(b/expDays))}`:''}</div>`;
 }
 return `<section class="card" id="rcard">
  <div class="ch"><h3>Trung bình theo khoảng ngày</h3></div>
  <div class="presets"><button class="btn sm" data-p="month">Tháng này</button><button class="btn sm" data-p="30">30 ngày qua</button><button class="btn sm" data-p="year">Năm nay</button></div>
  <div class="range"><input type="date" id="rf" value="${f}"><span class="to">đến</span><input type="date" id="rt" value="${t}"></div>
  ${body}</section>`}
function bindRange(){
 if(!$('#rcard'))return;
 const redraw=()=>{$('#rcard').outerHTML=rangeCard();bindRange()};
 $('#rf').onchange=e=>{S.rf=e.target.value;redraw()};
 $('#rt').onchange=e=>{S.rt=e.target.value;redraw()};
 document.querySelectorAll('[data-p]').forEach(b=>b.onclick=()=>{
  const now=new Date(),p=b.dataset.p;
  if(p==='month'){S.rf=ymd(new Date(now.getFullYear(),now.getMonth(),1));S.rt=TODAY}
  else if(p==='year'){S.rf=now.getFullYear()+'-01-01';S.rt=TODAY}
  else{const s=new Date(now);s.setDate(now.getDate()-29);S.rf=ymd(s);S.rt=TODAY}
  redraw()})}

/* ---------- Tổng kết (theo tháng) ---------- */
const PALETTE=['#0a84ff','#ff9f0a','#ff375f','#30d158','#bf5af2','#64d2ff','#ffd60a','#ac8e68','#ff453a','#8e8e93'];
function shiftMonth(n){const [Y,M]=S.smo.split('-').map(Number),d=new Date(Y,M-1+n,1);S.smo=d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')}
function catColorOf(c){if(c.color)return c.color;const outs=V().cats.filter(z=>z.kind==='out'),i=outs.findIndex(z=>z.id===c.id);return i>=0?PALETTE[i%PALETTE.length]:'#8e8e93'}
function catColorMap(){const m={};V().cats.forEach(c=>m[c.id]=catColorOf(c));return m}
function catBreakdown(ym){
 const X=V(),cm=catColorMap(),map=new Map();
 X.tx.filter(x=>x.kind==='out'&&x.d.startsWith(ym)).forEach(x=>{const id=x.cat||0;map.set(id,(map.get(id)||0)+x.amt)});
 return [...map.entries()].map(([id,v])=>{const c=X.cats.find(z=>z.id===id);return {id,v,l:c?c.name:'Chưa phân loại',c:(id&&cm[id])||'#8e8e93'}}).sort((a,b)=>b.v-a.v)}
function pieChart(parts){
 const tot=parts.reduce((s,p)=>s+p.v,0);
 if(!tot)return '<div class="empty">Chưa có chi tiêu trong tháng</div>';
 let a0=-Math.PI/2,svg='';
 if(parts.length===1)svg=`<circle cx="100" cy="100" r="92" fill="${parts[0].c}" data-tip="${esc('<b>'+esc(parts[0].l)+'</b><div>'+fm(parts[0].v)+' · 100% tổng chi</div>')}"/>`;
 else parts.forEach(p=>{const a1=a0+p.v/tot*2*Math.PI,x0=100+92*Math.cos(a0),y0=100+92*Math.sin(a0),x1=100+92*Math.cos(a1),y1=100+92*Math.sin(a1),lg=(a1-a0)>Math.PI?1:0;
  svg+=`<path d="M100 100L${x0.toFixed(2)} ${y0.toFixed(2)}A92 92 0 ${lg} 1 ${x1.toFixed(2)} ${y1.toFixed(2)}Z" fill="${p.c}" data-tip="${esc('<b>'+esc(p.l)+'</b><div>'+fm(p.v)+' · '+Math.round(p.v/tot*100)+'% tổng chi</div>')}"></path>`;a0=a1});
 return `<svg class="pie" viewBox="0 0 200 200" role="img" aria-label="Biểu đồ chi theo danh mục">${svg}<circle cx="100" cy="100" r="50" fill="var(--card)"/><text x="100" y="94" text-anchor="middle" class="pt1">Tổng chi</text><text x="100" y="116" text-anchor="middle" class="pt2">${shortN(tot)}</text></svg>`}

function sumView(){tipHide();const X=V(),ym=S.smo,[Y,M]=ym.split('-').map(Number),n=new Date(Y,M,0).getDate(),rows=[];
for(let i=1;i<=n;i++){const d=ym+'-'+String(i).padStart(2,'0'),e=X.tx.filter(x=>x.d===d);rows.push({d,l:String(i),a:sum(e,'in'),b:sum(e,'out')})}
const a=rows.reduce((s,x)=>s+x.a,0),b=rows.reduce((s,x)=>s+x.b,0),act=rows.filter(x=>x.a||x.b).length;
const dl=dayLim(),ml=monLim(ym),overDays=rows.filter(x=>{const L=effDay(x.d);return L!==null&&x.b>L}).length;
const lines=rows.filter(x=>x.a||x.b).map(x=>`<div class="li"><span>${vd(x.d)}</span><span class="lv"><span class="in">+${num(x.a)}</span><span class="out">−${num(x.b)}</span></span></div>`).join('')||'<div class="empty">Chưa có giao dịch trong tháng</div>';
const parts=catBreakdown(ym),ptot=parts.reduce((s,p)=>s+p.v,0),ro=RO();
const legend=parts.map(p=>`<div class="li"><span class="nm"><i class="sw" style="background:${p.c}"></i>${esc(p.l)}</span><span class="lv">${fm(p.v)}<small>${Math.round(p.v/ptot*100)}%</small></span></div>`).join('');
const manager=ro?'':`<section class="card"><div class="ch"><h3>Danh mục</h3></div>
 <div class="seg full" id="ck">${[['out','Chi tiêu'],['in','Thu nhập']].map(([v,l])=>`<button data-k="${v}" class="${S.catKind===v?'on':''}">${l}</button>`).join('')}</div>
 <div class="cats">${X.cats.filter(c=>c.kind===S.catKind).map(c=>`<span class="cchip static"><label class="cdot" style="background:${catColorOf(c)}"><input type="color" value="${catColorOf(c)}" data-col="${c.id}" aria-label="Đổi màu"></label>${esc(c.name)}<button class="cx" data-del="${c.id}" aria-label="Xoá">×</button></span>`).join('')||'<span class="sub">Chưa có danh mục</span>'}</div>
 <div class="addrow2"><input class="ic ic-cn" id="cn" maxlength="24" autocomplete="off" placeholder="Tên danh mục mới"><button class="btn pri" id="cadd">Thêm</button></div><div class="err" id="ce"></div></section>`;
$('#m').innerHTML=`<div class="phead"><div><small>Tổng kết</small><h2 class="ttl">Tháng ${M}, ${Y}</h2></div>
<div class="cnav"><button class="btn chev" id="mp" aria-label="Tháng trước">‹</button><input type="month" id="mo" value="${ym}"><button class="btn chev" id="mn" aria-label="Tháng sau">›</button></div></div>
<section class="rcard"><div class="rc-ico"><svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M7 3h7l5 5v13H7z"/><path d="M14 3v5h5M10 13h6M10 17h6"/></svg></div>
<div class="rc-txt"><small>Báo cáo tháng</small><h3>Báo cáo tài chính tháng ${M}/${Y}</h3><p>Xem trước và tải PDF: lịch thu chi, biểu đồ, hạn mức, đánh giá và phần ký tên.</p></div>
<button class="btn pri" id="prt">In báo cáo</button></section>
<div class="stats"><div class="card stat">${si('in')}<small>Tổng thu</small><b class="in">${fm(a)}</b></div><div class="card stat">${si('out')}<small>Tổng chi</small><b class="out">${fm(b)}</b></div><div class="card stat">${si('wallet')}<small>Còn lại</small><b>${fm(a-b)}</b></div></div>
${rangeCard()}
<section class="card"><div class="ch"><h3>Chi theo danh mục</h3><b class="out">${fm(ptot)}</b></div>${pieChart(parts)}${parts.length?`<div class="list">${legend}</div>`:''}</section>
<section class="card"><div class="ch"><h3>Biểu đồ thu theo ngày</h3><b class="in">${fm(a)}</b></div>${chart(rows,'a','in')}</section>
<section class="card"><div class="ch"><h3>Biểu đồ chi theo ngày</h3><b class="out">${fm(b)}</b></div>${chart(rows,'b','out',dl)}</section>
<section class="card"><div class="ch"><h3>Hạn mức tháng</h3><b class="${ml!==null&&b>ml?'out':''}">${ml!==null?fm(ml):'Chưa đặt'}</b></div>
${ml!==null?meter(b,ml)+`<div class="sub">${b>ml?'Vượt '+fm(b-ml):'Còn '+fm(ml-b)} · đã chi ${fm(b)}</div>`:'<div class="sub">Đặt hạn mức trong tab Lịch</div>'}
${dl!==null?`<div class="sub">Hạn mức mỗi ngày ${fm(dl)} · vượt ở ${overDays} ngày</div>`:''}</section>
<section class="card"><div class="ch"><h3>Các ngày có giao dịch</h3><small>${act} ngày</small></div><div class="list">${lines}</div></section>
${manager}`;
bindRange();
$('#mo').onchange=e=>{S.smo=e.target.value||S.smo;render()};
$('#mp').onclick=()=>{shiftMonth(-1);render()};
$('#mn').onclick=()=>{shiftMonth(1);render()};
$('#prt').onclick=printReport;
document.querySelectorAll('#ck button').forEach(b=>b.onclick=()=>{S.catKind=b.dataset.k;render()});
const cadd=$('#cadd');if(cadd)cadd.onclick=addCategory;
document.querySelectorAll('[data-del]').forEach(b=>b.onclick=()=>delCategory(b.dataset.del));
document.querySelectorAll('[data-col]').forEach(i=>i.onchange=async()=>{try{await api('/api/cats/'+i.dataset.col,'POST',{color:i.value});await load();toast('Đã đổi màu danh mục')}catch(e){toast(e.message,true)}})}

/* ---------- Popup chi tiết khi rê chuột / chạm vào biểu đồ (phần tử có data-tip) ---------- */
const TIP=document.createElement('div');TIP.className='tip';TIP.setAttribute('role','tooltip');document.body.append(TIP);
let TIPEL=null,TIPT=null;
function tipPlace(x,y){const w=TIP.offsetWidth,h=TIP.offsetHeight;let l=x+16,t=y+16;
 if(l+w>innerWidth-8)l=x-w-16;if(t+h>innerHeight-8)t=y-h-16;
 TIP.style.left=Math.max(8,l)+'px';TIP.style.top=Math.max(8,t)+'px'}
function tipShow(el,x,y){clearTimeout(TIPT);TIPEL=el;TIP.innerHTML=el.dataset.tip;TIP.classList.add('show');tipPlace(x,y)}
function tipHide(){clearTimeout(TIPT);TIPEL=null;TIP.classList.remove('show')}
const tipTarget=e=>e.target&&e.target.closest?e.target.closest('[data-tip]'):null;
document.addEventListener('pointerover',e=>{if(e.pointerType!=='mouse')return;const el=tipTarget(e);if(el)tipShow(el,e.clientX,e.clientY);else if(TIPEL)tipHide()});
document.addEventListener('pointermove',e=>{if(TIPEL&&e.pointerType==='mouse')tipPlace(e.clientX,e.clientY)});
document.addEventListener('pointerdown',e=>{if(e.pointerType==='mouse')return;const el=tipTarget(e);
 if(el){tipShow(el,e.clientX,e.clientY);TIPT=setTimeout(tipHide,2800)}else tipHide()});
addEventListener('scroll',()=>{if(TIPEL)tipHide()},{passive:true});

/* ---------- Báo cáo tháng: xem trước, nhập tên, in / lưu PDF ---------- */
const rpPct=v=>Math.round(v)+'%';
function rpMonth(ym){
 const X=V(),[Y,M]=ym.split('-').map(Number),n=new Date(Y,M,0).getDate(),off=(new Date(Y,M-1,1).getDay()+6)%7;
 const days=[];
 for(let i=1;i<=n;i++){const d=ym+'-'+String(i).padStart(2,'0'),e=X.tx.filter(x=>x.d===d);
  days.push({d,i,a:sum(e,'in'),b:sum(e,'out'),w:new Date(Y,M-1,i).getDay(),L:effDay(d)})}
 const inM=X.tx.filter(x=>x.d.startsWith(ym)),a=sum(inM,'in'),b=sum(inM,'out');
 const ml=monLim(ym),dl=dayLim();
 const limited=days.filter(x=>x.L!==null),overs=limited.filter(x=>x.b>x.L);
 const overAmt=overs.reduce((s,x)=>s+x.b-x.L,0);
 const worst=overs.slice().sort((p,q)=>(q.b-q.L)-(p.b-p.L))[0]||null;
 const pym=M===1?(Y-1)+'-12':Y+'-'+String(M-1).padStart(2,'0');
 const prevB=sum(X.tx.filter(x=>x.d.startsWith(pym)),'out');
 const svM=X.sv.filter(x=>x.d.startsWith(ym));
 const svIn=sum(svM,'dep'),svOut=sum(svM,'wd');
 const parts=catBreakdown(ym),ptot=parts.reduce((s,p)=>s+p.v,0);
 const weeks=[];
 for(let s=1;s<=n;s+=7){const e=Math.min(n,s+6),seg=days.filter(x=>x.i>=s&&x.i<=e);
  weeks.push({l:s+'–'+e,a:seg.reduce((t,x)=>t+x.a,0),b:seg.reduce((t,x)=>t+x.b,0)})}
 return {Y,M,n,off,days,a,b,ml,dl,limited,overs,overAmt,worst,prevB,pym,svIn,svOut,parts,ptot,weeks};
}
function rpEval(D){
 const {a,b,ml,limited,overs,prevB,parts}=D,checks=[],notes=[];
 if(a||b)checks.push({k:'Cân đối thu chi',ok:a>=b,note:a>=b?'Thu nhiều hơn chi '+fm(a-b):'Chi nhiều hơn thu '+fm(b-a)});
 if(ml!==null)checks.push({k:'Trong hạn mức tháng',ok:b<=ml,note:b<=ml?'Còn '+fm(ml-b)+' so với hạn mức':'Vượt '+fm(b-ml)+' so với hạn mức'});
 if(limited.length)checks.push({k:'Kỷ luật theo ngày',ok:overs.length<=limited.length*0.2,note:overs.length+'/'+limited.length+' ngày vượt hạn mức (đạt khi dưới 20% số ngày)'});
 if(a>0)checks.push({k:'Giữ lại từ thu nhập',ok:(a-b)/a>=0.2,note:'Còn lại '+rpPct((a-b)/a*100)+' trên tổng thu (mục tiêu từ 20%)'});
 if(prevB>0)checks.push({k:'So với tháng trước',ok:b<=prevB*1.1,note:'Chi '+(b>=prevB?'tăng ':'giảm ')+rpPct(Math.abs(b-prevB)/prevB*100)+' so với tháng trước'});
 const ratio=checks.length?checks.filter(c=>c.ok).length/checks.length:null;
 const grade=ratio===null?'Chưa đủ dữ liệu':ratio>=0.8?'Tốt':ratio>=0.5?'Khá':'Cần cải thiện';
 if(parts.length&&b>0)notes.push('Danh mục chi nhiều nhất là '+esc(parts[0].l)+': '+fm(parts[0].v)+', chiếm '+rpPct(parts[0].v/b*100)+' tổng chi.');
 if(ml!==null&&b>ml)notes.push('Cần cắt khoảng '+fm(b-ml)+' để đưa chi tiêu về trong hạn mức'+(parts.length?', nên bắt đầu từ '+esc(parts[0].l):'')+'.');
 if(D.worst)notes.push('Ngày vượt hạn mức nặng nhất là '+vd(D.worst.d)+', vượt '+fm(D.worst.b-D.worst.L)+'. Tổng số tiền vượt trong tháng là '+fm(D.overAmt)+'.');
 if(a>0&&(a-b)/a<0.2)notes.push('Nên giữ lại ít nhất 20% thu nhập mỗi tháng; tháng này mới còn '+rpPct((a-b)/a*100)+'.');
 if(D.svIn>0)notes.push('Trong tháng đã gửi '+fm(D.svIn)+' vào heo đất'+(D.svOut?', rút '+fm(D.svOut):'')+'.');
 if(!notes.length)notes.push('Không có điểm cần cảnh báo trong tháng này.');
 return {checks,grade,notes};
}
const RP_PAL=['#1F3A5F','#3D5A80','#5E7FA6','#8DA6C4','#B9C8DA','#4B5563','#9CA3AF','#D1D5DB','#E5E7EB','#6B7280'];
function rpPie(parts,tot){
 let a0=-Math.PI/2,svg='';
 if(parts.length===1)svg=`<circle cx="100" cy="100" r="92" fill="${RP_PAL[0]}"/>`;
 else parts.forEach((p,i)=>{const c=RP_PAL[i%RP_PAL.length],a1=a0+p.v/tot*2*Math.PI,x0=100+92*Math.cos(a0),y0=100+92*Math.sin(a0),x1=100+92*Math.cos(a1),y1=100+92*Math.sin(a1),lg=(a1-a0)>Math.PI?1:0;
  svg+=`<path d="M100 100L${x0.toFixed(2)} ${y0.toFixed(2)}A92 92 0 ${lg} 1 ${x1.toFixed(2)} ${y1.toFixed(2)}Z" fill="${c}" stroke="#FFFFFF" stroke-width="1.5"/>`;a0=a1});
 return `<svg viewBox="0 0 200 200" width="190" height="190" role="img" aria-label="Biểu đồ tròn chi theo danh mục">${svg}</svg>`;
}
function rpCols(weeks){
 const W=560,H=220,pad=30,mx=Math.max(1,...weeks.flatMap(w=>[w.a,w.b])),gw=(W-pad*2)/weeks.length,bw=Math.min(34,gw*0.3);
 let s='';
 weeks.forEach((w,i)=>{const cx=pad+gw*i+gw/2;
  [[w.a,'#1F3A5F',-bw-2],[w.b,'#8DA6C4',2]].forEach(([v,c,dx])=>{const h=(H-56)*v/mx,y=H-30-h,x=cx+dx;
   s+=`<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${bw.toFixed(1)}" height="${h.toFixed(1)}" fill="${c}"/>`;
   if(v)s+=`<text x="${(x+bw/2).toFixed(1)}" y="${(y-5).toFixed(1)}" text-anchor="middle" font-size="11" fill="#111111">${esc(shortN(v))}</text>`});
  s+=`<text x="${cx.toFixed(1)}" y="${H-10}" text-anchor="middle" font-size="11" fill="#333333">Ngày ${w.l}</text>`});
 s+=`<line x1="${pad}" x2="${W-pad}" y1="${H-30}" y2="${H-30}" stroke="#333333" stroke-width="0.8"/>`;
 return `<svg viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="Biểu đồ cột thu chi theo tuần">${s}</svg>`;
}
const RP_CSS='@page{size:A4;margin:22mm 20mm 22mm 25mm;@bottom-right{content:"Trang " counter(page) " / " counter(pages);font-family:"Times New Roman",Times,serif;font-size:10pt;color:#333333}}'+
'*{box-sizing:border-box}body{margin:0;font-family:"Times New Roman",Times,serif;font-size:12pt;line-height:1.35;color:#111111;background:#FFFFFF}'+
'.hd{display:flex;justify-content:space-between;font-size:10.5pt;color:#1F3A5F;border-bottom:1.2px solid #1F3A5F;padding-bottom:4px;margin-bottom:6px}'+
'h1{font-size:15pt;text-align:center;text-transform:uppercase;color:#1F3A5F;margin:14px 0 4px;letter-spacing:.02em}'+
'.sub{text-align:center;font-style:italic;margin:0 0 12px}'+
'.meta{width:100%;border-collapse:collapse;font-size:11.5pt;margin-bottom:4px}.meta td{padding:2px 0;vertical-align:top}.meta td:first-child{width:170px;font-weight:bold}'+
'.sec{break-inside:avoid;margin-top:14px}'+
'h2{font-size:12pt;text-transform:uppercase;color:#1F3A5F;margin:0 0 6px;font-weight:bold}'+
'.note{font-style:italic;font-size:10.5pt;margin:4px 0 4px;color:#333333}'+
'table{width:100%;border-collapse:collapse;font-size:11pt}th,td{border:0.6px solid #8A94A3;padding:4px 7px;text-align:left;vertical-align:top}'+
'th{background:#E9EEF5;color:#1F3A5F;font-weight:bold}.r{text-align:right}'+
'tr.tot td{font-weight:bold;background:#F4F6F9}'+
'table.lay{border-collapse:collapse}table.lay>tbody>tr>td{border:0;padding:0 6px;vertical-align:middle}'+
'.dows,.cal{display:grid;grid-template-columns:repeat(7,minmax(0,1fr))}'+
'.dows div{text-align:center;font-weight:bold;font-size:10.5pt;color:#1F3A5F;background:#E9EEF5;border:0.6px solid #8A94A3;padding:3px 0}'+
'.c{border:0.6px solid #8A94A3;min-height:50px;padding:3px 4px;font-size:9pt;overflow:hidden}.c.e{border:0.6px solid #8A94A3}.c.wk{background:#FAFBFC}'+
'.c.ov{background:#E9EEF5;box-shadow:inset 0 0 0 1.4px #1F3A5F}'+
'.c .dn{font-weight:bold;font-size:10pt}.c .in{color:#1F3A5F}.c .out{color:#4B5563}'+
'.sw{display:inline-block;width:10px;height:10px;margin-right:6px;vertical-align:baseline;border:0.4px solid #8A94A3}'+
'ol{margin:6px 0 0 0;padding-left:22px}li{margin:3px 0}'+
'.sign{display:flex;align-items:flex-end;justify-content:space-between;break-inside:avoid;page-break-inside:avoid;margin-top:26px}.sign .box{width:55%;margin-left:auto;text-align:center}.stamp{width:128px;flex:none}.stamp svg{width:128px;height:128px;display:block}'+
'.sign p{margin:0}.sign .dt{font-style:italic}.sign .ti{font-weight:bold;text-transform:uppercase;margin-top:2px}.sign .it{font-style:italic;font-size:10.5pt}'+
'.sign .sp{height:66px}.sign .nm{font-weight:bold}'+
'.foot{margin-top:22px;padding-top:4px;border-top:0.6px solid #8A94A3;font-size:10pt;font-style:italic;color:#333333}'+
'svg text{font-family:"Times New Roman",Times,serif}'+
'@media print{body{margin:0}}';
const RP_JS="function p2(n){return String(n).padStart(2,'0')}"+
"function stamp(){var d=new Date();var t=p2(d.getDate())+'/'+p2(d.getMonth()+1)+'/'+d.getFullYear();var hms=p2(d.getHours())+':'+p2(d.getMinutes())+':'+p2(d.getSeconds());"+
"var a=document.getElementById('ptime');if(a)a.textContent=t+' '+hms;"+
"var b=document.getElementById('pdate');if(b)b.textContent='Ngày '+p2(d.getDate())+' tháng '+p2(d.getMonth()+1)+' năm '+d.getFullYear()}"+
"stamp();window.addEventListener('beforeprint',stamp);";
const RP_STAMP=`<svg viewBox="0 0 200 200" role="img" aria-label="Dấu mộc tròn Ban điều hành Financev"><defs><path id="rpArcT" d="M 32 100 A 68 68 0 0 1 168 100"/></defs>
<g fill="none" stroke="#1F3A5F" stroke-opacity=".92"><circle cx="100" cy="100" r="95" stroke-width="3.5"/><circle cx="100" cy="100" r="88" stroke-width="1.2"/><circle cx="100" cy="100" r="58" stroke-width="1.2"/></g>
<text font-family="Times New Roman,Times,serif" font-size="14" font-weight="700" fill="#1F3A5F" letter-spacing=".4"><textPath href="#rpArcT" startOffset="50%" text-anchor="middle">BAN ĐIỀU HÀNH FINANCEV</textPath></text>
<text x="100" y="95" text-anchor="middle" font-family="Times New Roman,Times,serif" font-size="19" font-weight="700" fill="#1F3A5F">FINANCEV</text>
<text x="100" y="116" text-anchor="middle" font-family="Times New Roman,Times,serif" font-size="12" font-weight="700" fill="#1F3A5F">2026 - VINCENT</text>
</svg>`;
function rpHTML(name){
 const D=rpMonth(S.smo),E=rpEval(D),{Y,M,n,off,days,a,b,ml,dl,limited,overs,parts,ptot,weeks,prevB,pym}=D;
 const mm=String(M).padStart(2,'0'),ch=prevB>0?(b-prevB)/prevB*100:null;
 let cal='';
 for(let i=0;i<off;i++)cal+='<div class="c e"></div>';
 days.forEach(x=>{const over=x.L!==null&&x.b>x.L,wk=x.w===0||x.w===6;
  cal+=`<div class="c${wk?' wk':''}${over?' ov':''}"><div class="dn">${x.i}</div>${x.a?`<div class="in">+${num(x.a)}</div>`:''}${x.b?`<div class="out">−${num(x.b)}</div>`:''}</div>`});
 const tail=(7-(off+n)%7)%7;
 for(let i=0;i<tail;i++)cal+='<div class="c e"></div>';
 const legendRows=parts.map((p,i)=>`<tr><td><i class="sw" style="background:${RP_PAL[i%RP_PAL.length]}"></i>${esc(p.l)}</td><td class="r">${num(p.v)}</td><td class="r">${rpPct(p.v/ptot*100)}</td></tr>`).join('');
 const wkRows=weeks.map(w=>`<tr><td>Ngày ${w.l}</td><td class="r">${num(w.a)}</td><td class="r">${num(w.b)}</td></tr>`).join('');
 const mLeft=ml===null?'—':b<=ml?'Còn '+num(ml-b):'Vượt '+num(b-ml);
 const html=`<!doctype html><html lang="vi"><head><meta charset="utf-8"><title>Bao-cao-tai-chinh-thang-${mm}-${Y}</title><style>${RP_CSS}</style></head><body>
<div class="hd"><span><b>FINANCEV</b></span><span>Ứng dụng quản lý tài chính của Vincent</span></div>
<h1>Báo cáo tài chính tháng ${mm} năm ${Y}</h1>
<p class="sub">Từ ngày 01/${mm}/${Y} đến ngày ${String(n).padStart(2,'0')}/${mm}/${Y}</p>
<table class="meta"><tr><td>Người lập báo cáo:</td><td>${name?esc(name):'.............................................'}</td></tr>
<tr><td>Đơn vị tiền tệ:</td><td>Đồng Việt Nam (VND)</td></tr></table>

<section class="sec"><h2>I. Tổng hợp thu chi</h2>
<p class="note">Đơn vị tính: đồng</p>
<table><thead><tr><th>Chỉ tiêu</th><th class="r">Số tiền</th></tr></thead><tbody>
<tr><td>Tổng thu trong tháng</td><td class="r">${num(a)}</td></tr>
<tr><td>Tổng chi trong tháng</td><td class="r">${num(b)}</td></tr>
<tr class="tot"><td>Chênh lệch (thu − chi)</td><td class="r">${num(a-b)}</td></tr>
<tr><td>Gửi vào heo đất trong tháng</td><td class="r">${num(D.svIn)}</td></tr>
<tr><td>Rút từ heo đất trong tháng</td><td class="r">${num(D.svOut)}</td></tr>
</tbody></table></section>

<section class="sec"><h2>II. Lịch thu chi theo ngày</h2>
<div class="dows">${['T2','T3','T4','T5','T6','T7','CN'].map(x=>`<div>${x}</div>`).join('')}</div>
<div class="cal">${cal}</div>
<p class="note">Ô được đóng khung đậm là ngày chi vượt hạn mức theo ngày. Số có dấu (+) là thu, dấu (−) là chi. Đơn vị tính: đồng.</p></section>

<section class="sec"><h2>III. Cơ cấu chi tiêu theo danh mục</h2>
<table class="lay"><tr><td style="width:46%">${parts.length?rpPie(parts,ptot):'<p>Không có dữ liệu chi tiêu.</p>'}</td>
<td><table><thead><tr><th>Danh mục</th><th class="r">Số tiền</th><th class="r">Tỷ lệ</th></tr></thead><tbody>${legendRows}<tr class="tot"><td>Tổng chi</td><td class="r">${num(ptot)}</td><td class="r">100%</td></tr></tbody></table></td></tr></table></section>

<section class="sec"><h2>IV. Thu chi theo tuần</h2>
${rpCols(weeks)}
<p class="note">Cột đậm là thu, cột nhạt là chi. Đơn vị tính: đồng.</p>
<table><thead><tr><th>Khoảng ngày</th><th class="r">Thu</th><th class="r">Chi</th></tr></thead><tbody>${wkRows}<tr class="tot"><td>Tổng cộng</td><td class="r">${num(a)}</td><td class="r">${num(b)}</td></tr></tbody></table></section>

<section class="sec"><h2>V. Hạn mức tài chính và đối sánh thực tế</h2>
<p class="note">A. Hạn mức chi tiêu tháng. Đơn vị tính: đồng.</p>
<table><thead><tr><th>Chỉ tiêu</th><th class="r">Hạn mức</th><th class="r">Thực tế</th><th class="r">Chênh lệch</th><th class="r">Tỷ lệ sử dụng</th></tr></thead><tbody>
<tr><td>Chi tiêu cả tháng</td><td class="r">${ml===null?'Chưa đặt':num(ml)}</td><td class="r">${num(b)}</td><td class="r">${mLeft}</td><td class="r">${ml?rpPct(b/ml*100):'—'}</td></tr></tbody></table>
<p class="note">B. Hạn mức theo ngày.</p>
<table><thead><tr><th>Chỉ tiêu</th><th class="r">Kết quả</th></tr></thead><tbody>
<tr><td>Hạn mức chi mỗi ngày</td><td class="r">${dl===null?'Chưa đặt':num(dl)}</td></tr>
<tr><td>Số ngày có hạn mức / số ngày vượt hạn mức</td><td class="r">${limited.length} / ${overs.length}</td></tr>
<tr><td>Tổng số tiền vượt hạn mức ngày</td><td class="r">${overs.length?num(D.overAmt):'Không có'}</td></tr>
<tr><td>Ngày vượt hạn mức nhiều nhất</td><td class="r">${D.worst?vd(D.worst.d)+' (vượt '+num(D.worst.b-D.worst.L)+')':'Không có'}</td></tr></tbody></table>
<p class="note">C. Đối sánh với tháng trước.</p>
<table><thead><tr><th>Chỉ tiêu</th><th class="r">Số tiền</th></tr></thead><tbody>
<tr><td>Tổng chi tháng ${pym.slice(5)}/${pym.slice(0,4)}</td><td class="r">${num(prevB)}</td></tr>
<tr><td>Tổng chi tháng ${mm}/${Y}</td><td class="r">${num(b)}</td></tr>
<tr class="tot"><td>Mức thay đổi</td><td class="r">${ch===null?'Không có dữ liệu':(ch>=0?'Tăng ':'Giảm ')+rpPct(Math.abs(ch))}</td></tr></tbody></table></section>

<section class="sec"><h2>VI. Đánh giá báo cáo tháng</h2>
<p>Xếp loại tổng thể: <b>${E.grade}</b></p>
<table><thead><tr><th>Tiêu chí</th><th>Kết quả</th><th>Nhận định</th></tr></thead><tbody>
${E.checks.map(c=>`<tr><td>${esc(c.k)}</td><td>${c.ok?'<b>Đạt</b>':'<b>Chưa đạt</b>'}</td><td>${esc(c.note)}</td></tr>`).join('')||'<tr><td colspan="3">Chưa đủ dữ liệu để đánh giá.</td></tr>'}
</tbody></table>
<p class="note" style="margin-top:10px;font-style:normal"><b>Nhận xét và kiến nghị:</b></p>
<ol>${E.notes.map(t=>`<li>${t}</li>`).join('')}</ol></section>

<section class="sign"><div class="stamp">${RP_STAMP}</div><div class="box"><p class="dt" id="pdate">Ngày ... tháng ... năm ...</p><p class="ti">Người lập báo cáo</p><p class="it">(Ký, ghi rõ họ tên)</p><div class="sp"></div><p class="nm">${name?esc(name):'&nbsp;'}</p></div></section>
<div class="foot">Thời gian in: <span id="ptime"></span> · Tài liệu được tạo từ ứng dụng Financev</div>
<script>${RP_JS}<\/script></body></html>`;
 return html.replace(/₫/g,'đ');
}
function printReport(){
 const ym=S.smo,[Y,M]=ym.split('-');
 const o=modal(`<h3 style="margin-bottom:10px">Xem trước báo cáo tháng ${M}/${Y}</h3>
 <iframe id="rpf" title="Xem trước báo cáo" style="display:block;width:100%;height:62vh;border:0;border-radius:12px;background:#fff"></iframe>
 <div style="margin-top:12px"><label style="display:block"><small>Họ tên người lập (hiển thị trên báo cáo)</small>
 <input class="ic ic-rpn" id="rpn" maxlength="40" autocomplete="off" placeholder="Nhập họ và tên" style="width:100%;margin-top:4px"></label></div>
 <p style="font-size:12px;color:var(--mut);margin:10px 0 0">Mẹo: trong hộp thoại in, bỏ chọn "Tiêu đề và chân trang" (Headers and footers) để báo cáo không in kèm ngày giờ và tên file.</p><div class="bar end"><button class="btn" id="rpc">Đóng</button><button class="btn pri" id="rpd">Tải PDF</button></div>`,true);
 o.firstElementChild.style.maxWidth='920px';
 const ifr=o.querySelector('#rpf'),inp=o.querySelector('#rpn');
 let tm=null;
 const paint=()=>{ifr.srcdoc=rpHTML(inp.value.trim())};
 inp.oninput=()=>{clearTimeout(tm);tm=setTimeout(paint,350)};
 paint();
 o.querySelector('#rpc').onclick=()=>close(o);
 o.querySelector('#rpd').onclick=()=>{try{ifr.contentWindow.focus();ifr.contentWindow.print()}catch(e){toast('Không mở được hộp thoại in. Hãy thử trình duyệt máy tính',true)}};
}

async function addCategory(){const n=$('#cn').value.trim();if(!n){$('#ce').textContent='Nhập tên danh mục';return}
try{await api('/api/cats','POST',{kind:S.catKind,name:n});await load();toast('Đã thêm danh mục')}catch(e){$('#ce').textContent=e.message}}
async function delCategory(id){if(!confirm('Xoá danh mục này? Các giao dịch trong danh mục sẽ chuyển sang "Chưa phân loại".'))return;
try{await api('/api/cats/'+id,'DELETE');await load();toast('Đã xoá danh mục')}catch(e){toast(e.message,true)}}

/* ---------- Tài khoản của bạn: đổi tên đăng nhập và mật khẩu ---------- */
function accView(){
 $('#m').innerHTML=`<div class="phead"><div><small>Tài khoản</small><h2 class="ttl">Tài khoản của bạn</h2></div></div>
<section class="card"><h3>Đổi tên đăng nhập và mật khẩu</h3>
<p class="hint">Đang đăng nhập: <b>${esc(S.user)}</b>. Để trống ô nào thì giữ nguyên thông tin đó. Bắt buộc nhập mật khẩu hiện tại để xác nhận.</p>
<div class="stack">
<label class="fld"><span>Tên đăng nhập mới</span><input class="ic ic-nn" id="nn" autocomplete="off" placeholder="${esc(S.user)}"></label>
<label class="fld"><span>Mật khẩu mới</span><input class="ic ic-np" id="np" type="password" autocomplete="new-password" placeholder="Tối thiểu 4 ký tự"></label>
<label class="fld"><span>Nhập lại mật khẩu mới</span><input class="ic ic-np2" id="np2" type="password" autocomplete="new-password"></label>
<label class="fld"><span>Mật khẩu hiện tại</span><input class="ic ic-op" id="op" type="password" autocomplete="current-password"></label></div>
<div class="err" id="ae"></div><div class="rowend"><button class="btn pri" id="asave">Lưu thay đổi</button></div></section><section class="card appinfo"><div class="ch"><h3>Thông tin ứng dụng</h3></div><div class="list"><div class="li"><span class="nm">Tên ứng dụng</span><span class="lv">Financev</span></div><div class="li"><span class="nm">Phiên bản hiện tại</span><span class="lv">6.0.0 Office</span></div><div class="li wrap"><span class="nm">Dev</span><span class="lv">Vincent (Pham Minh Tien)</span></div></div><div class="ch" style="margin-top:16px"><h3>Các phiên bản trước</h3></div><div class="list"><div class="li"><span class="nm">1.0.0</span><span class="lv">pre-launch</span></div><div class="li"><span class="nm">1.0.1</span><span class="lv">debug</span></div><div class="li"><span class="nm">1.0.2</span><span class="lv">official</span></div><div class="li"><span class="nm">2.0.0</span><span class="lv">renew</span></div><div class="li"><span class="nm">3.0.0</span><span class="lv">—</span></div><div class="li"><span class="nm">4.0.0</span><span class="lv">beta</span></div><div class="li"><span class="nm">4.0.0</span><span class="lv">release</span></div><div class="li"><span class="nm">5.0.0</span><span class="lv">spectator</span></div><div class="li"><span class="nm">6.0.0</span><span class="lv">Office <small>· hiện tại</small></span></div></div></section><section class="card appinfo"><div class="ch"><h3>Thông tin pháp lý</h3></div><div class="pro-line">Bản quyền: Copyright 2026 · Vincent (Pham Minh Tien). Mọi quyền đối với mã nguồn và giao diện thuộc về tác giả.</div><div class="pro-line">Dữ liệu cá nhân: các khoản thu, chi, hạn mức và danh mục do bạn nhập được lưu trên máy chủ chạy ứng dụng. Bạn có thể xoá từng giao dịch hoặc đổi danh mục bất cứ lúc nào.</div><div class="pro-line">Tài khoản: bạn chịu trách nhiệm bảo mật tên đăng nhập và mật khẩu. Nên đổi mật khẩu ngay sau lần đăng nhập đầu tiên.</div><div class="pro-line">Miễn trừ trách nhiệm: điểm sức khoẻ tài chính, phân tích và lời khuyên trong tab Phân tích chỉ mang tính tham khảo. Đây không phải tư vấn tài chính, pháp lý hay thuế. Bạn tự chịu trách nhiệm với các quyết định tài chính của mình.</div><div class="pro-line">Voucher PRO: mỗi tài khoản được nhận một lần, có hiệu lực 30 ngày kể từ lúc nhận. Voucher không được chuyển nhượng và không quy đổi thành tiền mặt.</div><div class="pro-line">Thay đổi dịch vụ: ứng dụng có thể được cập nhật, và các tính năng có thể thay đổi theo từng phiên bản.</div></section>`;
 $('#asave').onclick=saveAccount}
async function saveAccount(){const nn=$('#nn').value.trim(),np=$('#np').value,np2=$('#np2').value,op=$('#op').value;
if(!op){$('#ae').textContent='Nhập mật khẩu hiện tại để xác nhận';return}
if(!nn&&!np){$('#ae').textContent='Nhập tên đăng nhập mới hoặc mật khẩu mới';return}
if(np&&np!==np2){$('#ae').textContent='Mật khẩu nhập lại không khớp';return}
try{const r=await api('/api/account','POST',{pw:op,name:nn,new_pw:np});S.user=r.name;toast('Đã cập nhật tài khoản');render()}catch(e){$('#ae').textContent=e.message}}

/* ---------- Quản trị: danh sách người dùng ---------- */
async function admView(){
 $('#m').innerHTML='<div class="empty">Đang tải…</div>';
 try{const d=await api('/api/admin/users');
  const act=d.users.filter(u=>u.active).length;
  const rows=d.users.map(u=>`<div class="li row-click" data-id="${u.id}"><span class="nm">${esc(u.name)}<small> · tạo ${u.created?vd(u.created.slice(0,10)):'—'}</small></span><span class="lv"><span class="chip ${u.active?'ok':'off'}">${u.active?'Đang hoạt động':'Không hoạt động'}</span><small>${u.last_seen?'Lần cuối '+vd(u.last_seen.slice(0,10)):'Chưa đăng nhập'}</small></span></div>`).join('')||'<div class="empty">Chưa có người dùng nào</div>';
  $('#m').innerHTML=`<div class="phead"><div><small>Quản trị</small><h2 class="ttl">Người dùng</h2></div></div>
  <div class="stats"><div class="card stat">${si('users')}<small>Tổng tài khoản</small><b>${d.users.length}</b></div><div class="card stat">${si('check')}<small>Đang hoạt động</small><b class="in">${act}</b></div><div class="card stat">${si('minus')}<small>Không hoạt động</small><b>${d.users.length-act}</b></div></div>
  <section class="card"><div class="ch"><h3>Danh sách tài khoản</h3><small>Bấm vào tên để xem lịch (chỉ đọc)</small></div><div class="list">${rows}</div>
  <p class="hint">Hoạt động nghĩa là có đăng nhập trong ${d.active_days} ngày gần nhất.</p></section>`;
  document.querySelectorAll('.row-click').forEach(e=>e.onclick=()=>openUser(e.dataset.id));
 }catch(e){$('#m').innerHTML=`<div class="err">${esc(e.message)}</div>`}
}
async function openUser(id){try{const d=await api('/api/admin/user/'+id);S.peek=d;S.tab='cal';S.view='month';S.sel=TODAY;render(1)}catch(e){toast(e.message,true)}}

/* ---------- Phân tích PRO: chấm điểm nghiêm ngặt, phân tích chuyên sâu, lời khuyên ---------- */
const PRO_PRICE=199000;
const PRO_LOGO=`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><defs><linearGradient id="pgG" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ffe27a"/><stop offset="1" stop-color="#d69a1c"/></linearGradient></defs><rect width="64" height="64" rx="15" fill="#1c1c1e"/><path d="M11 42 L7 20 L22 31 L32 14 L42 31 L57 20 L53 42 Z" fill="url(#pgG)"/><rect x="11" y="45" width="42" height="7" rx="3.5" fill="url(#pgG)"/><circle cx="7" cy="18" r="3.2" fill="#ffe27a"/><circle cx="32" cy="12" r="3.2" fill="#ffe27a"/><circle cx="57" cy="18" r="3.2" fill="#ffe27a"/></svg>`;
const TICKET_ICON=`<svg viewBox="0 0 24 24" width="34" height="34" aria-hidden="true"><path d="M3 8a2 2 0 0 0 0 4v4h18v-4a2 2 0 0 0 0-4V4H3z" fill="#1c1c1e"/></svg>`;
const DOW=['Chủ nhật','Thứ hai','Thứ ba','Thứ tư','Thứ năm','Thứ sáu','Thứ bảy'];
const r1k=v=>Math.round(v/1000)*1000;
const rnd=v=>Math.round(v);
const pct=p=>Math.round(p*100);
const avgOf=a=>a.length?a.reduce((s,v)=>s+v,0)/a.length:null;
const clamp=(v,lo=0,hi=1)=>Math.min(hi,Math.max(lo,v));
function proActive(){return !!(S.pro&&S.pro.active)}

/* Trạng thái tháng: đã qua, đang diễn ra (tính đến hôm nay), hoặc sắp tới */
function monthState(ym){
 const [Y,M]=ym.split('-').map(Number),n=new Date(Y,M,0).getDate(),cur=TODAY.slice(0,7);
 if(ym<cur)return {Y,M,n,elapsed:n,left:0,status:'past'};
 if(ym>cur)return {Y,M,n,elapsed:0,left:n,status:'future'};
 return {Y,M,n,elapsed:T.getDate(),left:n-T.getDate()+1,status:'now'};
}
const comfort=r=>r<0?'đã vượt ngân sách':r>=1?'thoải mái':r>=0.7?'vừa phải':r>=0.4?'khá căng':'rất căng';

function analyzePro(ym){
 const X=V(),st=monthState(ym),{Y,M,n,elapsed}=st;
 const outs=X.tx.filter(x=>x.kind==='out'&&x.d.startsWith(ym));
 const b=outs.reduce((s,x)=>s+x.amt,0);
 const ml=monLim(ym),dl=dayLim();
 const ref=dl!==null?dl:(ml!==null?ml/n:null);
 const byDay={};outs.forEach(x=>byDay[x.d]=(byDay[x.d]||0)+x.amt);

 /* Từng ngày đã qua: chi bao nhiêu, hạn mức ngày là bao nhiêu, vượt bao nhiêu */
 const days=[];
 for(let i=1;i<=elapsed;i++){
  const d=ym+'-'+String(i).padStart(2,'0'),v=byDay[d]||0,L=effDay(d),ex=L!==null?Math.max(0,v-L):0;
  days.push({d,i,v,L,ex,pct:L?ex/L:0,w:new Date(Y,M-1,i).getDay()});
 }
 const limited=days.filter(x=>x.L!==null);
 const overs=limited.filter(x=>x.ex>0);
 /* Điểm ngày = 1 nếu không vượt; vượt bao nhiêu % hạn mức ngày thì trừ bấy nhiêu điểm ngày đó (vượt từ 100% trở lên là 0) */
 const disc=limited.length?avgOf(limited.map(x=>x.ex===0?1:Math.max(0,1-x.ex/x.L))):null;
 const small=overs.filter(x=>x.pct<=0.1),mid=overs.filter(x=>x.pct>0.1&&x.pct<=0.5),big=overs.filter(x=>x.pct>0.5);
 const totalEx=overs.reduce((s,x)=>s+x.ex,0);
 const worst=overs.slice().sort((p,q)=>q.ex-p.ex)[0]||null;

 /* Chuỗi vượt liên tiếp dài nhất */
 let best=0,bestS=null,bestE=null,cur=0,curS=null;
 days.forEach(x=>{if(x.ex>0){if(cur===0)curS=x.d;cur++;if(cur>best){best=cur;bestS=curS;bestE=x.d}}else cur=0});

 /* Sau một ngày vượt, hôm sau có trở lại trong hạn mức không */
 let recN=0,recOk=0;
 days.forEach((x,k)=>{if(x.ex>0&&k+1<days.length&&days[k+1].L!==null){recN++;if(days[k+1].ex===0)recOk++}});

 /* Độ đều chi tiêu: hệ số biến động (độ lệch chuẩn / trung bình) trên mọi ngày đã qua, kể cả ngày không chi */
 const vals=days.map(x=>x.v),mean=avgOf(vals)||0;
 const sd=mean?Math.sqrt(avgOf(vals.map(v=>(v-mean)**2))):0;
 const cv=mean?sd/mean:null;
 const zeroDays=vals.filter(v=>v===0).length;
 const maxDay=days.slice().sort((p,q)=>q.v-p.v)[0]||null;

 /* Cuối tuần và ngày thường */
 const wkAvg=avgOf(days.filter(x=>x.w===0||x.w===6).map(x=>x.v));
 const wdAvg=avgOf(days.filter(x=>x.w!==0&&x.w!==6).map(x=>x.v));
 const byW=Array.from({length:7},(_,w)=>avgOf(days.filter(x=>x.w===w).map(x=>x.v))||0);
 const overW=Array.from({length:7},(_,w)=>days.filter(x=>x.w===w&&x.ex>0).length);
 const topW=byW.indexOf(Math.max(...byW)),worstW=overW.indexOf(Math.max(...overW));

 /* Nửa đầu và nửa sau tháng đã qua */
 const h=Math.floor(elapsed/2);
 const h1=h?avgOf(days.slice(0,h).map(x=>x.v)):null,h2=h?avgOf(days.slice(h).map(x=>x.v)):null;

 /* Danh mục và giao dịch */
 const parts=catBreakdown(ym),top=parts[0]||null,share=top&&b?top.v/b:0;
 const catsOver40=parts.filter(p=>b&&p.v/b>0.4).length;
 const avgTx=outs.length?b/outs.length:0;
 const sortedTx=outs.slice().sort((p,q)=>q.amt-p.amt);
 const top3Share=b?sortedTx.slice(0,3).reduce((s,x)=>s+x.amt,0)/b:0;
 const bigTx=outs.filter(x=>avgTx&&x.amt>2*avgTx);

 /* Tháng trước để so sánh */
 const pym=M===1?(Y-1)+'-12':Y+'-'+String(M-1).padStart(2,'0');
 const prevB=sum(X.tx.filter(x=>x.d.startsWith(pym)),'out'),prevML=monLim(pym);

 /* Ngân sách, quỹ còn lại, tốc độ chi và dự báo */
 const left=ml!==null?ml-b:null;
 const allowance=ml!==null&&st.left>0?left/st.left:null;
 const pace=elapsed>0?b/elapsed:null;
 const proj=st.status==='now'&&pace!==null?pace*n:(st.status==='past'?b:null);

 /* Chấm điểm: trọng số 100 */
 const comp=[];
 if(ml!==null)comp.push({k:'Ngân sách tháng',w:30,s:b<=ml?1:clamp(1-(b-ml)/ml),
  note:b<=ml?`đã dùng ${pct(b/ml)}% hạn mức, còn ${num(ml-b)} đ`:`vượt ${num(b-ml)} đ (${pct((b-ml)/ml)}% hạn mức)`});
 if(disc!==null)comp.push({k:'Kỷ luật theo ngày',w:25,s:disc,
  note:`${overs.length}/${limited.length} ngày vượt: nhỏ ${small.length}, vừa ${mid.length}, lớn ${big.length}; tổng vượt ${num(totalEx)} đ`});
 if(allowance!==null&&ref)comp.push({k:'Quỹ còn lại mỗi ngày',w:20,s:clamp(allowance/ref),
  note:`còn ${num(rnd(left))} đ cho ${st.left} ngày = ${num(rnd(allowance))} đ/ngày, ${pct(allowance/ref)}% mức chuẩn ${num(rnd(ref))} đ (${comfort(allowance/ref)})`});
 if(cv!==null&&mean>0)comp.push({k:'Độ đều chi tiêu',w:15,s:clamp((2-cv)/1.5),
  note:`trung bình ${num(rnd(mean))} đ/ngày, hệ số biến động ${cv.toFixed(2)} (dưới 0,5 là khá đều)`});
 if(b>0){const s1=share<=0.5?1:clamp(1-(share-0.5)/0.5),s2=top3Share<=0.5?1:clamp(1-(top3Share-0.5)/0.5);
  comp.push({k:'Cân bằng chi tiêu',w:10,s:(s1+s2)/2,note:`${top?top.l:''} chiếm ${pct(share)}%; 3 khoản lớn nhất chiếm ${pct(top3Share)}% tổng chi`})}
 const W=comp.reduce((s,c)=>s+c.w,0);
 const score=comp.length?Math.round(comp.reduce((s,c)=>s+c.s*c.w,0)/W*100):null;

 return {Y,M,n,st,elapsed,status:st.status,moneyLeft:left,remDays:st.left,b,ml,dl,ref,allowance,pace,proj,
  days,limited,overs,small,mid,big,totalEx,worst,best,bestS,bestE,recN,recOk,
  mean,cv,zeroDays,maxDay,wkAvg,wdAvg,byW,overW,topW,worstW,h1,h2,
  parts,top,share,catsOver40,avgTx,sortedTx,top3Share,bigTx,big1:sortedTx[0]||null,
  pym,prevB,prevML,comp,score};
}

function proLines(A){
 const L=[];
 if(!A.elapsed){L.push('Tháng này chưa có ngày nào để phân tích.');return L}
 if(A.ml!==null&&A.proj!==null){
  L.push(A.status==='past'
   ?`Tháng đã chi ${num(rnd(A.b))} đ, ${A.b>A.ml?`vượt hạn mức ${num(rnd(A.b-A.ml))} đ`:`còn dư ${num(rnd(A.ml-A.b))} đ so với hạn mức`}.`
   :`Với nhịp chi trung bình ${num(rnd(A.pace))} đ mỗi ngày, cuối tháng ước tính chi ${num(rnd(A.proj))} đ, ${A.proj>A.ml?`vượt hạn mức ${num(rnd(A.proj-A.ml))} đ`:`còn dư ${num(rnd(A.ml-A.proj))} đ so với hạn mức`}.`);
 }
 if(A.limited.length){
  L.push(`Trong ${A.limited.length} ngày có hạn mức, ${A.overs.length} ngày vượt: ${A.small.length} ngày vượt nhỏ (dưới 10% hạn mức ngày), ${A.mid.length} ngày vượt vừa (10–50%), ${A.big.length} ngày vượt lớn (trên 50%).`);
 }else L.push('Chưa đặt hạn mức ngày nên chưa chấm được kỷ luật theo từng ngày.');
 if(A.worst)L.push(`Ngày vượt nặng nhất là ${vd(A.worst.d)}: chi ${num(A.worst.v)} đ, vượt ${num(A.worst.ex)} đ, tức ${pct(A.worst.pct)}% hạn mức ngày ${num(A.worst.L)} đ.`);
 if(A.best>=2)L.push(`Chuỗi vượt liên tiếp dài nhất là ${A.best} ngày, từ ${vd(A.bestS)} đến ${vd(A.bestE)}.`);
 if(A.recN)L.push(`Sau mỗi ngày vượt, hôm sau trở lại trong hạn mức ${A.recOk}/${A.recN} lần (${pct(A.recOk/A.recN)}%).`);
 if(A.allowance!==null&&A.ref)L.push(`Quỹ còn lại ${num(rnd(A.moneyLeft))} đ cho ${A.remDays} ngày tới, trung bình ${num(rnd(A.allowance))} đ mỗi ngày, bằng ${pct(A.allowance/A.ref)}% mức chuẩn ${num(rnd(A.ref))} đ: ${comfort(A.allowance/A.ref)}.`);
 if(A.elapsed>=6&&A.h1>0&&A.h2!==null){const ch=Math.round((A.h2-A.h1)/A.h1*100);L.push(`Nửa đầu tháng chi trung bình ${num(rnd(A.h1))} đ mỗi ngày, nửa sau ${num(rnd(A.h2))} đ mỗi ngày (${ch>=0?'tăng':'giảm'} ${Math.abs(ch)}%).`)}
 if(A.wkAvg!==null&&A.wdAvg!==null)L.push(`Cuối tuần chi trung bình ${num(rnd(A.wkAvg))} đ mỗi ngày, ngày thường ${num(rnd(A.wdAvg))} đ mỗi ngày${A.wkAvg>A.wdAvg*1.3?'. Cuối tuần chi nhiều hơn rõ rệt':''}.`);
 if(A.b>0&&A.overW[A.worstW]>0)L.push(A.topW===A.worstW?`${DOW[A.topW]} vừa là ngày chi trung bình nhiều nhất (${num(rnd(A.byW[A.topW]))} đ), vừa là ngày vượt hạn mức nhiều nhất (${A.overW[A.worstW]} lần).`:`${DOW[A.topW]} là ngày chi trung bình nhiều nhất (${num(rnd(A.byW[A.topW]))} đ), còn ${DOW[A.worstW]} là ngày vượt hạn mức nhiều nhất (${A.overW[A.worstW]} lần).`);
 if(A.mean>0)L.push(`Có ${A.zeroDays} ngày không chi tiêu trên ${A.elapsed} ngày đã qua. Ngày chi nhiều nhất là ${vd(A.maxDay.d)} với ${num(A.maxDay.v)} đ, gấp ${(A.maxDay.v/A.mean).toFixed(1)} lần mức trung bình.`);
 if(A.top){
  L.push(`Ba danh mục chi nhiều nhất: ${A.parts.slice(0,3).map(p=>`${esc(p.l)} ${pct(p.v/A.b)}%`).join(', ')}${A.catsOver40>1?`; có ${A.catsOver40} danh mục chiếm trên 40% chi tiêu`:''}.`);
 }
 if(A.sortedTx.length){
  L.push(`${A.sortedTx.length} giao dịch chi, trung bình ${num(rnd(A.avgTx))} đ. ${A.bigTx.length} giao dịch vượt gấp đôi mức trung bình. Ba khoản lớn nhất chiếm ${pct(A.top3Share)}% tổng chi.`);
 }
 if(A.big1)L.push(`Khoản chi lớn nhất là ${num(A.big1.amt)} đ vào ${vd(A.big1.d)}${A.big1.note?' ('+esc(A.big1.note)+')':''}.`);
 if(A.prevB>0){const ch=Math.round((A.b-A.prevB)/A.prevB*100);L.push(`So với tháng ${A.pym}, tổng chi ${ch>=0?'tăng':'giảm'} ${Math.abs(ch)}%${A.prevML!==null?`; tháng đó ${A.prevB>A.prevML?'vượt':'nằm trong'} hạn mức`:''}.`)}
 return L;
}

function proAdvice(A){
 const T=[];
 if(!A.elapsed){T.push('Chưa có dữ liệu để đưa ra lời khuyên.');return T}
 if(A.ml===null&&A.dl===null){T.push('Hãy đặt hạn mức tháng và hạn mức ngày trong tab Lịch. Khi có hạn mức, lời khuyên sẽ đưa ra số tiền cụ thể cho từng ngày.');return T}
 if(A.status!=='past'){
  if(A.allowance!==null&&A.allowance>0)T.push(`Từ nay đến hết tháng, mức chi an toàn là khoảng ${num(r1k(A.allowance))} đ mỗi ngày. Giữ đúng mức này thì cuối tháng không vượt hạn mức.`);
  else if(A.allowance!==null)T.push(`Đã vượt ngân sách tháng ${num(rnd(-A.moneyLeft))} đ. Từ nay nên hạn chế các khoản không thiết yếu.`);
  else if(A.dl!==null)T.push(`Giữ mỗi ngày không vượt ${num(A.dl)} đ.`);
 }
 const w=A.comp.length?A.comp.slice().sort((p,q)=>p.s-q.s)[0]:null;
 if(w)T.push(`Điểm yếu thấp nhất hiện tại là "${w.k}" (${Math.round(w.s*100)}%). Đây là mục nên ưu tiên cải thiện trước.`);
 if(w&&w.k==='Ngân sách tháng'&&A.ml!==null&&A.b>A.ml)T.push(`Cần cắt ít nhất ${num(rnd(A.b-A.ml))} đ trong những ngày còn lại. Bắt đầu từ ${A.top?esc(A.top.l):'danh mục chi lớn nhất'}.`);
 if(w&&w.k==='Kỷ luật theo ngày'&&A.overW[A.worstW]>0)T.push(`Hãy đặt mức chi riêng cho ${DOW[A.worstW]}, ngày vượt hạn mức nhiều nhất (${A.overW[A.worstW]} lần). Các ngày vượt nhỏ dễ sửa hơn nên làm trước.`);
 if(w&&w.k==='Quỹ còn lại mỗi ngày')T.push('Quỹ mỗi ngày đang căng. Giảm các khoản chi lặt vặt thường xuyên sẽ nhanh tạo quỹ hơn là cắt một khoản lớn.');
 if(w&&w.k==='Độ đều chi tiêu'&&A.maxDay&&A.mean>0)T.push(`Chi tiêu đang dồn vào vài ngày. Ngày ${vd(A.maxDay.d)} chi gấp ${(A.maxDay.v/A.mean).toFixed(1)} lần mức trung bình, nên tách các khoản lớn thành nhiều ngày.`);
 if(A.top&&A.share>0.4)T.push(`${esc(A.top.l)} đang chiếm ${pct(A.share)}% chi tiêu. Giảm khoản này 10% sẽ giải phóng khoảng ${num(r1k(A.top.v*0.1))} đ mỗi tháng.`);
 if(A.big1&&A.bigTx.length)T.push(`Xem lại khoản ${num(A.big1.amt)} đ ngày ${vd(A.big1.d)}: đây là khoản lớn nhất tháng, nếu là chi bất thường thì không nên lặp lại.`);
 return T;
}

function showVoucher(){
 const o=modal(`<div class="pro-center"><div class="pro-badge">${PRO_LOGO}</div><h3>Bạn nhận Voucher giảm giá</h3>
 <div class="ticket"><div class="tk-main"><small>Phân tích PRO</small><b>Dùng miễn phí 30 ngày</b></div><div class="tk-cut"></div><div class="tk-side">${TICKET_ICON}</div></div>
 <div class="prices"><div><small>Giá gốc</small><s>${num(PRO_PRICE)} đ</s></div><div><small>Giá hiện tại</small><b class="in">0 đ</b></div></div>
 <p class="hint">Mỗi tài khoản chỉ nhận một lần. Thời hạn 30 ngày kể từ lúc nhận.</p>
 <div class="bar end"><button class="btn" id="vx">Để sau</button><button class="btn pri" id="vok">Nhận voucher</button></div></div>`);
 o.querySelector('#vx').onclick=()=>close(o);
 o.querySelector('#vok').onclick=async()=>{try{await api('/api/pro/claim','POST');close(o);toast('Đã nhận voucher PRO');await load()}catch(e){toast(e.message,true)}};
}

function proLocked(P){
 const btn=P.claimed?'<button class="btn pri wide" disabled>Gói PRO · thanh toán sẽ có sau</button>':'<button class="btn pri wide" id="pv">Nhận voucher</button>';
 $('#m').innerHTML=`<div class="phead"><div><small>Phân tích <span class="protag">PRO</span></small><h2 class="ttl">Phân tích PRO</h2></div></div>
 <section class="card pro-center"><div class="pro-badge">${PRO_LOGO}</div><h3>Mở khoá phân tích chuyên sâu</h3>
 <p class="hint">Điểm sức khoẻ tài chính, phân tích chi tiêu theo ngày và tháng, cùng lời khuyên nên tiêu bao nhiêu mỗi ngày.</p>
 ${P.claimed?`<p class="sub">Voucher đã hết hạn${P.until?' từ '+vd(P.until.slice(0,10)):''}.</p>`:''}
 <div class="prices pro-prices"><div><small>Giá gốc</small><s>${num(PRO_PRICE)} đ</s></div><div><small>Giá hiện tại</small><b>${P.claimed?num(PRO_PRICE)+' đ':'0 đ'}</b></div></div>
 ${btn}</section>`;
 const pv=$('#pv');if(pv)pv.onclick=showVoucher;
}

function proView(){
 const P=S.pro||{claimed:false,active:false};
 if(!P.claimed&&!S.voucherSeen){S.voucherSeen=true;showVoucher()}
 if(!proActive()){proLocked(P);return}
 const A=analyzePro(S.smo);
 const ring=A.score===null?'':`<div class="ring"><svg viewBox="0 0 120 120"><circle cx="60" cy="60" r="50" fill="none" stroke="var(--fill)" stroke-width="10"/><circle cx="60" cy="60" r="50" fill="none" stroke="var(--accent)" stroke-width="10" stroke-linecap="round" stroke-dasharray="314.16" stroke-dashoffset="${(314.16*(1-A.score/100)).toFixed(2)}" transform="rotate(-90 60 60)"/></svg><div class="rv"><b>${A.score}</b><small>/ 100</small></div></div>`;
 const grade=A.score===null?'':A.score>=85?'Rất tốt':A.score>=70?'Ổn định':A.score>=50?'Cần chú ý':'Rủi ro cao';
 const comps=A.comp.map(c=>`<div class="li wrap"><span class="nm">${c.k} <small>(${c.w}%)</small><small class="nt"> · ${esc(c.note)}</small></span><span class="lv">${Math.round(c.s*100)}%</span></div>${meter(c.s*100,100)}`).join('')||'<div class="empty">Chưa có dữ liệu để chấm điểm</div>';
 $('#m').innerHTML=`<div class="phead"><div><small>Phân tích <span class="protag">PRO</span></small><h2 class="ttl">Sức khoẻ tài chính</h2></div>
 <div class="cnav"><button class="btn chev" id="pp" aria-label="Tháng trước">‹</button><input type="month" id="pm" value="${S.smo}"><button class="btn chev" id="pn" aria-label="Tháng sau">›</button></div></div>
 <section class="card pro-center"><div class="ch" style="justify-content:center"><h3>Tháng ${A.M}, ${A.Y}</h3></div>
 ${ring}<b class="grade">${grade||'Chưa đủ dữ liệu'}</b>
 <p class="hint">${A.score===null?'Chưa có ngày nào để chấm điểm.':'Mức trừ tỉ lệ với số tiền vượt so với hạn mức ngày: vượt 10% hạn mức ngày thì ngày đó mất 10% điểm, vượt từ 100% trở lên thì mất hết.'}</p>
 ${A.ml===null?'<div class="sub">Đặt hạn mức tháng trong tab Lịch để chấm đủ 4 tiêu chí.</div>':''}</section>
 <section class="card"><div class="ch"><h3>Chấm điểm chi tiết</h3></div><div class="list">${comps}</div></section>
 <section class="card"><div class="ch"><h3>Phân tích chuyên sâu</h3><small>Chỉ tham khảo</small></div>${proLines(A).map(t=>`<div class="pro-line">${t}</div>`).join('')}</section>
 <section class="card"><div class="ch"><h3>Lời khuyên cuối cùng</h3></div>${proAdvice(A).map(t=>`<div class="pro-line">${t}</div>`).join('')}<p class="hint">Nội dung chỉ mang tính tham khảo, không thay thế tư vấn tài chính chuyên nghiệp.</p></section>`;
 $('#pp').onclick=()=>{shiftMonth(-1);render()};
 $('#pn').onclick=()=>{shiftMonth(1);render()};
 $('#pm').onchange=e=>{S.smo=e.target.value||S.smo;render()};
}

/* ---------- Hoàn tác / làm lại: Ctrl+Z, Ctrl+Y (hoặc Ctrl+Shift+Z) ---------- */
const HIST={undo:[],redo:[]};
function pushHist(undo,redo){HIST.undo.push({undo,redo});if(HIST.undo.length>100)HIST.undo.shift();HIST.redo.length=0}
async function histRun(from,to,msg){const a=HIST[from].pop();if(!a)return;
 try{await (from==='undo'?a.undo:a.redo)();HIST[to].push(a);await load();paintDay();toast(msg)}catch(e){HIST[from].push(a);toast(e.message,true)}}
addEventListener('keydown',e=>{
 if(!(e.ctrlKey||e.metaKey)||e.altKey)return;
 const k=e.key.toLowerCase(),ae=document.activeElement;
 /* Đang gõ dở trong ô văn bản thì để trình duyệt tự hoàn tác từng ký tự */
 const typing=ae&&(ae.tagName==='TEXTAREA'||(ae.tagName==='INPUT'&&ae.type!=='color'&&ae.value.length>0));
 if(typing)return;
 if(k==='z'&&!e.shiftKey){if(HIST.undo.length){e.preventDefault();histRun('undo','redo','Đã hoàn tác')}}
 else if(k==='y'||(k==='z'&&e.shiftKey)){if(HIST.redo.length){e.preventDefault();histRun('redo','undo','Đã làm lại')}}
});

/* ---------- Tải dữ liệu & khởi động ---------- */
async function load(){const d=await api('/api/data');S.tx=d.tx;S.sv=d.sv;S.lim=d.lim;S.cats=d.cats;S.pro=d.pro||null;render()}
async function boot(){try{const m=await api('/api/me');if(!m.name)return authView();S.user=m.name;S.role=m.role;S.peek=null;HIST.undo.length=0;HIST.redo.length=0;
 if(m.role==='admin'){S.tab='adm';render(1)}else{S.tab='cal';await load()}}catch(e){authView()}}
const bootP=boot();
const waitMs=ms=>new Promise(r=>setTimeout(r,ms));
function hideSplash(){const s=$('#splash');if(!s)return;s.classList.add('hide');setTimeout(()=>s.remove(),600)}
if(SPLASH_FIRST){
 $('#updbtn').onclick=()=>{
  $('#updbtn').remove();
  $('#slt').textContent='Đang cập nhật…';
  $('#slb').style.display='block';
  Promise.all([bootP,waitMs(SPLASH_MS)]).then(()=>{try{localStorage.setItem('fv_ver',APP_VER)}catch(e){}hideSplash()});
 };
}else{
 Promise.all([bootP,waitMs(SPLASH_MS)]).then(hideSplash);
}
</script></body></html>"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
