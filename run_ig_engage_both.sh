#!/bin/bash
# Run IG engage for both accounts sequentially with cooldown
cd /home/amit/tatami-bot
source venv/bin/activate

echo "Sun Mar  1 11:27:11 EST 2026: Starting tatamispaces IG engage"
python ig_engage.py --niche tatamispaces --max-likes 10 --max-comments 3 --max-follows 3 2>&1
echo "Sun Mar  1 11:27:11 EST 2026: tatamispaces done. Cooling down 5 minutes..."
sleep 300

echo "Sun Mar  1 11:27:11 EST 2026: Starting museumstories IG engage"
python ig_engage.py --niche museumstories --max-likes 10 --max-comments 3 --max-follows 3 2>&1
echo "Sun Mar  1 11:27:11 EST 2026: museumstories done."
