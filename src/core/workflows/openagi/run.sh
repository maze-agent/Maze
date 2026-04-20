#!/bin/bash

echo "Stopping existing services..."

pkill -f redis-server 2>/dev/null || echo "Warning: could not stop some redis-server processes"
pkill -f ray 2>/dev/null || echo "Warning: could not stop some Ray processes"
pkill -f api_server.py 2>/dev/null || echo "Warning: could not stop some api_server.py processes"
pkill -f scheduler.py 2>/dev/null || echo "Warning: could not stop some scheduler.py processes"
sleep 3

echo "Starting core services..."

echo "Starting Redis..."

if [ -f "$CONDA_PREFIX/bin/redis-server" ]; then
    REDIS_SERVER="$CONDA_PREFIX/bin/redis-server"
    REDIS_CLI="$CONDA_PREFIX/bin/redis-cli"
    echo "  Using conda Redis: $REDIS_SERVER"
elif command -v redis-server &> /dev/null; then
    REDIS_SERVER="redis-server"
    REDIS_CLI="redis-cli"
    echo "  Using system Redis: $(which redis-server)"
else
    echo "ERROR: redis-server not found"
    exit 1
fi

REDIS_CONFIG="/tmp/redis_6380.conf"
cat > $REDIS_CONFIG << EOF
port 6380
bind 0.0.0.0
protected-mode no
daemonize yes
pidfile /tmp/redis_6380.pid
logfile /tmp/redis_6380.log
dir /tmp
EOF

echo "  Redis config: $REDIS_CONFIG"

echo "  Launching Redis..."
$REDIS_SERVER $REDIS_CONFIG

sleep 3

if pgrep -f "redis-server.*6380" > /dev/null; then
    echo "  Redis process is running"
else
    echo "  ERROR: Redis process not found"
    echo "  Redis log:"
    cat /tmp/redis_6380.log 2>/dev/null || echo "    (no log file)"
    exit 1
fi

echo "  Testing Redis..."
if $REDIS_CLI -p 6380 ping 2>/dev/null | grep -q PONG; then
    echo "Redis is up"
else
    echo "ERROR: Redis ping failed"
    exit 1
fi

echo "Starting Ray..."
ray stop > /dev/null 2>&1
ray start --head --port=6379 --object-manager-port=8076 --node-manager-port=8077 > /dev/null 2>&1
sleep 3

if ray status > /dev/null 2>&1; then
    echo "Ray is up"
else
    echo "ERROR: Ray failed to start"
    exit 1
fi

echo "Core services are ready."
echo ""
echo "Next (from maze-sc repository root):"
echo "  python src/core/resource/api_server.py --redis_ip 127.0.0.1 --redis_port 6380 --flask_port 5000"
echo "  python src/core/scheduler/scheduler.py --master_addr 127.0.0.1:5000 --redis_ip 127.0.0.1 --redis_port 6380 --strategy hacs --flask_port 5001"
echo "  python src/core/agent/dispatch_task.py --master_addr 127.0.0.1:5001"
