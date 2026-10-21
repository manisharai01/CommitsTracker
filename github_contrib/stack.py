"""Technologies used in the repositories a report covers.

GitHub's language statistics say which languages a repository is written in.
Its dependency files (package.json, pubspec.yaml, requirements.txt, go.mod,
pom.xml, ...) and a few well-known files (Dockerfile, .github/workflows,
Terraform files, ...) say which frameworks, databases, cloud services and
tools it uses. Both are read for the repositories with the tracked accounts'
commits, with a handful of batched GraphQL queries (never the repositories'
full contents), and kept on each RepoRecord (repositories.csv), so --regen
can rebuild the report's technology section offline.

A technology found in a repository shows it is used there; it doesn't prove
which parts the tracked accounts worked on. The report says so, and weights
each technology by the accounts' commits in the repositories that use it.
"""

from __future__ import annotations

import json
import re
import tomllib
from typing import Callable

from .client import GitHubClient
from .graphql import Walk, _Progress, _repo_root, _single, _str, run_walks
from .logging_config import get_logger
from .models import RepoRecord

log = get_logger("stack")

# ---------------------------------------------------------------------------
# What there is to find
# ---------------------------------------------------------------------------

#: Technology categories, in display order, with what each holds.
CATALOG: dict[str, tuple[str, ...]] = {
    "Frontend": (
        "React", "Next.js", "Vue.js", "Nuxt", "Angular", "Svelte", "SvelteKit", "Astro", "Electron",
        "Redux", "React Query", "Zustand", "MobX", "Tailwind CSS", "Material UI", "Chakra UI",
        "Bootstrap", "Ant Design", "styled-components", "Three.js", "D3.js", "Chart.js", "jQuery",
        "Vite", "Webpack", "Storybook",
    ),
    "Backend": (
        "Express", "NestJS", "Fastify", "Koa", "Hapi", "GraphQL", "Apollo GraphQL", "tRPC", "Socket.IO",
        "Django", "Django REST framework", "Flask", "FastAPI", "Celery", "Streamlit", "Scrapy",
        "Spring Boot", "Hibernate", "Ruby on Rails", "Sinatra", "Sidekiq", "Laravel", "Symfony",
        "Gin", "Echo", "Fiber", "Actix Web", "Axum", "Rocket", "Tokio", "ASP.NET Core",
        "BullMQ", "RabbitMQ", "Kafka",
    ),
    "Mobile": (
        "Flutter", "React Native", "Expo", "Ionic", "Capacitor", "Android", "Jetpack Compose",
        "Provider", "Riverpod", "BLoC", "GetX",
    ),
    "Data & storage": (
        "PostgreSQL", "MySQL", "MongoDB", "Mongoose", "Redis", "SQLite", "Elasticsearch", "Supabase",
        "Prisma", "TypeORM", "Sequelize", "Drizzle ORM", "Knex", "SQLAlchemy", "GORM", "Diesel",
        "Entity Framework", "Hive",
    ),
    "Cloud & DevOps": (
        "Docker", "Docker Compose", "Kubernetes", "Helm", "Terraform", "GitHub Actions", "GitLab CI",
        "Jenkins", "CircleCI", "Bitbucket Pipelines", "Azure Pipelines", "Travis CI", "AWS",
        "Google Cloud", "Google Cloud Build", "Azure", "Firebase", "Vercel", "Netlify", "Heroku",
        "Render", "Fly.io", "Serverless Framework", "Nginx",
    ),
    "Testing": (
        "Jest", "Vitest", "Mocha", "Chai", "Testing Library", "Cypress", "Playwright", "Puppeteer",
        "Selenium", "Supertest", "pytest", "JUnit", "RSpec", "PHPUnit", "xUnit",
    ),
    "AI & data": (
        "OpenAI API", "Anthropic API", "LangChain", "Hugging Face Transformers", "TensorFlow",
        "PyTorch", "scikit-learn", "pandas", "NumPy", "Matplotlib", "OpenCV",
    ),
    "Integrations": (
        "Stripe", "Razorpay", "PayPal", "Twilio", "SendGrid", "Nodemailer", "Google Maps", "Mapbox",
        "Auth0", "Passport", "JWT", "Sentry", "Algolia", "Cloudinary",
    ),
}
CATEGORIES: tuple[str, ...] = tuple(CATALOG)
CATEGORY_OF: dict[str, str] = {tech: category for category, techs in CATALOG.items() for tech in techs}

