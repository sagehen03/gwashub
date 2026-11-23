# GWASHub on a shared host

How to run GWASHub on hosts the Broad already pays for: the EC2 machine that runs the data-registry API, a new database
on the existing MySQL server, and the existing nginx proxy. Only the parts those can't supply go in AWS (files, sign-in,
Batch, a queue), for about $5 a month fixed. This is the second deployment target; the first, GWASHub with its own
network, database and load balancer, is `docs/AWS_FULL.md`. The code is the same (the website, the pipelines, the meta-analysis
methods, the admin commands); what differs is where it runs. Nothing moves between the two systems: no data migration, separate user accounts.

This guide is for the Broad staff who deploy it: you need SSH and passwordless sudo on the shared host, and admin
rights in the Broad AWS account. The environment is called `broad` below; its settings are `SHARED_HOSTS["broad"]` in
`infra/gwashub_infra/config.py`.

## Why it's built this way

- **Reuse what's paid for.** The Broad's host, MySQL server and proxy already exist and have room. GWASHub's own
  stack costs about $120 a month in dev and an estimated $457 in prod (`docs/PRODUCTION_ROADMAP.md`); this costs about
  $5 and then storage and compute as used.
- **data-registry must not notice.** GWASHub changes none of data-registry's code, packages, systemd units, nginx
  blocks or database. It runs as its own Linux user in its own folder, on its own port, with memory and CPU limits on both services.
- **The dev sign-in can never come up.** `GWASHUB_AUTH` defaults to `dev`, which lets anyone in by typing an email address. The
  wrapper that starts both services (`infra/shared_host/gwashub-run`) reads the env file as data and never runs it: every line
  must be `KEY='value'` (blank lines and lines starting with `#` aside), each value is passed to the service exactly as written,
  a `GWASHUB_AUTH` line is ignored with a warning, and any other line stops the wrapper before anything runs (the message gives
  the line's number, never its content). The wrapper then sets `GWASHUB_AUTH=cognito` itself, so neither the env file, however
  it is edited, nor the caller's environment can turn the dev sign-in on. The units don't start without the env file, and
  every deploy checks that `/api/auth/mode` says `cognito` and that `/api/auth/login` and `/api/auth/users` don't answer.
- **Secrets reach the host only through the deploy.** The deploy reads them from Secrets Manager with *your* AWS
  credentials and writes them into `/etc/gwashub/env` (mode 0600, owned by `gwashub`) over SSH, on stdin. The host's role gets
  no Secrets Manager access from GWASHub, and each secret's own resource policy denies that role every Secrets Manager action,
  so a policy on the shared role that isn't GWASHub's can't grant it either. Any process on the host can use that role, so if
  it could read the session key (which can forge a sign-in as anyone) the 0600 file would be pointless. Rotating a secret
  means redeploying.
- **Files still never pass through the API.** Browsers upload to S3 in parts with presigned URLs, as in `docs/AWS_FULL.md`,
  so nginx only carries small requests (the bulk-upload manifest, JSON).
- **Region: us-east-1.** The Broad's account and network are there, so cohort data is stored in the US. The production
  plan had UK storage pending information-governance sign-off (`docs/PRODUCTION_ROADMAP.md` 1.1); HERMES cohorts may
  need to agree to this.

## What runs where

| Piece | Where and how |
|---|---|
| Website and API | One uvicorn process on the shared host, systemd unit `gwashub-api`. The API serves the built website itself (`GWASHUB_WEB_DIR`), so nginx proxies one upstream |
| Worker | `python -m gwashub_api.worker` on the same host, systemd unit `gwashub-worker`: reads Batch job events from SQS and records results |
| Database | Database `gwashub` on the existing MySQL server, with its own MySQL user that has rights on that database only. `utf8mb4`, collation `utf8mb4_0900_as_cs` (case-sensitive, as on Postgres): the migrations, and so both services, refuse a database whose collation isn't case-sensitive. Over TLS, verified against the RDS CA bundle |
| Domain and TLS | A new server block on the existing nginx proxy, with its own certificate, proxying every path to `<host private IP>:<api_port>` |
| Sign-in | A new Cognito user pool `gwashub-broad` (invite-only, MFA for everyone), on a Cognito prefix domain. Sign-in returns only to `site_url` |
| Files | Three S3 buckets (uploads, results, resources): KMS-encrypted, versioned, private, TLS only. The uploads bucket's CORS rule names `site_url` |
| QC runs and meta-analyses | AWS Batch on EC2 as in `docs/AWS_FULL.md`: Graviton and x86-64 queues `gwashub-broad` and `gwashub-broad-x86` (Spot), meta-analyses on On-Demand (`-ondemand`, `-ondemand-x86`), scaling to zero. In **public subnets the stack makes in the existing VPC**, with public IPs (the VPC has no NAT gateway) |
| Job events | EventBridge to SQS (with a dead-letter queue) to the worker |
| Pipeline images | ECR repositories `gwashub-images/<owner>/<image>` in the Broad account, made by the deploy; job definitions pinned by digest. There is no `gwashub/app` image: the API runs from a virtualenv |
| Logs | Batch jobs: CloudWatch log group `/gwashub/broad/app`. API and worker: journald on the host |
| Secrets | Secrets Manager: `gwashub/broad/session`, `gwashub/broad/cognito-client`, `gwashub/broad/db`. Each one's resource policy denies the host's role |

The stack is `Gwashub-broad-Shared` (`infra/gwashub_infra/shared_host_stack.py`). It creates no VPC, NAT gateway, database,
load balancer, container service, website bucket or jump host, and has no hibernate mode (idle cost is already about $5). Besides the
pieces above it adds **one inbound rule** to the host's security group (the API port, from the nginx machine's address only) and **one inline
policy**, `gwashub-broad-host`, to the host's existing instance role (see "One-off setup", step 1).

