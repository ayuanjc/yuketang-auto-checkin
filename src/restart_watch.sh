#!/bin/bash
cd /opt/yktsign || exit 1
for p in $(pgrep -f "lesson_watch[.]py"); do
  if [ "$p" != "$$" ] && [ "$p" != "$PPID" ]; then kill "$p" 2>/dev/null; fi
done
sleep 1
setsid nohup python3 -u /opt/yktsign/lesson_watch.py "$1" "$2" "$3" > /opt/yktsign/watch_main.log 2>&1 < /dev/null &
sleep 9
tail -n 12 /opt/yktsign/watch_main.log
echo "--- events"
ls -l /opt/yktsign/events 2>&1 | head -5
