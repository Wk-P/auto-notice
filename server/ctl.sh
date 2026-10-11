#!/usr/bin/env bash
# Manages a PNU Notice server described by a config file (default server/server.conf, see server/README.md).
#
# Usage: server/ctl.sh [-c CONF] <command> [options]
#
#   check                        read-only: ssh, Docker, files on the server, school sites, public URL
#   setup [--from OLD_CONF] [--env FILE] [--cloudflared DIR]
#                                prepare a new server: directories, .env, tunnel credentials, data volume, code
#   deploy [--skip-tests] [--allow-dirty] [--dry-run] [service ...]
#                                upload the current checkout, rebuild and restart
#   migrate --from OLD_CONF [--force] [--yes]
#                                stop the old server, move its database here and start
#   start | stop | status        control the services (stop never deletes the database)
#   logs [service] [-f]          recent logs of one service (default scheduler)
#   rollback                     swarm only: return the services to the previous image
#   swarm-init [--yes]           turn the server's Docker into a single-node swarm
#   switch compose|swarm         move the running services to the other mode; the database is kept
#
# CONF defaults to $SERVER_CONF, then server/server.conf.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ORIG_PWD="$PWD"

# Shared by both modes, so switching modes or servers always finds the same database volume.
PROJECT=pnu-notice
VOLUME=pnu-notice_pnu_data
IMAGE_REPO=pnu-notice
COMPOSE_FILE=compose.tunnel.yaml
SWARM_FILE=compose.swarm.yaml
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=15)
# Server-only state is never uploaded or overwritten: secrets, tunnel credentials, data and backups.
RSYNC_EXCLUDES=(--exclude .git --exclude .venv --exclude .env --exclude keys --exclude var --exclude backups
  --exclude cloudflared --exclude scripts --exclude server --exclude '*.egg-info' --exclude __pycache__
  --exclude .pytest_cache --exclude .DS_Store)

step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
note() { printf '\033[33m%s\033[0m\n' "$*"; }
fail() { printf '\033[1;31mError: %s\033[0m\n' "$*" >&2; exit 1; }
usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; }

# Runs the bash script on stdin on a host; the remaining arguments become $1, $2, ... there.
remote() {
  local host="$1"; shift
  ssh "${SSH_OPTS[@]}" "$host" "bash -s -- $(printf '%q ' "$@")"
}

