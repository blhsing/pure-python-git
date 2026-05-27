# pythongit-git-shim

A tiny companion package to [pythongit](https://github.com/blhsing/pythongit)
whose only purpose is to install a `git` console-script that calls
`pythongit.cli:main`.

Install via the `pythongit[git]` extra:

```bash
pip install "pythongit[git]"
```

That pulls in `pythongit` and this shim, giving you both `pygit` and `git`
commands. Uninstalling this shim (`pip uninstall pythongit-git-shim`) cleanly
removes the `git` console-script and leaves `pygit` working.

The point of having this as a separate distribution is that
`[project.scripts]` in `pyproject.toml` can't be gated by extras — every
declared entry point gets installed unconditionally. By moving the `git`
entry point into a separate distribution, we make the drop-in behavior an
opt-in choice rather than something that silently happens on every
`pip install pythongit`.

See pythongit's main README for usage.
