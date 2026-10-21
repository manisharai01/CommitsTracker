"""Technology detection tests - a fake GitHub answers the GraphQL queries from
an in-memory file tree, so no network or token is needed.

Run directly:

    python tests/test_stack.py

or with pytest:

    pytest -q tests
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# Make the package importable when run as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from github_contrib import stack  # noqa: E402
from github_contrib.analytics import compute_analytics  # noqa: E402
from github_contrib.exporters import export_csvs  # noqa: E402
from github_contrib.models import CollectedData, CommitRecord, RepoRecord  # noqa: E402
from github_contrib.offline import load_collected_from_csv  # noqa: E402
from github_contrib.statistics import compute_statistics  # noqa: E402

PACKAGE_JSON = json.dumps({
    "dependencies": {"next": "14.0.0", "react": "18.2.0", "@prisma/client": "5.0.0", "stripe": "1.0.0",
                     "@aws-sdk/client-s3": "3.0.0", "left-pad": "1.0.0"},
    "devDependencies": {"jest": "29.0.0", "@testing-library/react": "14.0.0", "tailwindcss": "3.0.0"},
})
PUBSPEC = """name: fat_money
dependencies:
  flutter:
    sdk: flutter
  provider: ^6.0.0
  firebase_core: ^2.0.0
  dio: ^5.0.0
dev_dependencies:
  flutter_test:
    sdk: flutter
flutter:
  uses-material-design: true
"""


def test_dependency_files_name_their_technologies():
    t = stack.technologies_in_file
    assert t("package.json", PACKAGE_JSON) == {
        "Next.js", "React", "Prisma", "Stripe", "AWS", "Jest", "Testing Library", "Tailwind CSS"}
    assert t("package.json", "not json") == set() and t("package.json", "[1, 2]") == set()
    assert t("pubspec.yaml", PUBSPEC) == {"Flutter", "Provider", "Firebase"}
    assert t("requirements.txt", "Django>=4.2\n# comment\npsycopg2-binary==2.9 ; python_version>'3'\n"
             "-r base.txt\ncelery[redis]\nscikit_learn\ngoogle-cloud-storage\n") == {
        "Django", "PostgreSQL", "Celery", "scikit-learn", "Google Cloud"}
    assert t("requirements-dev.txt", "pytest\n") == {"pytest"}
    assert t("pyproject.toml", '[project]\ndependencies = ["fastapi>=0.1", "SQLAlchemy"]\n'
             '[project.optional-dependencies]\ntest = ["pytest"]\n'
             '[tool.poetry.dependencies]\nredis = "*"\n') == {"FastAPI", "SQLAlchemy", "pytest", "Redis"}
    assert t("pyproject.toml", "this is = = not toml") == set()
    assert t("Pipfile", '[packages]\nflask = "*"\n[dev-packages]\npytest = "*"\n') == {"Flask", "pytest"}
    assert t("go.mod", "module x\n\ngo 1.21\n\nrequire (\n\tgithub.com/gin-gonic/gin v1.9.1\n"
             "\tgorm.io/gorm v1.25.0\n)\nrequire github.com/redis/go-redis/v9 v9.0.0\n") == {"Gin", "GORM", "Redis"}
    assert t("Cargo.toml", '[dependencies]\naxum = "0.7"\ntokio = { version = "1" }\n') == {"Axum", "Tokio"}
    assert t("pom.xml", "<artifactId>spring-boot-starter-web</artifactId><artifactId>postgresql</artifactId>") == {
        "Spring Boot", "PostgreSQL"}
    assert t("build.gradle.kts", 'plugins { id("com.android.application") }\nimplementation("androidx.compose.ui:ui")') == {
        "Android", "Jetpack Compose"}
    assert t("Api.csproj", '<PackageReference Include="Microsoft.AspNetCore.OpenApi" />') == {"ASP.NET Core"}
    assert t("Gemfile", "source 'x'\ngem 'rails', '~> 7'\ngem \"pg\"\n") == {"Ruby on Rails", "PostgreSQL"}
    assert t("composer.json", json.dumps({"require": {"laravel/framework": "^10", "symfony/console": "^6"}})) == {
        "Laravel", "Symfony"}
    assert t("README.md", "react next.js") == set()
    print("ok  test_dependency_files_name_their_technologies")


def test_files_and_folders_name_technologies_and_implied_ones_follow():
    entries = [("Dockerfile", "blob"), ("docker-compose.yml", "blob"), ("main.tf", "blob"),
               ("next.config.mjs", "blob"), ("k8s", "tree"), (".github", "tree"), ("README.md", "blob")]
    found = stack.technologies_in_listing(entries, {".github": ["workflows", "CODEOWNERS"]})
    assert found == {"Docker", "Docker Compose", "Terraform", "Next.js", "Kubernetes", "GitHub Actions"}
    assert stack.technologies_in_listing([(".github", "tree")], {".github": ["ISSUE_TEMPLATE"]}) == set()
    # Next.js brings React, Mongoose brings MongoDB; core frameworks first, then
    # catalog order, unknown names last.
    assert stack.with_implied({"Next.js", "Mongoose", "Docker"}) == ["React", "Next.js", "MongoDB", "Mongoose", "Docker"]
    assert stack.with_implied({"Socket.IO", "GetX", "Flutter", "Razorpay"}) == ["Flutter", "Socket.IO", "GetX", "Razorpay"]
    assert set(stack.CATEGORY_OF) >= {"React", "Flutter", "PostgreSQL", "GitHub Actions", "Jest", "OpenAI API", "Stripe"}
    print("ok  test_files_and_folders_name_technologies_and_implied_ones_follow")


class FakeGitHub:
    """Answers stack.py's GraphQL queries from {repo: {path: text | None (folder)}}."""

    def __init__(self, repos: dict[str, dict[str, str | None]], languages: dict[str, dict[str, int]],
                 forbidden=()) -> None:
        self.repos = repos
        self.languages = languages
        self.forbidden = set(forbidden)
        self.queries: list[str] = []

    def _listing(self, files: dict[str, str | None], folder: str) -> dict | None:
        prefix = f"{folder}/" if folder else ""
        if folder and folder not in files:
            return None
        names: dict[str, str] = {}
        for path, text in files.items():
            if path.startswith(prefix) and path != folder:
                rest = path[len(prefix):]
                name = rest.split("/", 1)[0]
                names[name] = "tree" if "/" in rest or files.get(prefix + name, "") is None else "blob"
        return {"entries": [{"name": n, "type": k} for n, k in sorted(names.items())]}

    async def graphql(self, query: str):
        self.queries.append(query)
        data, errors = {}, []
        for line in query.strip().splitlines()[1:-1]:
            alias, owner, name, body = re.match(
                r'^(w\d+): repository\(owner: "([^"]+)", name: "([^"]+)"\) \{ (.*) \}$', line).groups()
            full = f"{owner}/{name}"
            if full in self.forbidden:
                data[alias] = None
                errors.append({"type": "FORBIDDEN", "path": [alias], "message": "no access"})
                continue
            files = self.repos[full]
            obj: dict = {}
            if body.startswith("languages("):
                obj["languages"] = {"edges": [{"size": s, "node": {"name": n}}
                                              for n, s in self.languages.get(full, {}).items()]}
            for inner, path, kind in re.findall(r'(\w+): object\(expression: "HEAD:([^"]*)"\) \{ \.\.\. on (Tree|Blob)', body):
                if kind == "Tree":
                    obj[inner] = self._listing(files, path)
                else:
                    text = files.get(path)
                    obj[inner] = {"text": text} if isinstance(text, str) else None
            data[alias] = obj
        return data, errors


