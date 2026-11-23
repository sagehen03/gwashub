#!/usr/bin/env bash
# Deploy GWASHub to the shared host (docs/SHARED_HOST.md): build the website and the pipeline images, deploy the
# Gwashub-<env>-Shared stack, ship the committed code to a new release folder on the host over SSH, switch to it and check the
# site. Stops at the first failure. Run devtools/shared_host_setup.sh once first.
#
#   GWASHUB_SSH_TARGET=ec2-user@<host> ./devtools/shared_host_deploy.sh broad            # deploy HEAD
#   GWASHUB_SSH_TARGET=ec2-user@<host> ./devtools/shared_host_deploy.sh broad rollback   # back to the previous release
#
# Deploying needs a clean git tree and the environment's AWS account (AWS_PROFILE, default gwashub-<env>). Both modes first check
# that GWASHUB_SSH_TARGET is the configured host. A rollback restores code only, never the database: it refuses when the live
# release added a migration the previous one lacks (that release would not start on the upgraded database); fix forward then.
set -euo pipefail
cd "$(dirname "$0")/.."
ENV_NAME=${1:?usage: shared_host_deploy.sh ENV [rollback]}
MODE=${2:-deploy}
# shellcheck source=devtools/lib/shared_host_common.sh
source devtools/lib/shared_host_common.sh
# shellcheck source=devtools/lib/pipeline_images.sh
source devtools/lib/pipeline_images.sh
[ "$MODE" = deploy ] || [ "$MODE" = rollback ] || die "usage: shared_host_deploy.sh ENV [rollback]"
export JSII_SILENCE_WARNING_DEPRECATED_NODE_VERSION=1
shared_host() { .venv/bin/python devtools/shared_host.py "$@"; }  # the checks and renderers (every command takes the environment first)
HOST_IP=$(cfg host_private_ip)
API_PORT=$(cfg api_port)
PREFIX=/opt/gwashub

# restart_and_wait: restart both units, then wait (about 60 s) until both are active and the API answers on the host's own
# address. systemd calls a service active before uvicorn listens, so the site check must not run before this returns.
restart_and_wait() {
  remote sudo systemctl restart gwashub-api gwashub-worker
  remote sudo bash -s -- "$HOST_IP" "$API_PORT" <<'EOF'
ip=$1 port=$2
for _ in $(seq 1 30); do
  if systemctl is-active --quiet gwashub-api && systemctl is-active --quiet gwashub-worker \
     && curl -fsS --max-time 3 "http://$ip:$port/api/health" 2>/dev/null; then
    echo
    sleep 3  # a service that crashes on start is "active" until it dies: it must still be up
    if systemctl is-active --quiet gwashub-api && systemctl is-active --quiet gwashub-worker; then exit 0; fi
    break
  fi
  sleep 2
done
echo "gwashub-api and gwashub-worker are not both up and answering at http://$ip:$port/api/health" >&2
systemctl --no-pager status gwashub-api gwashub-worker >&2 || true
journalctl -u gwashub-api -n 50 --no-pager >&2 || true
journalctl -u gwashub-worker -n 20 --no-pager >&2 || true
exit 1
EOF
}

