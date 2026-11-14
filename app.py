"""Tiny private photo/video gallery. Stdlib HTTP server + Pillow + ffmpeg."""
import hashlib, hmac, json, mimetypes, os, re, secrets, shutil, subprocess, threading, time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from PIL import Image, ImageOps

DATA = os.environ.get("DATA_DIR", "/data")
MEDIA, THUMBS = os.path.join(DATA, "media"), os.path.join(DATA, "thumbs")
PASSWORD = os.environ.get("GALLERY_PASSWORD", "")
PORT = int(os.environ.get("PORT", "8088"))
INDEX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"}
VIDEO_EXT = {".mp4", ".webm", ".mov", ".m4v"}
SESSION_SECONDS = 30 * 86400
MIN_FREE_BYTES = 2 * 1024**3  # never fill the Pi's disk: Pi-hole etc. live there too
MAX_PER_POST = 10  # carousel posts, like Instagram
PEOPLE = 4  # people who share the gallery (one shared password, pick your name at login)
SIZES = {"t": (400, True), "p": (1080, False)}  # t = square grid tile, p = feed preview

if not PASSWORD:
    raise SystemExit("Set GALLERY_PASSWORD")
os.makedirs(MEDIA, exist_ok=True)
os.makedirs(THUMBS, exist_ok=True)
TRASH = os.path.join(DATA, "trash")  # "Recently deleted": one folder per delete, kept 30 days
TRASH_DAYS = 30
os.makedirs(TRASH, exist_ok=True)
_secret_file = os.path.join(DATA, ".secret")
if not os.path.exists(_secret_file):
    with open(_secret_file, "w") as f:
        f.write(secrets.token_hex(32))
SECRET = open(_secret_file).read().strip().encode()
META_FILE = os.path.join(DATA, "meta.json")
meta_lock = threading.Lock()


def load_meta():
    try:
        with open(META_FILE) as f:
            m = json.load(f)
    except (OSError, ValueError):
        m = {}
    users = (m.get("users") or [])[:PEOPLE]
    users += [f"user{i + 1}" for i in range(len(users), PEOPLE)]  # older files had 2 names
    return {"users": users, "tags": m.get("tags") or {},
            "captions": m.get("captions") or {}, "comments": m.get("comments") or {},
            "posts": m.get("posts") or {}}  # post id -> [[index, filename], ...]; single files have none


def post_members(meta, name):
    """(post id, filenames in order) for a file; a lone file is its own post."""
    for pid, lst in meta["posts"].items():
        if any(n == name for _, n in lst):
            return pid, [n for _, n in sorted(lst)]
    return None, [name]


def save_meta(m):
    tmp = META_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(m, f)
    os.replace(tmp, META_FILE)  # atomic: a crash never leaves half a file


def update_meta(fn):
    with meta_lock:
        m = load_meta()
        fn(m)
        save_meta(m)
        return m


thumb_lock = threading.Semaphore(1)  # one ffmpeg/Pillow job at a time keeps the Pi cool
login_lock = threading.Lock()  # serialises failed logins: ~1 guess/second max


def sign(value):
    return hmac.new(SECRET, value.encode(), hashlib.sha256).hexdigest()


def new_token(user):
    body = f"{int(time.time()) + SESSION_SECONDS}.{user}"
    return body + "." + sign(body)


def token_user(token):
    """Logged-in person (0 or 1) for a cookie token, or None."""
    exp, _, rest = token.partition(".")
    user, _, sig = rest.partition(".")
    if not (exp.isdigit() and user.isdigit() and int(user) < PEOPLE and int(exp) > time.time()):
        return None
    return int(user) if hmac.compare_digest(sig, sign(f"{exp}.{user}")) else None


def trash_entries():
    out = []
    for tid in os.listdir(TRASH):
        try:
            with open(os.path.join(TRASH, tid, "info.json")) as f:
                out.append(dict(json.load(f), id=tid))
        except (OSError, ValueError):
            continue
    return sorted(out, key=lambda e: e["at"], reverse=True)


def purge_trash():
    """Erase deleted items older than TRASH_DAYS for good."""
    cutoff = (time.time() - TRASH_DAYS * 86400) * 1000
    for e in trash_entries():
        if e["at"] < cutoff:
            shutil.rmtree(os.path.join(TRASH, e["id"]), ignore_errors=True)


def purge_loop():
    while True:
        try:
            purge_trash()
        except Exception as e:
            print("purge failed", e, flush=True)
        time.sleep(6 * 3600)