# Paths given on the command line are relative to where the script was started, not to the repository.
abs_path() { case "$1" in /*) echo "$1" ;; *) echo "$ORIG_PWD/$1" ;; esac; }

confirm() {
  local answer
  printf '%s [y/N] ' "$1"
  read -r answer
  [ "$answer" = y ] || [ "$answer" = Y ] || fail "cancelled."
}

# ---------------------------------------------------------------- configuration

load_defaults() {
  HOST="" DIR=pnu-notice URL="" MODE=compose SERVICES="web scheduler backup" STACK_NAME=pnu-notice
  KEEP_IMAGES=3 SWARM_ADVERTISE_ADDR=127.0.0.1 SWARM_ADDR_POOL=10.201.0.0/16 CHECK_URLS=""
}

load_conf() {
  [ -f "$1" ] || fail "config $1 not found; copy server/server.conf.example to it and fill it in."
  load_defaults
  # shellcheck disable=SC1090
  . "$1"
  [ -n "$HOST" ] || fail "HOST is empty in $1."
  case "$MODE" in compose|swarm) ;; *) fail "MODE in $1 must be compose or swarm, not '$MODE'." ;; esac
}

# Reads another server's config into O_HOST, O_DIR and O_STACK without touching the current settings.
load_old_conf() {
  [ -f "$1" ] || fail "config $1 not found."
  local values
  values="$(load_defaults; . "$1"; printf '%s\n%s\n%s' "$HOST" "$DIR" "$STACK_NAME")"
  O_HOST="$(printf '%s\n' "$values" | sed -n 1p)"
  O_DIR="$(printf '%s\n' "$values" | sed -n 2p)"
  O_STACK="$(printf '%s\n' "$values" | sed -n 3p)"
  [ -n "$O_HOST" ] || fail "HOST is empty in $1."
}

set_conf_mode() {
  local tmp
  tmp="$(mktemp)"
  if grep -q '^MODE=' "$CONF"; then
    awk -v m="$1" '/^MODE=/ { print "MODE=\"" m "\""; next } { print }' "$CONF" > "$tmp"
  else
    cat "$CONF" > "$tmp"; printf 'MODE="%s"\n' "$1" >> "$tmp"
  fi
  cat "$tmp" > "$CONF"; rm -f "$tmp"
  echo "MODE=$1 saved to $CONF"
}

# ---------------------------------------------------------------- server state

# Prints compose, swarm, both or none: what is actually running on a host, whatever the config says.
running_mode() {
  remote "$1" "$PROJECT" "$2" <<'EOF'
c="$(docker ps -q --filter "label=com.docker.compose.project=$1" | head -1)"
s="$(docker service ls -q --filter "label=com.docker.stack.namespace=$2" 2>/dev/null | head -1)"
if [ -n "$c" ] && [ -n "$s" ]; then echo both; elif [ -n "$c" ]; then echo compose; elif [ -n "$s" ]; then echo swarm; else echo none; fi
EOF
}

swarm_state() { ssh "${SSH_OPTS[@]}" "$1" "docker info --format '{{.Swarm.LocalNodeState}}'"; }

require_ready() {
  ssh "${SSH_OPTS[@]}" "$HOST" "test -f ~/$DIR/.env && test -f ~/$DIR/cloudflared/credentials.json" \
    || fail "~/$DIR on $HOST is missing .env or cloudflared/credentials.json; run setup first."
}

# Refuses to start this mode while the other one is running: two schedulers would send every email twice.
require_mode_free() {
  local running
  running="$(running_mode "$HOST" "$STACK_NAME")"
  case "$running" in
    none|"$MODE") ;;
    *) fail "$HOST is running '$running' but the config says MODE=$MODE; use 'switch $MODE' (or fix MODE)." ;;
  esac
  if [ "$MODE" = swarm ] && [ "$(swarm_state "$HOST")" != active ]; then
    fail "Docker on $HOST is not in swarm mode; run swarm-init first."
  fi
}

ensure_volume() {
  remote "$1" "$VOLUME" "$PROJECT" <<'EOF'
# Compose labels let compose.tunnel.yaml adopt the volume without warnings; swarm uses it as external.
docker volume inspect "$1" >/dev/null 2>&1 || docker volume create \
  --label "com.docker.compose.project=$2" --label com.docker.compose.volume=pnu_data "$1" >/dev/null
EOF
}

upload_code() {
  local host="$1" dir="$2" revision="$3"
  rsync -az --itemize-changes "${RSYNC_EXCLUDES[@]}" ./ "$host:$dir/" | awk '$1 ~ /^<f/ {print "  updated " $2}'
  ssh "${SSH_OPTS[@]}" "$host" "echo '$revision' > ~/$dir/REVISION"
}

# ---------------------------------------------------------------- start / stop

# Stops whichever mode is running. The database volume is never removed.
stop_services() {
  remote "$1" "$2" "$3" "$PROJECT" "$COMPOSE_FILE" <<'EOF'
set -e
cd ~/"$1"
if [ -n "$(docker service ls -q --filter "label=com.docker.stack.namespace=$2" 2>/dev/null)" ]; then
  docker stack rm "$2"
  # stack rm returns at once; wait until the containers and the network are gone so the volume is free.
  for _ in $(seq 1 60); do
    if [ -z "$(docker ps -q --filter "label=com.docker.stack.namespace=$2")" ] \
      && ! docker network inspect "${2}_default" >/dev/null 2>&1; then break; fi
    sleep 2
  done
  [ -z "$(docker ps -q --filter "label=com.docker.stack.namespace=$2")" ] || { echo "stack $2 did not stop in 120s" >&2; exit 1; }
fi
if [ -n "$(docker ps -aq --filter "label=com.docker.compose.project=$3")" ]; then
  docker compose -f "$4" down
fi
EOF
}

# Builds the image for the next swarm deploy and prints its tag.
swarm_build() {
  local tag="$IMAGE_REPO:$1-$(date +%Y%m%d%H%M%S)"
  # nice keeps the build from competing with other workloads on a shared machine.
  ssh "${SSH_OPTS[@]}" "$HOST" "cd ~/$DIR && nice -n 10 docker build -q -t '$tag' . >/dev/null" >&2
  echo "$tag"
}

# Deploys the stack with the given image (empty: the newest pnu-notice image on the server).
swarm_up() {
  remote "$HOST" "$DIR" "$STACK_NAME" "$1" "$SWARM_FILE" "$IMAGE_REPO" <<'EOF'
set -e
cd ~/"$1"
image="$3"
[ -n "$image" ] || image="$(docker images "$5" --format '{{.Repository}}:{{.Tag}}' | grep -v ':<none>$' | head -1)"
[ -n "$image" ] || { echo "No $5 image on this server yet; run deploy." >&2; exit 1; }
uid="$(sed -n 's/^HOST_UID=//p' .env | tail -1)"
gid="$(sed -n 's/^HOST_GID=//p' .env | tail -1)"
echo "Deploying $image"
# timeout: a first deploy whose web never turns healthy has nothing to roll back to and would wait forever.
IMAGE="$image" APP_DIR="$PWD" HOST_UID="$uid" HOST_GID="$gid" \
  timeout 300 docker stack deploy --detach=false --resolve-image never -c "$4" "$2" \
  || { echo "stack deploy did not finish; see: server/ctl.sh logs web" >&2; exit 1; }
# A failed update is rolled back by swarm; then web runs the old image and this deploy did not happen.
current="$(docker service inspect -f '{{.Spec.TaskTemplate.ContainerSpec.Image}}' "${2}_web")"
[ "$current" = "$image" ] || { echo "web runs $current instead of $image: the update failed and was rolled back." >&2; exit 1; }
docker service ls --filter "label=com.docker.stack.namespace=$2" --format '{{.Name}}\t{{.Replicas}}\t{{.Image}}'
EOF
}

compose_up() {
  # cloudflared is always included so a fresh server is reachable after its first deploy.
  ssh "${SSH_OPTS[@]}" "$HOST" "cd ~/$DIR && docker compose -f $COMPOSE_FILE config -q \
    && nice -n 10 docker compose -f $COMPOSE_FILE build --quiet $1 \
    && docker compose -f $COMPOSE_FILE up -d $1 cloudflared"
  local state=""
  for _ in $(seq 1 30); do
    state="$(ssh "${SSH_OPTS[@]}" "$HOST" "docker inspect -f '{{.State.Health.Status}}' \$(cd ~/$DIR && docker compose -f $COMPOSE_FILE ps -q web)" 2>/dev/null || true)"
    [ "$state" = healthy ] && break
    sleep 2
  done
  [ "$state" = healthy ] || fail "web is '$state' after 60s; see: server/ctl.sh logs web"
  ssh "${SSH_OPTS[@]}" "$HOST" "cd ~/$DIR && docker compose -f $COMPOSE_FILE ps --format '{{.Service}}\t{{.Status}}'"
}

start_services() {
  if [ "$MODE" = swarm ]; then swarm_up "${1:-}"; else compose_up "$SERVICES"; fi
}

verify_public() {
  [ -n "$URL" ] || { note "URL is empty in the config; skipping the public check."; return 0; }
  local code=""
  # The tunnel may need a few seconds to reconnect after a restart.
  for _ in $(seq 1 15); do
    code="$(curl -sS -m 10 -o /dev/null -w '%{http_code}' "$URL/healthz" 2>/dev/null || true)"
    [ "$code" = 200 ] && break
    sleep 2
  done
  [ "$code" = 200 ] || fail "$URL/healthz returned '$code'"
  echo "$URL/healthz: 200"
}

fetch_errors() {
  remote "$HOST" "$DIR" "$MODE" "$STACK_NAME" "$COMPOSE_FILE" <<'EOF'
cd ~/"$1"
if [ "$2" = swarm ]; then
  for s in web scheduler backup; do docker service logs --since 2m "${3}_$s" 2>&1; done
else
  docker compose -f "$4" logs --since 2m 2>&1
fi | grep -E 'Traceback|ERROR|Exception' | tail -5
EOF
}

recent_errors() {
  local errors
  errors="$(fetch_errors || true)"
  if [ -n "$errors" ]; then echo "Recent errors in the logs:"; echo "$errors"; else echo "No errors in the last 2 minutes of logs."; fi
}

prune_images() {
  # Only this project's images; the machine may be shared with other projects.
  if [ "$MODE" = swarm ]; then
    ssh "${SSH_OPTS[@]}" "$HOST" "docker images $IMAGE_REPO --format '{{.Repository}}:{{.Tag}}' | grep -v ':<none>\$' \
      | tail -n +$((KEEP_IMAGES + 1)) | xargs -r docker rmi >/dev/null 2>&1" || true
  else
    ssh "${SSH_OPTS[@]}" "$HOST" "docker image prune -f --filter label=com.docker.compose.project=$PROJECT >/dev/null" || true
  fi
}

# ---------------------------------------------------------------- commands

cmd_check() {
  step "Server $HOST (~/$DIR, MODE=$MODE)"
  ssh "${SSH_OPTS[@]}" "$HOST" true || fail "cannot ssh to $HOST without a password."
  remote "$HOST" "$DIR" "$VOLUME" $CHECK_URLS <<'EOF'
dir="$1" volume="$2"; shift 2
echo "docker   $(docker version --format '{{.Server.Version}}' 2>&1 | head -1)"
echo "compose  $(docker compose version --short 2>&1 | head -1)"
echo "swarm    $(docker info --format '{{.Swarm.LocalNodeState}}' 2>&1 | head -1)"
echo "user     uid=$(id -u) gid=$(id -g)"
for f in .env cloudflared/config.yml cloudflared/credentials.json backups REVISION; do
  if [ -e ~/"$dir/$f" ]; then echo "ok       ~/$dir/$f"; else echo "missing  ~/$dir/$f"; fi
done
if [ -f ~/"$dir/.env" ]; then
  uid="$(sed -n 's/^HOST_UID=//p' ~/"$dir/.env" | tail -1)" gid="$(sed -n 's/^HOST_GID=//p' ~/"$dir/.env" | tail -1)"
  if [ "$uid:$gid" = "$(id -u):$(id -g)" ]; then echo "ok       HOST_UID/HOST_GID in .env"; else echo "WRONG    HOST_UID/HOST_GID in .env are $uid:$gid"; fi
fi
if docker volume inspect "$volume" >/dev/null 2>&1; then echo "ok       volume $volume"; else echo "missing  volume $volume"; fi
for url in "$@"; do
  code="$(curl -sS -m 20 -o /dev/null -w '%{http_code}' "$url" 2>/dev/null || true)"
  echo "http ${code:-fail}  $url"
done
EOF
  echo "running  $(running_mode "$HOST" "$STACK_NAME")"
  if [ -n "$URL" ]; then
    echo "public   $URL/healthz: $(curl -sS -m 10 -o /dev/null -w '%{http_code}' "$URL/healthz" 2>/dev/null || echo fail)"
  fi
}

cmd_setup() {
  local from="" env_file="" cf_dir=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --from) from="$(abs_path "${2:?--from needs a config file}")"; shift 2 ;;
      --env) env_file="$(abs_path "${2:?--env needs a file}")"; shift 2 ;;
      --cloudflared) cf_dir="$(abs_path "${2:?--cloudflared needs a directory}")"; shift 2 ;;
      *) fail "unknown setup option: $1" ;;
    esac
  done
  [ -n "$from" ] && load_old_conf "$from"

  step "Checking $HOST"
  ssh "${SSH_OPTS[@]}" "$HOST" "docker version --format '{{.Server.Version}}' >/dev/null && docker compose version >/dev/null" \
    || fail "$HOST needs Docker Engine and Docker Compose v2, usable by this user without sudo."

  step "Creating ~/$DIR"
  ssh "${SSH_OPTS[@]}" "$HOST" "mkdir -p ~/$DIR/backups ~/$DIR/cloudflared && chmod 2775 ~/$DIR/backups && chmod 700 ~/$DIR/cloudflared"

  step "Copying secrets (existing files on $HOST are kept)"
  if ssh "${SSH_OPTS[@]}" "$HOST" "test -f ~/$DIR/.env"; then
    echo "  .env already present"
  elif [ -n "$env_file" ]; then
    ssh "${SSH_OPTS[@]}" "$HOST" "umask 077 && cat > ~/$DIR/.env" < "$env_file"; echo "  .env from $env_file"
  elif [ -n "$from" ]; then
    ssh "${SSH_OPTS[@]}" "$O_HOST" "cat ~/$O_DIR/.env" | ssh "${SSH_OPTS[@]}" "$HOST" "umask 077 && cat > ~/$DIR/.env"
    echo "  .env from $O_HOST"
  else
    fail "no .env on $HOST; pass --from OLD_CONF or --env FILE."
  fi
  if ssh "${SSH_OPTS[@]}" "$HOST" "test -f ~/$DIR/cloudflared/credentials.json"; then
    echo "  cloudflared/ already present"
  elif [ -n "$cf_dir" ]; then
    COPYFILE_DISABLE=1 tar -C "$cf_dir" -cf - config.yml credentials.json | ssh "${SSH_OPTS[@]}" "$HOST" "tar -C ~/$DIR/cloudflared -xpf -"
    echo "  cloudflared/ from $cf_dir"
  elif [ -n "$from" ]; then
    ssh "${SSH_OPTS[@]}" "$O_HOST" "tar -C ~/$O_DIR/cloudflared -cf - config.yml credentials.json" \
      | ssh "${SSH_OPTS[@]}" "$HOST" "tar -C ~/$DIR/cloudflared -xpf -"
    echo "  cloudflared/ from $O_HOST (same tunnel: only one server may run it at a time)"
  else
    fail "no cloudflared/credentials.json on $HOST; pass --from OLD_CONF or --cloudflared DIR."
  fi

  step "Matching HOST_UID/HOST_GID to the user on $HOST"
  remote "$HOST" "$DIR" <<'EOF'
set -e
cd ~/"$1"
for pair in "HOST_UID=$(id -u)" "HOST_GID=$(id -g)"; do
  key="${pair%%=*}"
  if grep -q "^$key=" .env; then sed -i "s/^$key=.*/$pair/" .env; else echo "$pair" >> .env; fi
done
chmod 600 .env cloudflared/credentials.json
grep -E '^HOST_(UID|GID)=' .env | sed 's/^/  /'
EOF

  step "Data volume"
  ensure_volume "$HOST"
  echo "  $VOLUME ready"

  step "Uploading code"
  upload_code "$HOST" "$DIR" "$(git rev-parse --short HEAD)"

  cmd_check
  step "Setup finished"
  echo "Next: 'server/ctl.sh${CONF_ARG} migrate --from OLD_CONF' to bring the database over,"
  echo "  or 'server/ctl.sh${CONF_ARG} deploy' to start with an empty database (recreate the admin with set-admin-password)."
  [ "$MODE" = swarm ] && echo "MODE=swarm: run 'swarm-init' first if 'swarm' above is not active."
  return 0
}