# previous_release: point current at the newest release older than the one now live, and print its name. Nothing changes
# if there is none, or if the live release added a migration the older one lacks: that migration has been applied, and
# the older code would meet a database at a revision it doesn't know, so neither service would start.
previous_release() {
  remote sudo bash -s -- "$PREFIX" <<'EOF'
set -euo pipefail
shopt -s nullglob
export LC_ALL=C  # release names compare as plain text
prefix=$1
link=$(readlink "$prefix/current") || { echo "$prefix/current is not a symlink: nothing has been deployed" >&2; exit 1; }
current=${link##*/}
previous=
for dir in "$prefix"/releases/*/; do
  name=$(basename "$dir")
  [[ $name =~ ^[0-9]{8}-[0-9]{6}-[0-9a-f]+$ ]] || continue
  [[ $name < $current ]] || continue
  [ -x "$dir/infra/shared_host/gwashub-run" ] && [ -x "$dir/.venv/bin/python" ] || continue
  if [[ -z $previous || $name > $previous ]]; then previous=$name; fi
done
[ -n "$previous" ] || { echo "There is no usable release older than $current to roll back to" >&2; exit 1; }
added=
for f in "$prefix/releases/$current/api/migrations/versions/"*.py; do
  [ -e "$prefix/releases/$previous/api/migrations/versions/${f##*/}" ] || added="$added ${f##*/}"
done
if [ -n "$added" ]; then
  echo "Not rolling back: $current added database migrations that $previous lacks:$added" >&2
  echo "They have been applied, migrations are never rolled back, and $previous would not start on that database." >&2
  echo "Fix forward instead: deploy a corrected commit. Nothing was changed." >&2
  exit 1
fi
ln -sfn "releases/$previous" "$prefix/current"
echo "$previous"
EOF
}

if [ "$MODE" = rollback ]; then
  echo "Rolling $ENV_NAME back to the previous release."
  echo "Database migrations are not rolled back: this refuses if the live release added one."
  remote true || die "Cannot SSH to $GWASHUB_SSH_TARGET without prompts"
  remote sudo -n true || die "sudo on $GWASHUB_SSH_TARGET needs a password: it must work with 'sudo -n'"
  check_host_identity
  PREVIOUS=$(previous_release)
  echo "current -> releases/$PREVIOUS"
  restart_and_wait
  shared_host check-site "$ENV_NAME"
  echo "Rolled back to $PREVIOUS. Database migrations were not rolled back."
  exit 0
fi

# ---- the deploy ----
TAG=$(date -u +%Y%m%d-%H%M%S)-$(git rev-parse --short HEAD)  # release folder and image tag
[[ $TAG =~ ^[0-9]{8}-[0-9]{6}-[0-9a-f]+$ ]] || die "Could not make a release name (got '$TAG')"
RELEASE_DIR=$PREFIX/releases/$TAG
RELEASE_STARTED=0  # a release folder exists on the host
LIVE=0             # current points at it (the rollback command is worth printing)

on_exit() {
  local code=$?
  [ "$code" -ne 0 ] || return 0
  if [ "$LIVE" = 1 ]; then
    echo >&2
    echo "The deploy failed after $TAG went live. To go back to the previous release (refused if $TAG added a migration):" >&2
    echo "  GWASHUB_SSH_TARGET=$GWASHUB_SSH_TARGET ./devtools/shared_host_deploy.sh $ENV_NAME rollback" >&2
  elif [ "$RELEASE_STARTED" = 1 ]; then
    # Not live and not finished: remove it, so it does not take the place of a good release the prune keeps for rollback.
    # Unless current points at it after all (ln can succeed on the host while ssh reports a failure): the host checks.
    local rc=0
    remote sudo -u gwashub bash -s -- "$PREFIX" "$TAG" <<'EOF' || rc=$?
[ "$(readlink "$1/current")" != "releases/$2" ] || exit 3
rm -rf "$1/releases/$2"
EOF
    if [ "$rc" = 0 ]; then
      echo "The deploy failed before $TAG went live: removed the unfinished release $RELEASE_DIR" >&2
    elif [ "$rc" = 3 ]; then
      echo "The deploy failed, but $PREFIX/current points at $TAG after all, so it was left in place. To go back to the" \
        "previous release (refused if $TAG added a migration):" >&2
      echo "  GWASHUB_SSH_TARGET=$GWASHUB_SSH_TARGET ./devtools/shared_host_deploy.sh $ENV_NAME rollback" >&2
    else
      echo "The deploy failed before $TAG went live, and its unfinished release $RELEASE_DIR could not be removed: remove it" \
        "by hand, unless $PREFIX/current points at it" >&2
    fi
  fi
}
trap on_exit EXIT

echo "== 1. checks: AWS account and git tree"
shared_host preflight "$ENV_NAME"

echo "== 2. the host: SSH, sudo, which machine it is, setup"
remote true || die "Cannot SSH to $GWASHUB_SSH_TARGET without prompts"
remote sudo -n true || die "sudo on $GWASHUB_SSH_TARGET needs a password: it must work with 'sudo -n'"
check_host_identity
remote test -d "$PREFIX/releases" || die "$PREFIX/releases is missing on the host: run ./devtools/shared_host_setup.sh $ENV_NAME first"

echo "== 3. website"
rm -rf web/.output  # a failed build must not leave an old one to ship
(cd web && npx nuxi generate >/dev/null)
# The site's files are what the host serves: never ship an empty or half-written build.
for f in index.html 200.html paused.html; do
  [ -f "web/.output/public/$f" ] || die "The website build has no $f: not deploying (rebuild with: cd web && npx nuxi generate)"
done

echo "== 4. pipeline images"
ACCOUNT=$(cfg account)  # preflight checked that the AWS credentials are for this account
REGISTRY=$ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com
aws ecr get-login-password | docker login --username AWS --password-stdin "$REGISTRY" >/dev/null
push_pipeline_images "$REGISTRY" "$TAG"
[ "$PIPELINE_IMAGES" != "{}" ] || die "No pipeline images were built: not deploying"

echo "== 5. infrastructure: Gwashub-$ENV_NAME-Shared"
[ -x infra/.venv/bin/python ] || { python3 -m venv infra/.venv && infra/.venv/bin/pip install -q -r infra/requirements.txt; }
# Service-linked roles that first-time Batch and Spot use needs (no-ops once they exist).
for svc in ecs.amazonaws.com batch.amazonaws.com spot.amazonaws.com; do
  aws iam create-service-linked-role --aws-service-name "$svc" >/dev/null 2>&1 || true
done
ensure_deploy_bucket
cd infra
npx --yes aws-cdk@2 deploy "Gwashub-$ENV_NAME-Shared" -c env="$ENV_NAME" -c pipeline_images="$PIPELINE_IMAGES" \
  --require-approval never --outputs-file "cdk-outputs-$ENV_NAME.json"
cd ..

echo "== 6. release $TAG: code, website, virtualenv"
RELEASE_STARTED=1
remote sudo -u gwashub mkdir -p "$RELEASE_DIR/web"
# HEAD is what preflight checked is on disk. The website source stays out: the folder web/ is served as it is, so it holds
# only the built site.
git archive --format=tar HEAD -- . ':(exclude)web' | remote sudo -u gwashub tar -x -C "$RELEASE_DIR"
tar -c -C web/.output/public . | remote sudo -u gwashub tar -x -C "$RELEASE_DIR/web"
remote sudo -u gwashub /usr/bin/python3.12 -m venv "$RELEASE_DIR/.venv"
remote sudo -u gwashub "$RELEASE_DIR/.venv/bin/pip" install --disable-pip-version-check --no-cache-dir --progress-bar off \
  -r "$RELEASE_DIR/images/app/requirements.txt"

echo "== 7. the env file"
# Rendered here first, and only into a variable: a failure (expired credentials, a missing output) must not reach the host as an
# empty file. The secrets travel on stdin only. On the host the file is made 0600 from the start, handed to gwashub, then
# moved over the old one in one step.
ENV_FILE=$(shared_host render-env "$ENV_NAME")
[ -n "$ENV_FILE" ] || die "render-env printed nothing"
# shellcheck disable=SC2016  # the $ in the script are for the host's shell
printf '%s\n' "$ENV_FILE" | remote sudo bash -c \
  'umask 077; tmp=$(mktemp /etc/gwashub/env.XXXXXX) || exit 1; cat > "$tmp" && chown gwashub:gwashub "$tmp" && mv -f "$tmp" /etc/gwashub/env || { rm -f "$tmp"; exit 1; }'
unset ENV_FILE
echo "  /etc/gwashub/env written"

echo "== 8. systemd units"
for kind in api worker; do
  UNIT=$(shared_host render-unit "$ENV_NAME" "$kind")
  [ -n "$UNIT" ] || die "render-unit $kind printed nothing"
  printf '%s\n' "$UNIT" | remote sudo tee "/etc/systemd/system/gwashub-$kind.service" >/dev/null
done
remote sudo systemctl daemon-reload
remote sudo systemctl enable gwashub-api gwashub-worker

echo "== 9. database migrations (from the new release)"
remote sudo -u gwashub "$RELEASE_DIR/infra/shared_host/gwashub-run" python -m gwashub_api.migrate

echo "== 10. switch to $TAG and restart"
remote sudo ln -sfn "releases/$TAG" "$PREFIX/current"
LIVE=1  # from here a failure prints the rollback command
restart_and_wait

echo "== 11. memory limits"
remote sudo systemctl show gwashub-api gwashub-worker -p MemoryMax -p MemoryLimit | shared_host limits-ok "$ENV_NAME"
echo "  in force for both units"

echo "== 12. the site"
shared_host check-site "$ENV_NAME"

echo "== 13. old releases"
LISTING=$(remote ls -1 "$PREFIX/releases")
RELEASES=()
while IFS= read -r name; do RELEASES+=("$name"); done <<< "$LISTING"
PRUNE=$(shared_host prune "$ENV_NAME" "$TAG" "${RELEASES[@]}")
TO_REMOVE=()
while IFS= read -r name; do [ -z "$name" ] || TO_REMOVE+=("$name"); done <<< "$PRUNE"
if [ "${#TO_REMOVE[@]}" -eq 0 ]; then
  echo "  nothing to remove"
else
  for name in "${TO_REMOVE[@]}"; do
    [[ $name =~ ^[0-9]{8}-[0-9]{6}-[0-9a-f]+$ ]] || die "Not removing '$name': not a release name"
    echo "  removing $name"
    remote sudo -u gwashub rm -rf "$PREFIX/releases/$name"
  done
fi

echo
echo "Deployed $TAG to $ENV_NAME: $(cfg site_url)"
echo "Previous release, if needed (refused if $TAG added a migration):" \
  "GWASHUB_SSH_TARGET=$GWASHUB_SSH_TARGET ./devtools/shared_host_deploy.sh $ENV_NAME rollback"