**On the host**

| Path | What |
|---|---|
| user `gwashub` | No login shell. Owns everything below; runs both services |
| `/opt/gwashub/releases/<tag>` | One folder per deploy, named `<UTC yyyymmdd-hhmmss>-<git short sha>`: the committed code, the built website (`web/`) and a virtualenv (`.venv/`), about 1 GB. The current release and the 2 before it are kept, so there are 4 (about 4 GB) during a deploy, until the oldest is removed |
| `/opt/gwashub/current` | Symlink to the live release |
| `/opt/gwashub/data` | Scratch space, home and plot cache for the services |
| `/opt/gwashub/rds-global-bundle.pem` | The RDS CA bundle the database connection verifies against |
| `/etc/gwashub/env` | The settings and secrets (0600, owner `gwashub`), written by every deploy |
| `/etc/systemd/system/gwashub-api.service`, `gwashub-worker.service` | The units, written by every deploy: `Restart=always`, `MemoryMax`/`MemoryLimit` and `CPUQuota` from `config.py`, `ProtectHome`, `ProtectSystem=full`, `PrivateTmp`. The API listens on the host's private IP only, and believes `X-Forwarded-*` headers only from the nginx machine |

## The facts to collect

None of these are in any repository; the Broad operators know them. Each goes into one field of `SHARED_HOSTS["broad"]`.

**Every AWS CLI command in this guide names `--region us-east-1`** (IAM ignores it), so a different default region in your
shell or profile can't send it elsewhere. Set the profile first, in each shell: `export AWS_PROFILE=gwashub-broad`.