cmd_deploy() {
  local skip_tests=0 allow_dirty=0 dry_run=0 services=""
  for arg in "$@"; do
    case "$arg" in
      --skip-tests) skip_tests=1 ;;
      --allow-dirty) allow_dirty=1 ;;
      --dry-run) dry_run=1 ;;
      -*) fail "unknown deploy option: $arg" ;;
      *) services="$services $arg" ;;
    esac
  done
  services="${services# }"
  [ -n "$services" ] || services="$SERVICES"
  if [ "$MODE" = swarm ] && [ "$services" != "$SERVICES" ]; then
    note "MODE=swarm updates the whole stack; the service list is ignored (unchanged services are not restarted)."
  fi

  step "Checking the working tree"
  local revision ahead
  revision="$(git rev-parse --short HEAD)"
  if [ -n "$(git status --porcelain)" ]; then
    [ "$allow_dirty" -eq 1 ] || fail "uncommitted changes. Commit them first, or pass --allow-dirty."
    revision="$revision-dirty"
    echo "Deploying with uncommitted changes ($revision)."
  fi
  if git rev-parse --abbrev-ref '@{upstream}' >/dev/null 2>&1; then
    git fetch --quiet || echo "Could not reach the remote; skipping the push check."
    ahead="$(git rev-list --count '@{upstream}..HEAD' 2>/dev/null || echo 0)"
    [ "$ahead" -gt 0 ] && echo "Note: $ahead local commit(s) not pushed to GitHub yet."
  fi
  echo "Revision: $revision -> $HOST:~/$DIR (MODE=$MODE)"

  if [ "$skip_tests" -eq 0 ]; then
    step "Running tests"
    uv run pytest -q
  fi

  step "Checking the server"
  require_ready
  require_mode_free

  if [ "$dry_run" -eq 1 ]; then
    step "Dry run: files that would be updated"
    rsync -azn --itemize-changes "${RSYNC_EXCLUDES[@]}" ./ "$HOST:$DIR/" | awk '$1 ~ /^<f/ {print "  " $2}'
    exit 0
  fi

  step "Uploading code"
  upload_code "$HOST" "$DIR" "$revision"

  if [ "$MODE" = swarm ]; then
    step "Building the image and updating stack $STACK_NAME"
    swarm_up "$(swarm_build "$revision")"
  else
    step "Building and restarting $services"
    compose_up "$services"
  fi

  step "Verifying"
  verify_public
  recent_errors
  prune_images
  step "Deployed $revision"
}

