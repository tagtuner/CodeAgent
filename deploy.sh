#!/bin/bash
set -euo pipefail

echo "=== CodeAgent Deployment ==="

# Create directories
mkdir -p /opt/codeagent/sessions
mkdir -p /opt/codeagent/skills/library

# Install Python deps
echo "[1/5] Installing Python dependencies..."
cd /opt/codeagent
pip3 install httpx fastapi uvicorn websockets textual pyyaml oracledb rich 2>&1 | tail -5

# Create sample skill
echo "[2/5] Creating sample EBS skill..."
cat > /opt/codeagent/skills/library/oracle_ebs_basics.md << 'SKILL_EOF'
---
name: oracle_ebs_basics
description: Oracle EBS fundamentals and common patterns
tags: [oracle, ebs, sql]
triggers: [ebs, oracle, po_headers, ap_invoices, purchase order, invoice]
---

# Oracle EBS SQL Patterns

## Key Rules
- Always use _all suffix tables (po_headers_all, not po_headers)
- Always filter by org_id for multi-org tables
- Use authorization_status = 'APPROVED' for approved POs
- Use NVL() for nullable numeric columns
- Date columns: use TRUNC() for date-only comparisons

## Common Patterns
- Pending POs: authorization_status IN ('IN PROCESS', 'INCOMPLETE', 'REQUIRES REAPPROVAL')
- Active suppliers: enabled_flag = 'Y' AND NVL(end_date_active, SYSDATE+1) > SYSDATE
- Unpaid invoices: payment_status_flag != 'Y'
SKILL_EOF

# Create systemd service for web UI
echo "[3/5] Creating systemd services..."
cat > /etc/systemd/system/codeagent-web.service << 'SVC_EOF'
[Unit]
Description=CodeAgent Web UI
After=network-online.target llama-server.service
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/codeagent
ExecStart=/usr/bin/python3 main.py web
Restart=always
RestartSec=5
Environment=PYTHONDONTWRITEBYTECODE=1

[Install]
WantedBy=multi-user.target
SVC_EOF

# Nginx proxy config
echo "[4/5] Configuring Nginx proxy..."
cat > /etc/nginx/conf.d/codeagent.conf << 'NGX_EOF'
server {
    listen 8083;
    server_name _;

    location / {
        proxy_pass http://127.0.0.1:4200;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }
}
NGX_EOF

# Enable and start
echo "[5/5] Starting services..."
systemctl daemon-reload
systemctl enable codeagent-web
systemctl restart codeagent-web

# Restart nginx if config is valid
nginx -t 2>/dev/null && systemctl reload nginx || echo "Nginx config check failed, skipping reload"

echo ""
echo "=== Deployment Complete ==="
echo "  Web UI:  http://$(hostname -I | awk '{print $1}'):8083"
echo "  TUI:     cd /opt/codeagent && python3 main.py tui"
echo "  CLI:     cd /opt/codeagent && python3 main.py chat 'your message'"
echo "  Status:  systemctl status codeagent-web"
echo ""
