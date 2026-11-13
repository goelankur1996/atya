#!/usr/bin/env bash
# Switch on web research (Amazon Nova 2 Lite with Nova Web Grounding). Safe to re-run.
# Needs deploy/enable-bedrock.sh to have run once. Then: deploy/deploy.sh
set -euo pipefail
export AWS_REGION=${AWS_REGION:-us-east-1} AWS_DEFAULT_REGION=${AWS_REGION:-us-east-1}
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)

echo "== Web search permission for the server role"
aws iam put-role-policy --role-name atya-ec2 --policy-name atya-research --policy-document "{
  \"Version\": \"2012-10-17\",
  \"Statement\": [
    {\"Effect\": \"Allow\", \"Action\": [\"bedrock:InvokeTool\"],
     \"Resource\": [\"arn:aws:bedrock::$ACCOUNT:system-tool/amazon.nova_grounding\"]}
  ]}"
echo ok

echo "== Research on"
aws ssm put-parameter --name /atya/RESEARCH --type String --value on --overwrite >/dev/null
echo ok
echo
echo "Now run: deploy/deploy.sh"
