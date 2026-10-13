import json
import logging
import os
import secrets
import sqlite3
import time
from functools import wraps
from urllib.parse import urlparse

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   session, url_for)
from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash

DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "portal.db")
os.makedirs(DATA_DIR, exist_ok=True)

log = logging.getLogger("portail")


def _load_secret_key():
    key = os.environ.get("SECRET_KEY")
    if key:
        return key
    path = os.path.join(DATA_DIR, "secret_key")
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
    key = secrets.token_hex(32)
    with open(path, "w") as f:
        f.write(key)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


app = Flask(__name__)
app.config.update(
    SECRET_KEY=_load_secret_key(),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Mettre COOKIE_SECURE=1 lorsque le portail est servi en HTTPS
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 8,
)

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
_pwd = os.environ.get("ADMIN_PASSWORD")
if not _pwd:
    _pwd = secrets.token_urlsafe(12)
    log.warning("ADMIN_PASSWORD non défini. Mot de passe temporaire: %s", _pwd)
    print(f"[portail] ADMIN_PASSWORD non défini -> mot de passe temporaire pour '{ADMIN_USER}': {_pwd}", flush=True)
ADMIN_HASH = generate_password_hash(_pwd)
PORTAL_NAME = os.environ.get("PORTAL_NAME", "BDUCI")

SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT DEFAULT '',
    display_order INTEGER NOT NULL DEFAULT 0,
    icon TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    url TEXT NOT NULL,
    category_id INTEGER NOT NULL REFERENCES categories(id),
    display_order INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'ok',
    status_message TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    actor TEXT NOT NULL,
    ip TEXT,
    action TEXT NOT NULL
);
"""

SEED = [
    ("Banque et opérations", "Core Banking et applications métiers", 1),
    ("Finance et comptabilité", "Outils financiers et comptables", 2),
    ("Ressources humaines", "Gestion du personnel et congés", 3),
    ("Conformité et risques", "KYC et outils de contrôle", 4),
    ("Reporting et statistiques", "Tableaux de bord et rapports", 5),
    ("Outils internes", "Applications développées par la banque", 6),
    ("Informatique", "Supervision et outils techniques", 7),
]


def connect():
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def init_db():
    db = connect()
    db.execute("PRAGMA journal_mode = WAL")
    db.executescript(SCHEMA)
    for table, column, ddl in [("categories", "icon", "TEXT DEFAULT ''"),
                               ("applications", "status", "TEXT NOT NULL DEFAULT 'ok'"),
                               ("applications", "status_message", "TEXT DEFAULT ''")]:
        if column not in [r[1] for r in db.execute(f"PRAGMA table_info({table})")]:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            db.commit()
    if db.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 0:
        db.executemany("INSERT INTO categories(name, description, display_order) VALUES (?,?,?)", SEED)
        db.commit()
    db.close()


init_db()


def get_db():
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def close_db(_):
    db = g.pop("db", None)
    if db:
        db.close()


def audit(action):
    db = get_db()
    db.execute("INSERT INTO audit_log(actor, ip, action) VALUES (?,?,?)",
               (session.get("user", "?"), request.remote_addr, action))
    db.commit()


# ---------- Sécurité : session, CSRF, anti brute-force ----------

def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


# Bibliothèque Lucide (licence ISC, voir static/LICENSE-lucide.txt) : {nom: [contenu svg, mots-clés]}
with open(os.path.join(os.path.dirname(__file__), "static", "icons.json"), encoding="utf-8") as _f:
    ICONS = json.load(_f)

ICON_KEYWORDS = [("banque", "landmark"), ("financ", "banknote"), ("compta", "calculator"),
                 ("ressource", "users"), ("rh", "users"), ("conform", "shield-check"),
                 ("risque", "triangle-alert"), ("report", "chart-column"), ("statist", "chart-column"),
                 ("inform", "server"), ("outil", "box")]


def icon(name, cls="h-5 w-5", key=None):
    """Icône monochrome (trait). `key` = choix explicite ; sinon déduit d'un mot-clé du nom."""
    if key not in ICONS:
        n = (name or "").lower()
        key = next((v for k, v in ICON_KEYWORDS if k in n), "layout-grid")
    return Markup(f'<svg class="{cls}" fill="none" stroke="currentColor" stroke-width="1.8" '
                  f'stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24">{ICONS[key][0]}</svg>')


