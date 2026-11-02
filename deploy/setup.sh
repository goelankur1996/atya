#!/usr/bin/env bash
# One-time AWS setup for ATYA. Run from the repo on your Mac:  deploy/setup.sh
# Safe to re-run: every step skips what already exists.
#
# Creates: ECR repo 'atya', S3 bucket for backups, IAM role for the server,
# secrets in SSM Parameter Store (/atya/...), a security group with NO inbound
# access (the server is managed through AWS Systems Manager, not SSH), and one
# t4g.small server. Then builds and pushes the app image and boots the server.
set -euo pipefail
cd "$(dirname "$0")/.."

export AWS_REGION=${AWS_REGION:-us-east-1} AWS_DEFAULT_REGION=${AWS_REGION:-us-east-1}
NAME=atya
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
BUCKET="atya-backups-$ACCOUNT"
ENV_FILE=${ENV_FILE:-.env}
V1_ENV=${V1_ENV:-$HOME/Desktop/AssistantBot/PersonalAssistantBot/.env}
step() { printf '\n== %s\n' "$*"; }

step "Container registry (ECR)"
aws ecr describe-repositories --repository-names "$NAME" >/dev/null 2>&1 \
  || aws ecr create-repository --repository-name "$NAME" --image-scanning-configuration scanOnPush=true >/dev/null
echo "ok: $ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com/$NAME"

step "Backup bucket (S3)"
if ! aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  if [ "$AWS_REGION" = us-east-1 ]; then aws s3api create-bucket --bucket "$BUCKET" >/dev/null
  else aws s3api create-bucket --bucket "$BUCKET" --create-bucket-configuration LocationConstraint="$AWS_REGION" >/dev/null; fi
fi
aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" --lifecycle-configuration \
  '{"Rules":[{"ID":"backups-30d","Status":"Enabled","Filter":{"Prefix":"backups/"},"Expiration":{"Days":30}},{"ID":"exports-7d","Status":"Enabled","Filter":{"Prefix":"exports/"},"Expiration":{"Days":7}}]}'
echo "ok: s3://$BUCKET (private; backups kept 30 days)"

step "Server role (IAM)"
if ! aws iam get-role --role-name atya-ec2 >/dev/null 2>&1; then
  aws iam create-role --role-name atya-ec2 --assume-role-policy-document \
    '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null
fi
aws iam attach-role-policy --role-name atya-ec2 --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
aws iam attach-role-policy --role-name atya-ec2 --policy-arn arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly
aws iam put-role-policy --role-name atya-ec2 --policy-name atya-app --policy-document "{
  \"Version\": \"2012-10-17\",
  \"Statement\": [
    {\"Effect\": \"Allow\", \"Action\": [\"ssm:GetParametersByPath\", \"ssm:GetParameter\", \"ssm:GetParameters\"],
     \"Resource\": \"arn:aws:ssm:$AWS_REGION:$ACCOUNT:parameter/atya/*\"},
    {\"Effect\": \"Allow\", \"Action\": [\"ssm:GetParametersByPath\"],
     \"Resource\": \"arn:aws:ssm:$AWS_REGION:$ACCOUNT:parameter/atya\"},
    {\"Effect\": \"Allow\", \"Action\": [\"s3:PutObject\", \"s3:GetObject\", \"s3:ListBucket\"],
     \"Resource\": [\"arn:aws:s3:::$BUCKET\", \"arn:aws:s3:::$BUCKET/*\"]}
  ]}"
if ! aws iam get-instance-profile --instance-profile-name atya-ec2 >/dev/null 2>&1; then
  aws iam create-instance-profile --instance-profile-name atya-ec2 >/dev/null
  aws iam add-role-to-instance-profile --instance-profile-name atya-ec2 --role-name atya-ec2
  sleep 15   # new instance profiles take a moment to become usable
fi
echo "ok: role atya-ec2"

step "Secrets (SSM Parameter Store, encrypted)"
put() { aws ssm put-parameter --name "/atya/$1" --value "$2" --type SecureString --overwrite >/dev/null; echo "set /atya/$1 (${#2} chars)"; }
from_env() { grep "^$1=" "$2" 2>/dev/null | head -1 | cut -d= -f2- || true; }
for k in TELEGRAM_BOT_TOKEN OWNER_TELEGRAM_USER_ID ANTHROPIC_API_KEY VOYAGE_API_KEY OWNER_TIMEZONE; do
  v=$(from_env "$k" "$ENV_FILE"); [ -n "$v" ] && put "$k" "$v"
done
nt=$(from_env NOTION_TOKEN "$V1_ENV"); [ -n "$nt" ] && put NOTION_TOKEN "$nt"
aws ssm get-parameter --name /atya/POSTGRES_PASSWORD >/dev/null 2>&1 || put POSTGRES_PASSWORD "$(openssl rand -hex 24)"
put BACKUP_BUCKET "$BUCKET"
put ECR_IMAGE "$ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com/$NAME:latest"

step "Build and push the app image"
deploy/deploy.sh --push-only

step "Network (security group with no inbound access)"
VPC=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)
[ "$VPC" = None ] && { echo "No default VPC in $AWS_REGION. Create one: aws ec2 create-default-vpc"; exit 1; }
SG=$(aws ec2 describe-security-groups --filters Name=group-name,Values=atya Name=vpc-id,Values="$VPC" \
  --query 'SecurityGroups[0].GroupId' --output text)
[ "$SG" = None ] && SG=$(aws ec2 create-security-group --group-name atya --vpc-id "$VPC" \
  --description "ATYA app server - no inbound; managed via SSM" --query GroupId --output text)
echo "ok: $SG"

step "Server (EC2 t4g.small)"
ID=$(aws ec2 describe-instances --filters Name=tag:Name,Values=atya Name=instance-state-name,Values=pending,running,stopping,stopped \
  --query 'Reservations[0].Instances[0].InstanceId' --output text)
if [ "$ID" = None ]; then
  AMI=$(aws ssm get-parameter --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64 --query Parameter.Value --output text)
  ID=$(aws ec2 run-instances --image-id "$AMI" --instance-type t4g.small \
    --iam-instance-profile Name=atya-ec2 --security-group-ids "$SG" \
    --user-data file://deploy/user-data.sh \
    --metadata-options HttpTokens=required,HttpEndpoint=enabled \
    --block-device-mappings '[{"DeviceName":"/dev/xvda","Ebs":{"VolumeSize":20,"VolumeType":"gp3","Encrypted":true}}]' \
    --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=atya}]' \
    --query 'Instances[0].InstanceId' --output text)
  echo "launched $ID; waiting for it to boot"
  aws ec2 wait instance-running --instance-ids "$ID"
else
  echo "exists: $ID"
fi

step "Waiting for the server to finish setting itself up (3-5 min)"
for i in $(seq 1 60); do
  if out=$(deploy/run.sh "test -f /opt/atya/bootstrapped && echo READY" 2>/dev/null) && grep -q READY <<<"$out"; then
    echo "server is up"; deploy/run.sh "cd /opt/atya && docker compose ps"
    exit 0
  fi
  sleep 10
done
echo "Not ready after 10 min. Check: deploy/run.sh 'tail -50 /var/log/cloud-init-output.log'"
exit 1
