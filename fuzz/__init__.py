"""Fuzz harnesses for the parsers that take untrusted text (ClusterFuzzLite with Atheris).

`fuzz.targets` holds what each harness checks and imports no Atheris, so the test suite runs
every seed through it; each ``*_fuzzer.py`` hands one target to Atheris. See "Fuzzing" in
``docs/agent/security.md``.
"""
