#!/usr/bin/env bash
# Set the Status of an issue or PR on the project board.
# Usage: scripts/board-status.sh <number> "<Todo|In progress|Blocked|In review|Done>"
# Uses GraphQL directly: `gh project` needs the read:org scope, which most tokens lack.
set -euo pipefail

OWNER=yassinebrahem2
REPO=agentic-mlops-pipeline
PROJECT_NUMBER=5

if [ $# -ne 2 ]; then
  echo "usage: $0 <issue-or-pr-number> \"<status>\"" >&2
  exit 2
fi
number=$1
status=$2

project=$(gh api graphql -f query='
  query($owner: String!, $n: Int!) {
    user(login: $owner) {
      projectV2(number: $n) {
        id
        field(name: "Status") {
          ... on ProjectV2SingleSelectField { id options { id name } }
        }
      }
    }
  }' -f owner=$OWNER -F n=$PROJECT_NUMBER --jq '.data.user.projectV2')

project_id=$(jq -r '.id' <<<"$project")
field_id=$(jq -r '.field.id' <<<"$project")
option_id=$(jq -r --arg s "$status" '.field.options[] | select(.name == $s) | .id' <<<"$project")
if [ -z "$option_id" ]; then
  echo "unknown status \"$status\"; valid: $(jq -r '[.field.options[].name] | join(", ")' <<<"$project")" >&2
  exit 2
fi

content_id=$(gh api "repos/$OWNER/$REPO/issues/$number" --jq '.node_id')
find_item() {
  gh api graphql -f query='
    query($c: ID!) {
      node(id: $c) {
        ... on Issue { projectItems(first: 20) { nodes { id project { id } } } }
        ... on PullRequest { projectItems(first: 20) { nodes { id project { id } } } }
      }
    }' -f c="$content_id" --jq ".data.node.projectItems.nodes[] | select(.project.id == \"$project_id\") | .id"
}
# The board's auto-add workflows may add the issue at the same moment (adding a plan issue
# also adds its sub-issues), so an add that finds it already there looks it up again.
item_id=$(find_item)
if [ -z "$item_id" ]; then
  item_id=$(gh api graphql -f query='
    mutation($p: ID!, $c: ID!) {
      addProjectV2ItemById(input: {projectId: $p, contentId: $c}) { item { id } }
    }' -f p="$project_id" -f c="$content_id" --jq '.data.addProjectV2ItemById.item.id' 2>/dev/null) \
    || item_id=$(find_item)
fi
if [ -z "$item_id" ]; then
  echo "#$number could not be found on or added to the project" >&2
  exit 1
fi

gh api graphql -f query='
  mutation($p: ID!, $i: ID!, $f: ID!, $o: String!) {
    updateProjectV2ItemFieldValue(
      input: {projectId: $p, itemId: $i, fieldId: $f, value: {singleSelectOptionId: $o}}
    ) { projectV2Item { id } }
  }' -f p="$project_id" -f i="$item_id" -f f="$field_id" -f o="$option_id" >/dev/null

echo "#$number -> $status"
