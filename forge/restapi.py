"""REST v3 API, dispatched through a route table.

Each route is ``(method, pattern, handler)``. A handler receives the
request context and the regex match and returns ``(status, payload)``,
where ``payload`` is a dict/list (JSON) or bytes (already encoded).
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from urllib.parse import parse_qs, unquote, urlparse

from . import model, objects as obj
from .shapes import (API, WEB, comment_shape, issue_shape, pull_shape,
                     repo_shape)

NOT_FOUND = {"message": "Not Found",
             "documentation_url": "https://docs.github.com/rest"}
NEEDS_AUTH = {"message": "Requires authentication",
              "documentation_url": "https://docs.github.com/rest"}
DOCS = "https://docs.github.com/rest"

LANG_BY_EXT = {".py": "Python", ".js": "JavaScript", ".ts": "TypeScript",
               ".go": "Go", ".rs": "Rust", ".java": "Java", ".rb": "Ruby",
               ".md": "Markdown", ".json": "JSON", ".html": "HTML",
               ".css": "CSS", ".c": "C", ".cpp": "C++", ".sh": "Shell"}


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode())


class Request:
    """One parsed HTTP request for the REST layer."""

    def __init__(self, method: str, raw_path: str, headers: dict,
                 body: bytes):
        self.method = method
        parsed = urlparse(raw_path)
        self.path = unquote(parsed.path)
        self.query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        self.headers = headers
        self.body = body
        self.json = None
        self.json_error = False
        if body:
            try:
                self.json = json.loads(body.decode())
            except (ValueError, UnicodeDecodeError):
                self.json = None
                self.json_error = True

    def authed(self) -> bool:
        value = self.headers.get("authorization", "")
        return value in ("Bearer test-token", "token test-token")


class RestApi:
    """The REST surface. ``dispatch`` returns ``(status, body, ctype)``."""

    def __init__(self, forge: model.ForgeState):
        self.forge = forge
        self.routes: list[tuple[str, re.Pattern, str]] = []
        self.link_header: str | None = None
        self._register()

    # -- routing ---------------------------------------------------------

    def _on(self, method: str, pattern: str, handler: str) -> None:
        self.routes.append((method, re.compile(pattern), handler))

    def _register(self) -> None:
        on = self._on
        on("GET", r"^/$", "root")
        on("GET", r"^/zen$", "zen")
        on("GET", r"^/rate_limit$", "rate_limit")
        on("GET", r"^/meta$", "meta")
        on("GET", r"^/user$", "current_user")
        on("GET", r"^/user/repos$", "list_user_repos")
        on("GET", r"^/users/(?P<login>[^/]+)$", "get_user")
        on("GET", r"^/search/repositories$", "search_repos")
        on("GET", r"^/search/issues$", "search_issues")
        on("GET", r"^/search/code$", "search_code")
        on("POST", r"^/user/repos$", "create_repo")
        on("GET", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)$", "get_repo")
        on("PATCH", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)$", "patch_repo")
        on("DELETE", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)$", "delete_repo")
        on("GET", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues$", "list_issues")
        on("POST", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues$", "create_issue")
        on("GET", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues/(?P<number>\d+)$", "get_issue")
        on("PATCH", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues/(?P<number>\d+)$", "patch_issue")
        on("GET", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues/(?P<number>\d+)/comments$", "list_comments")
        on("POST", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues/(?P<number>\d+)/comments$", "create_comment")
        on("GET", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues/(?P<number>\d+)/labels$", "list_issue_labels")
        on("POST", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues/(?P<number>\d+)/labels$", "add_issue_labels")
        on("DELETE", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues/(?P<number>\d+)/labels/(?P<name>[^/]+)$", "remove_issue_label")
        on("POST", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues/(?P<number>\d+)/assignees$", "add_assignees")
        on("DELETE", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues/(?P<number>\d+)/assignees$", "remove_assignees")
        on("GET", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/labels$", "list_labels")
        on("POST", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/labels$", "create_label")
        on("GET", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/milestones$", "list_milestones")
        on("POST", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/milestones$", "create_milestone")
        on("GET", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pulls$", "list_pulls")
        on("POST", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pulls$", "create_pull")
        on("GET", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pulls/(?P<number>\d+)$", "get_pull")
        on("PATCH", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pulls/(?P<number>\d+)$", "patch_pull")
        on("PUT", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pulls/(?P<number>\d+)/merge$", "merge_pull")
        on("GET", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pulls/(?P<number>\d+)/commits$", "pull_commits")
        on("GET", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pulls/(?P<number>\d+)/files$", "pull_files")
        on("GET", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pulls/(?P<number>\d+)/reviews$", "list_reviews")
        on("POST", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pulls/(?P<number>\d+)/reviews$", "create_review")
        on("GET", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/git/refs/(?P<ref>.+)$", "get_ref")
        on("POST", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/git/refs$", "create_ref")
        on("PATCH", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/git/refs/(?P<ref>.+)$", "patch_ref")
        on("DELETE", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/git/refs/(?P<ref>.+)$", "delete_ref")
        on("GET", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/git/commits/(?P<sha>[0-9a-f]+)$", "get_git_commit")
        on("POST", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/git/commits$", "create_git_commit")
        on("GET", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/git/trees/(?P<sha>[0-9a-f]+)$", "get_tree")
        on("POST", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/git/trees$", "create_tree")
        on("GET", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/git/blobs/(?P<sha>[0-9a-f]+)$", "get_blob")
        on("POST", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/git/blobs$", "create_blob")
        on("GET", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/git/tags/(?P<sha>[0-9a-f]+)$", "get_tag")
        on("POST", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/git/tags$", "create_tag")
        on("GET", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/contents/(?P<path>.+)$", "get_content")
        on("PUT", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/contents/(?P<path>.+)$", "put_content")
        on("DELETE", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/contents/(?P<path>.+)$", "delete_content")
        on("GET", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/branches$", "list_branches")
        on("GET", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/branches/(?P<branch>[^/]+)$", "get_branch")
        on("GET", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/commits$", "list_commits")
        on("GET", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/commits/(?P<sha>[0-9a-f]+)$", "get_rest_commit")
        on("GET", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/compare/(?P<base>[^.]+)\.\.\.(?P<head>.+)$", "compare")
        on("GET", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/collaborators$", "list_collaborators")
        on("GET", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/contributors$", "list_contributors")
        on("GET", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/languages$", "languages")
        on("GET", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/stargazers$", "list_stargazers")
        on("GET", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/subscribers$", "list_subscribers")
        on("GET", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/tags$", "list_tags")
        on("GET", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/releases$", "list_releases")
        on("POST", r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/releases$", "create_release")

    def dispatch(self, req: Request) -> tuple[int, bytes, str]:
        self.link_header = None
        if req.path not in ("/zen", "/rate_limit") and not req.authed():
            return 401, json.dumps(NEEDS_AUTH).encode(), "application/json"
        if req.json_error and req.method in ("POST", "PUT", "PATCH"):
            return 400, json.dumps({"message": "Problems parsing JSON",
                                    "documentation_url": DOCS}).encode(), \
                "application/json"
        for method, pattern, name in self.routes:
            if method != req.method:
                continue
            match = pattern.match(req.path)
            if match:
                handler = getattr(self, "h_" + name)
                status, payload = handler(req, match)
                if isinstance(payload, bytes):
                    return status, payload, "text/plain"
                return status, json.dumps(payload).encode(), "application/json"
        return 404, json.dumps(NOT_FOUND).encode(), "application/json"

    # -- helpers ----------------------------------------------------------

    def _repo(self, match) -> model.Repository | None:
        return self.forge.find_repo(match.group("owner"),
                                    match.group("repo"))

    def _need_repo(self, match):
        repo = self._repo(match)
        if repo is None:
            return None, (404, NOT_FOUND)
        return repo, None

    def _page(self, req: Request, items: list) -> list:
        try:
            page = max(1, int(req.query.get("page", "1")))
        except ValueError:
            page = 1
        try:
            per = int(req.query.get("per_page", "30"))
        except ValueError:
            per = 30
        per = min(max(per, 1), 100)
        return items[(page - 1) * per:page * per]

    def _page_with_link(self, req: Request, items: list) -> list:
        """Paginate and remember the RFC 5988 ``Link`` header."""
        try:
            page = max(1, int(req.query.get("page", "1")))
        except ValueError:
            page = 1
        try:
            per = int(req.query.get("per_page", "30"))
        except ValueError:
            per = 30
        per = min(max(per, 1), 100)
        total = len(items)
        last_page = max(1, (total + per - 1) // per)
        host = req.headers.get("host", "127.0.0.1")
        base = f"http://{host}/api/v3{req.path}"
        links: list[str] = []
        if page < last_page:
            links.append(f'<{base}?page={page + 1}&per_page={per}>;'
                         ' rel="next"')
            links.append(f'<{base}?page={last_page}&per_page={per}>;'
                         ' rel="last"')
        if page > 1:
            links.append(f'<{base}?page=1&per_page={per}>; rel="first"')
            links.append(f'<{base}?page={page - 1}&per_page={per}>;'
                         ' rel="prev"')
        self.link_header = ", ".join(links) if links else None
        return items[(page - 1) * per:page * per]

    def _actor(self, req: Request) -> str:
        return "adaiversen"

    # -- meta -------------------------------------------------------------

    def h_root(self, req, match):
        return 200, {"current_user_url": API + "/user",
                     "current_user_authorizations_html_url":
                         WEB + "/settings/applications",
                     "authorizations_url": API + "/authorizations",
                     "code_search_url": API + "/search/code?q={query}",
                     "commit_search_url": API + "/search/commits?q={query}",
                     "emails_url": API + "/user/emails",
                     "emojis_url": API + "/emojis",
                     "events_url": API + "/events",
                     "feeds_url": API + "/feeds",
                     "followers_url": API + "/user/followers",
                     "following_url": API + "/user/following{/other_user}",
                     "gists_url": API + "/gists{/gist_id}",
                     "hub_url": API + "/hub",
                     "issue_search_url": API + "/search/issues?q={query}",
                     "issues_url": API + "/issues",
                     "keys_url": API + "/user/keys",
                     "labels_url": API + "/labels{/id}",
                     "marketplace_purchases_url": API + "/marketplace_listing/stubbed",
                     "marketplace_listing_url": API + "/marketplace_listing",
                     "notifications_url": API + "/notifications",
                     "organization_repositories_url": API + "/orgs/{org}/repos{?type,page,per_page,sort}",
                     "organization_url": API + "/orgs/{org}",
                     "public_gists_url": API + "/gists/public",
                     "rate_limit_url": API + "/rate_limit",
                     "repository_url": API + "/repos/{owner}/{repo}",
                     "repository_search_url": API + "/search/repositories?q={query}",
                     "current_user_repositories_url": API + "/user/repos{?type,page,per_page,sort}",
                     "starred_url": API + "/user/starred{/owner}{/repo}",
                     "starred_gists_url": API + "/user/starred{/owner}{/repo}",
                     "topic_search_url": API + "/search/topics?q={query}",
                     "user_url": API + "/users/{user}",
                     "user_organizations_url": API + "/user/orgs",
                     "user_repositories_url": API + "/users/{user}/repos{?type,page,per_page,sort}",
                     "user_search_url": API + "/search/users?q={query}"}

    def h_zen(self, req, match):
        return 200, b"Keep it logically awesome."

    def h_rate_limit(self, req, match):
        reset = obj.FIXED_TS + 3600
        core = {"limit": 5000, "used": 0, "remaining": 5000, "reset": reset}
        search = {"limit": 30, "used": 0, "remaining": 30, "reset": reset}
        return 200, {"resources": {"core": core, "search": search,
                                   "graphql": {"limit": 5000, "used": 0,
                                               "remaining": 5000,
                                               "reset": reset},
                                   "integration_manifest": {"limit": 5000,
                                                            "used": 0,
                                                            "remaining": 5000,
                                                            "reset": reset}},
                    "rate": core}

    def h_meta(self, req, match):
        return 200, {"verifiable_password_authentication": True,
                     "installed_version": "3.13.0",
                     "ssh_key_fingerprints": {},
                     "ssh_keys": [],
                     "hooks": ["127.0.0.1/32"],
                     "web": ["127.0.0.1/32"],
                     "api": ["127.0.0.1/32"],
                     "git": ["127.0.0.1/32"],
                     "packages": ["127.0.0.1/32"],
                     "pages": ["127.0.0.1/32"],
                     "importer": ["127.0.0.1/32"],
                     "actions": ["127.0.0.1/32"],
                     "dependabot": ["127.0.0.1/32"]}

    def h_list_user_repos(self, req, match):
        actor = self._actor(req)
        items = [repo_shape(self.forge, repo)
                 for repo in self.forge.repositories.values()
                 if repo.owner == actor]
        items.sort(key=lambda s: s["name"])
        return 200, self._page_with_link(req, items)

    def h_current_user(self, req, match):
        return 200, self.forge.account(self._actor(req)).public()

    def h_get_user(self, req, match):
        return 200, self.forge.account(match.group("login")).public()

    # -- search -----------------------------------------------------------

    def h_search_repos(self, req, match):
        query = req.query.get("q", "").lower()
        items = []
        for repo in self.forge.repositories.values():
            hay = f"{repo.owner}/{repo.name} {repo.description}".lower()
            if not query or query in hay or repo.name.lower() in query:
                items.append(repo_shape(self.forge, repo))
        return 200, {"total_count": len(items),
                     "incomplete_results": False,
                     "items": self._page(req, items)}

    def h_search_issues(self, req, match):
        query = req.query.get("q", "").lower()
        items = []
        for repo in self.forge.repositories.values():
            for issue in repo.issues.values():
                hay = f"{issue.title} {issue.body}".lower()
                if not query or query in hay:
                    items.append(issue_shape(self.forge, repo, issue))
            for pr in repo.pulls.values():
                hay = f"{pr.title} {pr.body}".lower()
                if not query or query in hay:
                    items.append(pull_shape(self.forge, repo, pr))
        return 200, {"total_count": len(items),
                     "incomplete_results": False,
                     "items": self._page(req, items)}

    def h_search_code(self, req, match):
        return 200, {"total_count": 0, "incomplete_results": False,
                     "items": []}

    # -- repositories -----------------------------------------------------

    def h_create_repo(self, req, match):
        body = req.json or {}
        name = body.get("name", "")
        if not name:
            return 422, {"message": "Validation Failed", "errors": [
                {"resource": "Repository", "code": "missing_field",
                 "field": "name"}]}
        try:
            repo = self.forge.create_repo(
                self._actor(req), name,
                description=body.get("description", ""),
                private=bool(body.get("private", False)))
        except ValueError:
            return 422, {"message": "Validation Failed", "errors": [
                {"resource": "Repository", "code": "custom",
                 "field": "name", "message": "name already exists"}]}
        return 201, repo_shape(self.forge, repo)

    def h_get_repo(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        return 200, repo_shape(self.forge, repo)

    def h_patch_repo(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        body = req.json or {}
        if "description" in body:
            repo.description = body["description"]
        if "private" in body:
            repo.private = bool(body["private"])
        if "default_branch" in body:
            repo.default_branch = body["default_branch"]
        return 200, repo_shape(self.forge, repo)

    def h_delete_repo(self, req, match):
        if not self.forge.delete_repo(match.group("owner"),
                                      match.group("repo")):
            return 404, NOT_FOUND
        return 204, {}

    # -- issues -----------------------------------------------------------

    def h_list_issues(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        state = req.query.get("state", "open")
        items = []
        for issue in repo.issues.values():
            if state != "all" and issue.state != state:
                continue
            items.append(issue_shape(self.forge, repo, issue))
        for pr in repo.pulls.values():
            if state != "all" and pr.state != state:
                continue
            shape = pull_shape(self.forge, repo, pr)
            shape["pull_request"] = {
                "url": f"{API}/repos/{repo.owner}/{repo.name}/pulls/{pr.number}",
                "html_url": f"{WEB}/{repo.owner}/{repo.name}/pull/{pr.number}",
                "diff_url": f"{API}/repos/{repo.owner}/{repo.name}/pulls/{pr.number}.diff",
                "patch_url": f"{API}/repos/{repo.owner}/{repo.name}/pulls/{pr.number}.patch"}
            items.append(shape)
        # GitHub sorts by creation time, newest first by default.
        direction = req.query.get("direction", "desc")
        items.sort(key=lambda s: s["number"],
                   reverse=(direction != "asc"))
        return 200, self._page(req, items)

    def h_create_issue(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        body = req.json or {}
        title = body.get("title", "")
        if not title:
            return 422, {"message": "Validation Failed", "errors": [
                {"resource": "Issue", "code": "missing_field",
                 "field": "title"}]}
        labels = body.get("labels", [])
        label_names = [l if isinstance(l, str) else l.get("name", "")
                       for l in labels]
        known = {l["name"] for l in repo.labels}
        unknown = [n for n in label_names if n and n not in known]
        if unknown:
            return 422, {"message": "Validation Failed", "errors": [
                {"resource": "Issue", "code": "invalid",
                 "field": "labels",
                 "message": f"unknown label: {unknown[0]}"}]}
        issue = repo.open_issue(title, body.get("body", ""),
                                self._actor(req), label_names)
        if body.get("assignees"):
            issue.assignee = body["assignees"][0]
        if body.get("milestone") is not None:
            for m in repo.milestones:
                if m["number"] == body["milestone"]:
                    issue.milestone = m
        issue.touch()
        return 201, issue_shape(self.forge, repo, issue)

    def _find_issue(self, repo, number: int):
        return repo.issues.get(number)

    def h_get_issue(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        issue = self._find_issue(repo, int(match.group("number")))
        if issue is None:
            return 404, NOT_FOUND
        return 200, issue_shape(self.forge, repo, issue)

    def h_patch_issue(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        issue = self._find_issue(repo, int(match.group("number")))
        if issue is None:
            return 404, NOT_FOUND
        body = req.json or {}
        if "title" in body:
            issue.title = body["title"]
        if "body" in body:
            issue.body = body["body"]
        if "state" in body:
            issue.state = body["state"]
            issue.closed_at = (obj.FIXED_DATE + "T00:00:00Z"
                               if body["state"] == "closed" else None)
        if "labels" in body:
            issue.labels = [{"name": l if isinstance(l, str)
                             else l.get("name", ""),
                             "color": "ededed", "description": ""}
                            for l in body["labels"]]
        if "assignees" in body:
            issue.assignee = (body["assignees"][0]
                              if body["assignees"] else None)
        issue.touch()
        return 200, issue_shape(self.forge, repo, issue)

    def h_list_comments(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        number = int(match.group("number"))
        target = repo.issues.get(number) or repo.pulls.get(number)
        if target is None:
            return 404, NOT_FOUND
        return 200, self._page(req, [comment_shape(self.forge, repo, number, c)
                                     for c in target.comments])

    def h_create_comment(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        number = int(match.group("number"))
        target = repo.issues.get(number) or repo.pulls.get(number)
        if target is None:
            return 404, NOT_FOUND
        body = req.json or {}
        comment = {"id": self.forge.next_id(),
                   "node_id": "IC_kwDO" + hashlib.sha1(
                       f"c:{self.forge._seq}".encode()).hexdigest()[:20],
                   "body": body.get("body", ""),
                   "author": self._actor(req),
                   "created_at": obj.FIXED_DATE + "T00:00:00Z",
                   "updated_at": obj.FIXED_DATE + "T00:00:00Z"}
        target.comments.append(comment)
        target.touch()
        return 201, comment_shape(self.forge, repo, number, comment)

    def h_list_issue_labels(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        issue = self._find_issue(repo, int(match.group("number")))
        if issue is None:
            return 404, NOT_FOUND
        return 200, issue.label_view()

    def h_add_issue_labels(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        issue = self._find_issue(repo, int(match.group("number")))
        if issue is None:
            return 404, NOT_FOUND
        body = req.json or {}
        names = body.get("labels", []) if isinstance(body, dict) else []
        known = {l["name"] for l in repo.labels}
        unknown = [n if isinstance(n, str) else n.get("name", "")
                   for n in names]
        unknown = [n for n in unknown if n and n not in known]
        if unknown:
            return 422, {"message": "Validation Failed", "errors": [
                {"resource": "Issue", "code": "invalid",
                 "field": "labels",
                 "message": f"unknown label: {unknown[0]}"}]}
        for name in names:
            name = name if isinstance(name, str) else name.get("name", "")
            if name and name not in [l["name"] for l in issue.labels]:
                issue.labels.append({"name": name, "color": "ededed",
                                     "description": ""})
        issue.touch()
        return 200, issue.label_view()

    def h_remove_issue_label(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        issue = self._find_issue(repo, int(match.group("number")))
        if issue is None:
            return 404, NOT_FOUND
        name = match.group("name")
        issue.labels = [l for l in issue.labels if l["name"] != name]
        issue.touch()
        return 200, issue.label_view()

    def h_add_assignees(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        issue = self._find_issue(repo, int(match.group("number")))
        if issue is None:
            return 404, NOT_FOUND
        body = req.json or {}
        assignees = body.get("assignees", [])
        if assignees:
            issue.assignee = assignees[0]
        issue.touch()
        return 201, {"assignees": [self.forge.account(a).public()
                                   for a in assignees]}

    def h_remove_assignees(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        issue = self._find_issue(repo, int(match.group("number")))
        if issue is None:
            return 404, NOT_FOUND
        issue.assignee = None
        issue.touch()
        return 200, {"assignees": []}

    # -- labels / milestones ----------------------------------------------

    def h_list_labels(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        out = []
        for index, label in enumerate(repo.labels):
            out.append({"id": index + 1,
                        "node_id": "LA_kwDO" + hashlib.sha1(
                            label["name"].encode()).hexdigest()[:20],
                        "url": "", "name": label["name"],
                        "color": label["color"], "default": False,
                        "description": label.get("description", "")})
        return 200, out

    def h_create_label(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        body = req.json or {}
        label = {"name": body.get("name", ""),
                 "color": body.get("color", "ededed"),
                 "description": body.get("description", "")}
        repo.labels.append(label)
        return 201, {"id": len(repo.labels),
                     "node_id": "LA_kwDO" + hashlib.sha1(
                         label["name"].encode()).hexdigest()[:20],
                     "url": "", "name": label["name"],
                     "color": label["color"], "default": False,
                     "description": label["description"]}

    def h_list_milestones(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        return 200, [_milestone_view(m) for m in repo.milestones]

    def h_create_milestone(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        body = req.json or {}
        number = repo.next_milestone
        repo.next_milestone += 1
        milestone = {
            "id": self.forge.next_id(),
            "node_id": "MI_kwDO" + hashlib.sha1(
                f"m:{number}".encode()).hexdigest()[:20],
            "number": number,
            "title": body.get("title", ""),
            "description": body.get("description", ""),
            "creator": self.forge.account(self._actor(req)).public(),
            "open_issues": 0, "closed_issues": 0, "state": "open",
            "created_at": obj.FIXED_DATE + "T00:00:00Z",
            "updated_at": obj.FIXED_DATE + "T00:00:00Z",
            "due_on": body.get("due_on"),
            "closed_at": None,
            "url": f"{API}/repos/{repo.owner}/{repo.name}/milestones/{number}",
            "html_url": f"{WEB}/{repo.owner}/{repo.name}/milestone/{number}",
            "labels_url": f"{API}/repos/{repo.owner}/{repo.name}/milestones/{number}/labels",
        }
        repo.milestones.append(milestone)
        return 201, _milestone_view(milestone)

    # -- pulls --------------------------------------------------------------

    def h_list_pulls(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        state = req.query.get("state", "open")
        items = []
        for pr in repo.pulls.values():
            if state != "all" and pr.state != state:
                continue
            items.append(pull_shape(self.forge, repo, pr))
        items.sort(key=lambda s: s["number"])
        return 200, self._page(req, items)

    def h_create_pull(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        body = req.json or {}
        head = body.get("head", "")
        base = body.get("base", repo.default_branch)
        if not head or not body.get("title"):
            return 422, {"message": "Validation Failed", "errors": [
                {"resource": "PullRequest", "code": "missing_field",
                 "field": "head" if not head else "title"}]}
        if repo.refs.get("refs/heads/" + head) is None:
            return 422, {"message": "Validation Failed", "errors": [
                {"resource": "PullRequest", "code": "invalid",
                 "field": "head", "message": "head branch not found"}]}
        pr = repo.open_pull(body["title"], body.get("body", ""), head, base,
                            self._actor(req),
                            draft=bool(body.get("draft", False)))
        return 201, pull_shape(self.forge, repo, pr)

    def h_get_pull(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        pr = repo.pulls.get(int(match.group("number")))
        if pr is None:
            return 404, NOT_FOUND
        return 200, pull_shape(self.forge, repo, pr)

    def h_patch_pull(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        pr = repo.pulls.get(int(match.group("number")))
        if pr is None:
            return 404, NOT_FOUND
        body = req.json or {}
        if "title" in body:
            pr.title = body["title"]
        if "body" in body:
            pr.body = body["body"]
        if "state" in body:
            pr.state = body["state"]
            pr.closed_at = (obj.FIXED_DATE + "T00:00:00Z"
                            if body["state"] == "closed" else None)
        if "base" in body:
            pr.base = body["base"]
            pr.base_sha = repo.refs.get("refs/heads/" + body["base"], "")
        pr.touch()
        return 200, pull_shape(self.forge, repo, pr)

    def h_merge_pull(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        number = int(match.group("number"))
        pr = repo.pulls.get(number)
        if pr is None:
            return 404, NOT_FOUND
        if pr.state != "open":
            return 405, {"message": "Pull request is not open"}
        body = req.json or {}
        method = body.get("merge_method", "merge")
        if repo.merge_conflict(pr):
            return 405, {"message": "Merge conflict",
                         "documentation_url": DOCS}
        head_oid = repo.refs.get("refs/heads/" + pr.head)
        base_oid = repo.refs.get("refs/heads/" + pr.base)
        if head_oid is None or base_oid is None:
            return 422, {"message": "Validation Failed", "errors": [
                {"resource": "PullRequest", "code": "invalid",
                 "field": "head"}]}
        head_blob = repo.objects.get(head_oid)
        head_parsed = obj.decode_commit(obj.load_raw("commit", head_blob))
        parents = [base_oid, head_oid] if method == "merge" else [base_oid]
        message = (body.get("commit_title")
                   or f"{pr.title} (#{pr.number})")
        if body.get("commit_message"):
            message += "\n\n" + body["commit_message"]
        merged = obj.encode_commit(head_parsed["tree"], parents, message)
        merged_oid = obj.object_id("commit", merged)
        repo.objects[merged_oid] = obj.raw_bytes("commit", merged)
        repo.refs["refs/heads/" + pr.base] = merged_oid
        pr.state = "closed"
        pr.merged = True
        pr.merge_sha = merged_oid
        pr.merged_at = obj.FIXED_DATE + "T00:00:00Z"
        pr.closed_at = obj.FIXED_DATE + "T00:00:00Z"
        pr.touch()
        return 200, {"sha": merged_oid, "merged": True,
                     "message": "Pull request successfully merged"}

    def h_pull_commits(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        pr = repo.pulls.get(int(match.group("number")))
        if pr is None:
            return 404, NOT_FOUND
        commits = []
        for oid in self._commit_chain(repo, pr.head_sha, pr.base_sha):
            commits.append(self._rest_commit(repo, oid))
        return 200, self._page(req, commits)

    def _commit_chain(self, repo, tip: str, stop: str) -> list[str]:
        """Commits reachable from ``tip`` but not from ``stop``."""
        from_tip = set()
        stack = [tip]
        while stack:
            oid = stack.pop()
            if not oid or oid in from_tip or oid not in repo.objects:
                continue
            from_tip.add(oid)
            blob = repo.objects[oid]
            if blob.split(b" ", 1)[0] != b"commit":
                continue
            stack.extend(obj.decode_commit(obj.load_raw("commit", blob))["parents"])
        from_base = set()
        stack = [stop]
        while stack:
            oid = stack.pop()
            if not oid or oid in from_base or oid not in repo.objects:
                continue
            from_base.add(oid)
            blob = repo.objects[oid]
            if blob.split(b" ", 1)[0] != b"commit":
                continue
            stack.extend(obj.decode_commit(obj.load_raw("commit", blob))["parents"])
        chain = [oid for oid in from_tip - from_base]
        # Order: newest first by walking from the tip.
        ordered = []
        seen = set()
        stack = [tip]
        while stack:
            oid = stack.pop(0)
            if oid in seen or oid not in chain:
                continue
            seen.add(oid)
            ordered.append(oid)
            blob = repo.objects[oid]
            stack.extend(obj.decode_commit(obj.load_raw("commit", blob))["parents"])
        return ordered

    def h_pull_files(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        pr = repo.pulls.get(int(match.group("number")))
        if pr is None:
            return 404, NOT_FOUND
        files = []
        base_oid = pr.base_sha
        head_oid = pr.head_sha
        base_tree = self._tree_of(repo, base_oid)
        head_tree = self._tree_of(repo, head_oid)
        for path in sorted(set(base_tree) | set(head_tree)):
            if base_tree.get(path) == head_tree.get(path):
                continue
            old_data = self._blob_bytes(repo, base_tree.get(path))
            new_data = self._blob_bytes(repo, head_tree.get(path))
            added, removed = _line_diff(old_data, new_data)
            files.append({"sha": head_tree.get(path, ""),
                          "filename": path,
                          "status": ("added" if path not in base_tree
                                     else "removed" if path not in head_tree
                                     else "modified"),
                          "additions": added, "deletions": removed,
                          "changes": added + removed,
                          "blob_url": "", "raw_url": "", "contents_url": ""})
        return 200, self._page(req, files)

    @staticmethod
    def _blob_bytes(repo, oid: str | None) -> bytes:
        if not oid:
            return b""
        blob = repo.objects.get(oid)
        if blob is None:
            return b""
        return obj.load_raw("blob", blob)

    def _tree_of(self, repo, commit_oid: str) -> dict[str, str]:
        """Flat ``{path: blob_oid}`` for a commit."""
        out: dict[str, str] = {}
        if not commit_oid or commit_oid not in repo.objects:
            return out
        parsed = obj.decode_commit(
            obj.load_raw("commit", repo.objects[commit_oid]))
        self._flatten_tree(repo, parsed["tree"], "", out)
        return out

    def _flatten_tree(self, repo, tree_oid: str, prefix: str,
                      out: dict[str, str]) -> None:
        blob = repo.objects.get(tree_oid)
        if blob is None:
            return
        for mode, name, oid in obj.decode_tree(obj.load_raw("tree", blob)):
            path = prefix + name
            if mode == "40000":
                self._flatten_tree(repo, oid, path + "/", out)
            else:
                out[path] = oid

    def h_list_reviews(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        if int(match.group("number")) not in repo.pulls:
            return 404, NOT_FOUND
        return 200, []

    def h_create_review(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        pr = repo.pulls.get(int(match.group("number")))
        if pr is None:
            return 404, NOT_FOUND
        body = req.json or {}
        review = {"id": self.forge.next_id(),
                  "node_id": "PRR_kwDO" + hashlib.sha1(
                      f"r:{self.forge._seq}".encode()).hexdigest()[:20],
                  "user": self.forge.account(self._actor(req)).public(),
                  "body": body.get("body", ""),
                  "state": body.get("event", "COMMENTED"),
                  "commit_id": pr.head_sha,
                  "submitted_at": obj.FIXED_DATE + "T00:00:00Z",
                  "html_url": "",
                  "pull_request_url": "",
                  "author_association": "OWNER",
                  "_links": {"html": {"href": ""},
                             "pull_request": {"href": ""}}}
        return 201, review

    # -- git refs ---------------------------------------------------------

    def h_get_ref(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        ref = "refs/" + match.group("ref")
        oid = repo.refs.get(ref)
        if oid is None:
            return 404, NOT_FOUND
        return 200, self._ref_shape(repo, ref, oid)

    def _ref_shape(self, repo, ref: str, oid: str) -> dict:
        kind = "tag" if ref.startswith("refs/tags/") else "commit"
        blob = repo.objects.get(oid)
        if blob is not None and blob.split(b" ", 1)[0] == b"tag":
            kind = "tag"
        return {"ref": ref,
                "node_id": "REF_kwDO" + hashlib.sha1(
                    ref.encode()).hexdigest()[:20],
                "url": f"{API}/repos/{repo.owner}/{repo.name}/git/refs/{ref[5:]}",
                "object": {"sha": oid, "type": kind,
                           "url": f"{API}/repos/{repo.owner}/{repo.name}/git/{kind}s/{oid}"}}

    def h_create_ref(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        body = req.json or {}
        ref = body.get("ref", "")
        sha = body.get("sha", "")
        if not ref or not sha:
            return 422, {"message": "Validation Failed", "errors": [
                {"resource": "Reference", "code": "missing_field",
                 "field": "ref" if not ref else "sha"}]}
        if ref in repo.refs:
            return 422, {"message": "Validation Failed", "errors": [
                {"resource": "Reference", "code": "custom",
                 "field": "ref", "message": "Reference already exists"}]}
        if sha not in repo.objects:
            return 422, {"message": "Validation Failed", "errors": [
                {"resource": "Reference", "code": "invalid",
                 "field": "sha", "message": "Object does not exist"}]}
        repo.refs[ref] = sha
        return 201, self._ref_shape(repo, ref, sha)

    def h_patch_ref(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        ref = "refs/" + match.group("ref")
        if ref not in repo.refs:
            return 404, NOT_FOUND
        body = req.json or {}
        sha = body.get("sha", "")
        if sha not in repo.objects:
            return 422, {"message": "Validation Failed", "errors": [
                {"resource": "Reference", "code": "invalid",
                 "field": "sha"}]}
        old = repo.refs[ref]
        if not body.get("force") and not obj.is_ancestor(
                repo.objects, old, sha):
            return 422, {"message": "Validation Failed", "errors": [
                {"resource": "Reference", "code": "custom",
                 "field": "sha", "message": "Update is not a fast forward"}]}
        repo.update_ref(ref, sha)
        return 200, self._ref_shape(repo, ref, sha)

    def h_delete_ref(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        ref = "refs/" + match.group("ref")
        if ref not in repo.refs:
            return 404, NOT_FOUND
        repo.delete_ref(ref)
        return 204, {}

    # -- git objects ------------------------------------------------------

    def _commit_shape(self, repo, oid: str) -> dict:
        parsed = obj.decode_commit(
            obj.load_raw("commit", repo.objects[oid]))
        return {
            "sha": oid,
            "node_id": "C_kwDO" + hashlib.sha1(oid.encode()).hexdigest()[:20],
            "url": f"{API}/repos/{repo.owner}/{repo.name}/git/commits/{oid}",
            "html_url": f"{WEB}/{repo.owner}/{repo.name}/commit/{oid}",
            "author": {"name": "A U Thor", "email": "author@example.com",
                       "date": _iso_from_ident(parsed.get("author", ""))},
            "committer": {"name": "C O Mitter",
                          "email": "committer@example.com",
                          "date": _iso_from_ident(parsed.get("committer", ""))},
            "tree": {"sha": parsed["tree"],
                     "url": f"{API}/repos/{repo.owner}/{repo.name}/git/trees/{parsed['tree']}"},
            "message": parsed["message"],
            "parents": [{"sha": p,
                         "url": f"{API}/repos/{repo.owner}/{repo.name}/git/commits/{p}",
                         "html_url": f"{WEB}/{repo.owner}/{repo.name}/commit/{p}"}
                        for p in parsed["parents"]],
            "verification": {"verified": False, "reason": "unsigned",
                             "signature": None, "payload": None,
                             "verified_at": None},
        }

    def h_get_git_commit(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        oid = match.group("sha")
        blob = repo.objects.get(oid)
        if blob is None or blob.split(b" ", 1)[0] != b"commit":
            return 404, NOT_FOUND
        return 200, self._commit_shape(repo, oid)

    def h_create_git_commit(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        body = req.json or {}
        tree = body.get("tree", "")
        if tree not in repo.objects:
            return 422, {"message": "Validation Failed", "errors": [
                {"resource": "Commit", "code": "invalid",
                 "field": "tree"}]}
        parents = body.get("parents", [])
        payload = obj.encode_commit(tree, parents,
                                    body.get("message", ""))
        oid = repo.store_object("commit", payload)
        return 201, self._commit_shape(repo, oid)

    def _tree_shape(self, repo, oid: str, recursive: bool) -> dict:
        entries = []
        self._collect_tree(repo, oid, "", recursive, entries)
        return {"sha": oid,
                "url": f"{API}/repos/{repo.owner}/{repo.name}/git/trees/{oid}",
                "tree": entries, "truncated": False}

    def _collect_tree(self, repo, tree_oid: str, prefix: str,
                      recursive: bool, out: list) -> None:
        blob = repo.objects.get(tree_oid)
        if blob is None:
            return
        for mode, name, child in obj.decode_tree(
                obj.load_raw("tree", blob)):
            kind = "tree" if mode == "40000" else "blob"
            size = None
            if kind == "blob":
                child_blob = repo.objects.get(child)
                if child_blob is not None:
                    size = len(obj.load_raw("blob", child_blob))
            out.append({"path": prefix + name, "mode": mode, "type": kind,
                        "sha": child, "size": size,
                        "url": f"{API}/repos/{repo.owner}/{repo.name}/git/{kind}s/{child}"})
            if recursive and kind == "tree":
                self._collect_tree(repo, child, prefix + name + "/",
                                   recursive, out)

    def h_get_tree(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        oid = match.group("sha")
        blob = repo.objects.get(oid)
        if blob is None or blob.split(b" ", 1)[0] != b"tree":
            return 404, NOT_FOUND
        recursive = req.query.get("recursive") == "1"
        return 200, self._tree_shape(repo, oid, recursive)

    def h_create_tree(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        body = req.json or {}
        base_tree = body.get("base_tree")
        entries: dict[str, tuple[str, str]] = {}
        if base_tree and base_tree in repo.objects:
            for mode, name, child in obj.decode_tree(
                    obj.load_raw("tree", repo.objects[base_tree])):
                entries[name] = (mode, child)
        for item in body.get("tree", []):
            path = item.get("path", "")
            if "sha" in item and item["sha"]:
                entries[path] = (item.get("mode", "100644"), item["sha"])
            elif "content" in item:
                data = item["content"].encode()
                blob_oid = repo.store_object("blob", data)
                entries[path] = (item.get("mode", "100644"), blob_oid)
        flat = sorted(entries.items())
        payload = obj.encode_tree(
            [(mode, name, child) for name, (mode, child) in flat])
        oid = repo.store_object("tree", payload)
        return 201, self._tree_shape(repo, oid, False)

    def h_get_blob(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        oid = match.group("sha")
        blob = repo.objects.get(oid)
        if blob is None or blob.split(b" ", 1)[0] != b"blob":
            return 404, NOT_FOUND
        data = obj.load_raw("blob", blob)
        return 200, {"sha": oid,
                     "node_id": "B_kwDO" + hashlib.sha1(
                         oid.encode()).hexdigest()[:20],
                     "size": len(data),
                     "url": f"{API}/repos/{repo.owner}/{repo.name}/git/blobs/{oid}",
                     "content": _b64(data), "encoding": "base64"}

    def h_create_blob(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        body = req.json or {}
        content = body.get("content", "")
        encoding = body.get("encoding", "utf-8")
        data = _unb64(content) if encoding == "base64" else content.encode()
        oid = repo.store_object("blob", data)
        return 201, {"sha": oid,
                     "url": f"{API}/repos/{repo.owner}/{repo.name}/git/blobs/{oid}"}

    def h_get_tag(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        oid = match.group("sha")
        blob = repo.objects.get(oid)
        if blob is None or blob.split(b" ", 1)[0] != b"tag":
            return 404, NOT_FOUND
        parsed = obj.decode_tag(obj.load_raw("tag", blob))
        return 200, {"sha": oid,
                     "node_id": "T_kwDO" + hashlib.sha1(
                         oid.encode()).hexdigest()[:20],
                     "url": f"{API}/repos/{repo.owner}/{repo.name}/git/tags/{oid}",
                     "tag": parsed["tag"], "message": parsed["message"],
                     "object": {"sha": parsed["object"],
                                "type": parsed["type"],
                                "url": f"{API}/repos/{repo.owner}/{repo.name}/git/{parsed['type']}s/{parsed['object']}"},
                     "tagger": {"name": "A U Thor",
                                "email": "author@example.com",
                                "date": obj.FIXED_DATE + "T00:00:00Z"}}

    def h_create_tag(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        body = req.json or {}
        target = body.get("object", "")
        if target not in repo.objects:
            return 422, {"message": "Validation Failed", "errors": [
                {"resource": "Tag", "code": "invalid", "field": "object"}]}
        payload = obj.encode_tag(target, body.get("type", "commit"),
                                 body.get("tag", ""),
                                 body.get("message", ""))
        oid = repo.store_object("tag", payload)
        return 201, {"sha": oid,
                     "node_id": "T_kwDO" + hashlib.sha1(
                         oid.encode()).hexdigest()[:20],
                     "url": f"{API}/repos/{repo.owner}/{repo.name}/git/tags/{oid}",
                     "tag": body.get("tag", ""),
                     "message": body.get("message", ""),
                     "object": {"sha": target,
                                "type": body.get("type", "commit"),
                                "url": ""},
                     "tagger": {"name": "A U Thor",
                                "email": "author@example.com",
                                "date": obj.FIXED_DATE + "T00:00:00Z"}}

    # -- contents ---------------------------------------------------------

    def _branch_tip(self, repo, branch: str | None) -> str:
        name = branch or repo.default_branch
        return repo.refs.get("refs/heads/" + name, "")

    def h_get_content(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        path = match.group("path")
        tip = self._branch_tip(repo, req.query.get("ref"))
        flat = self._tree_of(repo, tip)
        if path in flat:
            blob = repo.objects[flat[path]]
            data = obj.load_raw("blob", blob)
            return 200, {"type": "file", "encoding": "base64",
                         "size": len(data), "name": path.rsplit("/", 1)[-1],
                         "path": path, "content": _b64(data),
                         "sha": flat[path],
                         "url": f"{API}/repos/{repo.owner}/{repo.name}/contents/{path}",
                         "html_url": f"{WEB}/{repo.owner}/{repo.name}/blob/{repo.default_branch}/{path}",
                         "git_url": f"{API}/repos/{repo.owner}/{repo.name}/git/blobs/{flat[path]}",
                         "download_url": "",
                         "_links": {"git": f"{API}/repos/{repo.owner}/{repo.name}/git/blobs/{flat[path]}",
                                    "self": f"{API}/repos/{repo.owner}/{repo.name}/contents/{path}",
                                    "html": f"{WEB}/{repo.owner}/{repo.name}/blob/{repo.default_branch}/{path}"}}
        prefix = path.rstrip("/") + "/"
        children = sorted(p for p in flat if p.startswith(prefix)
                          and "/" not in p[len(prefix):])
        if not children and path != "":
            subdirs = sorted({p[len(prefix):].split("/")[0]
                              for p in flat if p.startswith(prefix)})
            if not subdirs:
                return 404, NOT_FOUND
            entries = [{"type": "dir", "name": d, "path": prefix + d,
                        "sha": "", "size": 0,
                        "url": f"{API}/repos/{repo.owner}/{repo.name}/contents/{prefix}{d}",
                        "html_url": f"{WEB}/{repo.owner}/{repo.name}/tree/{repo.default_branch}/{prefix}{d}",
                        "git_url": "", "download_url": None,
                        "_links": {"self": f"{API}/repos/{repo.owner}/{repo.name}/contents/{prefix}{d}",
                                   "git": "", "html": f"{WEB}/{repo.owner}/{repo.name}/tree/{repo.default_branch}/{prefix}{d}"}}
                       for d in subdirs]
            return 200, entries
        entries = []
        for child in children:
            blob = repo.objects[flat[child]]
            data = obj.load_raw("blob", blob)
            entries.append({"type": "file", "name": child.rsplit("/", 1)[-1],
                            "path": child, "sha": flat[child],
                            "size": len(data),
                            "url": f"{API}/repos/{repo.owner}/{repo.name}/contents/{child}",
                            "html_url": f"{WEB}/{repo.owner}/{repo.name}/blob/{repo.default_branch}/{child}",
                            "git_url": f"{API}/repos/{repo.owner}/{repo.name}/git/blobs/{flat[child]}",
                            "download_url": None,
                            "_links": {"self": f"{API}/repos/{repo.owner}/{repo.name}/contents/{child}",
                                       "git": f"{API}/repos/{repo.owner}/{repo.name}/git/blobs/{flat[child]}",
                                       "html": f"{WEB}/{repo.owner}/{repo.name}/blob/{repo.default_branch}/{child}"}})
        return 200, entries

    def _write_file(self, repo, path: str, data: bytes, branch: str,
                    message: str) -> tuple[str, str]:
        """Commit ``data`` at ``path`` on ``branch``; return (commit, blob)."""
        tip = repo.refs.get("refs/heads/" + branch, "")
        flat = self._tree_of(repo, tip)
        flat[path] = repo.store_object("blob", data)
        tree_oid = self._tree_from_flat(repo, flat)
        parents = [tip] if tip else []
        payload = obj.encode_commit(tree_oid, parents, message)
        commit_oid = repo.store_object("commit", payload)
        repo.update_ref("refs/heads/" + branch, commit_oid)
        return commit_oid, flat[path]

    def _tree_from_flat(self, repo, flat: dict[str, str]) -> str:
        """Build nested trees from a flat ``{path: blob_oid}`` map."""
        def build(prefix: str) -> str:
            entries = []
            direct: dict[str, str] = {}
            subs: dict[str, dict] = {}
            for path, oid in flat.items():
                if not path.startswith(prefix):
                    continue
                rest = path[len(prefix):]
                if "/" in rest:
                    head, _ = rest.split("/", 1)
                    subs.setdefault(head, {})[path] = oid
                else:
                    direct[rest] = oid
            for name, oid in direct.items():
                entries.append(("100644", name, oid))
            for name in subs:
                sub_oid = build(prefix + name + "/")
                entries.append(("40000", name, sub_oid))
            payload = obj.encode_tree(entries)
            return repo.store_object("tree", payload)
        return build("")

    def h_put_content(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        body = req.json or {}
        path = match.group("path")
        branch = body.get("branch", repo.default_branch)
        content = body.get("content", "")
        data = _unb64(content)
        flat = self._tree_of(repo, self._branch_tip(repo, branch))
        existed = path in flat
        # A stale sha means the caller is overwriting a version it has
        # not seen; GitHub answers 409.
        if "sha" in body and body["sha"] is not None:
            if not existed or flat.get(path) != body["sha"]:
                return 409, {"message": "Conflict",
                             "documentation_url": DOCS}
        commit_oid, blob_oid = self._write_file(
            repo, path, data, branch, body.get("message", "Update " + path))
        status = 200 if existed else 201
        return status, {"content": {
            "type": "file", "encoding": "base64", "size": len(data),
            "name": path.rsplit("/", 1)[-1], "path": path,
            "content": _b64(data), "sha": blob_oid,
            "url": f"{API}/repos/{repo.owner}/{repo.name}/contents/{path}",
            "html_url": f"{WEB}/{repo.owner}/{repo.name}/blob/{branch}/{path}",
            "git_url": f"{API}/repos/{repo.owner}/{repo.name}/git/blobs/{blob_oid}",
            "download_url": "",
            "_links": {"git": f"{API}/repos/{repo.owner}/{repo.name}/git/blobs/{blob_oid}",
                       "self": f"{API}/repos/{repo.owner}/{repo.name}/contents/{path}",
                       "html": f"{WEB}/{repo.owner}/{repo.name}/blob/{branch}/{path}"}},
            "commit": self._commit_shape(repo, commit_oid)}

    def h_delete_content(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        body = req.json or {}
        path = match.group("path")
        branch = body.get("branch", repo.default_branch)
        tip = self._branch_tip(repo, branch)
        flat = self._tree_of(repo, tip)
        if path not in flat:
            return 404, NOT_FOUND
        del flat[path]
        tree_oid = self._tree_from_flat(repo, flat)
        payload = obj.encode_commit(
            tree_oid, [tip] if tip else [],
            body.get("message", "Delete " + path))
        commit_oid = repo.store_object("commit", payload)
        repo.update_ref("refs/heads/" + branch, commit_oid)
        return 200, {"commit": self._commit_shape(repo, commit_oid)}

    # -- branches / commits / compare -------------------------------------

    def h_list_branches(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        out = []
        for ref in sorted(repo.refs):
            if not ref.startswith("refs/heads/"):
                continue
            name = ref[len("refs/heads/"):]
            oid = repo.refs[ref]
            out.append({"name": name,
                        "commit": {"sha": oid,
                                   "url": f"{API}/repos/{repo.owner}/{repo.name}/commits/{oid}"},
                        "protected": False})
        return 200, self._page(req, out)

    def h_get_branch(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        branch = match.group("branch")
        oid = repo.refs.get("refs/heads/" + branch)
        if oid is None:
            return 404, NOT_FOUND
        return 200, {"name": branch,
                     "commit": {"sha": oid,
                                "url": f"{API}/repos/{repo.owner}/{repo.name}/commits/{oid}"},
                     "protected": False,
                     "protection": {"enabled": False,
                                    "required_status_checks": {"enforcement_level": "off",
                                                               "contexts": [], "checks": []}}}

    def h_list_commits(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        tip = self._branch_tip(repo, req.query.get("sha"))
        path = req.query.get("path")
        items = []
        seen = set()
        stack = [tip]
        while stack:
            oid = stack.pop(0)
            if not oid or oid in seen or oid not in repo.objects:
                continue
            seen.add(oid)
            blob = repo.objects[oid]
            if blob.split(b" ", 1)[0] != b"commit":
                continue
            parsed = obj.decode_commit(obj.load_raw("commit", blob))
            if path is None or path in self._tree_of(repo, oid):
                items.append(self._rest_commit(repo, oid))
            stack.extend(parsed["parents"])
        return 200, self._page(req, items)

    def _rest_commit(self, repo, oid: str) -> dict:
        shape = self._commit_shape(repo, oid)
        parsed = obj.decode_commit(
            obj.load_raw("commit", repo.objects[oid]))
        shape["commit"] = {
            "author": shape["author"], "committer": shape["committer"],
            "message": parsed["message"],
            "tree": shape["tree"],
            "url": shape["url"],
            "comment_count": 0,
            "verification": shape["verification"]}
        shape["author"] = self.forge.account("adaiversen").public()
        shape["committer"] = self.forge.account("adaiversen").public()
        shape["parents"] = shape["parents"]
        shape["comments_url"] = f"{API}/repos/{repo.owner}/{repo.name}/commits/{oid}/comments"
        return shape

    def h_get_rest_commit(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        oid = match.group("sha")
        blob = repo.objects.get(oid)
        if blob is None or blob.split(b" ", 1)[0] != b"commit":
            return 404, NOT_FOUND
        return 200, self._rest_commit(repo, oid)

    def h_compare(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        base_ref = match.group("base")
        head_ref = match.group("head")
        base_oid = repo.refs.get("refs/heads/" + base_ref, base_ref)
        head_oid = repo.refs.get("refs/heads/" + head_ref, head_ref)
        commits = []
        for oid in self._commit_chain(repo, head_oid, base_oid):
            commits.append(self._rest_commit(repo, oid))
        base_tree = self._tree_of(repo, base_oid)
        head_tree = self._tree_of(repo, head_oid)
        files = []
        for path in sorted(set(base_tree) | set(head_tree)):
            if base_tree.get(path) == head_tree.get(path):
                continue
            old_data = self._blob_bytes(repo, base_tree.get(path))
            new_data = self._blob_bytes(repo, head_tree.get(path))
            added, removed = _line_diff(old_data, new_data)
            files.append({"sha": head_tree.get(path, ""),
                          "filename": path,
                          "status": ("added" if path not in base_tree
                                     else "removed" if path not in head_tree
                                     else "modified"),
                          "additions": added, "deletions": removed,
                          "changes": added + removed,
                          "blob_url": "", "raw_url": "", "contents_url": ""})
        return 200, {"url": f"{API}/repos/{repo.owner}/{repo.name}/compare/{base_ref}...{head_ref}",
                     "html_url": f"{WEB}/{repo.owner}/{repo.name}/compare/{base_ref}...{head_ref}",
                     "permalink_url": f"{WEB}/{repo.owner}/{repo.name}/compare/{base_ref}...{head_ref}",
                     "diff_url": f"{WEB}/{repo.owner}/{repo.name}/compare/{base_ref}...{head_ref}.diff",
                     "patch_url": f"{WEB}/{repo.owner}/{repo.name}/compare/{base_ref}...{head_ref}.patch",
                     "base_commit": (self._rest_commit(repo, base_oid)
                                     if base_oid in repo.objects else None),
                     "merge_base_commit": (self._rest_commit(repo, base_oid)
                                           if base_oid in repo.objects else None),
                     "status": "ahead",
                     "ahead_by": len(commits), "behind_by": 0,
                     "total_commits": len(commits),
                     "commits": commits, "files": files}

    def h_list_collaborators(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        return 200, [self.forge.account(repo.owner).public()]

    def h_list_contributors(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        return 200, [dict(self.forge.account(repo.owner).public(),
                          contributions=1)]

    def h_languages(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        tip = self._branch_tip(repo, None)
        flat = self._tree_of(repo, tip)
        totals: dict[str, int] = {}
        for path, oid in flat.items():
            ext = "." + path.rsplit(".", 1)[-1] if "." in path else ""
            lang = LANG_BY_EXT.get(ext)
            if lang is None:
                continue
            blob = repo.objects.get(oid)
            size = len(obj.load_raw("blob", blob)) if blob else 0
            totals[lang] = totals.get(lang, 0) + size
        return 200, totals

    def h_list_stargazers(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        return 200, []

    def h_list_subscribers(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        return 200, []

    def h_list_tags(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        out = []
        for ref in sorted(repo.refs):
            if not ref.startswith("refs/tags/"):
                continue
            oid = repo.refs[ref]
            blob = repo.objects.get(oid)
            kind = "commit"
            commit_oid = oid
            if blob is not None and blob.split(b" ", 1)[0] == b"tag":
                kind = "tag"
                commit_oid = obj.decode_tag(obj.load_raw("tag", blob))["object"]
            out.append({"name": ref[len("refs/tags/"):],
                        "zipball_url": "", "tarball_url": "",
                        "commit": {"sha": commit_oid,
                                   "url": f"{API}/repos/{repo.owner}/{repo.name}/commits/{commit_oid}"},
                        "node_id": "REF_kwDO" + hashlib.sha1(
                            ref.encode()).hexdigest()[:20]})
        return 200, self._page(req, out)

    def h_list_releases(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        return 200, []

    def h_create_release(self, req, match):
        repo, err = self._need_repo(match)
        if err:
            return err
        body = req.json or {}
        return 201, {"id": self.forge.next_id(),
                     "node_id": "RE_kwDO" + hashlib.sha1(
                         body.get("tag_name", "").encode()).hexdigest()[:20],
                     "tag_name": body.get("tag_name", ""),
                     "target_commitish": body.get("target_commitish",
                                                  repo.default_branch),
                     "name": body.get("name", ""),
                     "draft": bool(body.get("draft", False)),
                     "prerelease": bool(body.get("prerelease", False)),
                     "created_at": obj.FIXED_DATE + "T00:00:00Z",
                     "published_at": obj.FIXED_DATE + "T00:00:00Z",
                     "assets": [], "tarball_url": "", "zipball_url": "",
                     "body": body.get("body", "")}


def _iso_from_ident(ident: str) -> str:
    """``Name <email> ts +0000`` → ISO date; falls back to FIXED_DATE."""
    parts = ident.rsplit(" ", 2)
    if len(parts) == 3 and parts[1].isdigit():
        from datetime import datetime, timezone
        return datetime.fromtimestamp(int(parts[1]),
                                      tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
    return obj.FIXED_DATE + "T00:00:00Z"


def _line_diff(old: bytes, new: bytes) -> tuple[int, int]:
    """Count added/removed lines between two file versions."""
    import difflib
    old_lines = old.decode(errors="replace").splitlines()
    new_lines = new.decode(errors="replace").splitlines()
    added = removed = 0
    for line in difflib.unified_diff(old_lines, new_lines, lineterm=""):
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed


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