| Fact | How to find it | Field |
|---|---|---|
| The Broad account ID, and whether any service control policy blocks Batch, Cognito, ECR, KMS, SQS, EventBridge, Secrets Manager, Lambda (CDK's custom resources), IAM, EC2, S3 or CloudWatch Logs in us-east-1 | `aws sts get-caller-identity --query Account --output text --region us-east-1`; ask whoever manages the organisation about SCPs | `account`, `region` (`us-east-1`) |
| The shared host: instance ID, private IP, security group, VPC | `aws ec2 describe-instances --instance-ids i-... --region us-east-1 --query 'Reservations[0].Instances[0].[PrivateIpAddress,SecurityGroups[].GroupId,VpcId,Placement.AvailabilityZone,InstanceType,IamInstanceProfile.Arn,MetadataOptions.HttpEndpoint]'` (the last must be `enabled`: the scripts ask the host's instance metadata service which machine it is) | `host_instance_id`, `host_private_ip`, `host_security_group_id`, `vpc_id` |
| Its instance role, and whether the role is attached to any other machine | `aws iam get-instance-profile --instance-profile-name <name from the ARN> --region us-east-1 --query 'InstanceProfile.Roles[].RoleName'`; `aws ec2 describe-iam-instance-profile-associations --region us-east-1 --query 'IamInstanceProfileAssociations[].[InstanceId,IamInstanceProfile.Arn]'` | `host_role_name` |
| The host's size and headroom (memory, CPUs, free disk: about 4 GB for GWASHub's releases, as a deploy holds 4 of them), its Python (3.12 needed) and Amazon Linux version | `shared_host_setup.sh` prints all of them (below); `systemctl status` of data-registry's services (`dr-api-prd` on 443 and `dr-api-dev` on 8000 when this was written) shows what they use | `api_memory`, `api_cpu`, `worker_memory`, `worker_cpu` (defaults `1536M`, `100%`, `512M`, `50%`: leave data-registry what it needs) |
| The nginx machine: its address, how it reaches the host, how certificates are issued there | Ask. It must reach the host's private IP on `api_port` | `nginx_cidr` (one address, `/32`); the certificate and key paths for the nginx block |
| The address for GWASHub and who manages its DNS | Ask | `site_url` (`https://...`, no trailing slash) |
| Free address space in the VPC, and its internet gateway | `aws ec2 describe-vpcs --region us-east-1`; `aws ec2 describe-subnets --region us-east-1 --filters Name=vpc-id,Values=<vpc>` shows what's taken; `aws ec2 describe-internet-gateways --region us-east-1 --filters Name=attachment.vpc-id,Values=<vpc>` | `batch_subnet_cidrs` (one range per zone; a /24 each is plenty), `internet_gateway_id` |
| Zones that offer the Batch machine families (`m7g`, `r7g`, `m7i`, `r7i`) | `aws ec2 describe-instance-type-offerings --region us-east-1 --location-type availability-zone --filters Name=instance-type,Values=m7g.large,r7g.large,m7i.large,r7i.large --query 'InstanceTypeOfferings[].[Location,InstanceType]' --output text`: pick two or three zones that list all four | `availability_zones` (same order as `batch_subnet_cidrs`) |
| Spare EC2 quota for Batch: Spot and On-Demand vCPUs in us-east-1 (a job takes up to 16 vCPU and 128 GiB) | Service Quotas console: "Running On-Demand Standard instances" and "All Standard Spot Instance Requests" | `batch_max_vcpus` (default 64: the cap of each of the four compute environments, so Spot and On-Demand can each reach 2 x 64 vCPUs at once) |
| The MySQL server: its exact version, whether it accepts TLS, who can create a database and user on it, and its DNS name | `aws rds describe-db-instances --region us-east-1 --query 'DBInstances[].[DBInstanceIdentifier,Endpoint.Address,EngineVersion]' --output table` | `db_host`, `db_port` (default 3306) |
| A port on the host nothing else uses (not 443 or 8000) | `shared_host_setup.sh` checks it | `api_port` |

**`db_host` must be the MySQL server's own DNS name** (the endpoint the RDS console shows), **not an IP address and not a
CNAME** that points at it. The connection verifies the server's TLS certificate name against the RDS CA bundle, so any other name fails with a
certificate error.

Add the entry under `SHARED_HOSTS` in `infra/gwashub_infra/config.py` (it starts empty) and **commit it**: the deploy
ships the committed code, and refuses an uncommitted tree (below).

```python
SHARED_HOSTS["broad"] = SharedHostConfig(
    name="broad", account="<12 digits>", region="us-east-1", site_url="https://<site address>",
    vpc_id="vpc-...", availability_zones=("us-east-1a", "us-east-1b"), batch_subnet_cidrs=("10.x.y.0/24", "10.x.z.0/24"),
    internet_gateway_id="igw-...", host_instance_id="i-...", host_role_name="<role name>", host_security_group_id="sg-...",
    host_private_ip="10.x.y.z", nginx_cidr="10.x.y.z/32", api_port=<free port>, db_host="<server>.<id>.us-east-1.rds.amazonaws.com",
    banner="")
```

`banner` is the strip across the top of every page (empty for none). The other fields (`batch_spot`, `meta_on_demand`,
`batch_volume_gb`, `log_days`, `noncurrent_version_days`) have defaults; see the class. Nothing here is a secret, and the host's SSH address
is not in it: that is `GWASHUB_SSH_TARGET`, set in your shell and never in git.

## What you need on your laptop

- **AWS CLI v2**, signed in with admin rights in the Broad account. The scripts use the profile `gwashub-broad` unless `AWS_PROFILE` is set, and always
  use the region from `config.py`, whatever your shell says.
- **Docker**, running: the deploy builds the pipeline images, most for arm64 and `vcf` and `ldsc` for x86-64, so on a laptop of the
  other architecture Docker builds them under emulation (slow the first time).
- **Node.js** (the deploy runs `npx nuxi generate` for the website and `npx aws-cdk@2` for the stack) and **Python 3**.
- **The website's dependencies**: run `cd web && npm install` once before the first deploy, and again after the web dependencies change.
  The deploy doesn't install them (`web/node_modules` is git-ignored, so a fresh checkout has none), and without them step 3, the website build, fails.
- The repository's virtualenv: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`. The deploy also makes
  `infra/.venv` from `infra/requirements.txt` if it is missing; that is what runs the CDK app.
- **SSH to the host with a key and no prompts** (the scripts use `BatchMode`), and passwordless sudo there (`sudo -n`):

  ```bash
  export GWASHUB_SSH_TARGET=ec2-user@<host>
  ```

## One-off setup

Do these in order.

### 1. Check the shared IAM role

The stack attaches an inline policy, `gwashub-broad-host`, to the **existing role data-registry uses** (`host_role_name`).
Before the first deploy, look at what is on it:

```bash
aws iam list-role-policies --role-name <host_role_name> --region us-east-1
for p in $(aws iam list-role-policies --role-name <host_role_name> --region us-east-1 --query 'PolicyNames[]' --output text); do
  aws iam get-role-policy --role-name <host_role_name> --policy-name "$p" --region us-east-1 --query PolicyDocument --output json \
    | python3 -c 'import json,sys; print(len(json.dumps(json.load(sys.stdin), separators=(",", ":"))), "characters")'
done
```

Check three things:

1. **The size.** IAM allows 10,240 characters in total across a role's inline policies. GWASHub's is about 3,000. If the
   existing ones plus 3,000 come near 10,240, the stack update fails; the fix is a change to the code (a managed policy
   instead), so stop and ask.
2. **Who manages the role.** If data-registry's own infrastructure code (CloudFormation, CDK or Terraform) manages it,
   check that it will not remove an inline policy it didn't create. Terraform's `inline_policy` blocks on `aws_iam_role` are exclusive,
   for example: the next apply deletes everything not listed. Tell the people who own that code that
   `gwashub-broad-host` is GWASHub's and must stay. After any change to that role, check the policy is still there
   (`aws iam get-role-policy --role-name <host_role_name> --policy-name gwashub-broad-host --region us-east-1`). If it has gone, GWASHub's S3,
   Batch and SQS calls fail with AccessDenied in the logs; find out what removed it before putting it back.
3. **What it already reaches in Secrets Manager and KMS.** Record the answers:

   ```bash
   aws iam get-role --role-name <host_role_name> --region us-east-1 --query Role.Arn --output text
   aws iam list-attached-role-policies --role-name <host_role_name> --region us-east-1
   aws iam simulate-principal-policy --region us-east-1 --policy-source-arn <the role ARN above> \
     --action-names secretsmanager:GetSecretValue kms:Decrypt --query 'EvaluationResults[].[EvalActionName,EvalDecision]' --output table
   ```

   `allowed` means a policy that isn't GWASHub's already gives the role that action. Each GWASHub secret's own resource policy
   denies the role every Secrets Manager action, which wins over any allow, so it still can't read them; but tell the role's owners
   what you found. That deny names the role as `arn:aws:iam::<account>:role/<host_role_name>`: if the ARN `get-role` prints has a
   path between `role/` and the name, the deny wouldn't match it, so stop and ask.

### 2. Prepare the host

```bash
GWASHUB_SSH_TARGET=ec2-user@<host> ./devtools/shared_host_setup.sh broad
```

Safe to repeat. It changes only the `gwashub` user, `/opt/gwashub` and `/etc/gwashub`, and reads the rest. Before it changes anything
it checks that `GWASHUB_SSH_TARGET` is the configured host: it asks the machine for its instance ID through the EC2 instance
metadata service (IMDSv2, `http://169.254.169.254`, from the host itself) and stops unless that is `host_instance_id`. The deploy and
the rollback do the same. If the metadata service can't be reached the scripts stop; they don't fall back to the IP address, as private
addresses repeat across VPCs. Then it creates the user and folders, downloads the RDS CA bundle, and checks the host is fit: Python
3.12 can make a virtualenv with pip, the API port is free (or already `gwashub-api`'s), and it prints the OS, systemd version, memory, disk and CPUs. Use those to
confirm `api_memory` and the other limits are fair to data-registry. If it stops, it says what to install or change.

### 3. The first deploy: it stops at the migrations, on purpose

The database password is made by the stack, so the MySQL user (next step) can't exist before the stack does. Run the
deploy once to create the stack and its secrets. Commit the `SHARED_HOSTS` entry first: the deploy needs a clean git tree
(see "Deploying" below, which also says what each step does):

```bash
GWASHUB_SSH_TARGET=ec2-user@<host> ./devtools/shared_host_deploy.sh broad
```

It will get as far as step 9 (database migrations) and fail there with a MySQL error, because the database and user don't
exist yet. That is expected the first time. It removes the unfinished release folder itself; the stack, the env file and
the units stay. (The units are enabled but not started: if the host reboots before the second deploy they fail to start, as there is
no release for them to run yet, and the second deploy puts that right.)

### 4. The database and its user

Run by whoever holds MySQL admin (its credentials are in the Broad's Secrets Manager secret `data-registry`). The
password is GWASHub's, in the secret `gwashub/broad/db` (a JSON document; the password has no punctuation, so it can go
inside the SQL quotes as it is). Fetch it with your own AWS credentials, and hand it over without pasting it into chat or email:

```bash
aws secretsmanager get-secret-value --secret-id gwashub/broad/db --region us-east-1 --query SecretString --output text
```

```sql
CREATE DATABASE gwashub CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_as_cs;
CREATE USER 'gwashub'@'<host_private_ip>' IDENTIFIED BY '<password>' REQUIRE SSL;
GRANT ALL PRIVILEGES ON gwashub.* TO 'gwashub'@'<host_private_ip>';
```

`<host_private_ip>` is the `host_private_ip` from `config.py`, the address the server sees the host connect from. The user has rights on
the `gwashub` database only: none on data-registry's. The collation matters: GWASHub treats names that differ only in case as different
(as on Postgres), and the migrations refuse a database whose collation isn't case-sensitive (`utf8mb4` with a collation ending in `_cs`
or `_bin`), naming the one they found. The host already reaches the MySQL server, so its network path needs nothing new.

### 5. nginx, the certificate and DNS

On your laptop, render the server block (it prints to the screen and writes nothing). Pass the certificate and key paths **as they will be on the
nginx machine**:

```bash
.venv/bin/python devtools/shared_host.py render-nginx broad --cert /path/on/nginx/gwashub.crt --key /path/on/nginx/gwashub.key
```

The block listens on 443 for the `site_url` name with that certificate, redirects port 80 to HTTPS, and proxies every path to
`<host private IP>:<api_port>`. It **overwrites** `Host`, `X-Forwarded-Proto` and `X-Forwarded-For` instead of passing the client's values on
(GWASHub believes those headers from this proxy only; dig-user-service's README records a past incident where an nginx didn't
forward `Host`), and allows request bodies up to 25 MB for a bulk-upload manifest (data files go straight to S3).

Then, on the nginx machine and by whoever manages them:

1. Put the block in the nginx configuration (where the Broad keeps its server blocks, for example `/etc/nginx/conf.d/`).
2. Get a certificate for the `site_url` name the way the Broad does for its other sites, at the paths you gave.
3. Add the DNS record for that name, pointing at the nginx machine.
4. `sudo nginx -t`, then reload nginx.

The stack's inbound rule lets the nginx machine (and only it) reach the API port. Once the deploy has gone live you can
check the path with `curl http://<host_private_ip>:<api_port>/api/health` from the nginx machine.

### 6. Deploy again

```bash
GWASHUB_SSH_TARGET=ec2-user@<host> ./devtools/shared_host_deploy.sh broad
```

This time it runs to the end, including the site check through nginx (step 12), so DNS, the certificate and the nginx block must
be in place first.

### 7. The first platform admin, then the reference files and a smoke test

Both are described under "Running it" and "First smoke test" below.

## Deploying

Needs Docker running, the AWS CLI signed in, `GWASHUB_SSH_TARGET` set and the setup done.

```bash
GWASHUB_SSH_TARGET=ec2-user@<host> ./devtools/shared_host_deploy.sh broad
```

It deploys what is **committed on the branch you have checked out** (`HEAD`), and stops at the first failure.

**Before the first deploy: a clean tree.** `shared_host.py preflight` is the first thing the deploy runs. It refuses to continue
if the AWS credentials are for another account than `config.py`'s, or if `git status --porcelain` prints anything: modified files **and
untracked ones**. The release is named after `HEAD` and contains `git archive HEAD`, but the website is built from your
working folder, so anything uncommitted could end up in the site under a commit that doesn't contain it. An untracked file under `web/` is
the clearest case: it goes into the website build and into no commit. So commit or stash your changes, and put files that must stay
out of git, such as local working files (for example agent notes), in `.git/info/exclude` (it is local, and not committed):

```bash
echo '<folder or file>' >> .git/info/exclude   # one line for each
git status --porcelain      # must print nothing
```

(or `git stash push --include-untracked` for a one-off).

### What it does

| # | Step | Notes |
|---|---|---|
| 1 | Checks | `shared_host.py preflight`: right AWS account, clean git tree |
| 2 | The host | SSH and `sudo -n` work; the machine is the configured host (its instance ID, from IMDSv2, is `host_instance_id`); `/opt/gwashub/releases` exists (the setup ran) |
| 3 | Website | `npx nuxi generate`; fails unless `index.html`, `200.html` and `paused.html` were built |
| 4 | Pipeline images | Logs in to ECR, builds and pushes each image from `pipeline/images/` not already in ECR at this tag, and records its digest |
| 5 | Infrastructure | `cdk deploy Gwashub-broad-Shared`; saves the outputs in `infra/cdk-outputs-broad.json` (git-ignored). Creates the service-linked roles ECS, Batch and Spot need (a no-op once they exist). The stack deploys with your own AWS credentials through one private bucket, `gwashub-broad-deploy-<account>-us-east-1`, made the first time: the account needs no `cdk bootstrap`, and gets none of its roles |
| 6 | The release | `git archive HEAD` and the built site, over SSH, into `/opt/gwashub/releases/<tag>`; a virtualenv from `images/app/requirements.txt` |
| 7 | The env file | `shared_host.py render-env` (reads the stack outputs and the three secrets); written to `/etc/gwashub/env` over stdin |
| 8 | The units | `shared_host.py render-unit` for `api` and `worker`; `daemon-reload` and `enable` |
| 9 | Migrations | `python -m gwashub_api.migrate` from the new release |
| 10 | Switch | `current` now points at the new release; both services restart, and the deploy waits until both are up and `/api/health` answers on the host |
| 11 | Limits | `shared_host.py limits-ok`: the memory limit is actually in force for both units |
| 12 | The site | `shared_host.py check-site`: `/api/health` answers through nginx, `/api/auth/mode` says `cognito`, and `/api/auth/users` and `/api/auth/login` do **not** answer |
| 13 | Old releases | Keeps the new one and 2 before it; deletes the rest |

At the end it prints the site address and the rollback command.

### What can go wrong, and what it leaves behind

- **Steps 7 and 8 replace the env file and the unit files before the migrations run (step 9).** If a migration fails, the old release
  keeps running on the old code, but a crash or reboot of either service before the next successful deploy starts it with the **new** env file and
  units. Don't leave it: fix the cause and deploy again promptly. A failure at step 9 leaves `current` pointing at the old release, so
  **don't run `rollback` then**: it would go one release further back than the one that is live. To put the old env file and units back
  instead, check out the commit that was live and deploy that.
- **MySQL migrations aren't transactional.** MySQL commits each schema change as it runs (Postgres rolls a failed migration back; MySQL
  can't), so a migration that fails part-way leaves the changes it had made, and the database still says it is at the old revision.
  Running it again fails on "already exists". A failed migration needs fixing by hand: look at the tables and at
  `SELECT version_num FROM alembic_version`, undo (or finish) the half-applied changes so the schema matches one revision exactly, and if you
  finished them set `version_num` to the new revision; then deploy again. A failed deploy never restores the database.
- **Know which migrations a deploy brings.** The release name ends with the commit's short sha, so compare with what is live:
  `ssh <host> readlink /opt/gwashub/current`, then `git log --oneline <sha>..HEAD -- api/migrations`. Before a deploy that has some,
  ask whoever runs the MySQL server for a snapshot or confirm a recent backup, and know that once it is live `rollback` can't take you
  back past it (below).
- **A failure between steps 6 and 10** removes the unfinished release folder, so it can't take the place of a good release the prune
  would keep; the host first checks that `current` doesn't point at it (if the switch in step 10 happened but SSH reported a failure, the
  release stays and the rollback command is printed). The stack, the env file and the units stay as they are.
