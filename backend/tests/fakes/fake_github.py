"""A fake GitHub REST server: in-memory PRs, issues, labels and comments with scripted failures.

Used by backend tests and end-to-end tests.
Run it alone with: python -m tests.fakes.fake_github --port N
"""

from __future__ import annotations

import argparse
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response


@dataclass
class FakeGitHubState:
    required_token: str | None = None
    rate_limit_remaining: int = 0
    simulate_permission_error: dict[str, int] = field(default_factory=dict[str, int])
    prs: dict[tuple[str, int], dict[str, Any]] = field(
        default_factory=dict[tuple[str, int], dict[str, Any]]
    )
    pr_files: dict[tuple[str, int], list[dict[str, Any]]] = field(
        default_factory=dict[tuple[str, int], list[dict[str, Any]]]
    )
    pr_counters: dict[str, int] = field(default_factory=dict[str, int])
    issues: dict[tuple[str, int], dict[str, Any]] = field(
        default_factory=dict[tuple[str, int], dict[str, Any]]
    )
    issue_counters: dict[str, int] = field(default_factory=dict[str, int])
    deleted_issues: set[tuple[str, int]] = field(default_factory=set[tuple[str, int]])
    comments: dict[tuple[str, int], list[dict[str, Any]]] = field(
        default_factory=dict[tuple[str, int], list[dict[str, Any]]]
    )
    comment_counter: int = 0
    labels: dict[str, set[str]] = field(default_factory=dict[str, set[str]])
    workflow_runs: dict[str, list[dict[str, Any]]] = field(
        default_factory=dict[str, list[dict[str, Any]]]
    )
    check_runs: dict[tuple[str, str], list[dict[str, Any]]] = field(
        default_factory=dict[tuple[str, str], list[dict[str, Any]]]
    )
    requests: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    def merge_pull(self, repo: str, number: int) -> dict[str, Any]:
        key = (repo, number)
        if key not in self.prs:
            raise KeyError(f"PR #{number} not found in {repo}")
        self.prs[key]["merged"] = True
        self.prs[key]["state"] = "closed"
        self.prs[key]["merged_at"] = datetime.now(UTC).isoformat()
        return self.prs[key]

    def close_pull(self, repo: str, number: int) -> dict[str, Any]:
        key = (repo, number)
        if key not in self.prs:
            raise KeyError(f"PR #{number} not found in {repo}")
        self.prs[key]["state"] = "closed"
        return self.prs[key]

    def delete_issue(self, repo: str, number: int) -> None:
        """Delete an issue as GitHub does: reading it then answers 410 Gone."""
        self.issues.pop((repo, number), None)
        self.deleted_issues.add((repo, number))

    def reset(self) -> None:
        self.required_token = None
        self.rate_limit_remaining = 0
        self.simulate_permission_error.clear()
        self.prs.clear()
        self.pr_files.clear()
        self.pr_counters.clear()
        self.issues.clear()
        self.issue_counters.clear()
        self.deleted_issues.clear()
        self.comments.clear()
        self.comment_counter = 0
        self.labels.clear()
        self.workflow_runs.clear()
        self.check_runs.clear()
        self.requests.clear()


