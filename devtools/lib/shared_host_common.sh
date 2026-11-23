# shellcheck shell=bash
# What devtools/shared_host_{setup,deploy,admin}.sh have in common. Source it from the repository root with ENV_NAME set
# (the environment's name in SHARED_HOSTS, infra/gwashub_infra/config.py); it defines functions and runs only local checks.
#
# The host is reached over SSH at $GWASHUB_SSH_TARGET (for example ec2-user@<host>). That address is never in git.

die() { echo "$*" >&2; exit 1; }

: "${GWASHUB_SSH_TARGET:?Set GWASHUB_SSH_TARGET to the SSH login for the shared host, for example ec2-user@<host>}"

# cfg <field>: a field of the environment's SharedHostConfig (region, account, host_private_ip, api_port, ...).
# An unknown environment stops the script with the list of known ones.
cfg() {
  .venv/bin/python -c 'import sys; from devtools.shared_host import load_config; print(getattr(load_config(sys.argv[1]), sys.argv[2]))' \
    "$ENV_NAME" "$1"
}

# The AWS account is the environment's own; the region always comes from its configuration, so a stale AWS_REGION in this
# shell cannot send images or stacks to the wrong place.
CONFIG_REGION=$(cfg region)
export AWS_PROFILE=${AWS_PROFILE:-gwashub-$ENV_NAME} AWS_REGION=$CONFIG_REGION AWS_DEFAULT_REGION=$CONFIG_REGION

# remote WORD...: run one command on the host, non-interactively. Every word is quoted for the remote shell (printf %q), so a
# value from this machine is never read as shell there. It starts in /opt/gwashub, a folder the gwashub user can read (the
# login folder is not, and `sudo -u gwashub` keeps it). Standard input is passed through: pipe data in, or use a here-document
# with `remote sudo bash -s -- ARG...` to run a script.
remote() {
  ssh -o BatchMode=yes -o ConnectTimeout=15 "$GWASHUB_SSH_TARGET" "cd /opt/gwashub 2>/dev/null; $(printf '%q ' "$@")"
}

# check_host_identity: stop unless the machine at $GWASHUB_SSH_TARGET is the environment's host (host_instance_id). Run it
# before anything is written there: a wrong target would otherwise get the gwashub user, a release, and the env file with
# its secrets. The machine is asked for its instance ID by the EC2 instance metadata service (IMDSv2), on the machine
# itself. There is no fallback: its IP address proves nothing, as private addresses repeat across VPCs.
check_host_identity() {
  local want got
  want=$(cfg host_instance_id)
  if ! got=$(remote bash -s <<'EOF'
imds=http://169.254.169.254/latest
token=$(curl -fsS --noproxy '*' --max-time 5 -X PUT -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' "$imds/api/token") &&
  curl -fsS --noproxy '*' --max-time 5 -H "X-aws-ec2-metadata-token: $token" "$imds/meta-data/instance-id"
EOF
  ); then
    die "Could not read the instance ID of $GWASHUB_SSH_TARGET from the EC2 instance metadata service (IMDSv2, on the host:" \
      "http://169.254.169.254), so there is no telling whether it is $ENV_NAME's host $want. It must be that EC2 instance," \
      "with the metadata service on. Nothing was changed there."
  fi
  [ "$got" = "$want" ] || die "$GWASHUB_SSH_TARGET is instance ${got:-(none)}, not $ENV_NAME's host $want" \
    "(host_instance_id in infra/gwashub_infra/config.py): check GWASHUB_SSH_TARGET. Nothing was changed there."
  echo "  $GWASHUB_SSH_TARGET is $want, the host in $ENV_NAME's configuration"
}

# ensure_deploy_bucket: the bucket the stack's template and its one small function go up through, made private the first time.
# With it the stack deploys on the operator's own AWS credentials, so the account needs no `cdk bootstrap`, which would leave
# standing roles (one an administrator) in an account GWASHub shares. Stops if the name belongs to another account.
ensure_deploy_bucket() {
  local bucket account
  bucket=$(cfg deploy_bucket)
  account=$(cfg account)
  aws s3api head-bucket --bucket "$bucket" --expected-bucket-owner "$account" >/dev/null 2>&1 && return 0
  aws s3 mb "s3://$bucket" --region "$AWS_REGION"
  aws s3api put-public-access-block --bucket "$bucket" --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
  aws s3api put-bucket-tagging --bucket "$bucket" --tagging "TagSet=[{Key=project,Value=gwashub},{Key=environment,Value=$ENV_NAME}]"
}