- **A failure after step 10** (the services don't come up, the memory limits aren't in force, or the site check fails) leaves the new release
  live and prints the rollback command, which refuses if that release added a migration (below).
- **Docker keeps an ECR token.** Step 4 runs `aws ecr get-login-password | docker login`, which writes a short-lived ECR token (about 12
  hours) into `~/.docker/config.json` unless a credential helper is configured. On a laptop that others can use, or
  that is backed up, configure the Amazon ECR credential helper, or log out when you are done:
  `docker logout <account>.dkr.ecr.us-east-1.amazonaws.com`.
- **The first run is the slowest**: it builds every pipeline image. Each deploy tags the images with its release name and builds them
  again, so later runs are quick only because of Docker's layer cache.

### Rolling back

```bash
GWASHUB_SSH_TARGET=ec2-user@<host> ./devtools/shared_host_deploy.sh broad rollback
```

First it checks that `GWASHUB_SSH_TARGET` is the configured host (as the deploy does). Then it points `current` at the newest release
older than the one now live (one with its virtualenv and wrapper intact), restarts both services, waits for them, and runs the site check.
Run it again to go back another release. It restores **code only**:

- **Migrations are not rolled back, so a rollback is only possible when the live release added none.** If the live release has a file
  in `api/migrations/versions/` that the previous one lacks, `rollback` names them and changes nothing: that migration has been applied,
  the older code doesn't know the database's revision (Alembic stops with "Can't locate revision"), and both services would fail to
  start. Fix forward instead: deploy a corrected commit.
