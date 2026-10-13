#!/bin/bash
# =============================================================================
#  One-click AWS EC2 setup for the Cloud-Based UHI Prediction platform
# -----------------------------------------------------------------------------
#  EITHER run it on the server (EC2 Instance Connect):
#    curl -fsSL https://raw.githubusercontent.com/rohitpawalia28/CloudBasedUrbanHeatIslandPrediction_Azure_Cloud_Project_2026/develop/scripts/aws_ec2_setup.sh | sudo bash
#  OR paste this whole file into  EC2 > Launch instance > Advanced details > User data
#  (Ubuntu 24.04 or 22.04 AMI). On first boot it:
#    1. installs Python, Git and Nginx
#    2. adds a 2 GB swap file (lets the free-tier t3.micro / t2.micro train models)
#    3. clones the project from GitHub into /opt/uhi
#    4. creates a virtual environment and installs requirements
#    5. writes /opt/uhi/.env with a random SECRET_KEY and region ap-south-1
#    6. runs the app as a systemd service (auto-start on reboot, auto-restart)
#    7. puts Nginx in front on port 80  ->  open  http://<EC2-public-IP>
#  Progress log on the server:  sudo tail -f /var/log/uhi-setup.log
#  Re-running the script (sudo bash aws_ec2_setup.sh) updates to the latest commit.
# =============================================================================
set -euo pipefail
exec > >(tee -a /var/log/uhi-setup.log) 2>&1
echo "=== UHI setup started $(date) ==="

REPO_URL="https://github.com/rohitpawalia28/CloudBasedUrbanHeatIslandPrediction_Azure_Cloud_Project_2026.git"
BRANCH="develop"
APP_DIR="/opt/uhi"
APP_USER="ubuntu"
id "$APP_USER" >/dev/null 2>&1 || APP_USER="$(logname 2>/dev/null || echo root)"

export DEBIAN_FRONTEND=noninteractive
# A fresh Ubuntu instance runs unattended-upgrades on first boot and holds the apt lock;
# wait for it (up to 10 min) instead of failing.
APT="apt-get -o DPkg::Lock::Timeout=600"
$APT update -y
$APT install -y python3 python3-venv python3-pip git nginx curl

# ---- swap (1 GB instances need it for scikit-learn training) ----------------
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
  chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# ---- code --------------------------------------------------------------------
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" fetch --depth 1 origin "$BRANCH"
  git -C "$APP_DIR" reset --hard "origin/$BRANCH"
else
  git clone --depth 1 -b "$BRANCH" "$REPO_URL" "$APP_DIR"
fi

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

# ---- configuration (kept across updates) ------------------------------------
if [ ! -f "$APP_DIR/.env" ]; then
  cat > "$APP_DIR/.env" <<EOF
CLOUD_MODE=emulated
AWS_REGION=ap-south-1
HOST=127.0.0.1
PORT=5000
SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')
ADMIN_EMAIL=admin@uhi.cloud
ADMIN_PASSWORD=Admin@123
EOF
fi
chown -R "$APP_USER":"$APP_USER" "$APP_DIR"
chmod 600 "$APP_DIR/.env"

# ---- systemd service ---------------------------------------------------------
cat > /etc/systemd/system/uhi.service <<EOF
[Unit]
Description=Cloud-Based Urban Heat Island Prediction platform
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/python run.py --host 127.0.0.1 --port 5000 --no-browser
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable uhi
systemctl restart uhi

# ---- nginx reverse proxy on port 80 ------------------------------------------
cat > /etc/nginx/sites-available/uhi <<'EOF'
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;
    client_max_body_size 50m;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300;
    }
}
EOF
ln -sf /etc/nginx/sites-available/uhi /etc/nginx/sites-enabled/uhi
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl enable nginx
systemctl restart nginx

TOKEN=$(curl -s -m 2 -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 60" || true)
IP=$(curl -s -m 2 -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/public-ipv4 || echo "<public-ip>")
echo "=== UHI setup finished $(date) ==="
echo "Open  http://$IP   (first start trains the models, ~1-2 minutes)"
echo "Login admin@uhi.cloud / Admin@123   |   logs: sudo journalctl -u uhi -f"
