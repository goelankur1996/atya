#!/bin/bash
# First-boot bootstrap for the ATYA server (Amazon Linux 2023, arm64).
# Installs Docker, writes /opt/atya (compose file + helper scripts), schedules
# nightly backups to S3, then starts the app. Logs: /var/log/cloud-init-output.log
set -euxo pipefail

dnf install -y docker
systemctl enable --now docker
mkdir -p /usr/local/lib/docker/cli-plugins
curl -fsSL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-aarch64 \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

mkdir -p /opt/atya
cd /opt/atya

cat > docker-compose.yml <<'EOF'
name: atya
services:
  db:
    image: pgvector/pgvector:pg16
    restart: unless-stopped
    environment:
      POSTGRES_USER: atya
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: atya
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U atya -d atya"]
      interval: 5s
      retries: 20
    logging: {driver: json-file, options: {max-size: "10m", max-file: "3"}}
  app:
    image: ${ECR_IMAGE}
    restart: unless-stopped
    env_file: .env
    environment:
      DATABASE_URL: postgresql://atya:${POSTGRES_PASSWORD}@db:5432/atya
    depends_on:
      db: {condition: service_healthy}
    logging: {driver: json-file, options: {max-size: "10m", max-file: "5"}}
volumes:
  pgdata:
EOF

cat > region.sh <<'EOF'
#!/bin/bash
# Prints this instance's AWS region (IMDSv2).
T=$(curl -s -X PUT http://169.254.169.254/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
curl -s -H "X-aws-ec2-metadata-token: $T" http://169.254.169.254/latest/meta-data/placement/region
EOF

cat > fetch-env.sh <<'EOF'
#!/bin/bash
# Secrets live in SSM Parameter Store under /atya/; this writes them to .env (root-only).
set -euo pipefail
cd /opt/atya
aws ssm get-parameters-by-path --path /atya/ --with-decryption --region "$(./region.sh)" \
  --query 'Parameters[].[Name,Value]' --output text | sed -e 's|^/atya/||' -e 's|\t|=|' > .env.new
chmod 600 .env.new && mv .env.new .env
EOF

cat > update.sh <<'EOF'
#!/bin/bash
# Pull the latest image and (re)start. Run by deploy/deploy.sh from the Mac.
set -euo pipefail
cd /opt/atya
./fetch-env.sh
IMAGE=$(grep '^ECR_IMAGE=' .env | cut -d= -f2-)
aws ecr get-login-password --region "$(./region.sh)" | docker login --username AWS --password-stdin "${IMAGE%%/*}"
docker compose pull app
docker compose up -d
docker image prune -f >/dev/null
EOF

cat > backup.sh <<'EOF'
#!/bin/bash
# Nightly: compressed Postgres dump to S3 (expires after 30 days via bucket lifecycle).
set -euo pipefail
cd /opt/atya
BUCKET=$(grep '^BACKUP_BUCKET=' .env | cut -d= -f2-)
TS=$(date -u +%Y%m%dT%H%M%SZ)
docker compose exec -T db pg_dump -U atya -Fc atya | aws s3 cp - "s3://$BUCKET/backups/atya-$TS.dump" --region "$(./region.sh)"
echo "backup ok: s3://$BUCKET/backups/atya-$TS.dump"
EOF
chmod 700 *.sh

cat > /etc/systemd/system/atya-backup.service <<'EOF'
[Unit]
Description=ATYA nightly database backup to S3
[Service]
Type=oneshot
ExecStart=/opt/atya/backup.sh
EOF
cat > /etc/systemd/system/atya-backup.timer <<'EOF'
[Unit]
Description=ATYA nightly backup (21:30 UTC = 03:00 IST)
[Timer]
OnCalendar=*-*-* 21:30:00
Persistent=true
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now atya-backup.timer

./update.sh
touch /opt/atya/bootstrapped