cmd_migrate() {
  local from="" force=0 yes=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --from) from="$(abs_path "${2:?--from needs a config file}")"; shift 2 ;;
      --force) force=1; shift ;;
      --yes) yes=1; shift ;;
      *) fail "unknown migrate option: $1" ;;
    esac
  done
  [ -n "$from" ] || fail "migrate needs --from OLD_CONF (the server the data comes from)."
  load_old_conf "$from"
  [ "$O_HOST:$O_DIR" != "$HOST:$DIR" ] || fail "--from points at this same server."

  step "Checking both servers"
  ssh "${SSH_OPTS[@]}" "$O_HOST" "docker volume inspect $VOLUME >/dev/null" || fail "$O_HOST has no volume $VOLUME."
  require_ready
  require_mode_free
  [ "$(running_mode "$HOST" "$STACK_NAME")" = none ] || fail "$HOST is already running pnu-notice; run stop there first."
  ensure_volume "$HOST"
  local existing
  existing="$(ssh "${SSH_OPTS[@]}" "$HOST" "docker run --rm -v $VOLUME:/data alpine ls -A /data")"
  if [ -n "$existing" ] && [ "$force" -eq 0 ]; then
    fail "the volume on $HOST already holds data ($(echo "$existing" | tr '\n' ' ')); pass --force to replace it."
  fi
  echo "From $O_HOST:~/$O_DIR (running: $(running_mode "$O_HOST" "$O_STACK"))  ->  $HOST:~/$DIR (MODE=$MODE)"

  # Build before stopping the old server so the downtime is only the copy and the start.
  local revision image=""
  revision="$(git rev-parse --short HEAD)"
  step "Uploading code and building on $HOST"
  upload_code "$HOST" "$DIR" "$revision"
  if [ "$MODE" = swarm ]; then
    image="$(swarm_build "$revision")"
  else
    ssh "${SSH_OPTS[@]}" "$HOST" "cd ~/$DIR && nice -n 10 docker compose -f $COMPOSE_FILE build --quiet"
  fi

  [ "$yes" -eq 1 ] || confirm "Stop pnu-notice on $O_HOST and move its database to $HOST now?"

  step "Stopping $O_HOST"
  stop_services "$O_HOST" "$O_DIR" "$O_STACK"

  step "Copying the database"
  mkdir -p "$ROOT/server/backups"
  local archive
  archive="$ROOT/server/backups/migrate-$(date +%Y%m%dT%H%M%S).tar.gz"
  ssh "${SSH_OPTS[@]}" "$O_HOST" "docker run --rm -v $VOLUME:/data:ro alpine tar -C /data -czf - ." > "$archive"
  tar -tzf "$archive" | grep -q 'pnu_notice.sqlite3$' \
    || fail "no database in the archive from $O_HOST. It is stopped: restart it with 'server/ctl.sh -c $from start'."
  echo "  local copy: $archive ($(du -h "$archive" | cut -f1))"
  ssh "${SSH_OPTS[@]}" "$HOST" "docker run --rm -i -v $VOLUME:/data alpine sh -c 'find /data -mindepth 1 -delete && tar -C /data -xzf -'" < "$archive"

  step "Starting $HOST"
  if ! start_services "$image"; then
    fail "start failed on $HOST. To go back: 'server/ctl.sh${CONF_ARG} stop' then 'server/ctl.sh -c $from start'."
  fi
  verify_public
  recent_errors
  step "Migrated"
  echo "$O_HOST is stopped; its data volume is untouched and can be restarted with 'server/ctl.sh -c $from start'."
}

