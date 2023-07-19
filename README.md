# Conan Dependency Submission for GitHub

This repository contains a GitHub Action that can be used to submit details of a Conan package to GitHub's Dependency Graph.

## Usage

To use this action, add the following to your workflow:

```yaml
...
```

## Inputs

...

## FAQ

### Why is this needed?

GitHub's Dependency Graph is a great way to see what packages your project depends on. However, by default it only works for supported packages that are understood statically, which doesn't include `conanfile.txt` nor `conanfile.py`.

### Where does it get platform specific details from?

It uses the Actions runner to run `conan`, and takes details from that platform.

If you do not use the default Actions runner to build your Conan package, then use an Actions runner that is the same platform as your build system, to ensure a match.

### Dependabot isn't showing any alerts - why?

Dependabot needs to know about an ecosytem before it can show alerts for it. At the time of writing, it doesn't support Conan.

Dependabot, at the time of writing, also only shows alerts for curated advisories in the [GitHub Advisory Database](https://github.com/advisories), and at present there are none for Conan packages.

Why bother with this then? Well, it's a good idea to submit your dependencies to the Dependency Graph, so that when Dependabot does support Conan, it will already have the data it needs.

### What use can I make of this if Dependabot doesn't support Conan?

There are workarounds you can use to match Dependency Graph content to local advisories, such as by using the [GitHub Field GHAS Toolkit](https://github.com/GeekMasher/ghas-toolkit).

It's also a way of generating a Software Bill of Materials (SBOM) for your project.

## Background

Conan uses a [central index](https://github.com/conan-io/conan-center-index) of packages. This is used by the `conan` client to find packages.

This Action install the `conan` tool, wraps it, parses the results, and submits them to the [Dependency Submission API](https://docs.github.com/en/code-security/supply-chain-security/understanding-your-software-supply-chain/using-the-dependency-submission-api).