#: A technology that implies another one.
_IMPLIES = {
    "Next.js": "React", "Nuxt": "Vue.js", "SvelteKit": "Svelte", "Expo": "React Native",
    "Mongoose": "MongoDB", "Django REST framework": "Django", "Docker Compose": "Docker",
}

# npm (package.json): exact package names, then scope prefixes.
_NPM = {
    "react": "React", "react-dom": "React", "next": "Next.js", "vue": "Vue.js", "nuxt": "Nuxt",
    "svelte": "Svelte", "@sveltejs/kit": "SvelteKit", "astro": "Astro", "electron": "Electron",
    "react-native": "React Native", "expo": "Expo", "@ionic/core": "Ionic", "@ionic/angular": "Ionic",
    "@ionic/react": "Ionic", "@ionic/vue": "Ionic", "@capacitor/core": "Capacitor",
    "redux": "Redux", "@reduxjs/toolkit": "Redux", "react-redux": "Redux",
    "@tanstack/react-query": "React Query", "react-query": "React Query", "zustand": "Zustand", "mobx": "MobX",
    "tailwindcss": "Tailwind CSS", "@mui/material": "Material UI", "@material-ui/core": "Material UI",
    "@chakra-ui/react": "Chakra UI", "bootstrap": "Bootstrap", "react-bootstrap": "Bootstrap",
    "antd": "Ant Design", "styled-components": "styled-components", "three": "Three.js", "d3": "D3.js",
    "chart.js": "Chart.js", "react-chartjs-2": "Chart.js", "jquery": "jQuery", "vite": "Vite",
    "webpack": "Webpack", "storybook": "Storybook",
    "express": "Express", "fastify": "Fastify", "koa": "Koa", "@hapi/hapi": "Hapi", "graphql": "GraphQL",
    "@apollo/client": "Apollo GraphQL", "@apollo/server": "Apollo GraphQL", "apollo-server": "Apollo GraphQL",
    "apollo-server-express": "Apollo GraphQL", "@trpc/server": "tRPC", "socket.io": "Socket.IO",
    "socket.io-client": "Socket.IO", "bull": "BullMQ", "bullmq": "BullMQ", "amqplib": "RabbitMQ",
    "kafkajs": "Kafka",
    "pg": "PostgreSQL", "postgres": "PostgreSQL", "mysql": "MySQL", "mysql2": "MySQL", "mongodb": "MongoDB",
    "mongoose": "Mongoose", "redis": "Redis", "ioredis": "Redis", "sqlite3": "SQLite",
    "better-sqlite3": "SQLite", "@elastic/elasticsearch": "Elasticsearch",
    "@supabase/supabase-js": "Supabase", "prisma": "Prisma", "@prisma/client": "Prisma",
    "typeorm": "TypeORM", "sequelize": "Sequelize", "drizzle-orm": "Drizzle ORM", "knex": "Knex",
    "firebase": "Firebase", "firebase-admin": "Firebase", "aws-sdk": "AWS",
    "jest": "Jest", "vitest": "Vitest", "mocha": "Mocha", "chai": "Chai", "cypress": "Cypress",
    "@playwright/test": "Playwright", "playwright": "Playwright", "puppeteer": "Puppeteer",
    "selenium-webdriver": "Selenium", "supertest": "Supertest",
    "openai": "OpenAI API", "@anthropic-ai/sdk": "Anthropic API", "langchain": "LangChain",
    "@tensorflow/tfjs": "TensorFlow", "@tensorflow/tfjs-node": "TensorFlow",
    "stripe": "Stripe", "@stripe/stripe-js": "Stripe", "razorpay": "Razorpay", "twilio": "Twilio",
    "@sendgrid/mail": "SendGrid", "nodemailer": "Nodemailer", "@react-google-maps/api": "Google Maps",
    "mapbox-gl": "Mapbox", "passport": "Passport", "jsonwebtoken": "JWT", "algoliasearch": "Algolia",
    "cloudinary": "Cloudinary",
}
_NPM_PREFIXES = {
    "@aws-sdk/": "AWS", "@google-cloud/": "Google Cloud", "@azure/": "Azure", "@nestjs/": "NestJS",
    "@angular/": "Angular", "@testing-library/": "Testing Library", "@storybook/": "Storybook",
    "@langchain/": "LangChain", "@sentry/": "Sentry", "@auth0/": "Auth0", "@paypal/": "PayPal",
    "@googlemaps/": "Google Maps", "@firebase/": "Firebase", "@react-native-firebase/": "Firebase",
}
# pub (pubspec.yaml). Flutter itself is "sdk: flutter".
_PUB = {
    "provider": "Provider", "flutter_riverpod": "Riverpod", "riverpod": "Riverpod",
    "hooks_riverpod": "Riverpod", "flutter_bloc": "BLoC", "bloc": "BLoC", "get": "GetX",
    "firebase_core": "Firebase", "cloud_firestore": "Firebase", "firebase_auth": "Firebase",
    "firebase_messaging": "Firebase", "sqflite": "SQLite", "hive": "Hive", "hive_flutter": "Hive",
    "google_maps_flutter": "Google Maps", "razorpay_flutter": "Razorpay", "flutter_stripe": "Stripe",
    "sentry_flutter": "Sentry", "supabase_flutter": "Supabase", "graphql_flutter": "GraphQL",
    "socket_io_client": "Socket.IO",
}
# PyPI (requirements*.txt, pyproject.toml, Pipfile): normalised names, then prefixes.
_PYPI = {
    "django": "Django", "djangorestframework": "Django REST framework", "flask": "Flask",
    "fastapi": "FastAPI", "celery": "Celery", "sqlalchemy": "SQLAlchemy", "psycopg2": "PostgreSQL",
    "psycopg2-binary": "PostgreSQL", "psycopg": "PostgreSQL", "asyncpg": "PostgreSQL",
    "pymysql": "MySQL", "mysqlclient": "MySQL", "pymongo": "MongoDB", "motor": "MongoDB",
    "redis": "Redis", "pytest": "pytest", "boto3": "AWS", "openai": "OpenAI API",
    "anthropic": "Anthropic API", "langchain": "LangChain", "tensorflow": "TensorFlow",
    "torch": "PyTorch", "scikit-learn": "scikit-learn", "pandas": "pandas", "numpy": "NumPy",
    "transformers": "Hugging Face Transformers", "opencv-python": "OpenCV",
    "opencv-python-headless": "OpenCV", "matplotlib": "Matplotlib", "streamlit": "Streamlit",
    "selenium": "Selenium", "scrapy": "Scrapy", "stripe": "Stripe", "twilio": "Twilio",
    "sentry-sdk": "Sentry", "elasticsearch": "Elasticsearch", "firebase-admin": "Firebase",
    "playwright": "Playwright", "supabase": "Supabase",
}
_PYPI_PREFIXES = {"google-cloud-": "Google Cloud", "langchain-": "LangChain", "azure-": "Azure"}
# Go modules (go.mod), by module-path prefix.
_GO = {
    "github.com/gin-gonic/gin": "Gin", "github.com/labstack/echo": "Echo", "github.com/gofiber/fiber": "Fiber",
    "gorm.io/gorm": "GORM", "github.com/redis/go-redis": "Redis", "github.com/go-redis/redis": "Redis",
    "github.com/lib/pq": "PostgreSQL", "github.com/jackc/pgx": "PostgreSQL",
    "go.mongodb.org/mongo-driver": "MongoDB", "github.com/aws/aws-sdk-go": "AWS",
    "cloud.google.com/go": "Google Cloud",
}
_CARGO = {"actix-web": "Actix Web", "axum": "Axum", "rocket": "Rocket", "tokio": "Tokio", "diesel": "Diesel",
          "redis": "Redis", "mongodb": "MongoDB"}