cmd_status() {
  step "$HOST (~/$DIR, MODE=$MODE)"
  remote "$HOST" "$DIR" "$PROJECT" "$STACK_NAME" "$COMPOSE_FILE" <<'EOF'
cd ~/"$1" 2>/dev/null || { echo "~/$1 does not exist"; exit 0; }
echo "revision: $(cat REVISION 2>/dev/null || echo unknown)"
if [ -n "$(docker service ls -q --filter "label=com.docker.stack.namespace=$3" 2>/dev/null)" ]; then
  echo "running: swarm stack $3"
  docker service ls --filter "label=com.docker.stack.namespace=$3" --format '  {{.Name}}\t{{.Replicas}}\t{{.Image}}'
fi
if [ -n "$(docker ps -aq --filter "label=com.docker.compose.project=$2")" ]; then
  echo "running: compose"
  docker compose -f "$4" ps --format '  {{.Service}}\t{{.Status}}'
fi
EOF
  if [ -n "$URL" ]; then
    echo "public: $URL/healthz $(curl -sS -m 10 -o /dev/null -w '%{http_code}' "$URL/healthz" 2>/dev/null || echo fail)"
  fi
}

cmd_logs() {
  local service=scheduler follow=""
  for arg in "$@"; do
    case "$arg" in -f) follow=-f ;; -*) fail "unknown logs option: $arg" ;; *) service="$arg" ;; esac
  done
  if [ "$MODE" = swarm ]; then
    ssh -t "${SSH_OPTS[@]}" "$HOST" "docker service logs --tail 100 $follow ${STACK_NAME}_$service"
  else
    ssh -t "${SSH_OPTS[@]}" "$HOST" "cd ~/$DIR && docker compose -f $COMPOSE_FILE logs --tail 100 $follow $service"
  fi
}

