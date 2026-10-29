#!/usr/bin/env bash
# The lead's watcher: prints one line per event worth acting on, then keeps polling.
#   PUSH  <repo>#n <sha> <author> <subject>     a PR into main got a new head (this repo or the demo repo)
#   CI    <repo>#n <sha> pass|fail              every check on that head finished
#   CARD  <status> #n <state> <title>           a card in Blocked or In review, a closed card not in
#                                               Done, or an open Decision issue
#   REREVIEW <repo>#n <sha>                     changes were requested and the owner pushed since:
#                                               the PR waits on the lead
#   OWNER <repo>#n                              changes were requested and nothing was pushed since:
#                                               the PR waits on its owner (its card is moved to
#                                               In progress when it sits in In review)
# Each event is printed once; a card is printed again only after 30 minutes out of those states.
# State lives outside the repo, so restarts don't repeat events. Only one watcher runs at a time.
# Usage: scripts/lead-watch.sh [poll-seconds]   (default 60)
set -uo pipefail

REPOS="yassinebrahem2/agentic-mlops-pipeline yassinebrahem2/hotel-cancellation-demo"
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
  local repo prs short
  for repo in $REPOS; do
    short=${repo#*/}
    prs=$(gh pr list -R "$repo" --state open --base main       --json number,headRefOid,author,commits       --jq '.[] | [.number, .headRefOid[0:7], .author.login, (.commits[-1].messageHeadline // "")] | @tsv' 2>/dev/null) || continue
    while IFS=$'	' read -r n sha author subject; do
      [ -z "$n" ] && continue
      key="$short#$n $sha"
      if ! seen heads "$key"; then mark heads "$key"; echo "PUSH $short#$n $sha $author $subject"; fi
      seen ci "$key" && continue
      buckets=$(gh pr checks "$n" -R "$repo" --json bucket --jq '[.[].bucket] | unique | join(",")' 2>/dev/null) || continue
      [ -z "$buckets" ] && continue
      case $buckets in *pending*) continue ;; esac
      mark ci "$key"
      case $buckets in pass|pass,skipping|skipping,pass) echo "CI $short#$n $sha pass" ;; *) echo "CI $short#$n $sha fail ($buckets)" ;; esac
    done <<<"$prs"
  done
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

# The board's own "changes requested" move can miss a review, so this re-derives who a PR waits on
# from the last change request and the commits after it.
check_reviews() {
  local repo short rows n sha reviewed committed status
  touch "$state/reviews"
  for repo in $REPOS; do
    short=${repo#*/}
    rows=$(gh pr list -R "$repo" --state open --base main --json number,headRefOid,reviews,commits \
      --jq '.[] | [.number, .headRefOid[0:7],
        ([.reviews[] | select(.state == "CHANGES_REQUESTED" or .state == "APPROVED") ] | last | if . == null then "" else (.state + "@" + .submittedAt) end),
        (.commits[-1].committedDate // "")] | @tsv' 2>/dev/null) || continue
    while IFS=$'\t' read -r n sha reviewed committed; do
      case $reviewed in CHANGES_REQUESTED@*) ;; *) continue ;; esac
      if [[ "$committed" > "${reviewed#*@}" ]]; then
        seen reviews "rereview $short#$n $sha" || { mark reviews "rereview $short#$n $sha"; echo "REREVIEW $short#$n $sha"; }
      else
        seen reviews "owner $short#$n ${reviewed#*@}" || { mark reviews "owner $short#$n ${reviewed#*@}"; echo "OWNER $short#$n"; }
        if [ "$short" = agentic-mlops-pipeline ]; then
          # The PR and every open issue it closes, as board.yml's own changes-requested move does.
          for item in "$n" $(gh pr view "$n" -R "$repo" --json closingIssuesReferences \
              --jq '.closingIssuesReferences[].number' 2>/dev/null); do
            status=$("$here/board-list.sh" open 2>/dev/null | awk -F'\t' -v k="#$item" '$2 == k { print $1 }')
            [ "$status" = "In review" ] && "$here/board-status.sh" "$item" "In progress" >/dev/null 2>&1
          done
        fi
      fi
    done <<<"$rows"
  done
}

while :; do
  check_prs
  check_reviews
  check_cards
  sleep "$POLL"
done