- The stack, the images, the env file and the units stay as the last deploy wrote them.
- Only the current release and 2 older ones exist, so you can go back at most 2 releases. To go forward again, deploy.

### Running the CDK by hand

Step 5 runs the stack from `infra/`, where `cdk.json` starts the app with `infra/.venv/bin/python`. The deploy makes that virtualenv from
`infra/requirements.txt` if it is missing; to make it yourself:

```bash
python3 -m venv infra/.venv && infra/.venv/bin/pip install -r infra/requirements.txt
```

The stack needs the pipeline images' digests as a JSON context value, `-c pipeline_images=...`, which step 4 builds (it prints each
image's reference and digest). With that JSON, from `infra/`:

```bash
IMAGES='{"gwashub/base": {"digest": "sha256:...", "platform": "linux/arm64"}, "gwashub/vcf": {"digest": "sha256:...", "platform": "linux/amd64"}}'
npx --yes aws-cdk@2 synth Gwashub-broad-Shared -c env=broad -c pipeline_images="$IMAGES"     # the CloudFormation template
npx --yes aws-cdk@2 diff Gwashub-broad-Shared -c env=broad -c pipeline_images="$IMAGES"      # what a deploy would change
```

`deploy` takes the same arguments plus `--outputs-file cdk-outputs-broad.json`; the deploy script does that for you, and the
other scripts read that file. The stack's tests (`infra/tests`) run on their own, with the same virtualenv:

```bash
infra/.venv/bin/python -m pytest infra/tests
```

### The pieces, one by one

`devtools/shared_host.py` does the work of steps 1, 7, 8, 11, 12 and 13 and can be run on its own from the repository root with
`.venv/bin/python devtools/shared_host.py <command> broad ...` (the environment always comes first):

| Command | What it does |
|---|---|
| `render-env broad` | The env file, on screen only. **It holds secrets**: don't paste it anywhere or redirect it to a file |
| `render-unit broad api` (or `worker`) | A systemd unit |
| `render-nginx broad --cert P --key P` | The nginx server block |
| `check-site broad` | The site is healthy, says its sign-in is Cognito's (`/api/auth/mode`), and the dev sign-in is not there. Needs your laptop to reach `site_url` |
| `preflight broad` | The AWS account and the clean tree |
| `prune broad <current> <releases...>` | Which release folders to delete |
| `limits-ok broad` | Reads `systemctl show` output on stdin; fails if a unit has no memory limit in force |

## Running it

**In the website** everything works as in `docs/AWS_FULL.md`: reviewers manage their project's members, platform admins create projects, everyone
has the Practice project and the guided tours, and the bulk upload script uses a personal API token.

**Admin commands** run on the host over SSH, as the `gwashub` user with the API's settings. They replace
`aws_admin.sh`'s one-off ECS tasks, which this deployment doesn't have:

```bash
export GWASHUB_SSH_TARGET=ec2-user@<host>
./devtools/shared_host_admin.sh broad users
./devtools/shared_host_admin.sh broad invite someone@uni.ac.uk "Full Name" --institution "University of X" [--admin]
./devtools/shared_host_admin.sh broad invite someone@uni.ac.uk "Full Name" --resend      # invitation email again
./devtools/shared_host_admin.sh broad add-member someone@uni.ac.uk hermes3 reviewer      # or uploader
./devtools/shared_host_admin.sh broad remove-member someone@uni.ac.uk hermes3
./devtools/shared_host_admin.sh broad add-project lvsys "LV systolic function GWAS" LVSF --phenotype LVEF
```

The arguments reach `python -m gwashub_api.admin` as typed: spaces and quotes survive. `db-readonly-user` refuses here
(it is Postgres only); to look at the database, ask the MySQL server's administrators for a read-only login.

**The first platform admin.** Once the deploy has finished:

```bash
./devtools/shared_host_admin.sh broad invite you@example.org "Your Name" --institution "Your institution" --admin
```

`invite` creates the GWASHub user and the Cognito account. Cognito emails a temporary password (check the junk folder; the
built-in email sends about 50 a day, and moving it to SES is `docs/PRODUCTION_ROADMAP.md` 2.9). At first sign-in the person sets their own password and
registers an authenticator app. A platform admin can create projects (New project on the Projects page) and becomes its first reviewer; for other
reviewer actions they need a reviewer role in the project. To remove someone completely, disable them in Cognito (console: Cognito, User pools,
`gwashub-broad`, Users) and remove their project memberships: the host's role can create users but not disable them.

**Reference files** (tens of GB) go up from your laptop and are then registered as shared resources. The default pipeline needs them, so do this
before the first QC run. Build them first (`resources/README.md`); then, with `AWS_PROFILE` set if it isn't `gwashub-broad`:

```bash
AWS_REGION=us-east-1 python devtools/aws_upload_resources.py broad     # run from the repository's virtualenv
./devtools/shared_host_admin.sh broad add-resources s3://…/resources/shared/_manifests/<time>.json   # the line it prints
```

`AWS_REGION` matters: the script defaults to London. It reads the bucket from `infra/cdk-outputs-broad.json`, skips
files already there, and prints the admin line, which reads the manifest from S3 with the host's role. 37 files, about 26 GB, so it takes a while
from a laptop. After a database reset the same `add-resources` line registers them again without uploading anything.

**Logs.**

- On the host: `sudo journalctl -u gwashub-api -u gwashub-worker` (add `-f` to follow, `--since "1 hour ago"`). Migrations log there too.
  `sudo systemctl status gwashub-api gwashub-worker` shows whether they are up.