cmd_rollback() {
  [ "$MODE" = swarm ] || fail "compose mode keeps no previous image; check out the older commit and run deploy."
  step "Rolling back stack $STACK_NAME"
  for s in web scheduler backup; do
    ssh "${SSH_OPTS[@]}" "$HOST" "docker service rollback --detach=false ${STACK_NAME}_$s" || note "$s: nothing to roll back"
  done
  verify_public
}

cmd_swarm_init() {
  local yes=0
  [ "${1:-}" = --yes ] && yes=1
  local state
  state="$(swarm_state "$HOST")"
  if [ "$state" = active ]; then echo "Docker on $HOST is already in swarm mode."; return 0; fi
  if [ "$yes" -eq 0 ]; then
    echo "This runs on $HOST:"
    echo "  docker swarm init --advertise-addr $SWARM_ADVERTISE_ADDR --default-addr-pool $SWARM_ADDR_POOL"
    echo "It switches the whole Docker daemon to a single-node swarm. Running containers (also other"
    echo "projects') keep running and docker compose keeps working; swarm overlay networks take"
    echo "subnets from $SWARM_ADDR_POOL. Undo with 'docker swarm leave --force' once no stack is left."
    echo "Re-run with --yes to do it."
    exit 1
  fi
  step "Initialising swarm on $HOST"
  ssh "${SSH_OPTS[@]}" "$HOST" "docker swarm init --advertise-addr $SWARM_ADVERTISE_ADDR \
    --default-addr-pool $SWARM_ADDR_POOL --default-addr-pool-mask-length 24 >/dev/null && docker info --format 'swarm: {{.Swarm.LocalNodeState}}'"
  echo "Next: 'server/ctl.sh${CONF_ARG} switch swarm'."
}