def kind(name):
    ext = os.path.splitext(name)[1].lower()
    return "image" if ext in IMAGE_EXT else "video" if ext in VIDEO_EXT else None


def clean_name(name):
    name = os.path.basename(name.replace("\\", "/")).strip()
    base, ext = os.path.splitext(name)
    if not kind(name):
        return None
    base = re.sub(r"[^\w.\- ]", "_", base)[:100].strip(" .") or "file"
    return base + ext.lower()


def media_path(name):
    """Existing media file for a URL name, or None. Blocks traversal and non-media."""
    if not name or name != os.path.basename(name) or name.startswith(".") or not kind(name):
        return None
    path = os.path.join(MEDIA, name)
    return path if os.path.isfile(path) else None


def thumb_for(name, src, size_key):
    size, crop = SIZES[size_key]
    dst = os.path.join(THUMBS, f"{name}.{size_key}.jpg")
    fresh = lambda: os.path.exists(dst) and os.path.getmtime(dst) >= os.path.getmtime(src)
    if fresh():
        return dst
    with thumb_lock:
        if fresh():
            return dst
        tmp = os.path.join(THUMBS, f".{name}.{size_key}.tmp.jpg")
        try:
            if kind(name) == "image":
                with Image.open(src) as im:
                    im.draft("RGB", (size, size))  # fast JPEG decode at reduced size
                    im = ImageOps.exif_transpose(im).convert("RGB")
                    if crop:
                        im = ImageOps.fit(im, (size, size))
                    else:
                        im.thumbnail((size, size))
                    im.save(tmp, "JPEG", quality=80)
            else:
                vf = (f"scale={size}:{size}:force_original_aspect_ratio=increase,crop={size}:{size}" if crop
                      else f"scale={size}:{size}:force_original_aspect_ratio=decrease")
                for seek in ("1", "0"):  # clips under 1s have no frame at 1s
                    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", seek, "-i", src,
                                        "-frames:v", "1", "-vf", vf, "-q:v", "5", tmp],
                                       capture_output=True, timeout=60)
                    if r.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp):
                        break
                else:
                    return None
            os.replace(tmp, dst)
            return dst
        except Exception as e:
            print("thumb failed", name, e, flush=True)
            if os.path.exists(tmp):
                os.remove(tmp)
            return None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "gallery"

    def log_message(self, fmt, *args):
        pass  # keep logs quiet; errors are printed explicitly

    # ---- helpers
    def send(self, code, body=b"", ctype="application/json", headers=()):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.common_headers()
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def common_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")

    def me(self):
        c = SimpleCookie(self.headers.get("Cookie", ""))
        return token_user(c["g"].value) if "g" in c else None

    def is_https(self):
        return self.headers.get("X-Forwarded-Proto") == "https"

    def cookie(self, value, max_age):
        secure = "; Secure" if self.is_https() else ""
        return ("Set-Cookie", f"g={value}; Max-Age={max_age}; Path=/; HttpOnly; SameSite=Strict{secure}")

    def mutating_ok(self):
        # Custom header can't be sent cross-site without CORS, so this blocks CSRF.
        return self.headers.get("X-Gallery") == "1"

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 4096:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}

    def serve_file(self, path, ctype):
        size = os.path.getsize(path)
        start, end, code = 0, size - 1, 200
        m = re.match(r"bytes=(\d*)-(\d*)$", (self.headers.get("Range") or "").strip())
        if m and (m[1] or m[2]):
            if m[1]:
                start, end = int(m[1]), int(m[2]) if m[2] else size - 1
            else:
                start = max(0, size - int(m[2]))
            end = min(end, size - 1)
            if start > end:
                return self.send(416, headers=[("Content-Range", f"bytes */{size}")])
            code = 206
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        # private: Cloudflare must never cache these for other visitors
        self.send_header("Cache-Control", "private, max-age=86400")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.common_headers()
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(256 * 1024, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    # ---- routes
    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True  # browser cancelled (video seek, closed tab)

    def do_GET(self):
        url = urlparse(self.path)
        p = url.path
        if p == "/":
            with open(INDEX, "rb") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8")
        if p == "/api/users":  # names only, so the login page can show who to pick
            return self.send(200, {"users": load_meta()["users"]})
        me = self.me()
        if me is None:
            return self.send(401, {"error": "login"})
        if p == "/api/list":
            items, meta = [], load_meta()
            where = {n: (pid, i) for pid, lst in meta["posts"].items() for i, n in lst}
            with os.scandir(MEDIA) as it:
                for e in it:
                    if e.is_file() and not e.name.startswith(".") and kind(e.name):
                        st = e.stat()
                        items.append({"name": e.name, "type": kind(e.name),
                                      "size": st.st_size, "mtime": int(st.st_mtime * 1000),
                                      "user": meta["tags"].get(e.name, 0),
                                      "caption": meta["captions"].get(e.name, ""),
                                      "comments": len(meta["comments"].get(e.name, [])),
                                      "last": (meta["comments"].get(e.name) or [None])[-1],
                                      "post": where.get(e.name, (None, 0))[0],
                                      "pi": where.get(e.name, (None, 0))[1]})
            items.sort(key=lambda i: i["mtime"], reverse=True)
            usage = shutil.disk_usage(MEDIA)
            return self.send(200, {"items": items, "users": meta["users"], "me": me, "free": usage.free})
        if p == "/api/trash":
            purge_trash()
            return self.send(200, {"items": trash_entries(), "days": TRASH_DAYS})
        if p.startswith("/x/"):  # a file inside the bin, for the Recently deleted previews
            tid, _, name = unquote(p[3:]).partition("/")
            path = os.path.join(TRASH, tid, name)
            if not (re.fullmatch(r"[0-9a-f]{12}", tid) and name == os.path.basename(name)
                    and not name.startswith(".") and kind(name) and os.path.isfile(path)):
                return self.send(404, {"error": "not found"})
            return self.serve_file(path, mimetypes.guess_type(name)[0] or "application/octet-stream")
        if p == "/api/comments":
            name = parse_qs(url.query).get("name", [""])[0]
            if not media_path(name):
                return self.send(404, {"error": "not found"})
            return self.send(200, {"comments": load_meta()["comments"].get(name, [])})
        if p[:3] in ("/m/", "/t/", "/p/"):
            name = unquote(p[3:])
            src = media_path(name)
            if not src:
                return self.send(404, {"error": "not found"})
            if p.startswith("/m/"):
                return self.serve_file(src, mimetypes.guess_type(name)[0] or "application/octet-stream")
            t = thumb_for(name, src, p[1])
            return self.serve_file(t, "image/jpeg") if t else self.send(404, {"error": "no thumb"})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        p = urlparse(self.path).path
        if not self.mutating_ok():
            return self.send(403, {"error": "forbidden"})
        if p == "/api/login":
            body = self.read_json()
            pw, user = str(body.get("password", "")), body.get("user")
            if user not in range(PEOPLE) or isinstance(user, bool):
                return self.send(400, {"error": "pick a person"})
            with login_lock:
                if hmac.compare_digest(pw.encode(), PASSWORD.encode()):
                    return self.send(200, {"ok": True, "me": user},
                                     headers=[self.cookie(new_token(user), SESSION_SECONDS)])
                time.sleep(1)
            return self.send(401, {"error": "wrong password"})
        if p == "/api/logout":
            return self.send(200, {"ok": True}, headers=[self.cookie("", 0)])
        me = self.me()
        if me is None:
            return self.send(401, {"error": "login"})
        body = self.read_json()
        if p == "/api/users":  # rename the two people
            names = body.get("users")
            if not (isinstance(names, list) and len(names) == PEOPLE
                    and all(isinstance(n, str) and 0 < len(n.strip()) <= 24 for n in names)):
                return self.send(400, {"error": f"{PEOPLE} names, 1-24 characters each"})
            m = update_meta(lambda m: m.update(users=[n.strip() for n in names]))
            return self.send(200, {"users": m["users"]})
        if p == "/api/caption":  # Instagram-style text under a post; owner only
            name, text = body.get("name"), body.get("text")
            if not media_path(name) or not isinstance(text, str) or len(text) > 2200:
                return self.send(400, {"error": "caption up to 2200 characters"})
            if load_meta()["tags"].get(name, 0) != me:
                return self.send(403, {"error": "not your post"})
            text = text.strip()
            update_meta(lambda m: m["captions"].__setitem__(name, text) if text else m["captions"].pop(name, None))
            return self.send(200, {"caption": text})
        if p == "/api/comment":  # either person can comment on any post
            name, text = body.get("name"), body.get("text")
            text = text.strip() if isinstance(text, str) else ""
            if not media_path(name) or not 0 < len(text) <= 500:
                return self.send(400, {"error": "comment must be 1-500 characters"})
            c = {"id": secrets.token_hex(4), "by": me, "text": text, "at": int(time.time() * 1000)}
            def add(m):
                lst = m["comments"].setdefault(name, [])
                if len(lst) >= 1000:
                    raise ValueError("too many comments")
                lst.append(c)
            try:
                update_meta(add)
            except ValueError:
                return self.send(400, {"error": "too many comments on this post"})
            return self.send(200, {"comment": c})
        if p in ("/api/trash/restore", "/api/trash/erase"):  # anyone logged in
            tid = str(body.get("id", ""))
            tdir = os.path.join(TRASH, tid)
            if not (re.fullmatch(r"[0-9a-f]{12}", tid) and os.path.isfile(os.path.join(tdir, "info.json"))):
                return self.send(404, {"error": "not found"})
            if p == "/api/trash/erase":
                shutil.rmtree(tdir, ignore_errors=True)
                return self.send(200, {"ok": True})
            with open(os.path.join(tdir, "info.json")) as f:
                snap = json.load(f)
            back = []  # [(new name, original entry)]
            for fi in snap["files"]:
                srcp = os.path.join(tdir, fi["name"])
                if not os.path.isfile(srcp):
                    continue
                name = fi["name"]
                base, ext = os.path.splitext(name)
                n = 1
                while os.path.exists(os.path.join(MEDIA, name)):  # a new upload took the name meanwhile
                    name, n = f"{base}-{n}{ext}", n + 1
                os.replace(srcp, os.path.join(MEDIA, name))
                back.append((name, fi))
            def put_back(m):
                for name, fi in back:
                    m["tags"][name] = fi["user"]
                pid = snap.get("pid")
                group = [name for name, _ in back]
                if pid:
                    entries = m["posts"].get(pid)
                    if entries is None:  # post had shrunk to one photo (or vanished): rebuild it
                        entries = [[i, n] for i, n in snap.get("siblings", []) if media_path(n)]
                    entries = [e for e in entries if e[1] not in group] + [[fi["pi"], name] for name, fi in back]
                    entries.sort()
                    if len(entries) > 1:
                        m["posts"][pid] = entries
                    group = [n for _, n in entries]
                lead = group[0] if group else None
                if lead:  # caption + comments always sit on the post's first photo
                    for k in ("captions", "comments"):
                        held = next((n for n in group if n in m[k]), None)
                        if held and held != lead:
                            m[k][lead] = m[k].pop(held)
                    if snap.get("caption") and lead not in m["captions"]:
                        m["captions"][lead] = snap["caption"]
                    if snap.get("comments") and lead not in m["comments"]:
                        m["comments"][lead] = snap["comments"]
            update_meta(put_back)
            shutil.rmtree(tdir, ignore_errors=True)
            return self.send(200, {"ok": True, "restored": [n for n, _ in back]})
        if p == "/api/tag":  # who posted it; anyone logged in may change it (the Gallery picker)
            name, user = body.get("name"), body.get("user")
            if user not in range(PEOPLE) or isinstance(user, bool) or not media_path(name):
                return self.send(400, {"error": "bad tag"})
            names = post_members(load_meta(), name)[1]  # a carousel post keeps a single owner
            update_meta(lambda m: [m["tags"].__setitem__(n, user) for n in names])
            return self.send(200, {"ok": True, "names": names})
        self.send(404, {"error": "not found"})

    def do_PUT(self):
        url = urlparse(self.path)
        me = self.me()
        if not self.mutating_ok() or me is None:
            self.close_connection = True  # don't read the unauthorised body
            return self.send(401, {"error": "login"})
        if url.path != "/api/upload":
            self.close_connection = True
            return self.send(404, {"error": "not found"})
        q = parse_qs(url.query)
        name = clean_name(q.get("name", [""])[0])
        length = int(self.headers.get("Content-Length") or -1)
        if not name or length < 0:
            self.close_connection = True
            return self.send(400, {"error": "only photos and videos (jpg png gif webp avif mp4 mov webm m4v)"})
        if shutil.disk_usage(MEDIA).free - length < MIN_FREE_BYTES:
            self.close_connection = True
            return self.send(507, {"error": "disk almost full"})
        pid, pi = q.get("post", [""])[0], q.get("i", ["0"])[0]
        if pid:
            if not (re.fullmatch(r"[0-9a-f]{8,32}", pid) and pi.isdigit() and int(pi) < MAX_PER_POST):
                self.close_connection = True
                return self.send(400, {"error": "bad post id"})
            meta = load_meta()
            lst = meta["posts"].get(pid, [])
            if len(lst) >= MAX_PER_POST:
                self.close_connection = True
                return self.send(400, {"error": f"a post holds at most {MAX_PER_POST} photos/videos"})
            if any(meta["tags"].get(n, 0) != me for _, n in lst):
                self.close_connection = True
                return self.send(403, {"error": "not your post"})
        base, ext = os.path.splitext(name)
        n = 1
        while os.path.exists(os.path.join(MEDIA, name)):
            name, n = f"{base}-{n}{ext}", n + 1
        final = os.path.join(MEDIA, name)
        part = os.path.join(MEDIA, f".{secrets.token_hex(6)}.part")
        try:
            with open(part, "wb") as f:
                left = length
                while left > 0:
                    chunk = self.rfile.read(min(1024 * 1024, left))
                    if not chunk:
                        raise ConnectionError("upload cut off")
                    f.write(chunk)
                    left -= len(chunk)
            os.replace(part, final)
        except Exception:
            if os.path.exists(part):
                os.remove(part)
            raise
        mt = q.get("mtime", [""])[0]
        if mt.isdigit() and 0 < int(mt) < time.time() * 1000 + 86400000:
            os.utime(final, (time.time(), int(mt) / 1000))  # keep the photo's own date
        def record(m):
            if me:  # uploads always belong to whoever is logged in (0 is the default)
                m["tags"][name] = me
            if pid:
                m["posts"].setdefault(pid, []).append([int(pi), name])
        update_meta(record)
        self.send(200, {"name": name})

    def do_DELETE(self):
        url = urlparse(self.path)
        me = self.me()
        if not self.mutating_ok() or me is None:
            return self.send(401, {"error": "login"})
        q = parse_qs(url.query)
        name = q.get("name", [""])[0]
        src = media_path(name)
        if url.path == "/api/comment" and src:  # only the writer can delete a comment
            cid, res = q.get("id", [""])[0], []
            def rm(m):
                lst = m["comments"].get(name, [])
                hit = [c for c in lst if c["id"] == cid]
                if hit and hit[0]["by"] == me:
                    lst.remove(hit[0]); res.append(200)
                    if not lst:
                        m["comments"].pop(name, None)
                else:
                    res.append(403 if hit else 404)
            update_meta(rm)
            return self.send(res[0], {"ok": res[0] == 200})
        if url.path != "/api/file" or not src:
            return self.send(404, {"error": "not found"})
        meta = load_meta()
        pid, members = post_members(meta, name)
        if q.get("scope", [""])[0] == "photo":
            # Gallery delete: anyone logged in may remove a single photo, from any post
            names = [name]
            lead_gone, rest = members[0] == name, [n for n in members if n != name]
        else:
            # Me / Feed delete: the owner removes the whole post
            names = members
            if any(meta["tags"].get(n, 0) != me for n in names):
                return self.send(403, {"error": "not your post"})
            lead_gone, rest = True, []
        tid = secrets.token_hex(6)
        tdir = os.path.join(TRASH, tid)
        os.makedirs(tdir)
        snap = {"at": int(time.time() * 1000), "by": me, "pid": pid,
                "siblings": [e for e in (meta["posts"].get(pid) or []) if e[1] not in names],
                "files": [{"name": n, "user": meta["tags"].get(n, 0),
                           "pi": next((i for i, x in (meta["posts"].get(pid) or []) if x == n), 0)} for n in names],
                # caption/comments go to the bin only when nothing of the post is left to carry them
                "caption": "" if rest else meta["captions"].get(members[0], ""),
                "comments": [] if rest else meta["comments"].get(members[0], [])}
        for n in names:
            p = media_path(n)
            if p:
                os.replace(p, os.path.join(tdir, n))  # same disk: instant, no copy
            for k in SIZES:
                t = os.path.join(THUMBS, f"{n}.{k}.jpg")
                if os.path.exists(t):
                    os.remove(t)
        def forget(m):
            if lead_gone and rest:  # caption and comments live on a post's first photo: hand them on
                for k in ("captions", "comments"):
                    if name in m[k]:
                        m[k][rest[0]] = m[k].pop(name)
            for n in names:
                for k in ("tags", "captions", "comments"):
                    m[k].pop(n, None)
            if pid:
                m["posts"][pid] = [e for e in m["posts"].get(pid, []) if e[1] not in names]
                if len(m["posts"][pid]) <= 1:
                    m["posts"].pop(pid)  # one photo left is just a normal post
        with open(os.path.join(tdir, "info.json"), "w") as f:
            json.dump(snap, f)
        update_meta(forget)
        self.send(200, {"ok": True, "deleted": names})


if __name__ == "__main__":
    print(f"gallery on :{PORT}, data in {DATA}", flush=True)
    threading.Thread(target=purge_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
