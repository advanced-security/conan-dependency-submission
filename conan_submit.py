#!/usr/bin/env python3

"""
Generate a graph from Conan packages,
and submit to the GitHub Dependency Graph using the Submission API.
"""

import os
import argparse
import logging
import json
from typing import Optional, Any, Tuple
import subprocess
import urllib
import uuid
import datetime
import pathlib

import git
from attrs import define
import requests
import anytree


LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)


@define
class Package(anytree.NodeMixin):
    """Conan package"""
    name: str
    version: str
    sha: str
    scope: str
    relationship: str
    metadata: Optional[dict[str, str]] = None


def find_conanfile(repo: git.Repo) -> Optional[Any]:
    """Find a conanfile.py or conanfile.txt in the repo"""
    for item in repo.tree().traverse():
        if item.name == "conanfile.py" or item.name == "conanfile.txt":
            return item
    return None


def get_graph(conan_path: str, repo: git.Repo, conanfile: Optional[Any] = None, graphfile: Optional[str] = None) -> Tuple[Optional[dict], Optional[str]]:
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
    process = subprocess.run([conan_path, "graph", "info", conanfile.abspath, "--format=json"], capture_output=True, env={"PATH":os.environ["PATH"]})
    stdout, stderr = process.stdout.decode(encoding="utf-8").rstrip(), process.stderr.decode(encoding="utf-8").rstrip()
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
    if process.returncode != 0:
        LOG.error("conan --version failed: %s", stderr)
        return None
    try:
        version = stdout.split()[1]
        return version
    except IndexError as err:
        LOG.error("conan --version output is not valid: %s", err)
        return None


def make_dependency(package: Package) -> dict:
    """Create a Dependency Graph package from a Conan package."""
    data = {
        "name": package.name,
        "version": package.version,
        "purl": make_purl(package),
        "dependencies": list({make_purl(child) for child in package.children}),
    }

    if package.metadata is not None and package.metadata != {}:
        data["metadata"] = package.metadata

    if package.scope is not None:
        data["scope"] = package.scope

    if package.relationship is not None:
        data["relationship"] = package.relationship

    return data


def make_purl(package: Package) -> str:
    """Create a Package URL from a Conan package reference."""
    return f"pkg:conan/{package.name}@{package.version}{'#'+package.sha if package.sha is not None else ''}"


def process_graph(graph: dict, tree: anytree.AnyNode) -> None:
    """Process the Conan graph into a Tree."""
    for index, entry in graph.items():
        try:
            if "ref" not in entry:
                continue
            ref = entry["ref"]
            if ref != "conanfile":
                name, remainder = ref.split("/")
                try:
                    version, sha = remainder.split("#")
                except ValueError:
                    LOG.debug("no sha for %s", ref)
                    version = remainder
                    sha = None
                # TODO:
                # add arch, os, compiler from "settings"
                # add "license" array
                # add "url" to know package repo this is from
                # we can store 8 domain-specific attributes in the Dependency Graph

                # TODO: pull these out of the "dependencies" keys of each graph node
                # scope = "development" if "build" in entry and entry["build"] == "True" else "runtime"
                # relationship = "direct" if "direct" in entry and entry["direct"] == "True" else "indirect"

                # TODO: build dependency tree from "dependencies" keys of each graph node
                # we can then use this to determine if a package is a child of the top level, or not
                # we might want to check if a package is a child of "conanfile", vs skipping it at present
                metadata = {}

                # only 8 attributes are allowed in the Dependency Graph, and they must be scalar values
                if "settings" in entry:
                    if "os" in entry["settings"]:
                        metadata["os"] = entry["settings"]["os"]
                    if "arch" in entry["settings"]:
                        metadata["arch"] = entry["settings"]["arch"]
                    if "compiler" in entry["settings"]:
                        metadata["compiler"] = entry["settings"]["compiler"] + f"-{entry['settings']['compiler.version']}" if "compiler.version" in entry["settings"] else ""
                    if "compiler.cppstd" in entry["settings"]:
                        metadata["cppstd"] = entry["settings"]["compiler.cppstd"]
                    if "compiler.libcxx" in entry["settings"]:
                        metadata["libcxx"] = entry["settings"]["compiler.libcxx"]
                
                if "url" in entry:
                    metadata["index-url"] = entry["url"]

                if "license" in entry:
                    lic = entry["license"]
                    metadata["license"] = ",".join(lic) if isinstance(lic, list) else lic

                # 1 left!
                if len(metadata.keys()) > 8:
                    LOG.error("too many metadata attributes for %s", ref)

                # TODO: put scope and relationship back in, once we've acquired them by traversing the dependency tree
                package = Package(name=name, version=version, sha=sha, scope=None, relationship=None, metadata=metadata)
                package.parent = tree

                if "dependencies" in entry:
                    process_graph(entry["dependencies"], package)
        except KeyError as err:
            LOG.error("graph is missing key: %s", err)


