import os
import re
import secrets
import sqlite3
import subprocess
import threading
import time
import urllib.request
import json
import mimetypes
import hashlib
import shutil

from pathlib import Path
from urllib.parse import urlparse

from flask import (
    Flask,
    request,
    redirect,
    session,
    abort,
    send_file,
    render_template_string,
    flash,
)

from werkzeug.utils import secure_filename


# =========================================================
# PATHS
# =========================================================

BASE = Path(__file__).resolve().parent

UPLOADS = BASE / "uploads"
DB = BASE / "projects.db"

UPLOADS.mkdir(parents=True, exist_ok=True)


# =========================================================
# SETTINGS
# =========================================================

PASSWORD = os.environ.get("SITE_PASSWORD")

if not PASSWORD:
    raise RuntimeError(
        "SITE_PASSWORD ortam değişkeni bulunamadı!"
    )

# ngrok tarafından otomatik güncellenecek
PUBLIC = ""

NGROK = os.environ.get(
    "NGROK_PATH",
    r"C:\Users\uzide\AppData\Local\Microsoft\WindowsApps\ngrok.exe",
)


# =========================================================
# FLASK
# =========================================================

app = Flask(__name__)

app.secret_key = os.environ.get(
    "FLASK_SECRET_KEY",
    secrets.token_hex(32),
)


HASH_CACHE = {}
NGROK_PROCESS = None


# =========================================================
# DATABASE
# =========================================================

def db():
    connection = sqlite3.connect(DB)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def query(sql, *args):
    connection = db()
    result = connection.execute(sql, args).fetchall()
    connection.close()
    return result


def one(sql, *args):
    connection = db()
    result = connection.execute(sql, args).fetchone()
    connection.close()
    return result


def execute(sql, *args):
    connection = db()
    result = connection.execute(sql, args)
    connection.commit()
    last_id = result.lastrowid
    connection.close()
    return last_id