_GEMS = {"rails": "Ruby on Rails", "sinatra": "Sinatra", "rspec": "RSpec", "rspec-rails": "RSpec",
         "sidekiq": "Sidekiq", "pg": "PostgreSQL", "mysql2": "MySQL", "redis": "Redis"}
_COMPOSER = {"laravel/framework": "Laravel", "phpunit/phpunit": "PHPUnit"}
# Text markers in JVM builds (pom.xml, build.gradle[.kts]) and .NET projects (*.csproj).
_JVM_MARKERS = (
    ("spring-boot", "Spring Boot"), ("com.android.application", "Android"),
    ("com.android.library", "Android"), ("androidx.compose", "Jetpack Compose"),
    ("hibernate", "Hibernate"), ("junit", "JUnit"), ("postgresql", "PostgreSQL"),
    ("mysql-connector", "MySQL"), ("mongodb", "MongoDB"), ("kafka", "Kafka"), ("firebase", "Firebase"),
)
_DOTNET_MARKERS = (
    ("microsoft.aspnetcore", "ASP.NET Core"), ("microsoft.entityframeworkcore", "Entity Framework"),
    ("xunit", "xUnit"),
)

# Files and folders that name a technology by being there.
_FILES = {
    "dockerfile": "Docker", "docker-compose.yml": "Docker Compose", "docker-compose.yaml": "Docker Compose",
    "compose.yml": "Docker Compose", "compose.yaml": "Docker Compose", ".gitlab-ci.yml": "GitLab CI",
    "jenkinsfile": "Jenkins", "bitbucket-pipelines.yml": "Bitbucket Pipelines",
    "azure-pipelines.yml": "Azure Pipelines", ".travis.yml": "Travis CI",
    "serverless.yml": "Serverless Framework", "serverless.yaml": "Serverless Framework",
    "firebase.json": "Firebase", "vercel.json": "Vercel", "netlify.toml": "Netlify", "render.yaml": "Render",
    "procfile": "Heroku", "fly.toml": "Fly.io", "cloudbuild.yaml": "Google Cloud Build",
    "cloudbuild.yml": "Google Cloud Build", "chart.yaml": "Helm", "nginx.conf": "Nginx",
    # (No "Podfile": every Flutter / React Native app has one; it says nothing.)
    "schema.prisma": "Prisma", "angular.json": "Angular", "manage.py": "Django",
}
_FILE_PREFIXES = {
    "dockerfile.": "Docker", "next.config.": "Next.js", "nuxt.config.": "Nuxt", "svelte.config.": "Svelte",
    "astro.config.": "Astro", "tailwind.config.": "Tailwind CSS", "vite.config.": "Vite",
    "capacitor.config.": "Capacitor", "jest.config.": "Jest", "vitest.config.": "Vitest",
    "cypress.config.": "Cypress", "playwright.config.": "Playwright", "webpack.config.": "Webpack",
}
_FILE_SUFFIXES = {".tf": "Terraform", ".dockerfile": "Docker"}
_DIRS = {".circleci": "CircleCI", "k8s": "Kubernetes", "kubernetes": "Kubernetes"}