cmd_switch() {
  local target="${1:-}"
  case "$target" in compose|swarm) ;; *) fail "usage: switch compose|swarm" ;; esac
  require_ready
  local running
  running="$(running_mode "$HOST" "$STACK_NAME")"
  if [ "$running" = "$target" ]; then
    [ "$MODE" = "$target" ] || set_conf_mode "$target"
    echo "$HOST already runs $target."; return 0
  fi
  if [ "$target" = swarm ] && [ "$(swarm_state "$HOST")" != active ]; then
    fail "Docker on $HOST is not in swarm mode; run swarm-init first."
  fi

  # Uses the code already on the server (the last deploy); the database volume is shared by both modes.
  local image=""
  MODE="$target"
  if [ "$target" = swarm ]; then
    step "Building the swarm image from the deployed code"
    image="$(swarm_build "$(ssh "${SSH_OPTS[@]}" "$HOST" "cat ~/$DIR/REVISION 2>/dev/null || echo unknown")")"
  fi
  step "Stopping $running on $HOST"
  stop_services "$HOST" "$DIR" "$STACK_NAME"
  step "Starting under $target"
  start_services "$image"
  set_conf_mode "$target"
  verify_public
}

cmd_start() {
  require_ready
  require_mode_free
  step "Starting ($MODE) on $HOST"
  start_services ""
  verify_public
}

