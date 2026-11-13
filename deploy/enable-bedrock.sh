#!/usr/bin/env bash
# Let the server call models on Amazon Bedrock. Safe to re-run.
# 1. Adds Bedrock permissions to the server's IAM role (no API key needed).
# 2. Lets programs inside Docker reach the server's role credentials
#    (instance metadata hop limit 2).
# Models are only used once LLM_PROVIDER=bedrock is set; until then this just
# allows the model tests (python -m atya.eval).
set -euo pipefail
export AWS_REGION=${AWS_REGION:-us-east-1} AWS_DEFAULT_REGION=${AWS_REGION:-us-east-1}
step() { printf '\n== %s\n' "$*"; }

ID=$(aws ec2 describe-instances --filters Name=tag:Name,Values=atya Name=instance-state-name,Values=running \
  --query 'Reservations[0].Instances[0].InstanceId' --output text)
[ "$ID" != None ] || { echo "no running server tagged Name=atya"; exit 1; }

step "Bedrock permissions for the server role"
aws iam put-role-policy --role-name atya-ec2 --policy-name atya-bedrock --policy-document '{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow",
     "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "bedrock:Converse",
                "bedrock:ConverseStream", "bedrock:ListFoundationModels", "bedrock:ListInferenceProfiles",
                "bedrock:GetFoundationModel", "bedrock:GetInferenceProfile"],
     "Resource": "*"}
  ]}'
echo ok

step "Credentials reachable from inside Docker"
aws ec2 modify-instance-metadata-options --instance-id "$ID" --http-put-response-hop-limit 2 \
  --http-endpoint enabled >/dev/null
echo ok
echo
echo "Done. Permissions can take a minute to apply."
