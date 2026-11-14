# N Gallery

A small, private photo and video gallery for a few people, built to run on a Raspberry Pi.
It works like a mini Instagram: each person posts photos and videos with captions, everyone can comment, and
there's an all-photos grid. It's one Python file and one HTML page, with no database and no framework,
and it uses about 15 MB of RAM.

## Features

- **Login**: tap your name, then enter the shared password. Up to 4 people by default.
- **Gallery tab**: every photo and video from everyone, grouped by month, with All / Photos / Videos filters.
  Anyone can change who a photo belongs to or delete a single photo from here.
- **Feed tab**: everyone else's posts, Instagram style, with a row of faces to filter by person.
  Sort by newest, oldest or random.
- **Me tab**: your own posts.
- **New post**: pick up to 10 photos or videos at once to make one swipeable carousel post, and write the caption before sharing.
- **Captions and comments**: one caption and one comment thread per post. Only the writer can edit a caption or delete a comment.
- **Full-screen viewer**: edge to edge, tap to show or hide controls, pinch / double-tap / mouse-wheel zoom, swipe between photos.
- **Recently deleted**: deletes go to a bin for 30 days with a Restore button that brings back the caption, comments and
  position in the post. After 30 days items are erased automatically.
- **Originals kept**: uploads are stored byte for byte. Previews are generated separately with Pillow and ffmpeg.
- **Safe for a small device**: thumbnails are made one at a time, uploads are refused when free disk would drop below 2 GB,
  and a memory limit is set on the container.

## How it works

```
browser ──HTTP──> app.py (Python stdlib http.server, port 8088)
                    ├── index.html          single-page UI (vanilla JS, no build step)
                    └── /data               (a Docker volume)
                         ├── media/         original uploads
                         ├── thumbs/        generated previews (safe to delete; rebuilt on demand)
                         ├── trash/         "Recently deleted" items, one folder per delete
                         ├── meta.json      names, owners, captions, comments, carousel posts
                         └── .secret        key for signing login cookies (created on first start)
```

- **Auth**: one shared password (`GALLERY_PASSWORD`). Logging in gives an HMAC-signed, HttpOnly, SameSite=Strict cookie
  that holds which person you are. It's valid for 30 days. Every change request also needs an `X-Gallery: 1` header, which
  blocks cross-site requests (CSRF). Failed logins are slowed to about one per second.
- **Storage**: `meta.json` is rewritten atomically (write a temp file, then rename) under a lock, so a crash or power cut can't
  corrupt it. This is plenty for a few people and tens of thousands of comments. Beyond that, swap it for SQLite.
- **Thumbnails**: square 400 px tiles for the grid and 1080 px previews for the feed, made on first view.
  ffmpeg grabs a frame for video previews.

## Requirements

- Any Linux machine with Docker. It was built on a Raspberry Pi 4 running DietPi (arm64), and works on x86 too.
- About 250 MB for the image, plus space for your photos.

## Setup

```sh
git clone https://github.com/syafiq-dev/n-gallery.git
cd n-gallery

# 1. pick a password (keep this file private)
echo "GALLERY_PASSWORD=$(head -c 12 /dev/urandom | base64 | tr -d '/+=' | cut -c1-14)" > .env
chmod 600 .env
cat .env        # this is the password everyone uses

# 2. build and run
docker build -t gallery .
mkdir -p /srv/gallery            # where photos will live; change to suit (e.g. a USB SSD)
docker run -d --name gallery --restart unless-stopped \
  -p 8088:8088 \
  --env-file .env \
  -v /srv/gallery:/data \
  --memory 300m \
  gallery
```

Open `http://<pi-ip>:8088`, tap a name, enter the password and you're in.
Rename the people with the gear icon (Settings).

Or with Docker Compose: `docker compose up -d --build`. See `docker-compose.yml`, and set the data path there first.

### Settings

| What | Where | Default |
|---|---|---|
| Password | `GALLERY_PASSWORD` in `.env` | (required) |
| Port inside the container | `PORT` env | 8088 |
| Data folder inside the container | `DATA_DIR` env | `/data` |
| Number of people | `PEOPLE` in `app.py` | 4 |
| Max files per post | `MAX_PER_POST` in `app.py` (and `index.html`) | 10 |
| Days in "Recently deleted" | `TRASH_DAYS` in `app.py` | 30 |
| Minimum free disk to accept uploads | `MIN_FREE_BYTES` in `app.py` | 2 GB |
| Login lifetime | `SESSION_SECONDS` in `app.py` | 30 days |

