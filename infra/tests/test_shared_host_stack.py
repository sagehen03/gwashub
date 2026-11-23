"""The shared-host stack (Gwashub-<name>-Shared): only what GWASHub's jobs need on the Broad's existing host.

The example configuration below is made up; the real one lives in infra/gwashub_infra/config.py (SHARED_HOSTS).
"""
import dataclasses
import json
import runpy
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from gwashub_infra import config
from gwashub_infra.config import SharedHostConfig
from gwashub_infra.shared_host_stack import SharedHostStack

INFRA = Path(__file__).resolve().parents[1]

PIPELINE_IMAGES = {
    "gwashub/base": {"digest": "sha256:" + "0" * 64, "platform": "linux/arm64"},
    "gwashub/vcf": {"digest": "sha256:" + "1" * 64, "platform": "linux/amd64"},
}

CFG = SharedHostConfig(
    name="example", account="111111111111", region="us-east-1", site_url="https://gwashub.example.org",
    vpc_id="vpc-0abc", availability_zones=("us-east-1a", "us-east-1b"),
    batch_subnet_cidrs=("10.0.200.0/24", "10.0.201.0/24"), internet_gateway_id="igw-0abc",
    host_instance_id="i-0123456789abcdef0", host_role_name="dr-api-host", host_security_group_id="sg-0host",
    host_private_ip="10.0.0.83", nginx_cidr="10.0.0.12/32", api_port=8410, db_host="db.example.internal")

SOURCE_INSTANCE = {"StringEquals": {"ec2:SourceInstanceARN": "arn:aws:ec2:us-east-1:111111111111:instance/i-0123456789abcdef0"}}
VIA_S3 = {"StringEquals": {"kms:ViaService": "s3.us-east-1.amazonaws.com"}}

OUTPUT_KEYS = {
    "OutSiteUrl", "OutUploadsBucket", "OutResultsBucket", "OutResourcesBucket", "OutJobQueue", "OutImagesEnv",
    "OutEventsQueueUrl", "OutCognitoDomain", "OutUserPoolId", "OutUserPoolClientId", "OutSessionSecret",
    "OutCognitoClientSecret", "OutDbSecret", "OutLogGroup",
}


def cdk_json_context() -> dict:
    """What `cdk synth` hands the app: the feature flags in infra/cdk.json (they change how policies are written)."""
    return json.loads((INFRA / "cdk.json").read_text())["context"]


@pytest.fixture(scope="module")
def template() -> Template:
    app = cdk.App(context=cdk_json_context())
    stack = SharedHostStack(app, f"Gwashub-{CFG.name}-Shared", cfg=CFG, pipeline_images=PIPELINE_IMAGES,
                            env=cdk.Environment(account=CFG.account, region=CFG.region))
    return Template.from_stack(stack)


def resources(template: Template, type_: str) -> dict:
    return template.find_resources(type_)


def host_policy(template: Template) -> dict:
    """The AWS::IAM::Policy attached to the shared host's existing role."""
    found = [r["Properties"] for r in resources(template, "AWS::IAM::Policy").values()
             if r["Properties"].get("Roles") == [CFG.host_role_name]]
    assert len(found) == 1
    return found[0]


def statements(template: Template) -> list[dict]:
    return host_policy(template)["PolicyDocument"]["Statement"]


def as_list(x) -> list:
    return x if isinstance(x, list) else [x]


def test_creates_only_what_the_jobs_need(template):
    template.resource_count_is("AWS::S3::Bucket", 3)
    template.resource_count_is("AWS::KMS::Key", 1)
    template.resource_count_is("AWS::Batch::ComputeEnvironment", 4)
    template.resource_count_is("AWS::Batch::JobQueue", 4)
    template.resource_count_is("AWS::SQS::Queue", 2)
    template.resource_count_is("AWS::Events::Rule", 1)
    template.has_resource_properties("AWS::Cognito::UserPool", {"MfaConfiguration": "ON"})
    for t in ("AWS::EC2::NatGateway", "AWS::EC2::VPC", "AWS::RDS::DBInstance", "AWS::ECS::Service",
              "AWS::ElasticLoadBalancingV2::LoadBalancer", "AWS::CloudFront::Distribution"):
        template.resource_count_is(t, 0)