def _repo(full: str) -> RepoRecord:
    owner, name = full.split("/")
    return RepoRecord(full_name=full, name=name, owner=owner, organization=owner)


def test_collect_stacks_reads_root_folders_and_sub_projects():
    repos = {
        "acme/web": {"package.json": PACKAGE_JSON, "Dockerfile": "FROM node", ".github": None,
                     ".github/workflows": None, ".github/workflows/ci.yml": "on: push",
                     "node_modules": None, "node_modules/react/package.json": PACKAGE_JSON},
        "acme/mono": {"apps": None, "apps/mobile": None, "apps/mobile/pubspec.yaml": PUBSPEC,
                      "apps/api": None, "apps/api/requirements.txt": "fastapi\nasyncpg\n",
                      "backend": None, "backend/go.mod": "require github.com/gin-gonic/gin v1.9.1\n",
                      "README.md": "hello"},
        "acme/empty": {},
        "acme/secret": {},
    }
    languages = {"acme/web": {"TypeScript": 9000, "CSS": 500, "Dockerfile": 40}, "acme/mono": {"Dart": 7000, "Python": 3000, "Go": 1500}}
    fake = FakeGitHub(repos, languages, forbidden={"acme/secret"})
    records = [_repo(full) for full in repos]
    unread = asyncio.run(stack.collect_stacks(records, lambda _repo: fake, inflight=2))
    by_name = {r.full_name: r for r in records}
    web, mono, empty, secret = (by_name[n] for n in ("acme/web", "acme/mono", "acme/empty", "acme/secret"))
    assert unread == 1 and secret.stack_read is False and secret.technologies == []
    assert web.stack_read and web.languages == {"TypeScript": 9000, "CSS": 500, "Dockerfile": 40}
    assert {"React", "Next.js", "Prisma", "Jest", "Docker", "GitHub Actions"} <= set(web.technologies)
    assert not any("node_modules" in q for q in fake.queries), "build folders are never opened"
    assert {"Flutter", "Provider", "Firebase", "FastAPI", "PostgreSQL", "Gin"} <= set(mono.technologies), mono.technologies
    assert empty.stack_read and empty.technologies == [] and empty.languages == {}
    print("ok  test_collect_stacks_reads_root_folders_and_sub_projects")