#: Dependency files read for their contents.
_MANIFESTS = (
    "package.json", "pubspec.yaml", "requirements.txt", "pyproject.toml", "pipfile", "go.mod",
    "cargo.toml", "pom.xml", "build.gradle", "build.gradle.kts", "gemfile", "composer.json",
)
#: Folders not worth opening (build output, vendored code, assets).
_SKIP_DIRS = {
    "node_modules", ".git", "vendor", "dist", "build", "out", "target", ".next", ".nuxt", ".idea",
    ".vscode", "coverage", "public", "static", "assets", "images", "img", "fonts", "docs", "doc",
    "test", "tests", "__tests__", "spec", "migrations", "scripts", "bin", ".dart_tool", "__pycache__",
}
#: Folders opened first (where a monorepo keeps its projects), and the ones
#: whose sub-folders are opened too.
_PREFERRED_DIRS = (
    "frontend", "backend", "client", "server", "web", "app", "api", "mobile", "admin", "dashboard",
    "android", "ios", ".github", "infra", "infrastructure", "deploy", "terraform", "k8s", "helm",
    "docker", "functions", "prisma", "packages", "apps", "services",
)
_CONTAINER_DIRS = {"packages", "apps", "services", "projects", "libs", "modules"}
_DIRS_PER_REPO = 12
_SUBDIRS_PER_CONTAINER = 10

# ---------------------------------------------------------------------------
# Reading dependency files
# ---------------------------------------------------------------------------

_REQUIREMENT_NAME = re.compile(r"[\s\[<>=!~;@(]")


def _requirement(line: str) -> str:
    """'Django[bcrypt]>=4.2 ; python_version>"3"' -> 'django'."""
    name = _REQUIREMENT_NAME.split(line.strip(), maxsplit=1)[0]
    return name.lower().replace("_", "-")


def _pypi(names: set[str]) -> set[str]:
    found = set()
    for name in names:
        if name in _PYPI:
            found.add(_PYPI[name])
        else:
            found.update(tech for prefix, tech in _PYPI_PREFIXES.items() if name.startswith(prefix))
    return found


