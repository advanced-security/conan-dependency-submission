#!/usr/bin/env python3

"""
Generate a graph from Conan packages,
and submit to the GitHub Dependency Graph using the Submission API.

See for reference:
https://docs.conan.io/2/reference/conanfile/attributes.html
https://github.com/package-url/purl-spec/blob/master/PURL-TYPES.rst#conan
https://docs.github.com/en/enterprise-cloud@latest/rest/dependency-graph/dependency-submission?apiVersion=2022-11-28
https://docs.github.com/en/enterprise-server@3.9/rest/dependency-graph/dependency-submission?apiVersion=2022-11-28
"""

import os
import argparse
import logging
import json
from typing import Optional, Any, Tuple, Sequence
import subprocess
from urllib.parse import urlparse, quote_plus
import uuid
import datetime
import pathlib

from furl import furl  # type: ignore
import git
from attrs import define
import requests
import anytree  # type: ignore


LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)


@define
class Package(anytree.NodeMixin):
    """Conan package"""

    id_: int
    name: str
    version: str
    sha: str
    scope: Optional[str]
    relationship: Optional[str]
    metadata: dict[str, str]
    dependencies: list[int]

    def __repr__(self):
        return f"Package({self.name}, {self.version})"


def find_conanfile(repo: git.Repo) -> Optional[Any]:
    """Find a conanfile.py or conanfile.txt in the repo"""
    for item in repo.tree().traverse():
        if item.name == "conanfile.py" or item.name == "conanfile.txt":
            return item
    return None


def get_graph(
    conan_path: str,
    repo: git.Repo,
    conanfile: Optional[Any] = None,
    graphfile: Optional[str] = None,
) -> Tuple[Optional[dict], Optional[str]]:
    """
    Run conan graph info to generate graph,
    call out to external conan process using subprocess.run()
    """
    if conanfile is None:
        conanfile = find_conanfile(repo)
        if conanfile is None:
            if graphfile is not None:
                try:
                    with open(graphfile, "r") as file:
                        graph = json.load(file)
                        return graph, graphfile
                except Exception as err:
                    LOG.error("Cannot find graphfile: %s", err)
                    return None, None
            LOG.error("Cannot find conanfile")
            return None, None

    # conan graph info conanfile.py|conanfile.txt --format=json > graph.json
    # clear the env so we don't leak secrets, etc. to the untrusted process
    # TODO: can I do this with the Conan Python API instead?
    process = subprocess.run(
        [conan_path, "graph", "info", conanfile.abspath, "--format=json"],
        capture_output=True,
        env={"PATH": os.environ["PATH"]},
    )
    stdout, stderr = (
        process.stdout.decode(encoding="utf-8").rstrip(),
        process.stderr.decode(encoding="utf-8").rstrip(),
    )
    if process.returncode != 0:
        if stderr.startswith("ERROR: No such file or directory:"):
            LOG.error("Cannot find conanfile: %s", conanfile.abspath)
            return None, None
        # just carry on with other "errors" - some conflicts raise errors, but still generate a graph
        # TODO: catch more fatal errors, e.g. permission denied
    try:
        graph = json.loads(stdout)
        return graph, conanfile
    except json.JSONDecodeError as err:
        LOG.error("conan graph info output is not valid JSON: %s", err)
        return None, None


def get_conan_version(conan_path: str) -> Optional[str]:
    """Get the version of Conan."""
    process = subprocess.run([conan_path, "--version"], capture_output=True)
    stdout = process.stdout.decode(encoding="utf-8").rstrip()
    stderr = process.stderr.decode(encoding="utf-8").rstrip()
    if process.returncode != 0:
        LOG.error("conan --version failed: %s", stderr)
        return None
    try:
        version = stdout.split()[1]
        return version
    except IndexError as err:
        LOG.error("conan --version output is not valid: %s", err)
        return None


def make_dependency(
    package: Package,
) -> dict[str, str | dict[str, str] | Sequence[str]]:
    """Create a Dependency Graph package from a Conan package."""
    data: dict[str, str | dict[str, str] | Sequence[str]] = {
        "package_url": make_purl(package),
        "dependencies": list(
            {make_purl(child, dep=True) for child in package.children}
        ),
    }

    if package.scope is not None:
        data["scope"] = package.scope

    if package.relationship is not None:
        data["relationship"] = package.relationship

    for key in conan_not_ok_keys:
        if key in package.metadata:
            data["metadata"] = {}
            data["metadata"][key] = package.metadata[key]  # type: ignore

    return data