def create_fake_github_app(state: FakeGitHubState) -> FastAPI:
    app = FastAPI(title="Fake GitHub")

    def _check_common(request: Request, needed_permission: str | None = None) -> Response | None:
        auth = request.headers.get("authorization", "")
        if state.required_token is not None and auth != f"Bearer {state.required_token}":
            return JSONResponse({"message": "Bad credentials"}, status_code=401)
        if state.rate_limit_remaining > 0:
            state.rate_limit_remaining -= 1
            return JSONResponse(
                {"message": "API rate limit exceeded"},
                status_code=429,
                headers={"retry-after": "60"},
            )
        if needed_permission and needed_permission in state.simulate_permission_error:
            status = state.simulate_permission_error[needed_permission]
            return JSONResponse(
                {"message": "Resource not accessible by integration"}, status_code=status
            )
        return None

    @app.get("/user")
    async def get_user(request: Request) -> Response:
        state.requests.append({"method": "GET", "path": "/user"})
        err = _check_common(request)
        if err is not None:
            return err
        return JSONResponse({"login": "fake-user"})

    @app.post("/repos/{owner}/{repo}/pulls")
    async def create_pull(owner: str, repo: str, request: Request) -> Response:
        full_name = f"{owner}/{repo}"
        body: dict[str, Any] = await request.json()
        state.requests.append({"method": "POST", "path": f"/repos/{full_name}/pulls", "body": body})
        err = _check_common(request, "pull requests: write")
        if err is not None:
            return err

        number = state.pr_counters.get(full_name, 0) + 1
        state.pr_counters[full_name] = number
        key = (full_name, number)
        pr = {
            "number": number,
            "title": body.get("title", ""),
            "body": body.get("body"),
            "head": body.get("head", ""),
            "base": body.get("base", ""),
            "html_url": f"https://github.com/{full_name}/pull/{number}",
            "state": "open",
            "merged": False,
            "merged_at": None,
        }
        state.prs[key] = pr
        resp = dict(pr)
        resp["changed_files"] = len(state.pr_files.get(key, []))
        return JSONResponse(resp, status_code=201)

    @app.get("/repos/{owner}/{repo}/pulls/{number}")
    async def get_pull(owner: str, repo: str, number: int, request: Request) -> Response:
        full_name = f"{owner}/{repo}"
        state.requests.append({"method": "GET", "path": f"/repos/{full_name}/pulls/{number}"})
        err = _check_common(request, "pull requests: write")
        if err is not None:
            return err

        key = (full_name, number)
        if key not in state.prs:
            return JSONResponse({"message": "Not Found"}, status_code=404)
        pr = dict(state.prs[key])
        if "changed_files" not in pr:
            pr["changed_files"] = len(state.pr_files.get(key, []))
        if "mergeable" not in pr:
            pr["mergeable"] = True
        if "mergeable_state" not in pr:
            pr["mergeable_state"] = "clean"
        if "head" not in pr:
            pr["head"] = {"sha": pr.get("head_sha", "fake_head_sha"), "ref": "branch"}
        elif isinstance(pr["head"], str):
            pr["head"] = {"sha": pr.get("head_sha", "fake_head_sha"), "ref": pr["head"]}
        elif isinstance(pr["head"], dict) and "sha" not in pr["head"]:
            pr["head"]["sha"] = pr.get("head_sha", "fake_head_sha")
        return JSONResponse(pr)

    @app.get("/repos/{owner}/{repo}/pulls/{number}/files")
    async def list_pull_files_route(
        owner: str, repo: str, number: int, request: Request, per_page: int = 30, page: int = 1
    ) -> Response:
        full_name = f"{owner}/{repo}"
        state.requests.append(
            {
                "method": "GET",
                "path": f"/repos/{full_name}/pulls/{number}/files",
                "params": {"per_page": per_page, "page": page},
            }
        )
        err = _check_common(request, "pull requests: write")
        if err is not None:
            return err

        key = (full_name, number)
        if key not in state.prs:
            return JSONResponse({"message": "Not Found"}, status_code=404)

        files = state.pr_files.get(key, [])
        start = (page - 1) * per_page
        end = start + per_page
        return JSONResponse(files[start:end])

    @app.patch("/repos/{owner}/{repo}/pulls/{number}")
    async def update_pull(owner: str, repo: str, number: int, request: Request) -> Response:
        full_name = f"{owner}/{repo}"
        body: dict[str, Any] = await request.json()
        state.requests.append(
            {"method": "PATCH", "path": f"/repos/{full_name}/pulls/{number}", "body": body}
        )
        err = _check_common(request, "pull requests: write")
        if err is not None:
            return err

        key = (full_name, number)
        if key not in state.prs:
            return JSONResponse({"message": "Not Found"}, status_code=404)
        pr = state.prs[key]
        if "title" in body:
            pr["title"] = body["title"]
        if "body" in body:
            pr["body"] = body["body"]
        if "state" in body:
            pr["state"] = body["state"]
        return JSONResponse(pr)

    @app.put("/repos/{owner}/{repo}/pulls/{number}/merge")
    async def merge_pull_route(owner: str, repo: str, number: int, request: Request) -> Response:
        full_name = f"{owner}/{repo}"
        state.requests.append({"method": "PUT", "path": f"/repos/{full_name}/pulls/{number}/merge"})
        err = _check_common(request, "pull requests: write")
        if err is not None:
            return err

        key = (full_name, number)
        if key not in state.prs:
            return JSONResponse({"message": "Not Found"}, status_code=404)
        pr = state.prs[key]
        raw_body = await request.body()
        body: dict[str, Any] = await request.json() if raw_body else {}
        expected_sha = body.get("sha")
        actual_sha = pr.get("head_sha")
        if actual_sha is None:
            pr_head: Any = pr.get("head")
            if isinstance(pr_head, dict):
                actual_sha = cast(dict[str, Any], pr_head).get("sha")
            elif isinstance(pr_head, str):
                actual_sha = "fake_head_sha"
            else:
                actual_sha = "fake_head_sha"
        if expected_sha is not None and actual_sha is not None and expected_sha != actual_sha:
            return JSONResponse(
                {"message": "Head branch was modified. Review and try the merge again."},
                status_code=409,
            )
        state.prs[key]["merged"] = True
        state.prs[key]["state"] = "closed"
        state.prs[key]["merged_at"] = datetime.now(UTC).isoformat()
        return JSONResponse(
            {
                "sha": "fake_sha_merged",
                "merged": True,
                "message": "Pull Request successfully merged",
            }
        )

    @app.get("/search/issues")
    async def search_issues(request: Request, q: str = "") -> Response:
        state.requests.append({"method": "GET", "path": "/search/issues", "params": {"q": q}})
        err = _check_common(request, "issues: write")
        if err is not None:
            return err

        repo_match = re.search(r"repo:([^\s]+)", q)
        target_repo = repo_match.group(1) if repo_match else None

        q_terms = [
            t.lower()
            for t in q.split()
            if not t.startswith("repo:") and t not in ("is:issue", "is:open", "is:closed")
        ]

        items: list[dict[str, Any]] = []
        for (repo_name, _num), issue in state.issues.items():
            if target_repo and repo_name != target_repo:
                continue
            if "is:open" in q and issue.get("state") != "open":
                continue
            if "is:closed" in q and issue.get("state") != "closed":
                continue
            if q_terms:
                title = issue.get("title", "").lower()
                body = (issue.get("body") or "").lower()
                if not all(term in title or term in body for term in q_terms):
                    continue
            items.append(issue)

        return JSONResponse(
            {"total_count": len(items), "incomplete_results": False, "items": items}
        )

    @app.post("/repos/{owner}/{repo}/issues")
    async def create_issue(owner: str, repo: str, request: Request) -> Response:
        full_name = f"{owner}/{repo}"
        body: dict[str, Any] = await request.json()
        state.requests.append(
            {"method": "POST", "path": f"/repos/{full_name}/issues", "body": body}
        )
        err = _check_common(request, "issues: write")
        if err is not None:
            return err

        number = state.issue_counters.get(full_name, 0) + 1
        state.issue_counters[full_name] = number
        raw_labels_obj: object = body.get("labels")
        raw_labels: list[object] = (
            cast(list[object], raw_labels_obj) if isinstance(raw_labels_obj, list) else []
        )
        labels: list[dict[str, Any]] = [
            {"name": lbl} if isinstance(lbl, str) else (lbl if isinstance(lbl, dict) else {})
            for lbl in raw_labels
        ]
        issue: dict[str, Any] = {
            "number": number,
            "title": str(body.get("title", "")),
            "body": body.get("body"),
            "html_url": f"https://github.com/{full_name}/issues/{number}",
            "state": "open",
            "labels": labels,
        }
        state.issues[(full_name, number)] = issue
        return JSONResponse(issue, status_code=201)

    @app.get("/repos/{owner}/{repo}/issues/{number}")
    async def get_issue(owner: str, repo: str, number: int, request: Request) -> Response:
        full_name = f"{owner}/{repo}"
        state.requests.append({"method": "GET", "path": f"/repos/{full_name}/issues/{number}"})
        err = _check_common(request, "issues: write")
        if err is not None:
            return err

        key = (full_name, number)
        if key in state.deleted_issues:
            return JSONResponse({"message": "This issue was deleted"}, status_code=410)
        if key not in state.issues:
            return JSONResponse({"message": "Not Found"}, status_code=404)
        return JSONResponse(state.issues[key])

    @app.post("/repos/{owner}/{repo}/issues/{number}/comments")
    async def create_comment(owner: str, repo: str, number: int, request: Request) -> Response:
        full_name = f"{owner}/{repo}"
        body: dict[str, Any] = await request.json()
        state.requests.append(
            {"method": "POST", "path": f"/repos/{full_name}/issues/{number}/comments", "body": body}
        )
        err = _check_common(request, "issues: write")
        if err is not None:
            return err

        state.comment_counter += 1
        cid = state.comment_counter
        comment = {
            "id": cid,
            "body": body.get("body", ""),
            "html_url": f"https://github.com/{full_name}/issues/{number}#issuecomment-{cid}",
        }
        state.comments.setdefault((full_name, number), []).append(comment)
        return JSONResponse(comment, status_code=201)

    @app.get("/repos/{owner}/{repo}/issues/{number}/comments")
    async def list_comments(owner: str, repo: str, number: int, request: Request) -> Response:
        full_name = f"{owner}/{repo}"
        state.requests.append(
            {"method": "GET", "path": f"/repos/{full_name}/issues/{number}/comments"}
        )
        err = _check_common(request, "issues: write")
        if err is not None:
            return err

        return JSONResponse(state.comments.get((full_name, number), []))

    @app.post("/repos/{owner}/{repo}/labels")
    async def create_label(owner: str, repo: str, request: Request) -> Response:
        full_name = f"{owner}/{repo}"
        body: dict[str, Any] = await request.json()
        state.requests.append(
            {"method": "POST", "path": f"/repos/{full_name}/labels", "body": body}
        )
        err = _check_common(request, "issues: write")
        if err is not None:
            return err

        name = body.get("name", "")
        repo_labels = state.labels.setdefault(full_name, set())
        if name in repo_labels:
            return JSONResponse(
                {
                    "message": "Validation Failed",
                    "errors": [{"resource": "Label", "code": "already_exists", "field": "name"}],
                },
                status_code=422,
            )
        repo_labels.add(name)
        return JSONResponse({"name": name}, status_code=201)

    @app.get("/repos/{owner}/{repo}/labels")
    async def list_labels(owner: str, repo: str, request: Request) -> Response:
        full_name = f"{owner}/{repo}"
        state.requests.append({"method": "GET", "path": f"/repos/{full_name}/labels"})
        err = _check_common(request, "issues: write")
        if err is not None:
            return err

        repo_labels = state.labels.get(full_name, set())
        return JSONResponse([{"name": lbl} for lbl in sorted(repo_labels)])

    def _parse_iso(s: str) -> datetime:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt

    @app.get("/repos/{owner}/{repo}/actions/runs")
    async def list_actions_runs(owner: str, repo: str, request: Request) -> Response:
        full_name = f"{owner}/{repo}"
        state.requests.append(
            {
                "method": "GET",
                "path": f"/repos/{full_name}/actions/runs",
                "params": dict(request.query_params),
            }
        )
        err = _check_common(request, "actions: read")
        if err is not None:
            return err

        runs = state.workflow_runs.get(full_name)
        if runs is None:
            runs = state.workflow_runs.get(repo, [])

        created = request.query_params.get("created")
        if created:
            filter_str = created[2:] if created.startswith(">=") else created
            filter_dt = _parse_iso(filter_str)
            filtered_runs: list[dict[str, Any]] = []
            for r in runs:
                raw_created = r.get("created_at")
                if raw_created and _parse_iso(str(raw_created)) >= filter_dt:
                    filtered_runs.append(r)
        else:
            filtered_runs = list(runs)

        page = int(request.query_params.get("page", 1))
        per_page = int(request.query_params.get("per_page", 100))
        start = (page - 1) * per_page
        end = start + per_page

        return JSONResponse(
            {
                "total_count": len(filtered_runs),
                "workflow_runs": filtered_runs[start:end],
            }
        )

    @app.get("/repos/{owner}/{repo}/commits/{ref}/check-runs")
    async def list_check_runs(owner: str, repo: str, ref: str, request: Request) -> Response:
        full_name = f"{owner}/{repo}"
        state.requests.append(
            {"method": "GET", "path": f"/repos/{full_name}/commits/{ref}/check-runs"}
        )
        err = _check_common(request)
        if err is not None:
            return err

        runs = state.check_runs.get((full_name, ref))
        if runs is None:
            runs = state.check_runs.get((repo, ref), [])
        return JSONResponse({"total_count": len(runs), "check_runs": runs})

    return app


@dataclass
class RunningFakeGitHub:
    base_url: str
    state: FakeGitHubState
    server: uvicorn.Server
    thread: threading.Thread

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def start_fake_github(state: FakeGitHubState, port: int = 0) -> RunningFakeGitHub:
    port = port or _free_port()
    config = uvicorn.Config(
        create_fake_github_app(state), host="127.0.0.1", port=port, log_level="warning"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("the fake GitHub server did not start within 10 seconds")
        time.sleep(0.02)
    return RunningFakeGitHub(f"http://127.0.0.1:{port}", state, server, thread)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fake GitHub REST server for tests")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    uvicorn.run(
        create_fake_github_app(FakeGitHubState()),
        host="127.0.0.1",
        port=args.port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
