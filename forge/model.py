"""In-memory state: repositories, issues, pull requests.

Pure data model plus shape rendering. No I/O, no HTTP. Shape rendering
is cached per object and keyed by a generation counter that every
mutation bumps, so a stale shape is impossible by construction.
"""

from __future__ import annotations

import hashlib

from . import objects as obj

REPO_NODE_PREFIX = "R_kgDO"
ISSUE_NODE_PREFIX = "I_kwDO"
PR_NODE_PREFIX = "PR_kwDO"
USER_NODE_PREFIX = "U_kgDO"

# Deterministic identifiers: the same creation order always yields the
# same ids, which keeps exported state byte-identical across runs.


def _node(prefix: str, seed: str) -> str:
    return prefix + hashlib.sha1(seed.encode()).hexdigest()[:20]


def _iso(ts: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


class Account:
    """A GitHub user as seen by the API."""

    __slots__ = ("login", "uid", "node")

    def __init__(self, login: str, uid: int):
        self.login = login
        self.uid = uid
        self.node = _node(USER_NODE_PREFIX, login)

    def public(self) -> dict:
        return {
            "login": self.login,
            "id": self.uid,
            "node_id": self.node,
            "avatar_url": f"https://avatars.githubusercontent.com/u/{self.uid}?v=4",
            "gravatar_id": "",
            "url": f"https://api.github.com/users/{self.login}",
            "html_url": f"https://github.com/{self.login}",
            "followers_url": f"https://api.github.com/users/{self.login}/followers",
            "following_url": f"https://api.github.com/users/{self.login}/following{{/other_user}}",
            "gists_url": f"https://api.github.com/users/{self.login}/gists{{/gist_id}}",
            "starred_url": f"https://api.github.com/users/{self.login}/starred{{/owner}}{{/repo}}",
            "subscriptions_url": f"https://api.github.com/users/{self.login}/subscriptions",
            "organizations_url": f"https://api.github.com/users/{self.login}/orgs",
            "repos_url": f"https://api.github.com/users/{self.login}/repos",
            "events_url": f"https://api.github.com/users/{self.login}/events{{/privacy}}",
            "received_events_url": f"https://api.github.com/users/{self.login}/received_events",
            "type": "User",
            "user_view_type": "public",
            "site_admin": False,
        }


class Issue:
    """One issue. ``generation`` bumps on every mutation."""

    __slots__ = ("number", "title", "body", "state", "labels", "assignee",
                 "milestone", "comments", "created_at", "updated_at",
                 "closed_at", "author", "locked", "reactions", "iid",
                 "node", "generation", "_shape", "_shape_gen")

    def __init__(self, number: int, title: str, body: str, author: str,
                 iid: int):
        self.number = number
        self.title = title
        self.body = body
        self.state = "open"
        self.labels: list[dict] = []
        self.assignee = None
        self.milestone = None
        self.comments: list[dict] = []
        self.created_at = obj.FIXED_DATE + "T00:00:00Z"
        self.updated_at = obj.FIXED_DATE + "T00:00:00Z"
        self.closed_at = None
        self.author = author
        self.locked = False
        self.reactions = {"url": "", "total_count": 0, "+1": 0, "-1": 0,
                          "laugh": 0, "hooray": 0, "confused": 0,
                          "heart": 0, "rocket": 0, "eyes": 0}
        self.iid = iid
        self.node = _node(ISSUE_NODE_PREFIX, f"issue:{iid}")
        self.generation = 0
        self._shape = None
        self._shape_gen = -1

    def touch(self) -> None:
        self.generation += 1

    def label_view(self) -> list[dict]:
        return [{"id": 1, "node_id": "LA_kwDO" + hashlib.sha1(
            l["name"].encode()).hexdigest()[:20],
                 "url": "", "name": l["name"], "color": l.get("color", "ededed"),
                 "default": False, "description": l.get("description", "")}
                for l in self.labels]


class PullRequest:
    """One pull request. ``generation`` bumps on every mutation."""

    __slots__ = ("number", "title", "body", "state", "head", "base",
                 "merged", "mergeable", "mergeable_state", "draft",
                 "head_sha", "base_sha", "labels", "assignee", "milestone",
                 "comments", "created_at", "updated_at", "closed_at",
                 "merged_at", "author", "reviewers", "iid", "node",
                 "merge_sha", "generation", "_shape", "_shape_gen")

    def __init__(self, number: int, title: str, body: str, head: str,
                 base: str, author: str, iid: int, head_sha: str,
                 base_sha: str, draft: bool = False):
        self.number = number
        self.title = title
        self.body = body
        self.state = "open"
        self.head = head
        self.base = base
        self.merged = False
        self.mergeable = True
        self.mergeable_state = "clean"
        self.draft = draft
        self.head_sha = head_sha
        self.base_sha = base_sha
        self.labels: list[dict] = []
        self.assignee = None
        self.milestone = None
        self.comments: list[dict] = []
        self.created_at = obj.FIXED_DATE + "T00:00:00Z"
        self.updated_at = obj.FIXED_DATE + "T00:00:00Z"
        self.closed_at = None
        self.merged_at = None
        self.author = author
        self.reviewers: list[str] = []
        self.iid = iid
        self.node = _node(PR_NODE_PREFIX, f"pr:{iid}")
        self.merge_sha = None
        self.generation = 0
        self._shape = None
        self._shape_gen = -1

    def touch(self) -> None:
        self.generation += 1


class Repository:
    """One repository: refs, objects, issues, pulls, labels, milestones."""

    def __init__(self, owner: str, name: str, rid: int, forge: "ForgeState"):
        self.owner = owner
        self.name = name
        self.rid = rid
        self.forge = forge
        self.node = _node(REPO_NODE_PREFIX, f"{owner}/{name}")
        self.description = ""
        self.private = False
        self.default_branch = "main"
        self.created_at = _iso(obj.FIXED_TS)
        self.updated_at = _iso(obj.FIXED_TS)
        self.pushed_at = _iso(obj.FIXED_TS)
        self.refs: dict[str, str] = {}
        self.objects: dict[str, bytes] = {}
        self.labels: list[dict] = [
            {"name": "bug", "color": "d73a4a", "description": "Something isn't working"},
            {"name": "documentation", "color": "0075ca", "description": "Improvements or additions to documentation"},
            {"name": "duplicate", "color": "cfd3d7", "description": "This issue or pull request already exists"},
            {"name": "enhancement", "color": "a2eeef", "description": "New feature or request"},
            {"name": "good first issue", "color": "7057ff", "description": "Good for newcomers"},
            {"name": "help wanted", "color": "008672", "description": "Extra attention is needed"},
            {"name": "invalid", "color": "e4e669", "description": "This doesn't seem right"},
            {"name": "question", "color": "d876e3", "description": "Further information is requested"},
            {"name": "wontfix", "color": "ffffff", "description": "This will not be worked on"},
        ]
        self.milestones: list[dict] = []
        self.issues: dict[int, Issue] = {}
        self.pulls: dict[int, PullRequest] = {}
        self.next_issue = 1
        self.next_milestone = 1
        # A new repository is EMPTY: no refs, no objects. The first
        # commit arrives through a push or the contents API, exactly
        # like a real empty GitHub repository.

    # -- git plumbing ---------------------------------------------------

    def lookup_ref(self, ref: str) -> str | None:
        return self.refs.get(ref)

    def update_ref(self, ref: str, oid: str) -> None:
        """Move a ref; pull requests tracking that branch see the change."""
        self.refs[ref] = oid
        self.pushed_at = _iso(obj.FIXED_TS)
        branch = ref.removeprefix("refs/heads/")
        if ref.startswith("refs/heads/"):
            for pr in self.pulls.values():
                if pr.state != "open":
                    continue
                if pr.head == branch:
                    pr.head_sha = oid
                    pr.touch()
                if pr.base == branch:
                    pr.base_sha = oid
                    pr.touch()

    def delete_ref(self, ref: str) -> None:
        self.refs.pop(ref, None)
        branch = ref.removeprefix("refs/heads/")
        if ref.startswith("refs/heads/"):
            for pr in self.pulls.values():
                if pr.state != "open":
                    continue
                if pr.head == branch:
                    pr.head_sha = ""
                    pr.touch()
                if pr.base == branch:
                    pr.base_sha = ""
                    pr.touch()

    def store_object(self, kind: str, payload: bytes) -> str:
        oid = obj.object_id(kind, payload)
        self.objects[oid] = obj.raw_bytes(kind, payload)
        return oid

    def fetch_object(self, oid: str) -> bytes | None:
        return self.objects.get(oid)

    # -- issues / pulls -------------------------------------------------

    def open_issue(self, title: str, body: str, author: str,
                   labels: list[str] | None = None) -> Issue:
        number = self.next_issue
        self.next_issue += 1
        issue = Issue(number, title, body or "", author,
                      self.forge.next_id())
        for name in labels or []:
            issue.labels.append({"name": name, "color": "ededed",
                                 "description": ""})
        self.issues[number] = issue
        return issue

    def open_pull(self, title: str, body: str, head: str, base: str,
                  author: str, draft: bool = False) -> PullRequest:
        number = self.next_issue
        self.next_issue += 1
        head_oid = self.refs.get("refs/heads/" + head, "")
        base_oid = self.refs.get("refs/heads/" + base, "")
        pr = PullRequest(number, title, body or "", head, base, author,
                         self.forge.next_id(), head_oid, base_oid, draft)
        self.pulls[number] = pr
        return pr

    def merge_base(self, first: str, second: str) -> str | None:
        """The nearest commit reachable from both ``first`` and ``second``."""
        ancestors: set[str] = set()
        stack = [first]
        while stack:
            oid = stack.pop()
            if not oid or oid in ancestors or oid not in self.objects:
                continue
            ancestors.add(oid)
            blob = self.objects[oid]
            if blob.split(b" ", 1)[0] != b"commit":
                continue
            stack.extend(obj.decode_commit(
                obj.load_raw("commit", blob))["parents"])
        stack = [second]
        seen: set[str] = set()
        while stack:
            oid = stack.pop(0)
            if not oid or oid in seen or oid not in self.objects:
                continue
            seen.add(oid)
            if oid in ancestors:
                return oid
            blob = self.objects[oid]
            if blob.split(b" ", 1)[0] != b"commit":
                continue
            stack.extend(obj.decode_commit(
                obj.load_raw("commit", blob))["parents"])
        return None

    def _flat_tree(self, commit_oid: str) -> dict[str, str]:
        """Flat ``{path: blob_oid}`` for a commit."""
        out: dict[str, str] = {}
        if not commit_oid or commit_oid not in self.objects:
            return out
        parsed = obj.decode_commit(
            obj.load_raw("commit", self.objects[commit_oid]))
        self._walk_tree(parsed["tree"], "", out)
        return out

    def _walk_tree(self, tree_oid: str, prefix: str,
                   out: dict[str, str]) -> None:
        blob = self.objects.get(tree_oid)
        if blob is None:
            return
        for mode, name, child in obj.decode_tree(
                obj.load_raw("tree", blob)):
            path = prefix + name
            if mode == "40000":
                self._walk_tree(child, path + "/", out)
            else:
                out[path] = child

    def merge_conflict(self, pr: PullRequest) -> bool:
        """Three-way check: head and base both changed the same file
        differently since their merge base."""
        base_oid = self.refs.get("refs/heads/" + pr.base)
        head_oid = self.refs.get("refs/heads/" + pr.head)
        if not base_oid or not head_oid:
            return False
        ancestor = self.merge_base(head_oid, base_oid)
        if ancestor is None:
            return False
        old = self._flat_tree(ancestor)
        new_head = self._flat_tree(head_oid)
        new_base = self._flat_tree(base_oid)
        for path in set(old) | set(new_head) | set(new_base):
            o, h, t = old.get(path), new_head.get(path), new_base.get(path)
            if h == o or t == o:
                continue
            if h != t:
                return True
        return False

    def squash_merge(self, number: int, commit_message: str = "") -> dict:
        """Squash-merge an open PR; returns the merge commit info."""
        pr = self.pulls.get(number)
        if pr is None:
            raise KeyError("pull request not found")
        if pr.state != "open":
            raise ValueError("Pull request is not open")
        if self.merge_conflict(pr):
            raise ValueError("Merge conflict")
        base_oid = self.refs.get("refs/heads/" + pr.base)
        head_oid = self.refs.get("refs/heads/" + pr.head)
        if base_oid is None or head_oid is None:
            raise ValueError("base or head branch missing")
        # The merge commit reuses the head tree and the base as its parent.
        head_blob = self.objects.get(head_oid)
        if head_blob is None:
            raise ValueError("head commit missing")
        head_parsed = obj.decode_commit(obj.load_raw("commit", head_blob))
        merge = obj.encode_commit(
            head_parsed["tree"], [base_oid],
            commit_message or f"{pr.title} (#{pr.number})")
        merge_oid = obj.object_id("commit", merge)
        self.objects[merge_oid] = obj.raw_bytes("commit", merge)
        self.refs["refs/heads/" + pr.base] = merge_oid
        pr.state = "closed"
        pr.merged = True
        pr.merge_sha = merge_oid
        pr.merged_at = obj.FIXED_DATE + "T00:00:00Z"
        pr.closed_at = obj.FIXED_DATE + "T00:00:00Z"
        pr.touch()
        return {"sha": merge_oid, "merged": True,
                "message": "Pull request successfully merged"}


class ForgeState:
    """The whole forge: every repository and every account."""

    def __init__(self):
        self.repositories: dict[tuple[str, str], Repository] = {}
        self.accounts: dict[str, Account] = {}
        self._seq = 1

    def next_id(self) -> int:
        value = self._seq
        self._seq += 1
        return value

    def account(self, login: str) -> Account:
        if login not in self.accounts:
            self.accounts[login] = Account(login, self.next_id())
        return self.accounts[login]

    def create_repo(self, owner: str, name: str, description: str = "",
                    private: bool = False) -> Repository:
        key = (owner, name)
        if key in self.repositories:
            raise ValueError("Repository already exists")
        repo = Repository(owner, name, self.next_id(), self)
        repo.description = description
        repo.private = private
        self.repositories[key] = repo
        return repo

    def find_repo(self, owner: str, name: str) -> Repository | None:
        return self.repositories.get((owner, name))

    def delete_repo(self, owner: str, name: str) -> bool:
        return self.repositories.pop((owner, name), None) is not None
