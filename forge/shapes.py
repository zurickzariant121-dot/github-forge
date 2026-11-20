"""REST shape rendering: repository, issue, pull request, and friends.

Each renderer takes the forge (for account lookup) and the object and
returns the public JSON dict. Issue and pull shapes are cached on the
object and keyed by its generation counter, so a mutation can never
serve a stale shape.
"""

from __future__ import annotations

import hashlib

from . import model, objects as obj

API = "https://api.github.com"
WEB = "https://github.com"


def _user(forge: model.ForgeState, login: str) -> dict:
    return forge.account(login).public()


def repo_shape(forge: model.ForgeState, repo: model.Repository) -> dict:
    open_count = sum(1 for i in repo.issues.values() if i.state == "open")
    open_count += sum(1 for p in repo.pulls.values() if p.state == "open")
    return {
        "id": repo.rid,
        "node_id": repo.node,
        "name": repo.name,
        "full_name": f"{repo.owner}/{repo.name}",
        "owner": _user(forge, repo.owner),
        "private": repo.private,
        "html_url": f"{WEB}/{repo.owner}/{repo.name}",
        "description": repo.description,
        "fork": False,
        "url": f"{API}/repos/{repo.owner}/{repo.name}",
        "created_at": repo.created_at,
        "updated_at": repo.updated_at,
        "pushed_at": repo.pushed_at,
        "homepage": None,
        "size": 0,
        "stargazers_count": 0,
        "watchers_count": 0,
        "language": None,
        "has_issues": True,
        "has_projects": True,
        "has_downloads": True,
        "has_wiki": True,
        "has_pages": False,
        "has_discussions": False,
        "forks_count": 0,
        "archived": False,
        "disabled": False,
        "open_issues_count": open_count,
        "license": None,
        "allow_forking": True,
        "is_template": False,
        "web_commit_signoff_required": False,
        "topics": [],
        "visibility": "private" if repo.private else "public",
        "default_branch": repo.default_branch,
        "permissions": {"admin": True, "maintain": True, "push": True,
                        "triage": True, "pull": True},
    }


def _milestone_view(m: dict | None) -> dict | None:
    if m is None:
        return None
    return {
        "url": m.get("url", ""),
        "html_url": m.get("html_url", ""),
        "labels_url": m.get("labels_url", ""),
        "id": m["id"],
        "node_id": m["node_id"],
        "number": m["number"],
        "title": m["title"],
        "description": m.get("description", ""),
        "creator": m.get("creator"),
        "open_issues": m.get("open_issues", 0),
        "closed_issues": m.get("closed_issues", 0),
        "state": m.get("state", "open"),
        "created_at": m.get("created_at", ""),
        "updated_at": m.get("updated_at", ""),
        "due_on": m.get("due_on"),
        "closed_at": m.get("closed_at"),
    }


def issue_shape(forge: model.ForgeState, repo: model.Repository,
                issue: model.Issue) -> dict:
    if issue._shape is not None and issue._shape_gen == issue.generation:
        return issue._shape
    base = f"{API}/repos/{repo.owner}/{repo.name}/issues/{issue.number}"
    shape = {
        "id": issue.iid,
        "node_id": issue.node,
        "url": base,
        "repository_url": f"{API}/repos/{repo.owner}/{repo.name}",
        "labels_url": base + "/labels{/name}",
        "comments_url": base + "/comments",
        "events_url": base + "/events",
        "html_url": f"{WEB}/{repo.owner}/{repo.name}/issues/{issue.number}",
        "number": issue.number,
        "title": issue.title,
        "body": issue.body,
        "user": _user(forge, issue.author),
        "labels": issue.label_view(),
        "state": issue.state,
        "locked": issue.locked,
        "assignee": (_user(forge, issue.assignee)
                     if issue.assignee else None),
        "assignees": ([_user(forge, issue.assignee)]
                      if issue.assignee else []),
        "milestone": _milestone_view(issue.milestone),
        "comments": len(issue.comments),
        "created_at": issue.created_at,
        "updated_at": issue.updated_at,
        "closed_at": issue.closed_at,
        "author_association": "OWNER",
        "active_lock_reason": None,
        "body_html": issue.body,
        "body_text": issue.body,
        "timeline_url": base + "/timeline",
        "performed_via_github_app": None,
        "state_reason": ("completed" if issue.state == "closed" else None),
        "draft": False,
        "reactions": issue.reactions,
    }
    issue._shape = shape
    issue._shape_gen = issue.generation
    return shape


