# pure-python-git-shim

A tiny companion package to [pure-python-git](https://pypi.org/project/pure-python-git/)
whose only purpose is to install a `git` console-script that calls
`pythongit.cli:main`.

Install via the `pure-python-git[git]` extra:

```bash
pip install "pure-python-git[git]"
```

That pulls in `pure-python-git` and this shim, giving you both `pygit` and
`git` commands. Uninstalling this shim (`pip uninstall pure-python-git-shim`)
cleanly removes the `git` console-script and leaves `pygit` working.

The point of having this as a separate distribution is that
`[project.scripts]` in `pyproject.toml` can't be gated by extras — every
declared entry point gets installed unconditionally. By moving the `git`
entry point into a separate distribution, we make the drop-in behavior an
opt-in choice rather than something that silently happens on every
`pip install pure-python-git`.

See pure-python-git's main README for usage.