def test_batch_subnets_are_public_and_route_to_the_existing_gateway(template):
    template.resource_count_is("AWS::EC2::Subnet", 2)
    template.all_resources_properties("AWS::EC2::Subnet", {"MapPublicIpOnLaunch": True, "VpcId": "vpc-0abc"})
    subnets = resources(template, "AWS::EC2::Subnet").values()
    assert {s["Properties"]["CidrBlock"] for s in subnets} == set(CFG.batch_subnet_cidrs)
    assert {s["Properties"]["AvailabilityZone"] for s in subnets} == set(CFG.availability_zones)
    template.has_resource_properties("AWS::EC2::Route", {"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-0abc"})
    # one default route per subnet, each in its own route table
    routes = [r["Properties"] for r in resources(template, "AWS::EC2::Route").values()]
    assert len(routes) == 2
    assert len({json.dumps(r["RouteTableId"], sort_keys=True) for r in routes}) == 2


def test_batch_runs_in_those_subnets_and_the_job_group(template):
    subnet_ids = {json.dumps({"Ref": k}) for k in resources(template, "AWS::EC2::Subnet")}
    job_sg = next(iter(resources(template, "AWS::EC2::SecurityGroup")))
    envs = [r["Properties"]["ComputeResources"] for r in resources(template, "AWS::Batch::ComputeEnvironment").values()]
    assert len(envs) == 4
    for res in envs:
        assert {json.dumps(s) for s in res["Subnets"]} == subnet_ids
        assert res["SecurityGroupIds"] == [{"Fn::GetAtt": [job_sg, "GroupId"]}]


def test_host_policy_is_tied_to_the_shared_host(template):
    stmts = statements(template)
    assert stmts
    for s in stmts:
        actions = as_list(s["Action"])
        assert s["Effect"] == "Allow"
        if all(a.startswith("kms:") for a in actions):
            assert s["Condition"] == VIA_S3
        else:
            assert {a.split(":")[0] for a in actions} <= {"s3", "batch", "sqs", "cognito-idp"}, actions
            assert s["Condition"] == SOURCE_INSTANCE, actions
    by_service = {a.split(":")[0] for s in stmts for a in as_list(s["Action"])}
    assert by_service == {"s3", "batch", "sqs", "cognito-idp", "kms"}


def test_host_policy_grants_what_the_task_roles_had(template):
    stmts = statements(template)

    def actions_for(service: str) -> set:
        return {a for s in stmts for a in as_list(s["Action"]) if a.startswith(service + ":")}

    assert actions_for("s3") == {"s3:GetObject*", "s3:GetBucket*", "s3:List*", "s3:PutObject", "s3:PutObjectTagging",
                                 "s3:DeleteObject*", "s3:Abort*"}
    assert actions_for("batch") == {"batch:SubmitJob", "batch:TagResource", "batch:DescribeJobs", "batch:TerminateJob"}
    assert actions_for("sqs") == {"sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility",
                                  "sqs:GetQueueAttributes", "sqs:GetQueueUrl"}
    assert actions_for("cognito-idp") == {"cognito-idp:AdminCreateUser"}
    assert actions_for("kms") == {"kms:Decrypt", "kms:DescribeKey", "kms:Encrypt", "kms:ReEncrypt*", "kms:GenerateDataKey*"}

    def resources_of(action: str) -> list:
        return [r for s in stmts if action in as_list(s["Action"]) for r in as_list(s["Resource"])]

    assert len(resources_of("s3:GetObject*")) == 6  # three buckets and their objects
    assert len(resources_of("batch:SubmitJob")) == 4 + 1  # four queues and the job definitions by name
    assert resources_of("batch:DescribeJobs") == ["*"]
    assert len(resources_of("batch:TerminateJob")) == 1
    assert len(resources_of("sqs:ReceiveMessage")) == 1
    assert len(resources_of("cognito-idp:AdminCreateUser")) == 1
    assert len(resources_of("kms:Decrypt")) == 1


