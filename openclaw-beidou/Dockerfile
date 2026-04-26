FROM ghcr.io/openclaw/openclaw:latest

# Copy Beidou agent config
COPY config/openclaw.json /home/node/.openclaw/openclaw.json

# Copy Beidou skills
COPY skills/ /home/node/.openclaw/skills/

# Verify config on build
RUN node -e "const c = require('/home/node/.openclaw/openclaw.json'); console.log('Config OK:', Object.keys(c.agents.list).length || c.agents.list.length, 'agents');"

EXPOSE 18789

# Use the default OpenClaw entrypoint