app.jinja_env.globals["icon"] = icon
app.jinja_env.globals["csrf_token"] = csrf_token
app.jinja_env.globals["portal_name"] = PORTAL_NAME


@app.before_request
def check_csrf():
    if request.method == "POST":
        sent = request.form.get("_csrf", "")
        if not sent or not secrets.compare_digest(sent, session.get("csrf", "")):
            abort(400, "Jeton CSRF invalide")


@app.after_request
def headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    return resp


def admin_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not session.get("user"):
            return redirect(url_for("login", next=request.path))
        return f(*a, **kw)
    return wrapper


_attempts = {}  # ip -> (count, first_ts)


def too_many_attempts(ip):
    count, first = _attempts.get(ip, (0, time.time()))
    if time.time() - first > 300:
        _attempts.pop(ip, None)
        return False
    return count >= 5


STATUSES = {"ok": "Opérationnel", "degraded": "Défaillant", "unavailable": "Indisponible"}
app.jinja_env.globals["STATUSES"] = STATUSES


# ---------- Validation ----------

def valid_url(url):
    try:
        p = urlparse(url)
        return p.scheme in ("http", "https") and bool(p.netloc) and len(url) <= 2000
    except ValueError:
        return False


def to_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ---------- Portail public ----------

@app.route("/")
def index():
    db = get_db()
    cats = db.execute("SELECT * FROM categories ORDER BY display_order, name").fetchall()
    apps_ = db.execute(
        "SELECT * FROM applications WHERE is_active=1 ORDER BY display_order, name COLLATE NOCASE").fetchall()
    groups = []
    for c in cats:
        items = [a for a in apps_ if a["category_id"] == c["id"]]
        if items:
            groups.append({"cat": c, "apps": items})
    return render_template("index.html", groups=groups, total=len(apps_))


@app.route("/healthz")
def healthz():
    return "ok"


# ---------- Authentification ----------