def test_submit_job_is_scoped_to_gwashub_job_definitions(template):
    """The account is shared: the host may submit GWASHub's job definitions only, never any other in the account."""
    stmts = statements(template)
    for action in ("batch:SubmitJob", "batch:TagResource"):
        rs = [r for s in stmts if action in as_list(s["Action"]) for r in as_list(s["Resource"])]
        queues = [r for r in rs if isinstance(r, dict) and r.get("Fn::GetAtt", [None, None])[1] == "JobQueueArn"]
        definitions = [r for r in rs if r not in queues]
        assert len(queues) == 4
        assert len(definitions) == 1, definitions
        assert json.dumps(definitions[0]) == json.dumps(
            {"Fn::Join": ["", ["arn:", {"Ref": "AWS::Partition"}, ":batch:us-east-1:111111111111:job-definition/gwashub-example-*"]]})
    # nothing in the policy reaches every job definition in the account, and only DescribeJobs is on "*"
    for s in stmts:
        for r in as_list(s["Resource"]):
            assert not json.dumps(r).endswith('job-definition/*"'), r
            if r == "*":
                assert as_list(s["Action"]) == ["batch:DescribeJobs"]


def test_job_definition_names_start_with_the_scoped_prefix(template):
    names = [r["Properties"]["JobDefinitionName"] for r in resources(template, "AWS::Batch::JobDefinition").values()]
    assert names and all(n.startswith("gwashub-example-") for n in names)


def test_host_cannot_read_secrets(template):
    actions = [a for s in statements(template) for a in as_list(s["Action"])]
    assert actions
    assert not [a for a in actions if a.lower().startswith("secretsmanager:")]


HOST_ROLE_ARN = {"Fn::Join": ["", ["arn:", {"Ref": "AWS::Partition"}, ":iam::111111111111:role/dr-api-host"]]}


def test_each_secret_denies_the_host_role(template):
    """The shared role may already reach Secrets Manager through a policy that isn't GWASHub's: each secret's own
    resource policy denies that role everything, whatever else it is granted."""
    secrets = resources(template, "AWS::SecretsManager::Secret")
    assert len(secrets) == 3
    policies = [r["Properties"] for r in resources(template, "AWS::SecretsManager::ResourcePolicy").values()]
    assert sorted(p["SecretId"]["Ref"] for p in policies) == sorted(secrets)
    for p in policies:
        assert p["ResourcePolicy"]["Statement"] == [{
            "Effect": "Deny", "Action": "secretsmanager:*", "Principal": {"AWS": "*"}, "Resource": "*",
            "Condition": {"ArnEquals": {"aws:PrincipalArn": HOST_ROLE_ARN}}}]


def test_host_port_open_only_to_nginx(template):
    template.has_resource_properties("AWS::EC2::SecurityGroupIngress",
        {"GroupId": "sg-0host", "IpProtocol": "tcp", "FromPort": 8410, "ToPort": 8410, "CidrIp": "10.0.0.12/32"})
    template.resource_count_is("AWS::EC2::SecurityGroupIngress", 1)


def test_job_security_group_has_no_inbound(template):
    groups = resources(template, "AWS::EC2::SecurityGroup")
    assert len(groups) == 1
    (logical_id, group), = groups.items()
    assert "SecurityGroupIngress" not in group["Properties"]
    assert group["Properties"]["VpcId"] == "vpc-0abc"
    # allow-all outbound, as the jobs fetch images and reference files from ECR and S3
    egress = group["Properties"]["SecurityGroupEgress"]
    assert [(e["CidrIp"], e["IpProtocol"]) for e in egress] == [("0.0.0.0/0", "-1")]
    # the only ingress resource is the host's, and nothing refers to the job group from one
    for r in resources(template, "AWS::EC2::SecurityGroupIngress").values():
        assert logical_id not in json.dumps(r["Properties"])
    assert not resources(template, "AWS::EC2::SecurityGroupEgress")