def init():
    connection = db()

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS projects(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS shares(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT UNIQUE NOT NULL,
            project_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(project_id)
                REFERENCES projects(id)
                ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS links(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            url TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(project_id)
                REFERENCES projects(id)
                ON DELETE CASCADE
        );
        """
    )

    columns = {
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info(projects)"
        )
    }

    if "description" not in columns:
        connection.execute(
            "ALTER TABLE projects "
            "ADD COLUMN description TEXT DEFAULT ''"
        )

    connection.commit()
    connection.close()


# =========================================================
# PROJECT HELPERS
# =========================================================

def proj(slug):
    return one(
        "SELECT * FROM projects WHERE slug=?",
        slug
    )


def folder(project_id):
    return UPLOADS / str(project_id)


def auth():
    return session.get("auth") is True


# =========================================================
# SECURITY / FILE HELPERS
# =========================================================

def safe(base, name):
    path = (base / name).resolve()

    try:
        path.relative_to(base.resolve())
    except ValueError:
        abort(404)

    return path


def size(number):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if number < 1024:
            return f"{number:.1f} {unit}"

        number /= 1024

    return f"{number:.1f} PB"


def valid_url(url):
    try:
        parsed = urlparse(url)

        return (
            parsed.scheme in ("http", "https")
            and parsed.netloc
            and not (
                PUBLIC
                and url.lower().startswith(PUBLIC.lower())
            )
        )

    except Exception:
        return False


def files(project_id):
    directory = folder(project_id)

    if not directory.exists():
        return []

    result = []

    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue

        result.append(
            {
                "path": path.relative_to(directory).as_posix(),
                "size": size(path.stat().st_size),
                "ext": path.suffix.lower(),
            }
        )

    return result


# =========================================================
# SHARING
# =========================================================

def share(token):
    return one(
        """
        SELECT
            s.*,
            p.slug,
            p.name,
            p.description
        FROM shares s
        JOIN projects p
            ON p.id=s.project_id
        WHERE s.token=?
        """,
        token,
    )


# =========================================================
# HASH / METADATA
# =========================================================

def sha(path):
    stat = path.stat()

    cache_key = (
        str(path),
        stat.st_size,
        stat.st_mtime_ns,
    )

    if cache_key in HASH_CACHE:
        return HASH_CACHE[cache_key]

    digest = hashlib.sha256()

    with open(path, "rb") as file:
        for chunk in iter(
            lambda: file.read(1024 * 1024),
            b"",
        ):
            digest.update(chunk)

    HASH_CACHE[cache_key] = digest.hexdigest()

    return HASH_CACHE[cache_key]


def metadata(path):
    stat = path.stat()

    result = {
        "name": path.name,
        "extension": path.suffix.lower(),
        "mime": (
            mimetypes.guess_type(path.name)[0]
            or "application/octet-stream"
        ),
        "size": stat.st_size,
        "size_human": size(stat.st_size),
        "sha256": sha(path),
    }

    try:
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=12,
        )

        if probe.returncode == 0:
            result["media"] = json.loads(
                probe.stdout
            )

    except Exception:
        pass

    return result


def serve(path):
    if not path.is_file():
        abort(404)

    return send_file(
        path,
        as_attachment=False,
        download_name=path.name,
        conditional=True,
        etag=True,
    )


# =========================================================
# CSS
# =========================================================

CSS = """
*{box-sizing:border-box}
body{
    margin:0;
    background:#09090b;
    color:#f4f4f5;
    font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif
}
a{
    color:inherit;
    text-decoration:none
}
.nav,main{
    width:min(980px,calc(100% - 32px));
    margin:auto
}
.nav{
    height:68px;
    display:flex;
    align-items:center;
    justify-content:space-between;
    border-bottom:1px solid #252529
}
.brand{
    font-size:18px;
    font-weight:750
}
.navlinks,.toolbar,.actions,.form-actions{
    display:flex;
    gap:8px;
    flex-wrap:wrap
}
.navlink{
    padding:8px 11px;
    border-radius:9px;
    color:#999
}
.navlink:hover{
    background:#17171a;
    color:#fff
}
main{
    padding:52px 0 80px
}
.eyebrow{
    color:#71717a;
    font-size:13px;
    font-weight:650;
    margin-bottom:10px
}
h1{
    font-size:clamp(32px,6vw,48px);
    line-height:1.05;
    letter-spacing:-2.3px;
    margin:0
}
.sub{
    color:#8b8b93;
    margin:11px 0 28px
}
.toolbar{
    margin:28px 0 34px
}
.btn,button{
    display:inline-flex;
    align-items:center;
    justify-content:center;
    gap:7px;
    min-height:40px;
    padding:9px 14px;
    border:1px solid #303035;
    border-radius:10px;
    background:#17171a;
    color:#f4f4f5;
    font:inherit;
    font-weight:550;
    cursor:pointer
}
.btn:hover,button:hover{
    background:#222226;
    border-color:#45454b
}
.primary{
    background:#f4f4f5;
    color:#101012;
    border-color:#f4f4f5
}
.primary:hover{
    background:#fff;
    border-color:#fff
}
.danger{
    color:#ff8585
}
.danger:hover{
    background:#241315;
    border-color:#542326
}
.list{
    border-top:1px solid #27272a
}
.item{
    display:flex;
    align-items:center;
    justify-content:space-between;
    gap:18px;
    padding:18px 2px;
    border-bottom:1px solid #27272a
}
.item:hover{
    background:#0f0f12
}
.itemmain{
    min-width:0
}
.itemtitle{
    font-weight:650;
    overflow:hidden;
    text-overflow:ellipsis;
    white-space:nowrap
}
.muted{
    color:#8b8b93
}
.card{
    background:#101012;
    border:1px solid #27272a;
    border-radius:14px;
    padding:22px;
    margin-top:22px
}
.cardtitle{
    font-weight:650
}
.empty{
    padding:28px 2px;
    color:#71717a
}
.dangerzone{
    border-color:#3a2022
}
input,textarea{
    width:100%;
    background:#111114;
    color:#f4f4f5;
    border:1px solid #303035;
    border-radius:10px;
    padding:12px;
    margin:7px 0 16px;
    font:inherit
}
textarea{
    min-height:105px;
    resize:vertical
}
input[type=file]{
    padding:9px
}
.urlbox{
    font:13px ui-monospace,Consolas,monospace
}
.flash{
    padding:11px 14px;
    background:#141416;
    border:1px solid #303035;
    border-radius:10px;
    margin-bottom:20px;
    color:#ddd
}
@media(max-width:650px){
    .nav{
        height:60px
    }

    .navlinks .navlink:first-child{
        display:none
    }

    main{
        padding-top:38px
    }

    .item{
        align-items:stretch;
        flex-direction:column
    }

    .actions{
        width:100%
    }

    .actions .btn,
    .actions button,
    .toolbar .btn{
        flex:1
    }
}
"""


# =========================================================
# PAGE HELPERS
# =========================================================

def page(title, body):
    return render_template_string(
        """
        <!doctype html>
        <html lang="tr">
        <meta charset="utf-8">
        <meta
            name="viewport"
            content="width=device-width,initial-scale=1"
        >
        <title>{{title}} — PlaceHolder</title>

        <style>
        """
        + CSS
        + """
        </style>

        <nav class="nav">
            <a class="brand" href="/">PlaceHolder</a>

            <div class="navlinks">
                <a class="navlink" href="/">Projeler</a>
                <a class="navlink" href="/logout">Çıkış</a>
            </div>
        </nav>

        <main>
            {% for message in get_flashed_messages() %}
                <div class="flash">{{message}}</div>
            {% endfor %}

            {{body|safe}}
        </main>
        </html>
        """,
        title=title,
        body=body,
    )


ICON = """
{{
    "🎵" if f.ext in [".mp3",".wav",".ogg",".flac"]
    else "🎬" if f.ext in [".mp4",".webm",".mov",".mkv"]
    else "🖼️" if f.ext in [".png",".jpg",".jpeg",".gif",".webp"]
    else "📦" if f.ext in [".zip",".rar",".7z"]
    else "📄"
}}
"""


# =========================================================
# AUTH
# =========================================================

PUBLIC_ROUTES = {
    "login",
    "public_share",
    "public_file",
    "public_info",
    "public_hash",
    "public_link",
}


@app.before_request
def protect():
    if (
        request.endpoint not in PUBLIC_ROUTES
        and not auth()
    ):
        return redirect("/login")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        password = request.form.get(
            "password",
            "",
        )

        if secrets.compare_digest(
            password,
            PASSWORD,
        ):
            session["auth"] = True
            return redirect("/")

        flash("Şifre yanlış.")

    return """
    <!doctype html>
    <html lang="tr">
    <meta
        name="viewport"
        content="width=device-width,initial-scale=1"
    >

    <title>Giriş — PlaceHolder</title>

    <style>
    """ + CSS + """

    .box{
        width:min(390px,calc(100% - 32px));
        margin:20vh auto
    }

    .loginbrand{
        font-size:25px;
        font-weight:750
    }

    .muted{
        color:#888;
        margin:7px 0 24px
    }

    .card{
        background:#101012;
        border:1px solid #27272a;
        border-radius:15px;
        padding:22px
    }

    input,button{
        width:100%;
        padding:12px;
        border-radius:10px;
        font:inherit
    }

    input{
        background:#111114;
        color:#fff;
        border:1px solid #303035;
        margin-bottom:10px
    }

    button{
        background:#f4f4f5;
        color:#111;
        border:0;
        font-weight:650
    }
    </style>

    <div class="box">
        <div class="loginbrand">PlaceHolder</div>

        <div class="muted">
            Devam etmek için giriş yap.
        </div>

        <div class="card">
            <form method="post">
                <input
                    name="password"
                    type="password"
                    placeholder="Şifre"
                    autofocus
                    required
                >

                <button>Devam et →</button>
            </form>
        </div>
    </div>
    """


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


# =========================================================
# HOME
# =========================================================

@app.route("/")
def home():
    projects = query(
        "SELECT * FROM projects "
        "ORDER BY created_at DESC"
    )

    return page(
        "Projeler",
        render_template_string(
            """
            <div class="eyebrow">PlaceHolder</div>

            <h1>Projelerin.</h1>

            <div class="sub">
                Dosyalarını ve bağlantılarını tek yerde yönet.
            </div>

            <div class="toolbar">
                <a class="btn primary" href="/project/new">
                    + Yeni proje
                </a>
            </div>

            <div class="list">
            {% for project in projects %}
                <div class="item">

                    <div class="itemmain">
                        <div class="itemtitle">
                            {{project.name}}
                        </div>

                        <div class="muted">
                            /p/{{project.slug}}
                        </div>
                    </div>

                    <div class="actions">
                        <a
                            class="btn"
                            href="/p/{{project.slug}}"
                        >
                            Aç →
                        </a>

                        <a
                            class="btn"
                            href="/p/{{project.slug}}/edit"
                        >
                            Düzenle
                        </a>
                    </div>
                </div>

            {% else %}

                <div class="empty">
                    Henüz proje yok.
                </div>

            {% endfor %}
            </div>
            """,
            projects=projects,
        ),
    )


# =========================================================
# NEW PROJECT
# =========================================================

@app.route("/project/new", methods=["GET", "POST"])
def new_project():
    if request.method == "POST":

        name = request.form.get(
            "name",
            "",
        ).strip()

        if not name:
            flash("Proje adı gerekli.")
            return redirect(request.url)

        raw_slug = (
            request.form.get(
                "slug",
                "",
            ).strip().lower()
            or name
        )

        slug = re.sub(
            "-+",
            "-",
            re.sub(
                r"[^a-z0-9ğüşıöç_-]+",
                "-",
                raw_slug,
            ),
        ).strip("-") or "project"

        if proj(slug):
            flash("Bu slug zaten kullanılıyor.")
            return redirect(request.url)

        project_id = execute(
            """
            INSERT INTO projects(
                slug,
                name,
                description
            )
            VALUES(?,?,?)
            """,
            slug,
            name,
            request.form.get(
                "description",
                "",
            ).strip(),
        )

        folder(project_id).mkdir(
            parents=True,
            exist_ok=True,
        )

        return redirect("/p/" + slug)

    return page(
        "Yeni proje",
        render_template_string(
            """
            <div class="eyebrow">
                Yeni proje
            </div>

            <h1>Proje oluştur.</h1>

            <div class="sub">
                Dosyalarını ve bağlantılarını
                burada toplayabilirsin.
            </div>

            <div class="card">
                <form method="post">

                    <input
                        name="name"
                        placeholder="Proje adı"
                        required
                    >

                    <input
                        name="slug"
                        placeholder="Link adı"
                    >

                    <textarea
                        name="description"
                        placeholder="Açıklama (isteğe bağlı)"
                    ></textarea>

                    <div class="form-actions">
                        <button class="primary">
                            Oluştur →
                        </button>

                        <a class="btn" href="/">
                            İptal
                        </a>
                    </div>

                </form>
            </div>
            """
        ),
    )


# =========================================================
# PROJECT
# =========================================================

@app.route("/p/<slug>")
def project_page(slug):
    project = proj(slug)

    if not project:
        abort(404)

    links = query(
        """
        SELECT *
        FROM links
        WHERE project_id=?
        ORDER BY id DESC
        """,
        project["id"],
    )

    share_row = one(
        """
        SELECT token
        FROM shares
        WHERE project_id=?
        LIMIT 1
        """,
        project["id"],
    )

    return page(
        project["name"],
        render_template_string(
            """
            <div class="eyebrow">Proje</div>

            <h1>{{project.name}}</h1>

            {% if project.description %}
                <div class="sub">
                    {{project.description}}
                </div>
            {% endif %}

            <div class="toolbar">

                <a
                    class="btn primary"
                    href="/p/{{project.slug}}/upload"
                >
                    + Dosya
                </a>

                <a
                    class="btn"
                    href="/p/{{project.slug}}/link"
                >
                    + Link
                </a>

                <a
                    class="btn"
                    href="/p/{{project.slug}}/edit"
                >
                    Düzenle
                </a>

            </div>

            <div class="list">

            {% for file in files %}

                <div class="item">

                    <div class="itemmain">

                        <div class="itemtitle">
                            """
            + ICON
            + """
                            {{file.path}}
                        </div>

                        <div class="muted">
                            {{file.size}}
                        </div>

                    </div>

                    <div class="actions">

                        <a
                            class="btn"
                            href="/p/{{project.slug}}/file/{{file.path}}"
                        >
                            Aç
                        </a>

                        <a
                            class="btn"
                            href="/p/{{project.slug}}/info/{{file.path}}"
                        >
                            Bilgi
                        </a>

                        <form
                            method="post"
                            action="/p/{{project.slug}}/delete-file"
                        >
                            <input
                                type="hidden"
                                name="filename"
                                value="{{file.path}}"
                            >

                            <button class="danger">
                                Sil
                            </button>
                        </form>

                    </div>
                </div>

            {% endfor %}


            {% for link in links %}

                <div class="item">

                    <div class="itemmain">

                        <div class="itemtitle">
                            🔗 {{link.title}}
                        </div>

                        <div class="muted">
                            {{link.url}}
                        </div>

                    </div>

                    <div class="actions">

                        <a
                            class="btn"
                            href="/go/{{link.id}}"
                        >
                            Aç
                        </a>

                        <form
                            method="post"
                            action="/p/{{project.slug}}/delete-link"
                        >
                            <input
                                type="hidden"
                                name="id"
                                value="{{link.id}}"
                            >

                            <button class="danger">
                                Sil
                            </button>
                        </form>

                    </div>
                </div>

            {% endfor %}


            {% if not files and not links %}

                <div class="empty">
                    Henüz dosya veya link yok.
                </div>

            {% endif %}

            </div>


            <div class="card">

                <div class="cardtitle">
                    Public paylaşım
                </div>

                <p class="muted">
                    Bu bağlantı şifre gerektirmez.
                </p>

                {% if share_row %}

                    <input
                        class="urlbox"
                        readonly
                        value="{{request.url_root.rstrip('/')}}/share/{{share_row.token}}"
                    >

                    <form
                        method="post"
                        action="/p/{{project.slug}}/share/delete"
                    >
                        <button class="danger">
                            Linki iptal et
                        </button>
                    </form>

                {% else %}

                    <form
                        method="post"
                        action="/p/{{project.slug}}/share"
                    >
                        <button class="primary">
                            Public link oluştur
                        </button>
                    </form>

                {% endif %}

            </div>
            """,
            project=project,
            files=files(project["id"]),
            links=links,
            share_row=share_row,
        ),
    )


# =========================================================
# PROJECT FILES
# =========================================================

@app.route("/p/<slug>/info/<path:name>")
def project_info(slug, name):
    project = proj(slug)

    if not project:
        abort(404)

    return metadata(
        safe(
            folder(project["id"]),
            name,
        )
    )


@app.route("/p/<slug>/file/<path:name>")
def project_file(slug, name):
    project = proj(slug)

    if not project:
        abort(404)

    return serve(
        safe(
            folder(project["id"]),
            name,
        )
    )


@app.route("/p/<slug>/hash/<path:name>")
def project_hash(slug, name):
    project = proj(slug)

    if not project:
        abort(404)

    return {
        "sha256": sha(
            safe(
                folder(project["id"]),
                name,
            )
        )
    }


@app.route(
    "/p/<slug>/upload",
    methods=["GET", "POST"],
)
def upload(slug):
    project = proj(slug)

    if not project:
        abort(404)

    directory = folder(project["id"])
    directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    if request.method == "POST":

        file = request.files.get("file")

        if not file or not file.filename:
            flash("Dosya seç.")
            return redirect(request.url)

        filename = secure_filename(
            Path(file.filename).name
        )

        if filename in ("", ".", ".."):
            flash("Geçersiz dosya adı.")
            return redirect(request.url)

        file.save(directory / filename)

        return redirect("/p/" + slug)

    return page(
        "Dosya yükle",
        render_template_string(
            """
            <div class="eyebrow">
                Dosya
            </div>

            <h1>Dosya yükle.</h1>

            <div class="sub">
                {{project.name}}
            </div>

            <div class="card">

                <form
                    method="post"
                    enctype="multipart/form-data"
                >

                    <input
                        type="file"
                        name="file"
                        required
                    >

                    <div class="form-actions">

                        <button class="primary">
                            Yükle ↑
                        </button>

                        <a
                            class="btn"
                            href="/p/{{project.slug}}"
                        >
                            İptal
                        </a>

                    </div>

                </form>

            </div>
            """,
            project=project,
        ),
    )


# =========================================================
# LINKS
# =========================================================

@app.route(
    "/p/<slug>/link",
    methods=["GET", "POST"],
)
def add_link(slug):
    project = proj(slug)

    if not project:
        abort(404)

    if request.method == "POST":

        title = request.form.get(
            "title",
            "",
        ).strip()

        url = request.form.get(
            "url",
            "",
        ).strip()

        if not title or not valid_url(url):
            flash(
                "Geçersiz veya PlaceHolder'a ait link."
            )

            return redirect(request.url)

        execute(
            """
            INSERT INTO links(
                project_id,
                title,
                url
            )
            VALUES(?,?,?)
            """,
            project["id"],
            title,
            url,
        )

        return redirect("/p/" + slug)

    return page(
        "Link ekle",
        render_template_string(
            """
            <div class="eyebrow">
                Bağlantı
            </div>

            <h1>Link ekle.</h1>

            <div class="sub">
                Harici bir web adresini
                projene ekle.
            </div>

            <div class="card">

                <form method="post">

                    <input
                        name="title"
                        placeholder="Link adı"
                        required
                    >

                    <input
                        name="url"
                        type="url"
                        placeholder="https://example.com/..."
                        required
                    >

                    <div class="form-actions">

                        <button class="primary">
                            Linki ekle →
                        </button>

                        <a
                            class="btn"
                            href="/p/{{project.slug}}"
                        >
                            İptal
                        </a>

                    </div>

                </form>

            </div>
            """,
            project=project,
        ),
    )


@app.route("/go/<int:link_id>")
def go(link_id):
    link = one(
        "SELECT url FROM links WHERE id=?",
        link_id,
    )

    if not link or not valid_url(link["url"]):
        abort(400)

    return redirect(link["url"])


# =========================================================
# DELETE
# =========================================================

@app.route(
    "/p/<slug>/delete-file",
    methods=["POST"],
)
def delete_file(slug):
    project = proj(slug)

    if not project:
        abort(404)

    path = safe(
        folder(project["id"]),
        request.form.get(
            "filename",
            "",
        ),
    )

    if path.is_file():
        path.unlink()

    return redirect("/p/" + slug)


@app.route(
    "/p/<slug>/delete-link",
    methods=["POST"],
)
def delete_link(slug):
    project = proj(slug)

    if not project:
        abort(404)

    execute(
        """
        DELETE FROM links
        WHERE id=?
        AND project_id=?
        """,
        request.form.get("id"),
        project["id"],
    )

    return redirect("/p/" + slug)


@app.route(
    "/p/<slug>/delete",
    methods=["POST"],
)
def delete_project(slug):
    project = proj(slug)

    if not project:
        abort(404)

    shutil.rmtree(
        folder(project["id"]),
        ignore_errors=True,
    )

    execute(
        "DELETE FROM shares WHERE project_id=?",
        project["id"],
    )

    execute(
        "DELETE FROM links WHERE project_id=?",
        project["id"],
    )

    execute(
        "DELETE FROM projects WHERE id=?",
        project["id"],
    )

    return redirect("/")


# =========================================================
# EDIT PROJECT
# =========================================================

@app.route(
    "/p/<slug>/edit",
    methods=["GET", "POST"],
)
def edit(slug):
    project = proj(slug)

    if not project:
        abort(404)

    if request.method == "POST":

        name = request.form.get(
            "name",
            "",
        ).strip()

        if not name:
            flash("Proje adı gerekli.")
            return redirect(request.url)

        execute(
            """
            UPDATE projects
            SET name=?, description=?
            WHERE id=?
            """,
            name,
            request.form.get(
                "description",
                "",
            ),
            project["id"],
        )

        return redirect("/p/" + slug)

    return page(
        "Düzenle",
        render_template_string(
            """
            <div class="eyebrow">
                Proje ayarları
            </div>

            <h1>{{project.name}}</h1>

            <div class="card">

                <form method="post">

                    <input
                        name="name"
                        value="{{project.name}}"
                        required
                    >

                    <textarea
                        name="description"
                    >{{project.description or ""}}</textarea>

                    <div class="form-actions">

                        <button class="primary">
                            Kaydet
                        </button>

                        <a
                            class="btn"
                            href="/p/{{project.slug}}"
                        >
                            İptal
                        </a>

                    </div>

                </form>

            </div>


            <div class="card dangerzone">

                <div class="cardtitle">
                    Tehlikeli bölge
                </div>

                <p class="muted">
                    Proje ve tüm dosyaları silinir.
                </p>

                <form
                    method="post"
                    action="/p/{{project.slug}}/delete"
                >
                    <button class="danger">
                        Projeyi sil
                    </button>
                </form>

            </div>
            """,
            project=project,
        ),
    )


# =========================================================
# PUBLIC SHARES
# =========================================================

@app.route(
    "/p/<slug>/share",
    methods=["POST"],
)
def create_share(slug):
    project = proj(slug)

    if not project:
        abort(404)

    if not one(
        "SELECT token FROM shares WHERE project_id=?",
        project["id"],
    ):
        execute(
            """
            INSERT INTO shares(
                token,
                project_id
            )
            VALUES(?,?)
            """,
            secrets.token_urlsafe(32),
            project["id"],
        )

    return redirect("/p/" + slug)


@app.route(
    "/p/<slug>/share/delete",
    methods=["POST"],
)
def delete_share(slug):
    project = proj(slug)

    if not project:
        abort(404)

    execute(
        "DELETE FROM shares WHERE project_id=?",
        project["id"],
    )

    return redirect("/p/" + slug)


PUBLIC_HTML = """
<!doctype html>
<html lang="tr">

<meta charset="utf-8">

<meta
    name="viewport"
    content="width=device-width,initial-scale=1"
>

<title>{{s.name}} — PlaceHolder</title>

<style>
""" + CSS + """

.share{
    width:min(900px,calc(100% - 32px));
    margin:auto
}
</style>

<main class="share">

<div class="eyebrow">
    PlaceHolder · Public paylaşım
</div>

<h1>{{s.name}}</h1>

{% if s.description %}
    <div class="sub">
        {{s.description}}
    </div>
{% endif %}

<div class="list">

{% for f in files %}

<div class="item">

    <div class="itemmain">

        <div class="itemtitle">
            """ + ICON + """
            {{f.path}}
        </div>

        <div class="muted">
            {{f.size}}
        </div>

    </div>

    <a
        class="btn"
        href="/share/{{s.token}}/{{f.path}}"
    >
        Aç →
    </a>

</div>

{% endfor %}


{% for x in links %}

<div class="item">

    <div class="itemmain">

        <div class="itemtitle">
            🔗 {{x.title}}
        </div>

        <div class="muted">
            {{x.url}}
        </div>

    </div>

    <a
        class="btn"
        href="/share/{{s.token}}/link/{{x.id}}"
    >
        Aç →
    </a>

</div>

{% endfor %}


{% if not files and not links %}

<div class="empty">
    Bu projede henüz paylaşılacak içerik yok.
</div>

{% endif %}

</div>

</main>
"""


@app.route("/share/<token>")
def public_share(token):
    share_data = share(token)

    if not share_data:
        abort(404)

    return render_template_string(
        PUBLIC_HTML,
        s=share_data,
        files=files(
            share_data["project_id"]
        ),
        links=query(
            """
            SELECT *
            FROM links
            WHERE project_id=?
            """,
            share_data["project_id"],
        ),
    )


@app.route(
    "/share/<token>/info/<path:name>"
)
def public_info(token, name):
    share_data = share(token)

    if not share_data:
        abort(404)

    return metadata(
        safe(
            folder(share_data["project_id"]),
            name,
        )
    )


@app.route(
    "/share/<token>/hash/<path:name>"
)
def public_hash(token, name):
    share_data = share(token)

    if not share_data:
        abort(404)

    return {
        "sha256": sha(
            safe(
                folder(
                    share_data["project_id"]
                ),
                name,
            )
        )
    }


@app.route(
    "/share/<token>/link/<int:link_id>"
)
def public_link(token, link_id):
    share_data = share(token)

    if not share_data:
        abort(404)

    link = one(
        """
        SELECT url
        FROM links
        WHERE id=?
        AND project_id=?
        """,
        link_id,
        share_data["project_id"],
    )

    if not link or not valid_url(link["url"]):
        abort(400)

    return redirect(link["url"])


@app.route(
    "/share/<token>/<path:name>"
)
def public_file(token, name):
    share_data = share(token)

    if not share_data:
        abort(404)

    return serve(
        safe(
            folder(
                share_data["project_id"]
            ),
            name,
        )
    )


# =========================================================
# NGROK
# =========================================================

def start_ngrok():
    global NGROK_PROCESS
    global PUBLIC

    ngrok_path = Path(NGROK)

    if not ngrok_path.exists():
        print("ngrok bulunamadı:")
        print(NGROK)
        return

    try:
        NGROK_PROCESS = subprocess.Popen(
            [
                str(ngrok_path),
                "http",
                "5000",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        for _ in range(20):

            if NGROK_PROCESS.poll() is not None:
                return

            time.sleep(1)

            try:
                with urllib.request.urlopen(
                    "http://127.0.0.1:4040/api/tunnels",
                    timeout=2,
                ) as response:

                    tunnels = json.load(
                        response
                    ).get("tunnels", [])

                for tunnel in tunnels:

                    url = tunnel.get(
                        "public_url",
                        "",
                    )

                    if url.startswith("https://"):

                        PUBLIC = url

                        print()
                        print(
                            "================================"
                        )
                        print(
                            "PlaceHolder hazır!"
                        )
                        print(
                            "PUBLIC:",
                            PUBLIC,
                        )
                        print(
                            "================================"
                        )
                        print()

                        return

            except Exception:
                pass

        print("ngrok adresi alınamadı.")

    except Exception as error:
        print(
            "ngrok başlatılamadı:",
            error,
        )


# =========================================================
# START
# =========================================================

if __name__ == "__main__":

    init()

    # Flask'ın başlamasını beklemeden
    # ngrok'u ayrı thread'de başlat.
    threading.Thread(
        target=start_ngrok,
        daemon=True,
    ).start()

    print("PlaceHolder Flask başlatılıyor...")
    print("Yerel sunucu: http://127.0.0.1:5000")

    app.run(
        host="127.0.0.1",
        port=5000,
        debug=False,
    )