def _from_npm(text: str) -> set[str]:
    try:
        data = json.loads(text)
    except ValueError:
        return set()
    if not isinstance(data, dict):
        return set()
    names: set[str] = set()
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        deps = data.get(key)
        if isinstance(deps, dict):
            names.update(str(name) for name in deps)
    found = {_NPM[name] for name in names if name in _NPM}
    found.update(tech for name in names for prefix, tech in _NPM_PREFIXES.items() if name.startswith(prefix))
    return found


def _from_pubspec(text: str) -> set[str]:
    found = {"Flutter"} if re.search(r"^\s*sdk:\s*flutter\b", text, re.MULTILINE) else set()
    found.update(_PUB[name] for name in re.findall(r"^  ([A-Za-z0-9_]+):", text, re.MULTILINE) if name in _PUB)
    return found


def _from_requirements(text: str) -> set[str]:
    names = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line and not line.startswith(("-", "git+", "http:", "https:")):
            names.add(_requirement(line))
    return _pypi(names)


def _from_toml(text: str, kind: str) -> set[str]:
    try:
        data = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError):
        return set()
    if kind == "cargo.toml":
        names = {str(n) for key in ("dependencies", "dev-dependencies") for n in (data.get(key) or {})}
        return {_CARGO[n] for n in names if n in _CARGO}
    names: set[str] = set()
    if kind == "pipfile":
        names = {str(n).lower() for key in ("packages", "dev-packages") for n in (data.get(key) or {})}
    else:  # pyproject.toml: PEP 621 and Poetry
        project = data.get("project") or {}
        requirements = list(project.get("dependencies") or [])
        for extra in (project.get("optional-dependencies") or {}).values():
            requirements.extend(extra or [])
        names = {_requirement(str(r)) for r in requirements}
        poetry = (data.get("tool") or {}).get("poetry") or {}
        tables = [poetry.get("dependencies") or {}, poetry.get("dev-dependencies") or {}]
        tables += [(group or {}).get("dependencies") or {} for group in (poetry.get("group") or {}).values()]
        names.update(str(n).lower().replace("_", "-") for table in tables for n in table)
    return _pypi(names)


def _from_go_mod(text: str) -> set[str]:
    modules = re.findall(r"^\s*(?:require\s+)?([a-z0-9.\-]+\.[a-z]{2,}/\S+)\s+v\d", text, re.MULTILINE)
    return {tech for module in modules for prefix, tech in _GO.items() if module.startswith(prefix)}


def _from_markers(text: str, markers: tuple[tuple[str, str], ...]) -> set[str]:
    low = text.lower()
    return {tech for marker, tech in markers if marker in low}


def _from_gemfile(text: str) -> set[str]:
    gems = re.findall(r"""^\s*gem\s+['"]([^'"]+)['"]""", text, re.MULTILINE)
    return {_GEMS[g] for g in gems if g in _GEMS}


def _from_composer(text: str) -> set[str]:
    try:
        data = json.loads(text)
    except ValueError:
        return set()
    if not isinstance(data, dict):
        return set()
    names = {str(n) for key in ("require", "require-dev") for n in (data.get(key) or {})}
    found = {_COMPOSER[n] for n in names if n in _COMPOSER}
    if any(n.startswith("symfony/") for n in names):
        found.add("Symfony")
    return found


def technologies_in_file(name: str, text: str) -> set[str]:
    """The technologies a dependency file declares (``name`` is its file name)."""
    low = name.lower()
    text = text[:300_000]  # a dependency file is small; anything huge is not one
    if low == "package.json":
        return _from_npm(text)
    if low == "pubspec.yaml":
        return _from_pubspec(text)
    if low.startswith("requirements") and low.endswith(".txt"):
        return _from_requirements(text)
    if low in ("pyproject.toml", "pipfile", "cargo.toml"):
        return _from_toml(text, low)
    if low == "go.mod":
        return _from_go_mod(text)
    if low in ("pom.xml", "build.gradle", "build.gradle.kts"):
        return _from_markers(text, _JVM_MARKERS)
    if low.endswith(".csproj"):
        return _from_markers(text, _DOTNET_MARKERS)
    if low == "gemfile":
        return _from_gemfile(text)
    if low == "composer.json":
        return _from_composer(text)
    return set()