def test_one_cidr_per_zone():
    with pytest.raises(ValueError, match="one entry each"):
        dataclasses.replace(CFG, batch_subnet_cidrs=("10.0.200.0/24",))


def test_uploads_cors_names_the_site(template):
    cors = [r for r in resources(template, "Custom::AWS").values() if "putBucketCors" in json.dumps(r["Properties"])]
    assert len(cors) == 1
    assert "https://gwashub.example.org" in json.dumps(cors[0]["Properties"])


def test_secrets(template):
    names = {r["Properties"].get("Name") for r in resources(template, "AWS::SecretsManager::Secret").values()}
    assert names == {"gwashub/example/session", "gwashub/example/cognito-client", "gwashub/example/db"}
    db = next(r["Properties"] for r in resources(template, "AWS::SecretsManager::Secret").values()
              if r["Properties"]["Name"] == "gwashub/example/db")
    gen = db["GenerateSecretString"]
    assert json.loads(gen["SecretStringTemplate"]) == {"username": "gwashub", "dbname": "gwashub",
                                                         "host": "db.example.internal", "port": 3306}
    assert gen["GenerateStringKey"] == "password"
    assert gen["PasswordLength"] == 32
    assert gen["ExcludePunctuation"] is True


def test_log_group(template):
    template.has_resource_properties("AWS::Logs::LogGroup", {"LogGroupName": "/gwashub/example/app", "RetentionInDays": 90})


def test_sign_in_calls_back_to_the_site(template):
    template.has_resource_properties("AWS::Cognito::UserPoolClient", {
        "CallbackURLs": ["https://gwashub.example.org/api/auth/callback"],
        "LogoutURLs": ["https://gwashub.example.org/login"]})


def test_outputs(template):
    outputs = template.to_json()["Outputs"]
    assert set(outputs) == OUTPUT_KEYS
    assert outputs["OutSiteUrl"]["Value"] == "https://gwashub.example.org"
    # the three secrets are named, never their values; each name is that of a secret the template creates
    assert outputs["OutSessionSecret"]["Value"] == "gwashub/example/session"
    assert outputs["OutCognitoClientSecret"]["Value"] == "gwashub/example/cognito-client"
    assert outputs["OutDbSecret"]["Value"] == "gwashub/example/db"
    created = {r["Properties"]["Name"] for r in resources(template, "AWS::SecretsManager::Secret").values()}
    assert {outputs[k]["Value"] for k in ("OutSessionSecret", "OutCognitoClientSecret", "OutDbSecret")} == created
    log_group = next(k for k, r in resources(template, "AWS::Logs::LogGroup").items()
                     if r["Properties"]["LogGroupName"] == "/gwashub/example/app")
    assert outputs["OutLogGroup"]["Value"] == {"Ref": log_group}
    for key, bucket in (("OutUploadsBucket", "Uploads"), ("OutResultsBucket", "Results"), ("OutResourcesBucket", "Resources")):
        ref = outputs[key]["Value"]["Ref"]
        assert ref.startswith(bucket) and resources(template, "AWS::S3::Bucket")[ref]
    # the job queue is an ARN (GWASHUB_BATCH_QUEUE takes an ARN), that of the ARM64 QC queue
    queue = next(k for k, r in resources(template, "AWS::Batch::JobQueue").items() if r["Properties"]["JobQueueName"] == "gwashub-example")
    assert outputs["OutJobQueue"]["Value"] == {"Fn::GetAtt": [queue, "JobQueueArn"]}
    assert "Fn::GetAtt" in outputs["OutEventsQueueUrl"]["Value"] or "Ref" in outputs["OutEventsQueueUrl"]["Value"]
    assert "Domain" in json.dumps(outputs["OutCognitoDomain"]["Value"])


