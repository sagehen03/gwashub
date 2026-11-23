"""GWASHub on a host, a MySQL server and a web proxy that already exist (docs/SHARED_HOST.md).

One stack, Gwashub-<name>-Shared. It creates only what those hosts can't supply: the file store (KMS key and three
buckets), Cognito sign-in, Batch and the job-events queue, the secrets, a policy on the host's existing instance role,
two public subnets for Batch in the existing VPC, and one inbound rule on the host's security group. No VPC, NAT
gateway, database, load balancer, container service or website bucket: the host runs the API and worker, the proxy
serves the site, and the database is the existing MySQL server.

The deploy script reads this stack's outputs (the `Out*` keys) to write the host's env file.
"""
import json

import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_secretsmanager as sm
from constructs import Construct

from . import parts
from .config import SharedHostConfig


class SharedHostStack(cdk.Stack):
    def __init__(self, scope: Construct, id: str, *, cfg: SharedHostConfig, pipeline_images: dict, **kwargs):
        """pipeline_images: {"gwashub/base": {"digest": "sha256:...", "platform": "linux/arm64"}, ...} (or just the
        digest), the images pushed by the deploy script to ECR repositories gwashub-images/<owner>/<image>."""
        # Deployed with the operator's own AWS credentials, through one plain bucket. A CDK bootstrap would leave standing
        # roles, one of them an administrator, in an account GWASHub only shares.
        super().__init__(scope, id, synthesizer=cdk.CliCredentialsStackSynthesizer(file_assets_bucket_name=cfg.deploy_bucket), **kwargs)
        n = cfg.name
        files = parts.file_store(self, cfg)
        log_group = logs.LogGroup(self, "Logs", log_group_name=f"/gwashub/{n}/app", retention=parts.retention(cfg.log_days),
                                  removal_policy=cdk.RemovalPolicy.RETAIN)

        # ---------- secrets ----------
        # Created here, read only by the deploy (with the deployer's own AWS credentials) to write the host's env file.
        # The host's role gets no access to them: its policy below has no secretsmanager: action, and each secret's
        # resource policy (after sign-in, which makes the third) denies that role everything, so a policy on the shared
        # role that isn't GWASHub's can't grant it either.
        session_secret = sm.Secret(self, "SessionSecret", secret_name=f"gwashub/{n}/session",
                                   description="GWASHub session-cookie signing key",
                                   generate_secret_string=sm.SecretStringGenerator(password_length=64, exclude_punctuation=True))
        db_secret = sm.Secret(self, "DbSecret", secret_name=f"gwashub/{n}/db", description="GWASHub's login on the shared MySQL server",
                              generate_secret_string=sm.SecretStringGenerator(
                                  secret_string_template=json.dumps({"username": "gwashub", "dbname": "gwashub",
                                                                     "host": cfg.db_host, "port": cfg.db_port}),
                                  generate_string_key="password", password_length=32, exclude_punctuation=True))

        # ---------- sign-in ----------
        signin = parts.sign_in(self, cfg, cfg.site_url)
        parts.uploads_cors(self, files.uploads, cfg.site_url)

        host_role = iam.Role.from_role_name(self, "HostRole", cfg.host_role_name)
        for secret in (session_secret, db_secret, signin.client_secret):
            secret.add_to_resource_policy(iam.PolicyStatement(
                effect=iam.Effect.DENY, actions=["secretsmanager:*"], principals=[iam.AnyPrincipal()], resources=["*"],
                conditions={"ArnEquals": {"aws:PrincipalArn": host_role.role_arn}}))

        # ---------- network for Batch ----------
        # The VPC has no NAT gateway, so Batch machines need public addresses to reach ECR, S3 and Batch. They take
        # them from their subnet, so the stack makes its own public subnets (one per zone, routed to the VPC's
        # existing internet gateway) and leaves the shared host's subnets alone. The job group has no inbound rule.
        vpc = ec2.Vpc.from_vpc_attributes(self, "Vpc", vpc_id=cfg.vpc_id, availability_zones=list(cfg.availability_zones))
        batch_subnets = []
        for i, (zone, cidr) in enumerate(zip(cfg.availability_zones, cfg.batch_subnet_cidrs)):
            subnet = ec2.PublicSubnet(self, f"BatchSubnet{i}", vpc_id=cfg.vpc_id, availability_zone=zone, cidr_block=cidr,
                                      map_public_ip_on_launch=True)
            ec2.CfnRoute(self, f"BatchRoute{i}", route_table_id=subnet.route_table.route_table_id,
                         destination_cidr_block="0.0.0.0/0", gateway_id=cfg.internet_gateway_id)
            batch_subnets.append(subnet)
        job_sg = ec2.SecurityGroup(self, "JobSecurityGroup", vpc=vpc, allow_all_outbound=True,
                                   description=f"GWASHub {n} Batch jobs: outbound only")

        # ---------- jobs: Batch + events queue ----------
        jobs = parts.batch_jobs(self, cfg, vpc=vpc, subnets=ec2.SubnetSelection(subnets=batch_subnets),
                                security_groups=[job_sg], files=files, log_group=log_group, pipeline_images=pipeline_images)

        # ---------- the host ----------
        # Nginx, and only nginx, reaches the API's port.
        ec2.CfnSecurityGroupIngress(self, "HostApiFromProxy", group_id=cfg.host_security_group_id, ip_protocol="tcp",
                                    from_port=cfg.api_port, to_port=cfg.api_port, cidr_ip=cfg.nginx_cidr,
                                    description=f"GWASHub {n} API from the web proxy")
        # What the ECS task roles have in the three-stack deployment, on the host's existing instance role. Any
        # process on the host (and on other hosts sharing the role) can use that role, so every statement is tied to
        # this one instance; the KMS statement can't be (S3 calls KMS for the caller, and the instance key may not
        # reach those calls), so it allows only calls made through S3, which is itself gated to the host.
        # No secretsmanager: action at all: the secrets reach the host only through the deploy (and deny the role, above).
        on_host = {"StringEquals": {"ec2:SourceInstanceARN": f"arn:aws:ec2:{cfg.region}:{cfg.account}:instance/{cfg.host_instance_id}"}}
        buckets = [files.uploads, files.results, files.resources]
        policy = iam.Policy(self, "HostPolicy", policy_name=f"gwashub-{n}-host", roles=[host_role])
        policy.add_statements(
            iam.PolicyStatement(
                actions=["s3:GetObject*", "s3:GetBucket*", "s3:List*", "s3:PutObject", "s3:PutObjectTagging",
                         "s3:DeleteObject*", "s3:Abort*"],
                resources=[*(b.bucket_arn for b in buckets), *(b.arn_for_objects("*") for b in buckets)], conditions=on_host),
            # The account is shared, so job definitions are matched by GWASHub's name prefix (parts.batch_jobs names them
            # gwashub-<name>-<image>), with or without a revision. parts.unversioned would match every one in the account.
            iam.PolicyStatement(
                actions=["batch:SubmitJob", "batch:TagResource"],
                resources=[*(q.job_queue_arn for q in jobs.queues),
                           self.format_arn(service="batch", resource="job-definition", resource_name=f"gwashub-{n}-*")],
                conditions=on_host),
            iam.PolicyStatement(actions=["batch:DescribeJobs"], resources=["*"], conditions=on_host),
            # Stop QC (jobs.stop_qc): terminate a run's job, waiting or running; job ARNs aren't known in advance
            iam.PolicyStatement(actions=["batch:TerminateJob"], conditions=on_host,
                                resources=[self.format_arn(service="batch", resource="job", resource_name="*")]),
            iam.PolicyStatement(
                actions=["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility", "sqs:GetQueueAttributes",
                         "sqs:GetQueueUrl"],
                resources=[jobs.events_queue.queue_arn], conditions=on_host),
            # The admin commands (devtools/aws_admin.sh) invite users.
            iam.PolicyStatement(actions=["cognito-idp:AdminCreateUser"], resources=[signin.pool.user_pool_arn], conditions=on_host),
            iam.PolicyStatement(
                actions=["kms:Decrypt", "kms:DescribeKey", "kms:Encrypt", "kms:ReEncrypt*", "kms:GenerateDataKey*"],
                resources=[files.key.key_arn],
                conditions={"StringEquals": {"kms:ViaService": f"s3.{cfg.region}.amazonaws.com"}}))

        # ---------- outputs: the values for the host's env file ----------
        outputs = {
            "SiteUrl": cfg.site_url,
            "UploadsBucket": files.uploads.bucket_name, "ResultsBucket": files.results.bucket_name,
            "ResourcesBucket": files.resources.bucket_name,
            "JobQueue": jobs.job_queue.job_queue_arn, "ImagesEnv": jobs.images_env,
            "EventsQueueUrl": jobs.events_queue.queue_url,
            "CognitoDomain": signin.domain.base_url(), "UserPoolId": signin.pool.user_pool_id,
            "UserPoolClientId": signin.client.user_pool_client_id,
            # The names, as literals: a created secret's `secret_name` is cut from its ARN at the first hyphen, so it
            # would give gwashub/<name>/cognito for gwashub/<name>/cognito-client.
            "SessionSecret": f"gwashub/{n}/session", "CognitoClientSecret": f"gwashub/{n}/cognito-client",
            "DbSecret": f"gwashub/{n}/db", "LogGroup": log_group.log_group_name,
        }
        for k, v in outputs.items():
            cdk.CfnOutput(self, f"Out{k}", value=v)