def submit_graph(repo: git.Repo, graph: dict, conan_path: str, conanfile: Any) -> None:
    """Submit the graph to the GitHub Dependency Graph using the Submission API."""
    packages = anytree.AnyNode(name="packages")

    repo_commit = repo.head.commit.hexsha
    repo_ref = f"refs/heads/{str(repo.head.ref)}"

    LOG.debug("repo_commit: %s", repo_commit)

    process_graph(graph["graph"]["nodes"], packages)

    LOG.info(anytree.RenderTree(packages))

    # submit to GitHub using the Submission API
    gh_token = os.environ.get("GITHUB_TOKEN", None)
    if gh_token is None:
        LOG.error("GITHUB_TOKEN is not set")
        return    

    owner, reponame = urllib.parse.urlparse(repo.remote().url).path.rstrip(".git").split("/")[1:]
    
    # set GitHub API headers
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {gh_token}",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    # clear the GITHUB_TOKEN
    gh_token = None
    os.environ["GITHUB_TOKEN"] = ''

    graph = {
        "version": 0,
        "sha": repo_commit,
        "ref": repo_ref,
        "job": {
            "correlator": "conan",
            "id": uuid.uuid4().hex,
        },
        "detector": {
            "name": "conan",
            "version": get_conan_version(conan_path),
            "url": "https://conan.io/"
        },
        # get current time
        "scanned": datetime.datetime.now().isoformat(),
        "manifests": {
            conanfile.name: {
                "name": conanfile.name,
                "file": {
                    "source_location": pathlib.Path(conanfile.abspath).relative_to(repo.working_dir).as_posix(),
                },
                # TODO: traverse tree properly
                "resolved": {
                    package.name: make_dependency(package) for package in packages.children
                }
            }
        }
    }

    LOG.debug("graph: %s", json.dumps(graph, indent=2))


def add_args(parser: argparse.ArgumentParser) -> None:
    """Add command line arguments to parser"""
    parser.add_argument("repo", help="GitHub repository path")
    parser.add_argument("--github-server", default="github.com", required=False, help="GitHub server")
    parser.add_argument("--conan-path", default="conan", required=False, help="Path to conan executable")
    parser.add_argument("--conanfile", required=False, help="Path to conanfile.py or conanfile.txt")
    parser.add_argument("--graph", required=False, help="Path to Conan build graph JSON file")
    parser.add_argument("--debug", "-d", action="store_true", help="Enable debug output")


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

    remote_url = urllib.parse.urlparse(remote.url)
    if remote_url.scheme != "https" or remote_url.netloc != args.github_server:
        LOG.error("Remote is not a GitHub repo: %s", remote)
        return
    owner, reponame = remote_url.path.rstrip(".git").split("/")[1:]

    graph, conanfile = get_graph(args.conan_path, repo, args.conanfile)

    LOG.debug(json.dumps(graph, indent=2))

    if graph is not None:
        submit_graph(repo, graph, args.conan_path, conanfile)


if __name__ == "__main__":
    main()
