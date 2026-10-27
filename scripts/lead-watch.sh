#!/usr/bin/env bash
# The lead's watcher: prints one line per event worth acting on, then keeps polling.
#   PUSH  #n <sha> <author> <commit subject>   a PR into main got a new head
#   CI    #n <sha> pass|fail                    every check on that head finished
#   CARD  <status> #n <state> <title>           a card in Blocked or In review, a closed card not in
#                                               Done, or an open Decision issue
# Each event is printed once; a card is printed again only after 30 minutes out of those states.
# State lives outside the repo, so restarts don't repeat events. Only one watcher runs at a time.
# Usage: scripts/lead-watch.sh [poll-seconds]   (default 60)
set -uo pipefail

REPO=yassinebrahem2/agentic-mlops-pipeline
POLL=${1:-60}
FORGET_AFTER=1800
here=$(cd "$(dirname "$0")" && pwd)
state=${LEAD_WATCH_STATE:-${TMPDIR:-${TMP:-/tmp}}/lead-watch}
mkdir -p "$state"
touch "$state/heads" "$state/ci" "$state/cards"

if [ -f "$state/pid" ]; then kill "$(cat "$state/pid")" 2>/dev/null; fi
echo $$ >"$state/pid"

seen() { grep -qxF "$2" "$state/$1"; }
mark() { echo "$2" >>"$state/$1"; }

check_prs() {
  local prs
  prs=$(gh pr list -R $REPO --state open --base main \
    --json number,headRefOid,author,commits \
    --jq '.[] | [.number, .headRefOid[0:7], .author.login, (.commits[-1].messageHeadline // "")] | @tsv' 2>/dev/null) || return
  while IFS=$'\t' read -r n sha author subject; do
    [ -z "$n" ] && continue
    key="$n $sha"
    if ! seen heads "$key"; then mark heads "$key"; echo "PUSH #$n $sha $author $subject"; fi
    seen ci "$key" && continue
    buckets=$(gh pr checks "$n" -R $REPO --json bucket --jq '[.[].bucket] | unique | join(",")' 2>/dev/null) || continue
    [ -z "$buckets" ] && continue
    case $buckets in *pending*) continue ;; esac
    mark ci "$key"
    case $buckets in pass|pass,skipping|skipping,pass) echo "CI #$n $sha pass" ;; *) echo "CI #$n $sha fail ($buckets)" ;; esac
  done <<<"$prs"
}

check_cards() {
  local now cur
  now=$(date +%s)
  cur=$("$here/board-list.sh" all 2>/dev/null | awk -F'\t' '
    ($3 == "OPEN" && ($1 == "Blocked" || $1 == "In review")) ||
    ($3 != "OPEN" && $1 != "Done") ||
    ($3 == "OPEN" && $6 ~ /^\[Decision\]/) { print $1 "\t" $2 "\t" $3 "\t" substr($6, 1, 80) }' | sort) || return
  while IFS=$'\t' read -r status num st title; do
    [ -z "$status" ] && continue
    grep -qF "$status"$'\t'"$num"$'\t' "$state/cards" || echo "CARD $status $num $st $title"
  done <<<"$cur"
  {
    while IFS=$'\t' read -r status num _; do
      [ -n "$status" ] && printf '%s\t%s\t%s\n' "$status" "$num" "$now"
    done <<<"$cur"
    while IFS=$'\t' read -r status num t; do
      [ -z "$status" ] && continue
      printf '%s\n' "$cur" | cut -f1,2 | grep -qxF "$status"$'\t'"$num" && continue
      if [ $((now - t)) -lt $FORGET_AFTER ]; then printf '%s\t%s\t%s\n' "$status" "$num" "$t"; fi
    done <"$state/cards"
  } >"$state/cards.tmp"
  mv "$state/cards.tmp" "$state/cards"
}

while :; do
  check_prs
  check_cards
  sleep "$POLL"
done
