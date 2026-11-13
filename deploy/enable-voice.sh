#!/usr/bin/env bash
# Switch on voice notes (Amazon Transcribe). Safe to re-run.
# 1. Lets the server role run transcription jobs and stage audio in the
#    backups bucket under transcribe-tmp/ (each file is deleted right after).
# 2. Sets VOICE_NOTES=on in SSM. Run deploy/deploy.sh afterwards.
set -euo pipefail
export AWS_REGION=${AWS_REGION:-us-east-1} AWS_DEFAULT_REGION=${AWS_REGION:-us-east-1}
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
BUCKET="atya-backups-$ACCOUNT"
step() { printf '\n== %s\n' "$*"; }

step "Transcribe permissions for the server role"
aws iam put-role-policy --role-name atya-ec2 --policy-name atya-voice --policy-document "{
  \"Version\": \"2012-10-17\",
  \"Statement\": [
    {\"Effect\": \"Allow\",
     \"Action\": [\"transcribe:StartTranscriptionJob\", \"transcribe:GetTranscriptionJob\",
                \"transcribe:DeleteTranscriptionJob\"],
     \"Resource\": \"*\"},
    {\"Effect\": \"Allow\", \"Action\": [\"s3:PutObject\", \"s3:GetObject\", \"s3:DeleteObject\"],
     \"Resource\": \"arn:aws:s3:::$BUCKET/transcribe-tmp/*\"}
  ]}"
echo ok

step "Voice notes on"
aws ssm put-parameter --name /atya/VOICE_NOTES --type String --value on --overwrite >/dev/null
echo "ok (bucket $BUCKET)"
echo
echo "Now run: deploy/deploy.sh"
