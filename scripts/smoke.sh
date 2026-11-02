#!/usr/bin/env bash
# Brings the Compose stack up and runs every command block in the README against it, checking
# what each one should show. CI runs this on every push.
set -euo pipefail
cd "$(dirname "$0")/.."

WEB="http://localhost:${WEB_PORT:-8210}"
TMP="$(mktemp -d)"

q() { docker compose exec -T db psql -U wx -d wx -t -A -c "$1"; }
wx() { docker compose exec -T worker wxtiles "$@"; }
check() {
  local desc="$1"; shift
  if "$@"; then echo "ok    $desc"; else echo "FAIL  $desc"; exit 1; fi
}
wait_idle() {
  # Done when no job is running or ready to run for three checks in a row. Steps waiting for
  # data that is not upstream yet are scheduled for later and do not count.
  local idle=0 n
  for _ in $(seq 1 240); do
    n=$(q "select count(*) from jobs where state = 'running' or (state = 'queued' and run_after <= now())")
    if [ "$n" = "0" ]; then idle=$((idle + 1)); else idle=0; fi
    if [ "$idle" -ge 3 ]; then return 0; fi
    sleep 5
  done
  echo "the worker did not drain its queue"
  return 1
}

# --- README: Running it
docker compose up -d --build --wait
wait_idle
wx status > "$TMP/status.txt"
cat "$TMP/status.txt"
wx events --limit 20
wx qc
wx runs icon

check "GFS 12Z is the default map" test "$(q "select run from runs where source = 'gfs' and state = 'complete' order by run_time desc limit 1")" = 2026102812
check "three GFS steps came from NOMADS" test "$(q "select count(*) from steps where source = 'gfs' and mirror = 'noaa-nomads'")" = 3
check "the truncated HRRR file was refetched" test "$(q "select count(*) from events where source = 'hrrr' and kind = 'integrity'")" = 1
check "ICON 12Z held for a missing hour" test "$(q "select state from runs where source = 'icon' and run = '2026102812'")" = held
check "ICON stays on 06Z" test "$(q "select run from runs where source = 'icon' and state = 'complete' order by run_time desc limit 1")" = 2026102806
check "GDPS 12Z held by QC" test "$(q "select state from runs where source = 'gdps' and run = '2026102812'")" = held
check "Pangu ran twice" test "$(q "select count(*) from events where kind = 'ai_run'")" = 2
check "GOES-West is flagged late" grep -q "goes_west .*LATE" "$TMP/status.txt"
check "no job failed" test "$(q "select count(*) from jobs where state = 'failed'")" = 0

# --- README: tiles, manifests and the point API through the CDN
TILE="$WEB/tiles/gfs/2026102812/mslp/24/3/1/2.png"
curl -sf -D "$TMP/h1.txt" -o "$TMP/tile.png" "$TILE"
curl -sf -D "$TMP/h2.txt" -o /dev/null "$TILE"
head -c 8 "$TMP/tile.png" > "$TMP/magic.bin"
check "tile is a PNG" grep -q "PNG" "$TMP/magic.bin"
check "first request rendered it (MISS)" grep -qi "x-cache-status: MISS" "$TMP/h1.txt"
curl -sf -D "$TMP/h0.txt" -o /dev/null "$WEB/tiles/gfs/2026102812/t2m/24/3/1/2.png"
check "the promoted run's default field was warmed (HIT on first request)" grep -qi "x-cache-status: HIT" "$TMP/h0.txt"
check "second request came from the cache (HIT)" grep -qi "x-cache-status: HIT" "$TMP/h2.txt"
check "tile is cacheable for a year" grep -qi "max-age=31536000" "$TMP/h1.txt"
curl -sf -o "$TMP/missing.txt" -w "%{http_code}" "$WEB/tiles/gfs/2026102812/t2m/999/3/1/2.png" > "$TMP/code.txt" || true
check "an unpublished hour is a 404" grep -q 404 "$TMP/code.txt"
curl -sf -o "$TMP/index.json" "$WEB/manifests/index.json"
check "index lists all 16 sources" test "$(grep -o '"center"' "$TMP/index.json" | wc -l)" -eq 16
curl -sf -o "$TMP/latest.json" "$WEB/manifests/icon/latest.json"
check "ICON manifest points at 06Z" grep -q '"live_run":"2026102806"' "$TMP/latest.json"
curl -sf -o "$TMP/point.json" "$WEB/api/point?source=gfs&run=2026102812&field=t2m&fhour=24&lon=-95&lat=40"
cat "$TMP/point.json"; echo
check "point value in degrees F" grep -q '"units":"°F"' "$TMP/point.json"
curl -sf -o "$TMP/index.html" "$WEB/"
curl -sf -o "$TMP/status.html" "$WEB/status.html"
check "map page is served" grep -q 'id="map"' "$TMP/index.html"
check "status page is served" grep -q 'id="feeds"' "$TMP/status.html"
curl -sf -o "$TMP/api_status.json" "$WEB/api/status"
check "status API answers" grep -q '"sources"' "$TMP/api_status.json"

# --- README: watch a new cycle arrive
wx advance --minutes 60
wait_idle
wx status > "$TMP/status2.txt"
cat "$TMP/status2.txt"
check "ICON 18Z completed and became the default" test "$(q "select run from runs where source = 'icon' and state = 'complete' order by run_time desc limit 1")" = 2026102818
check "GFS 18Z is posting" test "$(q "select state from runs where source = 'gfs' and run = '2026102818'")" = in_progress
check "posted-to-map time was measured" grep -q "posted to on the map" "$TMP/status2.txt"

# --- README: failover with the AWS bucket down
wx advance --minutes 20 --primary-down
wait_idle
check "steps posted while the AWS bucket was down came from fallbacks" test "$(q "select count(*) from events where kind = 'failover' and detail like '%noaa-aws: not posted%'")" -gt 3
check "no job failed after the advances" test "$(q "select count(*) from jobs where state = 'failed'")" = 0
echo "smoke passed"