- Batch jobs: CloudWatch Logs, log group `/gwashub/broad/app`, streams `batch-<owner>-<image>/…`. Batch jobs themselves are under Batch, Jobs, queues `gwashub-broad`,
  `gwashub-broad-x86` and the `-ondemand` ones.
- A user's problem: ask for the **reference** at the bottom of the dataset panel or meta-analysis page; it names the exact S3 folder.
- The settings the services run with: `sudo -u gwashub cat /etc/gwashub/env` on the host. It holds secrets: don't paste it.

## First smoke test

After the second deploy, with the reference files registered. Sign in as the first admin and, in the Practice project, work through the "upload a dataset" tour with its
synthetic files:

1. `.venv/bin/python devtools/shared_host.py check-site broad` prints `<site_url>: healthy, and the development sign-in is off` (the deploy runs it too).
2. Sign-in goes to the Cognito page and back to the site, with MFA.
3. **Upload a file from the browser** and let it finish. The browser sends the parts straight to S3 with presigned URLs, so this tests the
   bucket's CORS rule and the host's policy (see below).
4. Start QC. Watch the jobs in the Batch console: a job that stays in RUNNABLE usually means the compute environment can't get a
   machine (a vCPU quota, no Spot capacity in the zones, or a subnet without a route to the internet gateway).
5. When it finishes, open the report and **download a file** (a presigned link).
6. In the journal (`sudo journalctl -u gwashub-api -u gwashub-worker`), look for AccessDenied errors.

**If the browser upload is refused (403 AccessDenied on a part)**: every statement in the host's policy (KMS aside) carries an
`ec2:SourceInstanceARN` condition that names the shared host, so the rights apply only to calls made from that machine. A presigned URL is
signed by the host but *used* by the browser, and S3 judges the browser's request against the host role's policy, where that condition
may fail. This is untested until the first smoke test. The documented fallback is to relax the condition on the
**S3 statement only**, preferably to `StringEqualsIfExists`: in `infra/gwashub_infra/shared_host_stack.py` give the first statement
in `policy.add_statements(...)` its own conditions instead of `on_host`:

```python
conditions={"StringEqualsIfExists": {"ec2:SourceInstanceARN": f"arn:aws:ec2:{cfg.region}:{cfg.account}:instance/{cfg.host_instance_id}"}}
```

With `IfExists` a request that doesn't carry the key passes, and one that carries it must still name the host. Update the
test that pins the condition (`infra/tests/test_shared_host_stack.py`), run the infra tests (below), deploy, and **record why** in a comment next to the statement
and in this guide. Leave every other statement as it is.

## What the host's role can do

The inline policy `gwashub-broad-host` gives the shared role what the ECS task roles have in the three-stack deployment. Every statement
except KMS requires the call to come from the shared host's instance:

| Service | Grant |
|---|---|
| S3 | Read, write and delete in GWASHub's three buckets, and list them |
| Batch | `SubmitJob` and `TagResource` on GWASHub's four queues and on job definitions named `gwashub-broad-*` (the account is shared, so the prefix matters); `DescribeJobs` on all jobs (read only); `TerminateJob` on `job/*` in the account |
| SQS | Receive and delete on GWASHub's events queue |
| Cognito | `AdminCreateUser` on the `gwashub-broad` user pool (the invite commands) |
| KMS | Encrypt and decrypt with GWASHub's key, **only through S3** (`kms:ViaService`): S3 calls KMS for the caller, and the instance condition may not reach those calls |
| Secrets Manager | Nothing; and each of GWASHub's three secrets denies the role every Secrets Manager action in its own resource policy, which wins over an allow from any other policy on the role |

Two things to know:

- **`batch:TerminateJob` is on `job/*` for the whole account**, because job ARNs aren't known in advance and the Stop buttons (QC runs
  and meta-analyses) need it. The host could cancel any Batch job it knows the ID of, including data-registry's. Batch job IDs are random
  UUIDs, and GWASHub only ever terminates and describes the job IDs it recorded when it submitted them.
- **Both applications share the host's role**, so each can read the other's buckets, and a bad deploy or a runaway process in either can affect both. The
  owner accepted that for the saving. The env file isn't readable by other users, but root can read it.

## What's dropped compared with the full AWS deployment

Kept: Cognito invite-only sign-in with MFA; HMAC-signed session cookies; server-side permissions; private, KMS-encrypted, versioned buckets;
presigned URLs; images pinned by digest; secrets in Secrets Manager; the dev sign-in check at every deploy.

Dropped:

- WAF and rate limiting; GuardDuty and malware scanning of uploads; AWS Config, Security Hub and CloudTrail data events; alarms; CI/CD
  (`docs/PRODUCTION_ROADMAP.md` phases 2 and 3). Nothing here watches the jobs' dead-letter queue, the logs or the host.
- The API's private-only network position: in the full deployment it has no public address and is reached only through CloudFront; here the
  Broad's proxy is its front door.
- Separate accounts for dev and prod, and a second (QA) environment.
- GWASHub-specific database backups: it relies on the MySQL server's own. The deploy doesn't snapshot anything.
- The database browsing tunnel and read-only user, and hibernating.
- Containers for the API and worker: they run in a virtualenv under systemd, with journald for logs.

The rest of `docs/PRODUCTION_ROADMAP.md` (information-governance sign-off, SES for invitations, isolating consortium steps) applies here as much as to the other deployment.

## What it costs

About **$5 a month fixed**: KMS key ($1), three secrets ($1.20), ECR image storage (a few dollars), log storage (cents). Cognito is
free under 10,000 monthly active users; SQS and EventBridge stay in the free tier. The host, the MySQL server and the proxy are
the Broad's and cost nothing extra here. Per use: S3 at about $0.023 a GB a month, Batch compute (about $0.25 to
$0.40 for a full-size QC run and $0.10 to $0.50 for a meta-analysis at historical Spot prices), public IPs for Batch machines while they run, and
downloads after the first 100 GB a month. For comparison, `docs/AWS_FULL.md`'s dev deployment measured about $120 a month fixed.

