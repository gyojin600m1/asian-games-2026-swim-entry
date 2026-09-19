#!/bin/zsh
cd "$(dirname "$0")"
echo "手動で止めた $(date '+%m/%d %H:%M')" > .halted
launchctl bootout "gui/$(id -u)/com.fukuda.swim.asian-games-2026" 2>/dev/null
echo "見張りを止めました。再開するには .halted を消して plist を bootstrap してください。"
read -k1 "?閉じるには何かキーを押してください"