@app.route("/admin/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        ip = request.remote_addr
        if too_many_attempts(ip):
            flash("Trop de tentatives. Réessayez dans quelques minutes.", "error")
            return render_template("login.html"), 429
        user = request.form.get("username", "")
        pwd = request.form.get("password", "")
        ok = secrets.compare_digest(user, ADMIN_USER) and check_password_hash(ADMIN_HASH, pwd)
        if ok:
            _attempts.pop(ip, None)
            session.clear()
            session.permanent = True
            session["user"] = user
            audit("Connexion")
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/admin") else url_for("admin_apps"))
        count, first = _attempts.get(ip, (0, time.time()))
        _attempts[ip] = (count + 1, first)
        log.warning("Échec de connexion depuis %s", ip)
        flash("Identifiants incorrects.", "error")
    return render_template("login.html")


@app.post("/admin/logout")
def logout():
    if session.get("user"):
        audit("Déconnexion")
    session.clear()
    return redirect(url_for("index"))


# ---------- Admin : applications ----------

PER_PAGE = 20
PER_PAGE_CHOICES = (10, 20, 50, 100)


def safe_next(default):
    """Retour vers la liste filtrée après une action (uniquement des chemins /admin internes)."""
    nxt = request.form.get("next") or request.args.get("next") or ""
    return nxt if nxt.startswith("/admin") and not nxt.startswith("//") else default


def page_numbers(page, pages):
    """1 … 4 5 [6] 7 8 … 20"""
    nums = sorted({1, pages, *range(max(1, page - 2), min(pages, page + 2) + 1)})
    out, prev = [], 0
    for n in nums:
        if n - prev > 1:
            out.append(None)
        out.append(n)
        prev = n
    return out


@app.route("/admin")
@admin_required
def admin_apps():
    db = get_db()
    q = request.args.get("q", "").strip()
    cat = to_int(request.args.get("cat"), 0)
    etat = request.args.get("etat", "")
    page = max(to_int(request.args.get("page"), 1), 1)
    per_page = to_int(request.args.get("n"), PER_PAGE)
    if per_page not in PER_PAGE_CHOICES:
        per_page = PER_PAGE

    where, params = [], []
    if q:
        like = f"%{q.replace('%', '').replace('_', '')}%"
        where.append("(a.name LIKE ? OR a.description LIKE ? OR a.url LIKE ?)")
        params += [like, like, like]
    if cat:
        where.append("a.category_id = ?")
        params.append(cat)
    if etat == "active":
        where.append("a.is_active = 1")
    elif etat == "inactive":
        where.append("a.is_active = 0")
    elif etat == "incident":
        where.append("a.status != 'ok'")
    clause = ("WHERE " + " AND ".join(where)) if where else ""

    total = db.execute(f"SELECT COUNT(*) FROM applications a {clause}", params).fetchone()[0]
    pages = max((total + per_page - 1) // per_page, 1)
    page = min(page, pages)
    rows = db.execute(
        f"""SELECT a.*, c.name AS category FROM applications a
            JOIN categories c ON c.id = a.category_id {clause}
            ORDER BY c.display_order, c.name, a.display_order, a.name COLLATE NOCASE
            LIMIT ? OFFSET ?""", params + [per_page, (page - 1) * per_page]).fetchall()
    cats = db.execute("SELECT id, name FROM categories ORDER BY display_order, name").fetchall()
    return render_template("admin_apps.html", apps=rows, cats=cats, active="apps",
                           q=q, cat=cat, etat=etat, page=page, pages=pages, total=total,
                           first=(page - 1) * per_page + 1 if total else 0,
                           last=min(page * per_page, total), page_nums=page_numbers(page, pages),
                           per_page=per_page, per_page_choices=PER_PAGE_CHOICES, default_per_page=PER_PAGE)


def read_app_form():
    f = request.form
    data = {
        "name": f.get("name", "").strip(),
        "description": f.get("description", "").strip(),
        "url": f.get("url", "").strip(),
        "category_id": to_int(f.get("category_id"), 0),
        "display_order": to_int(f.get("display_order"), 0),
        "is_active": 1 if f.get("is_active") else 0,
        "status": f.get("status", "ok") if f.get("status", "ok") in STATUSES else "ok",
        "status_message": f.get("status_message", "").strip(),
    }
    errors = []
    if not data["name"] or len(data["name"]) > 100:
        errors.append("Le nom est obligatoire (100 caractères max).")
    if len(data["description"]) > 300:
        errors.append("La description est limitée à 300 caractères.")
    if len(data["status_message"]) > 200:
        errors.append("Le message de statut est limité à 200 caractères.")
    if data["status"] == "ok":
        data["status_message"] = ""
    if not valid_url(data["url"]):
        errors.append("L'URL doit commencer par http:// ou https:// (ex. http://10.0.0.5:8080/app).")
    if not get_db().execute("SELECT 1 FROM categories WHERE id=?", (data["category_id"],)).fetchone():
        errors.append("Catégorie invalide.")
    return data, errors


def app_form(item, title):
    cats = get_db().execute("SELECT * FROM categories ORDER BY display_order, name").fetchall()
    return render_template("admin_app_form.html", item=item, cats=cats, title=title, active="apps")


@app.route("/admin/apps/new", methods=["GET", "POST"])
@admin_required
def app_new():
    if request.method == "POST":
        data, errors = read_app_form()
        if not errors:
            db = get_db()
            db.execute("""INSERT INTO applications(name, description, url, category_id, display_order, is_active, status, status_message)
                          VALUES (:name,:description,:url,:category_id,:display_order,:is_active,:status,:status_message)""", data)
            db.commit()
            audit(f"Application créée : {data['name']} ({data['url']})")
            flash("Application ajoutée.", "ok")
            return redirect(url_for("admin_apps"))
        for e in errors:
            flash(e, "error")
        return app_form(data, "Nouvelle application")
    return app_form({"is_active": 1, "display_order": 0, "status": "ok"}, "Nouvelle application")


@app.route("/admin/apps/<int:app_id>/edit", methods=["GET", "POST"])
@admin_required
def app_edit(app_id):
    db = get_db()
    row = db.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
    if not row:
        abort(404)
    if request.method == "POST":
        data, errors = read_app_form()
        if not errors:
            data["id"] = app_id
            db.execute("""UPDATE applications SET name=:name, description=:description, url=:url,
                          category_id=:category_id, display_order=:display_order, is_active=:is_active,
                          status=:status, status_message=:status_message
                          WHERE id=:id""", data)
            db.commit()
            audit(f"Application modifiée : {data['name']} (#{app_id}), statut : {STATUSES[data['status']]}")
            flash("Application mise à jour.", "ok")
            return redirect(safe_next(url_for("admin_apps")))
        for e in errors:
            flash(e, "error")
        return app_form(data, "Modifier l'application")
    return app_form(row, "Modifier l'application")


@app.post("/admin/apps/<int:app_id>/toggle")
@admin_required
def app_toggle(app_id):
    db = get_db()
    row = db.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
    if not row:
        abort(404)
    new = 0 if row["is_active"] else 1
    db.execute("UPDATE applications SET is_active=? WHERE id=?", (new, app_id))
    db.commit()
    audit(f"Application {'activée' if new else 'désactivée'} : {row['name']} (#{app_id})")
    return redirect(safe_next(url_for("admin_apps")))


@app.post("/admin/apps/<int:app_id>/delete")
@admin_required
def app_delete(app_id):
    db = get_db()
    row = db.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
    if not row:
        abort(404)
    db.execute("DELETE FROM applications WHERE id=?", (app_id,))
    db.commit()
    audit(f"Application supprimée : {row['name']} (#{app_id})")
    flash("Application supprimée.", "ok")
    return redirect(safe_next(url_for("admin_apps")))


# ---------- Admin : catégories ----------

def read_cat_form(cat_id=None):
    f = request.form
    data = {
        "name": f.get("name", "").strip(),
        "description": f.get("description", "").strip(),
        "display_order": to_int(f.get("display_order"), 0),
        "icon": f.get("icon", "") if f.get("icon", "") in ICONS else "",
    }
    errors = []
    if not data["name"] or len(data["name"]) > 100:
        errors.append("Le nom est obligatoire (100 caractères max).")
    if len(data["description"]) > 300:
        errors.append("La description est limitée à 300 caractères.")
    dup = get_db().execute("SELECT id FROM categories WHERE name=? COLLATE NOCASE", (data["name"],)).fetchone()
    if dup and dup["id"] != cat_id:
        errors.append("Une catégorie porte déjà ce nom.")
    return data, errors


@app.route("/admin/categories", methods=["GET", "POST"])
@admin_required
def admin_categories():
    db = get_db()
    if request.method == "POST":
        data, errors = read_cat_form()
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            db.execute("INSERT INTO categories(name, description, display_order, icon) VALUES (:name,:description,:display_order,:icon)", data)
            db.commit()
            audit(f"Catégorie créée : {data['name']}")
            flash("Catégorie créée.", "ok")
        return redirect(url_for("admin_categories"))
    rows = db.execute(
        """SELECT c.*, (SELECT COUNT(*) FROM applications a WHERE a.category_id=c.id) AS app_count
           FROM categories c ORDER BY display_order, name""").fetchall()
    return render_template("admin_categories.html", cats=rows, active="cats")


@app.route("/admin/categories/<int:cat_id>/edit", methods=["GET", "POST"])
@admin_required
def category_edit(cat_id):
    db = get_db()
    row = db.execute("SELECT * FROM categories WHERE id=?", (cat_id,)).fetchone()
    if not row:
        abort(404)
    if request.method == "POST":
        data, errors = read_cat_form(cat_id)
        if not errors:
            data["id"] = cat_id
            db.execute("UPDATE categories SET name=:name, description=:description, display_order=:display_order, icon=:icon WHERE id=:id", data)
            db.commit()
            audit(f"Catégorie modifiée : {data['name']} (#{cat_id})")
            flash("Catégorie mise à jour.", "ok")
            return redirect(url_for("admin_categories"))
        for e in errors:
            flash(e, "error")
        row = data
    return render_template("admin_category_form.html", item=row, active="cats")


@app.post("/admin/categories/<int:cat_id>/delete")
@admin_required
def category_delete(cat_id):
    db = get_db()
    row = db.execute("SELECT * FROM categories WHERE id=?", (cat_id,)).fetchone()
    if not row:
        abort(404)
    if db.execute("SELECT COUNT(*) FROM applications WHERE category_id=?", (cat_id,)).fetchone()[0]:
        flash("Impossible de supprimer : cette catégorie contient encore des applications.", "error")
    else:
        db.execute("DELETE FROM categories WHERE id=?", (cat_id,))
        db.commit()
        audit(f"Catégorie supprimée : {row['name']} (#{cat_id})")
        flash("Catégorie supprimée.", "ok")
    return redirect(url_for("admin_categories"))


# ---------- Admin : journal ----------

@app.route("/admin/logs")
@admin_required
def admin_logs():
    rows = get_db().execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 200").fetchall()
    return render_template("admin_logs.html", logs=rows, active="logs")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