## Hosting

The app speaks plain HTTP on port 8088. Pick one or more of the options below.

### At home (LAN)
Use `http://<pi-ip>:8088` from any device on the same Wi-Fi. Nothing else is needed.

### Anywhere, privately (Tailscale)
Install [Tailscale](https://tailscale.com) on the Pi and on your phones, then open `http://<pi-tailscale-ip>:8088`.
Only your own devices can reach it, and there's no upload size limit.

### Public URL (Cloudflare Tunnel)
This gives you an https address on your own domain without opening any ports on your router.

```sh
# on the Pi, with cloudflared installed and your domain on Cloudflare
cloudflared tunnel login
cloudflared tunnel create gallery
cloudflared tunnel route dns gallery photos.example.com
```

`/etc/cloudflared/config.yml`:
```yaml
tunnel: gallery
credentials-file: /root/.cloudflared/<tunnel-id>.json
ingress:
  - hostname: photos.example.com
    service: http://localhost:8088
  - service: http_status:404
```

```sh
sudo cloudflared service install && sudo systemctl restart cloudflared
```

Notes:
- Cloudflare's free plan rejects uploads over **100 MB** per file. Upload big videos over LAN or Tailscale instead.
- Media is sent with `Cache-Control: private`, so Cloudflare never caches your photos for other visitors.
- The login cookie gets the `Secure` flag automatically when the request comes through https.
- For an extra lock, put [Cloudflare Access](https://developers.cloudflare.com/cloudflare-one/policies/access/) in front
  so only listed emails reach the login page.

## Everyday tasks

**Update to a new version**
```sh
git pull
docker build -t gallery .
docker rm -f gallery
docker run -d --name gallery --restart unless-stopped -p 8088:8088 --env-file .env -v /srv/gallery:/data --memory 300m gallery
docker image prune -f
```

**Change the password**: edit `.env`, then recreate the container with the two `docker rm` / `docker run` lines above.
A plain `docker restart` does not reload the env file.

**Back up**: copy the whole data folder (`/srv/gallery`). It holds the photos, captions, comments and the bin, e.g.
`rsync -a /srv/gallery/ /mnt/usb/gallery-backup/`. The `thumbs/` folder can be skipped.

**Smoke test** (creates and removes a few test posts):
```sh
./selftest.sh http://localhost:8088 'YOUR_PASSWORD'
```

## Things to know

- Everyone shares one password, and anyone who has it can pick any name. This is meant for a small group that trusts each other.
- In the Gallery tab anyone can delete any single photo. Deletes go to "Recently deleted" for 30 days, so mistakes can be undone.
- Photos keep their metadata, including GPS location, as uploaded.
- iPhone Safari converts HEIC photos to JPEG while uploading and may compress videos picked from Photos. Android and desktop send the originals.
- New comments and posts show up on refresh. There are no live updates or push notifications.

## API (for the curious)

All endpoints except `/`, `/api/users` and `/api/login` need the login cookie. Every POST/PUT/DELETE also needs `X-Gallery: 1`.

| Method | Path | What it does |
|---|---|---|
| GET | `/api/users` | the people's names (shown on the login page) |
| POST | `/api/login` / `/api/logout` | `{"password", "user"}` |
| GET | `/api/list` | all items with owner, caption, comment count, post id |
| PUT | `/api/upload?name=&mtime=&post=&i=` | upload one file (raw body); `post`+`i` group files into a carousel |
| POST | `/api/caption` | `{"name", "text"}`, post owner only |
| GET / POST / DELETE | `/api/comments?name=` / `/api/comment` / `/api/comment?name=&id=` | read / add / delete (writer only) |
| POST | `/api/tag` | `{"name", "user"}` change who posted it (whole post) |
| POST | `/api/users` | rename everyone |
| DELETE | `/api/file?name=` | owner deletes the whole post; add `&scope=photo` to delete one photo (anyone) |
| GET / POST | `/api/trash`, `/api/trash/restore`, `/api/trash/erase` | Recently deleted |
| GET | `/m/<name>` `/t/<name>` `/p/<name>` | original, grid tile, feed preview |
