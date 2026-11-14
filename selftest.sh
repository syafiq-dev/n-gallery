#!/bin/sh
# Smoke test against a running gallery. It creates a few test posts and deletes them again.
# Usage: ./selftest.sh http://localhost:8088 'YOUR_PASSWORD'
B=${1:?base url}; P=${2:?password}
T=$(mktemp -d); fail=0
trap 'rm -rf "$T"' EXIT
echo '/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAA0JCgsKCA0LCgsODg0PEyAVExISEyccHhcgLikxMC4pLSwzOko+MzZGNywtQFdBRkxOUlNSMj5aYVpQYEpRUk//2wBDAQ4ODhMREyYVFSZPNS01T09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT0//wAARCAAQABADASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDGooorgPrj/9k=' | base64 -d > "$T/a.jpg"
chk() { if [ "$2" = "$3" ]; then echo "ok   $1"; else echo "FAIL $1 (got $2, want $3)"; fail=1; fi; }
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
H="-H X-Gallery:1"
login() { curl -s -c "$T/j$1" -o /dev/null -w '%{http_code}' $H -X POST -d "{\"password\":\"$P\",\"user\":$1}" "$B/api/login"; }
json() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }

chk "page loads"               "$(code "$B/")" 200
chk "names are public"         "$(code "$B/api/users")" 200
chk "list needs login"         "$(code "$B/api/list")" 401
chk "login needs a person"     "$(code $H -X POST -d "{\"password\":\"$P\"}" "$B/api/login")" 400
chk "wrong password"           "$(code $H -X POST -d '{"password":"nope","user":0}' "$B/api/login")" 401
chk "login without header"     "$(code -X POST -d "{\"password\":\"$P\",\"user\":0}" "$B/api/login")" 403
chk "login as person 1"        "$(login 0)" 200
chk "login as person 2"        "$(login 1)" 200
chk "me is person 2"           "$(curl -s -b "$T/j1" "$B/api/list" | json 'd["me"]')" 1
chk "html upload refused"      "$(code -b "$T/j0" $H -X PUT --data-binary x "$B/api/upload?name=x.html")" 400

N=$(curl -s -b "$T/j0" $H -X PUT --data-binary @"$T/a.jpg" "$B/api/upload?name=selftest.jpg" | json 'd["name"]')
chk "upload"                   "$(echo "$N" | grep -c '^selftest')" 1
chk "thumbnail"                "$(curl -s -b "$T/j0" -o /dev/null -w '%{content_type}' "$B/t/$N")" image/jpeg
chk "range request"            "$(curl -s -b "$T/j0" -o /dev/null -w '%{http_code}:%{size_download}' -H 'Range: bytes=0-9' "$B/m/$N")" 206:10
chk "caption by owner"         "$(code -b "$T/j0" $H -X POST -d "{\"name\":\"$N\",\"text\":\"hello\"}" "$B/api/caption")" 200
chk "caption by other refused" "$(code -b "$T/j1" $H -X POST -d "{\"name\":\"$N\",\"text\":\"x\"}" "$B/api/caption")" 403
chk "comment by other"         "$(code -b "$T/j1" $H -X POST -d "{\"name\":\"$N\",\"text\":\"nice\"}" "$B/api/comment")" 200
chk "post delete by other"     "$(code -b "$T/j1" $H -X DELETE "$B/api/file?name=$N")" 403
chk "traversal blocked"        "$(code -b "$T/j0" --path-as-is "$B/m/..%2f.secret")" 404

PID=$(python3 -c 'import secrets; print(secrets.token_hex(8))')
for i in 0 1; do curl -s -b "$T/j0" -o /dev/null $H -X PUT --data-binary @"$T/a.jpg" "$B/api/upload?name=selftest-c$i.jpg&post=$PID&i=$i"; done
chk "carousel post"            "$(curl -s -b "$T/j0" "$B/api/list" | json "len([i for i in d['items'] if i['post']=='$PID'])")" 2

chk "owner deletes post"       "$(code -b "$T/j0" $H -X DELETE "$B/api/file?name=$N")" 200
TID=$(curl -s -b "$T/j0" "$B/api/trash" | json 'd["items"][0]["id"]')
chk "restore from bin"         "$(code -b "$T/j0" $H -X POST -d "{\"id\":\"$TID\"}" "$B/api/trash/restore")" 200
chk "caption came back"        "$(curl -s -b "$T/j0" "$B/api/list" | json "[i['caption'] for i in d['items'] if i['name']=='$N'][0]")" hello

# clean up: delete the test posts and erase them from the bin
for f in "$N" selftest-c0.jpg; do curl -s -b "$T/j0" -o /dev/null $H -X DELETE "$B/api/file?name=$f"; done
for id in $(curl -s -b "$T/j0" "$B/api/trash" | json '" ".join(e["id"] for e in d["items"] if any(f["name"].startswith("selftest") for f in e["files"]))'); do
  curl -s -b "$T/j0" -o /dev/null $H -X POST -d "{\"id\":\"$id\"}" "$B/api/trash/erase"
done
[ $fail = 0 ] && echo "all good" || echo "some checks failed"
exit $fail
