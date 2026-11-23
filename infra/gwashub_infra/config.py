"""Per-environment settings. "dev" is the lean setup; "prod" turns on the production-grade options
described in docs/AWS_FULL.md (Multi-AZ database, more NAT gateways and tasks, longer backups).

SHARED_HOSTS holds the other kind of target: GWASHub on a host, a MySQL server and a web proxy that already exist
(infra/gwashub_infra/shared_host_stack.py). Its settings are a different shape, so it has its own class."""
from dataclasses import dataclass


@dataclass(frozen=True)
class EnvConfig:
    name: str
    account: str
    region: str
    nat_gateways: int
    db_instance: str  # e.g. t4g.small
    db_multi_az: bool
    db_backup_days: int
    api_tasks: int
    batch_max_vcpus: int
    batch_spot: bool  # QC jobs on Spot
    meta_on_demand: bool  # meta-analyses on On-Demand machines: hours-long and restarted from scratch if Spot takes one back
    batch_volume_gb: int  # root volume of Batch instances (reference files + intermediate VCFs)
    log_days: int
    noncurrent_version_days: int  # how long deleted/overwritten files stay recoverable
    banner: str  # strip at the top of every page; empty for none
    domain: str = ""  # site address (a Route 53 hosted zone of that name in this account); empty: the CloudFront address
    www: bool = False  # also serve www.<domain>, redirecting to <domain> (for an apex domain)


ENVIRONMENTS = {
    "dev": EnvConfig(name="dev", account="203248996074", region="eu-west-2", nat_gateways=1, db_instance="t4g.small",
                     db_multi_az=False, db_backup_days=7, api_tasks=1, batch_max_vcpus=32, batch_spot=True, meta_on_demand=True,
                     batch_volume_gb=200,
                     log_days=90, noncurrent_version_days=30, banner="Development build", domain="gwashub.com",
                     www=True),
    "prod": EnvConfig(name="prod", account="269626333085", region="eu-west-2", nat_gateways=2, db_instance="t4g.medium",
                      db_multi_az=True, db_backup_days=30, api_tasks=2, batch_max_vcpus=128, batch_spot=True, meta_on_demand=True,
                      batch_volume_gb=500,
                      log_days=365, noncurrent_version_days=90, banner=""),
}


@dataclass(frozen=True)
class SharedHostConfig:
    """GWASHub on an existing host: the stack creates only the file store, sign-in, Batch and the host's permissions."""
    name: str
    account: str
    region: str
    site_url: str  # the address users type, https://...; the nginx proxy serves it
    vpc_id: str  # the existing VPC
    availability_zones: tuple[str, ...]  # one Batch subnet in each
    batch_subnet_cidrs: tuple[str, ...]  # free address ranges in the VPC, one per zone
    internet_gateway_id: str  # the VPC's existing internet gateway: Batch machines get public addresses, so no NAT
    host_instance_id: str  # permissions apply only to calls made from this instance
    host_role_name: str  # the host's existing instance role, which the stack adds a policy to
    host_security_group_id: str  # the host's security group, which gets one inbound rule
    host_private_ip: str
    nginx_cidr: str  # the proxy's address, the only thing allowed to reach api_port
    api_port: int
    db_host: str  # the existing MySQL server
    db_port: int = 3306
    batch_max_vcpus: int = 64
    batch_spot: bool = True  # QC jobs on Spot
    meta_on_demand: bool = True  # meta-analyses on On-Demand machines
    batch_volume_gb: int = 200  # root volume of Batch instances (reference files + intermediate VCFs)
    log_days: int = 90
    noncurrent_version_days: int = 30  # how long deleted/overwritten files stay recoverable
    banner: str = ""  # strip at the top of every page; empty for none
    api_memory: str = "1536M"  # systemd limits for the host's two services
    api_cpu: str = "100%"
    worker_memory: str = "512M"
    worker_cpu: str = "50%"

    def __post_init__(self):
        if not self.availability_zones or len(self.availability_zones) != len(self.batch_subnet_cidrs):
            raise ValueError("availability_zones and batch_subnet_cidrs need one entry each per Batch subnet")

    @property
    def deploy_bucket(self) -> str:
        """The bucket the stack's template goes up through (made by devtools/shared_host_deploy.sh); no CDK bootstrap."""
        return f"gwashub-{self.name}-deploy-{self.account}-{self.region}"


SHARED_HOSTS: dict[str, SharedHostConfig] = {}