# keys that are mapped to different names
conan_mapped_metadata_keys = {"sha": "rrev"}
# keys that are complex objects
conan_complex_keys = (
    "ref",
    "settings",
    "cpp_info",
    "options_definitions",
    "default_options",
    "options",
)
# keys already handled by previous processing
conan_handled_keys = ("dependencies",)
# keys not OK for purl submission in Dependency Graph
conan_not_ok_keys = ("386",)


def make_purl(package: Package, dep: bool = False) -> str:
    """Create a Package URL from a Conan package reference."""
    purl = furl()

    purl.scheme = "pkg"
    purl.path = f"conan/{package.name}@{package.version}"

    if not dep:
        query = {}

        for key, value in package.metadata.items():
            if (
                key not in conan_mapped_metadata_keys
                and key not in conan_handled_keys
                and key not in conan_not_ok_keys
            ):
                query[key] = value

        for key, mapped_name in conan_mapped_metadata_keys.items():
            if key in package.metadata:
                query[mapped_name] = package.get(key)

        purl.set(query_params=query)

    return purl.url


def process_graph(graph: dict, packages: dict[int, Package], dep: bool = False) -> None:
    """Process the Conan graph entries into a custom format."""
    for index, entry in graph.items():
        try:
            if "id" not in entry:
                continue
            id_ = int(entry["id"])

            if "ref" not in entry:
                continue
            ref = entry["ref"]

            if "/" in ref:
                name, remainder = ref.split("/")
            else:
                name = ref
                remainder = None

            if remainder is not None and "#" in remainder:
                version, sha = remainder.split("#")
            else:
                version = remainder
                sha = None

            metadata = {}

            if "settings" in entry:
                for key, value in entry["settings"].items():
                    if value is not None:
                        metadata[key] = value

            if "options" in entry:
                for key, value in entry["options"].items():
                    if value is not None:
                        metadata[key] = value

            for key, value in entry.items():
                if key not in conan_complex_keys and value is not None:
                    metadata[key] = value

            # TODO: consider how to put cpp_info in, given that it deeply nested

            scope = None
            if "context" in metadata:
                scope = "development" if metadata["context"] == "build" else "runtime"

            if scope is not None:
                LOG.debug("%s/%s scope: %s", name, version, scope)

            dependency_indexes = [
                int(id) for id in entry.get("dependencies", {}).keys()
            ]

            package = Package(
                id_=id_,
                name=name,
                version=version,
                sha=sha,
                scope=scope,
                relationship=None,
                metadata=metadata,
                dependencies=dependency_indexes,
            )

            # store dependency packages in a dict, indexed by id, so we can index into them later to retrieve relationship
            packages[int(index)] = package
        except KeyError as err:
            LOG.error("graph is missing key: %s", err)


def add_relationship(tree: anytree.AnyNode) -> None:
    """Add the relationship between packages to the tree."""
    for package in tree.descendants:
        package.relationship = (
            "direct" if getattr(package.parent, "id_", 0) == 0 else "indirect"
        )

        LOG.debug(
            "setting relationship for %s to %s", package.name, package.relationship
        )


def build_tree(
    tree: anytree.AnyNode, packages: dict[int, Package], index: int = 0
) -> None:
    """Build the tree of packages."""
    if package := packages.get(index):
        package.parent = tree

        LOG.debug("adding %s to tree", package.name)
        LOG.debug("parent: %s", package.parent.name)
        LOG.debug("child IDs: %s", package.dependencies)

        for child_index in package.dependencies:
            build_tree(package, packages, child_index)
    else:
        LOG.error("no package for index %s", index)


