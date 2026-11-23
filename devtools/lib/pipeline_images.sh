# shellcheck shell=bash
# ECR helpers and the pipeline-image build, shared by devtools/aws_cdk_deploy.sh and devtools/shared_host_deploy.sh.
# Source this file (it defines functions and LIFECYCLE and runs nothing); run from the repository root, with AWS_PROFILE
# and AWS_REGION set and Docker logged in to the registry.

# ECR repositories are made here (not by CDK). Each keeps its 5 newest images (enough to roll back; the running
# ones are always among them) and drops untagged layers after a day. Older images can be rebuilt from git.
LIFECYCLE='{"rules": [
  {"rulePriority": 1, "description": "untagged after 1 day", "selection": {"tagStatus": "untagged", "countType": "sinceImagePushed", "countUnit": "days", "countNumber": 1}, "action": {"type": "expire"}},
  {"rulePriority": 2, "description": "keep the 5 newest", "selection": {"tagStatus": "any", "countType": "imageCountMoreThan", "countNumber": 5}, "action": {"type": "expire"}}]}'
ensure_repo() {
  aws ecr describe-repositories --repository-names "$1" >/dev/null 2>&1 || \
    aws ecr create-repository --repository-name "$1" --image-scanning-configuration scanOnPush=true --image-tag-mutability IMMUTABLE \
      --tags Key=project,Value=gwashub >/dev/null  # the tag every GWASHub resource has: how to find and remove them
  aws ecr put-lifecycle-policy --repository-name "$1" --lifecycle-policy-text "$LIFECYCLE" >/dev/null
}

# push_pipeline_images <registry> <tag>: build and push every pipeline image (pipeline/images/<owner>/<image>) that isn't
# in ECR at <tag> yet, and set PIPELINE_IMAGES to the JSON the CDK app takes (-c pipeline_images=...).
push_pipeline_images() {
  local REGISTRY=$1 TAG=$2 LINE REF PLATFORM IREPO DIGEST
  PIPELINE_IMAGES="{"
  for LINE in $(.venv/bin/python -c "from pipeline.runner.catalog import build_order, load_images; i = load_images(); print(' '.join(f'{r}={i[r][\"platform\"]}' for r in build_order(i)))"); do
    REF=${LINE%%=*}; PLATFORM=${LINE#*=}
    IREPO=gwashub-images/$REF
    ensure_repo "$IREPO"
    # Tagged locally as <owner>/<image> too, so images built FROM gwashub/base find it.
    if ! aws ecr describe-images --repository-name "$IREPO" --image-ids imageTag="$TAG" >/dev/null 2>&1; then
      docker build -q --platform "$PLATFORM" -f "pipeline/images/$REF/Dockerfile" -t "$REF" -t "$REGISTRY/$IREPO:$TAG" . >/dev/null
      docker push -q "$REGISTRY/$IREPO:$TAG" >/dev/null
      docker rmi "$REGISTRY/$IREPO:$TAG" >/dev/null  # in ECR now; the local <owner>/<image> tag stays (images build FROM it)
    fi
    DIGEST=$(aws ecr describe-images --repository-name "$IREPO" --image-ids imageTag="$TAG" --query 'imageDetails[0].imageDigest' --output text)
    echo "  $REF $DIGEST ($PLATFORM)"
    PIPELINE_IMAGES="$PIPELINE_IMAGES\"$REF\": {\"digest\": \"$DIGEST\", \"platform\": \"$PLATFORM\"},"
  done
  PIPELINE_IMAGES="${PIPELINE_IMAGES%,}}"
}
