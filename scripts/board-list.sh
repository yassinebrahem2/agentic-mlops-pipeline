#!/usr/bin/env bash
# List the project board's cards, one per line: status, #number, state, kind, assignees or author, title.
# Usage: scripts/board-list.sh [open|closed|all]   (default: open)
# Pages past GraphQL's 100-item limit. Needs gh with a token that has the project scope.
set -euo pipefail

OWNER=yassinebrahem2
PROJECT_NUMBER=5
which=${1:-open}
case $which in open|closed|all) ;; *) echo "usage: $0 [open|closed|all]" >&2; exit 2 ;; esac

query='query($owner: String!, $n: Int!, $c: String) {
  user(login: $owner) { projectV2(number: $n) { items(first: 100, after: $c) {
    pageInfo { hasNextPage endCursor }
    nodes {
      fieldValueByName(name: "Status") { ... on ProjectV2ItemFieldSingleSelectValue { name } }
      content {
        ... on Issue { number title state assignees(first: 5) { nodes { login } } }
        ... on PullRequest { number title state author { login } }
      }
    }
  } } }
}'

cursor=""
while :; do
  args=(-f query="$query" -f owner=$OWNER -F n=$PROJECT_NUMBER)
  [ -n "$cursor" ] && args+=(-f c="$cursor")
  page=$(gh api graphql "${args[@]}")
  jq -r --arg which "$which" '
    .data.user.projectV2.items.nodes[]
    | select(.content.number != null)
    | select($which == "all" or ($which == "open") == (.content.state == "OPEN"))
    | [ (.fieldValueByName.name // "-"),
        "#\(.content.number)",
        .content.state,
        (if .content.author then "pr" else "issue" end),
        (if .content.author then .content.author.login else ([.content.assignees.nodes[].login] | join(",")) end),
        .content.title ] | @tsv' <<<"$page"
  [ "$(jq -r '.data.user.projectV2.items.pageInfo.hasNextPage' <<<"$page")" = true ] || break
  cursor=$(jq -r '.data.user.projectV2.items.pageInfo.endCursor' <<<"$page")
done