def technologies_in_listing(entries: list[tuple[str, str]], subfolders: dict[str, list[str]] | None = None) -> set[str]:
    """Technologies named by files and folders being there. ``entries`` is a
    folder's (name, "blob" | "tree") list; ``subfolders`` the names inside
    some of its folders (".github" -> ["workflows"])."""
    found: set[str] = set()
    for name, kind in entries:
        low = name.lower()
        if kind == "tree":
            if low in _DIRS:
                found.add(_DIRS[low])
            continue
        if low in _FILES:
            found.add(_FILES[low])
        found.update(tech for prefix, tech in _FILE_PREFIXES.items() if low.startswith(prefix))
        found.update(tech for suffix, tech in _FILE_SUFFIXES.items() if low.endswith(suffix))
    for folder, names in (subfolders or {}).items():
        if folder.lower() == ".github" and "workflows" in {n.lower() for n in names}:
            found.add("GitHub Actions")
    return found


#: The frameworks a project is built on: listed first for a repository (and
#: in the report's "main stack"), before the libraries used with them.
CORE_FRAMEWORKS = frozenset({
    "React", "Next.js", "Vue.js", "Nuxt", "Angular", "Svelte", "SvelteKit", "Astro", "Electron",
    "Express", "NestJS", "Fastify", "Koa", "Hapi", "Django", "Flask", "FastAPI", "Spring Boot",
    "Ruby on Rails", "Laravel", "Symfony", "Gin", "Echo", "Fiber", "Actix Web", "Axum", "Rocket",
    "ASP.NET Core", "Flutter", "React Native", "Expo", "Ionic", "Android",
})


def with_implied(found: set[str]) -> list[str]:
    """``found`` plus what it implies: core frameworks first, then the rest
    in catalog order (unknown names last)."""
    found = set(found) | {_IMPLIES[t] for t in found if t in _IMPLIES}
    order = {tech: i for i, tech in enumerate(CATEGORY_OF)}
    return sorted(found, key=lambda t: (t not in CORE_FRAMEWORKS, order.get(t, len(order)), t))


def _is_manifest(name: str) -> bool:
    low = name.lower()
    return low in _MANIFESTS or low.endswith(".csproj") or (low.startswith("requirements") and low.endswith(".txt"))


def _pick_dirs(names: list[str]) -> list[str]:
    """The folders of a repository worth opening, most promising first."""
    usable = [n for n in names if n.lower() not in _SKIP_DIRS]
    rank = {name: i for i, name in enumerate(_PREFERRED_DIRS)}
    return sorted(usable, key=lambda n: (rank.get(n.lower(), len(rank)), n.lower()))[:_DIRS_PER_REPO]


# ---------------------------------------------------------------------------
# Collection (batched GraphQL)
# ---------------------------------------------------------------------------

#: Repositories per query.
STACK_BATCH = 6
_LANGUAGES = "languages(first: 10, orderBy: {field: SIZE, direction: DESC}) { edges { size node { name } } }"
_TREE = "... on Tree { entries { name type } }"
_BLOB = "... on Blob { text }"


def _entries(obj: object) -> list[tuple[str, str]]:
    if not isinstance(obj, dict):
        return []
    return [
        (str(e.get("name") or ""), str(e.get("type") or ""))
        for e in obj.get("entries") or [] if isinstance(e, dict) and e.get("name")
    ]


class _RepoScan:
    """What one repository's scan has found so far, and what it asks next."""

    def __init__(self, repo: RepoRecord, client: GitHubClient) -> None:
        self.repo = repo
        self.client = client
        self.found: set[str] = set()
        self.readable = False
        self.requests: list[tuple[str, str, str]] = []  # (alias, kind "file"|"dir", path)

    def walk(self) -> Walk | None:
        if not self.requests:
            return None
        body = " ".join(
            f"{alias}: object(expression: {_str('HEAD:' + path)}) {{ {_BLOB if kind == 'file' else _TREE} }}"
            for alias, kind, path in self.requests
        )
        return Walk(self.client, _repo_root(self.repo.full_name), lambda _size, _after: body, _single)

    def ask(self, kind: str, path: str) -> None:
        self.requests.append((f"{kind[0]}{len(self.requests)}", kind, path))


async def _round(scans: list[_RepoScan], inflight: int) -> dict[_RepoScan, dict]:
    """Send every scan's pending requests (batched); returns each answered scan's objects."""
    walks = {scan: walk for scan in scans if (walk := scan.walk()) is not None}
    if walks:
        await run_walks(list(walks.values()), STACK_BATCH, inflight=inflight)
    answers: dict[_RepoScan, dict] = {}
    for scan, walk in walks.items():
        if walk.error or not walk.nodes:
            log.debug("technologies of %s: %s", scan.repo.full_name, walk.error or "no answer")
        else:
            answers[scan] = walk.nodes[0]
    return answers