cmd_stop() {
  step "Stopping pnu-notice on $HOST (the database is kept)"
  stop_services "$HOST" "$DIR" "$STACK_NAME"
  echo "Stopped."
}

# ---------------------------------------------------------------- main

CONF="${SERVER_CONF:-$ROOT/server/server.conf}"
CONF_ARG=""
if [ "${1:-}" = -c ]; then
  [ $# -ge 2 ] || fail "-c needs a config file."
  CONF="$(abs_path "$2")" CONF_ARG=" -c $2"
  shift 2
fi
COMMAND="${1:-help}"
if [ $# -gt 0 ]; then shift; fi
case "$COMMAND" in help|-h|--help) usage; exit 0 ;; esac

cd "$ROOT"
load_conf "$CONF"
case "$COMMAND" in
  check) cmd_check ;;
  setup) cmd_setup "$@" ;;
  deploy) cmd_deploy "$@" ;;
  migrate) cmd_migrate "$@" ;;
  start) cmd_start ;;
  stop) cmd_stop ;;
  status) cmd_status ;;
  logs) cmd_logs "$@" ;;
  rollback) cmd_rollback ;;
  swarm-init) cmd_swarm_init "$@" ;;
  switch) cmd_switch "$@" ;;
  *) usage; fail "unknown command: $COMMAND" ;;
esac
