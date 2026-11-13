#!/usr/bin/env bash
# Email alerts on AWS spending, counted before credits (so it tracks what the
# credits are paying for). Safe to re-run: it replaces the budget.
#   deploy/budget-alert.sh 500 you@example.com      # $500 a month; alerts at 50%, 80%, 100%
# Plus a daily check: an alert if any single day goes over a fifth of the monthly amount.
set -euo pipefail
export AWS_REGION=${AWS_REGION:-us-east-1} AWS_DEFAULT_REGION=${AWS_REGION:-us-east-1}
AMOUNT=${1:?usage: deploy/budget-alert.sh <monthly USD> <email>}
EMAIL=${2:?usage: deploy/budget-alert.sh <monthly USD> <email>}
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
DAILY=$(( AMOUNT / 5 ))

make_budget() {   # name, amount, period, thresholds...
  local name=$1 amount=$2 period=$3; shift 3
  aws budgets delete-budget --account-id "$ACCOUNT" --budget-name "$name" >/dev/null 2>&1 || true
  local notes=""
  for pct in "$@"; do
    notes+="{\"Notification\":{\"NotificationType\":\"ACTUAL\",\"ComparisonOperator\":\"GREATER_THAN\",\"Threshold\":$pct,\"ThresholdType\":\"PERCENTAGE\"},\"Subscribers\":[{\"SubscriptionType\":\"EMAIL\",\"Address\":\"$EMAIL\"}]},"
  done
  aws budgets create-budget --account-id "$ACCOUNT" \
    --budget "{\"BudgetName\":\"$name\",\"BudgetLimit\":{\"Amount\":\"$amount\",\"Unit\":\"USD\"},\"TimeUnit\":\"$period\",\"BudgetType\":\"COST\",
               \"CostTypes\":{\"IncludeCredit\":false,\"IncludeRefund\":false,\"IncludeTax\":true,\"IncludeSubscription\":true,\"UseBlended\":false}}" \
    --notifications-with-subscribers "[${notes%,}]"
  echo "ok: $name \$$amount ($period)"
}

make_budget atya-monthly "$AMOUNT" MONTHLY 50 80 100
make_budget atya-daily "$DAILY" DAILY 100
echo
echo "AWS emails $EMAIL to confirm the first alert. Spending isn't stopped automatically;"
echo "the app's own daily caps (LIMIT_* settings) do the stopping."