def submit_graph(
    server: str,
    repo: git.Repo,
    graph: dict,
    conan_path: str,
    conanfile: Any,
    dry_run: bool = False,
) -> None:
    """Submit the graph to the GitHub Dependency Graph using the Submission API."""
    repo_commit = repo.head.commit.hexsha
    repo_ref = f"refs/heads/{str(repo.head.ref)}"

    LOG.debug("repo_commit: %s", repo_commit)

    packages_dict: dict[int, Package] = {}
    packages_tree = anytree.AnyNode(name="packages")

    process_graph(graph["graph"]["nodes"], packages_dict)
    build_tree(packages_tree, packages_dict)
    add_relationship(packages_tree)

    LOG.debug(anytree.RenderTree(packages_tree))

    # submit to GitHub using the Submission API
    gh_token = os.environ.get("GITHUB_TOKEN", None)
    if gh_token is None:
        LOG.error("GITHUB_TOKEN is not set")
        return

    owner, reponame = urlparse(repo.remote().url).path.rstrip(".git").split("/")[1:]

    # set GitHub API headers
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {gh_token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    # clear the GITHUB_TOKEN
    gh_token = None
    os.environ["GITHUB_TOKEN"] = ""

    graph = {
        "version": 0,  # TODO: should we generate this somehow?
        "sha": repo_commit,
        "ref": repo_ref,
        "job": {
            "correlator": "conan-dependency-submission",
            "id": uuid.uuid4().hex,
        },
        "detector": {
            "name": "conan",
            "version": get_conan_version(conan_path),
            "url": "https://conan.io/",
        },
        # get current time
        "scanned": datetime.datetime.now().isoformat(),
        "manifests": {
            conanfile.name: {
                "name": conanfile.name,
                "file": {
                    "source_location": pathlib.Path(conanfile.abspath)
                    .relative_to(repo.working_dir)
                    .as_posix(),
                },
                "resolved": {
                    package.name: make_dependency(package)
                    for package in packages_dict.values()
                    if package.name != "conanfile"
                },
            }
        },
    }

    LOG.debug("graph: %s", json.dumps(graph, indent=2))

    host_and_path = (
        f"api.github.com"
        if server in ("github.com", "api.github.com")
        else f"{quote_plus(server)}/api/v3"
    )
    submission_url = f"https://{host_and_path}/repos/{quote_plus(owner)}/{quote_plus(reponame)}/dependency-graph/snapshots"

    LOG.debug("Submitting to %s", submission_url)

    request = requests.Request("POST", submission_url, headers=headers, json=graph)
    prepared = request.prepare()
    session = requests.Session()

    LOG.debug(prepared.headers)
    LOG.debug(json.dumps(graph, indent=2))

    if not dry_run:
        response = session.send(prepared)
        LOG.debug("response: %s", response.json())


def add_args(parser: argparse.ArgumentParser) -> None:
    """Add command line arguments to parser"""
    parser.add_argument("repo", help="GitHub repository path")
    parser.add_argument(
        "--github-server", default="github.com", required=False, help="GitHub server"
    )
    parser.add_argument(
        "--conan-path", default="conan", required=False, help="Path to conan executable"
    )
    parser.add_argument(
        "--conanfile", required=False, help="Path to conanfile.py or conanfile.txt"
    )
    parser.add_argument(
        "--graph", required=False, help="Path to Conan build graph JSON file"
    )
    parser.add_argument(
        "--debug", "-d", action="store_true", help="Enable debug output"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not submit to GitHub server - just a dry-run",
    )


def main() -> None:
    """Main entry point"""
    parser = argparse.ArgumentParser(description=__doc__)
    add_args(parser)
    args = parser.parse_args()

    logging.basicConfig()

    if args.debug:
        LOG.setLevel(logging.DEBUG)

    repo = git.Repo(args.repo)
    remote = repo.remote()
    if remote is None:
        LOG.error("Cannot find remote for repo: %s", args.repo)
        return

    remote_url = urlparse(remote.url)
    if remote_url.scheme != "https" or remote_url.netloc != args.github_server:
        LOG.error("Remote is not a GitHub repo: %s", remote)
        return
    owner, reponame = remote_url.path.rstrip(".git").split("/")[1:]

    graph, conanfile = get_graph(args.conan_path, repo, args.conanfile)

    # LOG.debug(json.dumps(graph, indent=2))

    if graph is not None:
        submit_graph(
            args.github_server, repo, graph, args.conan_path, conanfile, args.dry_run
        )


if __name__ == "__main__":
    main()