def _ref_view(forge: model.ForgeState, repo: model.Repository,
              branch: str, sha: str) -> dict:
    return {
        "label": f"{repo.owner}:{branch}",
        "ref": branch,
        "sha": sha,
        "user": _user(forge, repo.owner),
        "repo": repo_shape(forge, repo),
    }


def pull_shape(forge: model.ForgeState, repo: model.Repository,
               pr: model.PullRequest) -> dict:
    if pr._shape is not None and pr._shape_gen == pr.generation:
        return pr._shape
    base = f"{API}/repos/{repo.owner}/{repo.name}/pulls/{pr.number}"
    shape = {
        "id": pr.iid,
        "node_id": pr.node,
        "url": base,
        "html_url": f"{WEB}/{repo.owner}/{repo.name}/pull/{pr.number}",
        "diff_url": base + ".diff",
        "patch_url": base + ".patch",
        "issue_url": f"{API}/repos/{repo.owner}/{repo.name}/issues/{pr.number}",
        "number": pr.number,
        "state": pr.state,
        "locked": False,
        "title": pr.title,
        "user": _user(forge, pr.author),
        "body": pr.body,
        "created_at": pr.created_at,
        "updated_at": pr.updated_at,
        "closed_at": pr.closed_at,
        "merged_at": pr.merged_at,
        "merge_commit_sha": pr.merge_sha,
        "assignee": (_user(forge, pr.assignee) if pr.assignee else None),
        "assignees": ([_user(forge, pr.assignee)] if pr.assignee else []),
        "requested_reviewers": [_user(forge, r) for r in pr.reviewers],
        "requested_teams": [],
        "labels": [{"id": 1, "node_id": "LA_kwDO" + hashlib.sha1(
            l["name"].encode()).hexdigest()[:20],
                    "url": "", "name": l["name"],
                    "color": l.get("color", "ededed"), "default": False,
                    "description": l.get("description", "")}
                   for l in pr.labels],
        "milestone": _milestone_view(pr.milestone),
        "draft": pr.draft,
        "commits_url": base + "/commits",
        "review_comments_url": base + "/comments",
        "review_comment_url": f"{API}/repos/{repo.owner}/{repo.name}/pulls/comments{{/number}}",
        "comments_url": base + "/comments",
        "statuses_url": base + "/statuses",
        "head": _ref_view(forge, repo, pr.head, pr.head_sha),
        "base": _ref_view(forge, repo, pr.base, pr.base_sha),
        "_links": {
            "self": {"href": base},
            "html": {"href": f"{WEB}/{repo.owner}/{repo.name}/pull/{pr.number}"},
            "issue": {"href": f"{API}/repos/{repo.owner}/{repo.name}/issues/{pr.number}"},
            "comments": {"href": base + "/comments"},
            "review_comments": {"href": base + "/comments"},
            "review_comment": {"href": f"{API}/repos/{repo.owner}/{repo.name}/pulls/comments{{/number}}"},
            "commits": {"href": base + "/commits"},
            "statuses": {"href": base + "/statuses"},
        },
        "author_association": "OWNER",
        "auto_merge": None,
        "active_lock_reason": None,
        "merged": pr.merged,
        "mergeable": pr.mergeable,
        "mergeable_state": pr.mergeable_state,
        "merged_by": None,
        "comments": len(pr.comments),
        "review_comments": 0,
        "maintainer_can_modify": False,
        "commits": 1,
        "additions": 0,
        "deletions": 0,
        "changed_files": 0,
    }
    pr._shape = shape
    pr._shape_gen = pr.generation
    return shape


def comment_shape(forge: model.ForgeState, repo: model.Repository,
                  number: int, comment: dict) -> dict:
    base = (f"{API}/repos/{repo.owner}/{repo.name}/issues/{number}"
            f"/comments/{comment['id']}")
    return {
        "id": comment["id"],
        "node_id": comment["node_id"],
        "url": base,
        "html_url": (f"{WEB}/{repo.owner}/{repo.name}/issues/{number}"
                     f"#issuecomment-{comment['id']}"),
        "body": comment["body"],
        "user": _user(forge, comment["author"]),
        "created_at": comment["created_at"],
        "updated_at": comment["updated_at"],
        "author_association": "OWNER",
        "performed_via_github_app": None,
        "reactions": {"url": base + "/reactions", "total_count": 0,
                      "+1": 0, "-1": 0, "laugh": 0, "hooray": 0,
                      "confused": 0, "heart": 0, "rocket": 0, "eyes": 0},
    }