def _apply(scan: _RepoScan, answer: dict) -> dict[str, list[tuple[str, str]]]:
    """Use a round's answers: read the dependency files into ``scan.found``
    and return the folder listings ({folder path: entries})."""
    listings: dict[str, list[tuple[str, str]]] = {}
    for alias, kind, path in scan.requests:
        obj = answer.get(alias)
        if kind == "file" and isinstance(obj, dict) and isinstance(obj.get("text"), str):
            scan.found |= technologies_in_file(path.rsplit("/", 1)[-1], obj["text"])
        elif kind == "dir":
            listings[path] = _entries(obj)
    scan.requests = []
    return listings


async def collect_stacks(
    repos: list[RepoRecord],
    client_for: Callable[[RepoRecord], GitHubClient],
    *,
    inflight: int,
) -> int:
    """Read the languages and technologies of ``repos`` into each record
    (``languages``, ``technologies``, ``stack_read``). Returns how many could
    not be read. Never raises: technologies are a bonus, not a count."""
    scans = [_RepoScan(repo, client_for(repo)) for repo in repos]
    if not scans:
        return 0
    try:
        with _Progress("Technologies", len(scans)) as bar:
            # Round 1: languages and the top-level listing.
            walks = {
                scan: Walk(
                    scan.client, _repo_root(scan.repo.full_name),
                    lambda _size, _after: f"{_LANGUAGES} root: object(expression: \"HEAD:\") {{ {_TREE} }}",
                    _single,
                )
                for scan in scans
            }
            await run_walks(list(walks.values()), STACK_BATCH, inflight=inflight)
            for scan, walk in walks.items():
                bar.update()
                if walk.error or not walk.nodes:
                    log.info("technologies of %s could not be read: %s", scan.repo.full_name, walk.error or "no answer")
                    continue
                obj = walk.nodes[0]
                scan.readable = True
                edges = ((obj.get("languages") or {}).get("edges") or [])
                scan.repo.languages = {
                    str((e.get("node") or {}).get("name")): int(e.get("size") or 0)
                    for e in edges if isinstance(e, dict) and (e.get("node") or {}).get("name")
                }
                root = _entries(obj.get("root"))
                dirs = [name for name, kind in root if kind == "tree"]
                scan.found |= technologies_in_listing(root)
                for name, kind in root:
                    if kind == "blob" and _is_manifest(name):
                        scan.ask("file", name)
                for name in _pick_dirs(dirs):
                    scan.ask("dir", name)

            # Round 2: root dependency files and the chosen folders' listings.
            answers = await _round(scans, inflight)
            for scan, answer in answers.items():
                for folder, entries in _apply(scan, answer).items():
                    names = [name for name, _kind in entries]
                    scan.found |= technologies_in_listing(entries, {folder: names})
                    for name, kind in entries:
                        if kind == "blob" and _is_manifest(name):
                            scan.ask("file", f"{folder}/{name}")
                    if folder.lower() in _CONTAINER_DIRS:
                        children = [n for n, k in entries if k == "tree" and n.lower() not in _SKIP_DIRS]
                        for child in sorted(children)[:_SUBDIRS_PER_CONTAINER]:
                            scan.ask("dir", f"{folder}/{child}")

            # Round 3: dependency files in those folders, sub-project listings.
            answers = await _round(scans, inflight)
            for scan, answer in answers.items():
                for folder, entries in _apply(scan, answer).items():
                    scan.found |= technologies_in_listing(entries)
                    for name, kind in entries:
                        if kind == "blob" and _is_manifest(name):
                            scan.ask("file", f"{folder}/{name}")

            # Round 4: the sub-projects' dependency files.
            for scan, answer in (await _round(scans, inflight)).items():
                _apply(scan, answer)
    except Exception as exc:  # noqa: BLE001 - never fatal
        log.warning("Technologies could not be read (%s: %s); the report goes on without them.",
                    type(exc).__name__, exc)
    unread = 0
    for scan in scans:
        scan.repo.stack_read = scan.readable
        scan.repo.technologies = with_implied(scan.found) if scan.readable else []
        unread += not scan.readable
    log.info("technologies read for %d of %d repositories", len(scans) - unread, len(scans))
    return unread