def test_collect_stacks_never_raises():
    class Broken:
        async def graphql(self, query):
            raise RuntimeError("boom")

    records = [_repo("acme/x")]
    assert asyncio.run(stack.collect_stacks(records, lambda _repo: Broken(), inflight=1)) == 1
    assert records[0].stack_read is False
    print("ok  test_collect_stacks_never_raises")


def _commit(sha: str, repo: str, day: int) -> CommitRecord:
    owner, name = repo.split("/")
    when = datetime(2026, 3, day, 12, tzinfo=timezone.utc)
    return CommitRecord(
        repository=name, full_name=repo, owner=owner, organization=owner, sha=sha, message="feat: x",
        message_first_line="feat: x", author_login="neha", author_name="N", author_email="n@x.com",
        committer_name="N", committer_email="n@x.com", authored_date=when, committed_date=when,
        branch="main", url="", parent_count=1,
    )


def test_technologies_are_weighted_by_commits_and_survive_regen():
    web = _repo("acme/web")
    web.languages, web.technologies, web.stack_read = (
        {"TypeScript": 900, "CSS": 150, "Dockerfile": 5}, ["React", "Redux", "Docker"], True)
    app = _repo("acme/app")
    app.languages, app.technologies, app.stack_read = {"Dart": 1000, "Kotlin": 20}, ["Flutter", "Docker"], True
    old = _repo("acme/old")  # technologies never read (an older run)
    commits = [_commit(f"w{i}", "acme/web", 1 + i) for i in range(5)]
    commits += [_commit(f"a{i}", "acme/app", 10 + i) for i in range(3)] + [_commit("o", "acme/old", 20)]
    data = CollectedData(repos=[web, app, old], commits=commits, meta={"target_logins": ["neha"]})
    s = compute_analytics(data).stack
    assert (s.read, s.unread) == (2, 1)
    assert [(u.name, u.commits, len(u.repositories)) for u in s.languages] == [
        ("TypeScript", 5, 1), ("CSS", 5, 1), ("Dart", 3, 1)], "Kotlin is under 10%, Dockerfile isn't a language here"
    docker = next(u for u in s.technologies if u.name == "Docker")
    assert docker.commits == 8 and sorted(docker.repositories) == ["acme/app", "acme/web"]
    assert docker.category == "Cloud & DevOps" and docker.last_used.isoformat() == "2026-03-12"
    a = compute_analytics(data)
    summary = a.summary()
    # The frameworks projects are built on come before the libraries used with them.
    assert summary["languages_used"] == "TypeScript, CSS, Dart" and summary["frameworks_used"] == "React, Flutter, Redux"
    assert summary["data_and_infrastructure_used"] == "Docker"
    assert any("led by React (5 commits), Flutter (3 commits) and Redux (5 commits)." in h for h in a.highlights)
    # repositories.csv and back (--regen) keeps all of it.
    with tempfile.TemporaryDirectory() as tmp:
        export_csvs(Path(tmp), compute_statistics(data))
        loaded = {r.full_name: r for r in load_collected_from_csv(Path(tmp)).repos}
    assert loaded["acme/web"].languages == web.languages
    assert loaded["acme/web"].technologies == ["React", "Redux", "Docker"]
    assert loaded["acme/web"].stack_read is True and loaded["acme/old"].stack_read is False
    assert compute_analytics(CollectedData(repos=[old], commits=commits[-1:])).stack is None, "nothing read: no section"
    print("ok  test_technologies_are_weighted_by_commits_and_survive_regen")


def _all_tests():
    return [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]


def main() -> int:
    failures = 0
    for test in _all_tests():
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {test.__name__}: {exc!r}")
            import traceback
            traceback.print_exc()
    total = len(_all_tests())
    print(f"\n{total - failures}/{total} tests passed.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