def test_images_env_lists_each_image_with_its_queues(template):
    value = template.to_json()["Outputs"]["OutImagesEnv"]["Value"]
    text = json.dumps(value)
    for ref in PIPELINE_IMAGES:
        assert ref in text
    assert "meta_job_queue" in text


# ---- infra/app.py ----

def run_app(monkeypatch, tmp_path, context: dict) -> Path:
    """Run infra/app.py as `cdk synth -c ...` would: the context is passed to the App (the jsii process was started
    before any test could set CDK_CONTEXT_JSON, so the environment variable can't carry it)."""
    out = tmp_path / "cdk.out"
    real_app = cdk.App
    monkeypatch.setattr(cdk, "App", lambda: real_app(context={**cdk_json_context(), **context}, outdir=str(out)))
    runpy.run_path(str(INFRA / "app.py"), run_name="__main__")
    return out


def test_app_builds_only_the_shared_stack_for_a_shared_host(monkeypatch, tmp_path):
    monkeypatch.setitem(config.SHARED_HOSTS, CFG.name, CFG)
    out = run_app(monkeypatch, tmp_path, {"env": CFG.name, "pipeline_images": PIPELINE_IMAGES})  # no image_tag needed
    assert sorted(p.name for p in out.glob("*.template.json")) == ["Gwashub-example-Shared.template.json"]
    template = json.loads((out / "Gwashub-example-Shared.template.json").read_text())
    assert set(template["Outputs"]) == OUTPUT_KEYS


def test_deploys_without_a_bootstrapped_account(monkeypatch, tmp_path):
    """The deployer's own credentials and one bucket: no CDK bootstrap, and none of the roles it leaves in the account."""
    monkeypatch.setitem(config.SHARED_HOSTS, CFG.name, CFG)
    out = run_app(monkeypatch, tmp_path, {"env": CFG.name, "pipeline_images": PIPELINE_IMAGES})
    stack = json.loads((out / "manifest.json").read_text())["artifacts"]["Gwashub-example-Shared"]["properties"]
    assert CFG.deploy_bucket == "gwashub-example-deploy-111111111111-us-east-1"
    assert stack["stackTemplateAssetObjectUrl"].startswith(f"s3://{CFG.deploy_bucket}/")
    assert not {"assumeRoleArn", "cloudFormationExecutionRoleArn", "lookupRole", "requiresBootstrapStackVersion"} & set(stack)
    text = (out / "Gwashub-example-Shared.template.json").read_text() + (out / "Gwashub-example-Shared.assets.json").read_text()
    assert "hnb659fds" not in text and "BootstrapVersion" not in text  # nothing from a bootstrap: its bucket, roles or version
    files = json.loads((out / "Gwashub-example-Shared.assets.json").read_text())["files"].values()
    destinations = [d for f in files for d in f["destinations"].values()]
    assert destinations and all(d["bucketName"] == CFG.deploy_bucket and "assumeRoleArn" not in d for d in destinations)


def test_app_needs_the_pipeline_images_for_a_shared_host(monkeypatch, tmp_path):
    monkeypatch.setitem(config.SHARED_HOSTS, CFG.name, CFG)
    with pytest.raises(SystemExit) as e:
        run_app(monkeypatch, tmp_path, {"env": CFG.name})
    assert "pipeline_images" in str(e.value)


def test_app_unknown_environment_lists_both_kinds(monkeypatch, tmp_path):
    monkeypatch.setitem(config.SHARED_HOSTS, CFG.name, CFG)
    with pytest.raises(SystemExit) as e:
        run_app(monkeypatch, tmp_path, {"env": "nope"})
    message = str(e.value)
    assert "nope" in message and "dev" in message and "prod" in message and "example" in message


def test_app_three_stack_environments_still_need_an_image_tag(monkeypatch, tmp_path):
    with pytest.raises(SystemExit) as e:
        run_app(monkeypatch, tmp_path, {"env": "dev", "pipeline_images": PIPELINE_IMAGES})
    assert "image_tag" in str(e.value)
