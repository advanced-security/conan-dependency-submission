#!/usr/bin/env python3

"""
Generate a pkglist.json from Conan packages,
and submit to the GitHub Dependency Graph using the Submission API.
"""

import os
import argparse
import logging
import json
from typing import Optional, Any, Tuple
import subprocess
import urllib
import git
from attrs import define
import requests
import uuid
import datetime


LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)


@define(frozen=True)
class Ref:
    """Conan package reference"""
    package: str
    version: str
    sha: str


def find_conanfile(repo: git.Repo) -> Optional[Any]:
    """Find a conanfile.py or conanfile.txt in the repo"""
    for item in repo.tree().traverse():
        if item.name == "conanfile.py" or item.name == "conanfile.txt":
            return item
    return None


def get_pkglist(conan_path: str, repo: git.Repo, conanfile: Optional[Any] = None) -> Tuple[Optional[dict], Optional[str]]:
    """
    Run conan graph info to generate pkglist.json,
    call out to external conan process using subprocess.run()
    """
    if conanfile is None:
        conanfile = find_conanfile(repo)
        if conanfile is None:
            LOG.error("Cannot find conanfile")
            return None, None

    # conan graph info conanfile.py|conanfile.txt --format=json > pkglist.json
    process = subprocess.run([conan_path, "graph", "info", conanfile.abspath, "--format=json"], capture_output=True)
    stdout, stderr = process.stdout.decode(encoding="utf-8").rstrip(), process.stderr.decode(encoding="utf-8").rstrip()
    if process.returncode != 0:
        if stderr.startswith("ERROR: No such file or directory:"):
            LOG.error("Cannot find conanfile: %s", conanfile.abspath)
            return None, None
        # just carry on with other "errors" - some conflicts raise errors, but still generate a pkglist.json
    try:
        pkglist = json.loads(stdout)
        return pkglist, conanfile
    except json.JSONDecodeError as err:
        LOG.error("conan graph info output is not valid JSON: %s", err)
        return None, None


def get_conan_version(conan_path: str) -> Optional[str]:
    """Get the version of Conan"""
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


def make_purl(ref: Ref) -> str:
    """Create a Package URL from a Conan package reference"""
    return f"pkg:conan/{ref.package}@{ref.version}#{ref.sha}"


def submit_pkglist(repo: git.Repo, pkglist: dict, conan_path: str, conanfile: Any) -> None:
    """Submit the pkglist.json to the GitHub Dependency Graph using the Submission API."""
    refs = set()

    repo_commit = repo.head.commit.hexsha
    repo_ref = f"refs/heads/{str(repo.head.ref)}"

    LOG.debug("repo_commit: %s", repo_commit)

    try:
        for key, value in pkglist["graph"]["nodes"].items():
            ref = value["ref"]
            if ref != "conanfile":
                package, remainder = ref.split("/")
                version, sha = remainder.split("#")
                ref_info = Ref(package=package, version=version, sha=sha)
                refs.add(ref_info)
            # TODO: add arch, os, compiler, compiler.version, compiler.libcxx, compiler.cppstd, etc.
        LOG.info(refs)
    except KeyError as err:
        LOG.error("pkglist.json is missing key: %s", err)

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
                    "source_location": conanfile.abspath
                },
                "resolved": {
                    ref.package: make_purl(ref) for ref in refs
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
    parser.add_argument("--pkglist", required=False, help="Path to pkglist.json")
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

    pkglist, conanfile = get_pkglist(args.conan_path, repo, args.conanfile)

    LOG.debug(pkglist)

    if pkglist is not None:
        submit_pkglist(repo, pkglist, args.conan_path, conanfile)


if __name__ == "__main__":
    main()
