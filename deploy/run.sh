#!/usr/bin/env bash
# Run a shell command on the ATYA server (as root) via AWS Systems Manager and
# print its output. No SSH or open ports needed.
# The command is written to a file and run with stdin closed, so a step that
# reads stdin (e.g. docker compose run) cannot swallow the rest of the script.
#   deploy/run.sh "cd /opt/atya && docker compose logs --tail=50 app"
set -euo pipefail
export AWS_REGION=${AWS_REGION:-us-east-1} AWS_DEFAULT_REGION=${AWS_REGION:-us-east-1}
[ $# -ge 1 ] || { echo "usage: deploy/run.sh '<command>'"; exit 2; }
ID=$(aws ec2 describe-instances --filters Name=tag:Name,Values=atya Name=instance-state-name,Values=running \
  --query 'Reservations[0].Instances[0].InstanceId' --output text)
[ "$ID" != None ] || { echo "no running server tagged Name=atya"; exit 1; }
B64=$(printf '%s' "$*" | base64 | tr -d '\n')
CID=$(aws ssm send-command --instance-ids "$ID" --document-name AWS-RunShellScript \
  --parameters "{\"commands\":[\"echo $B64 | base64 -d > /tmp/atya-run.sh && bash /tmp/atya-run.sh < /dev/null\"],\"executionTimeout\":[\"1800\"]}" \
  --query Command.CommandId --output text)
while :; do
  sleep 2
  STATUS=$(aws ssm get-command-invocation --command-id "$CID" --instance-id "$ID" --query Status --output text 2>/dev/null || echo Pending)
  case "$STATUS" in Pending|InProgress|Delayed) continue ;; esac
  break
done
aws ssm get-command-invocation --command-id "$CID" --instance-id "$ID" --query StandardOutputContent --output text
ERR=$(aws ssm get-command-invocation --command-id "$CID" --instance-id "$ID" --query StandardErrorContent --output text)
[ -n "$ERR" ] && [ "$ERR" != None ] && echo "$ERR" >&2
[ "$STATUS" = Success ]