## Changing things

| To change | Edit | Then |
|---|---|---|
| Memory or CPU limits of the services | `api_memory`, `api_cpu`, `worker_memory`, `worker_cpu` | Commit, deploy (step 8 rewrites the units, step 11 checks the limits) |
| The banner | `banner` | Commit, deploy (it is in the env file) |
| The site address | `site_url` | Commit, deploy (updates Cognito's return addresses and the uploads CORS rule); new nginx block, certificate and DNS |
| The API port | `api_port` | Commit, run `shared_host_setup.sh` (it checks the port), deploy (the inbound rule and unit change); render the nginx block again and reload |
| The nginx machine | `nginx_cidr` | Commit, deploy (the inbound rule and the addresses the API trusts) |
| Batch size | `batch_max_vcpus`, `batch_volume_gb`, `batch_spot`, `meta_on_demand` | Commit, deploy |
| The session key or Cognito client secret | Secrets Manager | Deploy. A new session key signs everyone out |
| The database password | The MySQL user and `gwashub/broad/db` together | Deploy |
| The MySQL server's address or port | `db_host`, `db_port` | Avoid it unless the server itself moves. Both are in the database secret's template, so the stack update (step 5) makes CloudFormation generate a **new password**, and step 7 puts it in the env file; the MySQL user still has the old one, so step 9 fails and a restart of either service can't connect. Then, with the new password from `gwashub/broad/db` (step 4's command), run `ALTER USER 'gwashub'@'<host_private_ip>' IDENTIFIED BY '<new password>';` on the server and deploy again |

## When something goes wrong

| Message or symptom | Cause and fix |
|---|---|
| `The git working tree isn't clean` | Commit, stash, or put the file in `.git/info/exclude` (above) |
| `The AWS credentials are for account … but broad deploys to account …` | Wrong profile: set `AWS_PROFILE` |
| `Cannot SSH to … without prompts` | Needs a key (no passphrase prompt; an agent works) and a route to the host |
| `sudo on … needs a password: it must work with 'sudo -n'` | Passwordless sudo on the host |
| `… is instance i-…, not broad's host i-…` | `GWASHUB_SSH_TARGET` reaches a different machine: correct it. Nothing was changed there |
| `Could not read the instance ID of … from the EC2 instance metadata service` | The metadata service is off on the host, or something there blocks `169.254.169.254` (a firewall rule, a proxy). The scripts need it to tell which machine they reached; ask the host's owners |
| `Not rolling back: … added database migrations that … lacks` | The live release's migrations are applied and the older code can't run on them. Fix forward: deploy a corrected commit |
| `… /releases is missing on the host: run ./devtools/shared_host_setup.sh broad first` | Run the setup |
| Setup: `Port N is already in use on the host, and not by gwashub-api` | Choose another `api_port` |
| Setup: `/usr/bin/python3.12 is missing` | Install Python 3.12 with its venv and pip on the host |
| Step 9 fails | The first time: expected (above). Otherwise read the error: MySQL access (the user, `REQUIRE SSL`, the host IP in the grant), a certificate error (`db_host` is an IP or a CNAME), or a failed migration (above) |
| `The MySQL database has character set … and collation …` | The database wasn't created as in step 4. While it is still empty (the first deploy), `ALTER DATABASE gwashub CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_as_cs;` fixes it; if it has tables, stop and ask, as they keep the old collation |
| `gwashub-run: line N of /etc/gwashub/env is not KEY='value'` | A hand edit broke the env file, and neither service starts. Deploy again to rewrite it, or fix line N: `KEY='value'`, no spaces around `=`, and no single quote inside the value |
| `gwashub-api and gwashub-worker are not both up and answering` | The deploy prints the journal. Usual causes: the database unreachable, a bad value in the env file, the port taken |
| `A service has no MemoryMax or MemoryLimit in force` | systemd isn't applying the limits (Amazon Linux 2's systemd ignores `MemoryMax`; the units set `MemoryLimit` too). Check `systemctl show gwashub-api -p MemoryMax -p MemoryLimit` |
| `/api/auth/mode answered …`, `/api/auth/users answered 200 …` or `/api/auth/login answered …` | The dev sign-in may be on. It shouldn't be possible: stop the API now (`sudo systemctl stop gwashub-api`) and find out why before anything else |
| `/api/health answered 502` or could not reach the site | nginx, DNS or the certificate; from the nginx machine, `curl http://<host_private_ip>:<api_port>/api/health` |
| Cognito shows `redirect_mismatch` | Users must use exactly `site_url`; Cognito returns only there |
| Browser upload refused | See "First smoke test" |
| A QC job waits in RUNNABLE | Batch console: the compute environment's status, vCPU quotas, the zones |

## Testing the pieces

- The stack: `infra/.venv/bin/python -m pytest infra/tests` (see "Running the CDK by hand" for the virtualenv). They check the stack's resources, the host policy
  and that the three-stack templates didn't change.
- The host files, the checks and the scripts' pieces: `.venv/bin/python -m pytest devtools/tests`.
- The API on MySQL: `./devtools/test_db.sh mysql-8.0` and `./devtools/test_db.sh mysql-8.4` (Docker). RDS MySQL 8.0's standard support ended in July 2026, so
  the server is on Extended Support or about to move to 8.4; both must work.
