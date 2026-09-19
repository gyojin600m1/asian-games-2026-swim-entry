#!/bin/zsh
cd "$(dirname "$0")"
echo "=== アジア競技大会2026 競泳 速報の見張り ==="
launchctl print "gui/$(id -u)/com.fukuda.swim.asian-games-2026" >/dev/null 2>&1 && echo "見張り: 動いています（1分おき・9:30〜19:59）" || echo "見張り: 止まっています"
[ -f .halted ] && echo "⚠ 停止中: $(cat .halted)"
echo
echo "--- いま取りに行く ---"
python3 update.py --now
echo
echo "--- ログ（最後の15行）---"
tail -15 log.txt 2>/dev/null
echo
echo "https://gyojin600m1.github.io/asian-games-2026-swim-entry/"
read -k1 "?閉じるには何かキーを押してください"
